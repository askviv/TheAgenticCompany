#!/usr/bin/env bash
# Upgrade the existing shared company with native apps; never reset or recreate it.
set -euo pipefail
apps_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime=${SIMULATION_CONFIG:-/data/theagentcompany/enterprise/config/runtime.json}
compose=${ERP_COMPOSE:-$(python3 - "$runtime" <<'PYCODE'
import json,sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())['erp_compose'])
PYCODE
)}
image=${ERP_IMAGE:-$(docker compose --file "$apps_dir/../compose.yml" --profile "*" config --format json |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')}
tools_urls=${COMPANY_TOOLS_URLS_FILE:-/data/theagentcompany/enterprise/secrets/company-tools-urls.json}
# Forward-initialize app role bindings in the existing authoritative directory.
python3 - "$runtime" "$apps_dir/../../../single-host/company-identities.json" <<'PYROLES'
import json,os,sys,tempfile
from pathlib import Path
runtime=json.loads(Path(sys.argv[1]).read_text())
directory=Path(runtime['company_profile_file'])
current=json.loads(directory.read_text())
defaults={e['id']:e['frappeAppRoles'] for e in json.loads(Path(sys.argv[2]).read_text())['employees']}
for employee in current['employees']:
    if 'frappeAppRoles' not in employee:
        employee['frappeAppRoles']=defaults[employee['id']]
descriptor, temporary = tempfile.mkstemp(dir=directory.parent, prefix='.' + directory.name + '-')
try:
    details = directory.stat()
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), details.st_mode & 0o777)
        os.fchown(output.fileno(), details.st_uid, details.st_gid)
        json.dump(current, output, indent=2, ensure_ascii=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, directory)
    parent = os.open(directory.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    Path(temporary).unlink(missing_ok=True)
PYROLES
ERP_IMAGE="$image" bash "$apps_dir/build.sh"
# Render with Compose before changing the image, retaining deployment-specific settings.
rendered=$(mktemp)
restart_needed=0
previously_running=()
cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 && restart_needed == 1 && ${#previously_running[@]} > 0 )); then
    if ! docker compose --file "$compose" up -d --wait "${previously_running[@]}"; then
      printf 'ERP recovery failed after original upgrade exit %s. Rerun install.sh after resolving the reported native error.\n' "$status" >&2
    fi
  fi
  if ! rm -f -- "$rendered"; then
    printf 'Could not remove private rendered Compose file: %s\n' "$rendered" >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose --file "$compose" --profile "*" config --format json > "$rendered"
python3 - "$rendered" "$compose" "$image" "$tools_urls" <<'PY'
import json,os,sys,tempfile
from pathlib import Path
source, destination, image, tools_urls = sys.argv[1:]
config=json.loads(Path(source).read_text())
config['services']['site-admin']['environment']['TAC_ERP_PUBLIC_URL']=json.loads(Path(tools_urls).read_text())['erpnext']
for name in config['services'].keys() & {'configurator','backend','frontend','websocket','queue-short','queue-long','scheduler','site-admin'}:
    config['services'][name]['image']=image
target = Path(destination)
descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix='.' + target.name + '-')
try:
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(config, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    parent = os.open(target.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    Path(temporary).unlink(missing_ok=True)

PY
run_id=$(python3 - "$runtime" <<'PY'
import json,sys,uuid
from pathlib import Path
site=json.loads(Path(sys.argv[1]).read_text())['company_binding']['site_name']
print(uuid.UUID(hex=site.removeprefix('erp-sim-').removesuffix('.localhost')))
PY
)
running_services=$(docker compose --file "$compose" ps --status running --services)
while IFS= read -r service; do
  case "$service" in
    backend|frontend|websocket|queue-short|queue-long) previously_running+=("$service") ;;
  esac
done <<< "$running_services"
restart_needed=1
docker compose --file "$compose" stop backend frontend websocket queue-short queue-long
docker compose --file "$compose" --profile admin run --rm site-admin install-business-apps "$run_id"
docker compose --file "$compose" up -d --wait
restart_needed=0
