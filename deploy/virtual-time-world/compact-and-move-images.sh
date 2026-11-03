#!/usr/bin/env bash
# Offline storage maintenance; retain company files and filesystem capacities.
set -euo pipefail
umask 077
root=${WORLD_ROOT:-/data/theagentcompany/virtual-time-world}
name=${WORLD_NAME:-tac-virtual-time-world}
destination=${1:?usage: sudo compact-and-move-images.sh /path/on/ssd}
images=$root/images
[[ $EUID == 0 && $destination == /* && ! -e $destination && ! -L $images ]]
[[ $(virsh domstate "$name") == 'shut off' ]] || { echo 'Stop the VM gracefully before disk maintenance' >&2; exit 1; }
if systemctl is-active --quiet virtual-time-world-clock.service; then
  echo 'Stop the clock supervisor before disk maintenance' >&2
  exit 1
fi
command -v virt-sparsify >/dev/null
command -v qemu-img >/dev/null
[[ ! -e $root/images.hdd-retired ]]
# Standard libguestfs performs filesystem-aware discard without resetting data.
for disk in world.qcow2 company-data.qcow2; do
  LIBGUESTFS_BACKEND=direct virt-sparsify --in-place "$images/$disk"
  qemu-img check "$images/$disk"
done
install -d -m 0755 "$(dirname "$destination")"
python3 - "$images" "$(dirname "$destination")" <<'PY'
import os, shutil, sys
from pathlib import Path
required=sum(p.stat().st_blocks*512 for p in Path(sys.argv[1]).iterdir() if p.is_file())
free=shutil.disk_usage(sys.argv[2]).free
if free < required + 8*1024**3:
    raise SystemExit(f'Insufficient destination headroom: {required} allocated bytes, {free} available')
PY
# Require non-rotational destination storage.
lsblk --inverse --noheadings --raw --output TYPE,ROTA "/dev/block/$(findmnt -nr -o MAJ:MIN -T "$(dirname "$destination")")" |
  awk '$1=="disk" {found=1; if ($2!="0") bad=1} END {exit (!found || bad)}'
install -d -m 0755 "$destination"
rsync -aS --exclude=world.qcow2 --exclude=company-data.qcow2 "$images/" "$destination/"
for disk in world.qcow2 company-data.qcow2; do
  qemu-img convert -p -f qcow2 -O qcow2 -S 4k "$images/$disk" "$destination/$disk"
  qemu-img compare -p -f qcow2 -F qcow2 "$images/$disk" "$destination/$disk"
  chown --reference="$images/$disk" "$destination/$disk"
  chmod --reference="$images/$disk" "$destination/$disk"
done
# Keep future guest TRIM operations effective after the move.
python3 - "$name" "$root/config/domain-storage.xml" <<'PYXML'
import subprocess, sys, xml.etree.ElementTree as ET
from pathlib import Path
xml=ET.fromstring(subprocess.check_output(['virsh','dumpxml','--inactive',sys.argv[1]],text=True))
for disk in xml.findall('./devices/disk'):
    driver=disk.find('driver')
    if driver is not None and driver.get('type')=='qcow2':
        driver.set('discard','unmap')
p=Path(sys.argv[2]); p.write_bytes(ET.tostring(xml)); p.chmod(0o600)
subprocess.run(['virsh','define',str(p)],check=True)
PYXML
sync
# Preserve configured image paths with a native persistent bind mount.
# This keeps kernel, clock controller and provisioning paths consistent.
mv "$images" "$root/images.hdd-retired"
install -d -m 0755 "$images"
mount --bind "$destination" "$images"
python3 - "$destination" "$images" <<'PY'
import sys
from pathlib import Path
escape=lambda p:p.replace('\\','\\134').replace(' ','\\040').replace('\t','\\011')
p=Path('/etc/fstab')
with p.open('a') as f:
    f.write('\n'+escape(sys.argv[1])+' '+escape(sys.argv[2])+' none bind 0 0\n')
PY
for unit in virtual-time-world-clock virtual-time-world-clock-api; do
  install -d "/etc/systemd/system/$unit.service.d"
  printf '[Unit]\nRequiresMountsFor=%s\n' "$images" > "/etc/systemd/system/$unit.service.d/image-storage.conf"
done
systemctl daemon-reload
findmnt -T "$images"
du -sh "$images"
echo "Storage moved. Start the clock supervisor and verify company services."
echo "After verification, remove the temporary original at $root/images.hdd-retired."
