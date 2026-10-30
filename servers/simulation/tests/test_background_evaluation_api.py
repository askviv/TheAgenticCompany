from contextlib import nullcontext
import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient

from servers.simulation.app import create_app
from servers.simulation.background_evaluation import (
    BackgroundEvaluationService,
    background_evaluation_guard,
)
from servers.simulation.evaluation_service import EvaluationService
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore


COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
ACTOR_ID = "roaster_A"


def _normal_service(store, evidence_directory):
    return EvaluationService(
        store,
        evidence_directory,
        snapshotter=lambda run_id, generation: {"run_id": run_id, "generation": generation},
        evaluator=lambda _run_id, _snapshot: {"checks": {"normal_case": True}},
        version_verifier=lambda _run_id, _snapshot: True,
        identity_resolver=lambda _run_id, _generation, _snapshot: {
            "benchmark": "company-scenarios",
            "domain": "A1",
            "benchmark_version": "fixture-v1",
            "evaluation_mode": "company_adapted",
        },
        guard_factory=lambda _run_id: nullcontext(),
    )


def _native_provider(source):
    def identity():
        fingerprint = hashlib.sha256(f"identity:{source['seed']}".encode()).hexdigest()
        initialization_hash = hashlib.sha256(f"init:{source['seed']}".encode()).hexdigest()
        source["fingerprint"] = fingerprint
        source["initialization_hash"] = initialization_hash
        return {
            "company_id": "company",
            "provider_id": "coffeebench",
            "upstream_commit": COMMIT,
            "seed": source["seed"],
            "started_at": "2026-10-30T00:00:00+00:00",
            "identity_fingerprint": fingerprint,
            "initialization_request_sha256": initialization_hash,
            "money_metric_units": {"audit.annual.true_net_income": "USD"},
        }

    def capture():
        return {
            "upstream_commit": COMMIT,
            "native_state_ref": f"coffeebench-state-{source['epoch']}",
            "native_state_hash": str(source["epoch"]) * 64,
            "native_company_time": "2026-10-30T00:00:00+00:00",
            "identity_fingerprint": source["fingerprint"],
            "initialization_request_sha256": source["initialization_hash"],
        }

    def evaluate(_snapshot):
        source["evaluation_calls"] += 1
        return {
            "native_evaluations": [{
                "actor_id": ACTOR_ID,
                "status": "final",
                "score_metric": "audit.annual.true_net_income",
                "score": source["score"],
            }],
        }

    return {
        "identity": identity,
        "capture": capture,
        "evaluate": evaluate,
        "verify": lambda _snapshot: True,
    }


def test_company_background_evaluation_routes_preserve_source_idempotency_and_normal_history(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    evidence_directory = tmp_path / "evaluation-evidence"
    normal_run_id = str(uuid4())
    store.create_run(normal_run_id, StartScenario(scenario_id="A1"))
    normal_service = _normal_service(store, evidence_directory)
    normal_record = normal_service.evaluate(normal_run_id, "controller-1", "normal-eval").record

    source = {"seed": 17, "epoch": 1, "score": 17.25, "evaluation_calls": 0}
    background_service = BackgroundEvaluationService(
        store,
        evidence_directory,
        providers={"coffeebench": _native_provider(source), "ecommercebench": {}},
        guard_factory=background_evaluation_guard(evidence_directory),
    )
    assert source["evaluation_calls"] == 0

    engine = type("Engine", (), {})()
    engine.store = store
    engine.evaluation_service = normal_service
    engine.background_evaluation_service = background_service
    client = TestClient(create_app(engine, {
        "controller-secret": {"actor_id": "controller-1", "role": "controller"},
        "evaluator-secret": {"actor_id": "evaluator-1", "role": "evaluator"},
        "agent-secret": {"actor_id": "viv", "role": "agent"},
    }))

    def headers(key, token="controller-secret"):
        return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    first_response = client.post("/company/evaluations/coffeebench", headers=headers("shared-world-key"))
    assert first_response.status_code == 200
    first = first_response.json()["evaluation"]
    assert first["provider_id"] == "coffeebench"
    assert first["scope"] == "company_global"
    assert first["attribution"] == "company_global_not_experiment"
    assert first["scores"]["company_feedback.native_benchmark_result.coffeebench.roaster_A.audit.annual.true_net_income"] == 17.25
    assert "run_id" not in first and "experiment_id" not in first

    source.update(seed=29, epoch=2, score=29.5)
    retry_response = client.post("/company/evaluations/coffeebench", headers=headers("shared-world-key"))
    assert retry_response.status_code == 200
    retry = retry_response.json()["evaluation"]
    assert retry["evaluation_id"] == first["evaluation_id"]
    assert retry["source_id"] == first["source_id"]
    assert retry["scores"] == first["scores"]
    assert source["evaluation_calls"] == 1

    new_response = client.post("/company/evaluations/coffeebench", headers=headers("next-world-key"))
    assert new_response.status_code == 200
    new = new_response.json()["evaluation"]
    assert new["evaluation_id"] != first["evaluation_id"]
    assert new["source_id"] != first["source_id"]
    assert new["seed"] == 29
    assert source["evaluation_calls"] == 2

    latest = client.get("/company/evaluations/coffeebench/latest", headers={
        "Authorization": "Bearer controller-secret",
    }).json()
    assert latest["selection"] == "latest_saved"
    assert latest["evaluation"]["evaluation_id"] == new["evaluation_id"]
    listing = client.get("/company/evaluations/coffeebench", headers={
        "Authorization": "Bearer evaluator-secret",
    })
    assert listing.status_code == 200
    assert {row["evaluation_id"] for row in listing.json()["evaluations"]} == {
        first["evaluation_id"], new["evaluation_id"],
    }
    detail = client.get(
        f"/company/evaluations/coffeebench/{first['evaluation_id']}",
        headers={"Authorization": "Bearer controller-secret"},
    )
    assert detail.status_code == 200
    assert detail.json()["evaluation"]["source_id"] == first["source_id"]
    assert client.get(
        f"/company/evaluations/ecommercebench/{first['evaluation_id']}",
        headers={"Authorization": "Bearer controller-secret"},
    ).status_code == 404
    assert client.get(
        "/company/evaluations/unknown", headers={"Authorization": "Bearer controller-secret"},
    ).status_code == 404
    assert client.post(
        "/company/evaluations/coffeebench", headers=headers("agent-key", "agent-secret"),
    ).status_code == 403
    assert client.get(
        "/company/evaluations/coffeebench", headers={"Authorization": "Bearer agent-secret"},
    ).status_code == 403

    # Background metadata rows share the evaluation database but are not
    # scenario runs; ordinary run APIs must not expose or mutate them.
    with store.transaction() as connection:
        background_run_id = connection.execute(
            "SELECT run_id FROM background_evaluation_subjects WHERE provider_id='coffeebench' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    assert client.post(
        f"/runs/{background_run_id}/evaluations", headers=headers("wrong-evaluator"),
    ).status_code == 404
    assert client.post(
        f"/scenarios/{background_run_id}/reset", headers=headers("wrong-reset"),
    ).status_code == 404
    assert client.get(
        f"/runs/{normal_run_id}/evaluations/latest",
        headers={"Authorization": "Bearer controller-secret"},
    ).json()["evaluation"]["evaluation_id"] == normal_record.evaluation_id


def test_runtime_registers_read_only_provider_hooks_on_shared_evaluation_store(tmp_path):
    import json

    from servers.simulation.runtime import build_app

    mail_credentials = tmp_path / "mail-credentials.json"
    principals = tmp_path / "principals.json"
    config_path = tmp_path / "runtime.json"
    mail_credentials.write_text(json.dumps({
        "agent@agent.example.test": {"username": "service", "password": "synthetic"},
    }))
    principals.write_text(json.dumps({
        "controller-secret": {"actor_id": "controller", "role": "controller"},
    }))
    for path in (mail_credentials, principals):
        path.chmod(0o600)
    config_path.write_text(json.dumps({
        "state_root": str(tmp_path / "state"),
        "erp_compose": str(tmp_path / "compose.yml"),
        "erp_credentials_root": str(tmp_path / "erp-secrets"),
        "company_binding": {
            "site_name": "erp-sim-" + "c" * 32 + ".localhost",
            "credentials_ref": str(tmp_path / "erp-secrets" / "company.json"),
            "documents": {},
        },
        "company_background_enabled": False,
        "company_issues_enabled": False,
        "actor_workers": {
            "supplier_url": "http://127.0.0.1:19001",
            "supplier_token": "synthetic-worker-token",
            "request_timeout_seconds": 1,
        },
        "mail": {"actors": [{
            "actor_id": "viv", "kind": "agent", "email": "agent@agent.example.test",
        }]},
        "mail_credentials_file": str(mail_credentials),
        "principals_file": str(principals),
        "workers_enabled": False,
    }))
    config_path.chmod(0o600)

    app, _config = build_app(config_path)
    engine = app.state.engine
    service = engine.background_evaluation_service
    assert set(service.providers) == {"coffeebench", "ecommercebench"}
    expected_directory = (tmp_path / "state" / "evaluation-evidence").resolve()
    assert engine.evaluation_service.records.snapshot_directory == expected_directory
    assert service.evaluations.records.snapshot_directory == expected_directory
    with engine.store.transaction() as connection:
        assert connection.execute(
            "SELECT count(*) FROM background_evaluation_subjects"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM ecommerce_native_world_state"
        ).fetchone()[0] == 0
    engine.actor_turn_client.session.close()
    engine.company_feedback.actor_client.session.close()
    for provider in service.providers.values():
        # Runtime provider adapters are read-only here; source capture and
        # evaluator callbacks are invoked only by the explicit POST endpoint.
        assert callable(provider["capture"])
    service_worker = service.providers["coffeebench"]["identity"].__self__
    service_worker.session.close()
