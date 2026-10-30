from contextlib import nullcontext
from uuid import uuid4

from fastapi.testclient import TestClient

from servers.simulation.app import create_app
from servers.simulation.evaluation_service import EvaluationService
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore


def _service(store, directory):
    return EvaluationService(
        store, directory,
        snapshotter=lambda run_id, generation: {"run_id": run_id, "generation": generation},
        evaluator=lambda _run_id, _snapshot: {
            "checks": {"native_document_read": True},
            "metrics": {"elapsed_simulated_seconds": 18},
        },
        version_verifier=lambda _run_id, _snapshot: True,
        identity_resolver=lambda _run_id, _generation, _snapshot: {
            "benchmark": "company-scenarios", "domain": "A1",
            "benchmark_version": "fixture-version", "evaluation_mode": "company_adapted",
        },
        guard_factory=lambda _run_id: nullcontext(),
    )


def test_evaluation_routes_require_privileged_role_and_return_safe_durable_dto(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    run_id = str(uuid4())
    store.create_run(run_id, StartScenario(scenario_id="A1"))
    engine = type("Engine", (), {})()
    engine.store = store
    engine.evaluation_service = _service(store, tmp_path / "private")
    client = TestClient(create_app(engine, {
        "controller-secret": {"actor_id": "controller-1", "role": "controller"},
        "evaluator-secret": {"actor_id": "evaluator-1", "role": "evaluator"},
        "agent-secret": {"actor_id": "viv", "role": "agent"},
    }))

    started = client.post(f"/runs/{run_id}/evaluations", headers={
        "Authorization": "Bearer controller-secret", "Idempotency-Key": "eval-1",
    })
    assert started.status_code == 200
    dto = started.json()["evaluation"]
    assert dto["status"] == "completed"
    assert dto["scores"]["checks.native_document_read"] is True
    assert dto["scores"]["metrics.elapsed_simulated_seconds"] == 18
    assert "result_ref" not in dto and "caller_actor_id" not in dto and "idempotency_key_hash" not in dto

    replay = client.post(f"/scenarios/{run_id}/evaluate", headers={
        "Authorization": "Bearer controller-secret", "Idempotency-Key": "eval-1",
    })
    assert replay.status_code == 200
    assert replay.json()["evaluation"]["evaluation_id"] == dto["evaluation_id"]
    assert client.get(f"/runs/{run_id}/evaluations", headers={
        "Authorization": "Bearer evaluator-secret",
    }).json()["evaluations"][0]["evaluation_id"] == dto["evaluation_id"]
    assert client.get(f"/runs/{run_id}/evaluations/latest", headers={
        "Authorization": "Bearer controller-secret",
    }).json()["evaluation"]["evaluation_id"] == dto["evaluation_id"]
    assert client.get(f"/evaluations/{dto['evaluation_id']}", headers={
        "Authorization": "Bearer controller-secret",
    }).json()["evaluation"]["evaluation_id"] == dto["evaluation_id"]
    assert client.post(f"/runs/{run_id}/evaluations", headers={
        "Authorization": "Bearer controller-secret",
    }).status_code == 400
    assert client.post(f"/runs/{run_id}/evaluations", headers={
        "Authorization": "Bearer agent-secret", "Idempotency-Key": "agent-eval",
    }).status_code == 403


def test_latest_returns_null_when_run_has_no_evaluation(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    run_id = str(uuid4())
    store.create_run(run_id, StartScenario(scenario_id="A1"))
    engine = type("Engine", (), {})()
    engine.store = store
    engine.evaluation_service = _service(store, tmp_path / "private")
    client = TestClient(create_app(engine, {
        "controller-secret": {"actor_id": "controller-1", "role": "controller"},
    }))
    response = client.get(f"/runs/{run_id}/evaluations/latest", headers={
        "Authorization": "Bearer controller-secret",
    })
    assert response.status_code == 200
    assert response.json() == {"evaluation": None}
