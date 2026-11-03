#!/usr/bin/env python3
"""Scale Docker healthchecks and native service timeouts from saved baselines."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

NATIVE_COMPANY_SERVICES = (
    'tac-enterprise-simulation.service',
    'tac-customer-actor.service',
    'tac-supplier-actor.service',
    'tac-company-provider-portal.service',
)
SYSTEMD_INFINITY_USEC = (1 << 64) - 1
MAILU_PROJECT = 'theagentcompany-mailu'
MAILU_FRONT_SERVICES = ('front', 'front-company-clock-internal')
MAILU_PROXY_CONFIG_TARGET = '/etc/dovecot/conf.d/99-company-clock-proxy.conf'


def docker_json(*args):
    return json.loads(subprocess.check_output(['docker', *args], text=True))


def write_private(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timespan_usec(value):
    # Dovecot's human-readable units differ slightly from systemd's parser.
    normalized = re.sub(
        r'\b(msecs|usecs|secs|mins|hours|days|weeks)\b',
        lambda match: {
            'msecs': 'ms', 'usecs': 'us', 'secs': 'sec', 'mins': 'min',
            'hours': 'h', 'days': 'd', 'weeks': 'w',
        }[match.group(1)],
        value.strip(),
    )
    result = subprocess.run(
        ['systemd-analyze', 'timespan', normalized], check=True,
        capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'},
    )
    for line in result.stdout.splitlines():
        label, separator, number = line.partition(':')
        if separator and label.strip() == 'us' and number.strip().isdigit():
            return int(number.strip())
    raise RuntimeError('systemd-analyze did not return the Dovecot timeout in microseconds')


def dovecot_timespan(value):
    if value % 1_000_000 == 0:
        return f'{value // 1_000_000} secs'
    if value % 1_000 == 0:
        return f'{value // 1_000} msecs'
    return f'{value} usecs'


def write_mailu_proxy_config(path, timeout_usec):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = f'login_proxy_timeout = {dovecot_timespan(timeout_usec)}\n'
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def prepare_mailu_proxy_timeout(config_dir, config_path, config, rate):
    if config.get('name') != MAILU_PROJECT:
        return False
    if any(service not in config.get('services', {}) for service in MAILU_FRONT_SERVICES):
        raise RuntimeError('Prepared Mailu Compose configuration is missing a required front service')

    baseline_dir = config_dir.parent / 'mailu-proxy-timeout-baseline'
    baseline_dir.mkdir(mode=0o700, exist_ok=True)
    generated_dir = (config_dir.parent / 'mailu-proxy-timeouts').resolve()
    generated_dir.mkdir(mode=0o700, exist_ok=True)
    for service in MAILU_FRONT_SERVICES:
        baseline_path = baseline_dir / f'{service}.json'
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
            timeout_usec = baseline.get('login_proxy_timeout_usec')
            if type(timeout_usec) is not int or timeout_usec <= 0:
                raise RuntimeError(f'Saved Mailu proxy timeout baseline is invalid for {service}')
        else:
            ids = subprocess.check_output([
                'docker', 'ps', '-q', '--filter',
                f'label=com.docker.compose.project={MAILU_PROJECT}', '--filter',
                f'label=com.docker.compose.service={service}',
            ], text=True).split()
            if len(ids) != 1:
                raise RuntimeError(
                    f'Cannot capture native Dovecot proxy timeout for {service}; '
                    'exactly one running container is required'
                )
            native = subprocess.check_output(
                ['docker', 'exec', ids[0], 'doveconf', '-h', 'login_proxy_timeout'],
                text=True,
            ).strip()
            timeout_usec = timespan_usec(native)
            write_private(baseline_path, {
                'login_proxy_timeout': native,
                'login_proxy_timeout_usec': timeout_usec,
            })

        generated_path = generated_dir / f'{service}.conf'
        write_mailu_proxy_config(generated_path, timeout_usec * rate)
        settings = config['services'][service]
        volumes = settings.setdefault('volumes', [])
        matching = [v for v in volumes if isinstance(v, dict)
                    and v.get('target') == MAILU_PROXY_CONFIG_TARGET]
        expected_mount = {
            'type': 'bind', 'source': str(generated_path),
            'target': MAILU_PROXY_CONFIG_TARGET, 'read_only': True,
            'bind': {'create_host_path': False},
        }
        if matching:
            if len(matching) != 1 or matching[0] != expected_mount:
                raise RuntimeError(f'Unexpected existing Dovecot proxy config mount for {service}')
        else:
            volumes.append(expected_mount)

    write_private(config_path, config)
    print(f'Prepared native Mailu Dovecot proxy timeouts at {rate}x')
    return True


def issue_timer_interval_us():
    subprocess.run([
        'systemctl', 'show', '--property=LoadState', '--value', 'tac-company-issues.timer',
    ], check=True, capture_output=True, text=True)
    output = subprocess.check_output([
        'busctl', '--system', 'get-property', 'org.freedesktop.systemd1',
        '/org/freedesktop/systemd1/unit/tac_2dcompany_2dissues_2etimer',
        'org.freedesktop.systemd1.Timer', 'TimersMonotonic',
    ], text=True)
    fields = shlex.split(output)
    if len(fields) < 2 or fields[0] != 'a(stt)':
        raise RuntimeError('Could not read the native company issue timer intervals')
    count = int(fields[1])
    offset = 2
    matches = []
    for _ in range(count):
        base, interval, _next = fields[offset:offset + 3]
        offset += 3
        if base == 'OnUnitInactiveUSec':
            matches.append(int(interval))
    if offset != len(fields) or len(matches) != 1:
        raise RuntimeError('Expected one native OnUnitInactiveSec issue timer interval')
    return matches[0]


def native_service_usec(unit, property_name):
    escaped_unit = unit.replace('_', '_5f').replace('-', '_2d').replace('.', '_2e')
    output = subprocess.check_output([
        'busctl', '--system', 'get-property', 'org.freedesktop.systemd1',
        f'/org/freedesktop/systemd1/unit/{escaped_unit}',
        'org.freedesktop.systemd1.Service', property_name,
    ], text=True)
    fields = shlex.split(output)
    if len(fields) != 2 or fields[0] != 't':
        raise RuntimeError(f'Could not read native {property_name} for {unit}')
    return int(fields[1])


def systemd_usec(value):
    return 'infinity' if value == SYSTEMD_INFINITY_USEC else f'{value}us'


def prepare_native_service_timeouts(rate, config_dir):
    baseline_dir = config_dir.parent / 'service-timeout-baseline'
    baseline_dir.mkdir(mode=0o700, exist_ok=True)
    for unit in NATIVE_COMPANY_SERVICES:
        baseline_path = baseline_dir / f'{unit}.json'
        dropin_dir = Path('/etc/systemd/system') / f'{unit}.d'
        dropin_path = dropin_dir / '50-virtual-time-world-timeouts.conf'
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
        else:
            if dropin_path.exists():
                raise RuntimeError(f'Native timeout baseline is missing while its scaled drop-in exists: {unit}')
            baseline = {
                'timeout_stop_usec': native_service_usec(unit, 'TimeoutStopUSec'),
                'restart_usec': native_service_usec(unit, 'RestartUSec'),
            }
            write_private(baseline_path, baseline)
        values = (baseline.get('timeout_stop_usec'), baseline.get('restart_usec'))
        if any(not isinstance(value, int) or value < 0 or value > SYSTEMD_INFINITY_USEC for value in values):
            raise RuntimeError(f'Saved native timeout baseline is invalid: {unit}')
        dropin_dir.mkdir(parents=True, exist_ok=True)
        content = ('[Service]\n'
                   f'TimeoutStopSec={systemd_usec(values[0] * rate) if values[0] != SYSTEMD_INFINITY_USEC else "infinity"}\n'
                   f'RestartSec={systemd_usec(values[1] * rate) if values[1] != SYSTEMD_INFINITY_USEC else "infinity"}\n')
        descriptor, temporary_name = tempfile.mkstemp(dir=dropin_dir, prefix='.' + dropin_path.name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, dropin_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def prepare_issue_timer_cadence(rate, config_dir):
    baseline_dir = config_dir.parent / 'issue-timer-cadence-baseline'
    baseline_dir.mkdir(mode=0o700, exist_ok=True)
    baseline_path = baseline_dir / 'tac-company-issues.json'
    dropin_dir = Path('/etc/systemd/system/tac-company-issues.timer.d')
    dropin_path = dropin_dir / '50-virtual-time-world-cadence.conf'
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
        interval_us = baseline['on_unit_inactive_usec']
    else:
        if dropin_path.exists():
            raise RuntimeError('Native issue timer baseline is missing while its scaled drop-in exists')
        interval_us = issue_timer_interval_us()
        write_private(baseline_path, {'on_unit_inactive_usec': interval_us})
    if not isinstance(interval_us, int) or interval_us < 0:
        raise RuntimeError('Saved native issue timer interval is invalid')

    dropin_dir.mkdir(parents=True, exist_ok=True)
    content = ('[Timer]\n'
               'OnUnitInactiveSec=\n'
               f'OnUnitInactiveSec={interval_us * rate}us\n')
    descriptor, temporary_name = tempfile.mkstemp(dir=dropin_dir, prefix='.' + dropin_path.name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, dropin_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)

    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    active_state = subprocess.check_output([
        'systemctl', 'show', '--property=ActiveState', '--value', 'tac-company-issues.timer',
    ], text=True).strip()
    if active_state == 'active':
        subprocess.run(['systemctl', 'restart', 'tac-company-issues.timer'], check=True)
    elif active_state != 'inactive':
        raise RuntimeError(f'Company issue timer has unexpected state: {active_state}')
    print(f'Prepared native company issue recovery cadence at {rate}x')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--rate', type=int)
    args = parser.parse_args()
    if args.rate is None:
        args.rate = int(Path('/sys/kernel/virtual_time_rate').read_text().strip())
    if args.rate < 1:
        parser.error('--rate must be a positive whole number')
    baseline_dir = args.config_dir.parent / 'healthcheck-cadence-baseline'
    baseline_dir.mkdir(mode=0o700, exist_ok=True)
    paths = sorted(args.config_dir.glob('*.json'))
    if not paths:
        raise FileNotFoundError('No prepared Compose configurations found')
    for path in paths:
        config = json.loads(path.read_text())
        baseline_path = baseline_dir / path.name
        baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
        for service, settings in config['services'].items():
            if service not in baseline:
                ids = subprocess.check_output([
                    'docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={config["name"]}',
                    '--filter', f'label=com.docker.compose.service={service}',
                ], text=True).split()
                if ids:
                    health = docker_json('inspect', ids[0])[0]['Config'].get('Healthcheck')
                else:
                    health = docker_json('image', 'inspect', settings['image'])[0]['Config'].get('Healthcheck')
                    if settings.get('healthcheck', {}).get('test'):
                        raise RuntimeError(f'Create {config["name"]}/{service} before capturing its native health configuration')
                baseline[service] = health
                write_private(baseline_path, baseline)
            health = baseline[service]
            if not health or not health.get('Test') or health['Test'][0] == 'NONE':
                continue
            # Docker defaults: interval/timeout 30s, startup interval 5s.
            interval = health.get('Interval') or 30_000_000_000
            timeout = health.get('Timeout') or 30_000_000_000
            start_period = health.get('StartPeriod') or 0
            start_interval = health.get('StartInterval') or 5_000_000_000
            settings.setdefault('healthcheck', {}).update({
                'interval': f'{interval * args.rate}ns',
                'timeout': f'{timeout * args.rate}ns',
                'start_period': f'{max(start_period * args.rate, interval * args.rate) if args.rate > 1 else start_period}ns',
                'start_interval': f'{start_interval * args.rate}ns',
            })
        prepare_mailu_proxy_timeout(args.config_dir, path, config, args.rate)
        write_private(path, config)
        subprocess.run(['docker', 'compose', '-f', str(path), '--profile', '*', 'config', '--quiet'],
                       check=True, capture_output=True, text=True)
        print(f'Prepared baseline Docker health configuration: {path.name}')
    prepare_native_service_timeouts(args.rate, args.config_dir)
    prepare_issue_timer_cadence(args.rate, args.config_dir)


if __name__ == '__main__':
    main()
