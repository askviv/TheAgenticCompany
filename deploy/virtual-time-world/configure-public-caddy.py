#!/usr/bin/env python3
"""Render the host's public Caddy frontend for a fresh virtual company world."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

DOMAINS = ('owncloud', 'chat', 'plane', 'mail', 'erp', 'midpoint', 'experiments')
HOST = re.compile(r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$')


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--guest-root-ca', type=Path, required=True)
    parser.add_argument('--config-dir', type=Path, default=Path('/etc/caddy/company-world'))
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    domains = profile.get('domains')
    if not isinstance(domains, dict):
        raise ValueError('deployment profile must contain domains')
    selected = {name: domains[name].strip().rstrip('.').lower() for name in DOMAINS}
    for name, domain in selected.items():
        if len(domain) > 253 or not HOST.fullmatch(domain) or '..' in domain:
            raise ValueError(f'invalid domain in domains.{name}')
    if len(set(selected.values())) != len(selected):
        raise ValueError('each company service requires a distinct public hostname')
    if not args.guest_root_ca.is_file():
        raise FileNotFoundError(f'guest CA certificate is missing: {args.guest_root_ca}')

    args.config_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(args.config_dir, 0, __import__('grp').getgrnam('caddy').gr_gid)
    args.config_dir.chmod(0o750)
    root_ca = args.config_dir / 'guest-root-ca.crt'
    shutil.copyfile(args.guest_root_ca, root_ca)
    os.chown(root_ca, 0, __import__('grp').getgrnam('caddy').gr_gid)
    root_ca.chmod(0o644)

    tls = profile.get('public_tls', {})
    cert_source, key_source = tls.get('cert_file'), tls.get('key_file')
    if bool(cert_source) != bool(key_source):
        raise ValueError('public_tls.cert_file and public_tls.key_file must be supplied together')
    tls_directive = ''
    if cert_source:
        cert_source, key_source = Path(cert_source), Path(key_source)
        if not cert_source.is_file() or not key_source.is_file():
            raise FileNotFoundError('configured public TLS certificate and key files must exist')
        cert_path, key_path = args.config_dir / 'fullchain.pem', args.config_dir / 'privkey.pem'
        shutil.copyfile(cert_source, cert_path)
        shutil.copyfile(key_source, key_path)
        os.chown(cert_path, 0, __import__('grp').getgrnam('caddy').gr_gid)
        os.chown(key_path, 0, __import__('grp').getgrnam('caddy').gr_gid)
        cert_path.chmod(0o644)
        key_path.chmod(0o640)
        tls_directive = f'\n\ttls {cert_path} {key_path}'

    sites = []
    for domain in selected.values():
        job_status = ''
        if domain == selected['experiments']:
            job_status = '''
\t@company_job_status {
\t\tpath /api/company-environments/jobs/*
\t\tmethod GET POST
\t}
\thandle @company_job_status {
\t\turi strip_prefix /api/company-environments/jobs
\t\trewrite * /company-switch/jobs{path}
\t\treverse_proxy 127.0.0.1:8133
\t}
'''
        sites.append(f'''{domain} {{{tls_directive}
{job_status}\thandle {{
\treverse_proxy https://127.0.0.1:18443 {{
\t\theader_up Host {{host}}
\t\ttransport http {{
\t\t\ttls_server_name {domain}
\t\t\ttls_trust_pool file {root_ca}
\t\t}}
\t}}
\t}}
}}''')
    caddyfile = ('{\n\tadmin unix//run/tac-https/admin.sock\n'
                 '\tstorage file_system /var/lib/tac-https/caddy\n}\n\n'
                 + '\n\n'.join(sites) + '\n')
    output = args.config_dir / 'Caddyfile'
    atomic_write(output, caddyfile, 0o640)
    os.chown(output, 0, __import__('grp').getgrnam('caddy').gr_gid)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
