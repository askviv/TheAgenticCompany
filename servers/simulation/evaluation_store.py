"""Append-only run evaluation reservations, lifecycle events, and outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .store import SQLiteStore, StoreConflict, StoreNotFound, _digest, _json


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)

    criterion_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    criterion: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    status: Literal["complete", "partial", "inconclusive", "incomplete", "completed",
                    "failed", "unevaluated", "not_started", "not_applicable", "evaluated"] | None = None
    score: bool | int | float | None = None
    value: bool | int | float | None = None
    actual: bool | int | float | None = None
    expected: bool | int | float | None = None
    maximum: int | float | None = None
    max_score: int | float | None = None
    weight: int | float | None = None
    gap_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    passed: bool | None = None


class EvaluationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    benchmark: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    evaluation_mode: Literal["native", "company_adapted"]
    company_id: str = "company"


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)

    schema_version: Literal[1]
    evaluation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    evaluation_version: int = Field(ge=1)
    caller_actor_id: str = Field(min_length=1)
    idempotency_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark: str | None
    domain: str | None
    evaluation_mode: Literal["native", "company_adapted"] | None
    benchmark_version: str | None
    status: Literal["completed", "incomplete", "failed"]
    lifecycle: Literal["reserved", "snapshot_captured", "evaluator_started", "uncertain", "terminal"]
    created_at: AwareDatetime
    requested_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None
    snapshot_ref: str | None
    snapshot_sha256: str | None
    result_ref: str | None
    result_sha256: str | None
    scores: dict[str, bool | int | float | str | None]
    criterion_results: list[CriterionResult]
    error_type: str | None
    company_id: str = "company"
    experiment_id: str | None = None
    evaluated_at: AwareDatetime | None = None
    warnings: list[str] = Field(default_factory=list)
    mapping_gaps: list[str] = Field(default_factory=list)
    native_scale: dict[str, str] = Field(default_factory=dict)
    correlation_id: str | None = None
    failure: dict[str, str] | None = None
    native_state_ref: str | None = None
    native_state_hash: str | None = None
    native_company_time: AwareDatetime | None = None

    def public_dict(self) -> dict[str, Any]:
        lifecycle = {"snapshot_captured": "running", "evaluator_started": "running",
                     "uncertain": "uncertain"}.get(self.lifecycle, "reserved")
        return {
            "evaluation_id": self.evaluation_id,
            "company_id": self.company_id,
            "experiment_id": self.experiment_id or self.run_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "benchmark": self.benchmark,
            "domain": self.domain,
            "benchmark_version": self.benchmark_version,
            "evaluation_mode": "native_benchmark" if self.evaluation_mode == "native" else self.evaluation_mode,
            "requested_at": (self.requested_at or self.created_at).isoformat(),
            "evaluated_at": self.evaluated_at.isoformat() if self.completed_at and self.evaluated_at else None,
            "status": self.status,
            "scores": self.scores,
            "criterion_results": [item.model_dump(mode="json", exclude_none=True) for item in self.criterion_results],
            "warnings": self.warnings,
            "mapping_gaps": self.mapping_gaps,
            "native_scale": self.native_scale,
            "evidence_ref": f"evaluation:{self.evaluation_id}:snapshot" if self.snapshot_ref else None,
            "evidence_hash": self.snapshot_sha256,
            "correlation_id": self.correlation_id,
            "failure": self.failure,
            **({"native_state_ref": self.native_state_ref,
                "native_state_hash": self.native_state_hash,
                "native_company_time": self.native_company_time.isoformat()}
               if self.native_state_ref and self.native_state_hash and self.native_company_time else {}),
            **({"lifecycle": lifecycle} if self.completed_at is None else {}),
        }


class EvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    evaluation_id: str
    sequence: int = Field(ge=1)
    event: Literal[
        "reserved", "identity_resolved", "snapshot_captured", "evaluator_started", "uncertain",
        "evaluator_result_staged", "outcome_persisted",
    ]
    occurred_at: AwareDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    record: EvaluationRecord


class EvaluationStore:
    """Private durable history over the coordinator's existing SQLite database."""

    def __init__(self, store: SQLiteStore, snapshot_directory: str | Path | None = None):
        self.store = store
        self.snapshot_directory = Path(snapshot_directory or (store.db_path.parent / "evaluation-evidence")).resolve()
        self.snapshot_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.snapshot_directory.chmod(0o700)
        with store.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS evaluation_reservations(
                    evaluation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    generation INTEGER NOT NULL,
                    evaluation_version INTEGER NOT NULL,
                    caller_actor_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id,caller_actor_id,idempotency_key_hash),
                    UNIQUE(run_id,generation,evaluation_version)
                );
                CREATE TABLE IF NOT EXISTS evaluation_lifecycle_events(
                    evaluation_id TEXT NOT NULL REFERENCES evaluation_reservations(evaluation_id),
                    sequence INTEGER NOT NULL,
                    event TEXT NOT NULL CHECK(event IN ('reserved','identity_resolved','snapshot_captured','evaluator_started','uncertain','evaluator_result_staged','outcome_persisted')),
                    occurred_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(evaluation_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS evaluation_outcomes(
                    evaluation_id TEXT PRIMARY KEY REFERENCES evaluation_reservations(evaluation_id),
                    status TEXT NOT NULL CHECK(status IN ('completed','incomplete','failed')),
                    completed_at TEXT NOT NULL,
                    snapshot_ref TEXT,
                    snapshot_sha256 TEXT,
                    result_ref TEXT,
                    result_sha256 TEXT,
                    scores_json TEXT NOT NULL,
                    criterion_results_json TEXT NOT NULL,
                    error_type TEXT,
                    public_summary_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TRIGGER IF NOT EXISTS evaluation_reservations_no_update
                    BEFORE UPDATE ON evaluation_reservations BEGIN
                    SELECT RAISE(ABORT, 'evaluation reservations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_reservations_no_delete
                    BEFORE DELETE ON evaluation_reservations BEGIN
                    SELECT RAISE(ABORT, 'evaluation reservations are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_events_no_update
                    BEFORE UPDATE ON evaluation_lifecycle_events BEGIN
                    SELECT RAISE(ABORT, 'evaluation lifecycle events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_events_no_delete
                    BEFORE DELETE ON evaluation_lifecycle_events BEGIN
                    SELECT RAISE(ABORT, 'evaluation lifecycle events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_outcomes_no_update
                    BEFORE UPDATE ON evaluation_outcomes BEGIN
                    SELECT RAISE(ABORT, 'evaluation outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS evaluation_outcomes_no_delete
                    BEFORE DELETE ON evaluation_outcomes BEGIN
                    SELECT RAISE(ABORT, 'evaluation outcomes are append-only');
                END;
            """
            # executescript commits an open transaction; execute complete DDL
            # statements individually so concurrent startup remains atomic.
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            outcome_columns = {row["name"] for row in db.execute("PRAGMA table_info(evaluation_outcomes)")}
            if "public_summary_json" not in outcome_columns:
                db.execute("ALTER TABLE evaluation_outcomes ADD COLUMN public_summary_json TEXT NOT NULL DEFAULT '{}' ")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def reserve(
        self, run_id: str, caller_actor_id: str, idempotency_key: str,
        *, requested_generation: int | None = None, correlation_id: str | None = None,
        connection: sqlite3.Connection | None = None, evaluation_id: str | None = None,
    ) -> tuple[EvaluationRecord, bool]:
        if not caller_actor_id or not idempotency_key or len(idempotency_key) > 240:
            raise ValueError("evaluation caller and idempotency key are required")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        if requested_generation is not None and requested_generation < 0:
            raise ValueError("evaluation generation cannot be negative")
        if evaluation_id is not None and (not isinstance(evaluation_id, str) or not evaluation_id):
            raise ValueError("evaluation ID must be a non-empty string")
        with self.store._write(connection) as db:
            run = self.store.get_run(run_id, connection=db)
            generation = run["generation"]
            request = {"run_id": str(run_id), "caller_actor_id": caller_actor_id,
                       "idempotency_key_hash": key_hash}
            row = db.execute(
                "SELECT * FROM evaluation_reservations WHERE run_id=? AND caller_actor_id=? AND idempotency_key_hash=?",
                (str(run_id), caller_actor_id, key_hash),
            ).fetchone()
            if row is not None:
                if requested_generation is not None and requested_generation != row["generation"]:
                    raise StoreConflict("evaluation idempotency key belongs to a different run generation")
                if row["request_hash"] != _digest(request):
                    raise StoreConflict("evaluation idempotency key was reused with different request data")
                if evaluation_id is not None and row["evaluation_id"] != evaluation_id:
                    raise StoreConflict("evaluation reservation uses a different pre-reserved ID")
                return self.get_record(row["evaluation_id"], connection=db), False
            if requested_generation is not None and requested_generation != generation:
                raise StoreConflict("evaluation request generation does not match the current run generation")
            request_hash = _digest(request)
            evaluation_version = db.execute(
                "SELECT COALESCE(MAX(evaluation_version),0)+1 FROM evaluation_reservations "
                "WHERE run_id=? AND generation=?", (str(run_id), generation),
            ).fetchone()[0]
            evaluation_id = evaluation_id or hashlib.sha256(
                f"{run_id}:{generation}:{caller_actor_id}:{key_hash}".encode()
            ).hexdigest()
            if db.execute(
                "SELECT 1 FROM evaluation_reservations WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone():
                raise StoreConflict("pre-reserved evaluation ID already belongs to another evaluation")
            created = self._now()
            db.execute(
                "INSERT INTO evaluation_reservations VALUES(?,?,?,?,?,?,?,?)",
                (evaluation_id, str(run_id), generation, evaluation_version, caller_actor_id,
                 key_hash, request_hash, created),
            )
            self._append_event(db, evaluation_id, "reserved", {
                "generation": generation, "correlation_id": correlation_id,
            })
            return self.get_record(evaluation_id, connection=db), True

    def _append_event(self, db, evaluation_id: str, event: str, metadata: dict[str, Any] | None = None) -> None:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM evaluation_lifecycle_events WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()[0]
        db.execute("INSERT INTO evaluation_lifecycle_events VALUES(?,?,?,?,?)",
                   (evaluation_id, sequence, event, self._now(), _json(metadata or {})))

    def events(self, evaluation_id: str) -> list[EvaluationEvent]:
        with self.store._connection(None) as db:
            rows = db.execute(
                "SELECT * FROM evaluation_lifecycle_events WHERE evaluation_id=? ORDER BY sequence",
                (evaluation_id,),
            ).fetchall()
        return [EvaluationEvent(evaluation_id=row["evaluation_id"], sequence=row["sequence"],
                                event=row["event"], occurred_at=row["occurred_at"],
                                metadata=json.loads(row["metadata_json"])) for row in rows]

    def append_event(self, evaluation_id: str, event: str, metadata: dict[str, Any] | None = None) -> None:
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM evaluation_reservations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
                raise StoreNotFound("evaluation reservation not found")
            self._append_event(db, evaluation_id, event, metadata)

    def current_lifecycle(self, evaluation_id: str) -> str:
        events = self.events(evaluation_id)
        return events[-1].event if events else "reserved"

    def complete(
        self, evaluation_id: str, *, status: Literal["completed", "incomplete", "failed"],
        snapshot_ref: str | None, snapshot_sha256: str | None,
        result_ref: str | None, result_sha256: str | None,
        scores: dict[str, Any], criterion_results: list[CriterionResult], error_type: str | None = None,
        public_summary: dict[str, Any] | None = None,
    ) -> EvaluationRecord:
        with self.store.transaction() as db:
            reservation = db.execute("SELECT 1 FROM evaluation_reservations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if not reservation:
                raise StoreNotFound("evaluation reservation not found")
            if db.execute("SELECT 1 FROM evaluation_outcomes WHERE evaluation_id=?", (evaluation_id,)).fetchone():
                return self.get_record(evaluation_id, connection=db)
            completed = self._now()
            if public_summary is None:
                public_summary = {"warnings": [], "mapping_gaps": [], "native_scale": {},
                                  "failure": ({"code": "evaluation_failed" if status == "failed" else "evaluation_incomplete",
                                               "exception_type": error_type} if error_type else None)}
            db.execute(
                "INSERT INTO evaluation_outcomes(evaluation_id,status,completed_at,snapshot_ref,snapshot_sha256,result_ref,result_sha256,scores_json,criterion_results_json,error_type,public_summary_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation_id, status, completed, snapshot_ref, snapshot_sha256,
                 result_ref, result_sha256, _json(scores),
                 _json([item.model_dump(mode="json", exclude_none=True) for item in criterion_results]),
                 error_type, _json(public_summary)),
            )
            self._append_event(db, evaluation_id, "outcome_persisted", {"status": status})
            return self.get_record(evaluation_id, connection=db)

    def get_record(self, evaluation_id: str, *, connection=None) -> EvaluationRecord:
        if connection is None:
            with self.store._connection(None) as stable_reader:
                stable_reader.execute("BEGIN")
                return self.get_record(evaluation_id, connection=stable_reader)
        with self.store._connection(connection) as db:
            reservation = db.execute("SELECT * FROM evaluation_reservations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if reservation is None:
                raise StoreNotFound("evaluation reservation not found")
            outcome = db.execute("SELECT * FROM evaluation_outcomes WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            latest = db.execute(
                "SELECT event FROM evaluation_lifecycle_events WHERE evaluation_id=? ORDER BY sequence DESC LIMIT 1",
                (evaluation_id,),
            ).fetchone()
            identity_row = db.execute(
                "SELECT metadata_json FROM evaluation_lifecycle_events "
                "WHERE evaluation_id=? AND event IN ('identity_resolved','snapshot_captured') "
                "ORDER BY CASE event WHEN 'snapshot_captured' THEN 1 ELSE 0 END DESC, sequence DESC LIMIT 1",
                (evaluation_id,),
            ).fetchone()
            identity = json.loads(identity_row["metadata_json"]).get("identity", {}) if identity_row else {}
            reserved = db.execute(
                "SELECT metadata_json FROM evaluation_lifecycle_events "
                "WHERE evaluation_id=? AND event='reserved' ORDER BY sequence LIMIT 1", (evaluation_id,),
            ).fetchone()
            correlation_id = (json.loads(reserved["metadata_json"]).get("correlation_id")
                              if reserved else None)
            lifecycle = latest["event"] if latest else "reserved"
            if lifecycle == "identity_resolved":
                lifecycle = "reserved"
            if lifecycle == "outcome_persisted":
                uncertain = db.execute(
                    "SELECT 1 FROM evaluation_lifecycle_events WHERE evaluation_id=? AND event='uncertain' LIMIT 1",
                    (evaluation_id,),
                ).fetchone()
                lifecycle = "uncertain" if uncertain else "terminal"
            if outcome is None:
                return EvaluationRecord(
                    schema_version=1, evaluation_id=evaluation_id, run_id=reservation["run_id"],
                    generation=reservation["generation"], evaluation_version=reservation["evaluation_version"],
                    caller_actor_id=reservation["caller_actor_id"], idempotency_key_hash=reservation["idempotency_key_hash"],
                    benchmark=identity.get("benchmark"), domain=identity.get("domain"),
                    evaluation_mode=identity.get("evaluation_mode"),
                    benchmark_version=identity.get("benchmark_version"),
                    status="incomplete", lifecycle=lifecycle,
                    created_at=reservation["created_at"], completed_at=None, snapshot_ref=None,
                    snapshot_sha256=None, result_ref=None, result_sha256=None, scores={}, criterion_results=[], error_type=None,
                    company_id=identity.get("company_id", "company"), experiment_id=reservation["run_id"],
                    correlation_id=correlation_id, requested_at=reservation["created_at"],
                )
            summary = json.loads(outcome["public_summary_json"] or "{}")
            return EvaluationRecord(
                schema_version=1, evaluation_id=evaluation_id, run_id=reservation["run_id"],
                generation=reservation["generation"], evaluation_version=reservation["evaluation_version"],
                caller_actor_id=reservation["caller_actor_id"], idempotency_key_hash=reservation["idempotency_key_hash"],
                benchmark=identity.get("benchmark"), domain=identity.get("domain"),
                evaluation_mode=identity.get("evaluation_mode"),
                benchmark_version=identity.get("benchmark_version"),
                status=outcome["status"], lifecycle=lifecycle,
                created_at=reservation["created_at"], completed_at=outcome["completed_at"],
                snapshot_ref=outcome["snapshot_ref"], snapshot_sha256=outcome["snapshot_sha256"],
                result_ref=outcome["result_ref"], result_sha256=outcome["result_sha256"],
                scores=json.loads(outcome["scores_json"]),
                criterion_results=[CriterionResult.model_validate(item) for item in json.loads(outcome["criterion_results_json"])],
                error_type=outcome["error_type"], company_id=identity.get("company_id", "company"),
                experiment_id=reservation["run_id"], evaluated_at=outcome["completed_at"],
                warnings=summary.get("warnings", []), mapping_gaps=summary.get("mapping_gaps", []),
                native_scale=summary.get("native_scale", {}), correlation_id=correlation_id,
                native_state_ref=(summary.get("native_source") or {}).get("native_state_ref"),
                native_state_hash=(summary.get("native_source") or {}).get("native_state_hash"),
                native_company_time=(summary.get("native_source") or {}).get("native_company_time"),
                failure=summary.get("failure") or (
                    {"code": "evaluation_failed" if outcome["status"] == "failed" else "evaluation_incomplete",
                     "exception_type": outcome["error_type"]}
                    if outcome["status"] in {"failed", "incomplete"} and outcome["error_type"] else None
                ),
                requested_at=reservation["created_at"],
            )

    def list_records(self, run_id: str, *, generation: int | None = None) -> list[EvaluationRecord]:
        with self.store._connection(None) as db:
            if generation is None:
                rows = db.execute("SELECT evaluation_id FROM evaluation_reservations WHERE run_id=? ORDER BY generation,evaluation_version",
                                  (str(run_id),)).fetchall()
            else:
                rows = db.execute("SELECT evaluation_id FROM evaluation_reservations WHERE run_id=? AND generation=? ORDER BY evaluation_version",
                                  (str(run_id), generation)).fetchall()
        return [self.get_record(row["evaluation_id"]) for row in rows]

    def unfinished_records(self) -> list[EvaluationRecord]:
        with self.store._connection(None) as db:
            rows = db.execute(
                "SELECT r.evaluation_id FROM evaluation_reservations r "
                "LEFT JOIN evaluation_outcomes o ON o.evaluation_id=r.evaluation_id "
                "WHERE o.evaluation_id IS NULL ORDER BY r.created_at",
            ).fetchall()
        return [self.get_record(row["evaluation_id"]) for row in rows]

    def latest_record(self, run_id: str, *, generation: int | None = None) -> EvaluationRecord | None:
        if generation is None:
            generation = self.store.get_run(run_id)["generation"]
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT evaluation_id FROM evaluation_reservations "
                "WHERE run_id=? AND generation=? ORDER BY evaluation_version DESC LIMIT 1",
                (str(run_id), generation),
            ).fetchone()
        return None if row is None else self.get_record(row["evaluation_id"])

    def write_artifact(self, evaluation_id: str, kind: Literal["snapshot", "result"], value: Any) -> tuple[str, str]:
        encoded = _json(value).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{evaluation_id}.{kind}.json"
        target = self.snapshot_directory / name
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise StoreConflict("evaluation artifact path is not a private regular file")
            existing = target.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise StoreConflict("evaluation artifact path already contains different evidence")
            return name, digest
        temporary = self.snapshot_directory / f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            if os.name == "posix":
                target.chmod(0o600)
            directory_fd = os.open(self.snapshot_directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return name, digest

    def read_artifact(self, reference: str, expected_sha256: str) -> Any:
        if not reference or Path(reference).name != reference:
            raise StoreConflict("evaluation artifact reference is outside the private evidence directory")
        candidate = self.snapshot_directory / reference
        if candidate.is_symlink():
            raise StoreConflict("evaluation artifact reference is outside the private evidence directory")
        path = candidate.resolve()
        if path.parent != self.snapshot_directory or not path.is_file():
            raise StoreConflict("evaluation artifact reference is outside the private evidence directory")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise StoreConflict("evaluation artifact checksum mismatch")
        return json.loads(data)

    def recover_artifact(self, evaluation_id: str, kind: Literal["snapshot", "result"]):
        """Recover a fully renamed private artifact whose DB event was interrupted."""
        reference = f"{evaluation_id}.{kind}.json"
        path = self.snapshot_directory / reference
        if path.is_symlink():
            raise StoreConflict("evaluation artifact path is not a private regular file")
        if not path.is_file():
            return None
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        return reference, digest, json.loads(data)

