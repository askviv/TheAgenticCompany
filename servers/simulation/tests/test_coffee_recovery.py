from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

import pytest

from servers.simulation.background_worker import (
    BackgroundOperationUncertain,
    BackgroundWorker,
    BackgroundProviderFailed,
    CoffeeRecoveryRequest,
)
from servers.simulation.background_runtime import BackgroundWorkerError, CompanyBackgroundRuntime
from servers.simulation.integration_contracts import BackgroundAdvanceRequest, BackgroundInitializeRequest
from servers.simulation.store import SQLiteStore, StoreConflict
from servers.simulation.app import create_app
from fastapi.testclient import TestClient
from servers.simulation.company_feedback import CompanyFeedback
from servers.simulation.feedback_contracts import NativeActionReceipt, TranslatedAction, TranslationResult


COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"


def _worker_with_confirmed_checkpoint(tmp_path):
    worker = BackgroundWorker(
        tmp_path / "worker.sqlite",
        checkpoint_directory=tmp_path / "private-checkpoints",
        coffee_model_factory=lambda _actor, model: type("NoCallModel", (), {"model": model})(),
        configured_model_id="gpt-6-luna",
    )
    root = worker.checkpoint_directory / "company-coffeebench"
    root.mkdir(parents=True, exist_ok=True)
    checkpoint_file = root / "confirmed.pickle"
    checkpoint_bytes = b"controlled confirmed checkpoint fixture"
    checkpoint_file.write_bytes(checkpoint_bytes)
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    snapshot = {
        "seed": 7,
        "started_at": start.isoformat(),
        "cursor": start.isoformat(),
        "status": "active",
        "company_business_ids": ["roaster_A"],
        "binding": {"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna"},
        "checkpoint": {
            "codec": "python-pickle-v1",
            "path": str(checkpoint_file),
            "sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
        },
    }
    initialize_body = {
        "provider_id": "coffeebench", "company_id": "company", "operation_id": "initialize",
        "seed": 7, "started_at": start.isoformat(), "binding": snapshot["binding"],
        "actor_private_states": {},
    }
    worker.store.claim_background_operation("coffeebench", "initialize", initialize_body)
    worker.store.complete_background_operation(
        "coffeebench", "initialize", initialize_body,
        upstream_commit=COMMIT, seed=7, started_at=start, cursor=start,
        snapshot=snapshot, result={"provider_id": "coffeebench", "cursor": start.isoformat()}, intents=[],
    )
    old_request = BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="uncertain-old-operation",
        until=start + timedelta(minutes=20),
        binding={"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna"},
    )
    old_body = old_request.model_dump(mode="json")
    worker.store.claim_background_operation("coffeebench", old_request.operation_id, old_body)
    _, turn_id = worker.coffee_model_responses.load_or_claim(
        "old-native-query", "old-query-hash", old_request.operation_id,
    )
    worker.coffee_model_responses.mark_submitted(turn_id, "old-query-hash")
    return worker, old_request, snapshot, checkpoint_file


def _recovery_request(old_request, operation_id="recovery-attempt-1"):
    return old_request.model_copy(update={"operation_id": operation_id})


def _advance_fixture(calls, worker, recovery_id, *, fail_first=False, uncertain=False):
    async def advance(snapshot, until, _binding):
        calls.append(until)
        if fail_first and len(calls) == 1:
            _, turn_id = worker.coffee_model_responses.load_or_claim(
                "recovery-native-query", "recovery-query-hash", recovery_id,
            )
            if uncertain:
                worker.coffee_model_responses.mark_submitted(turn_id, "recovery-query-hash")
            raise RuntimeError("synthetic interruption after recovery reservation")
        updated = deepcopy(snapshot)
        updated["cursor"] = until.isoformat()
        return {"snapshot": updated, "events": [], "outcomes": [], "erp_intents": []}

    return advance


def test_recovery_reservation_and_worker_completion_replay_from_confirmed_checkpoint(tmp_path, monkeypatch):
    worker, previous, base_snapshot, checkpoint_file = _worker_with_confirmed_checkpoint(tmp_path)
    inspection = asyncio.run(worker.inspect_coffee_recovery(previous))
    recovery = _recovery_request(previous)
    calls = []
    monkeypatch.setattr(
        "servers.simulation.background_worker.advance_coffee",
        _advance_fixture(calls, worker, recovery.operation_id),
    )
    request = CoffeeRecoveryRequest(
        previous_request=previous, recovery_request=recovery,
        confirmed_cursor=inspection.confirmed_cursor,
        checkpoint_sha256=inspection.checkpoint_sha256,
    )

    first = asyncio.run(worker.recover_coffee_advance(request))
    replay = asyncio.run(worker.recover_coffee_advance(request))
    assert first == replay
    assert calls == [previous.until]
    assert worker.store.get_background_provider_state("coffeebench")["snapshot"]["cursor"] == previous.until.isoformat()
    old_operation = worker.store.get_background_operation(
        "coffeebench", previous.operation_id, previous.model_dump(mode="json"),
    )
    assert old_operation["status"] == "started"  # old claim and turn provenance are retained
    resolution = worker.store.get_background_recovery_resolution(previous.operation_id)
    assert resolution["recovery_operation_id"] == recovery.operation_id
    assert resolution["previous_request"] == previous.model_dump(mode="json")
    with pytest.raises(StoreConflict, match="superseded"):
        worker.store.claim_background_operation(
            "coffeebench", previous.operation_id, previous.model_dump(mode="json"),
        )
    assert checkpoint_file.exists() and base_snapshot["cursor"] != previous.until.isoformat()


def test_interrupted_prepared_recovery_is_resumable_but_submitted_recovery_is_uncertain(tmp_path, monkeypatch):
    worker, previous, _snapshot, _checkpoint = _worker_with_confirmed_checkpoint(tmp_path)
    inspection = asyncio.run(worker.inspect_coffee_recovery(previous))
    recovery = _recovery_request(previous)
    request = CoffeeRecoveryRequest(
        previous_request=previous, recovery_request=recovery,
        confirmed_cursor=inspection.confirmed_cursor,
        checkpoint_sha256=inspection.checkpoint_sha256,
    )
    calls = []
    monkeypatch.setattr(
        "servers.simulation.background_worker.advance_coffee",
        _advance_fixture(calls, worker, recovery.operation_id, fail_first=True),
    )
    with pytest.raises(BackgroundProviderFailed):
        asyncio.run(worker.recover_coffee_advance(request))
    result = asyncio.run(worker.recover_coffee_advance(request))
    assert result.cursor == previous.until
    assert len(calls) == 2

    worker2, previous2, _snapshot2, _checkpoint2 = _worker_with_confirmed_checkpoint(tmp_path / "submitted")
    inspection2 = asyncio.run(worker2.inspect_coffee_recovery(previous2))
    recovery2 = _recovery_request(previous2)
    request2 = CoffeeRecoveryRequest(
        previous_request=previous2, recovery_request=recovery2,
        confirmed_cursor=inspection2.confirmed_cursor,
        checkpoint_sha256=inspection2.checkpoint_sha256,
    )
    submitted_calls = []
    monkeypatch.setattr(
        "servers.simulation.background_worker.advance_coffee",
        _advance_fixture(submitted_calls, worker2, recovery2.operation_id, fail_first=True, uncertain=True),
    )
    with pytest.raises(BackgroundProviderFailed):
        asyncio.run(worker2.recover_coffee_advance(request2))
    with pytest.raises(BackgroundOperationUncertain):
        asyncio.run(worker2.recover_coffee_advance(request2))
    assert len(submitted_calls) == 1


def test_checkpoint_change_after_inspection_prevents_recovery_and_native_advance(tmp_path, monkeypatch):
    worker, previous, _snapshot, checkpoint_file = _worker_with_confirmed_checkpoint(tmp_path)
    inspection = asyncio.run(worker.inspect_coffee_recovery(previous))
    recovery = _recovery_request(previous)
    request = CoffeeRecoveryRequest(
        previous_request=previous, recovery_request=recovery,
        confirmed_cursor=inspection.confirmed_cursor,
        checkpoint_sha256=inspection.checkpoint_sha256,
    )
    checkpoint_file.write_bytes(b"changed after read-only inspection")
    called = []

    async def advance(*args):
        called.append(args)
        raise AssertionError("recovery must stop before native advance")

    monkeypatch.setattr("servers.simulation.background_worker.advance_coffee", advance)
    with pytest.raises(StoreConflict, match="digest"):
        asyncio.run(worker.recover_coffee_advance(request))
    assert not called
    assert worker.store.get_background_recovery_resolution(previous.operation_id) is None


def test_coordinator_archival_is_atomic_append_only_and_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "coordinator.sqlite")
    previous = {
        "provider_id": "coffeebench", "company_id": "company", "operation_id": "old",
        "until": "2026-10-30T00:20:00+00:00", "binding": {"model_id": "m", "company_business_ids": ["roaster_A"]},
    }
    recovery = {**previous, "operation_id": "recovery-1"}
    store.persist_background_provider_pending("coffeebench", "old", previous)
    archived = store.prepare_background_provider_recovery(
        "old", "recovery-1", recovery, confirmed_cursor="2026-10-30T00:00:00+00:00",
        checkpoint_sha256="a" * 64,
    )
    reopened = SQLiteStore(tmp_path / "coordinator.sqlite")
    repeated = reopened.prepare_background_provider_recovery(
        "old", "recovery-1", recovery, confirmed_cursor="2026-10-30T00:00:00+00:00",
        checkpoint_sha256="a" * 64,
    )
    assert repeated == archived
    assert reopened.get_background_provider_pending("coffeebench")["operation_id"] == "recovery-1"
    assert reopened.get_background_provider_recovery("old")["previous_request"] == previous
    with pytest.raises(Exception, match="append-only"):
        with reopened.transaction() as db:
            db.execute("DELETE FROM background_provider_recovery_archive")


def test_coordinator_restart_resumes_archived_attempt_with_same_id_and_exact_request(tmp_path):
    store = SQLiteStore(tmp_path / "coordinator-restart.sqlite")
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    previous = BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="old", until=start + timedelta(minutes=20),
        binding={"model_id": "gpt-6-luna", "company_business_ids": ["roaster_A"]},
    )
    store.persist_background_provider_pending(
        "coffeebench", previous.operation_id, previous.model_dump(mode="json"),
    )

    class Worker:
        def __init__(self):
            self.inspections = 0
            self.recoveries = []
            self.fail_once = True

        def inspect_coffee_recovery(self, request):
            self.inspections += 1
            assert request == previous
            return {"status": "started", "confirmed_cursor": start.isoformat(), "checkpoint_sha256": "b" * 64}

        def recover_coffee_advance(self, old, recovery, *, confirmed_cursor, checkpoint_sha256):
            self.recoveries.append((old, recovery, confirmed_cursor, checkpoint_sha256))
            if self.fail_once:
                self.fail_once = False
                raise BackgroundWorkerError(202, "coffee_recovery_uncertain")
            return {"provider_id": "coffeebench",
                    "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
                    "cursor": recovery.until.isoformat(), "next_due": None, "events": [],
                    "outcomes": [], "erp_intents": []}

    worker = Worker()
    runtime = CompanyBackgroundRuntime(store, worker)
    first = runtime.recover_pending_coffeebench("old", "operator-recovery-key")
    assert first["status"] == "uncertain"
    archived = store.get_background_provider_recovery("old")
    assert archived is not None
    assert store.get_background_provider_pending("coffeebench")["operation_id"] == archived["recovery_operation_id"]
    assert archived["previous_request"] == previous.model_dump(mode="json")

    # Recreate both runtime/store objects after the coordinator-side commit.
    reopened = SQLiteStore(tmp_path / "coordinator-restart.sqlite")
    resumed = CompanyBackgroundRuntime(reopened, worker)
    result = resumed.recover_pending_coffeebench("old", "operator-recovery-key")
    assert result["status"] == "completed"
    assert result["operation_id"] == archived["recovery_operation_id"]
    assert worker.inspections == 1
    assert len(worker.recoveries) == 2
    assert worker.recoveries[0] == worker.recoveries[1]
    pending = reopened.get_background_provider_pending("coffeebench")
    assert pending["result"]["cursor"] == previous.until.isoformat()


def test_recovery_controller_operation_requires_controller_and_idempotency_key():
    calls = []
    engine = SimpleNamespace(
        company_background_recover=lambda operation_id, key: calls.append((operation_id, key)) or {
            "status": "completed", "operation_id": "safe-recovery-id", "cursor": "2030-01-01T00:00:00+00:00",
        },
    )
    client = TestClient(create_app(engine, {
        "controller": {"actor_id": "operator", "role": "controller"},
        "agent": {"actor_id": "employee", "role": "agent"},
    }))
    headers = {"Authorization": "Bearer controller", "Idempotency-Key": "recovery-key"}
    response = client.post(
        "/company/background/coffeebench/recover",
        json={"previous_operation_id": "uncertain-operation"}, headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["operation_id"] == "safe-recovery-id"
    assert calls == [("uncertain-operation", "recovery-key")]
    denied = client.post(
        "/company/background/coffeebench/recover",
        json={"previous_operation_id": "uncertain-operation"},
        headers={"Authorization": "Bearer agent", "Idempotency-Key": "agent-key"},
    )
    assert denied.status_code == 403
    missing_key = client.post(
        "/company/background/coffeebench/recover",
        json={"previous_operation_id": "uncertain-operation"},
        headers={"Authorization": "Bearer controller"},
    )
    assert missing_key.status_code == 400
    assert len(calls) == 1


def test_queued_native_action_remains_pending_until_shared_tick_receipt(tmp_path):
    store = SQLiteStore(tmp_path / "feedback.sqlite")
    action = TranslatedAction(
        action_id="queued-action", mapping_id="mapping", evidence_id="evidence",
        benchmark="coffeebench", native_action="send_message", actor_id="roaster_A",
        company_time=datetime(2026, 10, 30, tzinfo=timezone.utc),
        arguments={"recipient_id": "farmer_A", "title": "Re: Request", "body": "Confirmed."},
    )

    class Adapter:
        applied = False

        def reconcile(self, _action):
            if not self.applied:
                return None
            return NativeActionReceipt(
                action_id=action.action_id, accepted=True, reconciled=True,
                native_state_ref="confirmed-native-state", outcome={"status": "applied"},
            )

        def apply(self, _action):
            return NativeActionReceipt(
                action_id=action.action_id, accepted=True, reconciled=False,
                native_state_ref="queued-native-state", outcome={"status": "queued"},
            )

    adapter = Adapter()
    feedback = CompanyFeedback(store, {"coffeebench": adapter})
    translation = TranslationResult(
        status="supported", action=action, reason="native action is supported",
    )
    with store.transaction() as db:
        db.execute(
            "INSERT INTO company_feedback_actions(action_id,mapping_id,evidence_id,stage,translation_json) "
            "VALUES(?,?,?,'pending',?)",
            (action.action_id, action.mapping_id, action.evidence_id, translation.model_dump_json()),
        )
    feedback.reconcile()
    with store.transaction() as db:
        row = db.execute(
            "SELECT stage,receipt_json FROM company_feedback_actions WHERE action_id=?",
            (action.action_id,),
        ).fetchone()
    assert row["stage"] == "pending"
    assert '"status":"queued"' in row["receipt_json"]

    adapter.applied = True  # the next shared Coffee tick saved the native receipt
    feedback.reconcile()
    with store.transaction() as db:
        stage = db.execute(
            "SELECT stage FROM company_feedback_actions WHERE action_id=?", (action.action_id,),
        ).fetchone()[0]
    assert stage == "reconciled"
