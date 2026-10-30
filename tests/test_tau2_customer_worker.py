"""Integration tests using τ-bench's real Task/UserSimulator models and methods."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault(
    "TAU2_DATA_DIR",
    str(Path(__file__).resolve().parents[1] / "servers/simulation/tau2_actor_runtime/data"),
)

from tau2.data_model.tasks import Task
from tau2.user.user_simulator import UserSimulator
from tau2.user.user_simulator_base import UserState

from servers.simulation.actor_protocol import ActorTurnRequest
from servers.simulation.customers import build_customer_task
from servers.simulation.tau2_actor_worker import Tau2CustomerSettings, create_app
from servers.simulation.codex_model import CodexCLIModel


TOKEN = "x" * 48


def _request(*, turn_id="run-1:0:event-1", state=None):
    task = build_customer_task(
        run_id="run-1",
        generation=0,
        scenario_id="E1",
        private_state={
            "request_type": "damage",
            "order_id": "SALES-ORDER-1",
            "damage_description": "one cracked housing",
            "serial_number": None,
        },
        initial_message={"body": "Our delivery arrived damaged. I have no serial numbers."},
    )
    return ActorTurnRequest(
        turn_id=turn_id,
        run_id="run-1",
        generation=0,
        actor_id="customer",
        actor_type="customer",
        scenario=task,
        state=state,
        inbound_message={
            "message_id": "<agent-1@example.test>",
            "subject": "Re: damaged order",
            "body": "Could you provide the serial number?",
        },
        visible_context={"case_id": "E1-case"},
    )


def _settings(tmp_path: Path) -> Tau2CustomerSettings:
    return Tau2CustomerSettings(
        model="gpt-6-luna", codex_executable="/mock/codex",
        codex_home=tmp_path, codex_timeout_seconds=900, bearer_token=TOKEN,
    )


def _client(tmp_path: Path, monkeypatch, complete):
    monkeypatch.setattr(CodexCLIModel, "complete", complete)
    app = create_app(_settings(tmp_path), tmp_path / "turns.sqlite")
    return TestClient(app)


def _authorization():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_real_tau_task_user_simulator_and_litellm_bridge_produce_persisted_turn(tmp_path, monkeypatch):
    calls = []

    def complete(_self, messages, tools=None):
        calls.append((messages, tools))
        assert tools is None
        assert "Customer order reference: SALES-ORDER-1" in messages[0]["content"]
        assert messages[-1]["content"] == "Subject: Re: damaged order\n\nCould you provide the serial number?"
        return {
            "model": "gpt-6-luna",
            "content": "I did not include the serial number with my complaint.",
            "tool_calls": [],
            "usage": {"input_tokens": 45, "output_tokens": 9},
        }

    with _client(tmp_path, monkeypatch, complete) as client:
        response = client.post("/v1/turn", json=_request().model_dump(mode="json"), headers=_authorization())
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["body"] == "I did not include the serial number with my complaint."
        assert result["usage"] == {
            "provider": "codex-cli", "model_id": "gpt-6-luna", "model_version": None,
            "input_tokens": 45, "output_tokens": 9, "cost": None, "currency": None,
        }
        assert len(calls) == 1
        assert "secret_provider_payload" not in response.text
        assert "raw_data" not in response.text
        assert UserState.model_validate(result["state"])
        assert Task.model_validate(_request().scenario)
        assert isinstance(UserSimulator(llm="tac_codex/gpt-6-luna", instructions="test", tools=[]), UserSimulator)

        replay = client.post("/v1/turn", json=_request().model_dump(mode="json"), headers=_authorization())
        assert replay.status_code == 200
        assert replay.json() == result
        assert len(calls) == 1


def test_identical_inflight_retry_is_uncertain_and_never_reinvokes_tau(tmp_path, monkeypatch):
    calls = 0

    def fail_after_start(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("provider payload must not escape")

    with _client(tmp_path, monkeypatch, fail_after_start) as client:
        request = _request().model_dump(mode="json")
        first = client.post("/v1/turn", json=request, headers=_authorization())
        assert first.status_code == 503
        assert first.json()["detail"] == {
            "code": "customer_generation_uncertain", "error_type": "APIConnectionError"
        }
        assert "provider payload" not in first.text
        second = client.post("/v1/turn", json=request, headers=_authorization())
        assert second.status_code == 202
        assert second.json()["status"] == "started_uncertain"
        assert second.json()["error_type"] == "generation_started"
        assert calls == 1


def test_reused_turn_id_with_changed_request_is_conflict(tmp_path, monkeypatch):
    calls = 0

    def generate(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"model": "gpt-6-luna", "content": "We need the serial details.",
                "tool_calls": [], "usage": None}

    with _client(tmp_path, monkeypatch, generate) as client:
        first = _request()
        result = client.post("/v1/turn", json=first.model_dump(mode="json"), headers=_authorization())
        assert result.status_code == 200
        changed = first.model_copy(deep=True)
        changed.inbound_message.body = "Different question"
        conflict = client.post("/v1/turn", json=changed.model_dump(mode="json"), headers=_authorization())
        assert conflict.status_code == 409
        assert calls == 1


def test_missing_provider_or_worker_auth_fails_clearly_without_claiming_turn(tmp_path):
    task_request = _request().model_dump(mode="json")
    app = create_app(Tau2CustomerSettings(bearer_token=TOKEN), tmp_path / "turns.sqlite")
    with TestClient(app) as client:
        missing_provider = client.post("/v1/turn", json=task_request, headers=_authorization())
        assert missing_provider.status_code == 503
        assert missing_provider.json()["detail"] == "Codex CLI model environment is not configured"

    app = create_app(_settings(tmp_path), tmp_path / "auth.sqlite")
    with TestClient(app) as client:
        unauthorized = client.post("/v1/turn", json=task_request)
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"] == "actor worker authentication failed"


def test_private_evaluation_uses_tau_action_and_substring_evaluators_without_judge_calls(tmp_path, monkeypatch):
    task = build_customer_task(
        run_id="run-eval", generation=0, scenario_id="E1",
        private_state={"request_type": "damage", "order_id": "SO-E1",
                       "damage_description": "two damaged components", "serial_number": None},
    )
    good_trajectory = [
        {"role": "user", "content": "The order arrived damaged."},
        {"role": "assistant", "tool_calls": [{"id": "read-1", "name": "get_sales_order",
            "arguments": {"entity_id": "SO-E1"}}]},
        {"role": "assistant", "tool_calls": [{"id": "mail-1", "name": "send_message",
            "arguments": {"to_actor_id": "customer"}}]},
        {"role": "assistant", "content": "Please provide the serial number for each damaged item."},
    ]
    request = {"run_id": "run-eval", "generation": 0, "actor_id": "customer",
              "task": task, "trajectory": good_trajectory}
    app = create_app(_settings(tmp_path), tmp_path / "evaluation.sqlite", codex_model=object())
    with TestClient(app) as client:
        good = client.post("/v1/evaluate", json=request, headers=_authorization())
        assert good.status_code == 200, good.json()
        assert good.json()["evaluator"] == "tau2-action+substring-communicate"
        assert good.json()["action_reward"] == 1.0
        assert good.json()["communicate_reward"] == 1.0
        assert good.json()["reward"] == 1.0

        missed = client.post("/v1/evaluate", json={**request, "trajectory": [
            {"role": "user", "content": "The order arrived damaged."},
            {"role": "assistant", "content": "We are reviewing the issue."},
        ]}, headers=_authorization())
        assert missed.status_code == 200, missed.json()
        assert missed.json()["action_reward"] == 0.0
        assert missed.json()["communicate_reward"] == 0.0
        assert missed.json()["reward"] == 0.0

        from tau2.evaluator.evaluator_action import ActionEvaluator

        def evaluator_failure(*_args, **_kwargs):
            raise RuntimeError("private evaluator payload")

        monkeypatch.setattr(ActionEvaluator, "calculate_reward", evaluator_failure)
        failed = client.post("/v1/evaluate", json=request, headers=_authorization())
        assert failed.status_code == 500
        assert failed.json()["detail"] == "τ-bench evaluator failed"
        assert "private evaluator payload" not in failed.text
