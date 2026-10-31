"""Durable employee-browser dispatch for experiment handoff messages."""

from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Any

from servers.company.browser_chat import PlaywrightEmployeeBrowserSessionProvider
from servers.company.scenarios import RealChatTransport
from servers.simulation.runtime import private_json

LOG = logging.getLogger(__name__)


class ExperimentChatError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ExperimentChatDispatch:
    """Send an immutable, idempotent message through one employee's Chrome."""

    def __init__(
        self,
        company_profile_path: str | Path,
        credentials_path: str | Path,
        state_root: str | Path,
        desktop_manager: Any,
        rocketchat_base_url: str,
    ):
        self.company_profile_path = Path(company_profile_path)
        self.credentials_path = Path(credentials_path)
        self.state_root = Path(state_root)
        self.database = self.state_root / "employee-experiment-chat.sqlite"
        self.lock_root = self.state_root / "employee-experiment-chat-locks"
        self.desktop_manager = desktop_manager
        self.rocketchat_base_url = rocketchat_base_url.rstrip("/") + "/"
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS attempts (
                    key_hash TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submission_started INTEGER NOT NULL DEFAULT 0,
                    submission_started_at TEXT,
                    effective_message_text TEXT,
                    native_message_id TEXT,
                    receipt_json TEXT,
                    error_code TEXT,
                    updated_at TEXT NOT NULL
                )"""
            )
        os.chmod(self.database, 0o600)

    def resolve_sender(self, role: str, employee_id: str | None = None) -> dict[str, str]:
        profile = self._profile()
        company = profile.get("company")
        mapping = company.get("experimentDepartmentMap") if isinstance(company, dict) else None
        department = mapping.get(role) if isinstance(mapping, dict) else None
        if not isinstance(department, str) or not department:
            raise ExperimentChatError("sender_department_unmapped")
        employees = profile.get("employees")
        if not isinstance(employees, list):
            raise ExperimentChatError("company_identity_profile_invalid")
        candidates = []
        for employee in employees:
            if not isinstance(employee, dict) or employee.get("department") != department:
                continue
            account = employee.get("accounts", {}).get("rocketchat") if isinstance(employee.get("accounts"), dict) else None
            if (isinstance(employee.get("id"), str) and employee["id"]
                    and isinstance(employee.get("email"), str) and employee["email"]
                    and isinstance(account, dict) and isinstance(account.get("id"), str) and account["id"]
                    and isinstance(account.get("username"), str) and account["username"]):
                candidates.append({
                    "employee_id": employee["id"], "department": department,
                    "email": employee["email"], "author_id": account["id"],
                    "username": account["username"],
                })
        if not candidates:
            raise ExperimentChatError("department_employee_unavailable")
        if employee_id is not None:
            match = next((candidate for candidate in candidates if candidate["employee_id"] == employee_id), None)
            if match is None:
                raise ExperimentChatError("sender_identity_mismatch")
            return match
        # Profile order is the configured choice when a department has several employees.
        return candidates[0]

    def dispatch(
        self,
        *,
        idempotency_key: str,
        role: str,
        employee_id: str,
        room_id: str,
        message_text: str,
        mention_account_id: str,
    ) -> dict[str, Any]:
        if not all(isinstance(value, str) and value for value in
                   (idempotency_key, role, employee_id, room_id, message_text, mention_account_id)):
            raise ExperimentChatError("invalid_request")
        sender = self.resolve_sender(role, employee_id)
        credentials = self._credentials(employee_id, sender["username"])
        fixture = {
            "employee_mailbox": {
                "address": sender["email"],
                "rocketchat_base_url": self.rocketchat_base_url,
                "rocketchat_room_id": room_id,
                "rocketchat_expected_user_id": sender["author_id"],
            },
            "mention_account_id": mention_account_id,
            "chat_message": message_text,
        }
        request_hash = hashlib.sha256(json.dumps({
            "role": role, "employee_id": employee_id, "room_id": room_id,
            "message_text": message_text, "mention_account_id": mention_account_id,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        with self._attempt_lock(key_hash):
            row = self._load_or_create(key_hash, request_hash, employee_id, room_id)
            if row["request_hash"] != request_hash:
                raise ExperimentChatError("idempotency_conflict")
            if row["status"] == "sent":
                return json.loads(row["receipt_json"])
            transport = self._transport()
            if row["submission_started"]:
                receipt = None
                if row["native_message_id"]:
                    receipt = transport.confirm_message(
                        fixture, credentials, row["native_message_id"],
                        row["effective_message_text"],
                        row["submission_started_at"],
                    )
                if receipt:
                    response = {"status": "sent", "employee_id": employee_id, **receipt}
                    self._finish(key_hash, response)
                    return response
                self._set_status(key_hash, "uncertain", "browser_send_outcome_uncertain")
                return {
                    "status": "uncertain", "employee_id": employee_id, "room_id": room_id,
                    "message_id": row["native_message_id"],
                    "error_code": "browser_send_outcome_uncertain",
                }

            self._set_status(key_hash, "prepared", None)

            def mark_submission_started() -> None:
                started_at = datetime.now(timezone.utc).isoformat()
                self._set_submission_started(key_hash, started_at)

            def record_native_message_id(message_id: str) -> None:
                self._set_native_message_id(key_hash, message_id)

            def record_effective_message_text(effective_text: str) -> None:
                self._set_effective_message_text(key_hash, effective_text)

            try:
                receipt = transport.post(
                    fixture, credentials, idempotency_key,
                    on_submission_started=mark_submission_started,
                    on_message_id_captured=record_native_message_id,
                    on_effective_message_text=record_effective_message_text,
                )
            except Exception as error:
                current = self._load(key_hash)
                error_code = type(error).__name__
                if current["submission_started"]:
                    frames = [(frame.filename, frame.lineno, frame.name)
                              for frame in traceback.extract_tb(error.__traceback__)]
                    LOG.error("Employee chat submission outcome uncertain exception_type=%s stack=%s",
                              error_code, frames)
                    self._set_status(key_hash, "uncertain", "browser_send_outcome_uncertain")
                    return {
                        "status": "uncertain", "employee_id": employee_id, "room_id": room_id,
                        "message_id": current["native_message_id"],
                        "error_code": "browser_send_outcome_uncertain",
                    }
                self._set_status(key_hash, "retryable", error_code)
                raise ExperimentChatError("browser_send_failed_before_submit") from error
            response = {"status": "sent", "employee_id": employee_id, **receipt}
            self._finish(key_hash, response)
            return response

    def _profile(self) -> dict[str, Any]:
        try:
            profile = json.loads(self.company_profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ExperimentChatError("company_identity_profile_unavailable") from error
        if not isinstance(profile, dict):
            raise ExperimentChatError("company_identity_profile_invalid")
        return profile

    def _credentials(self, employee_id: str, username: str) -> dict[str, str]:
        try:
            payload = private_json(self.credentials_path)
        except (OSError, ValueError, PermissionError) as error:
            raise ExperimentChatError("employee_chat_credentials_unavailable") from error
        entry = payload.get(employee_id) if isinstance(payload, dict) else None
        password = entry.get("password") if isinstance(entry, dict) else None
        configured_username = entry.get("username") if isinstance(entry, dict) else username
        if configured_username != username or not isinstance(password, str) or not password:
            raise ExperimentChatError("employee_chat_credentials_unavailable")
        return {"username": username, "password": password}

    def _transport(self) -> RealChatTransport:
        provider = PlaywrightEmployeeBrowserSessionProvider(lambda: self.desktop_manager)
        return RealChatTransport(browser_session_provider=provider)

    @contextmanager
    def _attempt_lock(self, key_hash: str):
        lock_path = self.lock_root / (key_hash + ".lock")
        with closing(lock_path.open("a+b")) as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _load_or_create(self, key_hash: str, request_hash: str,
                        employee_id: str, room_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT * FROM attempts WHERE key_hash=?", (key_hash,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """INSERT INTO attempts
                           (key_hash,request_hash,employee_id,room_id,status,updated_at)
                           VALUES(?,?,?,?,?,?)""",
                        (key_hash, request_hash, employee_id, room_id, "prepared", now),
                    )
                    row = connection.execute(
                        "SELECT * FROM attempts WHERE key_hash=?", (key_hash,),
                    ).fetchone()
                return dict(row)

    def _load(self, key_hash: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM attempts WHERE key_hash=?", (key_hash,)).fetchone()
        if row is None:
            raise ExperimentChatError("dispatch_state_unavailable")
        return dict(row)

    def _set_status(self, key_hash: str, status: str, error_code: str | None) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE attempts SET status=?,error_code=?,updated_at=? WHERE key_hash=?",
                    (status, error_code, datetime.now(timezone.utc).isoformat(), key_hash),
                )

    def _set_submission_started(self, key_hash: str, started_at: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """UPDATE attempts SET status='submitting',submission_started=1,
                       submission_started_at=?,updated_at=? WHERE key_hash=?""",
                    (started_at, started_at, key_hash),
                )

    def _set_native_message_id(self, key_hash: str, message_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT native_message_id FROM attempts WHERE key_hash=?", (key_hash,),
                ).fetchone()
                if row is None or row["native_message_id"] not in {None, message_id}:
                    raise ExperimentChatError("native_message_id_conflict")
                connection.execute(
                    """UPDATE attempts SET native_message_id=?,status='reconciling',updated_at=?
                       WHERE key_hash=?""",
                    (message_id, datetime.now(timezone.utc).isoformat(), key_hash),
                )

    def _set_effective_message_text(self, key_hash: str, message_text: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT effective_message_text FROM attempts WHERE key_hash=?", (key_hash,),
                ).fetchone()
                if row is None or row["effective_message_text"] not in {None, message_text}:
                    raise ExperimentChatError("message_text_conflict")
                connection.execute(
                    "UPDATE attempts SET effective_message_text=?,updated_at=? WHERE key_hash=?",
                    (message_text, datetime.now(timezone.utc).isoformat(), key_hash),
                )

    def _finish(self, key_hash: str, receipt: dict[str, Any]) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """UPDATE attempts SET status='sent',native_message_id=?,receipt_json=?,
                       error_code=NULL,updated_at=? WHERE key_hash=?""",
                    (receipt.get("message_id"), json.dumps(receipt, sort_keys=True),
                     datetime.now(timezone.utc).isoformat(), key_hash),
                )
