from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from servers.simulation.background_runtime import CompanyBackgroundRuntime, ECOMMERCE_UPSTREAM_COMMIT
from servers.simulation.background_worker import BackgroundWorker, attach_background_routes
from servers.simulation.integration_contracts import BackgroundAdvanceRequest, BackgroundInitializeRequest
from servers.simulation.store import SQLiteStore
from servers.simulation.businesses import initialize_supplier_business
from servers.simulation.ecommerce_background import initialize as initialize_ecommerce


def _legacy_request(until: datetime, event_id: str) -> BackgroundAdvanceRequest:
    return BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="legacy-coffee-operation", until=until,
        binding={
            "model_id": "gpt-6-luna",
            "company_business_ids": ["roaster_A"],
            "external_supply_condition_policy": {
                "affected_categories": ["all"],
                "target_scope": "native_coffee_market",
                "native_effect": "delivery_lag_days",
            },
            "external_supply_conditions": [{
                "event_id": event_id,
                "source": "qwenlm-ecommercebench-calendar",
                "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
                "occurred_at": "2026-01-15T00:00:00Z",
                "effective_until": "2026-01-19T00:00:00Z",
                "duration_days": 4,
                "event_name": "Winter Storm",
                "store_type": "auto_hardware",
                "affected_categories": "all",
                "delivery_delay_multiplier": "2.5",
            }],
        },
    )


def _source_event(event_id: str) -> dict:
    return {
        "schema_version": 1, "company_id": "company", "event_id": event_id,
        "event_type": "SUPPLIER_SUPPLY_DISRUPTED", "simulated_time": "2026-01-15T00:00:00Z",
        "source": "qwenlm-ecommercebench-calendar", "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "entity_type": "supplier", "entity_id": "SUP-001",
        "payload": {
            "upstream_event_name": "Winter Storm", "upstream_calendar_start_date": "01-15",
            "upstream_calendar_duration_days": 4, "affected_categories": "all",
            "delivery_delay_multiplier": 2.5, "store_type": "auto_hardware",
        },
        "visibility": ["supplier"],
    }


def _source_event_hash(event_id: str) -> str:
    return hashlib.sha256(json_bytes(_source_event(event_id))).hexdigest()


def _source_identity_sha256(initialization: BackgroundInitializeRequest) -> str:
    binding = initialization.binding
    actor_id = binding["supplier_actor_id"]
    private = initialization.actor_private_states[actor_id]
    identity = {
        "provider_id": "ecommercebench", "provider_version": "1",
        "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT, "seed": initialization.seed,
        "started_at": initialization.started_at.astimezone(timezone.utc).isoformat(),
        "supplier_actor_id": actor_id, "supplier_id": private["supplier_id"],
        "store_type": private["business_model"]["store_type"],
    }
    return hashlib.sha256(json_bytes(identity)).hexdigest()


def json_bytes(value: dict) -> bytes:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class LegacyWorker:
    def __init__(self, candidates):
        self.candidates = candidates
        self.lookups = []
        self.advances = []

    def lookup_legacy_ecommerce_receipts(self, target, required_event_ids, company_id):
        self.lookups.append((target, required_event_ids, company_id))
        return self.candidates

    def advance(self, request):
        self.advances.append(request)
        return {
            "provider_id": "coffeebench",
            "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
            "cursor": request.until.isoformat(), "next_due": None, "events": [],
            "outcomes": [], "erp_intents": [],
        }

    def inspect_coffee_recovery(self, request):
        self.inspected_request = request
        return {"status": "started", "confirmed_cursor": "2026-01-15T00:00:00+00:00",
                "checkpoint_sha256": "b" * 64}

    def recover_coffee_advance(self, previous, recovery, *, confirmed_cursor, checkpoint_sha256):
        assert previous == self.inspected_request
        assert recovery.binding == previous.binding
        assert recovery.until == previous.until
        return {
            "provider_id": "coffeebench",
            "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
            "cursor": recovery.until.isoformat(), "next_due": None, "events": [],
            "outcomes": [], "erp_intents": [],
        }


def test_legacy_pending_coffee_request_derives_equivalent_receipts_without_rewriting_request(tmp_path):
    database = tmp_path / "legacy-handoff.sqlite"
    store = SQLiteStore(database)
    event_id = "storm-event-legacy"
    store.record_company_source_events([_source_event(event_id)])
    supplier_state = initialize_supplier_business(
        {"market_config": {"store_type": "auto_hardware"}}, seed=31, supplier_id="SUP-001",
    )
    initialization = BackgroundInitializeRequest(
        provider_id="ecommercebench", seed=31, started_at=datetime(2026, 1, 14, tzinfo=timezone.utc),
        binding={"supplier_actor_id": "supplier", "supplier_id": "SUP-001"},
        actor_private_states={"supplier": supplier_state},
    )
    store.background_initialization_request(initialization.model_dump(mode="json"))
    until = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    request = _legacy_request(until, event_id)
    pending = store.persist_background_provider_pending(
        "coffeebench", request.operation_id, request.model_dump(mode="json"),
    )
    # Recreate the pre-receipt coordinator schema while retaining its old
    # pending row and source facts, then let the normal startup migration run.
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER background_provider_result_ack_no_update")
        connection.execute("DROP TRIGGER background_provider_result_ack_no_delete")
        connection.execute("DROP TABLE background_provider_result_acknowledgements")
        connection.execute("DROP TRIGGER background_provider_result_receipts_no_update")
        connection.execute("DROP TRIGGER background_provider_result_receipts_no_delete")
        connection.execute("DROP TABLE background_provider_result_receipts")
    store = SQLiteStore(database)
    assert store.get_background_provider_pending("coffeebench")["request"] == pending["request"]
    now = datetime.now(timezone.utc).isoformat()
    candidate = {
        "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "cursor": "2026-01-15T00:00:00+00:00", "next_due": "2026-01-16T00:00:00+00:00",
        "coverage_until": until.isoformat(),
        "source_identity_sha256": _source_identity_sha256(initialization),
        "coverage_gap_event_ids_sha256": hashlib.sha256(json_bytes([])).hexdigest(),
        "result_sha256": hashlib.sha256(b"actual-worker-result").hexdigest(),
        "source_event_ids": [event_id], "source_event_sha256": {event_id: _source_event_hash(event_id)},
        "finance_intent_hashes": {},
        "created_at": "2026-01-15T11:00:00+00:00", "completed_at": "2026-01-15T11:01:00+00:00",
    }
    worker = LegacyWorker([
        {**candidate, "operation_id": "ecom-original-a"},
        {**candidate, "operation_id": "ecom-original-b"},
    ])

    result = CompanyBackgroundRuntime(store, worker).advance_company(
        until, bindings={"coffeebench": {}}, provider_ids=("coffeebench",),
        coupling_policy=request.binding["external_supply_condition_policy"],
    )

    assert result["status"] == "complete"
    assert worker.lookups == [(until, [], "company")]
    assert worker.advances == [request]
    assert store.get_background_provider_pending("coffeebench")["request"] == pending["request"]
    receipts = [store.get_background_provider_result_receipt(
        "ecommercebench", operation_id, require_acknowledged=True,
    ) for operation_id in ("ecom-original-a", "ecom-original-b")]
    assert all(receipt and receipt["evidence_origin"] == "derived_worker_operation" for receipt in receipts)
    assert all(receipt["cursor"] == "2026-01-15T00:00:00+00:00" for receipt in receipts)
    assert all(receipt["coverage_until"] == until.isoformat() for receipt in receipts)
    with store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM background_provider_result_coverage").fetchone()[0] == 2
    assert all(receipt["source_event_ids"] == [event_id] for receipt in receipts)


def test_legacy_pending_request_reports_integrity_gap_when_worker_proof_is_missing(tmp_path):
    store = SQLiteStore(tmp_path / "legacy-handoff-gap.sqlite")
    event_id = "storm-event-legacy"
    store.record_company_source_events([_source_event(event_id)])
    supplier_state = initialize_supplier_business(
        {"market_config": {"store_type": "auto_hardware"}}, seed=31, supplier_id="SUP-001",
    )
    initialization = BackgroundInitializeRequest(
        provider_id="ecommercebench", seed=31, started_at=datetime(2026, 1, 14, tzinfo=timezone.utc),
        binding={"supplier_actor_id": "supplier", "supplier_id": "SUP-001"},
        actor_private_states={"supplier": supplier_state},
    )
    store.background_initialization_request(initialization.model_dump(mode="json"))
    until = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    request = _legacy_request(until, event_id)
    store.persist_background_provider_pending(
        "coffeebench", request.operation_id, request.model_dump(mode="json"),
    )
    worker = LegacyWorker([])

    result = CompanyBackgroundRuntime(store, worker).advance_company(
        until, bindings={"coffeebench": {}}, provider_ids=("coffeebench",),
        coupling_policy=request.binding["external_supply_condition_policy"],
    )

    assert result["status"] == "partial"
    assert result["provider_errors"]["coffeebench"]["code"] == "legacy_worker_proof_not_found"
    assert worker.advances == []
    assert store.get_background_provider_pending("coffeebench")["request"] == request.model_dump(mode="json")
    assert store.latest_acknowledged_background_provider_result_receipt("ecommercebench") is None


def test_worker_receipt_lookup_reads_only_completed_pinned_native_results(tmp_path):
    worker = BackgroundWorker(
        tmp_path / "worker.sqlite", checkpoint_directory=tmp_path / "checkpoints",
        coffee_model_factory=lambda actor, model: None, configured_model_id="gpt-6-luna",
    )
    source_cursor = datetime(2026, 1, 15, tzinfo=timezone.utc)
    until = source_cursor + timedelta(hours=9, minutes=30)
    supplier_state = initialize_supplier_business(
        {"market_config": {"store_type": "auto_hardware"}}, seed=31, supplier_id="SUP-001",
    )
    binding = {"company_id": "company", "supplier_actor_id": "supplier", "supplier_id": "SUP-001"}
    snapshot = initialize_ecommerce(31, source_cursor, binding, {"supplier": supplier_state})
    snapshot["cursor"] = source_cursor.isoformat()
    result = {
        "provider_id": "ecommercebench", "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "cursor": source_cursor.isoformat(), "next_due": "2026-01-16T00:00:00Z", "events": [],
        "outcomes": [], "erp_intents": [],
    }
    request = {"operation_id": "ecom-completed", "target": until.isoformat()}
    worker.store.claim_background_operation("ecommercebench", "ecom-completed", request)
    worker.store.complete_background_operation(
        "ecommercebench", "ecom-completed", request,
        upstream_commit=ECOMMERCE_UPSTREAM_COMMIT, seed=31,
        started_at=source_cursor, cursor=source_cursor,
        snapshot=snapshot, result=result, intents=[],
    )
    worker.store.claim_background_operation(
        "ecommercebench", "ecom-still-started", {"operation_id": "ecom-still-started"},
    )

    app = FastAPI()
    attach_background_routes(app, worker, bearer_secret="internal-test-token")
    client = TestClient(app)
    body = {"company_id": "company", "target_cursor": until.isoformat(),
            "required_event_ids": []}
    assert client.post("/v1/background/ecommerce/legacy-receipts", json=body).status_code == 401
    response = client.post(
        "/v1/background/ecommerce/legacy-receipts", json=body,
        headers={"Authorization": "Bearer internal-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [candidate["operation_id"] for candidate in payload["candidates"]] == ["ecom-completed"]
    assert payload["candidates"][0]["source_event_ids"] == []
    assert payload["candidates"][0]["result_sha256"]
    assert payload["candidates"][0]["cursor"] == source_cursor.isoformat()
    assert payload["candidates"][0]["coverage_until"] == until.isoformat()
    assert payload["candidates"][0]["coverage_gap_event_ids_sha256"] == hashlib.sha256(json_bytes([])).hexdigest()
    assert "events" not in payload["candidates"][0]

    # A stored next_due later than the target is insufficient by itself: the
    # pinned native calendar still emits its next daily demand event at midnight.
    claimed_future_result = {**result, "next_due": "2026-01-17T00:00:00Z"}
    future_request = {"operation_id": "ecom-future-claim", "target": "2026-01-17T00:00:00Z"}
    worker.store.claim_background_operation("ecommercebench", "ecom-future-claim", future_request)
    worker.store.complete_background_operation(
        "ecommercebench", "ecom-future-claim", future_request,
        upstream_commit=ECOMMERCE_UPSTREAM_COMMIT, seed=31,
        started_at=source_cursor, cursor=source_cursor,
        snapshot=snapshot, result=claimed_future_result, intents=[],
    )
    gap_response = client.post(
        "/v1/background/ecommerce/legacy-receipts",
        json={**body, "target_cursor": "2026-01-16T09:30:00Z"},
        headers={"Authorization": "Bearer internal-test-token"},
    )
    assert gap_response.status_code == 200
    assert gap_response.json()["candidates"] == []


def test_direct_legacy_recovery_verifies_handoff_before_creating_recovery_attempt(tmp_path):
    store = SQLiteStore(tmp_path / "legacy-recovery.sqlite")
    event_id = "storm-event-legacy-recovery"
    store.record_company_source_events([_source_event(event_id)])
    supplier_state = initialize_supplier_business(
        {"market_config": {"store_type": "auto_hardware"}}, seed=31, supplier_id="SUP-001",
    )
    initialization = BackgroundInitializeRequest(
        provider_id="ecommercebench", seed=31, started_at=datetime(2026, 1, 14, tzinfo=timezone.utc),
        binding={"supplier_actor_id": "supplier", "supplier_id": "SUP-001"},
        actor_private_states={"supplier": supplier_state},
    )
    store.background_initialization_request(initialization.model_dump(mode="json"))
    until = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    request = _legacy_request(until, event_id)
    store.persist_background_provider_pending(
        "coffeebench", request.operation_id, request.model_dump(mode="json"),
    )
    candidate = {
        "operation_id": "ecom-recovery-source", "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "cursor": "2026-01-15T00:00:00+00:00", "next_due": "2026-01-16T00:00:00+00:00",
        "coverage_until": until.isoformat(),
        "source_identity_sha256": _source_identity_sha256(initialization),
        "coverage_gap_event_ids_sha256": hashlib.sha256(json_bytes([])).hexdigest(),
        "result_sha256": hashlib.sha256(b"actual-worker-result").hexdigest(),
        "source_event_ids": [event_id], "source_event_sha256": {event_id: _source_event_hash(event_id)},
        "finance_intent_hashes": {},
        "created_at": "2026-01-15T11:00:00+00:00", "completed_at": "2026-01-15T11:01:00+00:00",
    }
    worker = LegacyWorker([candidate])

    outcome = CompanyBackgroundRuntime(store, worker).recover_pending_coffeebench(
        request.operation_id, "operator-key",
    )

    assert outcome["status"] == "completed"
    assert worker.inspected_request == request
    recovery_pending = store.get_background_provider_pending("coffeebench")
    assert recovery_pending["request"]["binding"] == request.binding
    assert datetime.fromisoformat(recovery_pending["request"]["until"].replace("Z", "+00:00")) == request.until
    receipt = store.get_background_provider_result_receipt(
        "ecommercebench", candidate["operation_id"], require_acknowledged=True,
    )
    assert receipt["evidence_origin"] == "derived_worker_operation"
