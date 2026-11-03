#!/usr/bin/env bash
# Create an isolated guest. Existing domains and disks are never replaced.
set -euo pipefail

WORLD_ROOT=${WORLD_ROOT:-/data/theagentcompany/virtual-time-world}
WORLD_NAME=${WORLD_NAME:-tac-virtual-time-world}
WORLD_MEMORY_MIB=${WORLD_MEMORY_MIB:-2048}
WORLD_VCPUS=${WORLD_VCPUS:-1}
WORLD_SSH_PORT=${WORLD_SSH_PORT:-22220}
WORLD_DISK_SIZE=${WORLD_DISK_SIZE:-40G}
WORLD_DATA_DISK_SIZE=${WORLD_DATA_DISK_SIZE:-250G}
WORLD_MAIL_BIND_IP=${WORLD_MAIL_BIND_IP:-127.0.0.1}
WORLD_MAIL_SMTP_PORT=${WORLD_MAIL_SMTP_PORT:-2525}
WORLD_MAIL_SUBMISSION_PORT=${WORLD_MAIL_SUBMISSION_PORT:-2587}
WORLD_MAIL_IMAPS_PORT=${WORLD_MAIL_IMAPS_PORT:-2993}
connection=qemu:///system
base=noble-server-cloudimg-amd64.img

if virsh --connect "$connection" dominfo "$WORLD_NAME" >/dev/null 2>&1; then
  virsh --connect "$connection" dominfo "$WORLD_NAME"
  exit 0
fi

install -d -m 0755 "$WORLD_ROOT" "$WORLD_ROOT/images"
install -d -m 0700 "$WORLD_ROOT/config"
if [ ! -f "$WORLD_ROOT/images/SHA256SUMS" ]; then
  curl --fail --location --silent --show-error \
    https://cloud-images.ubuntu.com/noble/current/SHA256SUMS \
    -o "$WORLD_ROOT/images/SHA256SUMS"
fi
if [ ! -f "$WORLD_ROOT/images/$base" ]; then
  curl --fail --location --continue-at - \
    "https://cloud-images.ubuntu.com/noble/current/$base" \
    -o "$WORLD_ROOT/images/$base.part"
  mv "$WORLD_ROOT/images/$base.part" "$WORLD_ROOT/images/$base"
fi
(
  cd "$WORLD_ROOT/images"
  grep " \\*$base\$" SHA256SUMS | sha256sum --check -
)

if [ ! -f "$WORLD_ROOT/config/ssh_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N '' -C agentic-world-host-control \
    -f "$WORLD_ROOT/config/ssh_ed25519"
fi
public_key=$(cat "$WORLD_ROOT/config/ssh_ed25519.pub")
cat > "$WORLD_ROOT/config/user-data" <<EOF
#cloud-config
hostname: agentic-world
manage_etc_hosts: true
users:
  - default
ssh_pwauth: false
ssh_authorized_keys:
  - $public_key
fs_setup:
  - label: company-data
    filesystem: ext4
    device: /dev/vdc
    overwrite: false
mounts:
  - [LABEL=company-data, /data, ext4, "defaults", "0", "2"]
write_files:
  - path: /etc/docker/daemon.json
    permissions: '0644'
    content: |
      {"data-root": "/data/docker", "userland-proxy": false, "live-restore": true}
  - path: /etc/systemd/system/docker.service.d/company-data.conf
    permissions: '0644'
    content: |
      [Unit]
      RequiresMountsFor=/data
  - path: /etc/systemd/system/containerd.service.d/company-data.conf
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=/usr/bin/containerd --root /data/containerd --state /run/containerd
      [Unit]
      RequiresMountsFor=/data
  - path: /etc/systemd/system.conf.d/50-world-virtual-time.conf
    permissions: '0644'
    content: |
      [Manager]
      DefaultTimeoutStartSec=infinity
      DefaultTimeoutStopSec=infinity
      DefaultDeviceTimeoutSec=infinity
  - path: /etc/systemd/system/service.d/50-world-virtual-time.conf
    permissions: '0644'
    content: |
      [Service]
      TimeoutStartSec=infinity
      TimeoutStopSec=infinity
      WatchdogSec=0
package_update: true
packages:
  - docker.io
  - docker-compose-v2
  - qemu-guest-agent
  - util-linux-extra
  - libnss3-tools
  - build-essential
  - bc
  - bison
  - flex
  - libelf-dev
  - libssl-dev
  - libncurses-dev
  - dwarves
  - kmod
  - cpio
  - curl
  - git
  - rsync
  - caddy
  - python3-venv
  - openjdk-21-jdk-headless
  - unzip
  - perl
runcmd:
  - [systemctl, disable, --now, caddy.service]
  - [systemctl, disable, --now, docker.service, docker.socket]
  - [systemctl, disable, --now, systemd-timesyncd.service]
  - [systemctl, disable, --now, apt-daily.timer, apt-daily-upgrade.timer, fwupd-refresh.timer, multipathd.service, multipathd.socket]
  - [systemctl, mask, --now, apt-daily.service, apt-daily.timer, apt-daily-upgrade.service, apt-daily-upgrade.timer, unattended-upgrades.service, fwupd.service, fwupd-refresh.service, fwupd-refresh.timer, multipathd.service, multipathd.socket]
EOF
cat > "$WORLD_ROOT/config/meta-data" <<EOF
instance-id: $WORLD_NAME
local-hostname: agentic-world
EOF
cloud-localds "$WORLD_ROOT/images/seed.img" \
  "$WORLD_ROOT/config/user-data" "$WORLD_ROOT/config/meta-data"

if [ ! -f "$WORLD_ROOT/images/world.qcow2" ]; then
  qemu-img create -f qcow2 -F qcow2 \
    -b "$WORLD_ROOT/images/$base" "$WORLD_ROOT/images/world.qcow2" \
    "$WORLD_DISK_SIZE"
fi

if [ ! -f "$WORLD_ROOT/images/company-data.qcow2" ]; then
  qemu-img create -f qcow2 "$WORLD_ROOT/images/company-data.qcow2" "$WORLD_DATA_DISK_SIZE"
fi

virt-install --connect "$connection" \
  --name "$WORLD_NAME" --virt-type kvm --machine q35 \
  --memory "$WORLD_MEMORY_MIB" --vcpus "$WORLD_VCPUS" \
  --cpu host-passthrough --os-variant ubuntu24.04 \
  --disk "path=$WORLD_ROOT/images/world.qcow2,format=qcow2,bus=virtio" \
  --disk "path=$WORLD_ROOT/images/seed.img,format=raw,bus=virtio,readonly=on" \
  --disk "path=$WORLD_ROOT/images/company-data.qcow2,format=qcow2,bus=virtio" \
  --network none \
  --qemu-commandline="-netdev user,id=worldnet,hostfwd=tcp:127.0.0.1:$WORLD_SSH_PORT-:22,hostfwd=tcp:127.0.0.1:18080-:80,hostfwd=tcp:127.0.0.1:18443-:443,hostfwd=tcp:$WORLD_MAIL_BIND_IP:$WORLD_MAIL_SMTP_PORT-:$WORLD_MAIL_SMTP_PORT,hostfwd=tcp:$WORLD_MAIL_BIND_IP:$WORLD_MAIL_SUBMISSION_PORT-:$WORLD_MAIL_SUBMISSION_PORT,hostfwd=tcp:$WORLD_MAIL_BIND_IP:$WORLD_MAIL_IMAPS_PORT-:$WORLD_MAIL_IMAPS_PORT -device virtio-net-pci,netdev=worldnet,bus=pcie.0,addr=0x1e" \
  --channel unix,target.type=virtio,name=org.qemu.guest_agent.0 \
  --graphics none --console none \
  --serial "file,path=$WORLD_ROOT/images/console.log" \
  --import --noautoconsole

printf 'Guest created. SSH is bound to host localhost port %s.\n' "$WORLD_SSH_PORT"
printf 'Host control key: %s/config/ssh_ed25519\n' "$WORLD_ROOT"
