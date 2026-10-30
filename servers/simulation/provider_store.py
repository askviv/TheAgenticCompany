"""Private durable state for shared, native τ external-provider environments."""

from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import zlib

from servers.simulation.actor_worker_store import canonical_request_hash


def native_record_digests(agent_db: dict[str, Any], user_db: dict[str, Any] | None) -> dict[str, str]:
    """Hash source-native entity rows so shared-record writes can be attributed."""
    records: dict[str, str] = {}

    def add(ref: str, value: Any) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if ref in records:
            raise ValueError("native provider state contains duplicate record identities")
        records[ref] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def visit(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            if prefix.endswith("/surroundings"):
                add(prefix, value)
                return
            if "data" in value and isinstance(value["data"], dict):
                for source_id, row in value["data"].items():
                    add(f"{prefix}/{source_id}", row)
                return
            if "/" in prefix and value and all(isinstance(row, dict) for row in value.values()):
                # τ domains use keyed native rows (e.g. airline reservations and
                # Banking account tables) as well as list-backed Telecom rows.
                for source_id, row in value.items():
                    add(f"{prefix}/{source_id}", row)
                return
            for key, item in value.items():
                visit(item, f"{prefix}/{key}")
            if prefix.endswith("/surroundings"):
                add(prefix, value)
        elif isinstance(value, list):
            for row in value:
                if not isinstance(row, dict):
                    continue
                source_id = next((row[key] for key in (
                    # Prefer this row's identifier to a parent/customer ID.
                    # Telecom bills and lines carry customer_id too; choosing
                    # that first collapsed every bill/line for a customer into
                    # one digest and hid per-record shared changes.
                    "reservation_id", "bill_id", "line_id", "plan_id", "transaction_id",
                    "dispute_id", "card_id", "account_id", "flight_number", "user_id", "customer_id",
                ) if row.get(key) is not None), None)
                if source_id is not None:
                    add(f"{prefix}/{source_id}", row)

    visit(agent_db, "agent")
    if user_db is not None:
        visit(user_db, "user")
    return records


class ProviderOperationConflict(RuntimeError):
    pass


class ProviderOperationUncertain(RuntimeError):
    pass


class NativeProviderStore:
    """SQLite snapshots and idempotency records; one DB snapshot per provider."""

    def __init__(self, database_path: str | Path, lock_directory: str | Path | None = None):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_directory = Path(lock_directory or self.path.parent / "provider-locks")
        self.lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()
        if os.name == "posix":
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Close each SQLite handle after its transaction boundary completes."""
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS native_provider_state (
                    provider_id TEXT PRIMARY KEY,
                    upstream_commit TEXT NOT NULL,
                    agent_db_json TEXT NOT NULL,
                    user_db_json TEXT,
                    initialized_task_ids_json TEXT NOT NULL,
                    state_revision TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_provider_task_baselines (
                    provider_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_db_json TEXT NOT NULL,
                    user_db_json TEXT,
                    PRIMARY KEY(provider_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS native_provider_conversation_baselines (
                    provider_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_db_json BLOB NOT NULL,
                    user_db_json BLOB,
                    PRIMARY KEY(provider_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS native_provider_conversations (
                    provider_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_state_json TEXT NOT NULL,
                    trajectory_json TEXT NOT NULL,
                    initialization_status TEXT NOT NULL,
                    initialization_evidence_json TEXT NOT NULL DEFAULT '{}',
                    company_context_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(provider_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS native_provider_operations (
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started','completed')),
                    response_json TEXT,
                    PRIMARY KEY(provider_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS native_provider_evaluation_snapshots (
                    capture_ref TEXT PRIMARY KEY,
                    capture_sha256 TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    upstream_commit TEXT NOT NULL,
                    identity_fingerprint TEXT NOT NULL,
                    native_state_revision TEXT NOT NULL,
                    snapshot_json BLOB NOT NULL,
                    UNIQUE(provider_id, conversation_id, capture_sha256)
                );
                CREATE TABLE IF NOT EXISTS native_provider_operation_changes (
                    provider_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    before_revision TEXT NOT NULL,
                    after_revision TEXT NOT NULL,
                    changed_records_json TEXT NOT NULL,
                    PRIMARY KEY(provider_id, operation_id)
                );
            """)
            conversation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(native_provider_conversations)")
            }
            for name, declaration in (
                ("initialization_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("company_context_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in conversation_columns:
                    connection.execute(
                        f"ALTER TABLE native_provider_conversations ADD COLUMN {name} {declaration}"
                    )
            connection.commit()

    @contextmanager
    def locked_provider(self, provider_id: str) -> Iterator[None]:
        if provider_id not in {"tau2-airline", "tau2-telecom", "tau2-banking"}:
            raise ValueError("unknown native τ provider")
        path = self.lock_directory / f"{provider_id}.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _revision(agent_db: dict[str, Any], user_db: dict[str, Any] | None) -> str:
        return canonical_request_hash({"agent_db": agent_db, "user_db": user_db})

    def bootstrap(self, provider_id: str, upstream_commit: str,
                  agent_db: dict[str, Any], user_db: dict[str, Any] | None) -> dict[str, Any]:
        encoded_agent = json.dumps(agent_db, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        encoded_user = None if user_db is None else json.dumps(
            user_db, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        revision = self._revision(agent_db, user_db)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT upstream_commit,agent_db_json,user_db_json,initialized_task_ids_json,state_revision "
                "FROM native_provider_state WHERE provider_id=?", (provider_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO native_provider_state VALUES(?,?,?,?,?,?)",
                    (provider_id, upstream_commit, encoded_agent, encoded_user, "[]", revision),
                )
                row = connection.execute(
                    "SELECT upstream_commit,agent_db_json,user_db_json,initialized_task_ids_json,state_revision "
                    "FROM native_provider_state WHERE provider_id=?", (provider_id,),
                ).fetchone()
            elif row["upstream_commit"] != upstream_commit:
                connection.rollback()
                raise ValueError("persisted native provider state uses a different τ source revision")
            connection.commit()
        return self._decode_state(row)

    @staticmethod
    def _decode_state(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "upstream_commit": row["upstream_commit"],
            "agent_db": json.loads(row["agent_db_json"]),
            "user_db": json.loads(row["user_db_json"]) if row["user_db_json"] else None,
            "initialized_task_ids": json.loads(row["initialized_task_ids_json"]),
            "state_revision": row["state_revision"],
        }

    @staticmethod
    def _encode_snapshot(value: dict[str, Any] | None) -> bytes | None:
        if value is None:
            return None
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return b"Z" + zlib.compress(encoded, level=6)

    @staticmethod
    def _decode_snapshot(value: str | bytes | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            if value[:1] == b"Z":
                value = zlib.decompress(value[1:]).decode("utf-8")
            else:
                value = value.decode("utf-8")
        return json.loads(value)

    def state(self, provider_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT upstream_commit,agent_db_json,user_db_json,initialized_task_ids_json,state_revision "
                "FROM native_provider_state WHERE provider_id=?", (provider_id,),
            ).fetchone()
        return self._decode_state(row) if row else None

    def claim(self, provider_id: str, operation_id: str, request: dict[str, Any]) -> tuple[str, Any]:
        request_hash = canonical_request_hash(request)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash,status,response_json FROM native_provider_operations "
                "WHERE provider_id=? AND operation_id=?", (provider_id, operation_id),
            ).fetchone()
            if row:
                connection.commit()
                if row["request_hash"] != request_hash:
                    return "conflict", None
                if row["status"] == "completed":
                    return "completed", json.loads(row["response_json"])
                return "started_uncertain", None
            connection.execute(
                "INSERT INTO native_provider_operations(provider_id,operation_id,request_hash,status) "
                "VALUES(?,?,?,'started')", (provider_id, operation_id, request_hash),
            )
            connection.commit()
        return "claimed", request_hash

    def operation(self, provider_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,status,response_json FROM native_provider_operations "
                "WHERE provider_id=? AND operation_id=?", (provider_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        return {"request_hash": row["request_hash"], "status": row["status"],
                "response": json.loads(row["response_json"]) if row["response_json"] else None}

    def conversation(self, provider_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_id,agent_state_json,trajectory_json,initialization_status,"
                "initialization_evidence_json,company_context_json "
                "FROM native_provider_conversations WHERE provider_id=? AND conversation_id=?",
                (provider_id, conversation_id),
            ).fetchone()
        if not row:
            return None
        return {
            "task_id": row["task_id"], "agent_state": json.loads(row["agent_state_json"]),
            "trajectory": json.loads(row["trajectory_json"]),
            "initialization_status": row["initialization_status"],
            "initialization_evidence": json.loads(row["initialization_evidence_json"]),
            "company_context": json.loads(row["company_context_json"]),
        }

    def task_baseline(self, provider_id: str, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT agent_db_json,user_db_json FROM native_provider_task_baselines "
                "WHERE provider_id=? AND task_id=?", (provider_id, task_id),
            ).fetchone()
        if not row:
            return None
        return {"agent_db": self._decode_snapshot(row["agent_db_json"]),
                "user_db": self._decode_snapshot(row["user_db_json"])}

    def conversation_baseline(self, provider_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT agent_db_json,user_db_json FROM native_provider_conversation_baselines "
                "WHERE provider_id=? AND conversation_id=?", (provider_id, conversation_id),
            ).fetchone()
        if not row:
            return None
        return {"agent_db": self._decode_snapshot(row["agent_db_json"]),
                "user_db": self._decode_snapshot(row["user_db_json"])}

    def save_evaluation_snapshot(self, *, capture_ref: str, capture_sha256: str,
                                 provider_id: str, task_id: str, conversation_id: str,
                                 upstream_commit: str, identity_fingerprint: str,
                                 native_state_revision: str, snapshot: dict[str, Any]) -> None:
        """Insert an immutable private evaluation snapshot or verify its replay."""
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT capture_sha256,provider_id,task_id,conversation_id,upstream_commit,"
                "identity_fingerprint,native_state_revision,snapshot_json "
                "FROM native_provider_evaluation_snapshots WHERE capture_ref=?", (capture_ref,),
            ).fetchone()
            values = (capture_sha256, provider_id, task_id, conversation_id, upstream_commit,
                      identity_fingerprint, native_state_revision, encoded)
            if row:
                saved = tuple(row[key] for key in (
                    "capture_sha256", "provider_id", "task_id", "conversation_id", "upstream_commit",
                    "identity_fingerprint", "native_state_revision", "snapshot_json",
                ))
                if saved != values:
                    connection.rollback()
                    raise ValueError("immutable native provider evaluation capture identity conflicts")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO native_provider_evaluation_snapshots(capture_ref,capture_sha256,provider_id,"
                "task_id,conversation_id,upstream_commit,identity_fingerprint,native_state_revision,snapshot_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (capture_ref, *values),
            )
            connection.commit()

    def evaluation_snapshot(self, capture_ref: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT capture_ref,capture_sha256,provider_id,task_id,conversation_id,upstream_commit,"
                "identity_fingerprint,native_state_revision,snapshot_json "
                "FROM native_provider_evaluation_snapshots WHERE capture_ref=?", (capture_ref,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        blob = value["snapshot_json"]
        if isinstance(blob, bytes):
            blob = blob.decode("utf-8")
        value["snapshot"] = json.loads(blob)
        del value["snapshot_json"]
        return value

    def commit_operation(
        self,
        *,
        provider_id: str,
        operation_id: str,
        request_hash: str,
        state: dict[str, Any],
        conversation_id: str,
        task_id: str,
        conversation: dict[str, Any],
        baseline: dict[str, Any] | None,
        response: dict[str, Any],
        conversation_baseline: dict[str, Any] | None = None,
        before_state: dict[str, Any] | None = None,
    ) -> None:
        agent_json = json.dumps(state["agent_db"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        user_json = None if state["user_db"] is None else json.dumps(
            state["user_db"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        tasks_json = json.dumps(sorted(set(state["initialized_task_ids"])), separators=(",", ":"))
        state_revision = self._revision(state["agent_db"], state["user_db"])
        change_record = None
        if before_state is not None:
            before_records = native_record_digests(before_state["agent_db"], before_state["user_db"])
            after_records = native_record_digests(state["agent_db"], state["user_db"])
            changed = {
                ref: {"before_sha256": before_records.get(ref), "after_sha256": after_records.get(ref)}
                for ref in sorted(before_records.keys() | after_records.keys())
                if before_records.get(ref) != after_records.get(ref)
            }
            change_record = (
                self._revision(before_state["agent_db"], before_state["user_db"]), state_revision,
                json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            )
        agent_state_json = json.dumps(conversation["agent_state"], sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False)
        trajectory_json = json.dumps(conversation["trajectory"], sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False)
        initialization_evidence_json = json.dumps(
            conversation.get("initialization_evidence", {}), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False,
        )
        company_context_json = json.dumps(
            conversation.get("company_context", {}), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False,
        )
        response_json = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT 1 FROM native_provider_operations WHERE provider_id=? AND operation_id=? "
                "AND request_hash=? AND status='started'", (provider_id, operation_id, request_hash),
            ).fetchone()
            if not active:
                connection.rollback()
                raise RuntimeError("native provider operation claim was lost")
            connection.execute(
                "UPDATE native_provider_state SET agent_db_json=?,user_db_json=?,initialized_task_ids_json=?,state_revision=? "
                "WHERE provider_id=?", (agent_json, user_json, tasks_json, state_revision, provider_id),
            )
            if baseline is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO native_provider_task_baselines(provider_id,task_id,agent_db_json,user_db_json) "
                    "VALUES(?,?,?,?)", (provider_id, task_id,
                        self._encode_snapshot(baseline["agent_db"]),
                        self._encode_snapshot(baseline["user_db"])),
                )
            if conversation_baseline is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO native_provider_conversation_baselines "
                    "(provider_id,conversation_id,task_id,agent_db_json,user_db_json) VALUES(?,?,?,?,?)",
                    (provider_id, conversation_id, task_id,
                     self._encode_snapshot(conversation_baseline["agent_db"]),
                     self._encode_snapshot(conversation_baseline["user_db"])),
                )
            connection.execute(
                "INSERT INTO native_provider_conversations(provider_id,conversation_id,task_id,agent_state_json,trajectory_json,initialization_status,initialization_evidence_json,company_context_json) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(provider_id,conversation_id) DO UPDATE SET "
                "agent_state_json=excluded.agent_state_json,trajectory_json=excluded.trajectory_json,"
                "initialization_evidence_json=excluded.initialization_evidence_json,"
                "company_context_json=excluded.company_context_json",
                (provider_id, conversation_id, task_id, agent_state_json, trajectory_json,
                 conversation["initialization_status"], initialization_evidence_json, company_context_json),
            )
            updated = connection.execute(
                "UPDATE native_provider_operations SET status='completed',response_json=? "
                "WHERE provider_id=? AND operation_id=? AND request_hash=? AND status='started'",
                (response_json, provider_id, operation_id, request_hash),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("native provider operation claim was lost before completion")
            if change_record is not None:
                connection.execute(
                    "INSERT INTO native_provider_operation_changes(provider_id,operation_id,conversation_id,"
                    "before_revision,after_revision,changed_records_json) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(provider_id,operation_id) DO NOTHING",
                    (provider_id, operation_id, conversation_id, *change_record),
                )
            connection.commit()

    def complete_external_action(self, *, provider_id: str, operation_id: str,
                                 request_hash: str, state: dict[str, Any],
                                 conversation_id: str, task_id: str,
                                 conversation: dict[str, Any], response: dict[str, Any],
                                 before_state: dict[str, Any] | None = None) -> None:
        self.commit_operation(
            provider_id=provider_id, operation_id=operation_id, request_hash=request_hash,
            state=state, conversation_id=conversation_id, task_id=task_id,
            conversation=conversation, baseline=None, response=response, before_state=before_state,
        )

    def operation_changes(self, provider_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,conversation_id,before_revision,after_revision,changed_records_json "
                "FROM native_provider_operation_changes WHERE provider_id=? ORDER BY rowid", (provider_id,),
            ).fetchall()
        return [{
            "operation_id": row["operation_id"], "conversation_id": row["conversation_id"],
            "before_revision": row["before_revision"], "after_revision": row["after_revision"],
            "changed_records": json.loads(row["changed_records_json"]),
        } for row in rows]
