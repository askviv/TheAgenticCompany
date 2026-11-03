#!/usr/bin/env bash
set -euo pipefail
umask 077

WORLD_ROOT=${WORLD_ROOT:-/data/theagentcompany/virtual-time-world}
WORLD_SSH_PORT=${WORLD_SSH_PORT:-22220}
PROFILE=$WORLD_ROOT/config/deployment-profile.json
MAIL_TLS_DIR=/data/theagentcompany/https-tls
COMPOSE_FILE=/etc/company-workplace/compose/mailu.json
LOCK_FILE=/run/lock/virtual-time-world-public-mail-tls.lock

if (( EUID != 0 )); then
  printf 'Public Mailu TLS synchronization must run as root.\n' >&2
  exit 1
fi

[[ -s $PROFILE ]] || { printf 'Deployment profile is unavailable: %s\n' "$PROFILE" >&2; exit 1; }
[[ -s $WORLD_ROOT/config/ssh_ed25519 && -s $WORLD_ROOT/config/known_hosts ]] \
  || { printf 'Guest SSH identity or known-hosts file is unavailable.\n' >&2; exit 1; }
[[ -s $COMPOSE_FILE ]] || { printf 'Fresh Mailu Compose config is unavailable: %s\n' "$COMPOSE_FILE" >&2; exit 1; }

exec 9>"$LOCK_FILE"
flock --exclusive 9

mail_host=$(python3 - "$PROFILE" <<'PY'
import json, re, sys
profile=json.load(open(sys.argv[1], encoding='utf-8'))
host=profile['domains']['mail'].strip().rstrip('.').lower()
if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
    raise SystemExit('domains.mail is not a valid public hostname')
print(host)
PY
)

source_pair=$(python3 - "$PROFILE" "$mail_host" <<'PY'
import json, sys
from pathlib import Path

profile=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
domain=sys.argv[2]
config=Path('/etc/caddy/company-world')
tls=profile.get('public_tls', {})
if tls.get('cert_file') and tls.get('key_file'):
    cert, key = config/'fullchain.pem', config/'privkey.pem'
    if cert.is_file() and key.is_file():
        print(f'{cert}\t{key}')
        raise SystemExit(0)
    raise SystemExit('configured public TLS files were not copied into the Caddy config directory')

storage=Path('/var/lib/tac-https/caddy/certificates')
candidates=sorted(storage.glob(f'*/{domain}/{domain}.crt'),
                  key=lambda item: item.stat().st_mtime_ns, reverse=True)
for cert in candidates:
    key=cert.with_suffix('.key')
    if key.is_file():
        print(f'{cert}\t{key}')
        raise SystemExit(0)
raise SystemExit(f'Caddy has not stored a public certificate for {domain} yet')
PY
)
IFS=$'\t' read -r host_cert host_key <<< "$source_pair"
[[ -s $host_cert && -s $host_key ]] || { printf 'Public Mailu certificate or key is unavailable.\n' >&2; exit 1; }

leaf=$(mktemp)
stage=
stage_dir_created=0
openssl x509 -in "$host_cert" -out "$leaf"
openssl x509 -in "$leaf" -noout -checkhost "$mail_host" -checkend 0 >/dev/null
openssl verify -purpose sslserver -CApath /etc/ssl/certs -untrusted "$host_cert" "$leaf" >/dev/null
cert_public_key=$(openssl x509 -in "$leaf" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum | cut -d ' ' -f 1)
private_public_key=$(openssl pkey -in "$host_key" -pubout -outform DER | sha256sum | cut -d ' ' -f 1)
[[ $cert_public_key == "$private_public_key" ]] \
  || { printf 'Public Mailu certificate and private key do not match.\n' >&2; exit 1; }

ssh_opts=(-F /dev/null -i "$WORLD_ROOT/config/ssh_ed25519" -o IdentitiesOnly=yes \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$WORLD_ROOT/config/known_hosts" -p "$WORLD_SSH_PORT")
guest_ssh() { ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 "$@"; }
cleanup() {
  local status=$?
  trap - EXIT
  if (( stage_dir_created )); then
    if ! guest_ssh "sudo rm -rf -- '$stage'"; then
      printf 'Could not remove guest TLS staging directory: %s\n' "$stage" >&2
      (( status == 0 )) && status=1
    fi
  fi
  rm -f -- "$leaf"
  exit "$status"
}
trap cleanup EXIT
guest_file_hash() {
  local path=$1
  guest_ssh "if sudo test -r '$path'; then sudo sha256sum -- '$path' | cut -d ' ' -f 1; else printf missing; fi"
}

host_cert_hash=$(sha256sum -- "$host_cert" | cut -d ' ' -f 1)
host_key_hash=$(sha256sum -- "$host_key" | cut -d ' ' -f 1)
guest_cert_hash=$(guest_file_hash "$MAIL_TLS_DIR/fullchain.pem")
guest_key_hash=$(guest_file_hash "$MAIL_TLS_DIR/privkey.pem")
pending=$(guest_ssh "if sudo test -f '$MAIL_TLS_DIR/.public-mail-tls-reload-pending'; then printf yes; else printf no; fi")
changed=0
if [[ $host_cert_hash != "$guest_cert_hash" || $host_key_hash != "$guest_key_hash" || $pending == yes ]]; then
  changed=1
  stage="$MAIL_TLS_DIR/.public-mail-tls-$$-${RANDOM}"
  stage_dir_created=1
  guest_ssh "sudo install -d -o root -g root -m 0700 '$MAIL_TLS_DIR' '$stage' && sudo install -o root -g root -m 0600 /dev/null '$MAIL_TLS_DIR/.public-mail-tls-reload-pending'"
  cat -- "$host_cert" | guest_ssh "sudo install -o root -g root -m 0644 /dev/stdin '$stage/fullchain.pem'"
  cat -- "$host_key" | guest_ssh "sudo install -o root -g root -m 0600 /dev/stdin '$stage/privkey.pem'"
  guest_ssh "sudo mv -f '$stage/fullchain.pem' '$MAIL_TLS_DIR/fullchain.pem' && sudo mv -f '$stage/privkey.pem' '$MAIL_TLS_DIR/privkey.pem' && sudo rmdir '$stage'"
  stage_dir_created=0
fi

guest_ssh "sudo docker compose --profile public --file '$COMPOSE_FILE' up -d --pull never --no-build --wait front"
if (( changed )); then
  guest_ssh "sudo docker compose --profile public --file '$COMPOSE_FILE' exec -T front nginx -s reload"
  guest_ssh "sudo rm -f '$MAIL_TLS_DIR/.public-mail-tls-reload-pending'"
  printf 'Updated public Mailu TLS from the configured public certificate source.\n'
fi
