"""Durable, issue-only employee mail handoffs; independent of company time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import logging
import re
from pathlib import Path
import subprocess
from typing import Any, Callable
from urllib.parse import quote

import traceback

from pydantic import BaseModel, ConfigDict, Field

from .store import SQLiteStore, StoreConflict

TIMEOUT = timedelta(minutes=5)
LOG = logging.getLogger(__name__)


class ReadBeforeHandoff(Exception):
    """The employee read the tracked message before SMTP submission."""


class Stage(StrEnum):
    WAITING = "waiting"
    READ = "read"
    SUBMITTING = "submitting"
    UNCERTAIN = "uncertain"
    SENT = "sent"


class OrderReference(BaseModel):
    model_config = ConfigDict(extra="ignore")
    doctype: str = Field(min_length=1)
    name: str = Field(min_length=1)


class IssueNotification(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    issue_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    issue_name: str = Field(min_length=1)
    email_queue: str = Field(min_length=1)
    message_id: str = Field(min_length=3)
    subject: str = Field(min_length=1)
    recipient_email: str = Field(min_length=3)
    status: str
    business_description: str = Field(min_length=1)
    requested_outcome: str = Field(min_length=1)
    affected_orders: list[OrderReference] = Field(min_length=1)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Issue handoff wall time must include a timezone")
    return value.astimezone(timezone.utc)


class IssueHandoffStore:
    """Use the existing private SQLite transaction boundary, without fake runs."""
    def __init__(self, store: SQLiteStore):
        self.store = store
        with store.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS company_issue_handoffs (
                issue_key TEXT PRIMARY KEY, notification_json TEXT NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('waiting','read','submitting','uncertain','sent')),
                delivered_at TEXT, deadline_at TEXT, read_at TEXT,
                receipt_json TEXT, handoff_json TEXT, handoff_receipt_json TEXT,
                last_error TEXT, updated_at TEXT NOT NULL)""")

    def register(self, notification: IssueNotification, now: datetime) -> dict:
        payload = notification.model_dump(mode="json")
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?",
                                     (notification.issue_key,)).fetchone()
            if row:
                prior = json.loads(row["notification_json"])
                immutable = ("issue_key", "issue_name", "email_queue", "message_id", "recipient_email")
                if any(prior[key] != payload[key] for key in immutable):
                    raise StoreConflict("Issue notification identity changed")
                # Current native issue details may gain more affected orders.
                connection.execute("UPDATE company_issue_handoffs SET notification_json=? WHERE issue_key=?",
                                   (json.dumps(payload, sort_keys=True), notification.issue_key))
            else:
                connection.execute("""INSERT INTO company_issue_handoffs
                    (issue_key,notification_json,stage,updated_at) VALUES(?,?,'waiting',?)""",
                    (notification.issue_key, json.dumps(payload, sort_keys=True), utc(now).isoformat()))
        return self.get(notification.issue_key)

    def get(self, key: str) -> dict:
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            return dict(row)

    def pending(self) -> list[dict]:
        with self.store.transaction() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM company_issue_handoffs WHERE stage NOT IN ('read','sent') ORDER BY issue_key")]

    def delivered(self) -> list[dict]:
        with self.store.transaction() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM company_issue_handoffs WHERE delivered_at IS NOT NULL ORDER BY delivered_at,issue_key")]

    def observe(self, key: str, receipt: dict, now: datetime) -> dict:
        received_at = utc(datetime.fromisoformat(receipt["received_at"]))
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            if row["stage"] in (Stage.READ, Stage.SENT, Stage.SUBMITTING, Stage.UNCERTAIN):
                return dict(row)
            delivered = row["delivered_at"] or received_at.isoformat()
            deadline = row["deadline_at"] or (received_at + TIMEOUT).isoformat()
            stage = Stage.READ if receipt["seen"] else Stage.WAITING
            connection.execute("""UPDATE company_issue_handoffs SET stage=?, delivered_at=?,
                deadline_at=?,read_at=?,receipt_json=?,last_error=NULL,updated_at=? WHERE issue_key=?""",
                (stage, delivered, deadline, utc(now).isoformat() if receipt["seen"] else None,
                 json.dumps(receipt, sort_keys=True), utc(now).isoformat(), key))
        return self.get(key)

    def prepare(self, key: str, intent: dict, now: datetime) -> dict | None:
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row["stage"] != Stage.WAITING or not row["deadline_at"]:
                return None
            if utc(now) < datetime.fromisoformat(row["deadline_at"]):
                return None
            encoded = json.dumps(intent, sort_keys=True)
            connection.execute("UPDATE company_issue_handoffs SET handoff_json=?,updated_at=? WHERE issue_key=?",
                               (encoded, utc(now).isoformat(), key))
        return self.get(key)

    def mark_submitting(self, key: str, intent: dict, now: datetime) -> None:
        with self.store.transaction() as connection:
            changed = connection.execute("""UPDATE company_issue_handoffs SET stage='submitting',updated_at=?
                WHERE issue_key=? AND stage='waiting' AND handoff_json=?""",
                (utc(now).isoformat(), key, json.dumps(intent, sort_keys=True))).rowcount
            if changed != 1:
                raise StoreConflict("Issue handoff was already claimed or read")

    def complete(self, key: str, receipt: dict, now: datetime) -> None:
        with self.store.transaction() as connection:
            connection.execute("""UPDATE company_issue_handoffs SET stage='sent',handoff_receipt_json=?,
                last_error=NULL,updated_at=? WHERE issue_key=? AND stage IN ('submitting','uncertain')""",
                (json.dumps(receipt, sort_keys=True), utc(now).isoformat(), key))

    def fail(self, key: str, error: Exception, now: datetime) -> None:
        with self.store.transaction() as connection:
            connection.execute("""UPDATE company_issue_handoffs SET
                stage=CASE WHEN stage='submitting' THEN 'uncertain' ELSE stage END,
                last_error=?,updated_at=? WHERE issue_key=?""",
                (type(error).__name__, utc(now).isoformat(), key))


class SystemdIssueScheduler:
    """Native wall-clock timers; durable SQLite is authoritative after reboot."""
    def __init__(self, config_path: Path, *, executable: str, source_root: Path,
                 state_root: Path, runner=subprocess.run):
        self.config_path, self.executable, self.source_root = config_path, executable, source_root
        self.state_root = state_root
        self.runner = runner

    def schedule(self, key: str, deadline: datetime) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid issue timer identity")
        unit = f"tac-issue-handoff-{key}"
        existing = self.runner(["systemctl", "is-active", "--quiet", unit + ".timer"],
                               check=False, capture_output=True, timeout=30)
        if existing.returncode == 0:
            return
        scheduled_at = utc(deadline)
        if scheduled_at.microsecond:
            scheduled_at = scheduled_at.replace(microsecond=0) + timedelta(seconds=1)
        command = [
            "systemd-run", "--quiet", "--collect", "--unit=" + unit,
            "--on-calendar=" + scheduled_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "--timer-property=AccuracySec=1s", "--timer-property=Persistent=true",
            "--property=WorkingDirectory=" + str(self.source_root), "--property=UMask=0077",
            "--property=NoNewPrivileges=yes", "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "--property=ReadWritePaths=" + str(self.state_root),
            self.executable, "-m", "servers.simulation.issue_handoff_runtime",
            "--config", str(self.config_path), "--issue-key", key,
        ]
        try:
            self.runner(command, check=True, capture_output=True, timeout=30)
        except subprocess.CalledProcessError:
            # Another worker may have installed this exact timer after our check.
            concurrent = self.runner(["systemctl", "is-active", "--quiet", unit + ".timer"],
                                     check=False, capture_output=True, timeout=30)
            if concurrent.returncode != 0:
                raise


class IssueHandoffService:
    def __init__(self, store: IssueHandoffStore, mail, scheduler, *, actors: list[dict],
                 erp_public_url: str, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store, self.mail, self.scheduler = store, mail, scheduler
        self.actors = {actor["email"].casefold(): actor for actor in actors}
        self.viv = next(actor for actor in actors if actor["actor_id"] == "viv")
        self.erp_public_url = erp_public_url.rstrip("/")
        self.now = now

    def register(self, records: list[dict]) -> None:
        for value in records:
            record = IssueNotification.model_validate(value)
            actor = self.actors.get(record.recipient_email.casefold())
            if actor is None or actor["actor_id"] == "viv":
                raise ValueError("Issue recipient is not a configured employee mailbox")
            self.store.register(record, self.now())

    def process_pending(self) -> list[dict]:
        errors = []
        for row in self.store.pending():
            try:
                self.process(row["issue_key"])
            except Exception as error:
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("tracked issue processing failed key=%s exception_type=%s stack=%s",
                          row["issue_key"], type(error).__name__, frames)
                errors.append({"issue_key": row["issue_key"], "error_type": type(error).__name__})
        return errors

    def activity(self) -> list[dict]:
        result = []
        for row in self.store.delivered():
            record = IssueNotification.model_validate_json(row["notification_json"])
            result.append({"id": record.message_id, "timestamp": row["delivered_at"],
                           "event": "ISSUE_NOTIFICATION_DELIVERED", "type": "email", "direction": "sent",
                           "title": record.subject, "description": "ERPNext notified the responsible employee.",
                           "summary": record.business_description, "actor": "ERPNext"})
            if row["stage"] == Stage.SENT:
                intent = json.loads(row["handoff_json"])
                result.append({"id": intent["message_id"], "timestamp": row["updated_at"],
                               "event": "ISSUE_HANDED_TO_VIV", "type": "email", "direction": "sent",
                               "title": intent["subject"], "description": "The employee asked Viv to handle the issue.",
                               "summary": record.business_description, "content": intent["body"],
                               "actor": intent["sender"]["actor_id"]})
        return result

    def _intent(self, record: IssueNotification, actor: dict) -> dict:
        def link(doctype: str, name: str) -> str:
            return f"{self.erp_public_url}/app/{doctype.lower().replace(' ', '-')}/{quote(name, safe='')}"
        links = ["Company issue: " + link("Issue", record.issue_name)]
        links += [f"{row.doctype} {row.name}: {link(row.doctype, row.name)}" for row in record.affected_orders]
        sender = {key: actor[key] for key in ("actor_id", "email", "kind") if key in actor}
        return {"sender": sender, "subject": "Please handle this: " + record.subject,
                "body": "Please handle this,\n\n" + record.business_description
                        + "\n\nRequested outcome: " + record.requested_outcome + "\n\n" + "\n".join(links),
                "message_id": f"<company-issue-{record.issue_key}-handoff@theagentcompany.local>",
                "run_id": "company-issue:" + record.issue_key, "generation": 0}

    def process(self, key: str) -> None:
        now = utc(self.now())
        row = self.store.get(key)
        if row["stage"] in (Stage.READ, Stage.SENT):
            return
        try:
            record = IssueNotification.model_validate_json(row["notification_json"])
            actor = self.actors[record.recipient_email.casefold()]
            if row["stage"] in (Stage.SUBMITTING, Stage.UNCERTAIN):
                intent = json.loads(row["handoff_json"])
                receipt = self.mail.find_receipt(self.viv, intent["message_id"], intent["subject"],
                                                 intent["run_id"], intent["generation"])
                if receipt:
                    self.store.complete(key, receipt, now)
                # An uncertain SMTP boundary is never blindly resubmitted.
                return
            receipt = self.mail.inspect_issue_notification(actor, record.message_id)
            if receipt is None:
                if row["delivered_at"]:
                    raise RuntimeError("Previously delivered issue notification is no longer locatable")
                return
            row = self.store.observe(key, receipt, now)
            if row["stage"] == Stage.READ:
                return
            deadline = datetime.fromisoformat(row["deadline_at"])
            if now < deadline:
                self.scheduler.schedule(key, deadline)
                return
            intent = self._intent(record, actor)
            if self.store.prepare(key, intent, now) is None:
                return
            # Recheck at the submission edge; an observed read is terminal.
            receipt = self.mail.inspect_issue_notification(actor, record.message_id)
            if receipt is None:
                return
            if self.store.observe(key, receipt, now)["stage"] == Stage.READ:
                return
            def submission_started():
                latest = self.mail.inspect_issue_notification(actor, record.message_id)
                if latest is None:
                    raise RuntimeError("Issue notification receipt disappeared before submission")
                if self.store.observe(key, latest, self.now())["stage"] == Stage.READ:
                    raise ReadBeforeHandoff()
                self.store.mark_submitting(key, intent, self.now())
            result = self.mail.send(actor, self.viv, intent["subject"], intent["body"], intent["message_id"],
                                    intent["run_id"], intent["generation"], in_reply_to=record.message_id,
                                    on_submission_started=submission_started)
            self.store.complete(key, result, self.now())
        except ReadBeforeHandoff:
            return
        except StoreConflict as error:
            if self.store.get(key)["stage"] in (Stage.READ, Stage.SUBMITTING, Stage.UNCERTAIN, Stage.SENT):
                return
            self.store.fail(key, error, self.now())
            raise
        except Exception as error:
            self.store.fail(key, error, self.now())
            raise
