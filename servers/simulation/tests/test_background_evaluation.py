from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json

from servers.simulation.background_evaluation import BackgroundEvaluationService
from servers.simulation.store import SQLiteStore


COFFEE_COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
INIT_HASH = "f" * 64


def _provider():
    world = {"cursor": "2026-10-30T09:00:00+00:00", "net_income": 12.5}
    identity_payload = {
        "company_id": "company", "provider_id": "coffeebench",
        "seed": 17, "started_at": "2026-10-30T00:00:00+00:00",
        "upstream_commit": COFFEE_COMMIT, "business_ids": ["roaster_A"],
    }
    source = {
        "identity_fingerprint": hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "initialization_request_sha256": INIT_HASH,
            "money_metric_units": {"audit.annual.true_net_income": "USD"},
    }
    captures = {}
    calls = []
    identity_calls = []

    def identity():
        identity_calls.append(True)
        return {
            "company_id": "company", "provider_id": "coffeebench",
            "upstream_commit": COFFEE_COMMIT, "seed": identity_payload["seed"],
            "started_at": identity_payload["started_at"],
            **source,
        }

    def capture():
        native_state_hash = hashlib.sha256(
            f"{world['cursor']}:{world['net_income']}".encode()
        ).hexdigest()
        snapshot = {
            "upstream_commit": COFFEE_COMMIT,
            **source,
            "native_state_ref": f"coffeebench-native:{native_state_hash[:24]}",
            "native_state_hash": native_state_hash,
            "native_company_time": world["cursor"],
            "private_capture_id": native_state_hash,
        }
        captures[native_state_hash] = world["net_income"]
        return snapshot

    def evaluate(snapshot):
        calls.append(snapshot["native_state_hash"])
        return {"native_evaluations": [{
            "actor_id": "roaster_A", "status": "final",
            "score_metric": "audit.annual.true_net_income",
            "score": captures[snapshot["native_state_hash"]],
        }]}

    return {
        "identity": identity,
        "capture": capture,
        "evaluate": evaluate,
        "verify": lambda snapshot: snapshot["native_state_hash"] in captures,
    }, world, calls, identity_payload, source, identity_calls


def test_company_provider_history_is_company_global_and_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "background.sqlite")
    provider, world, calls, identity_payload, source, identity_calls = _provider()
    service = BackgroundEvaluationService(
        store, tmp_path / "private-evidence",
        providers={"coffeebench": provider}, guard_factory=lambda _run_id: nullcontext(),
    )

    first = service.evaluate("coffeebench", "controller", "score-1")
    world.update(cursor="2026-10-30T12:00:00+00:00", net_income=30.0)
    # A reinitialized provider source is a distinct history subject, but the
    # caller's existing idempotency key remains bound to its original source.
    identity_payload["seed"] = 18
    source["identity_fingerprint"] = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source["initialization_request_sha256"] = "e" * 64
    replay = service.evaluate("coffeebench", "controller", "score-1")
    second = service.evaluate("coffeebench", "controller", "score-2")

    assert first["status"] == "completed"
    assert first["scope"] == "company_global"
    assert first["attribution"] == "company_global_not_experiment"
    assert first["money_metric_units"] == {
        "company_feedback.native_benchmark_result.coffeebench.roaster_A.audit.annual.true_net_income": "USD"
    }
    assert len(first["native_state_hash"]) == 64
    assert first["native_state_ref"].startswith("coffeebench-native:")
    assert first["native_company_time"] == "2026-10-30T09:00:00+00:00"
    assert first["scores"][
        "company_feedback.native_benchmark_result.coffeebench.roaster_A.audit.annual.true_net_income"
    ] == 12.5
    assert replay == first
    assert second["scores"][
        "company_feedback.native_benchmark_result.coffeebench.roaster_A.audit.annual.true_net_income"
    ] == 30.0
    assert first["evidence_hash"] != second["evidence_hash"]
    assert first["source_id"] != second["source_id"]
    assert "run_id" not in first and "experiment_id" not in first
    assert len(calls) == 2
    assert len(identity_calls) == 2

    with store._connection(None) as db:
        subject = db.execute(
            "SELECT run_id FROM background_evaluation_subjects WHERE provider_id='coffeebench'"
        ).fetchone()
        run = db.execute("SELECT status,scenario_id FROM runs WHERE run_id=?", (subject["run_id"],)).fetchone()
        link = db.execute("SELECT 1 FROM company_experiments WHERE run_id=?", (subject["run_id"],)).fetchone()
    assert tuple(run) == ("background", "company_background")
    assert link is None
    assert service.latest("coffeebench") == second


def test_company_provider_incomplete_horizon_keeps_native_metric_null(tmp_path):
    store = SQLiteStore(tmp_path / "background-incomplete.sqlite")
    provider, _world, _calls, _identity_payload, _source, _identity_calls = _provider()
    provider["evaluate"] = lambda _snapshot: {"native_evaluations": [{
        "actor_id": "roaster_A", "status": "incomplete",
        "score_metric": "audit.annual.true_net_income", "score": None,
    }]}
    service = BackgroundEvaluationService(
        store, tmp_path / "private-evidence",
        providers={"coffeebench": provider}, guard_factory=lambda _run_id: nullcontext(),
    )

    result = service.evaluate("coffeebench", "controller", "incomplete-horizon")

    assert result["status"] == "incomplete"
    assert result["scores"][
        "company_feedback.native_benchmark_result.coffeebench.roaster_A.audit.annual.true_net_income"
    ] is None
    assert result["attribution"] == "company_global_not_experiment"


def test_preidentity_failure_is_reserved_persisted_and_same_key_replays_after_restart(tmp_path):
    store = SQLiteStore(tmp_path / "background-offline.sqlite")
    provider, _world, calls, _identity_payload, _source, _identity_calls = _provider()
    identity_calls = []

    def offline_identity():
        identity_calls.append(True)
        with store._connection(None) as db:
            reserved = db.execute(
                "SELECT status,upstream_commit,source_identity_json FROM background_evaluation_attempts "
                "WHERE caller_actor_id='controller'",
            ).fetchone()
        assert reserved["status"] == "reserved"
        assert reserved["upstream_commit"] == COFFEE_COMMIT
        assert reserved["source_identity_json"] is None
        raise RuntimeError("raw private transport details")

    provider["identity"] = offline_identity
    evidence_directory = tmp_path / "private-evidence"
    service = BackgroundEvaluationService(
        store, evidence_directory, providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )
    failed = service.evaluate("coffeebench", "controller", "offline-key", correlation_id="corr-offline")

    assert failed["status"] == "failed"
    assert failed["evaluation_id"]
    assert failed["upstream_commit"] == COFFEE_COMMIT
    assert failed["correlation_id"] == "corr-offline"
    assert failed["failure"] == {
        "code": "source_identity_unavailable", "exception_type": "RuntimeError",
    }
    assert not {"source_id", "seed", "started_at", "native_state_ref", "native_state_hash"} & set(failed)
    assert calls == []
    assert identity_calls == [True]
    assert service.list("coffeebench") == [failed]
    assert service.latest("coffeebench") == failed
    assert service.get(failed["evaluation_id"]) == failed
    with store._connection(None) as db:
        assert db.execute("SELECT count(*) FROM evaluation_reservations").fetchone()[0] == 0

    # A restart keeps the failure terminal for this key and does not probe or
    # evaluate a potentially different source generation on replay.
    restarted = BackgroundEvaluationService(
        store, evidence_directory, providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )
    assert restarted.evaluate("coffeebench", "controller", "offline-key") == failed
    assert identity_calls == [True]
    assert calls == []

    provider, _world, _calls, _identity_payload, _source, _identity_calls = _provider()
    resumed = BackgroundEvaluationService(
        store, evidence_directory, providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )
    success = resumed.evaluate("coffeebench", "controller", "new-key")
    assert success["status"] == "completed"
    assert success["evaluation_id"] != failed["evaluation_id"]
    with store._connection(None) as db:
        row = db.execute(
            "SELECT evaluation_id FROM evaluation_reservations WHERE evaluation_id=?",
            (success["evaluation_id"],),
        ).fetchone()
        attempt = db.execute(
            "SELECT status FROM background_evaluation_attempts WHERE evaluation_id=?",
            (success["evaluation_id"],),
        ).fetchone()
    assert row is not None
    assert attempt["status"] == "linked"
    assert {item["evaluation_id"] for item in resumed.list("coffeebench")} == {
        failed["evaluation_id"], success["evaluation_id"],
    }


def test_restarted_unfinished_preidentity_attempt_is_visible_uncertain_without_judging(tmp_path):
    store = SQLiteStore(tmp_path / "background-unfinished.sqlite")
    provider, _world, calls, _identity_payload, _source, _identity_calls = _provider()
    service = BackgroundEvaluationService(
        store, tmp_path / "private-evidence", providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )
    attempt = service.attempts.reserve(
        "coffeebench", "controller", "crashed-before-identity",
        upstream_commit=COFFEE_COMMIT, initialization_request_sha256=None,
        correlation_id="corr-pending",
    )

    restarted = BackgroundEvaluationService(
        store, tmp_path / "private-evidence", providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )
    pending = restarted.latest("coffeebench")
    assert pending["evaluation_id"] == attempt["evaluation_id"]
    assert pending["status"] == "incomplete"
    assert pending["lifecycle"] == "uncertain"
    assert pending["failure"] is None
    assert calls == []


def test_failed_preidentity_retry_survives_subsequent_company_initialization(tmp_path):
    store = SQLiteStore(tmp_path / "later-initialization.sqlite")
    provider, _world, calls, _identity_payload, _source, _identity_calls = _provider()
    lookups = []

    def unavailable():
        lookups.append(True)
        raise ConnectionError("private transport detail")

    provider["identity"] = unavailable
    service = BackgroundEvaluationService(
        store, tmp_path / "evidence", providers={"coffeebench": provider},
        guard_factory=lambda _identity: nullcontext(),
    )
    failed = service.evaluate("coffeebench", "controller", "same-request")
    store.background_initialization_request({
        "company_id": "company", "provider_id": "coffeebench", "seed": 17,
        "started_at": "2026-10-30T00:00:00+00:00", "binding": {"company_business_ids": ["roaster_A"]},
    })
    assert service.evaluate("coffeebench", "controller", "same-request") == failed
    assert lookups == [True]
    assert calls == []


def test_identity_must_match_durable_provider_initialization_before_subject_registration(tmp_path):
    store = SQLiteStore(tmp_path / "background-init-mismatch.sqlite")
    initialization = {
        "company_id": "company", "provider_id": "coffeebench", "seed": 18,
        "started_at": "2026-10-30T00:00:00+00:00", "binding": {"company_business_ids": ["roaster_A"]},
    }
    store.background_initialization_request(initialization)
    provider, _world, calls, _identity_payload, _source, _identity_calls = _provider()
    service = BackgroundEvaluationService(
        store, tmp_path / "private-evidence", providers={"coffeebench": provider},
        guard_factory=lambda _attempt_id: nullcontext(),
    )

    result = service.evaluate("coffeebench", "controller", "wrong-initialization")

    assert result["status"] == "failed"
    assert result["failure"] == {
        "code": "source_identity_unavailable", "exception_type": "ValueError",
    }
    assert "source_id" not in result
    assert calls == []
    with store._connection(None) as db:
        assert db.execute("SELECT count(*) FROM background_evaluation_subjects").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM evaluation_reservations").fetchone()[0] == 0
