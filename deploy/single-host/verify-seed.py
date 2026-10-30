"""Verify original task-author input bytes through the served ownCloud WebDAV API."""
import hashlib
import importlib.util
import json
import pathlib
from urllib.parse import quote

import requests

ROOT = pathlib.Path('/data/theagentcompany')
spec = importlib.util.spec_from_file_location('tac_config', ROOT / 'source/workspaces/base_image/config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
# Independently recorded from the original PR attachments; see the seed-repair provenance.
expected = {
    'Documents/Human Resources Team/Contracts/contracts.xlsx': '5fde7f7334504fdc2f5d46e6e110f0461bbba9049b1da486f32127992c435351',
    'Documents/Human Resources Team/Contracts/revenue_schedule.xlsx': 'cd1e6684d6191415a7aff0ae241befdb208f421bac20d8b3874138587426d07b',
    'Documents/Human Resources Team/Contracts/employee_contracts.xlsx': '0d24827a7482d6b6cfea948ffa3ce9562d9da401a80275507392c7936b715214',
    'Documents/Research/test_image.jpg': '3bcc67667327871f3b1b1e71552bbd7b230195cd988d6bfe26586062a8580343',
    'Documents/Financials/Expenses/category_rules.txt': '708c6f4b060b90f962ff24833269301469c314560a0a8a2468f694cf38a8c587',
}
session = requests.Session()
session.auth = (config.OWNCLOUD_USERNAME, config.OWNCLOUD_PASSWORD)
session.headers['Host'] = 'the-agent-company.com:8092'
results = []
for path, digest in expected.items():
    response = session.get('http://127.0.0.1:8092/remote.php/webdav/' + quote(path), timeout=60)
    response.raise_for_status()
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != digest:
        raise RuntimeError('Served benchmark input checksum mismatch: ' + path)
    results.append({'path': path, 'sha256': actual, 'bytes': len(response.content), 'http_status': response.status_code})
report = {'status': 'passed', 'original_assets_verified': len(results), 'files': results}
(ROOT / 'owncloud-seed-verification.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
