"""Private idempotency store shared by isolated τ and Coffee actor workers."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


def canonical_request_hash(request: dict[str, Any]) -> str:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ActorTurnStore:
    """Persist turn intent before model work and output before HTTP completion.

    Only the input digest is stored. Result state remains in the protected
    worker database; provider raw responses are the caller's responsibility to
    exclude before persisting.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()
        if os.name == "posix":
            self.path.chmod(0o600)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS actor_turns (
                    turn_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started','completed')),
                    result_json TEXT
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tau_retail_feedback_states (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    initial_projection_hash TEXT NOT NULL,
                    database_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id)
                )"""
            )
            state_columns = {row[1] for row in connection.execute("PRAGMA table_info(tau_retail_feedback_states)")}
            if "initial_projection_hash" not in state_columns:
                connection.execute(
                    "ALTER TABLE tau_retail_feedback_states ADD COLUMN initial_projection_hash TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tau_retail_feedback_receipts (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id, action_id)
                )"""
            )

    def claim(self, turn_id: str, request_hash: str) -> tuple[str, dict[str, Any] | None]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash,status,result_json FROM actor_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    connection.rollback()
                    return "conflict", None
                if row["status"] == "completed":
                    connection.commit()
                    return "completed", json.loads(row["result_json"])
                connection.commit()
                return "started", None
            connection.execute(
                "INSERT INTO actor_turns(turn_id,request_hash,status) VALUES(?,?,'started')",
                (turn_id, request_hash),
            )
            connection.commit()
        return "claimed", None

    def complete(self, turn_id: str, request_hash: str, result: dict[str, Any]) -> None:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE actor_turns SET status='completed',result_json=? "
                "WHERE turn_id=? AND request_hash=? AND status='started'",
                (encoded, turn_id, request_hash),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("actor turn claim was lost before result persistence")
            connection.commit()
