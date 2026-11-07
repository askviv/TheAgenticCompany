#!/usr/bin/env python3
"""Install the company dashboard from source using standard Node and Ubuntu Python."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, check=True, cwd=cwd, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    profile = json.loads(args.profile.read_text())
    config = args.profile.parent
    root = Path('/data/theagentcompany')
    enterprise = root / 'enterprise'
    principals = json.loads((enterprise / 'secrets/principals.json').read_text())
    controllers = [token for token, record in principals.items() if record['role'] == 'controller']
    if len(controllers) != 1:
        raise RuntimeError('The coordinator must provide exactly one dashboard controller principal')
    token_path = enterprise / 'secrets/dashboard-controller-token'
    token_path.write_text(controllers[0] + '\n')
    token_path.chmod(0o600)

    frontend = source / 'servers/experiments'
    for command in ('ci', 'run'):
        node_args = ['docker', 'run', '--rm', '--mount', f'type=bind,source={frontend},target=/frontend',
                     '--workdir', '/frontend/web', 'node:24-bookworm-slim', 'npm', command]
        if command == 'run':
            node_args.append('build')
        run(*node_args)
    venv = root / 'dashboard-venv'
    run(sys.executable, '-m', 'venv', str(venv))
    run(str(venv / 'bin/pip'), 'install', '--no-cache-dir',
        'Flask==3.1.3', 'requests==2.34.2', 'PyYAML==6.0.3')

    domains = profile['domains']
    tools = {'erpnext': 'erp', 'email': 'mail', 'rocketchat': 'chat',
             'owncloud': 'owncloud', 'plane': 'plane', 'midpoint': 'midpoint'}
    tool_urls = {key: f'https://{domains[value]}'
                 + ('/webmail/' if key == 'email' else '')
                 for key, value in tools.items()}
    tool_urls.update({key: f'https://{domains["erp"]}{path}' for key, path in {
        'payroll': '/hrms', 'lending': '/app/lending',
        'crm': '/login?redirect-to=%2Fcrm', 'helpdesk': '/helpdesk',
    }.items()})
    tools_path = config / 'company-tools-urls.json'
    tools_path.write_text(json.dumps(tool_urls, indent=2) + '\n')
    tools_path.chmod(0o600)
    env_path = config / 'dashboard.env'
    previous = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, _, value = line.partition('=')
            if key in {'EXPERIMENTS_SECRET_KEY', 'EXPERIMENTS_WEBHOOK_TOKEN'}:
                previous[key] = json.loads(value)
    environment = {
        'EXPERIMENTS_SECRET_KEY': previous.get('EXPERIMENTS_SECRET_KEY') or secrets.token_urlsafe(48),
        'EXPERIMENTS_WEBHOOK_TOKEN': previous.get('EXPERIMENTS_WEBHOOK_TOKEN') or secrets.token_urlsafe(48),
        'EXPERIMENTS_DB': str(root / 'experiments-data/experiments.sqlite'),
        'TAC_REPO': str(source),
        'COMPANY_PROFILE_FILE': str(config / 'company-identities.json'),
        'COMPANY_TOOLS_URLS_FILE': str(tools_path),
        'SIMULATION_COORDINATOR_URL': 'http://127.0.0.1:8131',
        'SIMULATION_CONTROLLER_TOKEN_FILE': str(token_path),
        'ROCKETCHAT_BASE_URL': f'https://{domains["chat"]}',
        'ROCKETCHAT_PUBLIC_URL': f'https://{domains["chat"]}',
        'EXPERIMENTS_PUBLIC_URL': f'https://{domains["experiments"]}',
        'ROCKETCHAT_USERNAME': 'theagentcompany',
        'ROCKETCHAT_PASSWORD': profile['sandbox_password'],
        'VIV_ROCKETCHAT_USERNAME': profile.get('viv', {}).get('chat_username', 'viv'),
        'EXPERIMENTS_ROCKETCHAT_CHANNEL': 'general',
        'CODEX_AUTH_BRIDGE_URL': 'http://10.0.2.2:8143',
        'CODEX_AUTH_BRIDGE_TOKEN_FILE': str(enterprise / 'actors/secrets/codex-inference-token'),
        'REQUESTS_CA_BUNDLE': '/etc/ssl/certs/ca-certificates.crt',
    }
    env_path.write_text(''.join(f'{key}={json.dumps(value)}\n' for key, value in environment.items()))
    env_path.chmod(0o600)
    run(str(venv / 'bin/python'), '-c',
        'from servers.experiments.runner import RocketChatClient; '
        'RocketChatClient.from_env().setup_outgoing_webhook('
        'endpoint="http://host.docker.internal:8110/api/chat-events")',
        cwd=source, env={**os.environ, **environment})
    (root / 'experiments-data').mkdir(mode=0o700, parents=True, exist_ok=True)
    unit = f'''[Unit]
Description=The Agentic Company dashboard
After=tac-enterprise-simulation.service
Wants=tac-enterprise-simulation.service

[Service]
Type=simple
WorkingDirectory={source}
EnvironmentFile={env_path}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={venv}/bin/python -m servers.experiments.app
Restart=on-failure
UMask=0077

[Install]
WantedBy=agentic-company.target
'''
    Path('/etc/systemd/system/tac-experiments.service').write_text(unit)
    target = Path('/etc/systemd/system/agentic-company.target.d/dashboard.conf')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('[Unit]\nWants=tac-experiments.service\nAfter=tac-experiments.service\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'tac-experiments.service')
    print('Dashboard installed from source; no custom application image was built.')


if __name__ == '__main__':
    main()
