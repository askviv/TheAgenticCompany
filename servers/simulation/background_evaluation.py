"""Company-global provider evaluation history on the durable evaluation service.

Each subject is an actual initialized company background world. Its internal
run row exists only to reuse the run evaluation ledger and private artifact
store; it has no experiment, task, agent execution, or scenario lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import traceback
from typing import Any, Callable
import uuid

from .evaluation_service import EvaluationService
from .store import IdempotencyConflict, SQLiteStore, StoreNotFound, StoreConflict, _digest, _json


LOG = logging.getLogger(__name__)
_PINNED_PROVIDER_REVISIONS = {
    "coffeebench": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
    "ecommercebench": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
}


class BackgroundEvaluationSubjectStore:
    """Register immutable metadata for a real shared provider initialization."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        schema = """
            CREATE TABLE IF NOT EXISTS background_evaluation_subjects(
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                company_id TEXT NOT NULL,
                provider_id TEXT NOT NULL CHECK(provider_id IN ('coffeebench','ecommercebench')),
                subject_sha256 TEXT NOT NULL,
                upstream_commit TEXT NOT NULL,
                seed INTEGER NOT NULL CHECK(seed >= 0),
                started_at TEXT NOT NULL,
                source_identity_sha256 TEXT NOT NULL,
                initialization_request_sha256 TEXT NOT NULL,
                money_metric_units_json TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(company_id,provider_id,subject_sha256)
            );
            CREATE TRIGGER IF NOT EXISTS background_subjects_no_update
                BEFORE UPDATE ON background_evaluation_subjects BEGIN
                SELECT RAISE(ABORT, 'background evaluation subjects are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS background_subjects_no_delete
                BEFORE DELETE ON background_evaluation_subjects BEGIN
                SELECT RAISE(ABORT, 'background evaluation subjects are append-only');
            END;
            CREATE TABLE IF NOT EXISTS background_evaluation_attempts(
                evaluation_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL CHECK(company_id='company'),
                provider_id TEXT NOT NULL CHECK(provider_id IN ('coffeebench','ecommercebench')),
                caller_actor_id TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','identity_resolved','linked','failed')),
                upstream_commit TEXT NOT NULL,
                initialization_request_sha256 TEXT,
                source_identity_json TEXT,
                linked_run_id TEXT,
                failure_json TEXT,
                correlation_id TEXT,
                requested_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(company_id,provider_id,caller_actor_id,idempotency_key_hash)
            );
        """
        with store.transaction() as db:
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""

    def register(
        self, identity: dict[str, Any], *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        normalized = _source_identity(identity)
        identity_json = _json(normalized)
        subject_hash = hashlib.sha256(identity_json.encode()).hexdigest()
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-background:{subject_hash}"))
        created_at = datetime.now(timezone.utc).isoformat()
        with self.store._write(connection) as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_subjects "
                "WHERE company_id=? AND provider_id=? AND subject_sha256=?",
                (normalized["company_id"], normalized["provider_id"], subject_hash),
            ).fetchone()
            if row is not None:
                if row["identity_json"] != identity_json:
                    raise RuntimeError("background evaluation subject digest collision")
                return _subject_row(row)

            # This row identifies a real native source process. It is marked
            # background so active-run recovery and scenario APIs never execute
            # or reset it. No company_experiments row is created.
            db.execute(
                "INSERT INTO runs(run_id,scenario_id,generation,seed,agent_id,status,simulated_time,paused,manifest_json,erp_binding_json) "
                "VALUES(?, 'company_background', 0, ?, 'company_background', 'background', ?, 0, ?, '{}')",
                (run_id, normalized["seed"], normalized["started_at"], _json({
                    "source_kind": "company_background",
                    "provider_id": normalized["provider_id"],
                    "subject_sha256": subject_hash,
                    "upstream_commit": normalized["upstream_commit"],
                })),
            )
            db.execute(
                "INSERT INTO background_evaluation_subjects("
                "run_id,company_id,provider_id,subject_sha256,upstream_commit,seed,started_at,"
                "source_identity_sha256,initialization_request_sha256,money_metric_units_json,identity_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, normalized["company_id"], normalized["provider_id"], subject_hash,
                 normalized["upstream_commit"], normalized["seed"], normalized["started_at"],
                 normalized["source_identity_sha256"], normalized["initialization_request_sha256"],
                 _json(normalized["money_metric_units"]), identity_json, created_at),
            )
            return {
                "run_id": run_id,
                "company_id": normalized["company_id"],
                "provider_id": normalized["provider_id"],
                "subject_sha256": subject_hash,
                "upstream_commit": normalized["upstream_commit"],
                "seed": normalized["seed"],
                "started_at": normalized["started_at"],
                "source_identity_sha256": normalized["source_identity_sha256"],
                "initialization_request_sha256": normalized["initialization_request_sha256"],
                "money_metric_units": normalized["money_metric_units"],
                "identity": normalized,
            }

    def by_run_id(self, run_id: str) -> dict[str, Any]:
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_subjects WHERE run_id=?", (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError("background evaluation subject not found")
        return _subject_row(row)

    def by_digest(self, provider_id: str, subject_sha256: str) -> dict[str, Any]:
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_subjects WHERE company_id='company' "
                "AND provider_id=? AND subject_sha256=?", (provider_id, subject_sha256),
            ).fetchone()
        if row is None:
            raise KeyError("background evaluation subject not found")
        return _subject_row(row)

    def all(self, provider_id: str) -> list[dict[str, Any]]:
        with self.store._connection(None) as db:
            rows = db.execute(
                "SELECT * FROM background_evaluation_subjects WHERE company_id='company' "
                "AND provider_id=? ORDER BY created_at,subject_sha256", (provider_id,),
            ).fetchall()
        return [_subject_row(row) for row in rows]


class BackgroundEvaluationAttemptStore:
    """Reserve public evaluation identity before native identity resolution."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    def reserve(
        self, provider_id: str, caller_actor_id: str, idempotency_key: str,
        *, upstream_commit: str, initialization_request_sha256: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        if provider_id not in _PINNED_PROVIDER_REVISIONS or not caller_actor_id or not idempotency_key:
            raise ValueError("company background evaluation attempt identity is required")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request = {
            "company_id": "company", "provider_id": provider_id,
        }
        request_hash = _digest(request)
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts "
                "WHERE company_id='company' AND provider_id=? AND caller_actor_id=? AND idempotency_key_hash=?",
                (provider_id, caller_actor_id, key_hash),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict("evaluation idempotency key reused with different source initialization")
                return _attempt_row(row)
            evaluation_id = hashlib.sha256(
                f"company-background-evaluation:company:{provider_id}:{caller_actor_id}:{key_hash}".encode()
            ).hexdigest()
            db.execute(
                "INSERT INTO background_evaluation_attempts("
                "evaluation_id,company_id,provider_id,caller_actor_id,idempotency_key_hash,request_hash,status,"
                "upstream_commit,initialization_request_sha256,correlation_id,requested_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'reserved',?,?,?,?,?)",
                (evaluation_id, "company", provider_id, caller_actor_id, key_hash, request_hash,
                 upstream_commit, initialization_request_sha256, correlation_id, now, now),
            )
            return _attempt_row(db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone())

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
        return _attempt_row(row) if row else None

    def by_key(self, provider_id: str, caller_actor_id: str, idempotency_key: str) -> dict[str, Any] | None:
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE company_id='company' "
                "AND provider_id=? AND caller_actor_id=? AND idempotency_key_hash=?",
                (provider_id, caller_actor_id, key_hash),
            ).fetchone()
        return _attempt_row(row) if row else None

    def unlinked(self, provider_id: str) -> list[dict[str, Any]]:
        with self.store._connection(None) as db:
            rows = db.execute(
                "SELECT * FROM background_evaluation_attempts "
                "WHERE company_id='company' AND provider_id=? AND status!='linked' "
                "AND NOT EXISTS (SELECT 1 FROM evaluation_reservations r "
                "WHERE r.evaluation_id=background_evaluation_attempts.evaluation_id) "
                "ORDER BY requested_at,evaluation_id", (provider_id,),
            ).fetchall()
        return [_attempt_row(row) for row in rows]

    def resolve_identity(self, evaluation_id: str, identity: dict[str, Any]) -> dict[str, Any]:
        encoded = _json(identity)
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise StoreNotFound("background evaluation attempt not found")
            if row["status"] == "failed":
                return _attempt_row(row)
            prior = row["source_identity_json"]
            if prior is not None and prior != encoded:
                raise StoreConflict("background evaluation attempt resolved to a different source identity")
            if row["status"] == "reserved":
                db.execute(
                    "UPDATE background_evaluation_attempts SET status='identity_resolved',source_identity_json=?,updated_at=? "
                    "WHERE evaluation_id=? AND status='reserved'",
                    (encoded, now, evaluation_id),
                )
            current = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
        return _attempt_row(current)

    def fail(self, evaluation_id: str, failure: dict[str, str]) -> dict[str, Any]:
        encoded = _json(failure)
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise StoreNotFound("background evaluation attempt not found")
            if row["status"] in {"failed", "linked"}:
                return _attempt_row(row)
            db.execute(
                "UPDATE background_evaluation_attempts SET status='failed',failure_json=?,updated_at=? "
                "WHERE evaluation_id=? AND status IN ('reserved','identity_resolved')",
                (encoded, now, evaluation_id),
            )
            current = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
        return _attempt_row(current)

    def link(self, evaluation_id: str, run_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
            if row is None:
                raise StoreNotFound("background evaluation attempt not found")
            if row["status"] == "linked":
                if row["linked_run_id"] != run_id:
                    raise StoreConflict("background evaluation attempt links to a different source subject")
                return _attempt_row(row)
            if row["status"] != "identity_resolved" or row["source_identity_json"] is None:
                raise StoreConflict("background evaluation attempt cannot link before source identity is resolved")
            db.execute(
                "UPDATE background_evaluation_attempts SET status='linked',linked_run_id=?,updated_at=? "
                "WHERE evaluation_id=? AND status='identity_resolved'",
                (run_id, now, evaluation_id),
            )
            current = db.execute(
                "SELECT * FROM background_evaluation_attempts WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
        return _attempt_row(current)


ProviderCapture = Callable[[], dict[str, Any]]
ProviderEvaluation = Callable[[dict[str, Any]], dict[str, Any]]
ProviderVerifier = Callable[[dict[str, Any]], bool]
ProviderIdentity = Callable[[], dict[str, Any]]


class BackgroundEvaluationService:
    """Persist original CoffeeBench/E-CommerceBench results per native snapshot."""

    def __init__(
        self,
        store: SQLiteStore,
        evidence_directory: str | Path,
        *,
        providers: dict[str, dict[str, Any]],
        guard_factory: Callable[[str], Any],
    ):
        allowed = {"coffeebench", "ecommercebench"}
        if set(providers) - allowed:
            raise ValueError("unsupported company background evaluation provider")
        self.store = store
        self.subjects = BackgroundEvaluationSubjectStore(store)
        self.attempts = BackgroundEvaluationAttemptStore(store)
        self.providers = providers

        def snapshotter(run_id: str, generation: int) -> dict[str, Any]:
            subject = self.subjects.by_run_id(run_id)
            provider = self._provider_for_subject(subject)
            snapshot = provider["capture"]()
            self._verify_capture_identity(subject, snapshot)
            return {**snapshot, "provider_id": subject["provider_id"],
                    "subject_sha256": subject["subject_sha256"],
                    "company_id": subject["company_id"], "scope": "company_global"}

        def evaluator(run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
            subject = self.subjects.by_run_id(run_id)
            provider = self._provider_for_subject(subject)
            native = provider["evaluate"](snapshot)
            if not isinstance(native, dict):
                raise TypeError("native company background evaluator must return an object")
            benchmark = subject["provider_id"]
            return {
                "company_feedback": {
                    "attribution": "company_global_not_experiment",
                    "native_benchmark_result": [{"benchmark": benchmark, "result": native}],
                },
                "scope": "company_global",
                "subject_sha256": subject["subject_sha256"],
                "native_state_ref": snapshot["native_state_ref"],
                "native_state_hash": snapshot["native_state_hash"],
                "native_company_time": snapshot["native_company_time"],
                "upstream_commit": snapshot["upstream_commit"],
                "company_background_source": {
                    "company_id": subject["company_id"],
                    "provider_id": subject["provider_id"],
                    "scope": "company_global",
                    "attribution": "company_global_not_experiment",
                    "upstream_commit": subject["upstream_commit"],
                    "subject_sha256": subject["subject_sha256"],
                    "initialization_request_sha256": subject["initialization_request_sha256"],
                    "identity_fingerprint": subject["source_identity_sha256"],
                    "seed": subject["seed"],
                    "started_at": subject["started_at"],
                    "native_state_ref": snapshot["native_state_ref"],
                    "native_state_hash": snapshot["native_state_hash"],
                    "native_company_time": snapshot["native_company_time"],
                    "money_metric_units": subject["money_metric_units"],
                },
            }

        def verifier(run_id: str, snapshot: dict[str, Any]) -> bool:
            subject = self.subjects.by_run_id(run_id)
            provider = self._provider_for_subject(subject)
            return bool(provider["verify"](snapshot))

        def identity_resolver(run_id: str, generation: int, snapshot: Any) -> dict[str, Any]:
            subject = self.subjects.by_run_id(run_id)
            if snapshot is not None:
                self._verify_capture_identity(subject, snapshot)
            return {
                "benchmark": subject["provider_id"],
                "domain": "company_background",
                "benchmark_version": subject["upstream_commit"],
                "evaluation_mode": "company_adapted",
                "company_id": subject["company_id"],
            }

        self.evaluations = EvaluationService(
            store,
            evidence_directory,
            snapshotter=snapshotter,
            evaluator=evaluator,
            version_verifier=verifier,
            identity_resolver=identity_resolver,
            guard_factory=guard_factory,
        )

    def _provider_for_subject(self, subject: dict[str, Any]) -> dict[str, Any]:
        provider = self.providers.get(subject["provider_id"])
        if provider is None:
            raise RuntimeError("company background evaluator is not configured")
        return provider

    @staticmethod
    def _verify_capture_identity(subject: dict[str, Any], snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            raise TypeError("native evaluation capture must be an object")
        if snapshot.get("upstream_commit") != subject["upstream_commit"]:
            raise ValueError("native evaluation capture uses a different pinned provider revision")
        if snapshot.get("identity_fingerprint") != subject["source_identity_sha256"]:
            raise ValueError("native evaluation capture belongs to a different initialized provider world")
        if snapshot.get("initialization_request_sha256") != subject["initialization_request_sha256"]:
            raise ValueError("native evaluation capture belongs to a different initialization request")
        captured_money_unit = snapshot.get("money_unit")
        if captured_money_unit is not None:
            if (not isinstance(captured_money_unit, dict)
                    or captured_money_unit.get("currency") not in {"USD", "GBP"}
                    or str(captured_money_unit.get("native_units_per_currency_unit")) != "1"
                    or subject["money_metric_units"] != {"profitability": captured_money_unit["currency"]}):
                raise ValueError("native evaluation capture money-unit mapping changed")
        for key in ("native_state_ref", "native_state_hash", "native_company_time"):
            if not isinstance(snapshot.get(key), str) or not snapshot[key]:
                raise ValueError("native evaluation capture lacks confirmed checkpoint provenance")
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot["native_state_hash"]):
            raise ValueError("native evaluation capture checkpoint digest is invalid")

    def evaluate(
        self, provider_id: str, caller_actor_id: str, idempotency_key: str,
        *, correlation_id: str | None = None,
    ) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError("unsupported company background evaluation provider")
        if not caller_actor_id or not idempotency_key or len(idempotency_key) > 240:
            raise ValueError("evaluation caller and idempotency key are required")
        scope = f"background-evaluation:{provider_id}:{caller_actor_id}"
        request = {"company_id": "company", "provider_id": provider_id}
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        with self.store._connection(None) as db:
            existing = db.execute(
                "SELECT request_hash,response_json FROM request_idempotency WHERE scope=? AND key_hash=?",
                (scope, key_hash),
            ).fetchone()
        if existing is not None and existing["request_hash"] != _digest(request):
            raise IdempotencyConflict("idempotency key reused with different company background request data")
        # Preserve requests created by older coordinator versions; their
        # idempotency response already binds the caller to an exact subject.
        if existing is not None:
            reservation = json.loads(existing["response_json"])
            attempt = self.attempts.by_key(provider_id, caller_actor_id, idempotency_key)
            if attempt is not None:
                if attempt["evaluation_id"] != reservation["evaluation_id"]:
                    raise StoreConflict("company background idempotency response changed evaluation ID")
                if attempt["status"] == "identity_resolved":
                    self.attempts.link(attempt["evaluation_id"], reservation["run_id"])
                elif attempt["status"] == "linked" and attempt["linked_run_id"] != reservation["run_id"]:
                    raise StoreConflict("company background evaluation is linked to a different source subject")
            subject = self.subjects.by_run_id(reservation["run_id"])
            response = self.evaluations.evaluate(
                subject["run_id"], caller_actor_id, idempotency_key,
                requested_generation=0, correlation_id=correlation_id,
            )
            return self._public_record(subject, response.record)

        initialization_request = self.store.get_background_initialization_request(provider_id, "company")
        initialization_hash = _digest(initialization_request) if initialization_request is not None else None
        expected_commit = _PINNED_PROVIDER_REVISIONS[provider_id]
        attempt = self.attempts.reserve(
            provider_id, caller_actor_id, idempotency_key,
            upstream_commit=expected_commit,
            initialization_request_sha256=initialization_hash,
            correlation_id=correlation_id,
        )
        if attempt["status"] == "failed":
            return self._public_attempt(attempt)
        if attempt["status"] == "linked":
            subject = self.subjects.by_run_id(attempt["linked_run_id"])
            response = self.evaluations.evaluate(
                subject["run_id"], caller_actor_id, idempotency_key,
                requested_generation=0, correlation_id=attempt["correlation_id"],
                evaluation_id=attempt["evaluation_id"],
            )
            return self._public_record(subject, response.record)

        # The attempt ID is durable before this read-only RPC. If the worker is
        # unavailable, the failure remains visible without inventing a source
        # identity or reserving a result against an unknown world.
        with self.evaluations.guard_factory(attempt["evaluation_id"]):
            attempt = self.attempts.get(attempt["evaluation_id"])
            if attempt["status"] == "failed":
                return self._public_attempt(attempt)
            if attempt["status"] == "linked":
                subject = self.subjects.by_run_id(attempt["linked_run_id"])
            else:
                try:
                    if attempt["source_identity"] is None:
                        source_identity = _source_identity(provider["identity"]())
                        self._validate_attempt_identity(
                            provider_id, source_identity, initialization_request, attempt["upstream_commit"],
                        )
                        reserved_initialization = attempt["initialization_request_sha256"]
                        if (reserved_initialization is not None
                                and source_identity["initialization_request_sha256"] != reserved_initialization):
                            raise ValueError("native source changed after evaluation attempt reservation")
                        attempt = self.attempts.resolve_identity(
                            attempt["evaluation_id"], source_identity,
                        )
                    else:
                        source_identity = attempt["source_identity"]
                    if attempt["status"] != "identity_resolved":
                        return self._public_attempt(attempt)
                except Exception as exc:
                    self._log_attempt_failure(attempt["evaluation_id"], correlation_id, exc)
                    attempt = self.attempts.fail(
                        attempt["evaluation_id"], self._safe_failure(exc),
                    )
                    return self._public_attempt(attempt)

                def reserve(connection: sqlite3.Connection) -> dict[str, Any]:
                    subject = self.subjects.register(source_identity, connection=connection)
                    record, _created = self.evaluations.records.reserve(
                        subject["run_id"], caller_actor_id, idempotency_key,
                        requested_generation=0, correlation_id=correlation_id,
                        connection=connection, evaluation_id=attempt["evaluation_id"],
                    )
                    return {"run_id": subject["run_id"], "evaluation_id": record.evaluation_id}

                try:
                    reservation = self.store.execute_idempotent(scope, idempotency_key, request, reserve)
                    if reservation["evaluation_id"] != attempt["evaluation_id"]:
                        raise StoreConflict("background evaluation attempt resolved to a different evaluation ID")
                    subject = self.subjects.by_run_id(reservation["run_id"])
                    attempt = self.attempts.link(attempt["evaluation_id"], subject["run_id"])
                except Exception as exc:
                    self._log_attempt_failure(attempt["evaluation_id"], correlation_id, exc)
                    attempt = self.attempts.fail(
                        attempt["evaluation_id"], self._safe_failure(exc),
                    )
                    return self._public_attempt(attempt)

            response = self.evaluations.evaluate(
                subject["run_id"], caller_actor_id, idempotency_key,
                requested_generation=0, correlation_id=correlation_id,
                evaluation_id=attempt["evaluation_id"],
            )
            return self._public_record(subject, response.record)

    @staticmethod
    def _safe_failure(error: BaseException) -> dict[str, str]:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", code):
            code = "source_identity_unavailable"
        return {"code": code, "exception_type": type(error).__name__}

    @staticmethod
    def _log_attempt_failure(evaluation_id: str, correlation_id: str | None, error: BaseException) -> None:
        frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]
        LOG.error("background evaluation identity failed evaluation_id=%s correlation_id=%s exception_type=%s stack=%s",
                  evaluation_id, correlation_id or "unavailable", type(error).__name__, frames)

    @staticmethod
    def _validate_attempt_identity(
        provider_id: str, identity: dict[str, Any], initialization_request: dict[str, Any] | None,
        expected_commit: str,
    ) -> None:
        if identity["provider_id"] != provider_id or identity["upstream_commit"] != expected_commit:
            raise ValueError("native evaluation identity does not match the configured provider revision")
        if initialization_request is None:
            return
        expected_started = datetime.fromisoformat(
            str(initialization_request["started_at"]).replace("Z", "+00:00"),
        ).astimezone(timezone.utc).isoformat()
        actual_started = datetime.fromisoformat(
            identity["started_at"].replace("Z", "+00:00"),
        ).astimezone(timezone.utc).isoformat()
        if (identity["seed"] != initialization_request["seed"]
                or actual_started != expected_started
                or identity["initialization_request_sha256"] != _digest(initialization_request)):
            raise ValueError("native evaluation identity does not match the durable initialization request")

    def list(self, provider_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for subject in self.subjects.all(provider_id):
            records.extend(self._public_record(subject, record)
                           for record in self.evaluations.list(subject["run_id"], generation=0))
        records.extend(self._public_attempt(attempt) for attempt in self.attempts.unlinked(provider_id))
        return sorted(records, key=lambda item: (item.get("requested_at") or "", item["evaluation_id"]))

    def latest(self, provider_id: str) -> dict[str, Any] | None:
        records = self.list(provider_id)
        return max(records, key=lambda item: (item.get("requested_at") or "", item["evaluation_id"])) if records else None

    def get(self, evaluation_id: str) -> dict[str, Any]:
        try:
            record = self.evaluations.get_record(evaluation_id)
        except StoreNotFound:
            attempt = self.attempts.get(evaluation_id)
            if attempt is None:
                raise KeyError("company evaluation not found") from None
            return self._public_attempt(attempt)
        subject = self.subjects.by_run_id(record.run_id)
        return self._public_record(subject, record)

    @staticmethod
    def _public_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
        identity = attempt["source_identity"]
        identity_values = {}
        if identity is not None:
            identity_values = {
                "source_id": hashlib.sha256(_json(identity).encode("utf-8")).hexdigest(),
                "initialization_request_hash": identity["initialization_request_sha256"],
                "seed": identity["seed"],
                "started_at": identity["started_at"],
                "money_metric_units": identity["money_metric_units"],
            }
        failure = attempt["failure"]
        return {
            "evaluation_id": attempt["evaluation_id"],
            "company_id": attempt["company_id"],
            "provider_id": attempt["provider_id"],
            "upstream_commit": attempt["upstream_commit"],
            **identity_values,
            "evaluation_mode": "company_adapted",
            "scope": "company_global",
            "attribution": "company_global_not_experiment",
            "requested_at": attempt["requested_at"],
            "evaluated_at": attempt["updated_at"] if attempt["status"] == "failed" else None,
            "status": "failed" if attempt["status"] == "failed" else "incomplete",
            "scores": {}, "criterion_results": [], "warnings": [], "mapping_gaps": [],
            "native_scale": {}, "correlation_id": attempt["correlation_id"],
            "failure": failure,
            "lifecycle": "uncertain" if attempt["status"] != "failed" else None,
        }

    @staticmethod
    def _public_record(subject: dict[str, Any], record: Any) -> dict[str, Any]:
        public = record.public_dict()
        return {
            "evaluation_id": record.evaluation_id,
            "company_id": subject["company_id"],
            "provider_id": subject["provider_id"],
            "source_id": subject["subject_sha256"],
            "initialization_request_hash": subject["initialization_request_sha256"],
            "upstream_commit": subject["upstream_commit"],
            "seed": subject["seed"],
            "started_at": subject["started_at"],
            "money_metric_units": _scored_money_units(
                subject["provider_id"], public["scores"], subject["money_metric_units"],
            ),
            **({"native_state_ref": record.native_state_ref,
                "native_state_hash": record.native_state_hash,
                "native_company_time": record.native_company_time.isoformat()}
               if record.native_state_ref and record.native_state_hash and record.native_company_time else {}),
            "evaluation_mode": "company_adapted",
            "scope": "company_global",
            "attribution": "company_global_not_experiment",
            "requested_at": public["requested_at"],
            "evaluated_at": public["evaluated_at"],
            "status": public["status"],
            "scores": public["scores"],
            "criterion_results": public["criterion_results"],
            "warnings": public["warnings"],
            "mapping_gaps": public["mapping_gaps"],
            "native_scale": public["native_scale"],
            "evidence_ref": public["evidence_ref"],
            "evidence_hash": public["evidence_hash"],
            "correlation_id": public["correlation_id"],
            "failure": public["failure"],
            **({"lifecycle": public["lifecycle"]} if "lifecycle" in public else {}),
        }


def coffee_evaluation_provider(worker_client: Any) -> dict[str, Any]:
    """Adapt the opaque Coffee worker endpoints to the shared evaluation callbacks."""
    def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
        result = worker_client.evaluate_captured_coffee_snapshot(snapshot)
        if (result.get("native_state_hash") != snapshot.get("native_state_hash")
                or result.get("identity_fingerprint") != snapshot.get("identity_fingerprint")):
            raise RuntimeError("CoffeeBench native evaluation did not match its captured checkpoint")
        return {"native_evaluations": result["native_evaluations"]}

    return {
        "identity": worker_client.coffee_evaluation_source_identity,
        "capture": worker_client.capture_coffee_evaluation_snapshot,
        "evaluate": evaluate,
        "verify": worker_client.verify_captured_coffee_snapshot,
    }


def ecommerce_evaluation_provider(adapter: Any) -> dict[str, Any]:
    """Adapt the pinned E-Commerce source checkpoint's native report methods."""
    required = ("evaluation_source_identity", "capture_evaluation_snapshot",
                "evaluate_captured_snapshot", "verify_evaluation_snapshot")
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise RuntimeError("E-Commerce adapter lacks captured-evaluation hooks: " + ",".join(missing))

    def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
        result = adapter.evaluate_captured_snapshot(snapshot)
        if (result.get("native_state_hash") != snapshot.get("native_state_hash")
                or result.get("identity_fingerprint") != snapshot.get("identity_fingerprint")):
            raise RuntimeError("E-CommerceBench native evaluation did not match its captured checkpoint")
        report = result.get("report")
        if not isinstance(report, dict):
            raise TypeError("E-CommerceBench native report is not an object")
        return {"native_analysis": report, "native_status": result.get("native_status")}

    return {
        "identity": adapter.evaluation_source_identity,
        "capture": adapter.capture_evaluation_snapshot,
        "evaluate": evaluate,
        "verify": adapter.verify_evaluation_snapshot,
    }


def background_evaluation_guard(evidence_directory: str | Path):
    """Create the same nonblocking/recoverable evaluation lock convention."""
    lock_dir = Path(evidence_directory).parent / "evaluation-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        lock_dir.chmod(0o700)

    @contextmanager
    def guard(run_id: str):
        path = lock_dir / f"{run_id}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return guard


def _source_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("company background source identity must be an object")
    fields = {"company_id", "provider_id", "upstream_commit", "seed", "started_at",
              "identity_fingerprint", "initialization_request_sha256", "money_metric_units"}
    normalized_fields = fields - {"identity_fingerprint"} | {"source_identity_sha256"}
    if set(value) == normalized_fields:
        value = {**value, "identity_fingerprint": value["source_identity_sha256"]}
        value.pop("source_identity_sha256")
    if set(value) != fields:
        raise ValueError("company background source identity has an unexpected shape")
    if value["company_id"] != "company" or value["provider_id"] not in {"coffeebench", "ecommercebench"}:
        raise ValueError("unsupported company background evaluation source")
    if type(value["seed"]) is not int or value["seed"] < 0:
        raise ValueError("company background source seed is invalid")
    if not isinstance(value["started_at"], str) or not value["started_at"]:
        raise ValueError("company background source start time is invalid")
    try:
        started_at = datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("company background source start time is invalid") from exc
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("company background source start time must be timezone-aware")
    if not isinstance(value["upstream_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["upstream_commit"]):
        raise ValueError("company background source commit is invalid")
    identity_fingerprint = value["identity_fingerprint"]
    if not isinstance(identity_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", identity_fingerprint):
        raise ValueError("company background source identity fingerprint is invalid")
    init_hash = value["initialization_request_sha256"]
    if not isinstance(init_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", init_hash):
        raise ValueError("company background initialization request digest is invalid")
    money_units = value["money_metric_units"]
    allowed_metrics = {"profitability", "audit.annual.true_net_income"}
    if (not isinstance(money_units, dict) or not money_units
            or any(metric not in allowed_metrics or unit not in {"GBP", "USD"}
                   for metric, unit in money_units.items())):
        raise ValueError("company background financial metric unit mapping is invalid")
    expected_units = ({"audit.annual.true_net_income": "USD"}
                      if value["provider_id"] == "coffeebench"
                      else {"profitability": "GBP"})
    if money_units != expected_units:
        raise ValueError("company background financial metric units do not match the pinned provider")
    return {key: value[key] for key in sorted(fields - {"identity_fingerprint"})} | {
        "source_identity_sha256": identity_fingerprint,
    }


def _subject_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "company_id": row["company_id"],
        "provider_id": row["provider_id"],
        "subject_sha256": row["subject_sha256"],
        "upstream_commit": row["upstream_commit"],
        "seed": int(row["seed"]),
        "started_at": row["started_at"],
        "source_identity_sha256": row["source_identity_sha256"],
        "initialization_request_sha256": row["initialization_request_sha256"],
        "money_metric_units": json.loads(row["money_metric_units_json"]),
        "identity": json.loads(row["identity_json"]),
    }


def _attempt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evaluation_id": row["evaluation_id"],
        "company_id": row["company_id"],
        "provider_id": row["provider_id"],
        "caller_actor_id": row["caller_actor_id"],
        "idempotency_key_hash": row["idempotency_key_hash"],
        "request_hash": row["request_hash"],
        "status": row["status"],
        "upstream_commit": row["upstream_commit"],
        "initialization_request_sha256": row["initialization_request_sha256"],
        "source_identity": json.loads(row["source_identity_json"]) if row["source_identity_json"] else None,
        "linked_run_id": row["linked_run_id"],
        "failure": json.loads(row["failure_json"]) if row["failure_json"] else None,
        "correlation_id": row["correlation_id"],
        "requested_at": row["requested_at"],
        "updated_at": row["updated_at"],
    }


def _scored_money_units(
    provider_id: str, scores: dict[str, Any], source_units: dict[str, str],
) -> dict[str, str]:
    """Attach native currency only to financial metrics from that provider."""
    result = {}
    for metric in scores:
        if provider_id == "coffeebench" and metric.endswith(".audit.annual.true_net_income"):
            result[metric] = source_units["audit.annual.true_net_income"]
        elif provider_id == "ecommercebench" and ".profitability." in metric:
            result[metric] = source_units["profitability"]
    return result


__all__ = [
    "BackgroundEvaluationService", "BackgroundEvaluationSubjectStore",
    "background_evaluation_guard", "coffee_evaluation_provider", "ecommerce_evaluation_provider",
]
