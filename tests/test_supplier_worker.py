from __future__ import annotations

from copy import deepcopy
import os
import sqlite3
import importlib

from fastapi.testclient import TestClient
import pytest

from coffeebench.models.types import ModelResponse, ToolCall
from servers.simulation.actor_protocol import ActorTurnRequest
from servers.simulation.actor_protocol import bearer_token_matches
from servers.simulation.actor_worker_store import canonical_request_hash
from servers.simulation.businesses import initialize_supplier_business
from servers.simulation.supplier_worker import (
    CoffeeSupplierWorker, WorkerTurnConflict, WorkerTurnFailed, create_supplier_worker_app,
)


class ScriptedModel:
    model = "gpt-codex-test"

    def __init__(self):
        self.calls = 0
        self.seen_messages = []
        self.responses = [
            ModelResponse(tool_calls=[ToolCall(id="lookup-1", name="get_dispatch_record", input={})]),
            ModelResponse(tool_calls=[ToolCall(id="reply-1", name="stage_reply", input={
                "body": "Our dispatch record shows 10000 components on shipment shipment-supplier-1.",
                "disclosed_facts": ["dispatch_quantity", "shipment_id"],
            })]),
        ]

    def query(self, messages, tools=None):
        self.calls += 1
        self.seen_messages.append(deepcopy(messages))
        return self.responses.pop(0)

    def get_usage_stats(self):
        return {"n_model_calls": self.calls, "model_cost": None}


def _request(**overrides):
    private = initialize_supplier_business({
        "dispatch_quantity": 10_000, "readable_unit": "components", "shipment_id": "shipment-supplier-1",
        "dispatch_evidence_ids": ["manifest-private-1"],
        "pricing_config": {"family": "Candid", "stance": "neutral", "kappa_b": 0.5,
                           "opening_harshness": 0.2, "reservation_price": "45.00",
                           "maximum_offer": "100.00", "maximum_rounds": 5, "minor_unit": 2},
    }, seed=17, supplier_id="supplier-1")
    values = {
        "turn_id": "turn-1", "run_id": "run-1", "generation": 2, "actor_id": "supplier-1",
        "actor_type": "supplier", "scenario": {"supplier_private_state": private}, "state": None,
        "inbound_message": {"message_id": "message-1", "subject": "Dispatch evidence",
                            "body": "Please provide evidence of what you dispatched."},
        "visible_context": {"case_id": "C1-case", "purchase_order_id": "PO-1042",
                            "order_quantity": 10_000, "readable_unit_price": "50.00",
                            "reported_received_quantity": 8_000},
    }
    values.update(overrides)
    return ActorTurnRequest.model_validate(values)


def test_supplier_worker_uses_coffee_agent_persists_private_transcript_and_replays_exact_result(tmp_path):
    models = []

    def factory(request):
        model = ScriptedModel()
        models.append(model)
        return model

    database = tmp_path / "actor-worker.sqlite3"
    worker = CoffeeSupplierWorker(database, model_factory=factory)
    request = _request()
    result = worker.process(request)

    assert result.status == "completed"
    assert "10000 components" in result.body
    assert "manifest-private-1" not in result.body
    assert result.disclosed_facts == ["dispatch_quantity", "shipment_id"]
    assert result.conversation_pointer
    assert result.supplier_private_state_patch.kernel_state.turns == []
    assert "9e2f3950b5812c8753de81ede9c68babc024b5c8" in result.conversation_pointer
    assert result.usage.model_version is None
    assert result.usage.cost is None
    assert models[0].calls == 2

    repeated = worker.process(request)
    assert repeated.model_dump(mode="json") == result.model_dump(mode="json")
    assert models[0].calls == 2

    reopened = CoffeeSupplierWorker(
        database,
        model_factory=lambda request: (_ for _ in ()).throw(AssertionError("cached turn must not call model")),
    )
    assert reopened.process(request).model_dump(mode="json") == result.model_dump(mode="json")

    followup_payload = request.model_dump(mode="json")
    followup_payload["turn_id"] = "turn-2"
    followup_payload["inbound_message"] = {
        "message_id": "message-2", "subject": "Follow-up",
        "body": "Please confirm the record reference.",
    }
    followup_request = ActorTurnRequest.model_validate(followup_payload)
    followup_model = ScriptedModel()
    followup_model.responses = [
        ModelResponse(tool_calls=[ToolCall(id="lookup-2", name="get_dispatch_record", input={})]),
        ModelResponse(tool_calls=[ToolCall(id="reply-2", name="stage_reply", input={
            "body": "Our cited dispatch record is manifest-private-1.",
            "disclosed_facts": ["dispatch_evidence_ids"],
        })]),
    ]
    followup = reopened.__class__(database, model_factory=lambda request: followup_model)
    followup_result = followup.process(followup_request)
    assert followup_result.body == "Our cited dispatch record is manifest-private-1."
    assert any("manifest-private-1" in str(message) for message in followup_model.seen_messages[0])

    with sqlite3.connect(database) as connection:
        transcript = connection.execute("SELECT messages_json FROM coffee_supplier_transcripts").fetchone()[0]
    assert "manifest-private-1" in transcript
    assert os.stat(database).st_mode & 0o777 == 0o600
    assert "manifest-private-1" not in result.model_dump_json()
    assert "dispatch_evidence_ids" not in result.model_dump_json()


def test_supplier_worker_request_hash_includes_private_state_and_rejects_reused_turn_id(tmp_path):
    worker = CoffeeSupplierWorker(tmp_path / "actors.db", model_factory=lambda request: ScriptedModel())
    request = _request()
    worker.process(request)
    changed_private = request.model_dump(mode="json")
    changed_private["scenario"]["supplier_private_state"]["dispatch_quantity"] = 9_000
    changed = ActorTurnRequest.model_validate(changed_private)

    with pytest.raises(WorkerTurnConflict):
        worker.process(changed)


def test_supplier_worker_returns_started_uncertain_without_repeating_model_call(tmp_path):
    database = tmp_path / "actors.db"
    request = _request()
    from servers.simulation.actor_worker_store import ActorTurnStore

    store = ActorTurnStore(database)
    state, cached = store.claim(request.turn_id, canonical_request_hash(request.model_dump(mode="json")))
    assert state == "claimed" and cached is None
    worker = CoffeeSupplierWorker(database, model_factory=lambda request: (_ for _ in ()).throw(AssertionError("must not retry model")))
    result = worker.process(request)

    assert result.status == "started_uncertain"
    assert result.body is None


def test_supplier_worker_fails_without_an_exact_model_identifier(tmp_path):
    model = ScriptedModel()
    model.model = None
    worker = CoffeeSupplierWorker(tmp_path / "actors.db", model_factory=lambda request: model)

    with pytest.raises(WorkerTurnFailed, match="supplier_model_identifier_missing"):
        worker.process(_request())


def test_supplier_private_transcripts_are_isolated_by_run_and_generation(tmp_path):
    model_instances = []

    def factory(request):
        model = ScriptedModel()
        model_instances.append(model)
        return model

    worker = CoffeeSupplierWorker(tmp_path / "actors.db", model_factory=factory)
    first = _request()
    worker.process(first)

    second_payload = first.model_dump(mode="json")
    second_payload.update({"turn_id": "turn-run-2", "run_id": "run-2", "generation": 1})
    second_payload["scenario"]["supplier_private_state"]["dispatch_evidence_ids"] = ["manifest-private-run-2"]
    second = ActorTurnRequest.model_validate(second_payload)
    worker.process(second)

    second_prompt = model_instances[1].seen_messages[0]
    assert "manifest-private-1" not in str(second_prompt)
    assert "manifest-private-run-2" not in str(second_prompt)
    second_tool_observation = model_instances[1].seen_messages[1]
    assert "manifest-private-1" not in str(second_tool_observation)
    assert "manifest-private-run-2" in str(second_tool_observation)


def test_supplier_worker_api_requires_configured_bearer_and_redacts_failed_turn(tmp_path):
    model_calls = []

    def failed_factory(request):
        model_calls.append(request.turn_id)
        raise RuntimeError("provider response must not be returned")

    app = create_supplier_worker_app(CoffeeSupplierWorker(tmp_path / "api.db", model_factory=failed_factory),
                                    bearer_secret="worker-secret")
    client = TestClient(app)
    request = _request()
    body = request.model_dump(mode="json")

    assert not bearer_token_matches("Bearer nonASCII-🔒", "worker-secret")
    unauthorized = client.post("/v1/turn", json=body, headers={"Authorization": "Bearer wrong-secret"})
    assert unauthorized.status_code == 401
    assert model_calls == []

    failed = client.post("/v1/turn", json=body, headers={"Authorization": "Bearer worker-secret"})
    assert failed.status_code == 503
    assert "provider response must not be returned" not in failed.text
    assert model_calls == [request.turn_id]


def test_started_uncertain_is_http_202_and_exact_retry_does_not_submit_model_again(tmp_path):
    database = tmp_path / "api.db"
    request = _request()
    from servers.simulation.actor_worker_store import ActorTurnStore

    ActorTurnStore(database).claim(request.turn_id, canonical_request_hash(request.model_dump(mode="json")))
    app = create_supplier_worker_app(
        CoffeeSupplierWorker(database, model_factory=lambda request: (_ for _ in ()).throw(AssertionError("no retry"))),
        bearer_secret="worker-secret",
    )
    response = TestClient(app).post("/v1/turn", json=request.model_dump(mode="json"),
                                   headers={"Authorization": "Bearer worker-secret"})

    assert response.status_code == 202
    assert response.json()["status"] == "started_uncertain"
    assert response.json()["body"] is None


def test_supplier_worker_entrypoint_fails_clearly_without_private_configuration(monkeypatch):
    for key in ("ACTOR_WORKER_DATABASE_PATH", "ACTOR_WORKER_BEARER_SECRET", "CODEX_HOME", "CODEX_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="missing required environment configuration"):
        importlib.import_module("servers.simulation.supplier_worker_entrypoint")


def test_supplier_worker_entrypoint_validates_and_redacts_bearer_setting(monkeypatch, tmp_path):
    from pydantic import SecretStr, ValidationError

    token = "private-worker-token-0123456789abcdef"
    monkeypatch.setenv("ACTOR_WORKER_DATABASE_PATH", str(tmp_path / "worker.sqlite"))
    monkeypatch.setenv("ACTOR_WORKER_BEARER_SECRET", token)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_MODEL", "gpt-6-luna")
    module = importlib.import_module("servers.simulation.supplier_worker_entrypoint")
    module = importlib.reload(module)
    settings = module.SupplierWorkerSettings.from_environment()

    assert isinstance(settings.bearer_secret, SecretStr)
    assert settings.bearer_secret.get_secret_value() == token
    with pytest.raises(ValidationError) as error:
        module.SupplierWorkerSettings(
            database_path=tmp_path / "worker.sqlite", bearer_secret="tinysecret",
            codex_home=tmp_path, codex_model="gpt-6-luna",
        )
    assert token not in str(error.value)
    assert "tinysecret" not in str(error.value)
