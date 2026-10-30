"""Durable registry for employee-facing external-provider work."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
import re
from typing import Any
from urllib.parse import quote

from .store import SQLiteStore, StoreConflict


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class ProviderPortalStore:
    """Keep case routing and message receipts in the existing company SQLite DB."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.lock_directory = store.db_path.parent / "provider-case-locks"
        self.lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_cases (
                case_id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL UNIQUE,
                responsible_actor_id TEXT NOT NULL,
                responsible_email TEXT NOT NULL,
                company_account_reference TEXT NOT NULL,
                issue_reference TEXT NOT NULL,
                subject TEXT NOT NULL,
                business_description TEXT NOT NULL,
                requested_outcome TEXT NOT NULL,
                private_factual_context_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(provider_id, issue_reference)
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(company_provider_cases)")}
            if "private_factual_context_json" not in columns:
                db.execute("ALTER TABLE company_provider_cases ADD COLUMN private_factual_context_json TEXT NOT NULL DEFAULT '[]'")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_messages (
                case_id TEXT NOT NULL REFERENCES company_provider_cases(case_id),
                client_message_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                native_operation_id TEXT NOT NULL UNIQUE,
                message_id TEXT NOT NULL UNIQUE,
                reply_message_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('started','outbound_submitting','outbound_uncertain','outbound_sent','provider_uncertain','provider_completed','reply_submitting','reply_uncertain','sent')),
                body TEXT NOT NULL,
                reply_body TEXT,
                native_response_json TEXT,
                outbound_receipt_json TEXT,
                send_receipt_json TEXT,
                error_type TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(case_id, client_message_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_portal_sessions (
                session_hash TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_case_notifications (
                case_id TEXT PRIMARY KEY REFERENCES company_provider_cases(case_id),
                message_id TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','submitting','uncertain','sent')),
                receipt_json TEXT,
                error_type TEXT,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_registered_emails (
                provider_actor_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                case_id TEXT NOT NULL REFERENCES company_provider_cases(case_id),
                uidvalidity INTEGER NOT NULL,
                mailbox_uid INTEGER NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('started','processed')),
                response_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(provider_actor_id, message_id),
                UNIQUE(provider_actor_id, uidvalidity, mailbox_uid)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_mail_cursors (
                provider_actor_id TEXT NOT NULL,
                case_id TEXT NOT NULL REFERENCES company_provider_cases(case_id),
                uidvalidity INTEGER NOT NULL,
                after_uid INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider_actor_id,case_id)
            )""")

    @contextmanager
    def locked_case(self, case_id: str):
        key = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        path = self.lock_directory / (key + ".lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def register_case(self, value: dict[str, Any]) -> dict[str, Any]:
        required = (
            "case_id", "provider_id", "task_id", "conversation_id",
            "responsible_actor_id", "responsible_email", "company_account_reference",
            "issue_reference", "subject", "business_description", "requested_outcome",
            "created_at",
        )
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ValueError("provider case registration is incomplete")
        if value["provider_id"] not in {
            "tau2-airline", "tau2-telecom", "tau2-banking"
        }:
            raise ValueError("unsupported external provider")
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT * FROM company_provider_cases WHERE case_id=?", (value["case_id"],)
            ).fetchone()
            encoded = _json({**{key: value[key] for key in required},
                             "private_factual_context": value.get("private_factual_context", [])})
            if prior:
                old = {**{key: prior[key] for key in required},
                       "private_factual_context": json.loads(prior["private_factual_context_json"])}
                if _json(old) != encoded:
                    raise StoreConflict("provider case identity or source context changed")
                result = dict(prior)
                result["private_factual_context"] = json.loads(result.pop("private_factual_context_json", "[]"))
                return result
            db.execute(
                "INSERT INTO company_provider_cases(case_id,provider_id,task_id,conversation_id,responsible_actor_id,"
                "responsible_email,company_account_reference,issue_reference,subject,business_description,"
                "requested_outcome,created_at,private_factual_context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*[value[key] for key in required], _json(value.get("private_factual_context", []))),
            )
            row = db.execute(
                "SELECT * FROM company_provider_cases WHERE case_id=?", (value["case_id"],)
            ).fetchone()
        result = dict(row)
        result["private_factual_context"] = json.loads(result.pop("private_factual_context_json", "[]"))
        return result

    def case(self, case_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM company_provider_cases WHERE case_id=?", (case_id,)
            ).fetchone()
        if row:
            value = dict(row)
            value["private_factual_context"] = json.loads(value.pop("private_factual_context_json", "[]"))
            return value
        return None

    def case_for_notification_message_id(self, message_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT c.* FROM company_provider_cases c JOIN company_provider_case_notifications n "
                "ON n.case_id=c.case_id WHERE n.message_id=? AND n.status='sent'", (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def registered_email_cases(self) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT c.case_id,c.provider_id,c.responsible_actor_id,n.message_id "
                "FROM company_provider_cases c JOIN company_provider_case_notifications n "
                "ON n.case_id=c.case_id WHERE n.status='sent' ORDER BY c.created_at,c.case_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def company_case_activity(self, case_id: str, portal_public_url: str) -> dict[str, Any] | None:
        """Return employee-visible case mail history without native/task internals."""
        case = self.case(case_id)
        if case is None:
            return None
        notification = self.notification(case_id)
        activities: list[dict[str, Any]] = []
        if notification is not None:
            body = (
                case["business_description"] + "\n\nRequested outcome: "
                + case["requested_outcome"]
                + "\n\nOpen this case in the Company Provider Portal: "
                + portal_public_url.rstrip("/") + "/?case=" + quote(case_id, safe="")
            )
            receipt = json.loads(notification["receipt_json"]) if notification.get("receipt_json") else {}
            received_at = next((receipt[key] for key in ("delivered_at", "accepted_at", "received_at")
                                if isinstance(receipt.get(key), str)), None)
            activities.append({
                "message_id": notification["message_id"], "status": notification["status"],
                "body": body, "created_at": notification["updated_at"],
                "direction": "outbound", "received_at": received_at,
            })
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT message_id,reply_message_id,status,body,reply_body,created_at,"
                "outbound_receipt_json,send_receipt_json FROM company_provider_messages "
                "WHERE case_id=? ORDER BY created_at,client_message_id", (case_id,),
            ).fetchall()
        for row in rows:
            inbound_receipt = json.loads(row["outbound_receipt_json"]) if row["outbound_receipt_json"] else {}
            registered_email = isinstance(inbound_receipt, dict) and "mailbox_uid" in inbound_receipt
            request_direction = (
                "outbound" if not registered_email or inbound_receipt.get("sender_actor_id") == case["responsible_actor_id"]
                else "inbound"
            )
            activities.append({
                "message_id": row["message_id"], "status": row["status"],
                "body": row["body"], "created_at": row["created_at"],
                "direction": request_direction,
                "received_at": (inbound_receipt.get("received_at") or row["created_at"])
                               if registered_email else None,
            })
            if row["reply_message_id"] and row["reply_body"] is not None:
                receipt = json.loads(row["send_receipt_json"]) if row["send_receipt_json"] else {}
                received_at = next((receipt[key] for key in ("delivered_at", "accepted_at", "received_at")
                                    if isinstance(receipt.get(key), str)), None)
                activities.append({
                    "message_id": row["reply_message_id"], "status": row["status"],
                    "body": row["reply_body"], "created_at": row["created_at"],
                    "direction": "inbound" if request_direction == "outbound" else "outbound",
                    "received_at": received_at,
                })
        activities.sort(key=lambda item: (item["created_at"], item["message_id"]))
        return {
            "case_id": case["case_id"], "provider_id": case["provider_id"],
            "subject": case["subject"], "business_description": case["business_description"],
            "requested_outcome": case["requested_outcome"], "created_at": case["created_at"],
            "activity": activities,
        }

    @staticmethod
    def _private_context_text(facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        lines = []
        for fact in facts:
            if isinstance(fact, dict) and isinstance(fact.get("text"), str):
                lines.append("- " + fact["text"])
        return "\n\nCustomer-provided context:\n" + "\n".join(lines) if lines else ""

    def mail_cursor(self, provider_actor_id: str, case_id: str) -> dict[str, int] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT uidvalidity,after_uid FROM company_provider_mail_cursors "
                "WHERE provider_actor_id=? AND case_id=?", (provider_actor_id, case_id),
            ).fetchone()
        return {"uidvalidity": row["uidvalidity"], "after_uid": row["after_uid"]} if row else None

    def claim_registered_email(self, *, provider_actor_id: str, message_id: str, case_id: str,
                               uidvalidity: int, mailbox_uid: int, request_hash: str,
                               created_at: str) -> tuple[str, dict[str, Any] | None]:
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT * FROM company_provider_registered_emails WHERE provider_actor_id=? AND message_id=?",
                (provider_actor_id, message_id),
            ).fetchone()
            if prior:
                if (prior["request_hash"] != request_hash or prior["case_id"] != case_id
                        or prior["uidvalidity"] != uidvalidity or prior["mailbox_uid"] != mailbox_uid):
                    raise StoreConflict("registered provider email identity changed")
                return prior["status"], dict(prior)
            db.execute(
                "INSERT INTO company_provider_registered_emails(provider_actor_id,message_id,case_id,uidvalidity,"
                "mailbox_uid,request_hash,status,response_json,created_at) VALUES(?,?,?,?,?,?,'started',NULL,?)",
                (provider_actor_id, message_id, case_id, uidvalidity, mailbox_uid, request_hash, created_at),
            )
        return "started", None

    def complete_registered_email(self, provider_actor_id: str, message_id: str,
                                  response: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT status FROM company_provider_registered_emails WHERE provider_actor_id=? AND message_id=?",
                (provider_actor_id, message_id),
            ).fetchone()
            if row is None:
                raise StoreConflict("registered provider email was not claimed")
            if row["status"] == "processed":
                saved = db.execute(
                    "SELECT response_json FROM company_provider_registered_emails WHERE provider_actor_id=? AND message_id=?",
                    (provider_actor_id, message_id),
                ).fetchone()["response_json"]
                if _json(json.loads(saved)) != _json(response):
                    raise StoreConflict("registered provider email receipt changed")
                return
            db.execute(
                "UPDATE company_provider_registered_emails SET status='processed',response_json=? "
                "WHERE provider_actor_id=? AND message_id=? AND status='started'",
                (_json(response), provider_actor_id, message_id),
            )

    def advance_mail_cursor(self, provider_actor_id: str, case_id: str, uidvalidity: int, mailbox_uid: int,
                            message_id: str, now: str) -> None:
        with self.store.transaction() as db:
            processed = db.execute(
                "SELECT 1 FROM company_provider_registered_emails WHERE provider_actor_id=? AND case_id=? "
                "AND message_id=? AND uidvalidity=? AND mailbox_uid=? AND status='processed'",
                (provider_actor_id, case_id, message_id, uidvalidity, mailbox_uid),
            ).fetchone()
            if not processed:
                raise StoreConflict("provider mailbox cursor cannot pass an unprocessed email")
            prior = db.execute(
                "SELECT uidvalidity,after_uid FROM company_provider_mail_cursors "
                "WHERE provider_actor_id=? AND case_id=?", (provider_actor_id, case_id),
            ).fetchone()
            after_uid = mailbox_uid if prior is None or prior["uidvalidity"] != uidvalidity else max(
                int(prior["after_uid"]), mailbox_uid
            )
            db.execute(
                "INSERT INTO company_provider_mail_cursors(provider_actor_id,case_id,uidvalidity,after_uid,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(provider_actor_id,case_id) DO UPDATE SET "
                "uidvalidity=excluded.uidvalidity,after_uid=excluded.after_uid,updated_at=excluded.updated_at",
                (provider_actor_id, case_id, uidvalidity, after_uid, now),
            )

    def cases_for_actor(self, actor_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT case_id,provider_id,responsible_actor_id,subject,business_description,"
                "requested_outcome,created_at FROM company_provider_cases "
                "WHERE responsible_actor_id=? ORDER BY created_at DESC,case_id", (actor_id,)
            )]

    def messages(self, case_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT client_message_id,message_id,reply_message_id,status,body,reply_body,created_at "
                "FROM company_provider_messages WHERE case_id=? ORDER BY created_at,client_message_id",
                (case_id,),
            )
            return [dict(row) for row in rows]

    def message(self, case_id: str, client_message_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM company_provider_messages WHERE case_id=? AND client_message_id=?",
                (case_id, client_message_id),
            ).fetchone()
        return dict(row) if row else None

    def claim_message(self, case_id: str, client_message_id: str, operation_id: str,
                      message_id: str, body: str, created_at: str) -> tuple[str, dict[str, Any] | None]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", client_message_id):
            raise ValueError("client message id has an invalid format")
        request_hash = _digest({"case_id": case_id, "client_message_id": client_message_id, "body": body})
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM company_provider_cases WHERE case_id=?", (case_id,)).fetchone():
                raise KeyError("provider case was not found")
            prior = db.execute(
                "SELECT * FROM company_provider_messages WHERE case_id=? AND client_message_id=?",
                (case_id, client_message_id),
            ).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise StoreConflict("client message id was reused with different content")
                return prior["status"], dict(prior)
            db.execute(
                "INSERT INTO company_provider_messages(case_id,client_message_id,request_hash,"
                "native_operation_id,message_id,status,body,created_at) VALUES(?,?,?,?,?,'started',?,?)",
                (case_id, client_message_id, request_hash, operation_id, message_id, body, created_at),
            )
        return "started", None

    def complete_provider(self, case_id: str, client_message_id: str, response: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='provider_completed',reply_body=?,"
                "reply_message_id=?,native_response_json=?,error_type=NULL "
                "WHERE case_id=? AND client_message_id=? AND status='outbound_sent'",
                (response["body"], response["reply_message_id"], _json(response),
                 case_id, client_message_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider message is no longer awaiting a native response")

    def complete_delivery(self, case_id: str, client_message_id: str, receipt: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='sent',send_receipt_json=?,error_type=NULL "
                "WHERE case_id=? AND client_message_id=? AND status IN ('provider_completed','reply_submitting','reply_uncertain')",
                (_json(receipt), case_id, client_message_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider reply is no longer awaiting mailbox delivery")

    def mark_uncertain(self, case_id: str, client_message_id: str, error: Exception, stage: str) -> None:
        target = {
            "outbound_submitting": "outbound_uncertain",
            "provider": "provider_uncertain",
            "reply_submitting": "reply_uncertain",
        }.get(stage)
        if target is None:
            raise ValueError("unknown provider uncertainty stage")
        expected = "outbound_submitting" if stage == "outbound_submitting" else (
            "reply_submitting" if stage == "reply_submitting" else "outbound_sent"
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE company_provider_messages SET status=?,error_type=? "
                "WHERE case_id=? AND client_message_id=? AND status=?",
                (target, type(error).__name__, case_id, client_message_id, expected),
            )

    def mark_submission_started(self, case_id: str, client_message_id: str, stage: str) -> None:
        if stage not in {"outbound_submitting", "reply_submitting"}:
            raise ValueError("unknown provider mail submission stage")
        expected = "started" if stage == "outbound_submitting" else "provider_completed"
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status=? WHERE case_id=? AND client_message_id=? AND status=?",
                (stage, case_id, client_message_id, expected),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider mail submission was already claimed")

    def record_outbound_receipt(self, case_id: str, client_message_id: str, receipt: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='outbound_sent',outbound_receipt_json=? "
                "WHERE case_id=? AND client_message_id=? AND status='outbound_submitting'",
                (_json(receipt), case_id, client_message_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider message is no longer awaiting outbound receipt")

    def record_inbound_received(self, case_id: str, client_message_id: str,
                                receipt: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='outbound_sent',outbound_receipt_json=? "
                "WHERE case_id=? AND client_message_id=? AND status='started'",
                (_json(receipt), case_id, client_message_id),
            ).rowcount
            if changed != 1:
                row = db.execute(
                    "SELECT status,outbound_receipt_json FROM company_provider_messages "
                    "WHERE case_id=? AND client_message_id=?", (case_id, client_message_id),
                ).fetchone()
                if row is None or row["status"] not in {"outbound_sent", "provider_uncertain", "provider_completed", "reply_submitting", "reply_uncertain", "sent"}:
                    raise StoreConflict("registered provider message is not ready for native processing")

    def record_reply_receipt(self, case_id: str, client_message_id: str, receipt: dict[str, Any]) -> None:
        self.complete_delivery(case_id, client_message_id, receipt)

    def record_reconciled_outbound(self, case_id: str, client_message_id: str, receipt: dict[str, Any]) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='outbound_sent',outbound_receipt_json=?,error_type=NULL "
                "WHERE case_id=? AND client_message_id=? AND status IN ('outbound_uncertain','outbound_submitting')",
                (_json(receipt), case_id, client_message_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider message is no longer awaiting outbound reconciliation")

    def resume_native_request(self, case_id: str, client_message_id: str) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_messages SET status='outbound_sent',error_type=NULL "
                "WHERE case_id=? AND client_message_id=? AND status='provider_uncertain'",
                (case_id, client_message_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider operation is no longer awaiting native reconciliation")

    def create_session(self, session_hash: str, actor_id: str, csrf_token: str, expires_at: str,
                       now: str) -> None:
        with self.store.transaction() as db:
            db.execute("DELETE FROM company_provider_portal_sessions WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO company_provider_portal_sessions VALUES(?,?,?,?)",
                (session_hash, actor_id, csrf_token, expires_at),
            )

    def session(self, session_hash: str, now: str) -> dict[str, str] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT actor_id,csrf_token,expires_at FROM company_provider_portal_sessions "
                "WHERE session_hash=? AND expires_at>?", (session_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, session_hash: str) -> None:
        with self.store.transaction() as db:
            db.execute("DELETE FROM company_provider_portal_sessions WHERE session_hash=?", (session_hash,))

    def notification(self, case_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM company_provider_case_notifications WHERE case_id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def claim_notification(self, case_id: str, message_id: str, subject: str, now: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM company_provider_case_notifications WHERE case_id=?", (case_id,)).fetchone()
            if row:
                if row["message_id"] != message_id or row["subject"] != subject:
                    raise StoreConflict("provider case notification identity changed")
                return dict(row)
            db.execute(
                "INSERT INTO company_provider_case_notifications VALUES(?,?,?,'pending',NULL,NULL,?)",
                (case_id, message_id, subject, now),
            )
        return self.notification(case_id)

    def notification_submission_started(self, case_id: str, now: str) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_case_notifications SET status='submitting',updated_at=? "
                "WHERE case_id=? AND status='pending'", (now, case_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider case notification submission was already claimed")

    def complete_notification(self, case_id: str, receipt: dict[str, Any], now: str) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE company_provider_case_notifications SET status='sent',receipt_json=?,error_type=NULL,updated_at=? "
                "WHERE case_id=? AND status IN ('pending','submitting','uncertain')",
                (_json(receipt), now, case_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("provider case notification is no longer awaiting its receipt")

    def mark_notification_uncertain(self, case_id: str, error: Exception, now: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE company_provider_case_notifications SET status='uncertain',error_type=?,updated_at=? "
                "WHERE case_id=? AND status='submitting'",
                (type(error).__name__, now, case_id),
            )
