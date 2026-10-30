from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.simulation.company_feedback_runtime import CompanyFeedbackRuntime
from servers.simulation.feedback_contracts import NativeActionReceipt
from servers.simulation.store import SQLiteStore


def test_unmapped_native_thread_reply_uses_durable_native_action_without_erp_mapping(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    with store.transaction() as db:
        db.execute("""CREATE TABLE company_native_message_outbox(
            source_event_id TEXT PRIMARY KEY, mapping_id TEXT, message_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, private_payload_json TEXT NOT NULL,
            status TEXT NOT NULL, submission_started INTEGER NOT NULL DEFAULT 0,
            error_type TEXT, receipt_json TEXT, updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE company_coffee_mail_cursors(
            mapping_id TEXT NOT NULL, mailbox_actor_id TEXT NOT NULL,
            uidvalidity TEXT NOT NULL, after_uid INTEGER NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(mapping_id,mailbox_actor_id))""")
        db.execute("""CREATE TABLE company_coffee_thread_actions(
            action_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL,
            incoming_message_id TEXT NOT NULL, thread_key TEXT NOT NULL,
            native_actor_id TEXT NOT NULL, native_recipient_id TEXT NOT NULL,
            native_refs_json TEXT NOT NULL, company_mail_actor_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, action_json TEXT NOT NULL,
            mail_receipt_json TEXT NOT NULL, status TEXT NOT NULL,
            receipt_json TEXT, error_type TEXT, updated_at TEXT NOT NULL,
            UNIQUE(source_event_id,incoming_message_id,native_recipient_id))""")
        db.execute("""CREATE TABLE company_feedback_runtime_reports(
            provider TEXT PRIMARY KEY, result_json TEXT NOT NULL)""")
        db.execute("CREATE TABLE company_feedback_actions(action_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE company_evidence(evidence_id TEXT PRIMARY KEY)")
        db.execute("""INSERT INTO company_native_message_outbox(
            source_event_id,mapping_id,message_id,request_hash,private_payload_json,status,receipt_json,updated_at)
            VALUES(?,?,?,?,?,'delivered','{}','now')""", (
            "source-event-1", None, "<delivered-native-root@example.test>", "hash",
            json.dumps({
                "native_source_message": {
                    "native_message_id": "native-msg-1", "sender_id": "supplier_A",
                    "recipient_id": "roaster_A", "ref_listing_id": "listing-9",
                },
            }),
        ))

    profile = {
        "employees": [{"email": "employee@example.test"}],
        "company": {"assistantEmail": "agent@agent.example.test", "background": {"coffee": {
            "company_business_ids": ["roaster_A"],
            "mail_sender_actor_id": "employee_mail",
            "counterparties": {"supplier_A": {
                "actor_id": "supplier_mail", "email": "supplier@example.test",
            }},
        }}},
    }
    profile_path = tmp_path / "company.json"
    profile_path.write_text(json.dumps(profile))
    actors = [
        {"actor_id": "employee_mail", "email": "employee@example.test", "kind": "employee"},
        {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "agent"},
        {"actor_id": "supplier_mail", "email": "supplier@example.test", "kind": "counterparty"},
    ]
    config = SimpleNamespace(mail={"actors": actors}, company_profile_file=profile_path)
    reply = {
        "message_id": "<reply-1@example.test>", "in_reply_to": "<delivered-native-root@example.test>",
        "references": "<delivered-native-root@example.test>",
        "sender_actor_id": "viv", "authenticated_sender_actor_id": "viv",
        "recipient_actor_id": "supplier_mail", "subject": "Re: shipment",
        "body": "Please confirm the delivery date.", "received_at": "2026-10-30T12:00:00+00:00",
        "mailbox_uid": "7", "uidvalidity": "42", "reference": "INBOX UID 7",
    }

    class Gateway:
        def __init__(self):
            self.calls = 0

        def poll_registered_replies(self, mailbox, roots, after_uid, senders, **kwargs):
            assert mailbox["actor_id"] == "supplier_mail"
            assert roots == {"<delivered-native-root@example.test>"}
            assert senders == {"employee_mail", "viv"}
            assert kwargs["require_authenticated_submission"] is True
            self.calls += 1
            return [reply] if self.calls <= 2 else []

    class Adapter:
        def __init__(self):
            self.actions = []
            self.saved = {}

        def reconcile(self, action):
            return self.saved.get(action["action_id"])

        def apply(self, action):
            self.actions.append(action)
            result = NativeActionReceipt(
                action_id=action["action_id"], accepted=True, reconciled=True,
                native_state_ref="coffee-checkpoint:abc", outcome={"native_action": "send_message"},
            )
            self.saved[action["action_id"]] = result
            return result

    adapter = Adapter()
    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = store
    runtime.config = config
    runtime.pipeline = SimpleNamespace(adapters={"coffeebench": adapter})
    runtime.company_time = lambda: datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc)
    gateway = Gateway()

    runtime._observe_unmapped_coffee_conversations(gateway, profile, {actor["actor_id"]: actor for actor in actors})
    runtime._observe_unmapped_coffee_conversations(gateway, profile, {actor["actor_id"]: actor for actor in actors})

    assert len(adapter.actions) == 1
    action = adapter.actions[0]
    assert action["native_action"] == "send_message"
    assert action["actor_id"] == "roaster_A"
    assert action["arguments"] == {
        "recipient": "supplier_A", "title": "Re: shipment",
        "body": "Please confirm the delivery date.", "ref_listing_id": "listing-9",
    }
    with store.transaction() as db:
        receipt = db.execute("SELECT * FROM company_coffee_thread_actions").fetchone()
        cursor = db.execute("SELECT after_uid FROM company_coffee_mail_cursors").fetchone()
        assert db.execute("SELECT COUNT(*) FROM company_feedback_actions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM company_evidence").fetchone()[0] == 0
    assert receipt["status"] == "applied"
    assert receipt["incoming_message_id"] == "<reply-1@example.test>"
    assert cursor["after_uid"] == 7


def test_unacknowledged_native_mail_outbox_retries_after_source_tick(tmp_path):
    store = SQLiteStore(tmp_path / "retry.sqlite")
    with store.transaction() as db:
        db.execute("""CREATE TABLE company_native_message_outbox(
            source_event_id TEXT PRIMARY KEY, mapping_id TEXT, message_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, private_payload_json TEXT NOT NULL,
            status TEXT NOT NULL, submission_started INTEGER NOT NULL DEFAULT 0,
            error_type TEXT, receipt_json TEXT, updated_at TEXT NOT NULL)""")
        db.execute("CREATE TABLE company_feedback_runtime_reports(provider TEXT PRIMARY KEY,result_json TEXT NOT NULL)")
        db.execute("""INSERT INTO company_native_message_outbox(
            source_event_id,mapping_id,message_id,request_hash,private_payload_json,status,updated_at)
            VALUES('event-acknowledged',NULL,'<root@test>','hash',?,'uncertain','now')""", (
                json.dumps({"native_source_message": {"native_message_id": "native-id"},
                            "mapping_status": "unmapped_native_message_source"}),
            ))

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = store
    runtime.pipeline = SimpleNamespace(mappings=lambda: [])
    seen = []

    runtime._coffee_native_message_plan_from_outbox = lambda row: {
        "source_id": row["source_event_id"], "mapping_id": None, "message_id": row["message_id"],
        "mapping_status": "unmapped_native_message_source",
        "native_message": {"native_message_id": "native-id"},
    }

    def replay(_event, plan):
        seen.append(plan["source_id"])
        with store.transaction() as db:
            db.execute("UPDATE company_native_message_outbox SET status='delivered' WHERE source_event_id=?",
                       (plan["source_id"],))

    runtime._deliver_coffee_native_message = replay
    result = runtime.deliver_native_messages([])

    assert seen == ["event-acknowledged"]
    assert result["status"] == "partial"
    with store.transaction() as db:
        assert db.execute("SELECT status FROM company_native_message_outbox").fetchone()[0] == "delivered"
        report = json.loads(db.execute(
            "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='component_coffee_native_mail'"
        ).fetchone()[0])
    assert report["mapping_gaps"] == [{
        "source_event_id": "event-acknowledged",
        "native_message_id": "native-id",
        "error_code": "unmapped_native_message_source",
    }]

    # A corrupt pending payload must be reported without reparsing it in the
    # error handler and losing both the original error and saved delivery gaps.
    with store.transaction() as db:
        db.execute("""INSERT INTO company_native_message_outbox(
            source_event_id,mapping_id,message_id,request_hash,private_payload_json,status,updated_at)
            VALUES('corrupt-pending',NULL,'<corrupt@test>','hash','not-json','uncertain','now')""")
    runtime._coffee_native_message_plan_from_outbox = (
        CompanyFeedbackRuntime._coffee_native_message_plan_from_outbox.__get__(runtime)
    )
    result = runtime.deliver_native_messages([])
    assert result["failures"] == [{
        "source_event_id": "corrupt-pending", "native_message_id": "unknown",
        "error_type": "JSONDecodeError",
    }]
    assert seen == ["event-acknowledged"]
    with store.transaction() as db:
        saved = json.loads(db.execute(
            "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='component_coffee_native_mail'"
        ).fetchone()[0])
    assert saved["failures"] == result["failures"]
    assert saved["mapping_gaps"] == report["mapping_gaps"]


def test_coffee_mail_outbox_distinguishes_pre_submission_failure_from_uncertain_smtp(tmp_path):
    store = SQLiteStore(tmp_path / "smtp.sqlite")
    with store.transaction() as db:
        db.execute("""CREATE TABLE company_native_message_outbox(
            source_event_id TEXT PRIMARY KEY, mapping_id TEXT, message_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, private_payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','submitting','uncertain','delivered')),
            submission_started INTEGER NOT NULL DEFAULT 0 CHECK(submission_started IN (0,1)),
            error_type TEXT, receipt_json TEXT, updated_at TEXT NOT NULL)""")

    class Gateway:
        def __init__(self, claim_first):
            self.claim_first = claim_first

        def send(self, *_args, on_submission_started=None, **_kwargs):
            if self.claim_first:
                on_submission_started()
            raise RuntimeError("synthetic transport boundary failure")

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = store
    plan = {
        "source_id": "before-submit", "mapping_id": None, "message_id": "<before@test>",
        "sender": {"actor_id": "supplier_mail", "email": "supplier@example.test"},
        "recipient": {"actor_id": "employee_mail", "email": "employee@example.test"},
        "native_message": {"native_message_id": "native-1", "sender_id": "supplier_A",
                           "recipient_id": "roaster_A", "title": "Subject", "body": "Body"},
        "in_reply_to": None, "run_id": "company-coffeebench-native", "generation": 0,
        "occurred_at": "2026-10-30T12:00:00+00:00", "erp_doctype": None, "erp_name": None,
        "mapping_status": "unmapped_native_message_source",
    }

    for source_id, claim_first, expected_status, expected_started in (
        ("before-submit", False, "pending", 0),
        ("after-submit", True, "uncertain", 1),
    ):
        runtime._mail_gateway = Gateway(claim_first)
        with pytest.raises(RuntimeError):
            runtime._deliver_coffee_native_message(None, {**plan, "source_id": source_id,
                                                           "message_id": f"<{source_id}@test>"})

    with store.transaction() as db:
        rows = {row["source_event_id"]: dict(row) for row in db.execute(
            "SELECT * FROM company_native_message_outbox"
        )}
    assert (rows["before-submit"]["status"], rows["before-submit"]["submission_started"]) == ("pending", 0)
    assert (rows["after-submit"]["status"], rows["after-submit"]["submission_started"]) == ("uncertain", 1)


def test_persisted_coffee_source_event_retries_after_pre_outbox_failure(tmp_path):
    store = SQLiteStore(tmp_path / "source-retry.sqlite")
    with store.transaction() as db:
        db.execute("""CREATE TABLE company_native_message_outbox(
            source_event_id TEXT PRIMARY KEY, mapping_id TEXT, message_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, private_payload_json TEXT NOT NULL,
            status TEXT NOT NULL, submission_started INTEGER NOT NULL DEFAULT 0,
            error_type TEXT, receipt_json TEXT, updated_at TEXT NOT NULL)""")
        db.execute("CREATE TABLE company_feedback_runtime_reports(provider TEXT PRIMARY KEY,result_json TEXT NOT NULL)")
    store.record_company_source_events([{
        "event_id": "persisted-native-event", "event_type": "COFFEE_NATIVE_MESSAGE_RECEIVED",
        "simulated_time": "2026-10-30T12:00:00+00:00",
        "source": "sakanaai-coffeebench-native-economy",
        "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
        "entity_type": "marketplace_message", "entity_id": "native-message-1",
        "payload": {"native_message_id": "native-message-1"}, "visibility": [],
    }])

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = store
    runtime.pipeline = SimpleNamespace(mappings=lambda: [])
    plan = {
        "source_id": "persisted-native-event", "mapping_id": "mapping-1", "message_id": "<native-root@test>",
        "mapping_status": "mapped", "native_message": {"native_message_id": "native-message-1"},
    }
    runtime._coffee_native_message_plan = lambda event: plan
    calls = []

    def deliver(_event, replay_plan):
        calls.append(replay_plan["source_id"])
        if len(calls) == 1:
            raise OSError("synthetic pre-outbox failure")
        with store.transaction() as db:
            db.execute("""INSERT INTO company_native_message_outbox(
                source_event_id,mapping_id,message_id,request_hash,private_payload_json,status,updated_at)
                VALUES(?,?,?,?,?,'delivered','now')""", (
                    replay_plan["source_id"], replay_plan["mapping_id"], replay_plan["message_id"],
                    "request-hash", json.dumps({"native_source_message": replay_plan["native_message"]}),
                ))

    runtime._deliver_coffee_native_message = deliver
    first = runtime.deliver_native_messages([])
    assert first["status"] == "partial"
    assert calls == ["persisted-native-event"]
    second = runtime.deliver_native_messages([])
    assert second["status"] == "ok"
    assert calls == ["persisted-native-event", "persisted-native-event"]
    with store.transaction() as db:
        assert db.execute("SELECT status FROM company_native_message_outbox").fetchone()[0] == "delivered"
        assert db.execute(
            "SELECT 1 FROM company_feedback_runtime_reports WHERE provider='component_coffee_native_mail'"
        ).fetchone() is None
