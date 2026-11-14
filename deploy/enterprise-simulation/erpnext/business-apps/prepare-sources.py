#!/usr/bin/env python3
"""Stage commit-locked Git sources for the native Bench installer."""
import argparse
import json
from pathlib import Path
import re
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('builder', type=Path)
arguments = parser.parse_args()
root = Path(__file__).resolve().parent
apps = json.loads((root / 'apps.json').read_text())
framework = json.loads((root / 'framework.json').read_text())
sources = arguments.builder / 'app-sources'
sources.mkdir()
manifest = []
for app in [framework, *apps]:
    revision = app['revision']
    if re.fullmatch(r'[0-9a-f]{40}', revision) is None:
        raise ValueError('Every app source must have a full Git commit SHA')
    name = app['url'].rstrip('/').rsplit('/', 1)[1].removesuffix('.git')
    checkout = sources / name
    subprocess.run(['git', 'init', '--quiet', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'remote', 'add', 'origin', app['url']], check=True)
    subprocess.run(['git', '-C', str(checkout), 'fetch', '--quiet', '--depth', '1', 'origin', revision], check=True)
    actual = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'FETCH_HEAD^{commit}'], text=True).strip()
    if actual != revision:
        raise ValueError('Fetched app source differs from its recorded commit')
    subprocess.run(['git', '-C', str(checkout), 'checkout', '--quiet', '-B', 'pinned', revision], check=True)
    # Bench's native manifest accepts local checkouts and branch names. This
    # private build-context ref cannot move independently of the recorded SHA.
    if app is not framework:
        manifest.append({'url': f'file:///opt/app-sources/{name}', 'branch': 'pinned'})
(sources / 'apps.json').write_text(json.dumps(manifest, indent=2) + '\n')
containerfile = arguments.builder / 'images/custom/Containerfile'
text = containerfile.read_text()
marker = '# Apply group permissions after app installation and cleanup, before the runtime COPY.'
if text.count(marker) != 1:
    raise ValueError('The pinned upstream Containerfile no longer has the expected source staging point')
containerfile.write_text(text.replace(marker,
    'COPY --from=app_sources --chown=frappe:0 . /opt/app-sources/\n\n' + marker))
