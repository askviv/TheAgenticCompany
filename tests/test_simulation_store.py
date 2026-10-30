from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

import pytest

from servers.simulation.models import CanonicalEvent, StartScenario
from servers.simulation.store import (
    IdempotencyConflict,
    SQLiteStore,
    StoreConflict,
    StoreNotFound,
)


def start(scenario_id="C1"):
    return StartScenario(scenario_id=scenario_id, seed=4, agent_id="agent-a")


def event(run_id, event_id="event-1", *, amount=2):
    return CanonicalEvent(
        event_id=event_id,
        run_id=run_id,
        scenario_id="C1",
        simulated_time=datetime(2026, 10, 29, tzinfo=timezone.utc),
        event_type="SHIPMENT_DELAYED",
        source="scenario",
        entity_type="shipment",
        entity_id="shipment-1",
        related_entities=["PO-1042", "case-1"],
        payload={"delay_days": amount},
        visibility=["logistics"],
    )


def test_records_are_scoped_by_run_and_generation(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_a, run_b = uuid4(), uuid4()
    store.create_run(run_a, start())
    store.create_run(run_b, start())
    store.put_case(run_a, 0, "case-1", {"status": "open", "received": 8_000})
    store.put_case(run_b, 0, "case-1", {"status": "open", "received": 7_000})
    store.put_actor(run_a, 0, "supplier", kind="supplier", email="supplier@example.test", private_state={"dispatch": 10_000})
    store.put_actor(run_b, 0, "supplier", kind="supplier", email="supplier@example.test", private_state={"dispatch": 7_000})

    assert store.get_case(run_a, 0, "case-1")["visible"]["received"] == 8_000
    assert store.get_case(run_b, 0, "case-1")["visible"]["received"] == 7_000
    assert store.get_actor(run_a, 0, "supplier")["private_state"]["dispatch"] == 10_000
    assert [actor["actor_id"] for actor in store.list_actors(run_b, 0)] == ["supplier"]
    with pytest.raises(StoreNotFound):
        store.get_case(run_a, 1, "case-1")


def test_run_clock_and_event_updates_compose_in_one_rollbackable_transaction(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_id = uuid4()
    store.create_run(run_id, start())
    instant = datetime(2026, 10, 30, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="abort composed write"):
        with store.transaction() as connection:
            store.update_run(
                run_id, 0, simulated_time=instant, paused=True,
                erp_binding={"site": "erp-sim-c1"}, connection=connection,
            )
            store.enqueue_event(event(run_id), 0, connection=connection)
            raise RuntimeError("abort composed write")

    run = store.get_run(run_id)
    assert run["simulated_time"] is None and run["paused"] is False
    assert run["erp_binding"] == {}
    assert store.list_events(run_id, 0) == []

    updated = store.update_run(run_id, 0, simulated_time=instant, paused=True, erp_binding={"site": "erp-sim-c1"})
    assert updated["simulated_time"] == instant.isoformat()
    assert updated["paused"] is True
    assert updated["erp_binding"] == {"site": "erp-sim-c1"}


def test_events_are_hash_checked_ordered_and_completed_idempotently(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_id = uuid4()
    store.create_run(run_id, start())

    first = store.enqueue_event(event(run_id, "evt-a"), 0)
    second = store.enqueue_event(event(run_id, "evt-b"), 0)
    assert [row["sequence"] for row in store.pending_events(run_id, 0)] == [1, 2]
    assert first["scenario_id"] == "C1"
    assert first["entity_type"] == "shipment" and first["entity_id"] == "shipment-1"
    assert first["related_entities"] == ["PO-1042", "case-1"]
    assert store.enqueue_event(event(run_id, "evt-a"), 0) == first
    with pytest.raises(StoreConflict):
        store.enqueue_event(event(run_id, "evt-a", amount=9), 0)

    done = store.complete_event(run_id, 0, "evt-a", {"processed": True})
    assert done["status"] == "completed"
    assert store.complete_event(run_id, 0, "evt-a", {"processed": True}) == done
    with pytest.raises(StoreConflict):
        store.complete_event(run_id, 0, "evt-a", {"processed": False})
    assert [row["event_id"] for row in store.pending_events(run_id, 0)] == ["evt-b"]
    assert second["payload"]["delay_days"] == 2


def test_outbox_references_event_and_keeps_private_payload_out_of_audit(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_id = uuid4()
    store.create_run(run_id, start())
    store.enqueue_event(event(run_id), 0)
    payload = {"to": "supplier@example.test", "body": "Dispatch manifest attached", "document_ref": "manifest-1"}
    outbox = store.enqueue_outbox(run_id, 0, "mail-1", payload, event_id="event-1", actor_id="supplier")
    assert outbox["private_payload"] == payload
    assert store.pending_outbox(run_id, 0) == [outbox]
    assert store.complete_outbox(run_id, 0, "mail-1", {"message_id": "smtp-1"})["status"] == "completed"

    store.append_audit(run_id, 0, {
        "action": "supplier_message_queued", "outbox_id": "mail-1",
        "run_id": str(run_id), "scenario_id": "C1", "case_id": "case-1",
        "event_id": "event-1", "entity_type": "shipment", "entity_id": "shipment-1",
    })
    assert "body" not in str(store.list_audit(run_id))
    audit_record = store.list_audit(run_id)[0]["private_record"]
    assert audit_record["scenario_id"] == "C1" and audit_record["entity_id"] == "shipment-1"
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit SET private_record_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit")


def test_outbox_claim_is_compare_and_set_across_concurrent_workers(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_id = uuid4()
    store.create_run(run_id, start())
    store.enqueue_outbox(run_id, 0, "mail-cas", {"to": "supplier@example.test", "body": "notice"})

    def claim():
        try:
            return store.mark_outbox_submission_started(run_id, 0, "mail-cas")["submission_started"]
        except StoreConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sorted(results) == [False, True]
    with pytest.raises(StoreConflict):
        store.mark_outbox_submission_started(run_id, 0, "mail-cas")


def test_outbox_pre_send_failure_retries_but_crash_after_boundary_is_uncertain(tmp_path):
    db = tmp_path / "simulation.sqlite"
    store = SQLiteStore(db)
    run_id = uuid4()
    store.create_run(run_id, start())
    store.enqueue_outbox(run_id, 0, "mail-pre", {"to": "a", "body": "before send"})
    failed_before_send = store.mark_outbox_failure(run_id, 0, "mail-pre", error_type="smtp_connect")
    assert failed_before_send["status"] == "pending"
    assert failed_before_send["submission_started"] is False
    assert failed_before_send["error_type"] == "smtp_connect"
    assert store.mark_outbox_submission_started(run_id, 0, "mail-pre")["submission_started"] is True
    store.complete_outbox(run_id, 0, "mail-pre", {"provider_receipt": "accepted-before-reopen"})

    store.enqueue_outbox(run_id, 0, "mail-validated", {"to": "c", "body": "not sent"})
    store.mark_outbox_submission_started(run_id, 0, "mail-validated")
    known_pre_send = store.mark_outbox_failure(
        run_id, 0, "mail-validated", error_type="smtp_validation", pre_send=True
    )
    assert known_pre_send["status"] == "pending"
    assert known_pre_send["submission_started"] is False

    store.enqueue_outbox(run_id, 0, "mail-failed-after", {"to": "d", "body": "maybe sent"})
    store.mark_outbox_submission_started(run_id, 0, "mail-failed-after")
    uncertain_failure = store.mark_outbox_failure(
        run_id, 0, "mail-failed-after", error_type="smtp_timeout"
    )
    assert uncertain_failure["status"] == "uncertain"
    assert uncertain_failure["error_type"] == "smtp_timeout"
    with pytest.raises(StoreConflict):
        store.mark_outbox_submission_started(run_id, 0, "mail-failed-after")

    store.enqueue_outbox(run_id, 0, "mail-crash", {"to": "b", "body": "may have sent"})
    store.mark_outbox_submission_started(run_id, 0, "mail-crash")
    reopened = SQLiteStore(db)
    recovered = reopened.pending_outbox(run_id, 0)
    crash_item = next(item for item in recovered if item["outbox_id"] == "mail-crash")
    assert crash_item["status"] == "uncertain"
    assert crash_item["submission_started"] is True
    assert all(item["outbox_id"] != "mail-pre" for item in recovered)
    assert next(item for item in recovered if item["outbox_id"] == "mail-validated")["status"] == "pending"
    with pytest.raises(StoreConflict):
        reopened.mark_outbox_submission_started(run_id, 0, "mail-crash")

    receipt = reopened.complete_outbox(run_id, 0, "mail-crash", {"provider_receipt": "accepted-1"})
    assert receipt["status"] == "completed"
    assert reopened.get_outbox(run_id, 0, "mail-crash")["outcome"] == {"provider_receipt": "accepted-1"}
    assert {item["outbox_id"] for item in reopened.list_outbox(run_id, 0, status="completed")} == {
        "mail-pre", "mail-crash"
    }


def test_old_development_schema_migrates_without_dropping_event_or_outbox_rows(tmp_path):
    db = tmp_path / "legacy.sqlite"
    run_id = str(uuid4())
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, generation INTEGER NOT NULL,
                seed INTEGER NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL,
                simulated_time TEXT, paused INTEGER NOT NULL, manifest_json TEXT NOT NULL,
                erp_binding_json TEXT NOT NULL
            );
            CREATE TABLE events (
                run_id TEXT NOT NULL REFERENCES runs(run_id), generation INTEGER NOT NULL,
                event_id TEXT NOT NULL, sequence INTEGER NOT NULL, simulated_time TEXT NOT NULL,
                event_type TEXT NOT NULL, source TEXT NOT NULL, payload_json TEXT NOT NULL,
                visibility_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','completed')), outcome_json TEXT,
                PRIMARY KEY(run_id,generation,event_id), UNIQUE(run_id,generation,sequence)
            );
            CREATE TABLE outbox (
                run_id TEXT NOT NULL REFERENCES runs(run_id), generation INTEGER NOT NULL,
                outbox_id TEXT NOT NULL, event_id TEXT, actor_id TEXT,
                private_payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','completed')), outcome_json TEXT,
                created_at TEXT NOT NULL, PRIMARY KEY(run_id,generation,outbox_id),
                FOREIGN KEY(run_id,generation,event_id) REFERENCES events(run_id,generation,event_id)
            );
            INSERT INTO runs VALUES('""" + run_id + """','C1',0,1,'agent-a','running',NULL,0,'{}','{}');
            INSERT INTO events VALUES('""" + run_id + """',0,'event-legacy',1,'2026-10-29T00:00:00+00:00',
                'SHIPMENT_DELAYED','scenario','{"delay":2}','["ops"]','old-hash','pending',NULL);
            INSERT INTO outbox VALUES('""" + run_id + """',0,'mail-legacy','event-legacy','supplier',
                '{"body":"preserve"}','old-body-hash','pending',NULL,'2026-10-29T00:00:00+00:00');
        """)

    store = SQLiteStore(db)
    old_event = store.list_events(run_id, 0)[0]
    old_outbox = store.get_outbox(run_id, 0, "mail-legacy")
    assert old_event["event_id"] == "event-legacy"
    assert old_event["payload"] == {"delay": 2}
    assert old_outbox["private_payload"] == {"body": "preserve"}
    assert old_outbox["status"] == "pending"
    assert old_outbox["submission_started"] is False


def test_idempotency_serializes_concurrent_retries_and_rejects_body_reuse(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    calls = []

    def operation(_connection):
        calls.append("run")
        return {"result": "created", "record_id": "r-1"}

    def invoke():
        return store.execute_idempotent("case.update", "key-1", {"case": "c-1", "status": "open"}, operation)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: invoke(), range(8)))
    assert all(response == {"result": "created", "record_id": "r-1"} for response in responses)
    assert calls == ["run"]
    with pytest.raises(IdempotencyConflict):
        store.execute_idempotent("case.update", "key-1", {"case": "c-1", "status": "closed"}, operation)

    run_id = uuid4()
    created = store.execute_idempotent(
        "run.start", "start-1", {"run_id": str(run_id), "scenario_id": "C1"},
        lambda connection: store.create_run(run_id, start(), connection=connection),
    )
    assert store.execute_idempotent(
        "run.start", "start-1", {"run_id": str(run_id), "scenario_id": "C1"},
        lambda connection: store.create_run(run_id, start(), connection=connection),
    ) == created
    assert store.get_run(run_id)["scenario_id"] == "C1"


def test_failed_idempotent_callback_rolls_back_work_and_key_can_retry(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_id = uuid4()
    store.create_run(run_id, start())

    def fails(connection):
        store.put_case(run_id, 0, "case-rollback", {"status": "open"}, connection=connection)
        raise failure

    failure = RuntimeError("database work failed")
    with pytest.raises(RuntimeError, match="database work failed") as raised:
        store.execute_idempotent("case.create", "retryable", {"case": "case-rollback"}, fails)
    assert raised.value is failure
    with pytest.raises(StoreNotFound):
        store.get_case(run_id, 0, "case-rollback")

    result = store.execute_idempotent(
        "case.create", "retryable", {"case": "case-rollback"},
        lambda connection: store.put_case(run_id, 0, "case-rollback", {"status": "open"}, connection=connection),
    )
    assert result["visible"] == {"status": "open"}


def test_reset_is_idempotent_and_retains_old_generation_and_other_runs(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    run_a, run_b = uuid4(), uuid4()
    store.create_run(run_a, start(), manifest={"initial": "a"})
    store.create_run(run_b, start("D1"), manifest={"initial": "b"})
    store.put_case(run_a, 0, "case-a", {"status": "in_review", "notes": "old trajectory"})
    store.put_actor(run_a, 0, "supplier-a", kind="supplier", email=None, private_state={"dispatch": 10_000})
    store.enqueue_event(event(run_a), 0)
    store.append_audit(run_a, 0, {"action": "investigated"})
    store.put_case(run_b, 0, "case-b", {"status": "open"})

    snapshot = {
        "run": {"manifest": {"initial": "a"}, "erp_binding": {"site": "erp-sim-a"}},
        "cases": [{"case_id": "case-a", "visible": {"status": "open", "notes": ""}}],
        "actors": [{"actor_id": "supplier-a", "kind": "supplier", "private_state": {"dispatch": 10_000}}],
    }
    result = store.reset_run(run_a, 0, snapshot, idempotency_scope="reset", idempotency_key="reset-a")
    repeated = store.reset_run(run_a, 0, snapshot, idempotency_scope="reset", idempotency_key="reset-a")
    assert result == repeated == {"run_id": str(run_a), "generation": 1, "status": "running"}
    assert store.get_run(run_a)["generation"] == 1
    assert store.get_case(run_a, 0, "case-a")["visible"]["status"] == "in_review"
    assert store.get_case(run_a, 1, "case-a")["visible"] == {"status": "open", "notes": ""}
    assert store.get_actor(run_a, 1, "supplier-a")["private_state"]["dispatch"] == 10_000
    assert store.get_run(run_b)["generation"] == 0
    assert store.get_case(run_b, 0, "case-b")["visible"]["status"] == "open"
    assert [row["event_id"] for row in store.list_events(run_a, 0)] == ["event-1"]
    assert [record["private_record"]["action"] for record in store.list_audit(run_a, 0)] == ["investigated"]
    assert store.list_audit(run_a, 1)[0]["private_record"]["event"] == "run_reset"
