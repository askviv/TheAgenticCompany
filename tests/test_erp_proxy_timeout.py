import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

PATH = Path(__file__).resolve().parents[1] / 'deploy/virtual-time-world/prepare-erp-proxy-timeout.py'
spec = importlib.util.spec_from_file_location('erp_proxy_timeout', PATH)
erp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erp)


def prepared_config(tmp_path):
    directory = tmp_path / 'compose'
    directory.mkdir()
    path = directory / 'tac-enterprise-erp.json'
    path.write_text(json.dumps({'name': 'tac-enterprise-erp', 'services': {
        'frontend': {'environment': {'PROXY_READ_TIMEOUT': 120}},
    }}))
    return directory, path


def test_scales_from_original_baseline_on_each_rate_change(tmp_path):
    directory, path = prepared_config(tmp_path)
    with patch.object(erp.subprocess, 'check_output', return_value=''), patch.object(erp.subprocess, 'run'):
        assert erp.sync(directory, 12)
        assert json.loads(path.read_text())['services']['frontend']['environment']['PROXY_READ_TIMEOUT'] == 1440
        assert erp.sync(directory, 3)
        assert json.loads(path.read_text())['services']['frontend']['environment']['PROXY_READ_TIMEOUT'] == 360


def test_updates_running_nginx_and_reloads(tmp_path):
    directory, path = prepared_config(tmp_path)
    original = 'location / {\n    proxy_read_timeout 120;\n}\n'
    with patch.object(erp.subprocess, 'check_output', side_effect=['frontend-id\n', original]), \
         patch.object(erp.subprocess, 'run') as run:
        assert erp.sync(directory, 12)
    writes = [call for call in run.call_args_list if call.kwargs.get('input')]
    assert len(writes) == 1
    assert 'proxy_read_timeout 1440;' in writes[0].kwargs['input']
    assert any(call.args[0][-2:] == ['nginx', '-t'] for call in run.call_args_list)
    assert any(call.args[0][-3:] == ['nginx', '-s', 'reload'] for call in run.call_args_list)
    assert json.loads(path.read_text())['services']['frontend']['environment']['PROXY_READ_TIMEOUT'] == 1440


def test_rejects_invalid_rate(tmp_path):
    directory, _ = prepared_config(tmp_path)
    with pytest.raises(ValueError, match='positive integer'):
        erp.sync(directory, 0)


def test_restores_running_config_if_nginx_validation_fails(tmp_path):
    directory, path = prepared_config(tmp_path)
    original = 'location / {\n    proxy_read_timeout 120;\n}\n'
    writes = []

    def run(command, **kwargs):
        if kwargs.get('input'):
            writes.append(kwargs['input'])
        if command[-2:] == ['nginx', '-t']:
            raise erp.subprocess.CalledProcessError(1, command)

    with patch.object(erp.subprocess, 'check_output', side_effect=['frontend-id\n', original]), \
         patch.object(erp.subprocess, 'run', side_effect=run), \
         pytest.raises(erp.subprocess.CalledProcessError):
        erp.sync(directory, 12)
    assert 'proxy_read_timeout 1440;' in writes[0]
    assert writes[1] == original
    assert json.loads(path.read_text())['services']['frontend']['environment']['PROXY_READ_TIMEOUT'] == 120


def test_clock_rate_change_triggers_proxy_synchronization(tmp_path, monkeypatch):
    import contextlib
    import sys
    import types
    from unittest.mock import Mock

    fake_libvirt = types.ModuleType('libvirt')
    domain = Mock()
    domain.isActive.return_value = True
    connection = Mock()
    connection.lookupByName.return_value = domain
    fake_libvirt.open = Mock(return_value=connection)
    monkeypatch.setitem(sys.modules, 'libvirt', fake_libvirt)
    path = PATH.with_name('clock_control.py')
    clock_spec = importlib.util.spec_from_file_location('clock_controller_test', path)
    clock = importlib.util.module_from_spec(clock_spec)
    clock_spec.loader.exec_module(clock)
    controller = clock.Controller(tmp_path, 'test-vm', 22220)
    db = Mock()
    db.execute.return_value.fetchone.return_value = None
    controller.state = lambda: contextlib.nullcontext(db)
    controller.row = Mock(return_value=(1, 1, 1))
    controller.running_rate = Mock(return_value=1)
    controller.guest_ns = Mock(return_value=2)
    controller.save_anchor = Mock()
    controller.ssh = Mock(side_effect=lambda *args: '3' if args[0] == 'cat' else '')
    controller.synchronize_erp_proxy_timeout = Mock()

    controller.set_rate(3)

    controller.synchronize_erp_proxy_timeout.assert_called_once_with(3)
    connection.close.assert_called_once()
