#!/usr/bin/env python3
"""Drain stateful app processes, then stop every prepared Compose project."""
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import traceback

LOG = logging.getLogger('virtual_time_world.company_compose_stop')
CONFIG_DIR = Path(os.environ.get(
    'COMPOSE_CONFIG_DIR',
    '/data/theagentcompany/virtual-time-world/config/migration-compose'))
POLICY_JOURNAL = Path(os.environ.get(
    'RESTART_POLICY_JOURNAL', str(CONFIG_DIR.parent / 'container-restart-policy-journal.json')))
DOCKER = '/usr/bin/docker'

# Mail protocols flush their stores before their Python wrappers and auth
# children receive shutdown signals.
NATIVE_DRAINS = (
    ('theagentcompany-mailu', 'imap', ('doveadm', 'stop')),
    ('theagentcompany-mailu', 'smtp', ('postfix', 'stop')),
)
CHILD_SIGNALS = (
    ('theagentcompany-mailu', 'admin', b'gunicorn', 'SIGTERM'),
    ('theagentcompany-mailu', 'front', b'nginx', 'SIGQUIT'),
    ('theagentcompany-mailu', 'front-company-clock-internal', b'nginx', 'SIGQUIT'),
    ('tac-enterprise-erp', 'frontend', b'nginx', 'SIGQUIT'),
    ('plane-app', 'worker', b'celery', 'SIGTERM'),
    ('plane-app', 'beat-worker', b'celery', 'SIGTERM'),
)
MAILU_WRAPPERS = (('theagentcompany-mailu', 'imap'),
                  ('theagentcompany-mailu', 'smtp'))

DIRECT_CHILD_SIGNAL = r'''
import os, signal, sys
needle = sys.argv[1].encode().lower()
requested_signal = getattr(signal, sys.argv[2])
sent = 0
for item in os.listdir('/proc'):
    if not item.isdigit() or int(item) <= 1:
        continue
    try:
        fields = open('/proc/' + item + '/stat', 'rb').read().rsplit(b')', 1)[1].split()
        ppid = int(fields[1])
        command = open('/proc/' + item + '/cmdline', 'rb').read().lower()
    except (OSError, ValueError, IndexError):
        continue
    if ppid == 1 and needle in command:
        try:
            os.kill(int(item), requested_signal)
        except ProcessLookupError:
            continue
        sent += 1
if sent == 0:
    sys.exit(3)
'''

MAILU_CHILDREN_DRAIN = r'''
import os, signal, sys
try:
    command = open('/proc/1/cmdline', 'rb').read().lower()
except OSError:
    sys.exit(2)
if b'python' not in command:
    sys.exit(3)
for item in os.listdir('/proc'):
    if not item.isdigit() or int(item) <= 1:
        continue
    try:
        fields = open('/proc/' + item + '/stat', 'rb').read().rsplit(b')', 1)[1].split()
        ppid = int(fields[1])
        child_command = open('/proc/' + item + '/cmdline', 'rb').read().lower()
    except (OSError, ValueError, IndexError):
        continue
    if ppid == 1 and b'python' in child_command:
        try:
            os.kill(int(item), signal.SIGTERM)
        except ProcessLookupError:
            pass
try:
    os.kill(1, signal.SIGINT)
except ProcessLookupError:
    sys.exit(4)
'''


class StopFailure(RuntimeError):
    pass


def compose(config, *args):
    return [DOCKER, 'compose', '-f', str(config), *args]


def command_error(label, result):
    detail = (result.stderr or '').strip()
    message = f'{label} failed with exit code {result.returncode}'
    return f'{message}: {detail}' if detail else message


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_policy_journal():
    if not os.path.lexists(POLICY_JOURNAL):
        return None
    if POLICY_JOURNAL.is_symlink() or not POLICY_JOURNAL.is_file():
        raise ValueError('restart-policy journal is not a regular file')
    os.chmod(POLICY_JOURNAL, 0o600)
    journal = json.loads(POLICY_JOURNAL.read_text(encoding='utf-8'))
    if not isinstance(journal, dict) or journal.get('version') != 1:
        raise ValueError('restart-policy journal has an unsupported format')
    if journal.get('status') not in {
            'captured', 'disabling', 'draining', 'stopping', 'restoring'}:
        raise ValueError('restart-policy journal has an unknown state')
    entries = journal.get('containers')
    if not isinstance(entries, list):
        raise ValueError('restart-policy journal has no container records')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('restart-policy journal has an invalid container record')
        container_id = entry.get('id')
        policy = entry.get('restart_policy')
        if (not isinstance(container_id, str) or not container_id or
                container_id in seen or not isinstance(entry.get('project'), str) or
                not isinstance(policy, dict) or not isinstance(policy.get('Name'), str) or
                policy['Name'] not in {'no', 'on-failure', 'always', 'unless-stopped'} or
                not isinstance(policy.get('MaximumRetryCount'), int) or
                policy['MaximumRetryCount'] < 0):
            raise ValueError('restart-policy journal has incomplete or duplicate state')
        seen.add(container_id)
    return journal


def docker_inspect(container_ids):
    if not container_ids:
        return {}
    result = subprocess.run([DOCKER, 'inspect', *container_ids],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if result.returncode:
        raise RuntimeError(command_error('Docker inspect for migration containers', result))
    records = json.loads(result.stdout)
    by_id = {record['Id']: record for record in records}
    if set(by_id) != set(container_ids):
        raise RuntimeError('Docker inspect returned a different container set')
    return by_id


def capture_restart_policies(projects):
    journal = read_policy_journal()
    existing = {entry['id']: entry for entry in journal['containers']} if journal else {}
    project_containers = {}
    for project, (config, _services) in projects.items():
        result = subprocess.run(compose(config, 'ps', '-aq'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        if result.returncode:
            raise RuntimeError(command_error(f'{project} container inventory', result))
        for container_id in result.stdout.split():
            if container_id in project_containers and project_containers[container_id] != project:
                raise RuntimeError(f'container {container_id} is listed in multiple projects')
            project_containers[container_id] = project

    for entry in existing.values():
        if entry['project'] not in projects:
            raise RuntimeError(
                f'restart-policy journal references unconfigured project {entry["project"]}')
    inspect_ids = sorted(set(project_containers) | set(existing))
    records = docker_inspect(inspect_ids)
    for container_id, project in project_containers.items():
        record = records[container_id]
        labels = record.get('Config', {}).get('Labels') or {}
        if labels.get('com.docker.compose.project') != project:
            raise RuntimeError(f'container {container_id} project label does not match {project}')
        previous = existing.get(container_id)
        if previous and previous['project'] != project:
            raise RuntimeError(f'container {container_id} changed projects during stop recovery')
        if previous is None:
            policy = record.get('HostConfig', {}).get('RestartPolicy')
            if (not isinstance(policy, dict) or not isinstance(policy.get('Name'), str) or
                    policy['Name'] not in {'no', 'on-failure', 'always', 'unless-stopped'} or
                    not isinstance(policy.get('MaximumRetryCount'), int) or
                    policy['MaximumRetryCount'] < 0):
                raise RuntimeError(f'container {container_id} has an unknown restart policy')
            existing[container_id] = {
                'id': container_id,
                'project': project,
                'restart_policy': {
                    'Name': policy['Name'],
                    'MaximumRetryCount': policy['MaximumRetryCount'],
                },
            }

    for container_id, entry in existing.items():
        labels = records[container_id].get('Config', {}).get('Labels') or {}
        if labels.get('com.docker.compose.project') != entry['project']:
            raise RuntimeError(
                f'journal container {container_id} project label does not match its record')

    if journal is None:
        journal = {'version': 1, 'containers': []}
    journal['containers'] = [existing[key] for key in sorted(existing)]
    journal['status'] = 'captured'
    atomic_json(POLICY_JOURNAL, journal)
    return journal


def restart_spec(policy):
    name = policy['Name']
    retries = policy['MaximumRetryCount']
    if name == 'on-failure' and retries:
        return f'on-failure:{retries}'
    return name


def update_restart_policy(container_id, policy, label):
    result = subprocess.run([DOCKER, 'update', f'--restart={policy}', container_id],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if result.returncode:
        return command_error(label, result)
    return None


def disable_restart_policies(journal):
    journal['status'] = 'disabling'
    atomic_json(POLICY_JOURNAL, journal)
    failures = []
    for entry in journal['containers']:
        error = update_restart_policy(
            entry['id'], 'no', f'disabling restart policy for {entry["id"]}')
        if error:
            LOG.error('%s', error)
            failures.append(error)
    journal['status'] = 'draining' if not failures else 'stopping'
    atomic_json(POLICY_JOURNAL, journal)
    return failures


def all_stopped(journal):
    records = docker_inspect([entry['id'] for entry in journal['containers']])
    running = [container_id for container_id, record in records.items()
               if record.get('State', {}).get('Running') is not False]
    if running:
        raise RuntimeError('company containers are not all stopped: ' + ', '.join(running))
    return records


def restore_restart_policies(journal):
    journal['status'] = 'restoring'
    atomic_json(POLICY_JOURNAL, journal)
    failures = []
    for entry in journal['containers']:
        error = update_restart_policy(
            entry['id'], restart_spec(entry['restart_policy']),
            f'restoring restart policy for {entry["id"]}')
        if error:
            LOG.error('%s', error)
            failures.append(error)
    if failures:
        raise RuntimeError('restart policies could not all be restored: ' + '; '.join(failures))

    records = docker_inspect([entry['id'] for entry in journal['containers']])
    for entry in journal['containers']:
        actual = records[entry['id']].get('HostConfig', {}).get('RestartPolicy')
        expected = entry['restart_policy']
        if not isinstance(actual, dict) or any(
                actual.get(key) != expected[key]
                for key in ('Name', 'MaximumRetryCount')):
            raise RuntimeError(f'restart-policy verification failed for {entry["id"]}')

    POLICY_JOURNAL.unlink()
    directory_fd = os.open(POLICY_JOURNAL.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run_checked(command, label):
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        LOG.error('%s', command_error(label, result))
        return False
    return True


def is_running(config, service):
    command = compose(config, 'ps', '--status', 'running', '-q', service)
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        LOG.error('%s', command_error(f'inspecting running state for {service}', result))
        return None
    return bool(result.stdout.strip())


def exec_service(config, project, service, command, label):
    state = is_running(config, service)
    if state is None:
        return False
    if not state:
        return True
    result = subprocess.run(
        compose(config, 'exec', '-T', '--user', '0', service, *command),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        if is_running(config, service) is False:
            LOG.info('Container exited during %s for %s/%s', label, project, service)
            return True
        LOG.error('%s', command_error(f'{label} for {project}/{service}', result))
        return False
    return True


def drain_mailu_wrapper(config, project, service):
    state = is_running(config, service)
    if state is None or not state:
        return state is False
    result = subprocess.run(
        compose(config, 'exec', '-T', '--user', '0', service,
                'python3', '-c', MAILU_CHILDREN_DRAIN),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        LOG.info('Requested post-flush Python wrapper interrupt for %s/%s', project, service)
        return True
    # PID 1 may exit immediately in response to SIGINT; that is the expected
    # outcome only when Compose confirms the container has stopped.
    stopped = is_running(config, service) is False
    if stopped:
        LOG.info('Python wrapper exited after the post-flush interrupt for %s/%s', project, service)
        return True
    LOG.error('%s', command_error(
        f'post-flush wrapper interrupt for {project}/{service}', result))
    return False


def load_projects():
    if not CONFIG_DIR.is_dir():
        raise FileNotFoundError('prepared migration Compose directory is unavailable')
    projects = []
    for path in sorted(CONFIG_DIR.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        name = data.get('name')
        services = data.get('services')
        if not isinstance(name, str) or not isinstance(services, dict):
            raise ValueError('prepared Compose configuration has no project name or services')
        if any(existing_name == name for existing_name, _path, _services in projects):
            raise ValueError(f'duplicate prepared Compose project name: {name}')
        projects.append((name, path, services))
    if not projects:
        raise FileNotFoundError('no prepared migration Compose configurations found')
    return {name: (path, services) for name, path, services in projects}


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    projects = load_projects()
    failures = []

    journal = None
    run_native_drains = False
    try:
        journal = capture_restart_policies(projects)
        policy_failures = disable_restart_policies(journal)
        failures.extend(policy_failures)
        run_native_drains = not policy_failures
    except Exception as error:
        LOG.exception('Could not prepare durable restart-policy state; skipping native drains')
        failures.append(f'restart-policy preparation: {error}')

    if run_native_drains:
        for project, service, command in NATIVE_DRAINS:
            entry = projects.get(project)
            if entry is None or service not in entry[1]:
                continue
            ok = exec_service(entry[0], project, service, command, 'graceful drain')
            if ok:
                LOG.info('Completed native graceful drain for %s/%s', project, service)
            else:
                failures.append(f'{project}/{service} graceful drain')

        for project, service in MAILU_WRAPPERS:
            entry = projects.get(project)
            if entry is None or service not in entry[1]:
                continue
            # Only interrupt the startup wrapper after the native mail daemon stop
            # for this same service completed successfully.
            if f'{project}/{service} graceful drain' in failures:
                continue
            if not drain_mailu_wrapper(entry[0], project, service):
                failures.append(f'{project}/{service} startup wrapper')

        for project, service, process_name, requested_signal in CHILD_SIGNALS:
            entry = projects.get(project)
            if entry is None or service not in entry[1]:
                continue
            command = compose(entry[0], 'exec', '-T', '--user', '0', service,
                              'python3', '-c', DIRECT_CHILD_SIGNAL,
                              process_name.decode(), requested_signal)
            result = subprocess.run(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            if result.returncode == 3:
                LOG.info('No direct %s child remained in %s/%s',
                         process_name.decode(), project, service)
            elif result.returncode:
                # A matching child can exit the container during its graceful
                # signal handling; accept that only after Compose confirms it.
                if is_running(entry[0], service) is False:
                    LOG.info('Container exited after %s for %s/%s', requested_signal,
                             project, service)
                else:
                    LOG.error('%s', command_error(
                        f'{requested_signal} for direct {process_name.decode()} child '
                        f'in {project}/{service}', result))
                    failures.append(f'{project}/{service} {requested_signal}')
    else:
        LOG.error('Skipped native drains because restart suppression is not established')

    # Always attempt graceful Compose stop for every project after prestop work.
    if journal is not None:
        journal['status'] = 'stopping'
        try:
            atomic_json(POLICY_JOURNAL, journal)
        except Exception as error:
            LOG.exception('Could not persist stopping state in restart-policy journal')
            failures.append(f'restart-policy journal update: {error}')
    for project, (config, _services) in projects.items():
        if not run_checked(compose(config, 'stop', '--timeout', '-1'),
                           f'{project} Compose graceful stop'):
            failures.append(f'{project} Compose graceful stop')

    if journal is not None:
        try:
            all_stopped(journal)
            restore_restart_policies(journal)
        except Exception as error:
            LOG.exception('Could not confirm stopped containers and restore their restart policies')
            failures.append(f'restart-policy recovery: {error}')

    if failures:
        raise StopFailure('; '.join(failures))
    LOG.info('All prepared Compose projects completed graceful stop')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
