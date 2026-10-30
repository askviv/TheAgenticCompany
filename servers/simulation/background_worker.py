"""Private worker RPC for company-clock CoffeeBench and E-CommerceBench providers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import os
import re
import traceback
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from servers.simulation.actor_protocol import bearer_token_matches
from servers.simulation.coffeebench_background import (
    UPSTREAM_COMMIT as COFFEE_COMMIT,
    advance as advance_coffee,
    background_operation_scope as coffee_operation_scope,
    configure_model_factory as configure_coffee_model_factory,
    initialize as initialize_coffee,
    _ModelResponseStore as CoffeeModelResponseStore,
    _native_evaluation as coffee_native_evaluation,
    queue_company_action as queue_coffee_company_action,
    _restore as restore_coffee,
)
from servers.simulation.ecommerce_background import (
    PROVIDER_ID as ECOMMERCE_SOURCE_ID,
    UPSTREAM_COMMIT as ECOMMERCE_COMMIT,
    PROVIDER_VERSION as ECOMMERCE_VERSION,
    apply_event as apply_ecommerce_event,
    events_between as ecommerce_events_between,
    initialize as initialize_ecommerce,
)
from servers.simulation.integration_contracts import (
    BackgroundAdvanceRequest,
    BackgroundInitializeRequest,
    BackgroundResult,
    FinanceSourceRecord,
)
from servers.simulation.feedback_contracts import NativeActionReceipt, TranslatedAction
from servers.simulation.store import IdempotencyConflict, SQLiteStore, StoreConflict, StoreNotFound


logger = logging.getLogger(__name__)


def _log_provider_failure(operation: str, provider: str, error: Exception) -> None:
    frames = [(frame.filename, frame.lineno, frame.name)
              for frame in traceback.extract_tb(error.__traceback__)]
    logger.error("background provider %s failed provider=%s exception_type=%s cause_type=%s errno=%s stack=%s",
                 operation, provider, type(error).__name__,
                 type(error.__cause__).__name__ if error.__cause__ else None,
                 getattr(error, "errno", None), frames)
_PROVIDERS = {
    "coffeebench": (COFFEE_COMMIT, initialize_coffee),
    "ecommercebench": (ECOMMERCE_COMMIT, initialize_ecommerce),
}


class BackgroundOperationUncertain(RuntimeError):
    """A claimed provider operation has no confirmed durable result."""


class BackgroundOperationConflict(RuntimeError):
    """A caller reused a durable operation identity with different input."""


class BackgroundProviderFailed(RuntimeError):
    """A background provider failed before it produced a confirmed result."""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(error_type)


class CoffeeEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    actor_ids: list[str] = Field(min_length=1)


class CoffeeRecoveryInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    previous_request: BackgroundAdvanceRequest


class CoffeeRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    previous_request: BackgroundAdvanceRequest
    recovery_request: BackgroundAdvanceRequest
    confirmed_cursor: str = Field(min_length=1)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CoffeeRecoveryInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: str = Field(pattern=r"^(started|completed)$")
    result: dict[str, Any] | None = None
    confirmed_cursor: str | None = None
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class LegacyEcommerceReceiptLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    company_id: str = Field(pattern=r"^company$")
    target_cursor: str = Field(min_length=1)
    required_event_ids: list[str]


class LegacyEcommerceReceiptCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    cursor: str = Field(min_length=1)
    next_due: str | None = None
    coverage_until: str = Field(min_length=1)
    source_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_gap_event_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_ids: list[str]
    source_event_sha256: dict[str, str]
    finance_intent_hashes: dict[str, str]


class BackgroundEvaluationCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider_id: str = Field(pattern=r"^coffeebench$")


class BackgroundEvaluationCaptureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(pattern=r"^coffeebench$")
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    native_state_ref: str = Field(min_length=1, max_length=200)
    native_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_company_time: str = Field(min_length=1)
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    money_metric_units: dict[str, str]
    seed: int = Field(ge=0)
    started_at: str = Field(min_length=1)


class CoffeeEvaluationSourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    company_id: str = Field(pattern=r"^company$")
    provider_id: str = Field(pattern=r"^coffeebench$")
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    seed: int = Field(ge=0)
    started_at: str = Field(min_length=1)
    initialization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    money_metric_units: dict[str, str]


class BackgroundEvaluationCaptureRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_ids: list[str] = Field(min_length=1)


class BackgroundWorker:
    """Own provider snapshots, claims, receipts, and private Coffee checkpoints."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        checkpoint_directory: str | Path,
        coffee_model_factory: Callable[[str, str], Any],
        configured_model_id: str,
    ):
        if not configured_model_id.strip():
            raise ValueError("an explicit Codex model ID is required for CoffeeBench background actors")
        self.store = SQLiteStore(database_path)
        self.checkpoint_directory = Path(checkpoint_directory).resolve()
        self.checkpoint_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Migrate phase metadata before accepting new operations. Legacy
        # started rows remain `unknown`; this timestamp fences off old claims
        # whose model turns cannot be correlated safely.
        self.coffee_model_responses = CoffeeModelResponseStore(
            self.checkpoint_directory / "company-coffeebench" / "model-turns.sqlite"
        )
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS coffee_evaluation_captures(
                capture_id TEXT PRIMARY KEY CHECK(length(capture_id)=64),
                company_id TEXT NOT NULL CHECK(company_id='company'),
                provider_id TEXT NOT NULL CHECK(provider_id='coffeebench'),
                upstream_commit TEXT NOT NULL,
                checkpoint_path TEXT NOT NULL,
                native_state_ref TEXT NOT NULL,
                native_state_hash TEXT NOT NULL,
                native_company_time TEXT NOT NULL,
                identity_fingerprint TEXT NOT NULL,
                initialization_request_sha256 TEXT NOT NULL,
                seed INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS coffee_eval_captures_no_update
                BEFORE UPDATE ON coffee_evaluation_captures BEGIN
                SELECT RAISE(ABORT, 'CoffeeBench evaluation captures are immutable');
            END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS coffee_eval_captures_no_delete
                BEFORE DELETE ON coffee_evaluation_captures BEGIN
                SELECT RAISE(ABORT, 'CoffeeBench evaluation captures are append-only');
            END""")
        self.configured_model_id = configured_model_id
        configure_coffee_model_factory(coffee_model_factory)

    def _provider_lock_path(self, provider_id: str, company_id: str) -> Path:
        self._provider(provider_id)
        if company_id != "company":
            raise ValueError("unsupported background company")
        return self.checkpoint_directory / f"{company_id}-{provider_id}.lock"

    def _coffee_operation_replay_safe(self, operation_id: str, created_at: str | None) -> bool:
        return bool(created_at) and self.coffee_model_responses.can_replay_operation(
            operation_id, created_at,
        )

    @asynccontextmanager
    async def _locked_provider(self, provider_id: str, company_id: str = "company"):
        """Serialize provider state transitions across threads and processes."""
        lock_file = open(self._provider_lock_path(provider_id, company_id), "a+b")
        acquire = asyncio.create_task(asyncio.to_thread(
            fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX,
        ))
        try:
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                # flock runs in a thread and cannot be cancelled. Wait for it
                # to finish so cancellation cannot strand an acquired lock.
                await acquire
                raise
            yield
        finally:
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    @contextmanager
    def _try_locked_provider(self, provider_id: str, company_id: str = "company"):
        """Probe for a live provider operation without blocking the request thread."""
        lock_file = open(self._provider_lock_path(provider_id, company_id), "a+b")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackgroundOperationUncertain("CoffeeBench provider operation is still active") from exc
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    @staticmethod
    def _provider(provider_id: str):
        try:
            return _PROVIDERS[provider_id]
        except KeyError as exc:
            raise ValueError("unsupported background provider") from exc

    def _binding(self, provider_id: str, supplied: dict[str, Any], company_id: str) -> dict[str, Any]:
        binding = deepcopy(supplied)
        if binding.get("company_id", company_id) != company_id:
            raise ValueError("background binding belongs to a different company")
        binding["company_id"] = company_id
        if provider_id == "coffeebench":
            model_id = str(binding.get("model_id", ""))
            if not model_id or model_id != self.configured_model_id:
                raise ValueError("CoffeeBench binding model_id must match the worker's configured Codex model")
            # Never accept a file path from the RPC caller. Private state paths
            # are derived from worker-owned configuration and company/provider.
            key = f"{company_id}-{provider_id}"
            binding["checkpoint_directory"] = str(self.checkpoint_directory / key)
        return binding

    @staticmethod
    def _request_dict(request: BackgroundInitializeRequest | BackgroundAdvanceRequest) -> dict[str, Any]:
        return request.model_dump(mode="json")

    @staticmethod
    def _result(
        provider_id: str,
        upstream_commit: str,
        cursor: datetime | str,
        *,
        events: list[dict[str, Any]] | None = None,
        outcomes: list[dict[str, Any]] | None = None,
        erp_intents: list[dict[str, Any]] | None = None,
        next_due: datetime | str | None = None,
        provider_status: str = "active",
    ) -> BackgroundResult:
        intents = []
        for value in erp_intents or []:
            item = dict(value)
            intents.append(FinanceSourceRecord.model_validate(item).model_dump(mode="json"))
        return BackgroundResult(
            provider_id=provider_id,
            upstream_commit=upstream_commit,
            cursor=cursor,
            next_due=next_due,
            status=provider_status,
            events=events or [],
            outcomes=outcomes or [],
            erp_intents=intents,
        )

    async def initialize(self, request: BackgroundInitializeRequest) -> BackgroundResult:
        provider_id = request.provider_id
        upstream_commit, initializer = self._provider(provider_id)
        body = self._request_dict(request)
        binding = self._binding(provider_id, request.binding, request.company_id)
        operation_id = "initialize"
        async with self._locked_provider(provider_id, request.company_id):
            if provider_id == "coffeebench":
                self.store.background_initialization_request(body)
            claim = self.store.claim_background_operation(provider_id, operation_id, body, request.company_id)
            if claim["status"] == "completed":
                return BackgroundResult.model_validate(claim["result"])
            # Initialization only constructs deterministic local state/checkpoints;
            # it performs no model calls or external business operations. A failed
            # local construction can safely resume the same exact claimed request.
            try:
                snapshot = initializer(
                    request.seed,
                    request.started_at,
                    binding,
                    request.actor_private_states,
                )
                if provider_id == "coffeebench":
                    next_due = datetime.fromisoformat(snapshot["started_at"]) + timedelta(hours=9)
                else:
                    started = datetime.fromisoformat(snapshot["started_at"])
                    next_due = datetime.combine(started.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
                result = self._result(
                    provider_id, upstream_commit, snapshot["cursor"], next_due=next_due,
                    provider_status=snapshot.get("status", "active"),
                )
                self.store.complete_background_operation(
                    provider_id,
                    operation_id,
                    body,
                    upstream_commit=upstream_commit,
                    seed=request.seed,
                    started_at=snapshot["started_at"],
                    cursor=snapshot["cursor"],
                    snapshot=snapshot,
                    result=result.model_dump(mode="json"),
                    intents=[],
                    company_id=request.company_id,
                )
                return result
            except (IdempotencyConflict, StoreConflict, BackgroundOperationUncertain):
                raise
            except Exception as exc:
                _log_provider_failure("initialization", provider_id, exc)
                raise BackgroundProviderFailed(type(exc).__name__) from exc

    async def advance(self, request: BackgroundAdvanceRequest) -> BackgroundResult:
        provider_id = request.provider_id
        async with self._locked_provider(provider_id, request.company_id):
            return await self._advance_locked(request)

    async def _advance_locked(self, request: BackgroundAdvanceRequest) -> BackgroundResult:
        provider_id = request.provider_id
        upstream_commit, _initializer = self._provider(provider_id)
        body = self._request_dict(request)
        binding = self._binding(provider_id, request.binding, request.company_id)
        state = self.store.get_background_provider_state(provider_id, request.company_id)
        if state is None:
            raise StoreNotFound("background provider has not been initialized")
        if request.until < datetime.fromisoformat(state["cursor"]):
            raise ValueError("background provider clock cannot run backwards")
        claim = self.store.claim_background_operation(
            provider_id, request.operation_id, body, request.company_id,
        )
        if claim["status"] == "completed":
            return BackgroundResult.model_validate(claim["result"])
        recovery_attempt = provider_id == "coffeebench" and self.store.is_coffee_recovery_operation(
            request.operation_id, request.company_id,
        )
        if claim["status"] == "started" and not (
            provider_id == "coffeebench" and self._coffee_operation_replay_safe(
                request.operation_id, claim.get("created_at"),
            )
        ):
            raise BackgroundOperationUncertain("provider advance has an unconfirmed prior result")

        try:
            if provider_id == "coffeebench":
                with coffee_operation_scope(request.operation_id, recovery_attempt=recovery_attempt):
                    advanced = await advance_coffee(state["snapshot"], request.until, binding)
            else:
                snapshot = state["snapshot"]
                source_events = ecommerce_events_between(
                    snapshot, snapshot["cursor"], request.until, binding,
                )
                outcomes: list[dict[str, Any]] = []
                intents: list[dict[str, Any]] = []
                current = snapshot
                for event in source_events:
                    current, event_outcomes, event_intents = apply_ecommerce_event(current, event)
                    outcomes.extend(event_outcomes)
                    intents.extend(event_intents)
                current["cursor"] = request.until.isoformat()
                advanced = {
                    "snapshot": current,
                    "events": source_events,
                    "outcomes": outcomes,
                    "erp_intents": intents,
                    "next_due": datetime.combine(
                        request.until.astimezone(timezone.utc).date() + timedelta(days=1),
                        time.min, tzinfo=timezone.utc,
                    ).isoformat(),
                }
            result = self._result(
                provider_id, upstream_commit, advanced["snapshot"]["cursor"],
                events=advanced.get("events"), outcomes=advanced.get("outcomes"),
                erp_intents=advanced.get("erp_intents"), next_due=advanced.get("next_due"),
                provider_status=advanced["snapshot"].get("status", "active"),
            )
            self.store.complete_background_operation(
                provider_id, request.operation_id, body,
                upstream_commit=upstream_commit,
                seed=int(advanced["snapshot"]["seed"]),
                started_at=advanced["snapshot"]["started_at"],
                cursor=advanced["snapshot"]["cursor"], snapshot=advanced["snapshot"],
                result=result.model_dump(mode="json"), intents=result.erp_intents,
                company_id=request.company_id,
            )
            return result
        except (IdempotencyConflict, StoreConflict, BackgroundOperationUncertain, StoreNotFound):
            raise
        except Exception as exc:
            _log_provider_failure("advance", provider_id, exc)
            raise BackgroundProviderFailed(type(exc).__name__) from exc

    def _checkpoint_identity(self, snapshot: dict[str, Any]) -> tuple[str, str]:
        checkpoint = snapshot.get("checkpoint")
        if not isinstance(checkpoint, dict) or checkpoint.get("codec") != "python-pickle-v1":
            raise StoreConflict("CoffeeBench confirmed checkpoint metadata is invalid")
        root = (self.checkpoint_directory / "company-coffeebench").resolve()
        raw_path = Path(str(checkpoint.get("path", "")))
        if raw_path.is_symlink():
            raise StoreConflict("CoffeeBench confirmed checkpoint is not a regular private file")
        path = raw_path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise StoreConflict("CoffeeBench confirmed checkpoint is unavailable")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != checkpoint.get("sha256"):
            raise StoreConflict("CoffeeBench confirmed checkpoint digest does not match provider state")
        cursor = snapshot.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise StoreConflict("CoffeeBench confirmed provider cursor is missing")
        return cursor, digest

    async def inspect_coffee_recovery(self, previous_request: BackgroundAdvanceRequest) -> CoffeeRecoveryInspection:
        if previous_request.provider_id != "coffeebench":
            raise ValueError("CoffeeBench recovery inspection only supports CoffeeBench operations")
        body = self._request_dict(previous_request)
        async with self._locked_provider("coffeebench", previous_request.company_id):
            operation = self.store.get_background_operation(
                "coffeebench", previous_request.operation_id, body, previous_request.company_id,
            )
            if operation["status"] == "completed":
                return CoffeeRecoveryInspection(status="completed", result=operation["result"])
            state = self.store.get_background_provider_state("coffeebench", previous_request.company_id)
            if state is None:
                raise StoreNotFound("CoffeeBench provider is not initialized")
            cursor, digest = self._checkpoint_identity(state["snapshot"])
            return CoffeeRecoveryInspection(
                status="started", confirmed_cursor=cursor, checkpoint_sha256=digest,
            )

    @staticmethod
    def _parse_aware_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("background receipt cursor must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def lookup_legacy_ecommerce_receipts(
        self, request: LegacyEcommerceReceiptLookupRequest,
    ) -> list[LegacyEcommerceReceiptCandidate]:
        """Find actual completed worker results that can substantiate a legacy Coffee handoff."""
        target = self._parse_aware_datetime(request.target_cursor)
        required = set(request.required_event_ids)
        if any(not value for value in required):
            raise ValueError("legacy receipt lookup contains an empty source event ID")
        candidates: list[LegacyEcommerceReceiptCandidate] = []
        state = self.store.get_background_provider_state("ecommercebench", request.company_id)
        if state is None:
            return candidates
        snapshot = state["snapshot"]
        supplier_state = snapshot.get("supplier_private_state")
        supplier_actor_id = snapshot.get("supplier_actor_id")
        supplier_id = snapshot.get("supplier_id")
        store_type = (supplier_state.get("business_model", {}).get("store_type")
                      if isinstance(supplier_state, dict) else None)
        if (snapshot.get("provider_id") != ECOMMERCE_SOURCE_ID
                or snapshot.get("provider_version") != ECOMMERCE_VERSION
                or snapshot.get("upstream_commit") != ECOMMERCE_COMMIT
                or state.get("upstream_commit") != ECOMMERCE_COMMIT
                or snapshot.get("seed") != state["seed"]
                or snapshot.get("started_at") != state["started_at"]
                or not isinstance(supplier_actor_id, str) or not supplier_actor_id
                or not isinstance(supplier_id, str) or not supplier_id
                or not isinstance(store_type, str) or not store_type):
            return candidates
        source_identity = {
            "provider_id": "ecommercebench", "provider_version": ECOMMERCE_VERSION,
            "upstream_commit": ECOMMERCE_COMMIT, "seed": int(state["seed"]),
            "started_at": str(state["started_at"]), "supplier_actor_id": supplier_actor_id,
            "supplier_id": supplier_id, "store_type": store_type,
        }
        source_identity_sha256 = hashlib.sha256(
            json.dumps(source_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False).encode("utf-8")
        ).hexdigest()
        native_binding = {
            "company_id": request.company_id,
            "supplier_actor_id": supplier_actor_id,
            "supplier_id": supplier_id,
        }
        source_start = self._parse_aware_datetime(str(state["started_at"]))
        for operation in self.store.list_completed_background_operations("ecommercebench", request.company_id):
            result = operation["result"]
            if (not isinstance(result, dict) or result.get("provider_id") != "ecommercebench"
                    or result.get("upstream_commit") != ECOMMERCE_COMMIT):
                continue
            try:
                cursor = self._parse_aware_datetime(str(result["cursor"]))
            except (KeyError, TypeError, ValueError):
                continue
            if cursor < source_start or cursor > target:
                continue
            events = result.get("events")
            intents = result.get("erp_intents")
            if not isinstance(events, list) or not isinstance(intents, list):
                continue
            try:
                next_due = (None if result.get("next_due") is None
                            else self._parse_aware_datetime(str(result["next_due"])))
            except (TypeError, ValueError):
                continue
            if next_due is None or target >= next_due:
                continue
            # Ask the pinned upstream event projector whether any native daily
            # market/calendar event falls between the result cursor and the
            # Coffee target. An empty interval proves unchanged supply coverage.
            intervening_events = ecommerce_events_between(snapshot, cursor, target, native_binding)
            if intervening_events:
                continue
            event_ids: list[str] = []
            event_hashes: dict[str, str] = {}
            valid = True
            for event in events:
                if (not isinstance(event, dict) or event.get("source") != "qwenlm-ecommercebench-calendar"
                        or event.get("upstream_commit") != ECOMMERCE_COMMIT
                        or not isinstance(event.get("event_id"), str)):
                    valid = False
                    break
                event_ids.append(event["event_id"])
                event_hashes[event["event_id"]] = hashlib.sha256(
                    json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False).encode("utf-8")
                ).hexdigest()
            intent_hashes: dict[str, str] = {}
            for intent in intents:
                if (not isinstance(intent, dict) or not isinstance(intent.get("id"), str)
                        or not intent["id"] or intent["id"] in intent_hashes):
                    valid = False
                    break
                intent_hashes[intent["id"]] = hashlib.sha256(
                    json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False).encode("utf-8")
                ).hexdigest()
            if (not valid or len(event_ids) != len(set(event_ids))
                    or not required.issubset(event_ids)):
                continue
            result_hash = hashlib.sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode("utf-8")
            ).hexdigest()
            candidates.append(LegacyEcommerceReceiptCandidate(
                operation_id=operation["operation_id"],
                created_at=operation["created_at"], completed_at=operation["completed_at"],
                upstream_commit=ECOMMERCE_COMMIT,
                cursor=cursor.isoformat(), next_due=result.get("next_due"),
                coverage_until=target.isoformat(), source_identity_sha256=source_identity_sha256,
                coverage_gap_event_ids_sha256=hashlib.sha256(
                    json.dumps([event["event_id"] for event in intervening_events], sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
                result_sha256=result_hash, source_event_ids=sorted(event_ids),
                source_event_sha256=event_hashes,
                finance_intent_hashes=intent_hashes,
            ))
        return sorted(candidates, key=lambda value: value.operation_id)

    async def recover_coffee_advance(self, request: CoffeeRecoveryRequest) -> BackgroundResult:
        previous = request.previous_request
        recovery = request.recovery_request
        if previous.provider_id != "coffeebench" or recovery.provider_id != "coffeebench":
            raise ValueError("CoffeeBench recovery requires CoffeeBench operations")
        if previous.company_id != recovery.company_id:
            raise ValueError("CoffeeBench recovery cannot change company scope")
        previous_body = self._request_dict(previous)
        recovery_body = self._request_dict(recovery)
        if ({key: value for key, value in previous_body.items() if key != "operation_id"}
                != {key: value for key, value in recovery_body.items() if key != "operation_id"}
                or previous.operation_id == recovery.operation_id):
            raise ValueError("CoffeeBench recovery must preserve exact target and binding with a new operation ID")
        async with self._locked_provider("coffeebench", previous.company_id):
            existing_resolution = self.store.get_background_recovery_resolution(
                previous.operation_id, previous.company_id,
            )
            if existing_resolution is not None:
                if (existing_resolution["previous_request"] != previous_body
                        or existing_resolution["recovery_request"] != recovery_body
                        or existing_resolution["confirmed_cursor"] != request.confirmed_cursor
                        or existing_resolution["checkpoint_sha256"] != request.checkpoint_sha256):
                    raise StoreConflict("CoffeeBench recovery attempt identity conflicts with its prior resolution")
                prior_attempt = self.store.get_background_operation(
                    "coffeebench", recovery.operation_id, recovery_body, recovery.company_id,
                )
                if prior_attempt["status"] == "completed":
                    return BackgroundResult.model_validate(prior_attempt["result"])
                state = self.store.get_background_provider_state("coffeebench", recovery.company_id)
                if state is None:
                    raise StoreNotFound("CoffeeBench provider is not initialized")
                cursor, digest = self._checkpoint_identity(state["snapshot"])
                if cursor != request.confirmed_cursor or digest != request.checkpoint_sha256:
                    raise StoreConflict("CoffeeBench recovery base changed before its operation committed")
            else:
                previous_operation = self.store.get_background_operation(
                    "coffeebench", previous.operation_id, previous_body, previous.company_id,
                )
                if previous_operation["status"] == "completed":
                    # The original worker transaction committed; return that
                    # durable result rather than creating a second attempt.
                    return BackgroundResult.model_validate(previous_operation["result"])
                state = self.store.get_background_provider_state("coffeebench", previous.company_id)
                if state is None:
                    raise StoreNotFound("CoffeeBench provider is not initialized")
                cursor, digest = self._checkpoint_identity(state["snapshot"])
                if cursor != request.confirmed_cursor or digest != request.checkpoint_sha256:
                    raise StoreConflict("CoffeeBench confirmed checkpoint changed since recovery inspection")
                self.store.prepare_coffee_background_recovery(
                    previous_body, recovery_body,
                    confirmed_cursor=cursor, checkpoint_sha256=digest,
                    company_id=previous.company_id,
                )
            return await self._advance_locked(recovery)

    async def apply_company_action(self, action: TranslatedAction) -> NativeActionReceipt:
        """Apply a mapped company action in Coffee's native clock and persist its receipt."""
        if action.benchmark != "coffeebench" or action.company_time is None:
            raise ValueError("CoffeeBench feedback action requires its pinned benchmark and company_time")
        operation_id = f"company-action:{action.action_id}"
        body = {"company_id": "company", "action": action.model_dump(mode="json")}
        async with self._locked_provider("coffeebench", "company"):
            state = self.store.get_background_provider_state("coffeebench")
            if state is None:
                raise StoreNotFound("CoffeeBench background provider has not been initialized")
            binding = self._binding("coffeebench", {
                "company_business_ids": state["snapshot"]["company_business_ids"],
                "model_id": state["snapshot"]["binding"]["model_id"],
            }, "company")
            claim = self.store.claim_background_operation(
                "coffeebench", operation_id, body, "company",
            )
            if claim["status"] == "completed":
                return self._action_receipt(claim["result"], action.action_id)
            if claim["status"] == "started" and not (
                claim.get("created_at") and
                self.coffee_model_responses.can_replay_queue_only_action(claim["created_at"])
            ):
                raise BackgroundOperationUncertain("CoffeeBench action predates queue-only action provenance")
            try:
                # Evidence may arrive between shared-clock ticks. Queue the
                # native action on Coffee's confirmed checkpoint and let the
                # next E-Commerce-first company tick execute it.
                advanced = queue_coffee_company_action(
                    state["snapshot"], action.model_dump(mode="json"), binding,
                )
                result = self._result(
                    "coffeebench", COFFEE_COMMIT, state["cursor"],
                    outcomes=advanced.get("outcomes"), erp_intents=advanced.get("erp_intents"),
                    next_due=advanced.get("next_due"),
                    provider_status=advanced["snapshot"].get("status", "active"),
                )
                self.store.complete_background_operation(
                    "coffeebench", operation_id, body,
                    upstream_commit=COFFEE_COMMIT,
                    seed=int(advanced["snapshot"]["seed"]),
                    started_at=advanced["snapshot"]["started_at"],
                    cursor=advanced["snapshot"]["cursor"],
                    snapshot=advanced["snapshot"],
                    result=result.model_dump(mode="json"),
                    intents=result.erp_intents,
                    company_id="company",
                )
                return self._action_receipt(result.model_dump(mode="json"), action.action_id)
            except (IdempotencyConflict, StoreConflict, StoreNotFound):
                raise
            except Exception as exc:
                _log_provider_failure("company action", "coffeebench", exc)
                raise BackgroundProviderFailed(type(exc).__name__) from exc

    @staticmethod
    def _action_receipt(result: dict[str, Any], action_id: str) -> NativeActionReceipt:
        outcomes = [row for row in result.get("outcomes", []) if row.get("action_id") == action_id]
        if len(outcomes) != 1:
            raise BackgroundOperationUncertain("CoffeeBench operation has no unique native action receipt")
        outcome = outcomes[0]
        accepted = outcome.get("status") in {"applied", "queued"}
        state_ref = "coffeebench-native:" + str(result.get("upstream_commit", COFFEE_COMMIT)) + ":" + action_id
        return NativeActionReceipt(
            action_id=action_id, accepted=accepted, reconciled=outcome.get("status") == "applied",
            native_state_ref=state_ref, outcome=outcome,
        )

    def reconcile_company_action(self, action: TranslatedAction) -> NativeActionReceipt | None:
        operation_id = f"company-action:{action.action_id}"
        body = {"company_id": "company", "action": action.model_dump(mode="json")}
        with self._try_locked_provider("coffeebench", "company"):
            with self.store._connection(None) as connection:
                row = connection.execute(
                    "SELECT request_hash,status,result_json FROM background_operations "
                    "WHERE company_id=? AND provider_id=? AND operation_id=?",
                    ("company", "coffeebench", operation_id),
                ).fetchone()
            if row is None:
                return None
            from servers.simulation.store import _digest

            if row["request_hash"] != _digest(body):
                raise StoreConflict("CoffeeBench company action ID was reused with different evidence")
            if row["status"] != "completed":
                # No process holds the provider lock and provider commits are
                # atomic, so started means the native action never committed.
                return None
            import json
            result = json.loads(row["result_json"])
            outcome = next((item for item in result.get("outcomes", [])
                            if item.get("action_id") == action.action_id), None)
            if outcome is not None and outcome.get("status") == "queued":
                state = self.store.get_background_provider_state("coffeebench", "company")
                native_receipt = (
                    state["snapshot"].get("company_action_receipts", {}).get(action.action_id)
                    if state is not None else None
                )
                if native_receipt is None:
                    return None
                result = {**result, "outcomes": [native_receipt["outcome"]]}
            return self._action_receipt(result, action.action_id)

    def evaluate_coffee_businesses(self, actor_ids: list[str]) -> list[dict[str, Any]]:
        state = self.store.get_background_provider_state("coffeebench")
        if state is None:
            raise StoreNotFound("CoffeeBench background provider has not been initialized")
        company_ids = state["snapshot"]["company_business_ids"]
        if not set(actor_ids).issubset(company_ids):
            raise ValueError("CoffeeBench evaluation requested an unmapped company business")
        binding = self._binding("coffeebench", {
            "company_business_ids": company_ids,
            "model_id": state["snapshot"]["binding"]["model_id"],
        }, "company")
        environment = restore_coffee(
            state["snapshot"]["checkpoint"], binding["checkpoint_directory"], company_ids,
        )
        checkpoint = state["snapshot"]["checkpoint"]
        native_state_hash = checkpoint.get("sha256")
        native_clock = state["snapshot"].get("cursor")
        if not isinstance(native_state_hash, str) or not isinstance(native_clock, str):
            raise RuntimeError("CoffeeBench native evaluation lacks its confirmed checkpoint identity")
        return [
            {
                **row,
                "native_state_hash": native_state_hash,
                "native_clock": native_clock,
                "evaluation_scope": "company_global",
                "attribution": "company_global_not_experiment",
            }
            for row in coffee_native_evaluation(environment, actor_ids)
        ]

    @staticmethod
    def _coffee_source_profile(snapshot: dict[str, Any]) -> dict[str, Any]:
        binding = snapshot.get("binding")
        if not isinstance(binding, dict):
            raise RuntimeError("CoffeeBench background source lacks its initialized role binding")
        business_ids = snapshot.get("company_business_ids")
        model_id = binding.get("model_id")
        if (not isinstance(business_ids, list) or not business_ids
                or sorted(set(business_ids)) != business_ids
                or not isinstance(model_id, str) or not model_id):
            raise RuntimeError("CoffeeBench background source identity is incomplete")
        return {
            "company_id": "company", "provider_id": "coffeebench",
            "upstream_commit": snapshot.get("upstream_commit"),
            "seed": snapshot.get("seed"), "started_at": snapshot.get("started_at"),
            "company_business_ids": business_ids, "model_id": model_id,
            "max_days": snapshot.get("max_days", 90),
            "money_metric_units": {"audit.annual.true_net_income": "USD"},
        }

    async def coffee_evaluation_source_identity(self) -> dict[str, Any]:
        async with self._locked_provider("coffeebench"):
            state = self.store.get_background_provider_state("coffeebench")
            if state is None:
                raise StoreNotFound("CoffeeBench background provider has not been initialized")
            profile = self._coffee_source_profile(state["snapshot"])
            fingerprint = hashlib.sha256(
                json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return {
                "company_id": "company", "provider_id": "coffeebench",
                "upstream_commit": state["upstream_commit"], "seed": state["seed"],
                "started_at": state["started_at"], "identity_fingerprint": fingerprint,
                "initialization_request_sha256": self._coffee_initialization_hash(),
                "money_metric_units": {"audit.annual.true_net_income": "USD"},
            }

    def _coffee_initialization_hash(self) -> str:
        request = self.store.get_background_initialization_request("coffeebench", "company")
        if not isinstance(request, dict):
            raise RuntimeError("CoffeeBench background initialization provenance is missing")
        return hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def capture_coffee_evaluation_snapshot(self) -> dict[str, Any]:
        """Pin the currently confirmed native checkpoint without advancing it."""
        async with self._locked_provider("coffeebench"):
            state = self.store.get_background_provider_state("coffeebench")
            if state is None:
                raise StoreNotFound("CoffeeBench background provider has not been initialized")
            snapshot = state["snapshot"]
            profile = self._coffee_source_profile(snapshot)
            identity_fingerprint = hashlib.sha256(
                json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            initialization_request_sha256 = self._coffee_initialization_hash()
            checkpoint = snapshot.get("checkpoint")
            if not isinstance(checkpoint, dict) or checkpoint.get("codec") != "python-pickle-v1":
                raise RuntimeError("CoffeeBench background checkpoint has an unsupported format")
            checkpoint_path = Path(str(checkpoint.get("path", ""))).resolve()
            checkpoint_root = (self.checkpoint_directory / "company-coffeebench").resolve()
            if not checkpoint_path.is_relative_to(checkpoint_root):
                raise RuntimeError("CoffeeBench evaluation checkpoint is outside the private worker directory")
            payload = checkpoint_path.read_bytes()
            native_state_hash = hashlib.sha256(payload).hexdigest()
            if native_state_hash != checkpoint.get("sha256"):
                raise StoreConflict("CoffeeBench confirmed checkpoint digest changed")
            native_state_ref = "coffeebench-native:" + hashlib.sha256(
                str(checkpoint_path).encode("utf-8")
            ).hexdigest()
            native_company_time = str(snapshot.get("cursor", ""))
            if not native_company_time:
                raise RuntimeError("CoffeeBench confirmed checkpoint lacks its company clock")
            capture_id = hashlib.sha256(
                f"{COFFEE_COMMIT}\0{identity_fingerprint}\0{initialization_request_sha256}\0"
                f"{native_state_hash}\0{native_company_time}".encode()
            ).hexdigest()
            binding_json = json.dumps({
                "company_business_ids": profile["company_business_ids"],
                "model_id": profile["model_id"],
            }, sort_keys=True, separators=(",", ":"))
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO coffee_evaluation_captures("
                    "capture_id,company_id,provider_id,upstream_commit,checkpoint_path,native_state_ref,"
                    "native_state_hash,native_company_time,identity_fingerprint,initialization_request_sha256,"
                    "seed,started_at,binding_json,created_at) VALUES(?,'company','coffeebench',?,?,?,?,?,?,?,?,?,?,?)",
                    (capture_id, COFFEE_COMMIT, str(checkpoint_path), native_state_ref,
                     native_state_hash, native_company_time, identity_fingerprint,
                     initialization_request_sha256, state["seed"], state["started_at"], binding_json,
                     datetime.now(timezone.utc).isoformat()),
                )
                row = db.execute(
                    "SELECT * FROM coffee_evaluation_captures WHERE capture_id=?", (capture_id,),
                ).fetchone()
            if (row is None or row["checkpoint_path"] != str(checkpoint_path)
                    or row["native_state_hash"] != native_state_hash
                    or row["identity_fingerprint"] != identity_fingerprint
                    or row["initialization_request_sha256"] != initialization_request_sha256):
                raise StoreConflict("CoffeeBench evaluation capture identity collided")
            return {
                "capture_id": capture_id, "provider_id": "coffeebench",
                "upstream_commit": COFFEE_COMMIT,
                "native_state_ref": native_state_ref,
                "native_state_hash": native_state_hash,
                "native_company_time": native_company_time,
                "identity_fingerprint": identity_fingerprint,
                "initialization_request_sha256": initialization_request_sha256,
                "money_metric_units": {"audit.annual.true_net_income": "USD"},
                "seed": state["seed"], "started_at": state["started_at"],
            }

    def _coffee_evaluation_capture(self, capture_id: str):
        if not re.fullmatch(r"[0-9a-f]{64}", capture_id):
            raise ValueError("CoffeeBench evaluation capture ID is invalid")
        with self.store._connection(None) as db:
            row = db.execute(
                "SELECT * FROM coffee_evaluation_captures WHERE capture_id=?", (capture_id,),
            ).fetchone()
        if row is None:
            raise StoreNotFound("CoffeeBench evaluation capture was not found")
        root = (self.checkpoint_directory / "company-coffeebench").resolve()
        path = Path(row["checkpoint_path"]).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("CoffeeBench evaluation capture is outside the private worker directory")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["native_state_hash"]:
            raise StoreConflict("CoffeeBench captured native state failed its checksum")
        ref = "coffeebench-native:" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        if ref != row["native_state_ref"]:
            raise StoreConflict("CoffeeBench captured native state reference changed")
        return row, path

    def evaluate_captured_coffee_snapshot(self, capture_id: str) -> dict[str, Any]:
        """Run the pinned native audit on saved bytes; do not initialize actors."""
        row, path = self._coffee_evaluation_capture(capture_id)
        binding = json.loads(row["binding_json"])
        environment = restore_coffee(
            {"codec": "python-pickle-v1", "sha256": row["native_state_hash"], "path": str(path)},
            self.checkpoint_directory / "company-coffeebench",
            binding["company_business_ids"],
        )
        evaluations = coffee_native_evaluation(environment, binding["company_business_ids"])
        return {
            "provider_id": "coffeebench", "upstream_commit": row["upstream_commit"],
            "native_state_ref": row["native_state_ref"],
            "native_state_hash": row["native_state_hash"],
            "native_company_time": row["native_company_time"],
            "identity_fingerprint": row["identity_fingerprint"],
            "initialization_request_sha256": row["initialization_request_sha256"],
            "native_evaluations": evaluations,
        }

    def verify_captured_coffee_snapshot(self, capture_id: str) -> bool:
        self._coffee_evaluation_capture(capture_id)
        return True


def attach_background_routes(app: FastAPI, worker: BackgroundWorker, *, bearer_secret: str) -> None:
    """Attach authenticated internal background endpoints to the supplier worker app."""
    if not bearer_secret:
        raise ValueError("background worker bearer secret is required")
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        authorization = None if credentials is None else f"{credentials.scheme} {credentials.credentials}"
        if not bearer_token_matches(authorization, bearer_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.post("/v1/background/initialize", response_model=BackgroundResult,
              dependencies=[Depends(authenticate)])
    async def initialize(request: BackgroundInitializeRequest, response: Response):
        try:
            return await worker.initialize(request)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="background_operation_conflict") from exc
        except StoreConflict as exc:
            raise HTTPException(status_code=409, detail="background_state_conflict") from exc
        except BackgroundOperationUncertain as exc:
            raise HTTPException(status_code=202, detail="background_operation_uncertain") from exc
        except BackgroundProviderFailed as exc:
            raise HTTPException(status_code=503, detail={"error_type": exc.error_type}) from exc
        except (ValueError, StoreNotFound) as exc:
            raise HTTPException(status_code=422, detail="invalid_background_initialization") from exc

    @app.post("/v1/background/advance", response_model=BackgroundResult,
              dependencies=[Depends(authenticate)])
    async def advance(request: BackgroundAdvanceRequest, response: Response):
        try:
            return await worker.advance(request)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="background_operation_conflict") from exc
        except StoreConflict as exc:
            raise HTTPException(status_code=409, detail="background_state_conflict") from exc
        except BackgroundOperationUncertain as exc:
            raise HTTPException(status_code=202, detail="background_operation_uncertain") from exc
        except BackgroundProviderFailed as exc:
            raise HTTPException(status_code=503, detail={"error_type": exc.error_type}) from exc
        except (ValueError, StoreNotFound) as exc:
            raise HTTPException(status_code=422, detail="invalid_background_advance") from exc

    @app.post("/v1/background/coffee/recovery/inspect", response_model=CoffeeRecoveryInspection,
              dependencies=[Depends(authenticate)])
    async def inspect_coffee_recovery(request: CoffeeRecoveryInspectionRequest):
        try:
            return await worker.inspect_coffee_recovery(request.previous_request)
        except (IdempotencyConflict, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="coffee_recovery_state_conflict") from exc
        except (ValueError, StoreNotFound) as exc:
            raise HTTPException(status_code=422, detail="invalid_coffee_recovery_inspection") from exc

    @app.post("/v1/background/ecommerce/legacy-receipts", dependencies=[Depends(authenticate)])
    async def lookup_legacy_ecommerce_receipts(request: LegacyEcommerceReceiptLookupRequest):
        try:
            return {"candidates": [item.model_dump(mode="json") for item in
                                   worker.lookup_legacy_ecommerce_receipts(request)]}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_legacy_ecommerce_receipt_lookup") from exc

    @app.post("/v1/background/coffee/recovery/advance", response_model=BackgroundResult,
              dependencies=[Depends(authenticate)])
    async def recover_coffee_advance(request: CoffeeRecoveryRequest):
        try:
            return await worker.recover_coffee_advance(request)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="coffee_recovery_identity_conflict") from exc
        except StoreConflict as exc:
            raise HTTPException(status_code=409, detail="coffee_recovery_state_conflict") from exc
        except BackgroundOperationUncertain as exc:
            raise HTTPException(status_code=202, detail="coffee_recovery_uncertain") from exc
        except BackgroundProviderFailed as exc:
            raise HTTPException(status_code=503, detail={"error_type": exc.error_type}) from exc
        except (ValueError, StoreNotFound) as exc:
            raise HTTPException(status_code=422, detail="invalid_coffee_recovery") from exc

    @app.post("/v1/background/company-action", response_model=NativeActionReceipt,
              dependencies=[Depends(authenticate)])
    async def apply_company_action(action: TranslatedAction):
        try:
            return await worker.apply_company_action(action)
        except (IdempotencyConflict, StoreConflict) as exc:
            raise HTTPException(status_code=409, detail="background_action_conflict") from exc
        except BackgroundOperationUncertain as exc:
            raise HTTPException(status_code=202, detail="background_action_uncertain") from exc
        except BackgroundProviderFailed as exc:
            raise HTTPException(status_code=503, detail={"error_type": exc.error_type}) from exc
        except (ValueError, StoreNotFound) as exc:
            raise HTTPException(status_code=422, detail="invalid_background_action") from exc

    @app.post("/v1/background/company-action/reconcile", response_model=NativeActionReceipt | None,
              dependencies=[Depends(authenticate)])
    def reconcile_company_action(action: TranslatedAction):
        try:
            return worker.reconcile_company_action(action)
        except StoreConflict as exc:
            raise HTTPException(status_code=409, detail="background_action_conflict") from exc
        except BackgroundOperationUncertain as exc:
            raise HTTPException(status_code=202, detail="background_action_uncertain") from exc

    @app.post("/v1/background/evaluate", response_model=list[dict[str, Any]],
              dependencies=[Depends(authenticate)])
    def evaluate_coffee_businesses(request: CoffeeEvaluationRequest):
        try:
            return worker.evaluate_coffee_businesses(request.actor_ids)
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="background_provider_not_initialized") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_background_evaluation") from exc

    @app.post("/v1/background/evaluation/source-identity", response_model=CoffeeEvaluationSourceIdentity,
              dependencies=[Depends(authenticate)])
    async def coffee_evaluation_source_identity():
        try:
            return await worker.coffee_evaluation_source_identity()
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="background_provider_not_initialized") from exc

    @app.post("/v1/background/evaluation/capture", response_model=BackgroundEvaluationCaptureResponse,
              dependencies=[Depends(authenticate)])
    async def capture_coffee_evaluation(request: BackgroundEvaluationCaptureRequest):
        try:
            return await worker.capture_coffee_evaluation_snapshot()
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="background_provider_not_initialized") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_background_evaluation_capture") from exc

    @app.post("/v1/background/evaluation/evaluate-captured", response_model=dict[str, Any],
              dependencies=[Depends(authenticate)])
    def evaluate_captured_coffee(request: BackgroundEvaluationCaptureRequestBody):
        try:
            return worker.evaluate_captured_coffee_snapshot(request.capture_id)
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="background_evaluation_capture_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_background_evaluation_capture") from exc

    @app.post("/v1/background/evaluation/verify-captured", response_model=bool,
              dependencies=[Depends(authenticate)])
    def verify_captured_coffee(request: BackgroundEvaluationCaptureRequestBody):
        try:
            return worker.verify_captured_coffee_snapshot(request.capture_id)
        except StoreNotFound as exc:
            raise HTTPException(status_code=404, detail="background_evaluation_capture_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid_background_evaluation_capture") from exc


__all__ = ["BackgroundWorker", "attach_background_routes"]
