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
image=tac-erpnext-apps:2026-11-07
tools_urls=${COMPANY_TOOLS_URLS_FILE:-/data/theagentcompany/enterprise/secrets/company-tools-urls.json}
# Forward-initialize app role bindings in the existing authoritative directory.
python3 - "$runtime" "$apps_dir/../../../single-host/company-identities.json" <<'PYROLES'
import json,sys
from pathlib import Path
runtime=json.loads(Path(sys.argv[1]).read_text())
directory=Path(runtime['company_profile_file'])
current=json.loads(directory.read_text())
defaults={e['id']:e['frappeAppRoles'] for e in json.loads(Path(sys.argv[2]).read_text())['employees']}
for employee in current['employees']:
    if 'frappeAppRoles' not in employee:
        employee['frappeAppRoles']=defaults[employee['id']]
directory.write_text(json.dumps(current,indent=2,ensure_ascii=False)+'\n')
PYROLES
bash "$apps_dir/build.sh"
# Render with Compose before changing the image, retaining deployment-specific settings.
rendered=$(mktemp)
trap 'rm -f -- "$rendered"' EXIT
docker compose --file "$compose" --profile "*" config --format json > "$rendered"
python3 - "$rendered" "$compose" "$image" "$tools_urls" <<'PY'
import json,sys
from pathlib import Path
source, destination, image, tools_urls = sys.argv[1:]
config=json.loads(Path(source).read_text())
config['services']['site-admin']['environment']['TAC_ERP_PUBLIC_URL']=json.loads(Path(tools_urls).read_text())['erpnext']
for name in config['services'].keys() & {'configurator','backend','frontend','websocket','queue-short','queue-long','scheduler','site-admin'}:
    config['services'][name]['image']=image
Path(destination).write_text(json.dumps(config,indent=2)+'\n')
Path(destination).chmod(0o600)

PY
run_id=$(python3 - "$runtime" <<'PY'
import json,sys,uuid
from pathlib import Path
site=json.loads(Path(sys.argv[1]).read_text())['company_binding']['site_name']
print(uuid.UUID(hex=site.removeprefix('erp-sim-').removesuffix('.localhost')))
PY
)
docker compose --file "$compose" stop backend frontend websocket queue-short queue-long
docker compose --file "$compose" --profile admin run --rm site-admin install-business-apps "$run_id"
docker compose --file "$compose" up -d --wait
