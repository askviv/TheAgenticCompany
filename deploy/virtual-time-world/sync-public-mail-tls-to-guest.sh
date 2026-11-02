#!/usr/bin/env bash
set -euo pipefail
umask 077
export LC_ALL=C

readonly host_tls_dir=/data/theagentcompany/https-tls
readonly guest_tls_dir=/data/theagentcompany/https-tls
readonly world_root=/data/theagentcompany/virtual-time-world
readonly guest_compose_file="$world_root/config/migration-compose/theagentcompany-mailu.json"
readonly pending_marker="$guest_tls_dir/.public-mail-tls-reload-pending"
readonly guest_domain=${VIRTUAL_TIME_GUEST_DOMAIN:-tac-virtual-time-world}

if (( EUID != 0 )); then
	printf 'This helper must run as root.\n' >&2
	exit 1
fi

if ! guest_state=$(virsh --connect qemu:///system domstate "$guest_domain"); then
	printf 'Could not query VM state for %s.\n' "$guest_domain" >&2
	exit 1
fi
case $guest_state in
	running)
		;;
	'shut off')
		exit 0
		;;
	*)
		printf 'VM %s is neither running nor shut off (state: %s).\n' \
			"$guest_domain" "$guest_state" >&2
		exit 1
		;;
esac

: "${VIRTUAL_TIME_GUEST_SSH_TARGET:?Set this in the private mail TLS sync EnvironmentFile}"
: "${VIRTUAL_TIME_GUEST_SSH_PORT:?Set this in the private mail TLS sync EnvironmentFile}"
: "${VIRTUAL_TIME_GUEST_SSH_IDENTITY_FILE:?Set this in the private mail TLS sync EnvironmentFile}"
: "${VIRTUAL_TIME_GUEST_SSH_KNOWN_HOSTS_FILE:?Set this in the private mail TLS sync EnvironmentFile}"

readonly guest_key=$VIRTUAL_TIME_GUEST_SSH_IDENTITY_FILE
readonly guest_known_hosts=$VIRTUAL_TIME_GUEST_SSH_KNOWN_HOSTS_FILE
readonly guest_ssh_port=$VIRTUAL_TIME_GUEST_SSH_PORT
readonly guest_ssh_target=$VIRTUAL_TIME_GUEST_SSH_TARGET

exec 9>/run/lock/virtual-time-world-public-mail-tls.lock
flock --exclusive 9

readonly host_fullchain="$host_tls_dir/fullchain.pem"
readonly host_privkey="$host_tls_dir/privkey.pem"
readonly guest_fullchain="$guest_tls_dir/fullchain.pem"
readonly guest_privkey="$guest_tls_dir/privkey.pem"

for file in "$host_fullchain" "$host_privkey" "$guest_key" "$guest_known_hosts"; do
	if [[ ! -s $file ]]; then
		printf 'Required TLS sync input is missing or empty: %s\n' "$file" >&2
		exit 1
	fi
done

cert_public_key=$(openssl x509 -in "$host_fullchain" -pubkey -noout |
	openssl pkey -pubin -outform DER |
	sha256sum | cut -d ' ' -f 1)
private_public_key=$(openssl pkey -in "$host_privkey" -pubout -outform DER |
	sha256sum | cut -d ' ' -f 1)
if [[ $cert_public_key != "$private_public_key" ]]; then
	printf 'Host fullchain and private key do not form a matching pair.\n' >&2
	exit 1
fi

guest_ssh() {
	ssh -F /dev/null \
		-i "$guest_key" \
		-o IdentitiesOnly=yes \
		-o BatchMode=yes \
		-o UserKnownHostsFile="$guest_known_hosts" \
		-p "$guest_ssh_port" \
		"$guest_ssh_target" "$@"
}

guest_file_hash() {
	local path=$1
	guest_ssh "if sudo test -r '$path'; then sudo sha256sum -- '$path' | cut -d ' ' -f 1; else printf missing; fi"
}

host_fullchain_hash=$(sha256sum -- "$host_fullchain" | cut -d ' ' -f 1)
host_privkey_hash=$(sha256sum -- "$host_privkey" | cut -d ' ' -f 1)

guest_fullchain_hash=$(guest_file_hash "$guest_fullchain")
guest_privkey_hash=$(guest_file_hash "$guest_privkey")
guest_reload_pending=$(guest_ssh "if sudo test -f '$pending_marker'; then printf yes; else printf no; fi")

if [[ $host_fullchain_hash == "$guest_fullchain_hash" && $host_privkey_hash == "$guest_privkey_hash" && $guest_reload_pending == no ]]; then
	exit 0
fi

readonly stage_dir="$guest_tls_dir/.renewal-sync-$$-${RANDOM}"
stage_created=0
cleanup_stage() {
	local status=$?
	trap - EXIT
	if (( stage_created )); then
		if ! guest_ssh "sudo rm -f -- '$stage_dir/fullchain.pem' '$stage_dir/privkey.pem' && sudo rmdir '$stage_dir'"; then
			printf 'Could not remove guest TLS staging directory: %s\n' "$stage_dir" >&2
			if (( status == 0 )); then
				status=1
			fi
		fi
	fi
	exit "$status"
}
trap cleanup_stage EXIT
guest_ssh "sudo install -d -o root -g caddy -m 0750 '$guest_tls_dir' '$stage_dir'"
stage_created=1
guest_ssh "sudo install -o root -g root -m 0600 /dev/null '$pending_marker'"

if [[ $host_fullchain_hash != "$guest_fullchain_hash" || $host_privkey_hash != "$guest_privkey_hash" ]]; then
    cat -- "$host_fullchain" |
        guest_ssh "sudo install -o root -g caddy -m 0644 /dev/stdin '$stage_dir/fullchain.pem'"
    cat -- "$host_privkey" |
        guest_ssh "sudo install -o root -g caddy -m 0640 /dev/stdin '$stage_dir/privkey.pem'"
    guest_ssh "sudo mv -f '$stage_dir/fullchain.pem' '$guest_fullchain' && sudo mv -f '$stage_dir/privkey.pem' '$guest_privkey' && sudo rmdir '$stage_dir'"
    stage_created=0
fi

guest_target_state=$(guest_ssh 'systemctl show -p ActiveState --value agentic-company.target')
if [[ $guest_target_state == active ]]; then
	running_front=$(guest_ssh "sudo docker compose -f '$guest_compose_file' ps --quiet --status running front")
	if [[ -n $running_front ]]; then
		guest_ssh "sudo docker compose -f '$guest_compose_file' exec -T front nginx -s reload"
		guest_ssh "sudo rm -f '$pending_marker'"
	fi
fi

printf 'Updated the guest public Mailu certificate and key from the host TLS pair.\n'
