#!/usr/bin/env python3
"""Quiesce and switch trusted company application datasets; never copy VM disks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

STOP_HELPER = '/usr/local/lib/virtual-time-world/company-compose-stop'
SWITCH_MARKER = Path('/etc/company-workplace/company-switch-in-progress')
Identifier = Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9-]{0,99}$')]


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    root: Path
    compose_files: list[Path] = Field(min_length=1)
    service_units: list[Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.@:-]*\.(?:service|timer|target|socket|path)$')]]
    data_paths: list[Path] = Field(min_length=1)
    additional_containers: list[Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')]] = Field(default_factory=list)

    @model_validator(mode='after')
    def trusted_paths(self):
        paths = [self.root, *self.compose_files, *self.data_paths]
        if any(not path.is_absolute() or '..' in path.parts for path in paths):
            raise ValueError('Company dataset configuration requires absolute normalized paths')
        data = [path.resolve() for path in self.data_paths]
        root = self.root.resolve()
        control_files = [SWITCH_MARKER.resolve(), Path('/etc/company-workplace/company-environments.json').resolve()]
        if any(path == control or path in control.parents for path in data for control in control_files):
            raise ValueError('Company switch control files must remain outside swapped dataset roots')
        for index, path in enumerate(data):
            if path == Path('/') or path == root or path in root.parents or root in path.parents:
                raise ValueError('Company dataset storage must be separate from captured paths')
            if any(path == other or path in other.parents or other in path.parents for other in data[index + 1:]):
                raise ValueError('Company data paths must be non-overlapping roots')
        if len(set(self.compose_files)) != len(self.compose_files):
            raise ValueError('Company Compose configuration paths must be unique')
        return self


class Arguments(BaseModel):
    company_id: Identifier
    job_id: UUID
    source_id: Identifier | None = None
    snapshot: str | None = None


def run(*command, env=None):
    return subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def replace_dataset(source, destination):
    os.replace(source, destination)
    directory = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class CompanyData:
    def __init__(self, configuration: Configuration, arguments: Arguments):
        self.config, self.arguments = configuration, arguments
        self.root = configuration.root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.job_directory = self.root / 'jobs' / str(arguments.job_id)
        self.job_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal_path = self.job_directory / 'journal.json'
        self.config_digest = hashlib.sha256(json.dumps({
            'data_paths': [str(path) for path in configuration.data_paths],
        }, sort_keys=True).encode()).hexdigest()

    @contextmanager
    def locked(self):
        with (self.root / 'operation.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    def journal(self):
        if self.journal_path.exists():
            journal = json.loads(self.journal_path.read_text())
            if journal['config_digest'] != self.config_digest:
                raise ValueError('Company data paths changed during a journaled operation')
            return journal
        return {'job_id': str(self.arguments.job_id), 'config_digest': self.config_digest,
                'status': 'created', 'paths': [], 'captures': {}}

    def save(self, journal):
        atomic_json(self.journal_path, journal)

    def _containers(self):
        identifiers = run('docker', 'ps', '-aq').split()
        if not identifiers:
            return []
        return json.loads(run('docker', 'inspect', *identifiers))

    def _assert_stopped(self):
        data = [path.resolve() for path in self.config.data_paths]
        for container in self._containers():
            for mount in container.get('Mounts', []):
                if mount.get('Type') not in {'bind', 'volume'}:
                    continue
                source = Path(mount['Source']).resolve()
                if any(source == path or source in path.parents or path in source.parents for path in data):
                    if container['State']['Running']:
                        raise RuntimeError('A container mounting captured company data is still running')
        for path in self.config.data_paths:
            if path.is_symlink() or os.path.ismount(path):
                raise ValueError('Captured data roots must be real directories/files, not symlinks or mountpoints')

    def _project_containers(self):
        identifiers = set()
        for config in self.config.compose_files:
            identifiers.update(run('docker', 'compose', '-f', str(config), 'ps', '-aq').split())
        return json.loads(run('docker', 'inspect', *sorted(identifiers))) if identifiers else []

    @staticmethod
    def _managed_desktops(containers):
        desktops = []
        for container in containers:
            name = container['Name'].lstrip('/')
            if not name.startswith('tac-employee-desktop-'):
                continue
            labels = container['Config'].get('Labels') or {}
            employee_key = labels.get('tac.employee-desktop.employee-id', '')
            if (labels.get('tac.employee-desktop.managed') != 'true'
                    or not re.fullmatch(r'[0-9a-f]{64}', employee_key)
                    or name != 'tac-employee-desktop-' + employee_key[:24]):
                raise RuntimeError('An employee desktop name has conflicting managed ownership labels')
            expected_volume = 'tac-employee-profile-' + employee_key[:24]
            if not any(mount.get('Type') == 'volume' and mount.get('Name') == expected_volume
                       and mount.get('Destination') == '/home/ubuntu' for mount in container.get('Mounts', [])):
                raise RuntimeError('A managed employee desktop has a conflicting persistent profile volume')
            desktops.append(container)
        return desktops

    def _retire_desktop_containers(self):
        containers = self._managed_desktops(self._containers())
        for container in containers:
            if container['State']['Running']:
                raise RuntimeError('A managed employee desktop is still running before namespace activation')
            for mount in container.get('Mounts', []):
                if mount.get('Type') in {'bind', 'volume'} and mount.get('RW') and not any(
                        Path(mount['Source']).resolve() == path.resolve()
                        or path.resolve() in Path(mount['Source']).resolve().parents
                        for path in self.config.data_paths):
                    raise ValueError('A managed desktop has writable data outside the trusted inventory')
        for container in containers:
            # Keep every named/anonymous volume. The next desktop action creates
            # fresh container credentials from the newly restored namespace.
            run('docker', 'rm', container['Id'])

    def quiesce(self):
        journal = self.journal()
        if journal.get('committed'):
            raise RuntimeError('This completed data operation requires a new job ID')
        marker = {'job_id': str(self.arguments.job_id), 'status': 'switching'}
        if SWITCH_MARKER.exists() and json.loads(SWITCH_MARKER.read_text())['job_id'] != marker['job_id']:
            raise RuntimeError('A different company switch owns the service boot interlock')
        atomic_json(SWITCH_MARKER, marker)
        journal['status'] = 'quiescing'
        if 'active_units' not in journal:
            active = []
            for unit in self.config.service_units:
                state = run('systemctl', 'show', '--property=ActiveState', '--value', unit).strip()
                if state in {'active', 'activating', 'reloading'}:
                    active.append(unit)
            journal['active_units'] = active
        project = {container['Id']: container for container in self._project_containers()}
        self._managed_desktops(self._containers())
        extra = {container['Id']: container for container in self._containers()
                 if container['Name'].lstrip('/').startswith('tac-employee-desktop-')
                 or container['Name'].lstrip('/') in self.config.additional_containers}
        for container in extra.values():
            for mount in container.get('Mounts', []):
                if mount.get('Type') in {'bind', 'volume'} and mount.get('RW') and not any(
                        Path(mount['Source']).resolve() == path.resolve()
                        or path.resolve() in Path(mount['Source']).resolve().parents
                        for path in self.config.data_paths):
                    raise ValueError('A managed additional container has writable data outside the trusted inventory')
        observed = journal.setdefault('observed_containers', {})
        for identifier, container in (project | extra).items():
            labels = container['Config'].get('Labels') or {}
            observed.setdefault(identifier, {'name': container['Name'].lstrip('/'),
                'project': labels.get('com.docker.compose.project'),
                'service': labels.get('com.docker.compose.service'),
                'policy': container['HostConfig']['RestartPolicy']})
        if 'restart_policies' not in journal:
            journal['restart_policies'] = {identifier: container['HostConfig']['RestartPolicy']
                                           for identifier, container in (project | extra).items()}
            running = {}
            for config in self.config.compose_files:
                project_name = json.loads(config.read_text())['name']
                running[str(config)] = sorted({
                    container['Config']['Labels']['com.docker.compose.service']
                    for container in project.values()
                    if container['State']['Running']
                    and container['Config']['Labels'].get('com.docker.compose.project') == project_name
                })
            journal['source_lifecycle'] = {
                'active_units': journal['active_units'], 'running_services': running,
                'additional_containers': [
                    {'name': container['Name'].lstrip('/'),
                     'running': container['State']['Running'],
                     'policy': container['HostConfig']['RestartPolicy']}
                    for container in extra.values()
                    if container['Name'].lstrip('/') in self.config.additional_containers],
                'restart_policies': [{'project': container['Config']['Labels'].get('com.docker.compose.project'),
                    'service': container['Config']['Labels'].get('com.docker.compose.service'),
                    'policy': container['HostConfig']['RestartPolicy']} for container in project.values()],
            }
        self.save(journal)
        for identifier in project | extra:
            run('docker', 'update', '--restart=no', identifier)
        if self.config.service_units:
            run('systemctl', 'stop', *self.config.service_units)
        compose_directory = self.job_directory / 'compose'
        compose_directory.mkdir(exist_ok=True, mode=0o700)
        for index, config in enumerate(self.config.compose_files):
            link = compose_directory / (str(index) + '.json')
            if not link.exists():
                link.symlink_to(config)
            elif link.resolve() != config.resolve():
                raise ValueError('Journaled Compose configuration link changed')
        environment = dict(os.environ, COMPOSE_CONFIG_DIR=str(compose_directory),
                           RESTART_POLICY_JOURNAL=str(self.job_directory / ('drain-restart-policies-' + str(uuid4()) + '.json')))
        run(STOP_HELPER, env=environment)
        # Keep Docker daemon restarts from booting a partially restored dataset.
        # Original values are immutable in this job and restored by start().
        for identifier in project | extra:
            run('docker', 'update', '--restart=no', identifier)
        if extra:
            run('docker', 'stop', '--time=-1', *extra)
        self._assert_stopped()
        journal['status'] = 'quiesced'
        self.save(journal)
        return {'status': 'quiesced'}

    def _pointer_path(self, identifier):
        return self.root / 'companies' / identifier / 'current.json'

    def _snapshot(self, identifier, token=None):
        pointer = ({'generation': token, 'config_digest': self.config_digest} if token is not None
                   else json.loads(self._pointer_path(identifier).read_text()))
        relative = Path(pointer['generation'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Company generation pointer must stay in immutable snapshot storage')
        generation = (self.root / 'snapshots' / relative).resolve()
        if not generation.is_relative_to((self.root / 'snapshots').resolve()):
            raise ValueError('Company generation pointer escapes snapshot storage')
        manifest = json.loads((generation / 'manifest.json').read_text())
        if manifest['config_digest'] != self.config_digest:
            raise ValueError('Company generation data paths do not match trusted configuration')
        return pointer, generation, manifest

    @staticmethod
    def _copy(source, destination, directory, *, link_dest=None):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory:
            destination.mkdir(exist_ok=True, mode=0o700)
            # These are trusted native data roots, not extracted pack paths.
            # Archive mode preserves links without following them; --safe-links
            # would silently omit legitimate application links such as assets.
            # Only immutable capture generations are eligible hard-link sources.
            # Compare content as well as metadata: company clock rebasing can make
            # timestamps repeat, so size/mtime alone cannot establish equality.
            previous = ['--checksum', '--link-dest=' + str(link_dest)] if link_dest is not None else []
            run('rsync', '-aHAX', '--numeric-ids', '--fsync', *previous,
                str(source) + '/', str(destination) + '/')
        else:
            run('rsync', '-aHAX', '--numeric-ids', '--fsync', str(source), str(destination))

    def capture(self):
        self._assert_stopped()
        journal = self.journal()
        if journal['status'] not in {'quiesced', 'capturing', 'captured'}:
            raise RuntimeError('Company data capture requires a completed quiesce')
        identifier = self.arguments.company_id
        relative = Path(identifier) / str(self.arguments.job_id)
        generation = self.root / 'snapshots' / relative
        if (generation / 'manifest.json').exists():
            pointer = {'generation': str(relative), 'config_digest': self.config_digest}
            atomic_json(self._pointer_path(identifier), pointer)
            journal['captures'][identifier] = pointer
            journal['status'] = 'captured'
            self.save(journal)
            return pointer | {'snapshot': pointer['generation']}
        published_generation = generation
        previous = (self._snapshot(identifier)[1]
                    if self._pointer_path(identifier).exists() else None)
        generation = generation.with_name(generation.name + '-staging-' + str(uuid4()))
        generation.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal['status'] = 'capturing'
        self.save(journal)
        entries = []
        for index, path in enumerate(self.config.data_paths):
            if not path.exists():
                raise FileNotFoundError('A trusted company data path is unavailable')
            directory = path.is_dir()
            destination = generation / 'data' / str(index)
            self._copy(path, destination, directory,
                       link_dest=previous / 'data' / str(index) if previous is not None and directory else None)
            entries.append({'path': str(path), 'entry': str(index), 'directory': directory})
        self._assert_stopped()
        manifest = {'config_digest': self.config_digest, 'paths': entries,
                    'lifecycle': journal['source_lifecycle']}
        atomic_json(generation / 'manifest.json', manifest)
        run('sync', '-f', str(generation))
        replace_dataset(generation, published_generation)
        pointer = {'generation': str(relative), 'config_digest': self.config_digest}
        atomic_json(self._pointer_path(identifier), pointer)
        journal['captures'][identifier] = pointer
        journal['status'] = 'captured'
        self.save(journal)
        return pointer | {'snapshot': pointer['generation']}

    def duplicate(self):
        if self.arguments.source_id is None:
            raise ValueError('Duplicate requires --source-id')
        capture = self.journal()['captures'].get(self.arguments.source_id)
        token = self.arguments.snapshot or (capture['generation'] if capture else None)
        pointer, _, _ = self._snapshot(self.arguments.source_id, token)
        target = self._pointer_path(self.arguments.company_id)
        if target.exists():
            if json.loads(target.read_text()) != pointer:
                raise ValueError('Target company already references a different immutable generation')
        else:
            atomic_json(target, pointer)
        return pointer | {'snapshot': pointer['generation']}

    def restore(self):
        self._assert_stopped()
        journal = self.journal()
        if journal['status'] in {'committed', 'started', 'rolled_back'}:
            raise RuntimeError('A completed data operation cannot be restored again')
        pointer, generation, manifest = self._snapshot(self.arguments.company_id, self.arguments.snapshot)
        if journal.get('restore_company') not in {None, self.arguments.company_id}:
            raise ValueError('Job is already bound to a different target company')
        if journal.get('restore_generation') not in {None, pointer['generation']}:
            raise ValueError('Target company generation changed during restore')
        if not journal['paths']:
            if journal['status'] not in {'quiesced', 'captured'}:
                raise RuntimeError('Company data restore requires a completed quiesce')
            journal.update(status='restoring', restore_company=self.arguments.company_id,
                           restore_generation=pointer['generation'], target_lifecycle=manifest['lifecycle'])
            for index, item in enumerate(manifest['paths']):
                path = self.config.data_paths[index]
                if item['path'] != str(path):
                    raise ValueError('Snapshot path differs from trusted data path')
                if not path.exists():
                    raise FileNotFoundError('Current dataset root must exist before restore')
                prefix = '.company-data-' + str(self.arguments.job_id) + '-' + str(index)
                journal['paths'].append(dict(item, staged=str(path.parent / (prefix + '.staged')),
                    retired=str(path.parent / (prefix + '.retired')), displaced=str(path.parent / (prefix + '.displaced')),
                    phase='pending'))
            self.save(journal)
        for item in journal['paths']:
            path, staged, retired = Path(item['path']), Path(item['staged']), Path(item['retired'])
            if item['phase'] == 'installed':
                continue
            if item['phase'] == 'pending':
                if retired.exists():
                    raise RuntimeError('Unexpected retired dataset before staging')
                self._copy(generation / 'data' / item['entry'], staged, item['directory'])
                item['phase'] = 'copied'
                self.save(journal)
            if item['phase'] == 'copied':
                if not retired.exists():
                    replace_dataset(path, retired)
                item['phase'] = 'retired'
                self.save(journal)
            if item['phase'] == 'retired':
                if staged.exists():
                    if path.exists():
                        raise RuntimeError('Both original path and staged dataset exist after retirement')
                    replace_dataset(staged, path)
                elif not path.exists():
                    raise RuntimeError('Restored dataset and its staged copy are both unavailable')
                item['phase'] = 'installed'
                self.save(journal)
        journal['status'] = 'restored'
        self.save(journal)
        return {'status': 'restored', 'company_id': self.arguments.company_id}

    def rollback(self):
        journal = self.journal()
        if journal.get('committed'):
            raise RuntimeError('Committed company data requires a new switch operation')
        if journal['status'] in {'started', 'ready_to_start', 'quiescing'}:
            self.quiesce()
            journal = self.journal()
        self._assert_stopped()
        journal['status'] = 'rolling_back'
        self.save(journal)
        for item in reversed(journal['paths']):
            if item['phase'] == 'rolled_back':
                continue
            path, retired, displaced = Path(item['path']), Path(item['retired']), Path(item['displaced'])
            if retired.exists():
                if path.exists():
                    if displaced.exists():
                        raise RuntimeError('Rollback displaced dataset already exists')
                    replace_dataset(path, displaced)
                replace_dataset(retired, path)
            elif not path.exists():
                raise RuntimeError('Original dataset is unavailable during rollback')
            item['phase'] = 'rolled_back'
            self.save(journal)
        if 'source_lifecycle' in journal:
            journal['target_lifecycle'] = journal['source_lifecycle']
        journal['status'] = 'rolled_back'
        self.save(journal)
        return {'status': 'rolled_back'}

    def commit(self):
        journal = self.journal()
        if journal['status'] not in {'committed', 'started'} or not journal.get('services_started'):
            raise RuntimeError('Only successfully started company data changes may be committed')
        if any(item['phase'] != 'installed' for item in journal['paths']):
            raise RuntimeError('Every dataset root must be installed before committing a switch')
        journal['committed'] = True
        journal['status'] = 'committed'
        self.save(journal)
        # Release only this job's redundant rollback copies after a successful
        # startup and a verified immutable capture of the original datasets.
        if journal.get('services_started') and journal['captures']:
            for identifier, pointer in journal['captures'].items():
                self._snapshot(identifier, pointer['generation'])
            for index, item in enumerate(journal['paths']):
                for key in ('retired', 'displaced', 'staged'):
                    retained = Path(item[key])
                    suffix = {'retired': '.retired', 'displaced': '.displaced', 'staged': '.staged'}[key]
                    expected = Path(item['path']).parent / ('.company-data-' + str(self.arguments.job_id) + '-' + str(index) + suffix)
                    if retained != expected or retained.is_symlink():
                        raise RuntimeError('Rollback cleanup path differs from its private journal namespace')
                    if retained.is_dir():
                        shutil.rmtree(retained)
                    elif retained.exists():
                        retained.unlink()
        return {'status': 'committed'}

    def _restore_policies(self, lifecycle):
        # Recreated target containers may have different IDs; native Compose
        # project/service labels remain the stable policy identity.
        policies = {(item['project'], item['service']): item['policy']
                    for item in lifecycle['restart_policies']}
        extras = {item['name']: item['policy']
                  for item in lifecycle.get('additional_containers', [])}
        for container in self._project_containers() + [
                item for item in self._containers() if item['Name'].lstrip('/') in extras]:
            labels = container['Config'].get('Labels') or {}
            policy = extras.get(container['Name'].lstrip('/')) or policies.get((labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')))
            if policy is None:
                continue
            spec = policy['Name']
            if spec == 'on-failure' and policy['MaximumRetryCount']:
                spec += ':' + str(policy['MaximumRetryCount'])
            run('docker', 'update', '--restart=' + spec, container['Id'])

    def start(self):
        journal = self.journal()
        if journal['status'] == 'started' or journal.get('committed'):
            return {'status': 'started'}
        if journal['status'] not in {'quiesced', 'captured', 'restored', 'committed', 'rolled_back', 'ready_to_start'} and not (journal['status'] == 'quiescing' and not journal['paths']):
            raise RuntimeError('Company services cannot start with an incomplete dataset restore')
        if journal['status'] == 'ready_to_start':
            self.quiesce()
            journal = self.journal()
        lifecycle = journal.get('target_lifecycle') or journal.get('source_lifecycle')
        if lifecycle is None:
            if journal['paths'] or journal.get('restart_policies'):
                raise RuntimeError('A mutated dataset operation has no recorded source lifecycle')
            # Quiesce failed before its durable lifecycle checkpoint and before
            # stopping anything. Only its service boot marker needs recovery.
            if SWITCH_MARKER.exists():
                if json.loads(SWITCH_MARKER.read_text())['job_id'] != str(self.arguments.job_id):
                    raise RuntimeError('A different company switch owns the service boot interlock')
                SWITCH_MARKER.unlink()
            journal['status'] = 'started'
            self.save(journal)
            return {'status': 'started'}
        self._assert_stopped()
        self._retire_desktop_containers()
        self._restore_policies(lifecycle)
        atomic_json(self.root / 'active-lifecycle.json', lifecycle)
        journal['status'] = 'ready_to_start'
        self.save(journal)
        if SWITCH_MARKER.exists():
            if json.loads(SWITCH_MARKER.read_text())['job_id'] != str(self.arguments.job_id):
                raise RuntimeError('A different company switch owns the service boot interlock')
            SWITCH_MARKER.unlink()
        for config in self.config.compose_files:
            services = lifecycle['running_services'].get(str(config), [])
            if services:
                run('docker', 'compose', '-f', str(config), 'up', '-d', '--wait', '--pull', 'never', '--no-build', *services)
        extras = [item['name'] for item in lifecycle.get('additional_containers', []) if item['running']]
        if extras:
            run('docker', 'start', *extras)
        self._restore_policies(lifecycle)
        units = [unit for unit in lifecycle['active_units']
                 if unit not in {'agentic-company.target', 'agentic-company-compose.service'}]
        if units:
            run('systemctl', 'start', *units)
        journal['status'] = 'started'
        journal['services_started'] = True
        self.save(journal)
        return {'status': 'started'}


def boot(configuration):
    if SWITCH_MARKER.exists():
        raise RuntimeError('Company services cannot boot during an unfinished data switch')
    lifecycle = json.loads((configuration.root / 'active-lifecycle.json').read_text())
    for config in configuration.compose_files:
        services = lifecycle['running_services'].get(str(config), [])
        if services:
            run('docker', 'compose', '-f', str(config), 'up', '-d', '--wait', '--pull', 'never', '--no-build', *services)
    extras = [item['name'] for item in lifecycle.get('additional_containers', []) if item['running']]
    if extras:
        run('docker', 'start', *extras)
    return {'status': 'started'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/company-workplace/company-environments.json'))
    parser.add_argument('operation', choices=['quiesce', 'capture', 'duplicate', 'restore', 'rollback', 'commit', 'start', 'boot'])
    parser.add_argument('--company-id')
    parser.add_argument('--job-id')
    parser.add_argument('--source-id')
    parser.add_argument('--snapshot')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Company dataset ownership preservation requires root')
    configuration = Configuration.model_validate_json(arguments.config.read_bytes())
    if arguments.operation == 'boot':
        print(json.dumps(boot(configuration)))
        return
    identity = Arguments(company_id=arguments.company_id, job_id=arguments.job_id, source_id=arguments.source_id, snapshot=arguments.snapshot)
    helper = CompanyData(configuration, identity)
    with helper.locked():
        result = getattr(helper, arguments.operation)()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
