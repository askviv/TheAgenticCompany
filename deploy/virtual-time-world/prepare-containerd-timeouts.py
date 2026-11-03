#!/usr/bin/env python3
"""Prepare clock-scaled containerd infrastructure timeout overrides."""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib


TIMEOUT_KEYS = (
    ('timeouts', 'io.containerd.timeout.bolt.open'),
    ('timeouts', 'io.containerd.timeout.cri.defercleanup'),
    ('timeouts', 'io.containerd.timeout.metrics.shimstats'),
    ('timeouts', 'io.containerd.timeout.shim.cleanup'),
    ('timeouts', 'io.containerd.timeout.shim.load'),
    ('timeouts', 'io.containerd.timeout.shim.shutdown'),
    ('timeouts', 'io.containerd.timeout.task.state'),
    ('plugins', 'io.containerd.nri.v1.nri', 'plugin_registration_timeout'),
    ('plugins', 'io.containerd.nri.v1.nri', 'plugin_request_timeout'),
)
OVERRIDE_PATH = Path('/etc/containerd/conf.d/98-virtual-time-world-timeouts.toml')


def run(*command, capture_output=True):
    environment = {**os.environ, 'LC_ALL': 'C'}
    return subprocess.run(command, check=True, text=True, capture_output=capture_output,
                          env=environment)


def nested_value(mapping, path):
    for key in path:
        mapping = mapping[key]
    return mapping


def timespan_usec(value):
    output = run('systemd-analyze', 'timespan', value).stdout
    for line in output.splitlines():
        label, separator, number = line.partition(':')
        if separator and label.strip() == 'us' and number.strip().isdigit():
            return int(number.strip())
    raise RuntimeError('systemd-analyze did not return its microsecond value')


def toml_key(key):
    return json.dumps(key, ensure_ascii=True)


def render_overrides(timeout_values):
    lines = ['[timeouts]']
    for path in TIMEOUT_KEYS[:7]:
        lines.append(f'{toml_key(path[1])} = {json.dumps(timeout_values[path])}')
    lines.extend((
        "[plugins.'io.containerd.nri.v1.nri']",
        f'plugin_registration_timeout = {json.dumps(timeout_values[TIMEOUT_KEYS[7]])}',
        f'plugin_request_timeout = {json.dumps(timeout_values[TIMEOUT_KEYS[8]])}',
        '',
    ))
    return '\n'.join(lines)


def validate_candidate(version, imports, overlay_text, expected):
    with tempfile.TemporaryDirectory(prefix='containerd-timeout-candidate-') as directory:
        root = Path(directory)
        overlay = root / 'timeouts.toml'
        candidate = root / 'config.toml'
        overlay.write_text(overlay_text)
        candidate.write_text(
            f'version = {version}\nimports = {json.dumps([*imports, str(overlay)])}\n'
        )
        result = tomllib.loads(run('containerd', '--config', str(candidate), 'config', 'dump').stdout)
        assert_effective_timeouts(result, expected)


def write_private(path, value):
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def write_override(text):
    OVERRIDE_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=OVERRIDE_PATH.parent,
                                                   prefix='.' + OVERRIDE_PATH.name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, OVERRIDE_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def atomic_write(path, text, mode):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def imported_override(imports):
    return any(fnmatch.fnmatch(str(OVERRIDE_PATH), pattern) for pattern in imports)


def assert_effective_timeouts(config, expected):
    for path in TIMEOUT_KEYS:
        if timespan_usec(nested_value(config, path)) != timespan_usec(expected[path]):
            raise RuntimeError('The effective containerd config does not use the requested scaled timeouts')


def prepare_containerd_timeouts(rate, config_dir):
    defaults = tomllib.loads(run('containerd', 'config', 'default').stdout)
    main_config = Path('/etc/containerd/config.toml')
    main_config_was_absent = not main_config.exists()
    current = tomllib.loads(run('containerd', 'config', 'dump').stdout)
    imports = current.get('imports') or defaults.get('imports') or []
    create_main_config = main_config_was_absent
    if main_config_was_absent:
        imports = defaults.get('imports') or []
        if not imported_override(imports):
            imports = [*imports, '/etc/containerd/conf.d/*.toml']
    elif not imported_override(imports):
        raise RuntimeError('The existing containerd config imports do not include a safe timeout override path')

    baseline_dir = config_dir.parent / 'containerd-timeout-baseline'
    baseline_dir.mkdir(mode=0o700, exist_ok=True)
    baseline_path = baseline_dir / 'native-effective.json'
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
    else:
        if OVERRIDE_PATH.exists():
            raise RuntimeError('Containerd timeout baseline is missing while its override exists')
        baseline = {
            'main_config_was_absent': main_config_was_absent,
            'effective_timeouts': {
                '.'.join(path): {
                    'native_duration': nested_value(current, path),
                    'microseconds': timespan_usec(nested_value(current, path)),
                }
                for path in TIMEOUT_KEYS
            },
        }
        write_private(baseline_path, baseline)

    expected = {
        path: f'{baseline["effective_timeouts"][".".join(path)]["microseconds"] * rate}us'
        for path in TIMEOUT_KEYS
    }
    override = render_overrides(expected)
    version = current.get('version', defaults['version'])
    validate_candidate(version, imports, override, expected)
    original_override = OVERRIDE_PATH.read_bytes() if OVERRIDE_PATH.exists() else None
    write_override(override)
    try:
        if create_main_config:
            atomic_write(main_config,
                         f'version = {version}\nimports = {json.dumps(imports)}\n', 0o644)
        effective = tomllib.loads(run('containerd', 'config', 'dump').stdout)
        assert_effective_timeouts(effective, expected)
    except BaseException:
        if original_override is None:
            OVERRIDE_PATH.unlink(missing_ok=True)
        else:
            atomic_write(OVERRIDE_PATH, original_override.decode('utf-8'), 0o600)
        if create_main_config:
            main_config.unlink(missing_ok=True)
        raise
    print(f'Prepared native containerd infrastructure timeouts at {rate}x; restart containerd to apply')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--rate', type=int)
    args = parser.parse_args()
    if args.rate is None:
        args.rate = int(Path('/sys/kernel/virtual_time_rate').read_text().strip())
    if args.rate < 1:
        parser.error('--rate must be a positive whole number')
    prepare_containerd_timeouts(args.rate, args.config_dir)


if __name__ == '__main__':
    main()
