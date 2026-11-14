#!/usr/bin/env python3
"""Install a private company dataset inventory from the existing guest deployment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CONTROL = Path('/etc/company-workplace/company-environments.json')
MARKER = Path('/etc/company-workplace/company-switch-in-progress')
INSTALL_ROOT = Path('/usr/local/lib/virtual-time-world')
DATA = Path('/data/theagentcompany')
WRITERS = (
    'tac-enterprise-simulation.service', 'tac-customer-actor.service',
    'tac-supplier-actor.service', 'tac-company-provider-portal.service',
    'tac-company-issue-recovery.timer', 'tac-company-issue-recovery.service',
    'tac-company-issues.timer', 'tac-company-issues.service',
    'tac-experiments.service', 'agentic-company-compose.service',
)


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def private_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inventory(runtime_path, compose_dir, source, root):
    runtime = json.loads(runtime_path.read_text())
    # Provision only persistent home volumes, using the desktop runtime's own
    # naming and ownership checks. No employee desktop or worker is started.
    # Inventorying all canonical homes now also covers desktops created later.
    sys.path.insert(0, str(source))
    from servers.simulation.employee_desktops import EmployeeDesktopRuntime
    desktop = EmployeeDesktopRuntime(
        Path(runtime['company_profile_file']), Path(runtime['state_root']) / 'employee-desktops')
    profile_volumes = []
    for employee in desktop._employees():
        volume = desktop._volume_name(employee['id'])
        desktop._ensure_volume(volume, employee['id'])
        profile_volumes.append(volume)
    compose_files = sorted(compose_dir.glob('*.json'))
    if not compose_files:
        raise ValueError('No rendered Compose projects found')
    containers = set()
    for compose in compose_files:
        containers.update(run('docker', 'compose', '-f', str(compose), 'ps', '-aq').split())
    dynamic = run('docker', 'ps', '-aq', '--filter', 'name=^tac-employee-desktop-').split()
    containers.update(dynamic)
    paths = set()
    if profile_volumes:
        paths.update(Path(volume['Mountpoint']).resolve()
                     for volume in json.loads(run('docker', 'volume', 'inspect', *profile_volumes)))
    if containers:
        inspected = json.loads(run('docker', 'inspect', *sorted(containers)))
        for container in inspected:
            for mount in container.get('Mounts', []):
                path = Path(mount['Source']).resolve()
                if mount['Type'] == 'volume':
                    paths.add(path)
                elif mount['Type'] == 'bind' and mount.get('RW') and path.is_relative_to(DATA):
                    paths.add(path)
    # These hold durable native state and credentials outside ordinary app mounts.
    paths.update((runtime_path.parent, compose_dir, Path(runtime['state_root']),
                  Path(runtime['mail_credentials_file']).parent,
                  Path(runtime['erp_credentials_root']).parent))
    for optional in (Path('/etc/company-workplace/secrets'),
                     Path('/etc/company-workplace/deployment-profile.json'),
                     DATA / 'experiments.env'):
        if optional.exists():
            paths.add(optional)
    profile = Path(runtime['company_profile_file']).resolve()
    enterprise = runtime_path.parent.parent
    excluded = {root.resolve(), CONTROL.resolve(), MARKER.resolve(), source.resolve(),
                enterprise / 'source', enterprise / 'venv', enterprise / 'tooling',
                enterprise / 'actors/venvs', DATA / 'virtual-time-world',
                Path('/var/run/docker.sock'), Path('/run/docker.sock')}
    # A broad writable bind can contain shared code or snapshot storage. Split it
    # into children instead of ever copying those global/control directories.
    selected = set()
    def capture(path):
        path = path.resolve()
        if any(path == skip or path.is_relative_to(skip) for skip in excluded):
            return
        if any(skip.is_relative_to(path) for skip in excluded):
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    capture(child)
            return
        selected.add(path)
    for path in paths:
        capture(path)
    selected.add(profile)  # Canonical company configuration may live beside shared code.
    # Existing deployments can keep rendered application configuration beneath
    # virtual-time-world/, whose other contents are shared VM infrastructure.
    selected.add(compose_dir.resolve())
    selected = sorted(selected, key=lambda path: (len(path.parts), str(path)))
    roots = []
    for path in selected:
        if not any(path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    units = [unit for unit in WRITERS
             if run('systemctl', 'show', unit, '--property=LoadState', '--value').strip() == 'loaded']
    return {'root': str(root), 'compose_files': [str(path.resolve()) for path in compose_files],
            'service_units': units, 'data_paths': [str(path) for path in roots],
            'additional_containers': []}


def initial_lifecycle(configuration):
    containers = {}
    running = {}
    for compose in configuration['compose_files']:
        identifiers = run('docker', 'compose', '-f', compose, 'ps', '-aq').split()
        project = json.loads(run('docker', 'inspect', *identifiers)) if identifiers else []
        containers.update({container['Id']: container for container in project})
        running[compose] = sorted({container['Config']['Labels']['com.docker.compose.service']
                                   for container in project if container['State']['Running']})
    active = [unit for unit in configuration['service_units']
              if unit not in {'agentic-company.target', 'agentic-company-compose.service'}
              and run('systemctl', 'show', unit, '--property=ActiveState', '--value').strip()
              in {'active', 'activating', 'reloading'}]
    policies = [{'project': container['Config']['Labels']['com.docker.compose.project'],
                 'service': container['Config']['Labels']['com.docker.compose.service'],
                 'policy': container['HostConfig']['RestartPolicy']} for container in containers.values()]
    return {'active_units': active, 'running_services': running, 'restart_policies': policies}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=DATA / 'enterprise/config/runtime.json')
    parser.add_argument('--compose-dir', type=Path, default=Path('/etc/company-workplace/compose'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=DATA / 'company-environments')
    parser.add_argument('--refresh-inventory', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("Installing the company dataset inventory requires root")
    runtime_python = args.runtime_config.resolve().parent.parent / 'venv/bin/python'
    if not runtime_python.exists():
        raise FileNotFoundError('The deployed runtime virtual environment is required')
    sys.path.insert(0, str(args.source / 'deploy/virtual-time-world/guest'))
    from company_data import Configuration
    helper_source = args.source / 'deploy/virtual-time-world/guest/company_data.py'
    helper = '#!' + str(runtime_python) + '\n' + helper_source.read_text().split('\n', 1)[1]
    private_write(INSTALL_ROOT / 'company-data', helper, 0o755)
    stop = INSTALL_ROOT / 'company-compose-stop'
    if not stop.exists():
        private_write(stop, (args.source / 'deploy/virtual-time-world/company-compose-stop.py').read_text(), 0o755)
    if CONTROL.exists() and not args.refresh_inventory:
        configuration = Configuration.model_validate_json(CONTROL.read_bytes()).model_dump(mode='json')
    else:
        configuration = Configuration.model_validate(inventory(
            args.runtime_config.resolve(), args.compose_dir.resolve(),
            args.source.resolve(), args.root.resolve())).model_dump(mode='json')
        if CONTROL.exists():
            previous = json.loads(CONTROL.read_text())
            if previous != configuration and Path(previous['root']).exists() and any(Path(previous['root']).iterdir()):
                raise ValueError('Company snapshots or journals exist; changing the inventory would invalidate their configuration digest')
        private_write(CONTROL, json.dumps(configuration, indent=2) + '\n')
    lifecycle_path = Path(configuration['root']) / 'active-lifecycle.json'
    if not lifecycle_path.exists():
        snapshot_root = Path(configuration['root'])
        if snapshot_root.exists() and any(snapshot_root.iterdir()):
            raise ValueError('Existing company state has no active lifecycle; operator reconciliation is required')
        snapshot_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshot_root.chmod(0o700)
        private_write(lifecycle_path, json.dumps(initial_lifecycle(configuration), indent=2) + '\n')
    for unit in configuration['service_units']:
        private_write(Path('/etc/systemd/system') / (unit + '.d/company-environments.conf'),
                      '[Unit]\nConditionPathExists=!' + str(MARKER) + '\n', 0o644)
    run('systemctl', 'daemon-reload')
    print('Company environment helper and private inventory installed; no company data was switched.')


if __name__ == '__main__':
    main()
