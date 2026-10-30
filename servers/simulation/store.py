"""Durable, run-scoped SQLite state for simulation orchestration.

This is an internal persistence boundary. It does not expose HTTP, ERPNext, or
email behavior; callers must keep private rows away from agent-facing APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID

from pydantic import BaseModel

from servers.simulation.models import CanonicalEvent, StartScenario


class IdempotencyConflict(RuntimeError):
    """An idempotency key was previously used for different request data."""


class StoreConflict(RuntimeError):
    """A persisted operation conflicts with the current run state."""


class StoreNotFound(LookupError):
    """A run-scoped record does not exist or is outside the requested scope."""


_UNSET = object()


def _json_default(value: Any) -> Any:
    if isinstance(value, (UUID, Decimal, datetime)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decode(value: str) -> Any:
    return json.loads(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    """SQLite-backed run state with explicit generation-scoped records."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if str(db_path) == ":memory:":
            raise ValueError("SQLiteStore requires a durable database path")
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()
        if os.name == "posix":
            self.db_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a writer transaction that serializes competing mutations."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _connection(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with closing(self._connect()) as owned:
                yield owned

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 0),
                    seed INTEGER NOT NULL CHECK(seed >= 0),
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    simulated_time TEXT,
                    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
                    manifest_json TEXT NOT NULL,
                    erp_binding_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    visible_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, generation, case_id)
                );
                CREATE TABLE IF NOT EXISTS actors (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    email TEXT,
                    private_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, generation, actor_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    simulated_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    visibility_json TEXT NOT NULL,
                    canonical_event_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
                    outcome_json TEXT,
                    PRIMARY KEY(run_id, generation, event_id),
                    UNIQUE(run_id, generation, sequence)
                );
                CREATE INDEX IF NOT EXISTS events_pending_idx
                    ON events(run_id, generation, status, sequence);
                CREATE TABLE IF NOT EXISTS outbox (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    outbox_id TEXT NOT NULL,
                    event_id TEXT,
                    actor_id TEXT,
                    private_payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    submission_started INTEGER NOT NULL DEFAULT 0 CHECK(submission_started IN (0,1)),
                    error_type TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','uncertain','completed')),
                    outcome_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, generation, outbox_id),
                    FOREIGN KEY(run_id, generation, event_id)
                        REFERENCES events(run_id, generation, event_id)
                );
                CREATE INDEX IF NOT EXISTS outbox_pending_idx
                    ON outbox(run_id, generation, status, created_at);
                CREATE TABLE IF NOT EXISTS audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    private_record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS private_conversation (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    simulated_time TEXT NOT NULL,
                    from_actor_id TEXT NOT NULL,
                    to_actor_id TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    message_hash TEXT NOT NULL,
                    PRIMARY KEY(run_id, generation, message_id)
                );
                CREATE INDEX IF NOT EXISTS private_conversation_order_idx
                    ON private_conversation(run_id, generation, simulated_time, message_id);
                CREATE INDEX IF NOT EXISTS audit_run_idx
                    ON audit(run_id, generation, audit_id);
                CREATE TRIGGER IF NOT EXISTS audit_no_update
                    BEFORE UPDATE ON audit BEGIN
                    SELECT RAISE(ABORT, 'audit records are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete
                    BEFORE DELETE ON audit BEGIN
                    SELECT RAISE(ABORT, 'audit records are append-only');
                END;
                CREATE TABLE IF NOT EXISTS request_idempotency (
                    scope TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'completed'),
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key_hash)
                );
                CREATE TABLE IF NOT EXISTS company_runtime (
                    company_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('running','paused')),
                    simulated_time TEXT NOT NULL,
                    wall_anchor TEXT NOT NULL,
                    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
                    erp_binding_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS background_provider_controls (
                    company_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    recovery_requested INTEGER NOT NULL DEFAULT 0,
                    recovery_operation_id TEXT, recovery_key TEXT, last_error TEXT,
                    updated_at TEXT NOT NULL, PRIMARY KEY(company_id,provider_id)
                );
                CREATE TABLE IF NOT EXISTS company_experiments (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    company_id TEXT NOT NULL REFERENCES company_runtime(company_id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS background_initialization_requests (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id)
                );
                CREATE TABLE IF NOT EXISTS background_provider_state (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    seed INTEGER NOT NULL CHECK(seed >= 0),
                    started_at TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id)
                );
                CREATE TABLE IF NOT EXISTS background_operations (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started','completed')),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(company_id,provider_id,operation_id)
                );
                CREATE TABLE IF NOT EXISTS background_operation_recovery_resolutions (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL CHECK(provider_id='coffeebench'),
                    previous_operation_id TEXT NOT NULL,
                    previous_request_json TEXT NOT NULL,
                    previous_request_hash TEXT NOT NULL,
                    recovery_operation_id TEXT NOT NULL,
                    recovery_request_json TEXT NOT NULL,
                    recovery_request_hash TEXT NOT NULL,
                    confirmed_cursor TEXT NOT NULL,
                    checkpoint_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,previous_operation_id),
                    UNIQUE(company_id,provider_id,recovery_operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS background_recovery_no_update
                    BEFORE UPDATE ON background_operation_recovery_resolutions BEGIN
                    SELECT RAISE(ABORT, 'background recovery resolutions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_recovery_no_delete
                    BEFORE DELETE ON background_operation_recovery_resolutions BEGIN
                    SELECT RAISE(ABORT, 'background recovery resolutions are append-only');
                END;
                CREATE TABLE IF NOT EXISTS background_intents (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,intent_id)
                );
                CREATE TABLE IF NOT EXISTS background_company_ticks (
                    company_id TEXT NOT NULL,
                    tick_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started','completed')),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(company_id,tick_id)
                );
                CREATE TABLE IF NOT EXISTS background_provider_pending (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id)
                );
                CREATE TABLE IF NOT EXISTS background_provider_recovery_archive (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL CHECK(provider_id='coffeebench'),
                    previous_operation_id TEXT NOT NULL,
                    previous_request_json TEXT NOT NULL,
                    previous_result_json TEXT,
                    previous_created_at TEXT NOT NULL,
                    recovery_operation_id TEXT NOT NULL,
                    recovery_request_json TEXT NOT NULL,
                    confirmed_cursor TEXT NOT NULL,
                    checkpoint_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,previous_operation_id),
                    UNIQUE(company_id,provider_id,recovery_operation_id)
                );
                CREATE TABLE IF NOT EXISTS background_provider_result_receipts (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    coverage_until TEXT,
                    next_due TEXT,
                    result_sha256 TEXT NOT NULL,
                    source_event_ids_json TEXT NOT NULL,
                    evidence_origin TEXT NOT NULL DEFAULT 'coordinator_result',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS background_provider_result_receipts_no_update
                    BEFORE UPDATE ON background_provider_result_receipts BEGIN
                    SELECT RAISE(ABORT, 'background provider result receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_provider_result_receipts_no_delete
                    BEFORE DELETE ON background_provider_result_receipts BEGIN
                    SELECT RAISE(ABORT, 'background provider result receipts are append-only');
                END;
                CREATE TABLE IF NOT EXISTS background_provider_result_acknowledgements (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,operation_id),
                    FOREIGN KEY(company_id,provider_id,operation_id)
                        REFERENCES background_provider_result_receipts(company_id,provider_id,operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS background_provider_result_ack_no_update
                    BEFORE UPDATE ON background_provider_result_acknowledgements BEGIN
                    SELECT RAISE(ABORT, 'background provider result acknowledgements are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_provider_result_ack_no_delete
                    BEFORE DELETE ON background_provider_result_acknowledgements BEGIN
                    SELECT RAISE(ABORT, 'background provider result acknowledgements are append-only');
                END;
                CREATE TABLE IF NOT EXISTS background_provider_result_coverage (
                    company_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    coverage_until TEXT NOT NULL,
                    source_identity_sha256 TEXT NOT NULL,
                    gap_event_ids_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,provider_id,operation_id,coverage_until),
                    FOREIGN KEY(company_id,provider_id,operation_id)
                        REFERENCES background_provider_result_receipts(company_id,provider_id,operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS background_provider_result_coverage_no_update
                    BEFORE UPDATE ON background_provider_result_coverage BEGIN
                    SELECT RAISE(ABORT, 'background provider result coverage is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_provider_result_coverage_no_delete
                    BEFORE DELETE ON background_provider_result_coverage BEGIN
                    SELECT RAISE(ABORT, 'background provider result coverage is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_provider_recovery_no_update
                    BEFORE UPDATE ON background_provider_recovery_archive BEGIN
                    SELECT RAISE(ABORT, 'background provider recovery archives are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS background_provider_recovery_no_delete
                    BEFORE DELETE ON background_provider_recovery_archive BEGIN
                    SELECT RAISE(ABORT, 'background provider recovery archives are append-only');
                END;
                CREATE TABLE IF NOT EXISTS company_source_events (
                    company_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    simulated_time TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    visibility_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,event_id)
                );
                CREATE TABLE IF NOT EXISTS company_finance_receipts (
                    company_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    doctype TEXT NOT NULL,
                    document_name TEXT NOT NULL,
                    created INTEGER NOT NULL CHECK(created IN (0,1)),
                    as_of TEXT NOT NULL,
                    PRIMARY KEY(company_id,source_id)
                );
                CREATE TABLE IF NOT EXISTS company_finance_source_intents (
                    company_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,source_id)
                );
                CREATE TABLE IF NOT EXISTS company_source_event_targets (
                    company_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    PRIMARY KEY(company_id,event_id,run_id,generation),
                    FOREIGN KEY(company_id,event_id) REFERENCES company_source_events(company_id,event_id)
                );
                CREATE TABLE IF NOT EXISTS company_due_obligation_observations (
                    company_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    doctype TEXT NOT NULL,
                    document_name TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    modified TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,observation_id)
                );
                """
            )
            self._migrate_existing_schema(connection)
            outbox_columns = {row["name"] for row in connection.execute("PRAGMA table_info(outbox)")}
            if "completed_at" not in outbox_columns:
                connection.execute("ALTER TABLE outbox ADD COLUMN completed_at TEXT")
            conversation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(private_conversation)")}
            if "observed_at" not in conversation_columns:
                connection.execute("ALTER TABLE private_conversation ADD COLUMN observed_at TEXT")
            pending_columns = {row["name"] for row in connection.execute("PRAGMA table_info(background_provider_pending)")}
            if "result_json" not in pending_columns:
                connection.execute("ALTER TABLE background_provider_pending ADD COLUMN result_json TEXT")
            receipt_columns = {row["name"] for row in connection.execute("PRAGMA table_info(background_provider_result_receipts)")}
            if "evidence_origin" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE background_provider_result_receipts "
                    "ADD COLUMN evidence_origin TEXT NOT NULL DEFAULT 'coordinator_result'"
                )
            if "coverage_until" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE background_provider_result_receipts ADD COLUMN coverage_until TEXT"
                )

    @staticmethod
    def _create_outbox_table(connection: sqlite3.Connection, *, table_name: str = "outbox") -> None:
        connection.execute(
            f"""CREATE TABLE {table_name} (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                generation INTEGER NOT NULL,
                outbox_id TEXT NOT NULL,
                event_id TEXT,
                actor_id TEXT,
                private_payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                submission_started INTEGER NOT NULL DEFAULT 0 CHECK(submission_started IN (0,1)),
                error_type TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending','uncertain','completed')),
                outcome_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id,generation,outbox_id),
                FOREIGN KEY(run_id,generation,event_id)
                    REFERENCES events(run_id,generation,event_id)
            )"""
        )

    def _migrate_existing_schema(self, connection: sqlite3.Connection) -> None:
        """Add fields without discarding persisted development records."""
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
        if "canonical_event_json" not in event_columns:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("ALTER TABLE events ADD COLUMN canonical_event_json TEXT NOT NULL DEFAULT '{}' ")
                rows = connection.execute("SELECT * FROM events").fetchall()
                for row in rows:
                    canonical = {
                        "event_id": row["event_id"], "run_id": row["run_id"], "scenario_id": "",
                        "simulated_time": row["simulated_time"], "event_type": row["event_type"],
                        "source": row["source"], "entity_type": "", "entity_id": "",
                        "related_entities": [], "payload": _decode(row["payload_json"]),
                        "visibility": _decode(row["visibility_json"]),
                    }
                    connection.execute(
                        "UPDATE events SET canonical_event_json=? WHERE run_id=? AND generation=? AND event_id=?",
                        (_json(canonical), row["run_id"], row["generation"], row["event_id"]),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        outbox_columns = {row["name"] for row in connection.execute("PRAGMA table_info(outbox)")}
        outbox_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'"
        ).fetchone()
        if outbox_sql_row and "uncertain" not in (outbox_sql_row["sql"] or ""):
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("ALTER TABLE outbox RENAME TO outbox_legacy")
                self._create_outbox_table(connection)
                submission_expr = "submission_started" if "submission_started" in outbox_columns else "0"
                error_expr = "error_type" if "error_type" in outbox_columns else "NULL"
                connection.execute(
                    f"""INSERT INTO outbox(run_id,generation,outbox_id,event_id,actor_id,
                        private_payload_json,payload_hash,submission_started,error_type,status,
                        outcome_json,created_at)
                        SELECT run_id,generation,outbox_id,event_id,actor_id,private_payload_json,
                        payload_hash,{submission_expr},{error_expr},status,outcome_json,created_at
                        FROM outbox_legacy"""
                )
                connection.execute("DROP TABLE outbox_legacy")
                connection.execute("CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox(run_id,generation,status,created_at)")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        else:
            if "submission_started" not in outbox_columns:
                connection.execute("ALTER TABLE outbox ADD COLUMN submission_started INTEGER NOT NULL DEFAULT 0")
            if "error_type" not in outbox_columns:
                connection.execute("ALTER TABLE outbox ADD COLUMN error_type TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox(run_id,generation,status,created_at)")

    @staticmethod
    def _run_id(run_id: UUID | str) -> str:
        return str(run_id)

    @staticmethod
    def _require_generation(connection: sqlite3.Connection, run_id: str, generation: int) -> None:
        row = connection.execute("SELECT generation FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreNotFound("run not found")
        if int(row["generation"]) != generation:
            raise StoreConflict("operation targets a stale run generation")

    def create_run(
        self,
        run_id: UUID | str,
        scenario: StartScenario,
        *,
        manifest: dict[str, Any] | None = None,
        erp_binding: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        rid = self._run_id(run_id)
        data = scenario.model_dump(mode="json")
        with self._write(connection) as conn:
            conn.execute(
                """INSERT INTO runs(run_id,scenario_id,generation,seed,agent_id,status,
                    simulated_time,paused,manifest_json,erp_binding_json)
                    VALUES(?,?,0,?,?, 'running',NULL,0,?,?)""",
                (rid, data["scenario_id"], data["seed"], data["agent_id"], _json(manifest or {}), _json(erp_binding or {})),
            )
            return self.get_run(rid, connection=conn)

    def get_run(self, run_id: UUID | str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self._connection(connection) as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (self._run_id(run_id),)).fetchone()
            if row is None:
                raise StoreNotFound("run not found")
            return {
                "run_id": row["run_id"],
                "scenario_id": row["scenario_id"],
                "generation": row["generation"],
                "seed": row["seed"],
                "agent_id": row["agent_id"],
                "status": row["status"],
                "simulated_time": row["simulated_time"],
                "paused": bool(row["paused"]),
                "manifest": _decode(row["manifest_json"]),
                "erp_binding": _decode(row["erp_binding_json"]),
            }

    def list_active_runs(self, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        """Return identifiers and generations for currently running simulations."""
        with self._connection(connection) as conn:
            rows = conn.execute(
                "SELECT run_id, generation FROM runs WHERE status='running' ORDER BY run_id"
            ).fetchall()
            return [{"run_id": row["run_id"], "generation": row["generation"]} for row in rows]

    def get_company(self, company_id: str = "company", *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        with self._connection(connection) as conn:
            row = conn.execute("SELECT * FROM company_runtime WHERE company_id=?", (company_id,)).fetchone()
            if row is None:
                return None
            return {"company_id": row["company_id"], "status": row["status"],
                    "simulated_time": row["simulated_time"], "wall_anchor": row["wall_anchor"],
                    "paused": bool(row["paused"]), "erp_binding": _decode(row["erp_binding_json"]),
                    "updated_at": row["updated_at"]}

    def start_company(self, company_id: str, simulated_time: datetime | str, wall_anchor: datetime | str,
                      erp_binding: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        simulated_value = simulated_time.isoformat() if isinstance(simulated_time, datetime) else simulated_time
        anchor_value = wall_anchor.isoformat() if isinstance(wall_anchor, datetime) else wall_anchor
        with self._write(connection) as conn:
            conn.execute("""INSERT INTO company_runtime(company_id,status,simulated_time,wall_anchor,paused,erp_binding_json,updated_at)
                VALUES(?,'running',?,?,0,?,?) ON CONFLICT(company_id) DO NOTHING""",
                (company_id, simulated_value, anchor_value, _json(erp_binding), _now()))
            return self.get_company(company_id, connection=conn)

    def update_company(self, company_id: str, *, status: str | None = None,
                       simulated_time: datetime | str | None = None,
                       wall_anchor: datetime | str | None = None, paused: bool | None = None,
                       connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        updates = {"status": status, "simulated_time": simulated_time, "wall_anchor": wall_anchor,
                   "paused": int(paused) if paused is not None else None}
        values = {key: value for key, value in updates.items() if value is not None}
        if not values:
            raise ValueError("company update has no fields")
        for key in ("simulated_time", "wall_anchor"):
            if isinstance(values.get(key), datetime):
                values[key] = values[key].isoformat()
        with self._write(connection) as conn:
            if conn.execute("SELECT 1 FROM company_runtime WHERE company_id=?", (company_id,)).fetchone() is None:
                raise StoreNotFound("company not found")
            values["updated_at"] = _now()
            conn.execute("UPDATE company_runtime SET " + ",".join(f"{key}=?" for key in values) + " WHERE company_id=?",
                         (*values.values(), company_id))
            return self.get_company(company_id, connection=conn)

    def link_company_experiment(self, company_id: str, run_id: UUID | str,
                                *, connection: sqlite3.Connection | None = None) -> None:
        with self._write(connection) as conn:
            conn.execute("INSERT INTO company_experiments(run_id,company_id,created_at) VALUES(?,?,?) "
                         "ON CONFLICT(run_id) DO NOTHING", (self._run_id(run_id), company_id, _now()))
            row = conn.execute("SELECT company_id FROM company_experiments WHERE run_id=?", (self._run_id(run_id),)).fetchone()
            if row is None or row["company_id"] != company_id:
                raise StoreConflict("experiment already belongs to another company")

    def company_experiments(self, company_id: str = "company", *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._connection(connection) as conn:
            rows = conn.execute("""SELECT r.run_id,r.scenario_id,r.generation,r.agent_id,r.status,r.simulated_time,r.paused
                FROM company_experiments x JOIN runs r ON r.run_id=x.run_id
                WHERE x.company_id=? ORDER BY x.created_at,r.run_id""", (company_id,)).fetchall()
            return [{"experiment_id": row["run_id"], "scenario_id": row["scenario_id"],
                     "generation": row["generation"], "agent_id": row["agent_id"], "status": row["status"],
                     "simulated_time": row["simulated_time"], "paused": bool(row["paused"])} for row in rows]

    def background_provider_control(self, provider_id: str, company_id: str = "company") -> dict[str, Any]:
        with self._connection(None) as db:
            row = db.execute("SELECT * FROM background_provider_controls WHERE company_id=? AND provider_id=?",
                             (company_id, provider_id)).fetchone()
        return ({**dict(row), "enabled": bool(row["enabled"]),
                 "recovery_requested": bool(row["recovery_requested"])} if row else
                {"enabled": True, "recovery_requested": False, "recovery_operation_id": None,
                 "recovery_key": None, "last_error": None})

    def set_background_provider_enabled(self, provider_id: str, enabled: bool, key: str,
                                        company_id: str = "company", *, request_recovery: bool = False) -> dict[str, Any]:
        def operation(db):
            db.execute("""INSERT INTO background_provider_controls
                (company_id,provider_id,enabled,recovery_requested,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(company_id,provider_id) DO UPDATE SET enabled=excluded.enabled,
                recovery_requested=excluded.recovery_requested,last_error=NULL,updated_at=excluded.updated_at""",
                (company_id, provider_id, int(enabled), int(enabled and request_recovery), _now()))
            return {"provider_id": provider_id, "enabled": enabled}
        return self.execute_idempotent(f"background-control:{company_id}:{provider_id}", key,
                                       {"enabled": enabled}, operation)

    def begin_background_recovery(self, provider_id: str, operation_id: str,
                                  company_id: str = "company") -> dict[str, Any]:
        with self.transaction() as db:
            db.execute("""UPDATE background_provider_controls SET
                recovery_operation_id=COALESCE(recovery_operation_id,?),
                recovery_key=COALESCE(recovery_key,?),updated_at=?
                WHERE company_id=? AND provider_id=?""",
                (operation_id, "background-control-recovery:" + operation_id, _now(), company_id, provider_id))
        return self.background_provider_control(provider_id, company_id)

    def finish_background_recovery(self, provider_id: str, error_code: str | None = None,
                                   company_id: str = "company") -> None:
        with self.transaction() as db:
            db.execute("""UPDATE background_provider_controls SET recovery_requested=0,
                enabled=CASE WHEN ? IS NULL THEN enabled ELSE 0 END,last_error=?,
                recovery_operation_id=CASE WHEN ? IS NULL THEN NULL ELSE recovery_operation_id END,
                recovery_key=CASE WHEN ? IS NULL THEN NULL ELSE recovery_key END,updated_at=?
                WHERE company_id=? AND provider_id=?""",
                (error_code, error_code, error_code, error_code, _now(), company_id, provider_id))

    def get_background_provider_state(self, provider_id: str, company_id: str = "company") -> dict[str, Any] | None:
        with self._connection(None) as conn:
            row = conn.execute(
                "SELECT * FROM background_provider_state WHERE company_id=? AND provider_id=?",
                (company_id, provider_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "company_id": row["company_id"], "provider_id": row["provider_id"],
            "upstream_commit": row["upstream_commit"], "seed": row["seed"],
            "started_at": row["started_at"], "cursor": row["cursor"],
            "snapshot": _decode(row["snapshot_json"]), "updated_at": row["updated_at"],
        }

    def get_background_operation(
        self, provider_id: str, operation_id: str, request: dict[str, Any],
        company_id: str = "company",
    ) -> dict[str, Any]:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT request_hash,status,result_json,created_at FROM background_operations "
                "WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
        if row is None:
            raise StoreNotFound("background operation not found")
        request_hash = _digest(request)
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("background operation request does not match its durable claim")
        return {
            "operation_id": operation_id, "request_hash": request_hash,
            "status": row["status"],
            "result": _decode(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
        }

    def list_completed_background_operations(
        self, provider_id: str, company_id: str = "company",
    ) -> list[dict[str, Any]]:
        """Read completed operation results for audited source reconciliation."""
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT operation_id,result_json,created_at,completed_at FROM background_operations "
                "WHERE company_id=? AND provider_id=? AND status='completed' "
                "ORDER BY completed_at,operation_id",
                (company_id, provider_id),
            ).fetchall()
        return [{"operation_id": row["operation_id"], "result": _decode(row["result_json"]),
                 "created_at": row["created_at"], "completed_at": row["completed_at"]} for row in rows]

    def get_background_recovery_resolution(
        self, previous_operation_id: str, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT * FROM background_operation_recovery_resolutions "
                "WHERE company_id=? AND provider_id='coffeebench' AND previous_operation_id=?",
                (company_id, previous_operation_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "previous_operation_id": row["previous_operation_id"],
            "previous_request": _decode(row["previous_request_json"]),
            "previous_request_hash": row["previous_request_hash"],
            "recovery_operation_id": row["recovery_operation_id"],
            "recovery_request": _decode(row["recovery_request_json"]),
            "recovery_request_hash": row["recovery_request_hash"],
            "confirmed_cursor": row["confirmed_cursor"],
            "checkpoint_sha256": row["checkpoint_sha256"],
            "created_at": row["created_at"],
        }

    def get_background_recovery_for_operation(
        self, operation_id: str, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT previous_operation_id FROM background_operation_recovery_resolutions "
                "WHERE company_id=? AND provider_id='coffeebench' AND recovery_operation_id=?",
                (company_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_background_recovery_resolution(row["previous_operation_id"], company_id)

    def prepare_coffee_background_recovery(
        self, previous_request: dict[str, Any], recovery_request: dict[str, Any], *,
        confirmed_cursor: str, checkpoint_sha256: str, company_id: str = "company",
    ) -> dict[str, Any]:
        """Atomically retain a superseded claim and reserve its recovery attempt."""
        previous_id = str(previous_request["operation_id"])
        recovery_id = str(recovery_request["operation_id"])
        previous_json, recovery_json = _json(previous_request), _json(recovery_request)
        previous_hash, recovery_hash = _digest(previous_request), _digest(recovery_request)
        if previous_request.get("provider_id") != "coffeebench" or recovery_request.get("provider_id") != "coffeebench":
            raise StoreConflict("CoffeeBench recovery request has an invalid provider")
        previous_without_id = {key: value for key, value in previous_request.items() if key != "operation_id"}
        recovery_without_id = {key: value for key, value in recovery_request.items() if key != "operation_id"}
        if previous_without_id != recovery_without_id or previous_id == recovery_id:
            raise StoreConflict("CoffeeBench recovery must preserve the exact target and binding under a fresh operation ID")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM background_operation_recovery_resolutions "
                "WHERE company_id=? AND provider_id='coffeebench' AND previous_operation_id=?",
                (company_id, previous_id),
            ).fetchone()
            if existing is not None:
                if (existing["previous_request_json"] != previous_json
                        or existing["recovery_request_json"] != recovery_json
                        or existing["confirmed_cursor"] != confirmed_cursor
                        or existing["checkpoint_sha256"] != checkpoint_sha256):
                    raise StoreConflict("CoffeeBench recovery identity was reused with different evidence")
                operation = connection.execute(
                    "SELECT request_hash,status,result_json,created_at FROM background_operations "
                    "WHERE company_id=? AND provider_id='coffeebench' AND operation_id=?",
                    (company_id, recovery_id),
                ).fetchone()
                if operation is None or operation["request_hash"] != recovery_hash:
                    raise StoreConflict("CoffeeBench recovery operation reservation is missing or inconsistent")
                return {"status": operation["status"], "result": _decode(operation["result_json"])
                        if operation["result_json"] else None, "created_at": operation["created_at"]}

            state = connection.execute(
                "SELECT cursor,snapshot_json FROM background_provider_state "
                "WHERE company_id=? AND provider_id='coffeebench'", (company_id,),
            ).fetchone()
            if state is None or state["cursor"] != confirmed_cursor:
                raise StoreConflict("CoffeeBench confirmed provider cursor changed during recovery preparation")
            snapshot = _decode(state["snapshot_json"])
            checkpoint = snapshot.get("checkpoint") if isinstance(snapshot, dict) else None
            if not isinstance(checkpoint, dict) or checkpoint.get("sha256") != checkpoint_sha256:
                raise StoreConflict("CoffeeBench confirmed checkpoint changed during recovery preparation")
            old = connection.execute(
                "SELECT request_hash,status FROM background_operations "
                "WHERE company_id=? AND provider_id='coffeebench' AND operation_id=?",
                (company_id, previous_id),
            ).fetchone()
            if old is None or old["request_hash"] != previous_hash:
                raise StoreConflict("CoffeeBench previous operation does not match its durable claim")
            if old["status"] != "started":
                raise StoreConflict("CoffeeBench previous operation is no longer uncertain")
            reused = connection.execute(
                "SELECT 1 FROM background_operations WHERE company_id=? AND provider_id='coffeebench' AND operation_id=?",
                (company_id, recovery_id),
            ).fetchone()
            if reused is not None:
                raise StoreConflict("CoffeeBench recovery operation ID is already in use")
            now = _now()
            connection.execute(
                "INSERT INTO background_operation_recovery_resolutions(company_id,provider_id,previous_operation_id,"
                "previous_request_json,previous_request_hash,recovery_operation_id,recovery_request_json,"
                "recovery_request_hash,confirmed_cursor,checkpoint_sha256,created_at) "
                "VALUES(?,'coffeebench',?,?,?,?,?,?,?,?,?)",
                (company_id, previous_id, previous_json, previous_hash, recovery_id, recovery_json,
                 recovery_hash, confirmed_cursor, checkpoint_sha256, now),
            )
            connection.execute(
                "INSERT INTO background_operations(company_id,provider_id,operation_id,request_hash,status,created_at) "
                "VALUES(?,'coffeebench',?,?,'started',?)",
                (company_id, recovery_id, recovery_hash, now),
            )
        return {"status": "started", "result": None, "created_at": now}

    def is_coffee_recovery_operation(self, operation_id: str, company_id: str = "company") -> bool:
        return self.get_background_recovery_for_operation(operation_id, company_id) is not None

    def claim_background_operation(
        self, provider_id: str, operation_id: str, request: dict[str, Any],
        company_id: str = "company",
    ) -> dict[str, Any]:
        if not provider_id or not operation_id:
            raise ValueError("background operation identity is required")
        request_hash = _digest(request)
        with self.transaction() as connection:
            resolved = connection.execute(
                "SELECT 1 FROM background_operation_recovery_resolutions "
                "WHERE company_id=? AND provider_id=? AND previous_operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
            if resolved is not None:
                raise StoreConflict("background operation has been superseded by an audited recovery attempt")
            row = connection.execute(
                "SELECT request_hash,status,result_json,created_at FROM background_operations "
                "WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict("background operation ID reused with different request data")
                return {"status": row["status"],
                        "result": _decode(row["result_json"]) if row["result_json"] else None,
                        "created_at": row["created_at"]}
            created_at = _now()
            connection.execute(
                "INSERT INTO background_operations(company_id,provider_id,operation_id,request_hash,status,created_at) "
                "VALUES(?,?,?,?,'started',?)",
                (company_id, provider_id, operation_id, request_hash, created_at),
            )
        return {"status": "claimed", "result": None, "request_hash": request_hash,
                "created_at": created_at}

    def complete_background_operation(
        self,
        provider_id: str,
        operation_id: str,
        request: dict[str, Any],
        *,
        upstream_commit: str,
        seed: int,
        started_at: datetime | str,
        cursor: datetime | str,
        snapshot: dict[str, Any],
        result: dict[str, Any],
        intents: list[dict[str, Any]],
        company_id: str = "company",
    ) -> dict[str, Any]:
        request_hash = _digest(request)
        started_value = started_at.isoformat() if isinstance(started_at, datetime) else str(started_at)
        cursor_value = cursor.isoformat() if isinstance(cursor, datetime) else str(cursor)
        result_json = _json(result)
        with self.transaction() as connection:
            operation = connection.execute(
                "SELECT request_hash,status,result_json FROM background_operations "
                "WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
            if operation is None or operation["request_hash"] != request_hash:
                raise StoreConflict("background operation claim is missing or does not match")
            if operation["status"] == "completed":
                if operation["result_json"] != result_json:
                    raise StoreConflict("background operation completed with a different result")
                return _decode(operation["result_json"])

            connection.execute(
                "INSERT INTO background_provider_state(company_id,provider_id,upstream_commit,seed,started_at,cursor,snapshot_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(company_id,provider_id) DO UPDATE SET "
                "upstream_commit=excluded.upstream_commit,seed=excluded.seed,started_at=excluded.started_at, "
                "cursor=excluded.cursor,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
                (company_id, provider_id, upstream_commit, int(seed), started_value,
                 cursor_value, _json(snapshot), _now()),
            )
            for intent in intents:
                intent_id = str(intent["id"])
                encoded = _json(intent)
                payload_hash = _digest(intent)
                prior = connection.execute(
                    "SELECT payload_hash FROM background_intents WHERE company_id=? AND provider_id=? AND intent_id=?",
                    (company_id, provider_id, intent_id),
                ).fetchone()
                if prior is not None:
                    if prior["payload_hash"] != payload_hash:
                        raise StoreConflict("native source intent ID reused with different source facts")
                    continue
                connection.execute(
                    "INSERT INTO background_intents(company_id,provider_id,intent_id,intent_json,payload_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (company_id, provider_id, intent_id, encoded, payload_hash, _now()),
                )
            connection.execute(
                "UPDATE background_operations SET status='completed',result_json=?,completed_at=? "
                "WHERE company_id=? AND provider_id=? AND operation_id=? AND request_hash=? AND status='started'",
                (result_json, _now(), company_id, provider_id, operation_id, request_hash),
            )
        return result

    def list_background_intents(
        self, provider_id: str, company_id: str = "company",
    ) -> list[dict[str, Any]]:
        with self._connection(None) as conn:
            rows = conn.execute(
                "SELECT intent_json FROM background_intents WHERE company_id=? AND provider_id=? ORDER BY created_at,intent_id",
                (company_id, provider_id),
            ).fetchall()
        return [_decode(row["intent_json"]) for row in rows]

    def record_company_source_events(self, events: list[dict[str, Any]], company_id: str = "company",
                                     targets: dict[str, list[tuple[str, int]]] | None = None) -> None:
        """Persist provider event identities and safe source facts once."""
        with self.transaction() as connection:
            for event in events:
                event_id = str(event["event_id"])
                values = (company_id, event_id, str(event["source"]), str(event["upstream_commit"]),
                          str(event["event_type"]), str(event["simulated_time"]),
                          str(event["entity_type"]), str(event["entity_id"]),
                          _json(event.get("payload", {})), _json(event.get("visibility", [])), _now())
                prior = connection.execute(
                    "SELECT source,upstream_commit,event_type,simulated_time,entity_type,entity_id,payload_json,visibility_json "
                    "FROM company_source_events WHERE company_id=? AND event_id=?", (company_id, event_id),
                ).fetchone()
                expected = values[2:10]
                if prior is not None:
                    if tuple(prior) != expected:
                        raise StoreConflict("company source event ID reused with different facts")
                    continue
                connection.execute(
                    "INSERT INTO company_source_events(company_id,event_id,source,upstream_commit,event_type,simulated_time,"
                    "entity_type,entity_id,payload_json,visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", values,
                )
                for run_id, generation in (targets or {}).get(event_id, []):
                    connection.execute(
                        "INSERT INTO company_source_event_targets(company_id,event_id,run_id,generation) "
                        "VALUES(?,?,?,?)", (company_id, event_id, self._run_id(run_id), int(generation)),
                    )

    def company_source_event_targets(self, event_id: str, company_id: str = "company") -> list[dict[str, Any]]:
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT run_id,generation FROM company_source_event_targets "
                "WHERE company_id=? AND event_id=? ORDER BY run_id,generation", (company_id, event_id),
            ).fetchall()
        return [{"run_id": row[0], "generation": row[1]} for row in rows]

    def list_company_source_events(self, company_id: str = "company") -> list[dict[str, Any]]:
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT event_id,source,upstream_commit,event_type,simulated_time,entity_type,entity_id,payload_json,visibility_json "
                "FROM company_source_events WHERE company_id=? ORDER BY simulated_time,event_id", (company_id,),
            ).fetchall()
        return [{"event_id": row[0], "source": row[1], "upstream_commit": row[2], "event_type": row[3],
                 "simulated_time": row[4], "entity_type": row[5], "entity_id": row[6],
                 "payload": _decode(row[7]), "visibility": _decode(row[8])} for row in rows]

    def company_source_event_created_at(self, event_id: str, company_id: str = "company") -> str | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT created_at FROM company_source_events WHERE company_id=? AND event_id=?",
                (company_id, event_id),
            ).fetchone()
        return None if row is None else row["created_at"]

    def record_company_finance_receipts(self, receipts: list[dict[str, Any]], as_of: str,
                                        company_id: str = "company") -> None:
        with self.transaction() as connection:
            for receipt in receipts:
                source_id = str(receipt["source_id"])
                values = (company_id, source_id, str(receipt["doctype"]), str(receipt["name"]),
                          int(bool(receipt["created"])), str(as_of))
                prior = connection.execute(
                    "SELECT doctype,document_name,created FROM company_finance_receipts "
                    "WHERE company_id=? AND source_id=?", (company_id, source_id),
                ).fetchone()
                if prior is not None:
                    if tuple(prior) != values[2:5]:
                        raise StoreConflict("finance source receipt identity changed")
                    continue
                connection.execute(
                    "INSERT INTO company_finance_receipts(company_id,source_id,doctype,document_name,created,as_of) "
                    "VALUES(?,?,?,?,?,?)", values,
                )

    def record_company_finance_source_intents(self, intents: list[dict[str, Any]],
                                              company_id: str = "company") -> None:
        with self.transaction() as connection:
            for intent in intents:
                source_id = str(intent["id"])
                encoded = _json(intent)
                payload_hash = _digest(intent)
                prior = connection.execute(
                    "SELECT payload_hash FROM company_finance_source_intents WHERE company_id=? AND source_id=?",
                    (company_id, source_id),
                ).fetchone()
                if prior is not None:
                    if prior[0] != payload_hash:
                        raise StoreConflict("finance source intent ID reused with different facts")
                    continue
                connection.execute(
                    "INSERT INTO company_finance_source_intents(company_id,source_id,intent_json,payload_hash,created_at) "
                    "VALUES(?,?,?,?,?)", (company_id, source_id, encoded, payload_hash, _now()),
                )

    def has_company_finance_source_intent(
        self, source_id: str, payload_hash: str, company_id: str = "company",
    ) -> bool:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT payload_hash FROM company_finance_source_intents "
                "WHERE company_id=? AND source_id=?", (company_id, source_id),
            ).fetchone()
        return row is not None and row["payload_hash"] == payload_hash

    def list_unreceipted_company_finance_source_intents(self, company_id: str = "company") -> list[dict[str, Any]]:
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT i.intent_json FROM company_finance_source_intents i "
                "LEFT JOIN company_finance_receipts r ON r.company_id=i.company_id AND r.source_id=i.source_id "
                "WHERE i.company_id=? AND r.source_id IS NULL ORDER BY i.created_at,i.source_id", (company_id,),
            ).fetchall()
        return [_decode(row[0]) for row in rows]

    def list_company_finance_receipts(self, company_id: str = "company") -> list[dict[str, Any]]:
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT source_id,doctype,document_name,created,as_of FROM company_finance_receipts "
                "WHERE company_id=? ORDER BY as_of,source_id", (company_id,),
            ).fetchall()
        return [{"source_id": row[0], "doctype": row[1], "name": row[2],
                 "created": bool(row[3]), "as_of": row[4]} for row in rows]

    def record_company_due_obligation_observations(self, obligations: list[dict[str, Any]], observed_at: str,
                                                    company_id: str = "company") -> None:
        with self.transaction() as connection:
            for obligation in obligations:
                doctype = str(obligation["doctype"])
                document_name = str(obligation["name"])
                due_date = str(obligation["due_date"])
                modified = str(obligation["modified"])
                observation_id = _digest({"doctype": doctype, "name": document_name,
                                          "due_date": due_date, "modified": modified})
                connection.execute(
                    "INSERT INTO company_due_obligation_observations(company_id,observation_id,doctype,document_name,"
                    "due_date,modified,observed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,observation_id) DO NOTHING",
                    (company_id, observation_id, doctype, document_name, due_date, modified, observed_at),
                )

    def list_company_due_obligation_observations(self, company_id: str = "company") -> list[dict[str, Any]]:
        with self._connection(None) as connection:
            rows = connection.execute(
                "SELECT observation_id,doctype,document_name,due_date,modified,observed_at "
                "FROM company_due_obligation_observations WHERE company_id=? ORDER BY observed_at,due_date,document_name",
                (company_id,),
            ).fetchall()
        return [{"observation_id": row[0], "doctype": row[1], "name": row[2], "due_date": row[3],
                 "modified": row[4], "observed_at": row[5]} for row in rows]

    def get_background_initialization_request(self, provider_id: str, company_id: str = "company") -> dict[str, Any] | None:
        """Read the coordinator-owned immutable provider origin, without reinitializing."""
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT request_json FROM background_initialization_requests WHERE company_id=? AND provider_id=?",
                (company_id, provider_id),
            ).fetchone()
        return _decode(row["request_json"]) if row else None

    def background_initialization_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Pin provider initialization before RPC, including its original clock anchor."""
        company_id, provider_id = request["company_id"], request["provider_id"]
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT request_json FROM background_initialization_requests WHERE company_id=? AND provider_id=?",
                (company_id, provider_id),
            ).fetchone()
            if row is not None:
                original = _decode(row["request_json"])
                if {**request, "started_at": original["started_at"]} != original:
                    raise IdempotencyConflict("active company provider initialization configuration changed")
                return original
            connection.execute(
                "INSERT INTO background_initialization_requests(company_id,provider_id,request_json,created_at) VALUES(?,?,?,?)",
                (company_id, provider_id, _json(request), _now()),
            )
        return request

    def claim_background_tick(self, tick_id: str, request: dict[str, Any], company_id: str = "company") -> dict[str, Any]:
        request_hash = _digest(request)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT request_hash,status,result_json FROM background_company_ticks WHERE company_id=? AND tick_id=?",
                (company_id, tick_id),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict("company background tick ID reused with different request data")
                return {"status": row["status"],
                        "result": _decode(row["result_json"]) if row["result_json"] else None}
            connection.execute(
                "INSERT INTO background_company_ticks(company_id,tick_id,request_hash,status,created_at) "
                "VALUES(?,?,?,'started',?)", (company_id, tick_id, request_hash, _now()),
            )
        return {"status": "claimed", "result": None, "request_hash": request_hash}

    def complete_background_tick(
        self, tick_id: str, request: dict[str, Any], result: dict[str, Any], company_id: str = "company",
    ) -> dict[str, Any]:
        request_hash = _digest(request)
        encoded = _json(result)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT request_hash,status,result_json FROM background_company_ticks WHERE company_id=? AND tick_id=?",
                (company_id, tick_id),
            ).fetchone()
            if row is None or row["request_hash"] != request_hash:
                raise StoreConflict("company background tick claim is missing or does not match")
            if row["status"] == "completed":
                if row["result_json"] != encoded:
                    raise StoreConflict("company background tick completed with a different result")
                return _decode(row["result_json"])
            connection.execute(
                "UPDATE background_company_ticks SET status='completed',result_json=?,completed_at=? "
                "WHERE company_id=? AND tick_id=? AND request_hash=? AND status='started'",
                (encoded, _now(), company_id, tick_id, request_hash),
            )
        return result

    def get_background_provider_pending(
        self, provider_id: str, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT operation_id,request_json,result_json,created_at FROM background_provider_pending "
                "WHERE company_id=? AND provider_id=?", (company_id, provider_id),
            ).fetchone()
        if row is None:
            return None
        return {"operation_id": row["operation_id"], "request": _decode(row["request_json"]),
                "result": _decode(row["result_json"]) if row["result_json"] else None,
                "created_at": row["created_at"]}

    @staticmethod
    def _background_provider_recovery_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "previous_operation_id": row["previous_operation_id"],
            "previous_request": _decode(row["previous_request_json"]),
            "previous_result": _decode(row["previous_result_json"]) if row["previous_result_json"] else None,
            "previous_created_at": row["previous_created_at"],
            "recovery_operation_id": row["recovery_operation_id"],
            "recovery_request": _decode(row["recovery_request_json"]),
            "confirmed_cursor": row["confirmed_cursor"],
            "checkpoint_sha256": row["checkpoint_sha256"],
            "created_at": row["created_at"],
        }

    def get_background_provider_recovery(
        self, previous_operation_id: str, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT * FROM background_provider_recovery_archive "
                "WHERE company_id=? AND provider_id='coffeebench' AND previous_operation_id=?",
                (company_id, previous_operation_id),
            ).fetchone()
        return None if row is None else self._background_provider_recovery_dict(row)

    def get_background_provider_recovery_for_operation(
        self, recovery_operation_id: str, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT * FROM background_provider_recovery_archive "
                "WHERE company_id=? AND provider_id='coffeebench' AND recovery_operation_id=?",
                (company_id, recovery_operation_id),
            ).fetchone()
        return None if row is None else self._background_provider_recovery_dict(row)

    def prepare_background_provider_recovery(
        self, previous_operation_id: str, recovery_operation_id: str,
        recovery_request: dict[str, Any], *, confirmed_cursor: str,
        checkpoint_sha256: str, company_id: str = "company",
    ) -> dict[str, Any]:
        """Archive a pending Coffee request and atomically replace its retry key."""
        encoded_recovery = _json(recovery_request)
        if recovery_request.get("operation_id") != recovery_operation_id:
            raise StoreConflict("CoffeeBench recovery request does not match its operation identity")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM background_provider_recovery_archive "
                "WHERE company_id=? AND provider_id='coffeebench' AND previous_operation_id=?",
                (company_id, previous_operation_id),
            ).fetchone()
            if existing is not None:
                archived = self._background_provider_recovery_dict(existing)
                if (archived["recovery_operation_id"] != recovery_operation_id
                        or _json(archived["recovery_request"]) != encoded_recovery
                        or archived["confirmed_cursor"] != confirmed_cursor
                        or archived["checkpoint_sha256"] != checkpoint_sha256):
                    raise StoreConflict("CoffeeBench recovery key was reused with different evidence")
                return archived

            pending = connection.execute(
                "SELECT operation_id,request_json,result_json,created_at FROM background_provider_pending "
                "WHERE company_id=? AND provider_id='coffeebench'", (company_id,),
            ).fetchone()
            if pending is None or pending["operation_id"] != previous_operation_id:
                raise StoreConflict("CoffeeBench pending operation changed before recovery was reserved")
            if pending["result_json"] is not None:
                raise StoreConflict("CoffeeBench pending operation already has a durable result")
            previous_request = _decode(pending["request_json"])
            comparable_old = {key: value for key, value in previous_request.items() if key != "operation_id"}
            comparable_new = {key: value for key, value in recovery_request.items() if key != "operation_id"}
            if comparable_old != comparable_new or previous_request.get("provider_id") != "coffeebench":
                raise StoreConflict("CoffeeBench recovery must preserve the exact target and binding")
            reused = connection.execute(
                "SELECT 1 FROM background_provider_pending WHERE company_id=? AND operation_id=?",
                (company_id, recovery_operation_id),
            ).fetchone()
            if reused is not None:
                raise StoreConflict("CoffeeBench recovery operation ID is already pending")
            now = _now()
            connection.execute(
                "INSERT INTO background_provider_recovery_archive(company_id,provider_id,previous_operation_id,"
                "previous_request_json,previous_result_json,previous_created_at,recovery_operation_id,"
                "recovery_request_json,confirmed_cursor,checkpoint_sha256,created_at) "
                "VALUES(?,'coffeebench',?,?,NULL,?,?,?,?,?,?)",
                (company_id, previous_operation_id, pending["request_json"], pending["created_at"],
                 recovery_operation_id, encoded_recovery, confirmed_cursor, checkpoint_sha256, now),
            )
            connection.execute(
                "DELETE FROM background_provider_pending WHERE company_id=? AND provider_id='coffeebench' "
                "AND operation_id=?", (company_id, previous_operation_id),
            )
            connection.execute(
                "INSERT INTO background_provider_pending(company_id,provider_id,operation_id,request_json,created_at) "
                "VALUES(?,'coffeebench',?,?,?)",
                (company_id, recovery_operation_id, encoded_recovery, now),
            )
            row = connection.execute(
                "SELECT * FROM background_provider_recovery_archive "
                "WHERE company_id=? AND provider_id='coffeebench' AND previous_operation_id=?",
                (company_id, previous_operation_id),
            ).fetchone()
            return self._background_provider_recovery_dict(row)

    def persist_background_provider_result(
        self, provider_id: str, operation_id: str, request: dict[str, Any], result: dict[str, Any],
        company_id: str = "company",
    ) -> None:
        """Durably queue a confirmed provider result until company facts are stored."""
        encoded_request, encoded_result = _json(request), _json(result)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT operation_id,request_json,result_json FROM background_provider_pending "
                "WHERE company_id=? AND provider_id=?", (company_id, provider_id),
            ).fetchone()
            if row is None or row["operation_id"] != operation_id or row["request_json"] != encoded_request:
                raise StoreConflict("provider result does not match its pending operation")
            if row["result_json"] is not None and row["result_json"] != encoded_result:
                raise StoreConflict("provider pending operation returned a different durable result")
            self._record_background_provider_result_receipt(
                connection, company_id, provider_id, operation_id, result,
            )
            connection.execute(
                "UPDATE background_provider_pending SET result_json=? WHERE company_id=? AND provider_id=? AND operation_id=?",
                (encoded_result, company_id, provider_id, operation_id),
            )

    def persist_background_provider_pending(
        self, provider_id: str, operation_id: str, request: dict[str, Any],
        company_id: str = "company",
    ) -> dict[str, Any]:
        """Keep the exact unconfirmed provider operation for retry after restart."""
        encoded = _json(request)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT operation_id,request_json FROM background_provider_pending "
                "WHERE company_id=? AND provider_id=?", (company_id, provider_id),
            ).fetchone()
            if row is not None:
                if row["operation_id"] != operation_id or row["request_json"] != encoded:
                    raise StoreConflict("provider already has a different unconfirmed background operation")
                return {"operation_id": row["operation_id"], "request": _decode(row["request_json"])}
            connection.execute(
                "INSERT INTO background_provider_pending(company_id,provider_id,operation_id,request_json,created_at) "
                "VALUES(?,?,?,?,?)", (company_id, provider_id, operation_id, encoded, _now()),
            )
        return {"operation_id": operation_id, "request": _decode(encoded)}

    def acknowledge_background_provider_result(
        self, provider_id: str, operation_id: str, company_id: str = "company",
    ) -> None:
        """Remove a result only after the coordinator durably records its facts."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM background_provider_pending "
                "WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
            if row is None:
                return
            if row["result_json"] is None:
                raise StoreConflict("cannot acknowledge a provider operation without a durable result")
            self._record_background_provider_result_receipt(
                connection, company_id, provider_id, operation_id, _decode(row["result_json"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO background_provider_result_acknowledgements "
                "(company_id,provider_id,operation_id,acknowledged_at) VALUES(?,?,?,?)",
                (company_id, provider_id, operation_id, _now()),
            )
            connection.execute(
                "DELETE FROM background_provider_pending WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            )

    @staticmethod
    def _record_background_provider_result_receipt(
        connection: sqlite3.Connection, company_id: str, provider_id: str,
        operation_id: str, result: dict[str, Any],
    ) -> None:
        events = result.get("events")
        if not isinstance(events, list):
            raise StoreConflict("confirmed background result has invalid source event data")
        event_ids = sorted(str(event["event_id"]) for event in events)
        if len(event_ids) != len(set(event_ids)):
            raise StoreConflict("confirmed background result repeats a source event ID")
        upstream_commit, cursor, next_due = (
            result.get("upstream_commit"), result.get("cursor"), result.get("next_due"),
        )
        if (not isinstance(upstream_commit, str) or not upstream_commit
                or not isinstance(cursor, str) or not cursor
                or (next_due is not None and not isinstance(next_due, str))):
            raise StoreConflict("confirmed background result is missing its pinned clock identity")
        expected = (upstream_commit, cursor, next_due, _digest(result), _json(event_ids))
        receipt = connection.execute(
            "SELECT upstream_commit,cursor,next_due,result_sha256,source_event_ids_json "
            "FROM background_provider_result_receipts WHERE company_id=? AND provider_id=? AND operation_id=?",
            (company_id, provider_id, operation_id),
        ).fetchone()
        if receipt is not None:
            if tuple(receipt) != expected:
                raise StoreConflict("background provider result receipt changed for an operation")
            return
        connection.execute(
            "INSERT INTO background_provider_result_receipts(company_id,provider_id,operation_id,"
            "upstream_commit,cursor,coverage_until,next_due,result_sha256,source_event_ids_json,evidence_origin,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, provider_id, operation_id, expected[0], expected[1], expected[1],
             *expected[2:], "coordinator_result", _now()),
        )

    def record_derived_background_provider_result_receipt(
        self, *, company_id: str, provider_id: str, operation_id: str,
        upstream_commit: str, cursor: str, next_due: str | None, result_sha256: str,
        source_event_ids: list[str], coverage_until: str, source_identity_sha256: str,
        coverage_gap_event_ids_sha256: str,
    ) -> None:
        """Record verified legacy worker evidence without backdating the derivation."""
        try:
            source_cursor = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            coverage_time = datetime.fromisoformat(coverage_until.replace("Z", "+00:00"))
            next_due_time = None if next_due is None else datetime.fromisoformat(next_due.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise StoreConflict("legacy E-Commerce coverage proof has invalid timestamps") from exc
        if (provider_id != "ecommercebench" or not operation_id
                or not re.fullmatch(r"[0-9a-f]{40}", upstream_commit)
                or not re.fullmatch(r"[0-9a-f]{64}", result_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", source_identity_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", coverage_gap_event_ids_sha256)
                or (next_due is not None and not isinstance(next_due, str))
                or not isinstance(coverage_until, str)
                or source_cursor.tzinfo is None or coverage_time.tzinfo is None
                or source_cursor > coverage_time or next_due_time is None
                or next_due_time.tzinfo is None or coverage_time >= next_due_time
                or source_event_ids != sorted(set(source_event_ids))
                or any(not isinstance(value, str) or not value for value in source_event_ids)):
            raise StoreConflict("legacy E-Commerce operation proof has invalid identity")
        with self.transaction() as connection:
            for event_id in source_event_ids:
                row = connection.execute(
                    "SELECT source,upstream_commit FROM company_source_events "
                    "WHERE company_id=? AND event_id=?", (company_id, event_id),
                ).fetchone()
                if row is None or row["source"] != "qwenlm-ecommercebench-calendar" or row["upstream_commit"] != upstream_commit:
                    raise StoreConflict("legacy E-Commerce proof references unpersisted or mismatched source events")
            values = (company_id, provider_id, operation_id, upstream_commit, cursor, coverage_until, next_due,
                      result_sha256, _json(source_event_ids))
            existing = connection.execute(
                "SELECT upstream_commit,cursor,next_due,result_sha256,source_event_ids_json "
                "FROM background_provider_result_receipts "
                "WHERE company_id=? AND provider_id=? AND operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (upstream_commit, cursor, next_due, result_sha256, _json(source_event_ids)):
                    raise StoreConflict("legacy E-Commerce receipt conflicts with an existing operation receipt")
            else:
                connection.execute(
                    "INSERT INTO background_provider_result_receipts(company_id,provider_id,operation_id,"
                    "upstream_commit,cursor,coverage_until,next_due,result_sha256,source_event_ids_json,evidence_origin,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, "derived_worker_operation", _now()),
                )
            coverage_values = (company_id, provider_id, operation_id, coverage_until,
                               source_identity_sha256, coverage_gap_event_ids_sha256)
            existing_coverage = connection.execute(
                "SELECT company_id,provider_id,operation_id,coverage_until,source_identity_sha256,gap_event_ids_sha256 "
                "FROM background_provider_result_coverage WHERE company_id=? AND provider_id=? "
                "AND operation_id=? AND coverage_until=?",
                (company_id, provider_id, operation_id, coverage_until),
            ).fetchone()
            if existing_coverage is not None:
                if tuple(existing_coverage) != coverage_values:
                    raise StoreConflict("legacy E-Commerce coverage proof conflicts with its prior record")
            else:
                connection.execute(
                    "INSERT INTO background_provider_result_coverage(company_id,provider_id,operation_id,"
                    "coverage_until,source_identity_sha256,gap_event_ids_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (*coverage_values, _now()),
                )
            connection.execute(
                "INSERT OR IGNORE INTO background_provider_result_acknowledgements "
                "(company_id,provider_id,operation_id,acknowledged_at) VALUES(?,?,?,?)",
                (company_id, provider_id, operation_id, _now()),
            )

    @staticmethod
    def _background_provider_result_receipt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "company_id": row["company_id"], "provider_id": row["provider_id"],
            "operation_id": row["operation_id"], "upstream_commit": row["upstream_commit"],
            "cursor": row["cursor"], "coverage_until": row["coverage_until"], "next_due": row["next_due"],
            "result_sha256": row["result_sha256"],
            "source_event_ids": _decode(row["source_event_ids_json"]),
            "evidence_origin": row["evidence_origin"],
            "created_at": row["created_at"], "acknowledged_at": row["acknowledged_at"],
        }

    def get_background_provider_result_receipt(
        self, provider_id: str, operation_id: str, *, require_acknowledged: bool = False,
        company_id: str = "company",
    ) -> dict[str, Any] | None:
        join = ("JOIN" if require_acknowledged else "LEFT JOIN")
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT r.*,a.acknowledged_at FROM background_provider_result_receipts r "
                f"{join} background_provider_result_acknowledgements a "
                "USING(company_id,provider_id,operation_id) "
                "WHERE r.company_id=? AND r.provider_id=? AND r.operation_id=?",
                (company_id, provider_id, operation_id),
            ).fetchone()
        return None if row is None else self._background_provider_result_receipt_dict(row)

    def get_background_provider_result_coverage(
        self, provider_id: str, operation_id: str, coverage_until: str, *, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT company_id,provider_id,operation_id,coverage_until,source_identity_sha256,"
                "gap_event_ids_sha256,created_at FROM background_provider_result_coverage "
                "WHERE company_id=? AND provider_id=? AND operation_id=? AND coverage_until=?",
                (company_id, provider_id, operation_id, coverage_until),
            ).fetchone()
        return None if row is None else dict(row)

    def latest_acknowledged_background_provider_result_receipt(
        self, provider_id: str, *, company_id: str = "company",
    ) -> dict[str, Any] | None:
        with self._connection(None) as connection:
            row = connection.execute(
                "SELECT r.*,a.acknowledged_at FROM background_provider_result_receipts r "
                "JOIN background_provider_result_acknowledgements a USING(company_id,provider_id,operation_id) "
                "WHERE r.company_id=? AND r.provider_id=? ORDER BY r.cursor DESC,r.operation_id DESC LIMIT 1",
                (company_id, provider_id),
            ).fetchone()
        return None if row is None else self._background_provider_result_receipt_dict(row)

    def is_company_experiment(self, run_id: UUID | str, *, connection: sqlite3.Connection | None = None) -> bool:
        with self._connection(connection) as conn:
            return conn.execute("SELECT 1 FROM company_experiments WHERE run_id=?", (self._run_id(run_id),)).fetchone() is not None

    def update_run(
        self,
        run_id: UUID | str,
        generation: int,
        *,
        simulated_time: datetime | str | None | object = _UNSET,
        status: str | object = _UNSET,
        paused: bool | object = _UNSET,
        erp_binding: dict[str, Any] | object = _UNSET,
        manifest: dict[str, Any] | object = _UNSET,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Persist only allowlisted coordinator-owned run fields."""
        updates = {
            key: value for key, value in {
                "simulated_time": simulated_time,
                "status": status,
                "paused": paused,
                "erp_binding": erp_binding,
                "manifest": manifest,
            }.items() if value is not _UNSET
        }
        columns = {"manifest": "manifest_json", "erp_binding": "erp_binding_json"}
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            for key, value in updates.items():
                column = columns.get(key, key)
                if key == "paused":
                    value = int(bool(value))
                elif key in columns:
                    value = _json(value)
                elif key == "simulated_time" and isinstance(value, datetime):
                    value = value.isoformat()
                conn.execute(f"UPDATE runs SET {column}=? WHERE run_id=?", (value, rid))
            return self.get_run(rid, connection=conn)

    def put_case(
        self, run_id: UUID | str, generation: int, case_id: str, visible: dict[str, Any],
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            conn.execute(
                "INSERT INTO cases VALUES(?,?,?,?) ON CONFLICT(run_id,generation,case_id) DO UPDATE SET visible_json=excluded.visible_json",
                (rid, generation, case_id, _json(visible)),
            )
            return {"run_id": rid, "generation": generation, "case_id": case_id, "visible": visible}

    def get_case(
        self, run_id: UUID | str, generation: int, case_id: str,
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._connection(connection) as conn:
            row = conn.execute(
                "SELECT visible_json FROM cases WHERE run_id=? AND generation=? AND case_id=?",
                (self._run_id(run_id), generation, case_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("case not found in requested run generation")
            return {"run_id": self._run_id(run_id), "generation": generation, "case_id": case_id, "visible": _decode(row["visible_json"])}

    def update_case(
        self, run_id: UUID | str, generation: int, case_id: str, updates: dict[str, Any],
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            current = self.get_case(rid, generation, case_id, connection=conn)["visible"]
            current.update(updates)
            return self.put_case(rid, generation, case_id, current, connection=conn)

    def put_actor(
        self, run_id: UUID | str, generation: int, actor_id: str, *, kind: str,
        email: str | None, private_state: dict[str, Any], connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            conn.execute(
                """INSERT INTO actors VALUES(?,?,?,?,?,?)
                ON CONFLICT(run_id,generation,actor_id) DO UPDATE SET
                kind=excluded.kind,email=excluded.email,private_json=excluded.private_json""",
                (rid, generation, actor_id, kind, email, _json(private_state)),
            )
            return self.get_actor(rid, generation, actor_id, connection=conn)

    def get_actor(
        self, run_id: UUID | str, generation: int, actor_id: str,
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._connection(connection) as conn:
            row = conn.execute(
                "SELECT * FROM actors WHERE run_id=? AND generation=? AND actor_id=?",
                (self._run_id(run_id), generation, actor_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("actor not found in requested run generation")
            return {"run_id": row["run_id"], "generation": row["generation"], "actor_id": row["actor_id"], "kind": row["kind"], "email": row["email"], "private_state": _decode(row["private_json"])}

    def list_actors(
        self, run_id: UUID | str, generation: int, *, connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._connection(connection) as conn:
            rows = conn.execute(
                "SELECT * FROM actors WHERE run_id=? AND generation=? ORDER BY actor_id",
                (self._run_id(run_id), generation),
            ).fetchall()
            return [self.get_actor(run_id, generation, row["actor_id"], connection=conn) for row in rows]

    def enqueue_event(
        self, event: CanonicalEvent, generation: int, *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(event.run_id)
            self._require_generation(conn, rid, generation)
            event_data = event.model_dump(mode="json")
            payload_hash = _digest(event_data)
            existing = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND generation=? AND event_id=?",
                (rid, generation, event.event_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise StoreConflict("event_id already exists with different event content")
                return self._event_row(existing)
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=? AND generation=?",
                (rid, generation),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO events(run_id,generation,event_id,sequence,simulated_time,
                    event_type,source,payload_json,visibility_json,canonical_event_json,payload_hash,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                (rid, generation, event.event_id, sequence, event.simulated_time.isoformat(), event.event_type,
                 event.source, _json(event.payload), _json(event.visibility), _json(event_data), payload_hash),
            )
            row = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND generation=? AND event_id=?",
                (rid, generation, event.event_id),
            ).fetchone()
            return self._event_row(row)

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        canonical = _decode(row["canonical_event_json"])
        return {**canonical, "generation": row["generation"], "sequence": row["sequence"],
                "payload_hash": row["payload_hash"], "status": row["status"],
                "outcome": _decode(row["outcome_json"]) if row["outcome_json"] is not None else None}

    def pending_events(self, run_id: UUID | str, generation: int) -> list[dict[str, Any]]:
        with self._connection(None) as conn:
            self._require_generation(conn, self._run_id(run_id), generation)
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND generation=? AND status='pending' ORDER BY sequence",
                (self._run_id(run_id), generation),
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def list_events(
        self, run_id: UUID | str, generation: int, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read one retained generation for audit or deterministic replay."""
        if status not in (None, "pending", "completed"):
            raise ValueError("status must be pending, completed, or None")
        with self._connection(None) as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND generation=? ORDER BY sequence",
                    (self._run_id(run_id), generation),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND generation=? AND status=? ORDER BY sequence",
                    (self._run_id(run_id), generation, status),
                ).fetchall()
            return [self._event_row(row) for row in rows]

    def complete_event(
        self, run_id: UUID | str, generation: int, event_id: str, outcome: dict[str, Any],
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            encoded = _json(outcome)
            row = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND generation=? AND event_id=?",
                (rid, generation, event_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("event not found in requested run generation")
            if row["status"] == "completed":
                if row["outcome_json"] != encoded:
                    raise StoreConflict("event was already completed with a different outcome")
                return self._event_row(row)
            conn.execute(
                "UPDATE events SET status='completed',outcome_json=? WHERE run_id=? AND generation=? AND event_id=?",
                (encoded, rid, generation, event_id),
            )
            return self._event_row(conn.execute(
                "SELECT * FROM events WHERE run_id=? AND generation=? AND event_id=?", (rid, generation, event_id)
            ).fetchone())

    def enqueue_outbox(
        self, run_id: UUID | str, generation: int, outbox_id: str, private_payload: dict[str, Any],
        *, event_id: str | None = None, actor_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            encoded, payload_hash = _json(private_payload), _digest(private_payload)
            existing = conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (rid, generation, outbox_id),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash or existing["event_id"] != event_id or existing["actor_id"] != actor_id:
                    raise StoreConflict("outbox_id already exists with different content")
                return self._outbox_row(existing)
            conn.execute(
                """INSERT INTO outbox(run_id,generation,outbox_id,event_id,actor_id,
                    private_payload_json,payload_hash,submission_started,error_type,status,
                    outcome_json,created_at)
                    VALUES(?,?,?,?,?,?,?,0,NULL,'pending',NULL,?)""",
                (rid, generation, outbox_id, event_id, actor_id, encoded, payload_hash, _now()),
            )
            return self._outbox_row(conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (rid, generation, outbox_id),
            ).fetchone())

    @staticmethod
    def _outbox_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"], "generation": row["generation"], "outbox_id": row["outbox_id"],
            "event_id": row["event_id"], "actor_id": row["actor_id"],
            "private_payload": _decode(row["private_payload_json"]), "payload_hash": row["payload_hash"],
            "submission_started": bool(row["submission_started"]), "error_type": row["error_type"],
            "status": row["status"], "outcome": _decode(row["outcome_json"]) if row["outcome_json"] else None,
            "created_at": row["created_at"], "completed_at": row["completed_at"],
        }

    def pending_outbox(self, run_id: UUID | str, generation: int) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            self._require_generation(conn, self._run_id(run_id), generation)
            # A worker that crashed after marking the SMTP boundary may have
            # delivered the message without persisting its receipt. Keep it
            # visible for reconciliation but never return it as retryable.
            conn.execute(
                """UPDATE outbox SET status='uncertain'
                WHERE run_id=? AND generation=? AND status='pending' AND submission_started=1""",
                (self._run_id(run_id), generation),
            )
            rows = conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND status IN ('pending','uncertain') ORDER BY created_at,outbox_id",
                (self._run_id(run_id), generation),
            ).fetchall()
            return [self._outbox_row(row) for row in rows]

    def get_outbox(
        self, run_id: UUID | str, generation: int, outbox_id: str,
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._connection(connection) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (self._run_id(run_id), generation, outbox_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("outbox item not found in requested run generation")
            return self._outbox_row(row)

    def list_outbox(
        self, run_id: UUID | str, generation: int, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status not in (None, "pending", "uncertain", "completed"):
            raise ValueError("invalid outbox status")
        with self._connection(None) as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE run_id=? AND generation=? ORDER BY created_at,outbox_id",
                    (self._run_id(run_id), generation),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE run_id=? AND generation=? AND status=? ORDER BY created_at,outbox_id",
                    (self._run_id(run_id), generation, status),
                ).fetchall()
            return [self._outbox_row(row) for row in rows]

    def mark_outbox_submission_started(
        self, run_id: UUID | str, generation: int, outbox_id: str,
    ) -> dict[str, Any]:
        """Atomically claim the one-way external submission boundary.

        Only one worker can transition a pending item to started. A row that
        crossed this boundary must be reconciled, never blindly resubmitted.
        """
        rid = self._run_id(run_id)
        with self.transaction() as conn:
            self._require_generation(conn, rid, generation)
            cursor = conn.execute(
                """UPDATE outbox SET submission_started=1,error_type=NULL
                WHERE run_id=? AND generation=? AND outbox_id=?
                AND status='pending' AND submission_started=0""",
                (rid, generation, outbox_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT 1 FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                    (rid, generation, outbox_id),
                ).fetchone()
                if row is None:
                    raise StoreNotFound("outbox item not found in requested run generation")
                raise StoreConflict("outbox item was already claimed or is not retryable")
            return self.get_outbox(rid, generation, outbox_id, connection=conn)

    def mark_outbox_failure(
        self, run_id: UUID | str, generation: int, outbox_id: str, *, error_type: str,
        pre_send: bool = False, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Record a safe failure code; callers may assert a definite pre-send error."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", error_type):
            raise ValueError("error_type must be a safe error code")
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            row = conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (rid, generation, outbox_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("outbox item not found in requested run generation")
            if row["status"] == "completed":
                raise StoreConflict("completed outbox item cannot be failed")
            new_status = "uncertain" if row["submission_started"] and not pre_send else "pending"
            conn.execute(
                "UPDATE outbox SET status=?,submission_started=?,error_type=? WHERE run_id=? AND generation=? AND outbox_id=?",
                (new_status, int(new_status == "uncertain"), error_type, rid, generation, outbox_id),
            )
            return self.get_outbox(rid, generation, outbox_id, connection=conn)

    def complete_outbox(
        self, run_id: UUID | str, generation: int, outbox_id: str, outcome: dict[str, Any],
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid, encoded = self._run_id(run_id), _json(outcome)
            self._require_generation(conn, rid, generation)
            row = conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (rid, generation, outbox_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("outbox item not found in requested run generation")
            if row["status"] == "completed":
                if row["outcome_json"] != encoded:
                    raise StoreConflict("outbox item was already completed with a different outcome")
                return self._outbox_row(row)
            conn.execute(
                "UPDATE outbox SET status='completed',outcome_json=?,completed_at=?,error_type=NULL WHERE run_id=? AND generation=? AND outbox_id=?",
                (encoded, _now(), rid, generation, outbox_id),
            )
            return self._outbox_row(conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND generation=? AND outbox_id=?",
                (rid, generation, outbox_id),
            ).fetchone())

    def append_audit(
        self, run_id: UUID | str, generation: int, private_record: dict[str, Any],
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            cursor = conn.execute(
                "INSERT INTO audit(run_id,generation,created_at,private_record_json) VALUES(?,?,?,?)",
                (rid, generation, _now(), _json(private_record)),
            )
            return {"audit_id": cursor.lastrowid, "run_id": rid, "generation": generation,
                    "private_record": private_record}

    def list_audit(self, run_id: UUID | str, generation: int | None = None) -> list[dict[str, Any]]:
        with self._connection(None) as conn:
            if generation is None:
                rows = conn.execute("SELECT * FROM audit WHERE run_id=? ORDER BY audit_id", (self._run_id(run_id),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit WHERE run_id=? AND generation=? ORDER BY audit_id",
                    (self._run_id(run_id), generation),
                ).fetchall()
            return [{"audit_id": row["audit_id"], "run_id": row["run_id"], "generation": row["generation"],
                     "created_at": row["created_at"], "private_record": _decode(row["private_record_json"])} for row in rows]

    def put_private_message(
        self, run_id: UUID | str, generation: int, message: dict[str, Any],
        *, simulated_time: str, from_actor_id: str, to_actor_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Persist actual email content outside the append-only operational audit."""
        message_id = str(message.get("message_id") or "")
        safe_message = {key: message.get(key) for key in (
            "message_id", "subject", "body", "from_header", "to_header", "in_reply_to",
            "references", "mailbox_uid", "uidvalidity", "reference", "webmail_url")}
        encoded = _json(safe_message)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        # Message-ID is optional in received mail. Keep the original headers
        # intact; use a private mailbox/content key when the sender omitted it.
        if not message_id:
            message_id = "mailbox:" + _digest({"recipient": to_actor_id,
                                               "uid": message.get("mailbox_uid"),
                                               "uidvalidity": message.get("uidvalidity"),
                                               "content_sha256": digest})
        with self._write(connection) as conn:
            rid = self._run_id(run_id)
            self._require_generation(conn, rid, generation)
            existing = conn.execute(
                "SELECT message_hash FROM private_conversation WHERE run_id=? AND generation=? AND message_id=?",
                (rid, generation, message_id),
            ).fetchone()
            if existing is not None:
                if existing["message_hash"] != digest:
                    raise StoreConflict("mailbox Message-ID was received with different content")
                return {"run_id": rid, "generation": generation, "message_id": message_id, "message_hash": digest}
            conn.execute(
                "INSERT INTO private_conversation(run_id,generation,message_id,simulated_time,from_actor_id,to_actor_id,message_json,message_hash,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, generation, message_id, simulated_time, from_actor_id, to_actor_id, encoded, digest, _now()),
            )
            return {"run_id": rid, "generation": generation, "message_id": message_id, "message_hash": digest}

    def list_private_messages(self, run_id: UUID | str, generation: int) -> list[dict[str, Any]]:
        with self._connection(None) as conn:
            rows = conn.execute(
                "SELECT * FROM private_conversation WHERE run_id=? AND generation=? ORDER BY simulated_time,message_id",
                (self._run_id(run_id), generation),
            ).fetchall()
            return [{"run_id": row["run_id"], "generation": row["generation"],
                     "message_id": row["message_id"], "simulated_time": row["simulated_time"], "observed_at": row["observed_at"],
                     "from_actor_id": row["from_actor_id"], "to_actor_id": row["to_actor_id"],
                     "message_hash": row["message_hash"], **_decode(row["message_json"])} for row in rows]

    def execute_idempotent(
        self,
        scope: str,
        key: str,
        request: dict[str, Any],
        operation: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run a database-only callback once per scoped key in one transaction.

        Do not perform network calls from ``operation``. A raised exception
        rolls back both application writes and the idempotency result.
        """
        if not scope or not key:
            raise ValueError("idempotency scope and key are required")
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        request_hash = _digest(request)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT request_hash,response_json FROM request_idempotency WHERE scope=? AND key_hash=?",
                (scope, key_hash),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency key reused with different request data")
                return _decode(existing["response_json"])
            response = operation(connection)
            if not isinstance(response, dict):
                raise TypeError("idempotent operation must return a JSON object")
            connection.execute(
                "INSERT INTO request_idempotency VALUES(?,?,?,?, 'completed',?)",
                (scope, key_hash, request_hash, _json(response), _now()),
            )
            return response

    def reset_run(
        self,
        run_id: UUID | str,
        generation: int,
        snapshot: dict[str, Any],
        *,
        idempotency_scope: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a new run generation from its initial durable scenario snapshot.

        Rows from earlier generations are retained for audit/replay. This is
        only the local store reset; external ERP reset must be coordinated by
        the caller outside SQLite transactions.
        """
        rid = self._run_id(run_id)
        request = {"operation": "reset_run", "run_id": rid, "generation": generation, "snapshot": snapshot}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._require_generation(connection, rid, generation)
            next_generation = generation + 1
            run_state = snapshot.get("run", {})
            connection.execute(
                """UPDATE runs SET generation=?,status=?,simulated_time=?,paused=?,manifest_json=?,erp_binding_json=?
                WHERE run_id=?""",
                (next_generation, run_state.get("status", "running"), run_state.get("simulated_time"),
                 int(bool(run_state.get("paused", False))), _json(run_state.get("manifest", {})),
                 _json(run_state.get("erp_binding", {})), rid),
            )
            for case in snapshot.get("cases", []):
                self.put_case(rid, next_generation, case["case_id"], case["visible"], connection=connection)
            for actor in snapshot.get("actors", []):
                self.put_actor(
                    rid, next_generation, actor["actor_id"], kind=actor["kind"],
                    email=actor.get("email"), private_state=actor["private_state"], connection=connection,
                )
            result = {"run_id": rid, "generation": next_generation, "status": run_state.get("status", "running")}
            connection.execute(
                "INSERT INTO audit(run_id,generation,created_at,private_record_json) VALUES(?,?,?,?)",
                (rid, next_generation, _now(), _json({"event": "run_reset", "from_generation": generation,
                                                       "snapshot_hash": _digest(snapshot)})),
            )
            return result

        return self.execute_idempotent(idempotency_scope, idempotency_key, request, operation)

    @contextmanager
    def _write(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with self.transaction() as owned:
                yield owned
