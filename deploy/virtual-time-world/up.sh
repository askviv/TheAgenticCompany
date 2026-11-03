#!/usr/bin/env bash
# Build and start a fresh isolated timed company world. Existing worlds are never adopted.
set -euo pipefail
umask 077

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORLD_ROOT=${WORLD_ROOT:-/data/theagentcompany/virtual-time-world}
WORLD_NAME=${WORLD_NAME:-tac-virtual-time-world}
WORLD_SSH_PORT=${WORLD_SSH_PORT:-22220}
PROFILE=${DEPLOYMENT_PROFILE:-$WORLD_ROOT/config/deployment-profile.json}
GUEST_SOURCE=/opt/agentic-world/source
GUEST_CONFIG=/etc/company-workplace
HOST_UNIT_DIR=/etc/systemd/system
HOST_LIB=/usr/local/lib/virtual-time-world
HOST_SOURCE=$WORLD_ROOT/company-source

fail() { printf 'up.sh: %s\n' "$*" >&2; exit 1; }
fresh=0
if [[ ${1:-} == --fresh ]]; then fresh=1; shift; fi
[[ $# == 0 ]] || fail 'usage: up.sh [--fresh]'
[[ $EUID == 0 ]] || fail 'run as root (sudo deploy/virtual-time-world/up.sh)'
[[ $WORLD_ROOT == /* ]] || fail 'WORLD_ROOT must be an absolute path'
[[ -f $PROFILE ]] || fail "deployment profile is required at $PROFILE; see deployment-profile.example.json"
if systemctl is-active --quiet caddy.service; then
  fail 'caddy.service already owns the default Caddy process; refusing to replace that unrelated service'
fi
caddy_binary_present=0
command -v caddy >/dev/null 2>&1 && caddy_binary_present=1
python3 - "$PROFILE" <<'PY'
import ipaddress, json, sys
from pathlib import Path
profile_path = Path(sys.argv[1])
if profile_path.stat().st_mode & 0o077:
    raise SystemExit('deployment profile must be private (mode 0600 or stricter)')
p = json.loads(profile_path.read_text())
required = {'public_ip', 'sandbox_password', 'viv', 'codex'}
missing = required - p.keys()
domain_keys = {'owncloud','chat','plane','mail','erp','midpoint','experiments'}
if 'domains' not in p or not isinstance(p['domains'], dict): missing.add('domains object')
else: missing |= domain_keys - p['domains'].keys()
if 'viv' not in p or not isinstance(p['viv'], dict): missing.add('viv object')
else: missing |= {'email','smtp_host','smtp_port','api_base_url','mailbox_name'} - p['viv'].keys()
if 'codex' not in p or not isinstance(p['codex'], dict): missing.add('codex object')
else: missing |= {'cli_version','model'} - p['codex'].keys()
if missing: raise SystemExit('deployment profile missing: ' + ', '.join(sorted(missing)))
ipaddress.IPv4Address(p['public_ip'])
ports = p.get('mail_ports', {})
ports = {name: ports.get(name, default) for name, default in
         {'smtp':2525, 'submission':2587, 'imaps':2993}.items()}
if any(not isinstance(port, int) or not 1 <= port <= 65535 for port in ports.values()):
    raise SystemExit('mail_ports values must be TCP ports from 1 through 65535')
if len(set(ports.values())) != len(ports): raise SystemExit('mail_ports values must be distinct')
clock_rate = p.get('clock_rate', 24)
if not isinstance(clock_rate, int) or clock_rate < 1:
    raise SystemExit('clock_rate must be a positive integer')
PY

if (( fresh == 1 )); then
  [[ ! -L $WORLD_ROOT && ! -L $WORLD_ROOT/config ]] || fail '--fresh refuses symlinked world roots or config directories'
fi
install -d -m 0700 "$WORLD_ROOT/config"
marker=$WORLD_ROOT/config/fresh-platform.json
stage=0
if (( fresh == 0 )) && [[ -e $marker ]]; then
  saved_profile=$WORLD_ROOT/config/deployment-profile.json
  [[ -f $saved_profile ]] || fail 'fresh world profile record is missing; refusing to change deployment configuration on retry'
  cmp -s "$PROFILE" "$saved_profile" || fail 'deployment profile changed after fresh bootstrap started'
  stage=$(python3 - "$marker" "$WORLD_NAME" <<'PY'
import json, sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
if m.get('world_name') != sys.argv[2] or m.get('format') != 1:
    raise SystemExit('fresh platform marker does not match requested world')
if m.get('complete'):
    print(8)
else:
    print(int(m.get('stage', 0)))
PY
  )
elif (( fresh == 0 )); then
  if virsh --connect qemu:///system dominfo "$WORLD_NAME" >/dev/null 2>&1 || \
     [[ -e $WORLD_ROOT/images/world.qcow2 || -e $WORLD_ROOT/images/company-data.qcow2 || -e $WORLD_ROOT/config/clock-state.sqlite3 ]]; then
    fail "world artifacts already exist without $marker; refusing to adopt or reset existing state"
  fi
fi

if (( fresh == 1 )); then
  if [[ $PROFILE != "$WORLD_ROOT/config/deployment-profile.json" ]]; then
    install -m 0600 "$PROFILE" "$WORLD_ROOT/config/deployment-profile.json"
  fi
elif [[ $PROFILE != "$WORLD_ROOT/config/deployment-profile.json" ]]; then
  if [[ -e $WORLD_ROOT/config/deployment-profile.json ]]; then
    cmp -s "$PROFILE" "$WORLD_ROOT/config/deployment-profile.json" || fail 'saved deployment profile differs from requested profile'
  else
    install -m 0600 "$PROFILE" "$WORLD_ROOT/config/deployment-profile.json"
  fi
fi

ssh_opts=(-i "$WORLD_ROOT/config/ssh_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -p "$WORLD_SSH_PORT")
ssh_cmd() { ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 "$@"; }

mark_stage() {
  stage=$1
  python3 - "$marker" "$WORLD_NAME" "$stage" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
path=Path(sys.argv[1])
value=json.loads(path.read_text()) if path.exists() else {'format':1,'world_name':sys.argv[2]}
value['stage']=int(sys.argv[3])
fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.fresh-platform-')
with os.fdopen(fd,'w') as stream:
    json.dump(value,stream,sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
os.chmod(tmp,0o600); os.replace(tmp,path)
PY
}

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  qemu-system-x86 qemu-utils cloud-image-utils libvirt-daemon-system libvirt-clients \
  python3-libvirt virtinst rsync openssh-client python3-venv build-essential nodejs npm caddy
if (( caddy_binary_present == 0 )) && systemctl cat caddy.service >/dev/null 2>&1; then
  systemctl disable --now caddy.service
fi

if (( fresh == 1 )); then
  images=$WORLD_ROOT/images
  [[ ! -L $images ]] || fail '--fresh refuses symlinked image directories'
  if systemctl cat virtual-time-world-public-mail-tls.timer >/dev/null 2>&1; then
    systemctl stop virtual-time-world-public-mail-tls.timer
  fi
  if systemctl cat virtual-time-world-public-mail-tls.path >/dev/null 2>&1; then
    systemctl stop virtual-time-world-public-mail-tls.path
  fi
  exec 8>/run/lock/virtual-time-world-public-mail-tls.lock
  flock --exclusive 8
  for name in world.qcow2 company-data.qcow2 seed.img console.log candidate-vmlinuz candidate-initrd candidate-release candidate.json; do
    [[ ! -L $images/$name ]] || fail "--fresh refuses symlinked image artifact: $images/$name"
  done
  if virsh --connect qemu:///system dominfo "$WORLD_NAME" >/dev/null 2>&1; then
    python3 - "$WORLD_ROOT" "$WORLD_NAME" <<'PY'
import subprocess, sys, xml.etree.ElementTree as ET
from pathlib import Path
root=Path(sys.argv[1]).resolve(strict=True)
xml=subprocess.check_output(['virsh','--connect','qemu:///system','dumpxml',sys.argv[2]],text=True)
for disk in ET.fromstring(xml).findall('./devices/disk'):
    source=disk.find('source')
    if source is None: continue
    path=source.get('file') or source.get('dev')
    if path and not Path(path).resolve().is_relative_to(root / 'images'):
        raise SystemExit(f'refusing --fresh: VM disk is outside this world root: {path}')
PY
    clock_dropin=/run/systemd/system/virtual-time-world-clock.service.d/90-fresh-reset.conf
    if systemctl is-active --quiet virtual-time-world-clock.service; then
      install -d -m 0755 "$(dirname "$clock_dropin")"
      cat > "$clock_dropin" <<'EOF2'
[Service]
Restart=no
ExecStop=
KillSignal=SIGKILL
SendSIGKILL=yes
TimeoutStopSec=0
EOF2
      restore_clock_unit() {
        rm -f "$clock_dropin"
        systemctl daemon-reload
      }
      trap restore_clock_unit EXIT
      systemctl daemon-reload
      systemctl stop virtual-time-world-clock.service
      restore_clock_unit
      trap - EXIT
    fi
    if systemctl cat virtual-time-world-clock-api.service >/dev/null 2>&1; then
      systemctl stop virtual-time-world-clock-api.service
    fi
    if [[ $(virsh --connect qemu:///system domstate "$WORLD_NAME" | tr -d '\r') != 'shut off' ]]; then
      virsh --connect qemu:///system destroy "$WORLD_NAME"
    fi
    virsh --connect qemu:///system undefine --managed-save "$WORLD_NAME"
  fi
  for name in world.qcow2 company-data.qcow2 seed.img console.log candidate-vmlinuz candidate-initrd candidate-release candidate.json; do
    rm -f -- "$images/$name"
  done
  rm -f -- "$WORLD_ROOT/config/clock-state.sqlite3" "$WORLD_ROOT/config/clock-state.sqlite3-wal" \
    "$WORLD_ROOT/config/clock-state.sqlite3-shm" "$WORLD_ROOT/config/clock-control.lock" \
    "$WORLD_ROOT/config/clock.env" "$WORLD_ROOT/config/clock-api-token" \
    "$WORLD_ROOT/config/codex-inference-token" "$WORLD_ROOT/config/guest-root-ca.crt" \
    "$WORLD_ROOT/config/fresh-platform.json" "$WORLD_ROOT/config/known_hosts" \
    /home/company/.codex-inference-token /home/company/.codex-inference.env
  rm -rf -- "$WORLD_ROOT/config/healthcheck-cadence-baseline" "$WORLD_ROOT/config/mailu-proxy-timeout-baseline" \
    "$WORLD_ROOT/config/containerd-timeout-baseline" "$WORLD_ROOT/config/issue-timer-cadence-baseline" \
    "$WORLD_ROOT/config/native-service-timeout-baseline" "$WORLD_ROOT/config/native-issue-timer-baseline"
  stage=0
  flock --unlock 8
  exec 8>&-
fi

WORLD_MAIL_BIND_IP=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["public_ip"])' "$PROFILE")
WORLD_CLOCK_RATE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("clock_rate",24))' "$PROFILE")
WORLD_MEMORY_MIB=${WORLD_MEMORY_MIB:-12288}
WORLD_VCPUS=${WORLD_VCPUS:-2}
mail_ports=$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1])).get("mail_ports",{}); print(p.get("smtp",2525),p.get("submission",2587),p.get("imaps",2993))' "$PROFILE")
read -r WORLD_MAIL_SMTP_PORT WORLD_MAIL_SUBMISSION_PORT WORLD_MAIL_IMAPS_PORT <<< "$mail_ports"
ip -o -4 addr show | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq "$WORLD_MAIL_BIND_IP" \
  || fail "public_ip $WORLD_MAIL_BIND_IP is not assigned to this host; QEMU SMTP/IMAP forwards require a local bind address"
if [[ ! -e $marker ]]; then
python3 - "$WORLD_SSH_PORT" "$WORLD_MAIL_BIND_IP" "$WORLD_MAIL_SMTP_PORT" \
  "$WORLD_MAIL_SUBMISSION_PORT" "$WORLD_MAIL_IMAPS_PORT" <<'PY'
import subprocess, sys
ssh_port, mail_ip, *mail_ports = sys.argv[1:]
requested = [('127.0.0.1', ssh_port), ('127.0.0.1', '18080'), ('127.0.0.1', '18443')]
requested.extend((mail_ip, port) for port in mail_ports)
listeners = subprocess.check_output(['ss', '-H', '-ltnp'], text=True)
conflicts = []
for line in listeners.splitlines():
    fields = line.split()
    if len(fields) < 4:
        continue
    endpoint = fields[3]
    host, separator, port = endpoint.rpartition(':')
    if not separator:
        continue
    host = host.strip('[]')
    for bind_ip, bind_port in requested:
        if port == bind_port and host in {'*', '0.0.0.0', '::', bind_ip}:
            conflicts.append(line)
if conflicts:
    raise SystemExit('QEMU forwarding port conflict:\n' + '\n'.join(conflicts))
PY
fi
export WORLD_ROOT WORLD_NAME WORLD_SSH_PORT WORLD_MEMORY_MIB WORLD_VCPUS
export WORLD_CLOCK_RATE
export WORLD_MAIL_BIND_IP WORLD_MAIL_SMTP_PORT WORLD_MAIL_SUBMISSION_PORT WORLD_MAIL_IMAPS_PORT
if [[ ! -e $marker ]]; then
  python3 - "$marker" "$WORLD_NAME" <<'PY'
import json, os, sys, tempfile
p=sys.argv[1]; data={'format':1,'world_name':sys.argv[2],'stage':0}
fd,tmp=tempfile.mkstemp(dir=os.path.dirname(p), prefix='.fresh-platform-')
with os.fdopen(fd,'w') as f: json.dump(data,f); f.write('\n'); f.flush(); os.fsync(f.fileno())
os.chmod(tmp,0o600); os.replace(tmp,p)
PY
fi
bash "$REPO_ROOT/deploy/virtual-time-world/provision.sh"

if (( stage < 1 )); then
ssh_opts=(-i "$WORLD_ROOT/config/ssh_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -p "$WORLD_SSH_PORT")
ssh_cmd() { ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 "$@"; }
# Wait only while SSH reports the expected not-yet-listening condition; cloud-init
# itself provides the readiness event and its diagnostic is preserved.
ssh_error=$(mktemp)
while ! ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 'sudo cloud-init status --wait' 2>"$ssh_error"; do
  if grep -q 'Connection refused' "$ssh_error"; then sleep 2; continue; fi
  cat "$ssh_error" >&2
  rm -f "$ssh_error"
  fail 'guest cloud-init readiness command failed'
done
rm -f "$ssh_error"
ssh_cmd "sudo install -d -o root -g root -m 0755 $GUEST_SOURCE"
rsync -a --delete --exclude=.git --exclude='__pycache__' --exclude='node_modules' \
  --rsync-path='sudo rsync' \
  -e "ssh -i $WORLD_ROOT/config/ssh_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$WORLD_ROOT/config/known_hosts -p $WORLD_SSH_PORT" \
  "$REPO_ROOT/" ubuntu@127.0.0.1:"$GUEST_SOURCE/" \
  || fail 'could not install repository source in guest'
# Build the timed kernel before starting applications, while the guest has
# its full memory and CPU available to the compiler.
ssh_cmd "sudo bash $GUEST_SOURCE/deploy/virtual-time-world/kernel/build.sh"
scp -i "$WORLD_ROOT/config/ssh_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -P "$WORLD_SSH_PORT" \
  "$PROFILE" ubuntu@127.0.0.1:/tmp/company-deployment-profile.json

# The bridge exposes only host loopback through QEMU's guest gateway.
id company >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/company --shell /usr/sbin/nologin company
install -d -o company -g company -m 0700 /home/company/.codex
install -d -o company -g company -m 0700 /home/company/.ssh
install -o company -g company -m 0600 "$WORLD_ROOT/config/ssh_ed25519" /home/company/.ssh/tac-guest-ed25519
install -o company -g company -m 0600 "$WORLD_ROOT/config/known_hosts" /home/company/.ssh/tac-guest-known-hosts
host_token="$WORLD_ROOT/config/codex-inference-token"
if [[ ! -s $host_token ]]; then umask 077; openssl rand -hex 32 > "$host_token"; fi
clock_api_token="$WORLD_ROOT/config/clock-api-token"
if [[ ! -s $clock_api_token ]]; then umask 077; openssl rand -hex 32 > "$clock_api_token"; fi
codex_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["codex"]["cli_version"])' "$PROFILE")
codex_model=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["codex"]["model"])' "$PROFILE")
[[ $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'codex.cli_version must be an exact npm version'
[[ $codex_model =~ ^[A-Za-z0-9._-]+$ ]] || fail 'codex.model contains unsupported characters'
# Install the complete official package, including its native tool host. A lone
# copied codex executable can report the right version but cannot execute tools.
codex_npm_root=$(npm root --global --prefix /usr/local)
if ! command -v codex >/dev/null 2>&1 || [[ $(codex --version) != "codex-cli $codex_version" ]] ||
   [[ $(readlink -f "$(command -v codex)") != "$codex_npm_root/@openai/codex/bin/codex.js" ]]; then
  npm install --global --prefix /usr/local --force "@openai/codex@$codex_version"
fi
[[ $(codex --version) == "codex-cli $codex_version" ]] || fail 'installed Codex CLI does not match codex.cli_version'
python3 - "$codex_npm_root/@openai" <<'PYCODEX'
import os
from pathlib import Path
import sys
if not any(os.access(path, os.X_OK) for path in Path(sys.argv[1]).rglob('codex-code-mode-host')):
    raise SystemExit('official Codex package is missing its native tool host; reinstall the pinned package')
PYCODEX
install -o company -g company -m 0600 "$host_token" /home/company/.codex-inference-token
cat > /home/company/.codex-inference.env <<EOF2
CODEX_INFERENCE_BEARER_TOKEN=$(cat "$host_token")
CODEX_MODEL=$codex_model
CODEX_HOME=/home/company/.codex
CODEX_EXECUTABLE=$(command -v codex)
CODEX_CUA_GUEST_SSH_PORT=$WORLD_SSH_PORT
EOF2
chown company:company /home/company/.codex-inference.env
chmod 0600 /home/company/.codex-inference.env
install -m 0644 "$REPO_ROOT/deploy/enterprise-simulation/actors/tac-codex-inference.service" "$HOST_UNIT_DIR/tac-codex-inference.service"

# Guest setup and application initialization run at the stock kernel rate (1x).
scp -i "$WORLD_ROOT/config/ssh_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -P "$WORLD_SSH_PORT" \
  "$host_token" ubuntu@127.0.0.1:/tmp/codex-inference-token
scp -i "$WORLD_ROOT/config/ssh_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -P "$WORLD_SSH_PORT" \
  "$clock_api_token" ubuntu@127.0.0.1:/tmp/clock-api-token
ssh_cmd "if ! getent passwd company >/dev/null; then sudo useradd --system --create-home --home-dir /home/company --shell /usr/sbin/nologin company; fi && sudo install -d -o company -g company -m 0700 /home/company/.codex && sudo install -d -m 0700 $GUEST_CONFIG /etc/company-workplace/compose /etc/company-workplace/secrets /data/theagentcompany/enterprise/config && sudo install -m 0600 /tmp/company-deployment-profile.json $GUEST_CONFIG/deployment-profile.json && sudo install -m 0600 /tmp/codex-inference-token /etc/company-workplace/secrets/codex-inference-token && sudo install -m 0600 /tmp/clock-api-token /etc/company-workplace/secrets/clock-api-token && sudo ln -sfn $GUEST_SOURCE /data/theagentcompany/source && sudo ln -sfn $GUEST_SOURCE /data/theagentcompany/enterprise/source && sudo systemctl start docker.service && sudo python3 $GUEST_SOURCE/deploy/virtual-time-world/guest/bootstrap_workplace.py --source $GUEST_SOURCE --profile $GUEST_CONFIG/deployment-profile.json"
ssh_cmd "sudo install -m 0644 $GUEST_SOURCE/deploy/virtual-time-world/guest/tac-company-clock-caddy.service /etc/systemd/system/tac-https.service && sudo systemctl daemon-reload && sudo systemctl start tac-https.service"
ssh_cmd "sudo python3 $GUEST_SOURCE/deploy/virtual-time-world/guest/bootstrap_fresh_company.py --source $GUEST_SOURCE --profile $GUEST_CONFIG/deployment-profile.json && sudo /data/theagentcompany/enterprise/venv/bin/python $GUEST_SOURCE/deploy/virtual-time-world/guest/configure-kernel-clock.py && sudo /data/theagentcompany/enterprise/venv/bin/python $GUEST_SOURCE/deploy/virtual-time-world/guest/bootstrap_fresh_identity.py --source $GUEST_SOURCE --profile $GUEST_CONFIG/deployment-profile.json && sudo python3 $GUEST_SOURCE/deploy/virtual-time-world/guest/bootstrap_dashboard.py --source $GUEST_SOURCE --profile $GUEST_CONFIG/deployment-profile.json && sudo python3 $GUEST_SOURCE/deploy/enterprise-simulation/actors/bootstrap.py --inference-url http://10.0.2.2:8143 --inference-token-file /etc/company-workplace/secrets/codex-inference-token --model $codex_model"
ssh_cmd 'sudo cat /etc/company-clock-tls/root-ca.crt' > "$WORLD_ROOT/config/guest-root-ca.crt"
chmod 0600 "$WORLD_ROOT/config/guest-root-ca.crt"
mark_stage 1
fi

# Install stock systemd/Compose wiring, without starting the company target yet.
if (( stage < 2 )); then
ssh_cmd "sudo install -d -m 0755 /usr/local/lib/virtual-time-world /etc/systemd/system/agentic-company.target.d && sudo install -m 0755 $GUEST_SOURCE/deploy/virtual-time-world/prepare-healthcheck-cadence.py /usr/local/lib/virtual-time-world/prepare-healthcheck-cadence.py && sudo install -m 0755 $GUEST_SOURCE/deploy/virtual-time-world/prepare-containerd-timeouts.py /usr/local/lib/virtual-time-world/prepare-containerd-timeouts.py && sudo install -m 0755 $GUEST_SOURCE/deploy/virtual-time-world/company-compose-stop.py /usr/local/lib/virtual-time-world/company-compose-stop && sudo install -m 0644 $GUEST_SOURCE/deploy/virtual-time-world/agentic-company.target /etc/systemd/system/agentic-company.target && sudo install -m 0644 $GUEST_SOURCE/deploy/virtual-time-world/agentic-company.target.d/services.conf /etc/systemd/system/agentic-company.target.d/services.conf && sudo install -m 0644 $GUEST_SOURCE/deploy/virtual-time-world/agentic-company-compose.service /etc/systemd/system/agentic-company-compose.service && sudo install -m 0644 $GUEST_SOURCE/deploy/virtual-time-world/guest/tac-company-clock-caddy.service /etc/systemd/system/tac-https.service && for unit in $GUEST_SOURCE/deploy/enterprise-simulation/coordinator/*.service $GUEST_SOURCE/deploy/enterprise-simulation/coordinator/*.timer $GUEST_SOURCE/deploy/enterprise-simulation/actors/tac-customer-actor.service $GUEST_SOURCE/deploy/enterprise-simulation/actors/tac-supplier-actor.service; do sudo install -m 0644 \$unit /etc/systemd/system/; done && for dropin in $GUEST_SOURCE/deploy/virtual-time-world/tac-*.service.d; do sudo install -d -m 0755 /etc/systemd/system/\$(basename \$dropin); sudo install -m 0644 \$dropin/* /etc/systemd/system/\$(basename \$dropin)/; done && sudo systemctl daemon-reload"
mark_stage 2
fi

# Install host clock controller/API and start the dedicated host services.
if (( stage < 3 )); then
install -d -m 0755 "$HOST_LIB" "$HOST_SOURCE" "$WORLD_ROOT/config"
rsync -a --delete --exclude=.git --exclude='__pycache__' "$REPO_ROOT/" "$HOST_SOURCE/"
python3 -m venv --system-site-packages "$WORLD_ROOT/host-venv"
"$WORLD_ROOT/host-venv/bin/pip" install --disable-pip-version-check --constraint "$REPO_ROOT/servers/simulation/requirements.txt" fastapi uvicorn requests
install -m 0755 "$REPO_ROOT/deploy/virtual-time-world/clock_control.py" "$HOST_LIB/clock_control.py"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/clock_api.py" "$HOST_LIB/clock_api.py"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/prepare-healthcheck-cadence.py" "$HOST_LIB/prepare-healthcheck-cadence.py"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/company-compose-stop.py" "$HOST_LIB/company-compose-stop"
install -m 0755 "$REPO_ROOT/deploy/virtual-time-world/sync-public-mail-tls-to-guest.sh" "$HOST_LIB/sync-public-mail-tls-to-guest.sh"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/virtual-time-world-clock.service" "$HOST_UNIT_DIR/virtual-time-world-clock.service"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/virtual-time-world-clock-api.service" "$HOST_UNIT_DIR/virtual-time-world-clock-api.service"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/virtual-time-world-public-mail-tls.service" "$HOST_UNIT_DIR/virtual-time-world-public-mail-tls.service"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/virtual-time-world-public-mail-tls.timer" "$HOST_UNIT_DIR/virtual-time-world-public-mail-tls.timer"
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/virtual-time-world-public-mail-tls.path" "$HOST_UNIT_DIR/virtual-time-world-public-mail-tls.path"
python3 - "$HOST_UNIT_DIR/virtual-time-world-clock.service" "$HOST_UNIT_DIR/virtual-time-world-clock-api.service" \
  "$HOST_UNIT_DIR/virtual-time-world-public-mail-tls.service" "$HOST_UNIT_DIR/virtual-time-world-public-mail-tls.path" \
  "$HOST_UNIT_DIR/tac-codex-inference.service" "$WORLD_ROOT" "$WORLD_NAME" "$WORLD_SSH_PORT" "$PROFILE" <<'PY'
from pathlib import Path
import json
import sys
clock, api, mail_tls, mail_path, codex, root, name, port, profile = sys.argv[1:]
units = (clock, api, mail_tls, codex)
for filename in units:
    path=Path(filename)
    content=path.read_text()
    content=content.replace('/data/theagentcompany/virtual-time-world', root)
    content=content.replace('tac-virtual-time-world', name)
    content=content.replace('22220', port)
    path.write_text(content)
path=Path(mail_path)
mail_host=json.loads(Path(profile).read_text(encoding='utf-8'))['domains']['mail']
path.write_text(path.read_text().replace('__PUBLIC_MAIL_HOST__', mail_host))
PY
install -m 0644 "$REPO_ROOT/deploy/virtual-time-world/host-tac-https.service" "$HOST_UNIT_DIR/tac-https.service"
install -m 0755 "$REPO_ROOT/deploy/virtual-time-world/configure-public-caddy.py" "$HOST_LIB/configure-public-caddy.py"
python3 "$REPO_ROOT/deploy/virtual-time-world/configure-public-caddy.py" \
  --profile "$PROFILE" --guest-root-ca "$WORLD_ROOT/config/guest-root-ca.crt"
caddy validate --config /etc/caddy/company-world/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable --now tac-https.service
mark_stage 3
fi
if [[ ! -s $WORLD_ROOT/config/clock-api-token ]]; then
  umask 077; openssl rand -hex 32 > "$WORLD_ROOT/config/clock-api-token"
fi
cat > "$WORLD_ROOT/config/clock.env" <<EOF2
WORLD_ROOT=$WORLD_ROOT
WORLD_NAME=$WORLD_NAME
WORLD_SSH_PORT=$WORLD_SSH_PORT
WORLD_CLOCK_RATE=$WORLD_CLOCK_RATE
EOF2
chmod 0600 "$WORLD_ROOT/config/clock.env"
systemctl enable --now virtual-time-world-public-mail-tls.timer
systemctl enable --now virtual-time-world-public-mail-tls.path
public_cert_configured=$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); t=p.get("public_tls",{}); print(int(bool(t.get("cert_file") and t.get("key_file"))))' "$PROFILE")
if (( public_cert_configured )); then
  systemctl start virtual-time-world-public-mail-tls.service
fi

# Start host bridge before workers so dashboard device-auth and inference share the same CLI.
if (( stage < 4 )); then
  systemctl daemon-reload
  systemctl enable tac-codex-inference.service
  systemctl restart tac-codex-inference.service
  ssh_cmd 'sudo systemctl start tac-https.service'
  ssh_cmd "sudo python3 $GUEST_SOURCE/deploy/virtual-time-world/prepare-healthcheck-cadence.py --config-dir /etc/company-workplace/compose --rate 1"
  ssh_cmd "sudo python3 /usr/local/lib/virtual-time-world/prepare-containerd-timeouts.py --config-dir /etc/company-workplace/compose --rate $WORLD_CLOCK_RATE"
  mark_stage 4
fi

# Prepare candidate while stock guest is available, then seed the first durable anchor.
if (( stage < 5 )); then
python3 "$HOST_LIB/clock_control.py" --root "$WORLD_ROOT" --name "$WORLD_NAME" --ssh-port "$WORLD_SSH_PORT" prepare
mark_stage 5
fi
if (( stage < 6 )); then
python3 "$HOST_LIB/clock_control.py" --root "$WORLD_ROOT" --name "$WORLD_NAME" --ssh-port "$WORLD_SSH_PORT" stop
mark_stage 6
fi
if (( stage < 7 )); then
python3 - "$HOST_LIB" "$WORLD_ROOT" "$WORLD_NAME" "$WORLD_SSH_PORT" "$WORLD_CLOCK_RATE" <<'PY_CLOCK_INIT'
from pathlib import Path
import sys, time
sys.path.insert(0, sys.argv[1])
from clock_control import Controller
controller = Controller(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
rate = int(sys.argv[5])
with controller.operation_lock():
    with controller.state() as db:
        anchor = controller.row(db)
    if anchor is None:
        controller.initialize(time.time_ns(), rate)
    elif anchor[2] != rate:
        raise RuntimeError('Interrupted initialization has a different configured clock rate')
    # A completed initialization survives an interruption before mark_stage.
    # Keep its original epoch instead of initializing the clock a second time.
PY_CLOCK_INIT
mark_stage 7
fi
systemctl daemon-reload
systemctl enable virtual-time-world-clock.service virtual-time-world-clock-api.service tac-codex-inference.service
systemctl start tac-codex-inference.service
if systemctl is-active --quiet virtual-time-world-clock.service; then
  systemctl reload virtual-time-world-clock.service
else
  systemctl start virtual-time-world-clock.service
fi
systemctl restart virtual-time-world-clock-api.service
if ! systemctl is-active --quiet tac-https.service; then
  listeners=$(ss -Hltpn '( sport = :80 or sport = :443 )')
  [[ -z $listeners ]] || fail "public ports 80/443 are already owned by another service; refusing to replace it: $listeners"
  systemctl enable --now tac-https.service
else
  systemctl restart tac-https.service
fi
ssh_error=$(mktemp)
while ! ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 \
  "test \"\$(cat /sys/kernel/virtual_time_rate)\" = $WORLD_CLOCK_RATE && systemctl is-active --quiet agentic-company.target" \
  >/dev/null 2>"$ssh_error"; do
  if grep -q 'Connection refused' "$ssh_error" || [[ ! -s $ssh_error ]]; then
    sleep 2
    continue
  fi
  cat "$ssh_error" >&2
  rm -f "$ssh_error"
  fail 'guest did not reach the synchronized company target'
done
rm -f "$ssh_error"
ssh_cmd 'sudo systemctl start agentic-company.target && sudo systemctl reload agentic-company-compose.service && sudo systemctl restart tac-https.service'
# Configure the external inbox only after the company is running. A missing
# external inbox reports an integration failure without stopping company apps.
ssh_cmd "sudo python3 $GUEST_SOURCE/deploy/single-host/smtp4dev-bridge/configure_reply.py --profile $GUEST_CONFIG/deployment-profile.json"
printf 'Fresh world boot requested. Clock supervisor will synchronize guest before the company target.\n'
printf 'Public HTTPS routes use the supplied certificate or Caddy ACME with the configured DNS.\n'
printf 'Complete Codex CLI device authentication from the dashboard when prompted.\n'
printf 'World root: %s\n' "$WORLD_ROOT"
python3 - "$marker" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
path=Path(sys.argv[1]); value=json.loads(path.read_text()); value['stage']=8; value['complete']=True
fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.fresh-platform-')
with os.fdopen(fd,'w') as stream:
    json.dump(value,stream,sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
os.chmod(tmp,0o600); os.replace(tmp,path)
PY
