"""Private τ retail worker endpoints exercise pinned upstream code without network calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault("TAU2_DATA_DIR", str(REPO / "servers/simulation/tau2_actor_runtime/data"))

from tau2.data_model.message import AssistantMessage
from tau2.domains.retail.environment import get_environment

from servers.simulation.actor_protocol import Tau2RetailSeedRequest
from servers.simulation.codex_model import CodexCLIModel
from servers.simulation.tau2_actor_worker import Tau2CustomerSettings, create_app
from servers.simulation.tau2_retail import get_task


TOKEN = "r" * 48


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path: Path) -> Tau2CustomerSettings:
    return Tau2CustomerSettings(
        model="gpt-6-luna",
        codex_executable="/mock/codex",
        codex_home=tmp_path,
        codex_timeout_seconds=900,
        bearer_token=TOKEN,
    )


def test_retail_catalog_only_exposes_metadata_and_manifest_requires_auth(tmp_path: Path) -> None:
    app = create_app(Tau2CustomerSettings(bearer_token=TOKEN), tmp_path / "catalog.sqlite")
    with TestClient(app) as client:
        catalog = client.get("/v1/retail/catalog", headers=_authorization())
        assert catalog.status_code == 200
        assert catalog.json()["task_count"] == 114
        assert catalog.json()["tasks"][0]["task_id"] == "0"
        assert set(catalog.json()["tasks"][0]) == {"task_id", "title", "summary"}
        assert "evaluation_criteria" not in catalog.text
        assert "original_db" not in catalog.text

        private = client.get("/v1/retail/tasks/0/manifest")
        assert private.status_code == 401
        manifest = client.get("/v1/retail/tasks/0/manifest", headers=_authorization())
        assert manifest.status_code == 200
        assert manifest.json()["task"]["evaluation_criteria"]
        assert manifest.json()["original_db"]["orders"]["#W2378156"]["order_id"] == "#W2378156"


def test_seed_email_uses_pinned_task_scenario_and_durable_turn_path(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_complete(_self, messages, tools=None):
        calls.append((messages, tools))
        assert tools is None
        assert "Domain: retail" in messages[0]["content"]
        assert messages[-1]["content"].endswith(
            "Write the first email you would send to customer support about the issue "
            "in your scenario. Begin as the customer and include the relevant details "
            "and request from your instructions."
        )
        return {"model": "gpt-6-luna", "content": "I need help with my order.", "tool_calls": [], "usage": None}

    monkeypatch.setattr(CodexCLIModel, "complete", fake_complete)
    app = create_app(_settings(tmp_path), tmp_path / "seed.sqlite")
    request = Tau2RetailSeedRequest(
        turn_id="run-retail:0:seed",
        run_id="run-retail",
        generation=0,
        actor_id="retail-customer",
        task_id="0",
        message_id="<seed@example.test>",
    )
    with TestClient(app) as client:
        first = client.post("/v1/retail/seed-email", json=request.model_dump(mode="json"), headers=_authorization())
        assert first.status_code == 200, first.text
        assert first.json()["body"] == "I need help with my order."
        replay = client.post("/v1/retail/seed-email", json=request.model_dump(mode="json"), headers=_authorization())
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert len(calls) == 1


def test_evaluate_endpoint_runs_native_nl_evaluator_with_codex_alias_without_provider_call(
    tmp_path: Path, monkeypatch,
) -> None:
    task = get_task("16")
    expected = get_environment()
    for action in task.evaluation_criteria.actions:
        expected.make_tool_call(
            tool_name=action.name,
            requestor=action.requestor,
            **action.arguments,
        )
    trajectory = [
        AssistantMessage(role="assistant", content="I completed the requested update.")
    ]
    nl_calls = []

    def fake_generate(*, model, messages, call_name, **kwargs):
        nl_calls.append((model, messages, call_name, kwargs))
        assertions = task.evaluation_criteria.nl_assertions
        return SimpleNamespace(content=json.dumps({
            "results": [
                {
                    "expectedOutcome": assertion,
                    "reasoning": "stubbed provider boundary for a no-network regression test",
                    "metExpectation": True,
                }
                for assertion in assertions
            ]
        }))

    from tau2.evaluator import evaluator_nl_assertions

    monkeypatch.setattr(evaluator_nl_assertions, "generate", fake_generate)
    app = create_app(_settings(tmp_path), tmp_path / "evaluate.sqlite", codex_model=object())
    request = {
        "run_id": "run-retail",
        "generation": 0,
        "actor_id": "retail-customer",
        "task_id": task.id,
        "observed_db": expected.tools.db.model_dump(mode="json"),
        "trajectory": [message.model_dump(mode="json") for message in trajectory],
    }
    with TestClient(app) as client:
        response = client.post("/v1/retail/evaluate", json=request, headers=_authorization())

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["components"]["DB"] == {"status": "evaluated", "reward": 1.0}
    assert result["components"]["NL_ASSERTION"] == {"status": "evaluated", "reward": 1.0}
    assert result["reward"] == 1.0
    assert result["reward_status"] == "complete"
    assert result["source_integrity"]["status"] == "valid"
    assert len(nl_calls) == 1
    assert nl_calls[0][0] == "tac_codex/codex-cli"
    assert nl_calls[0][2] == "nl_assertions_eval"
    assert nl_calls[0][3] == {}


def test_source_gap_does_not_block_task_evaluation_but_cannot_score_failed_db_reference(
    tmp_path: Path, monkeypatch,
) -> None:
    task = get_task("2")
    calls = []

    def fake_generate(*, model, messages, call_name, **kwargs):
        calls.append(model)
        return SimpleNamespace(content=json.dumps({
            "results": [
                {"expectedOutcome": assertion, "reasoning": "test stub", "metExpectation": True}
                for assertion in task.evaluation_criteria.nl_assertions
            ]
        }))

    from tau2.evaluator import evaluator_nl_assertions

    monkeypatch.setattr(evaluator_nl_assertions, "generate", fake_generate)
    app = create_app(_settings(tmp_path), tmp_path / "source-gap.sqlite", codex_model=object())
    request = {
        "run_id": "run-retail-gap",
        "generation": 0,
        "actor_id": "retail-customer",
        "task_id": task.id,
        "observed_db": get_environment().tools.db.model_dump(mode="json"),
        "trajectory": [
            AssistantMessage(role="assistant", content="I need a t-shirt returned.").model_dump(mode="json")
        ],
    }
    with TestClient(app) as client:
        response = client.post("/v1/retail/evaluate", json=request, headers=_authorization())

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_integrity"]["status"] == "failed"
    assert result["components"]["DB"] == {"status": "unevaluated", "reward": None}
    assert result["components"]["NL_ASSERTION"] == {"status": "evaluated", "reward": 1.0}
    assert result["reward"] is None
    assert result["reference_replay"]["status"] == "failed"
    assert calls == ["tac_codex/codex-cli"]


def test_nl_task_fails_clearly_when_codex_provider_is_not_configured(tmp_path: Path) -> None:
    app = create_app(Tau2CustomerSettings(bearer_token=TOKEN), tmp_path / "no-provider.sqlite")
    with TestClient(app) as client:
        response = client.post(
            "/v1/retail/evaluate",
            json={
                "run_id": "run-no-provider",
                "generation": 0,
                "actor_id": "retail-customer",
                "task_id": "16",
                "observed_db": get_environment().tools.db.model_dump(mode="json"),
                "trajectory": [],
            },
            headers=_authorization(),
        )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "τ retail NL assertion evaluator requires the configured Codex provider"
    )
