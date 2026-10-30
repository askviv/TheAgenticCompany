import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from servers.simulation.codex_model import (
    CodexCLIModel, CodexCLISettings, CodexModelError, CodexConfigurationError,
)


def model(tmp_path):
    return CodexCLIModel(CodexCLISettings(model="account-model", codex_home=tmp_path / "account"))


def completed_cli(monkeypatch, *, reply, events=None, returncode=0):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        destination = Path(command[command.index("--output-last-message") + 1])
        destination.write_text(json.dumps(reply))
        return SimpleNamespace(returncode=returncode, stderr="private-provider-diagnostic",
            stdout="\n".join(json.dumps(event) for event in (events or [{
                "type": "turn.completed", "usage": {"input_tokens": 37, "output_tokens": 12},
            }])))

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_cli_returns_runtime_tool_calls_as_data_with_account_auth_and_no_native_tools(tmp_path, monkeypatch):
    calls = completed_cli(monkeypatch, reply={"content": None, "tool_calls": [{
        "id": "call-1", "name": "read_order", "arguments_json": '{"order_id":"PO-1"}',
    }]})
    result = model(tmp_path).complete([{"role": "user", "content": "private-customer-question"}],
        tools=[{"type": "function", "function": {"name": "read_order", "parameters": {"type": "object"}}}])
    assert result["tool_calls"] == [{"id": "call-1", "name": "read_order", "arguments_json": '{"order_id":"PO-1"}'}]
    assert result["usage"] == {"input_tokens": 37, "output_tokens": 12}
    command, options = calls[0]
    assert "private-customer-question" not in " ".join(command)
    assert options["env"]["CODEX_HOME"] == str(tmp_path / "account")
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    for feature in ("shell_tool", "apps", "plugins", "multi_agent", "computer_use"):
        assert any(command[index:index + 2] == ["--disable", feature] for index in range(len(command)))
    assert not Path(options["cwd"]).exists()


@pytest.mark.parametrize("reply,events", [
    ({"content": None, "tool_calls": [{"id": "x", "name": "pay_anything", "arguments_json": "{}"}]}, None),
    ({"content": "reply", "tool_calls": []}, [{"type": "turn.failed"}]),
    ({"content": "reply", "tool_calls": []}, [{"type": "item.completed", "item": {"type": "command_execution"}}, {"type": "turn.completed"}]),
    ({"content": None, "tool_calls": []}, None),
])
def test_invalid_or_unsuccessful_cli_output_is_not_a_successful_actor_turn(tmp_path, monkeypatch, reply, events):
    completed_cli(monkeypatch, reply=reply, events=events)
    with pytest.raises(CodexModelError) as failure:
        model(tmp_path).complete([])
    assert failure.value.__cause__ is not None


def test_timeout_preserves_cause_without_repeating_the_model_call(tmp_path, monkeypatch):
    calls = []
    timeout = subprocess.TimeoutExpired(["codex", "exec"], 180,
        output="private model response", stderr="private provider diagnostic")

    def run(*args, **kwargs):
        calls.append(args)
        raise timeout

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(CodexModelError) as failure:
        model(tmp_path).complete([])
    cause = failure.value.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert cause.timeout == 180
    assert cause.stdout is None and cause.stderr is None
    assert len(calls) == 1


def test_cli_exit_error_redacts_provider_output_but_retains_exit_status(tmp_path, monkeypatch):
    completed_cli(monkeypatch, reply={}, returncode=17)
    with pytest.raises(CodexModelError) as failure:
        model(tmp_path).complete([])
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
    assert failure.value.__cause__.returncode == 17
    assert "private-provider-diagnostic" not in str(failure.value)


def test_native_configuration_rejection_keeps_safe_failure_category(tmp_path, monkeypatch):
    def rejected(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="",
            stderr="Error loading config.toml: unknown configuration field with private diagnostics")

    monkeypatch.setattr(subprocess, "run", rejected)
    with pytest.raises(CodexConfigurationError) as failure:
        model(tmp_path).complete([])
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
    assert "private diagnostics" not in str(failure.value)
