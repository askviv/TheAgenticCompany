"""Durable employee mail handoffs; independent of company time."""
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

from .clock_rate import simulation_message_footer
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


class MarketplaceNotification(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    issue_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_id: str = Field(min_length=3)
    subject: str = Field(min_length=1)
    recipient_email: str = Field(min_length=3)
    business_description: str = Field(min_length=1)


class IssueNotification(MarketplaceNotification):
    issue_name: str = Field(min_length=1)
    email_queue: str = Field(min_length=1)
    status: str
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
                delivered_at TEXT, deadline_at TEXT, observed_unread_real_at TEXT,
                timing_basis TEXT, read_at TEXT,
                handoff_eligible_at TEXT,
                receipt_json TEXT, handoff_json TEXT, handoff_receipt_json TEXT,
                submission_started_at TEXT, sent_uidvalidity TEXT, sent_uid_next INTEGER,
                native_message_id TEXT,
                last_error TEXT, updated_at TEXT NOT NULL)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(company_issue_handoffs)")}
            if "observed_unread_real_at" not in columns:
                connection.execute("ALTER TABLE company_issue_handoffs ADD COLUMN observed_unread_real_at TEXT")
            if "timing_basis" not in columns:
                connection.execute("ALTER TABLE company_issue_handoffs ADD COLUMN timing_basis TEXT")
            for name, declaration in (
                ("submission_started_at", "TEXT"), ("sent_uidvalidity", "TEXT"),
                ("sent_uid_next", "INTEGER"), ("native_message_id", "TEXT"),
                ("handoff_eligible_at", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE company_issue_handoffs ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS company_issue_handoff_native_message_id_idx "
                "ON company_issue_handoffs(native_message_id) WHERE native_message_id IS NOT NULL"
            )

    def register(self, notification: MarketplaceNotification, now: datetime) -> dict:
        payload = notification.model_dump(mode="json")
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?",
                                     (notification.issue_key,)).fetchone()
            if row:
                prior = json.loads(row["notification_json"])
                immutable = ("issue_key", "message_id", "recipient_email")
                if isinstance(notification, IssueNotification):
                    immutable += ("issue_name", "email_queue")
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
        with self.store.transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            return dict(row)

    def pending(self) -> list[dict]:
        with self.store.transaction(write=False) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM company_issue_handoffs WHERE stage NOT IN ('read','sent') ORDER BY issue_key")]

    def delivered(self) -> list[dict]:
        with self.store.transaction(write=False) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM company_issue_handoffs WHERE delivered_at IS NOT NULL ORDER BY delivered_at,issue_key")]

    def reserve_resend(self, key: str, source: dict, intent: dict, now: datetime) -> bool:
        with self.store.transaction() as connection:
            result = connection.execute(
                """INSERT INTO company_issue_handoffs
                   (issue_key,notification_json,stage,delivered_at,handoff_json,updated_at)
                   VALUES (?,?,'submitting',?,?,?) ON CONFLICT(issue_key) DO NOTHING""",
                (key, source["notification_json"], source["delivered_at"],
                 json.dumps(intent), utc(now).isoformat()),
            )
            return result.rowcount == 1

    def discard_unsubmitted_resend(self, key: str) -> None:
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM company_issue_handoffs WHERE issue_key=?", (key,))

    def observe(self, key: str, receipt: dict, now: datetime, *, kernel_mode: bool = False, immediate: bool = False) -> dict:
        received_at = utc(datetime.fromisoformat(receipt["received_at"]))
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            if row["stage"] in (Stage.READ, Stage.SENT, Stage.SUBMITTING, Stage.UNCERTAIN):
                return dict(row)
            delivered = row["delivered_at"] or received_at.isoformat()
            anchor = row["observed_unread_real_at"]
            if kernel_mode and not receipt["seen"] and anchor is None:
                anchor = utc(now).isoformat()
            deadline = row["deadline_at"] or (utc(now).isoformat() if immediate else None) or (
                (datetime.fromisoformat(anchor) + TIMEOUT).isoformat() if anchor else
                (received_at + TIMEOUT).isoformat())
            basis = row["timing_basis"] or ("first_unread_observation" if kernel_mode and anchor else None)
            stage = Stage.READ if not immediate and receipt["seen"] and row["handoff_eligible_at"] is None else Stage.WAITING
            connection.execute("""UPDATE company_issue_handoffs SET stage=?, delivered_at=?,
                deadline_at=?,observed_unread_real_at=?,timing_basis=?,read_at=?,receipt_json=?,
                last_error=NULL,updated_at=? WHERE issue_key=?""",
                (stage, delivered, deadline, anchor, basis, utc(now).isoformat() if receipt["seen"] else None,
                 json.dumps(receipt, sort_keys=True), utc(now).isoformat(), key))
        return self.get(key)

    def prepare(self, key: str, intent: dict, now: datetime) -> dict | None:
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM company_issue_handoffs WHERE issue_key=?", (key,)).fetchone()
            if row["stage"] != Stage.WAITING or not row["deadline_at"]:
                return None
            if utc(now) < datetime.fromisoformat(row["deadline_at"]):
                return None
            prior = json.loads(row["handoff_json"]) if row["handoff_json"] else None
            if prior is None or (
                prior["run_id"].startswith("company-issue:")
                and prior["body"].split("\n\nThis is a simulated environment.", 1)[0]
                != intent["body"].split("\n\nThis is a simulated environment.", 1)[0]
            ):
                encoded = json.dumps(intent, sort_keys=True)
                connection.execute("UPDATE company_issue_handoffs SET handoff_json=?,updated_at=? WHERE issue_key=?",
                                   (encoded, utc(now).isoformat(), key))
        return self.get(key)

    def mark_submitting(
        self, key: str, intent: dict, now: datetime,
        *, sent_uidvalidity: str | None = None, sent_uid_next: int | None = None,
    ) -> None:
        with self.store.transaction() as connection:
            changed = connection.execute("""UPDATE company_issue_handoffs SET stage='submitting',
                submission_started_at=?,sent_uidvalidity=?,sent_uid_next=?,updated_at=?
                WHERE issue_key=? AND stage='waiting' AND handoff_json=?""",
                (utc(now).isoformat(), sent_uidvalidity, sent_uid_next, utc(now).isoformat(),
                 key, json.dumps(intent, sort_keys=True))).rowcount
            if changed != 1:
                raise StoreConflict("Issue handoff was already claimed or read")

    def mark_eligible(self, key: str, intent: dict, now: datetime) -> None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT stage,deadline_at,handoff_json,handoff_eligible_at FROM company_issue_handoffs "
                "WHERE issue_key=?", (key,),
            ).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            if row["stage"] != Stage.WAITING or row["handoff_json"] != json.dumps(intent, sort_keys=True):
                raise StoreConflict("Issue handoff is no longer eligible")
            if not row["deadline_at"] or utc(now) < datetime.fromisoformat(row["deadline_at"]):
                raise StoreConflict("Issue handoff deadline has not elapsed")
            if row["handoff_eligible_at"] is None:
                connection.execute(
                    "UPDATE company_issue_handoffs SET handoff_eligible_at=?,updated_at=? WHERE issue_key=?",
                    (utc(now).isoformat(), utc(now).isoformat(), key),
                )

    def record_native_message_id(self, key: str, message_id: str) -> None:
        if not isinstance(message_id, str) or not re.fullmatch(r"<[^<>\s]+>", message_id):
            raise ValueError("native Message-ID must be an RFC message identifier")
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT submission_started_at,native_message_id FROM company_issue_handoffs WHERE issue_key=?",
                (key,),
            ).fetchone()
            if row is None:
                raise KeyError("Tracked company issue not found")
            if row["submission_started_at"] is None:
                raise StoreConflict("native Message-ID cannot be recorded before submission starts")
            if row["native_message_id"] not in (None, message_id):
                raise StoreConflict("Issue handoff already has a different native Message-ID")
            connection.execute(
                "UPDATE company_issue_handoffs SET native_message_id=? WHERE issue_key=?",
                (message_id, key),
            )

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
                 state_root: Path, runner=subprocess.run, command_factory=None,
                 registration_client=None):
        self.config_path, self.executable, self.source_root = config_path, executable, source_root
        self.state_root = state_root
        self.runner = runner
        self.command_factory = command_factory
        self.registration_client = registration_client

    def schedule(self, key: str, deadline: datetime) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid issue timer identity")
        if self.registration_client is not None:
            self.registration_client.register_deadline(key, utc(deadline))
            return
        unit = f"tac-issue-handoff-{key}"
        existing = self.runner(["systemctl", "is-active", "--quiet", unit + ".timer"],
                               check=False, capture_output=True, timeout=30)
        if existing.returncode == 0:
            return
        scheduled_at = utc(deadline)
        if scheduled_at <= datetime.now(timezone.utc):
            # A transient calendar timer in the past is immediately recreated
            # by monitor reconciliation. Back off while retaining the original
            # durable deadline as the source of truth.
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=30)
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
        ]
        command += (self.command_factory(key) if self.command_factory else [
            self.executable, "-m", "servers.simulation.issue_handoff_runtime",
            "--config", str(self.config_path), "--issue-key", key,
        ])
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
                 erp_public_url: str, kernel_mode: bool = False, coffee_handoff_delivery: str = "browser",
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store, self.mail, self.scheduler = store, mail, scheduler
        self.actors = {actor["email"].casefold(): actor for actor in actors}
        self.viv = next(actor for actor in actors if actor["actor_id"] == "viv")
        self.erp_public_url = erp_public_url.rstrip("/")
        self.kernel_mode = kernel_mode
        self.coffee_handoff_delivery = coffee_handoff_delivery
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
            payload = json.loads(row["notification_json"])
            marketplace = "issue_name" not in payload
            record = (MarketplaceNotification if marketplace else IssueNotification).model_validate(payload)
            if not marketplace:
                result.append({"id": record.message_id, "timestamp": row["delivered_at"],
                           "event": "ISSUE_NOTIFICATION_DELIVERED", "type": "email", "direction": "sent",
                           "title": record.subject, "description": "ERPNext notified the responsible employee.",
                           "summary": record.business_description, "actor": "ERPNext"})
            if row["stage"] in (Stage.SENT, Stage.SUBMITTING, Stage.UNCERTAIN):
                intent = json.loads(row["handoff_json"])
                sent = row["stage"] == Stage.SENT
                company_time = intent.get("company_time")
                if company_time is None:
                    footer = re.search(r"The current simulated time is (.+ UTC)\.\s*$", intent["body"])
                    if footer:
                        company_time = datetime.strptime(footer[1], "%d %b %Y, %H:%M UTC").replace(tzinfo=timezone.utc).isoformat()
                result.append({"id": intent["message_id"], "timestamp": company_time,
                               "event": ("COFFEE_REQUEST_HANDED_TO_VIV" if marketplace else "ISSUE_HANDED_TO_VIV")
                                        if sent else "HANDOFF_SUBMISSION_UNCONFIRMED",
                               "type": "email", "direction": "sent" if sent else None,
                               "delivery_status": (
                                   "Awaiting receipt confirmation" if sent else
                                   "Sending" if row["stage"] == Stage.SUBMITTING else "Submission unconfirmed"
                               ),
                               "native_message_id": row["native_message_id"],
                               "title": intent["subject"], "description": "The employee asked Viv to take care of the request.",
                               "summary": record.business_description, "content": intent["body"],
                               "actor": intent["sender"]["actor_id"], "recipient": self.viv["actor_id"],
                               "related_activity_id": record.message_id, "can_resend": marketplace and sent,
                               **({"attachments": ["Original email (.eml)"]} if intent.get("forward_original") else {})})
        return result

    def resend_marketplace_handoff(self, message_id: str, request_id: str) -> None:
        for source in self.store.delivered():
            if source["stage"] != Stage.SENT or "issue_name" in json.loads(source["notification_json"]):
                continue
            previous = json.loads(source["handoff_json"])
            if previous["message_id"] == message_id:
                break
        else:
            raise KeyError("Sent marketplace handoff not found")

        key = "manual-resend:" + request_id
        company_time = datetime.now(timezone.utc)
        body = previous["body"].split("\n\nThis is a simulated environment. The clock runs at ")[0]
        intent = {
            **previous,
            "resend_of": message_id,
            "message_id": f"<handoff-resend-{request_id}@theagentcompany.local>",
            "run_id": key,
            "company_time": company_time.isoformat(),
            "body": body.rstrip() + "\n\n" + simulation_message_footer(),
        }
        if not self.store.reserve_resend(key, source, intent, self.now()):
            existing = self.store.get(key)
            if (json.loads(existing["handoff_json"]).get("resend_of") == message_id
                    and existing["stage"] == Stage.SENT):
                return
            raise StoreConflict("Resend already in progress or delivery unconfirmed")

        submission_started = False

        def mark_submission_started() -> None:
            nonlocal submission_started
            submission_started = True

        try:
            receipt = self.mail.send(
                intent["sender"], self.viv, intent["subject"], intent["body"],
                intent["message_id"], intent["run_id"], intent["generation"],
                in_reply_to=intent.get("in_reply_to"),
                forward_original=bool(intent.get("forward_original")),
                system_handoff=True,
                on_submission_started=mark_submission_started,
            )
            self.store.complete(key, receipt, self.now())
        except Exception as error:
            if submission_started:
                self.store.fail(key, error, self.now())
            else:
                self.store.discard_unsubmitted_resend(key)
            raise

    def _intent(self, record: MarketplaceNotification, actor: dict) -> dict:
        company_time = datetime.now(timezone.utc).isoformat()
        if not isinstance(record, IssueNotification):
            return {"sender": actor, "company_time": company_time, "subject": "Fwd: " + record.subject,
                    "forward_original": True,
                    "body": "Hi Viv, can you take care of this request?\n\n"
                            + simulation_message_footer(),
                    "message_id": f"<coffee-{record.issue_key}-handoff@theagentcompany.local>",
                    "run_id": "coffee-handoff:" + record.issue_key, "generation": 0,
                    "in_reply_to": record.message_id}

        def link(doctype: str, name: str) -> str:
            return f"{self.erp_public_url}/app/{doctype.lower().replace(' ', '-')}/{quote(name, safe='')}"
        links = ["Company issue: " + link("Issue", record.issue_name)]
        links += [f"{row.doctype} {row.name}: {link(row.doctype, row.name)}" for row in record.affected_orders]
        sender = {key: actor[key] for key in ("actor_id", "email", "kind") if key in actor}
        return {"sender": sender, "company_time": company_time, "subject": "Please handle this: " + record.subject,
                "body": "Please handle this,\n\n" + record.business_description
                        + "\n\nRequested outcome: " + record.requested_outcome + "\n\n" + "\n".join(links)
                        + "\n\n" + simulation_message_footer(),
                "message_id": f"<company-issue-{record.issue_key}-handoff@theagentcompany.local>",
                "run_id": "company-issue:" + record.issue_key, "generation": 0,
                "in_reply_to": record.message_id}

    def process(self, key: str) -> None:
        now = utc(self.now())
        row = self.store.get(key)
        if row["stage"] in (Stage.READ, Stage.SENT):
            return
        try:
            payload = json.loads(row["notification_json"])
            marketplace = "issue_name" not in payload
            record = (MarketplaceNotification if marketplace else IssueNotification).model_validate(payload)
            actor = self.actors[record.recipient_email.casefold()]
            if row["stage"] in (Stage.SUBMITTING, Stage.UNCERTAIN):
                intent = json.loads(row["handoff_json"])
                if intent.get("resend_of"):
                    return  # An interrupted SMTP resend must not be submitted again.
                native_message_id = row["native_message_id"]
                if native_message_id is None:
                    native_message_id = self.mail.employee_sent_message_id(
                        intent["sender"], self.viv, intent["subject"], intent["body"],
                        intent.get("in_reply_to"), row.get("submission_started_at"),
                        sent_uidvalidity=row.get("sent_uidvalidity"),
                        sent_uid_next=row.get("sent_uid_next"),
                        **({"forward_original": True} if intent.get("forward_original") else {}),
                    )
                    if native_message_id is not None:
                        self.store.record_native_message_id(key, native_message_id)
                receipt = self.mail.find_receipt(
                    self.viv, native_message_id or intent["message_id"], intent["subject"],
                    intent["run_id"], intent["generation"],
                    native_message=native_message_id is not None,
                )
                if receipt:
                    self.store.complete(key, receipt, now)
                # An uncertain SMTP boundary is never blindly resubmitted.
                return
            receipt = self.mail.inspect_issue_notification(actor, record.message_id)
            if receipt is None:
                if row["delivered_at"]:
                    raise RuntimeError("Previously delivered issue notification is no longer locatable")
                return
            row = self.store.observe(key, receipt, now, kernel_mode=self.kernel_mode, immediate=marketplace)
            if row["stage"] == Stage.READ:
                return
            deadline = datetime.fromisoformat(row["deadline_at"])
            if now < deadline:
                self.scheduler.schedule(key, deadline)
                return
            intent = self._intent(record, actor)
            prepared = self.store.prepare(key, intent, now)
            if prepared is None:
                return
            intent = json.loads(prepared["handoff_json"])
            # Recheck at the submission edge; an observed read is terminal.
            receipt = self.mail.inspect_issue_notification(actor, record.message_id)
            if receipt is None:
                return
            if self.store.observe(key, receipt, now, kernel_mode=self.kernel_mode, immediate=marketplace)["stage"] == Stage.READ:
                return
            self.store.mark_eligible(key, intent, now)

            def mark_submitting(sent_uidvalidity: str | None = None,
                                sent_uid_next: int | None = None) -> None:
                self.store.mark_submitting(
                    key, intent, self.now(), sent_uidvalidity=sent_uidvalidity,
                    sent_uid_next=sent_uid_next,
                )

            def submission_started():
                # Non-browser delivery has not opened the employee message, so
                # retain the final read check at the SMTP submission edge.
                latest = self.mail.inspect_issue_notification(actor, record.message_id)
                if latest is None:
                    raise RuntimeError("Issue notification receipt disappeared before submission")
                if self.store.observe(key, latest, self.now(), kernel_mode=self.kernel_mode, immediate=marketplace)["stage"] == Stage.READ:
                    raise ReadBeforeHandoff()
                mark_submitting()

            def submission_snapshot(sent_uidvalidity: str, sent_uid_next: int) -> None:
                # The browser opens the exact message to compose its native
                # reply. Eligibility was durably established before that read.
                mark_submitting(sent_uidvalidity, sent_uid_next)

            def native_id_captured(message_id: str) -> None:
                self.store.record_native_message_id(key, message_id)
            transport = {"system_handoff": True} if marketplace and self.coffee_handoff_delivery == "api" else {}
            if intent.get("forward_original"):
                transport["forward_original"] = True
            result = self.mail.send(actor, self.viv, intent["subject"], intent["body"], intent["message_id"],
                                    intent["run_id"], intent["generation"], in_reply_to=record.message_id,
                                    on_submission_started=submission_started,
                                    on_submission_snapshot=submission_snapshot,
                                    on_native_message_id=native_id_captured, **transport)
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
