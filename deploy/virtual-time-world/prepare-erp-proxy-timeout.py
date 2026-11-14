#!/usr/bin/env python3
"""Keep ERP nginx's real-time proxy timeout stable as guest time changes."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess


HELPER = Path(__file__).with_name('prepare-healthcheck-cadence.py')
spec = importlib.util.spec_from_file_location('healthcheck_cadence', HELPER)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


def compose_dir():
    output = subprocess.check_output(
        ['systemctl', 'show', '--property=Environment', '--value', 'agentic-company-compose.service'],
        text=True,
    )
    values = dict(item.split('=', 1) for item in shlex.split(output) if '=' in item)
    if not values.get('COMPOSE_CONFIG_DIR'):
        raise RuntimeError('Company Compose configuration directory is unavailable')
    return Path(values['COMPOSE_CONFIG_DIR'])


def sync(config_dir, rate):
    if rate < 1:
        raise ValueError('Simulation rate must be a positive integer')
    config_path = config_dir / 'tac-enterprise-erp.json'
    if not config_path.exists():
        return False
    config = json.loads(config_path.read_text())
    if config.get('name') != 'tac-enterprise-erp':
        raise RuntimeError('ERP Compose project identity does not match')
    env = config['services']['frontend']['environment']
    baseline_dir = config_dir.parent / 'erp-proxy-timeout-baseline'
    baseline_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    baseline_path = baseline_dir / 'frontend.json'
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())['proxy_read_timeout_seconds']
    else:
        baseline = int(env['PROXY_READ_TIMEOUT'])
    if type(baseline) is not int or baseline <= 0:
        raise RuntimeError('ERP proxy timeout baseline must be a positive integer')
    if not baseline_path.exists():
        healthcheck.write_private(baseline_path, {'proxy_read_timeout_seconds': baseline})
    timeout = baseline * rate
    ids = subprocess.check_output([
        'docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=tac-enterprise-erp',
        '--filter', 'label=com.docker.compose.service=frontend',
    ], text=True).split()
    for container in ids:
        config_file = '/etc/nginx/conf.d/frappe.conf'
        original = subprocess.check_output(['docker', 'exec', container, 'cat', config_file], text=True)
        updated, count = re.subn(r'(?m)^(\s*proxy_read_timeout\s+)\d+s?;',
                                 lambda m: f'{m.group(1)}{timeout};', original)
        if count != 1:
            raise RuntimeError('Expected exactly one ERP nginx proxy read timeout')
        if updated != original:
            writer = ['docker', 'exec', '-i', '--user', '0', container, 'python3', '-c',
                      'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())', config_file]
            subprocess.run(writer, input=updated, text=True, check=True)
            try:
                subprocess.run(['docker', 'exec', container, 'nginx', '-t'], check=True, capture_output=True, text=True)
                subprocess.run(['docker', 'exec', container, 'nginx', '-s', 'reload'], check=True, capture_output=True, text=True)
            except (OSError, subprocess.SubprocessError) as cause:
                try:
                    subprocess.run(writer, input=original, text=True, check=True)
                except (OSError, subprocess.SubprocessError) as rollback:
                    raise ExceptionGroup('ERP nginx update and rollback failed', [cause, rollback]) from cause
                raise
    env['PROXY_READ_TIMEOUT'] = timeout
    healthcheck.write_private(config_path, config)
    subprocess.run(['docker', 'compose', '-f', str(config_path), '--profile', '*', 'config', '--quiet'],
                   check=True, capture_output=True, text=True)
    print(f'ERP proxy read timeout: {timeout}s at {rate}x')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path)
    parser.add_argument('--rate', type=int)
    args = parser.parse_args()
    rate = args.rate if args.rate is not None else int(Path('/sys/kernel/virtual_time_rate').read_text().strip())
    sync(args.config_dir if args.config_dir is not None else compose_dir(), rate)


if __name__ == '__main__':
    main()
