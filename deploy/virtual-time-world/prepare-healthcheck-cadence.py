#!/usr/bin/env python3
"""Scale native Docker health probes from their saved baseline by clock rate."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


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
        write_private(path, config)
        subprocess.run(['docker', 'compose', '-f', str(path), '--profile', '*', 'config', '--quiet'],
                       check=True, capture_output=True, text=True)
        print(f'Prepared native health cadence: {path.name}')
    prepare_issue_timer_cadence(args.rate, args.config_dir)


if __name__ == '__main__':
    main()
