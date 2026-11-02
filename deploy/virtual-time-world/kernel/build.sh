#!/usr/bin/env bash
# Run inside the empty guest. This installs a candidate alongside the stock kernel.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
version=6.12.115
expected=0ec90fe4b20422b1b664b77e6d2dde22b40bd41e7eb2cad8ac8739a4749cb1bb
build_root=${WORLD_KERNEL_BUILD_ROOT:-/opt/agentic-world-build}
patches=(
 "$here/linux-$version-virtual-time.patch"
 "$here/linux-$version-runtime-rate.patch"
 "$here/linux-$version-runtime-epoch.patch"
)
# Include every input that changes the installed kernel in its identity.
digest=$(cat "${patches[@]}" "$here/guest.config" | sha256sum | cut -d' ' -f1)
source_root=${WORLD_KERNEL_SOURCE_DIR:-"$build_root/linux-$version-${digest:0:12}"}
install -d "$build_root"
cd "$build_root"
if [ ! -f "linux-$version.tar.xz" ]; then
 curl --fail --location --continue-at - \
   "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$version.tar.xz" \
   -o "linux-$version.tar.xz.part"
 mv "linux-$version.tar.xz.part" "linux-$version.tar.xz"
fi
printf '%s  %s\n' "$expected" "linux-$version.tar.xz" | sha256sum --check -
if [ ! -d "$source_root" ]; then
 staging=$(mktemp -d "$build_root/.linux-$version.XXXXXXXX")
 tar -xf "linux-$version.tar.xz" --strip-components=1 -C "$staging"
 for patch_file in "${patches[@]}"; do
  patch -d "$staging" -p1 < "$patch_file"
 done
 printf '%s\n' "$digest" > "$staging/.agentic-patch-sha256"
 mv "$staging" "$source_root"
fi
cd "$source_root"
test "$(cat .agentic-patch-sha256)" = "$digest"
make defconfig
scripts/kconfig/merge_config.sh -m .config "$here/guest.config"
scripts/config --set-str LOCALVERSION "-agentic-time-${digest:0:8}"
make olddefconfig
make -j "${WORLD_BUILD_JOBS:-1}" bzImage modules
make modules_install
release=$(make -s kernelrelease)
install -m 0644 arch/x86/boot/bzImage "/boot/vmlinuz-$release"
install -m 0644 System.map "/boot/System.map-$release"
install -m 0644 .config "/boot/config-$release"
update-initramfs -c -k "$release"
python3 - "$build_root" "$release" "$version" "$digest" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

root, release, version, digest = sys.argv[1:]
manifest = {
    'kernel_release': release,
    'source_version': version,
    'patch_digest': digest,
    'features': ['runtime_rate', 'boot_epoch'],
    'rtc_hctosys': False,
}
descriptor, temporary = tempfile.mkstemp(prefix='.candidate-', dir=root)
with os.fdopen(descriptor, 'w') as output:
    json.dump(manifest, output, sort_keys=True)
    output.write('\n')
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary, Path(root) / 'candidate.json')
PY
printf '%s\n' "$release" > "$build_root/candidate-release"
printf 'Candidate kernel installed alongside stock kernel: %s\n' "$release"
# Boot selection is a separate host operation; the current guest is not rebooted.
