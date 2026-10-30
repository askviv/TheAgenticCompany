"""Company-clock adapter for the pinned CoffeeBench native economy.

CoffeeBench owns its external businesses, inventory, invoices and cash books.
This adapter advances CoffeeBench's actual event handlers and projects only
native transactions involving business IDs explicitly mapped by the company.
The company coordinator remains responsible for canonical event persistence and
ERPNext posting.

Checkpoints are private trusted coordinator state. They contain a Python pickle
of CoffeeBench's object graph with model clients removed; restore requires the
configured model factory. Never load a checkpoint supplied by an API caller.
"""

from __future__ import annotations

from contextlib import closing, contextmanager, redirect_stdout
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import sqlite3
import tempfile
import threading
from typing import Any, Callable
import uuid

from servers.simulation.integration_contracts import ExternalSupplyCondition


UPSTREAM_COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
PROVIDER_ID = "sakanaai-coffeebench-native-economy"
PROVIDER_VERSION = "1"
ECOMMERCE_PROVIDER_ID = "qwenlm-ecommercebench-calendar"
ECOMMERCE_UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
_COMPANY_SUPPLY_CONDITION = "COMPANY_SUPPLY_CONDITION"
_COMPANY_ACTION = "COMPANY_ACTION"
_UPSTREAM_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "coffeebench" / "upstream"
_MODEL_FACTORY: Callable[[str, str], Any] | None = None
_BUILD_LOCK = threading.Lock()
_BACKGROUND_OPERATION_ID: ContextVar[str | None] = ContextVar(
    "coffeebench_background_operation_id", default=None
)
_BACKGROUND_RECOVERY_ATTEMPT: ContextVar[bool] = ContextVar(
    "coffeebench_background_recovery_attempt", default=False
)


@contextmanager
def background_operation_scope(operation_id: str, *, recovery_attempt: bool = False):
    """Associate provider model turns with their write-ahead operation."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("CoffeeBench model operation ID is required")
    token = _BACKGROUND_OPERATION_ID.set(operation_id)
    recovery_token = _BACKGROUND_RECOVERY_ATTEMPT.set(recovery_attempt)
    try:
        yield
    finally:
        _BACKGROUND_RECOVERY_ATTEMPT.reset(recovery_token)
        _BACKGROUND_OPERATION_ID.reset(token)


def _jsonable(value: Any) -> Any:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported CoffeeBench JSON value type: {type(value).__name__}")


class _ModelResponseStore:
    """Write-ahead exact-response cache for retry-safe upstream actor turns."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self._initialize()
        if os.name == "posix":
            self.path.chmod(0o600)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS coffee_model_turns ("
                "turn_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('started','completed')), response_json TEXT, "
                "operation_id TEXT, submission_phase TEXT NOT NULL DEFAULT 'unknown', created_at TEXT)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(coffee_model_turns)")}
            if "operation_id" not in columns:
                connection.execute("ALTER TABLE coffee_model_turns ADD COLUMN operation_id TEXT")
            if "submission_phase" not in columns:
                # Existing claims have no proof that the external request was
                # never submitted. Preserve them as uncertain; never reset.
                connection.execute(
                    "ALTER TABLE coffee_model_turns ADD COLUMN submission_phase TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "created_at" not in columns:
                connection.execute("ALTER TABLE coffee_model_turns ADD COLUMN created_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS coffee_model_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO coffee_model_store_meta(key,value) VALUES('submission_phase_schema_at',?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO coffee_model_store_meta(key,value) VALUES('queue_only_action_schema_at',?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def load_or_claim(
        self, turn_id: str, request_hash: str, operation_id: str | None = None,
        *, recovery_attempt: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash,status,response_json,operation_id,submission_phase FROM coffee_model_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is not None:
                if row[0] != request_hash:
                    connection.rollback()
                    raise RuntimeError("CoffeeBench model turn ID was reused for a different request")
                if row[1] == "completed":
                    connection.commit()
                    return json.loads(row[2]), turn_id
                if row[4] == "prepared" and row[3] == operation_id:
                    # The Codex subprocess boundary was never crossed. The
                    # same operation can safely retry this exact prompt.
                    connection.commit()
                    return None, turn_id
                if recovery_attempt:
                    # A distinct, explicitly audited recovery operation may
                    # repeat pure inference. Keep the old ambiguous turn row
                    # untouched and give this attempt its own durable claim.
                    turn_id = hashlib.sha256(
                        f"{turn_id}:recovery:{operation_id}".encode("utf-8")
                    ).hexdigest()
                    row = connection.execute(
                        "SELECT request_hash,status,response_json,operation_id,submission_phase "
                        "FROM coffee_model_turns WHERE turn_id=?", (turn_id,),
                    ).fetchone()
                    if row is not None:
                        if row[0] != request_hash:
                            connection.rollback()
                            raise RuntimeError("CoffeeBench recovery turn ID was reused for a different request")
                        if row[1] == "completed":
                            connection.commit()
                            return json.loads(row[2]), turn_id
                        if row[4] == "prepared" and row[3] == operation_id:
                            connection.commit()
                            return None, turn_id
                        connection.commit()
                        raise RuntimeError("CoffeeBench recovery model turn has an uncertain prior outcome")
                else:
                    connection.commit()
                    raise RuntimeError("CoffeeBench model turn has an uncertain prior outcome; refusing duplicate inference")
            connection.execute(
                "INSERT INTO coffee_model_turns(turn_id,request_hash,status,operation_id,submission_phase,created_at) "
                "VALUES(?,?,'started',?,'prepared',?)",
                (turn_id, request_hash, operation_id, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        return None, turn_id

    def mark_submitted(self, turn_id: str, request_hash: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE coffee_model_turns SET submission_phase='submitted' "
                "WHERE turn_id=? AND request_hash=? AND status='started' AND submission_phase='prepared'",
                (turn_id, request_hash),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("CoffeeBench model submission claim is no longer prepared")
            connection.commit()

    def can_replay_operation(self, operation_id: str, operation_created_at: str) -> bool:
        """Only replay new, operation-linked turns proven not yet submitted."""
        with closing(self._connect()) as connection:
            marker = connection.execute(
                "SELECT value FROM coffee_model_store_meta WHERE key='submission_phase_schema_at'"
            ).fetchone()
            if marker is None or datetime.fromisoformat(operation_created_at) <= datetime.fromisoformat(marker[0]):
                # Old started operations predate operation/phase provenance.
                return False
            rows = connection.execute(
                "SELECT status,submission_phase FROM coffee_model_turns WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
        return all(
            status == "completed" or (status == "started" and phase == "prepared")
            for status, phase in rows
        )

    def can_replay_queue_only_action(self, operation_created_at: str) -> bool:
        with closing(self._connect()) as connection:
            marker = connection.execute(
                "SELECT value FROM coffee_model_store_meta WHERE key='queue_only_action_schema_at'"
            ).fetchone()
        return bool(marker) and datetime.fromisoformat(operation_created_at) > datetime.fromisoformat(marker[0])

    def complete(self, turn_id: str, request_hash: str, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE coffee_model_turns SET status='completed',response_json=? "
                "WHERE turn_id=? AND request_hash=? AND status='started'",
                (encoded, turn_id, request_hash),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RuntimeError("CoffeeBench model turn writeahead claim was lost")
            connection.commit()


class _IdempotentCoffeeModel:
    def __init__(self, actor_id: str, model: Any, store: _ModelResponseStore):
        self.actor_id = actor_id
        self.model = model.model
        self._model = model
        self._store = store

    def query(self, messages, tools=None):
        from coffeebench.models.types import ModelResponse, ToolCall

        request = {"actor_id": self.actor_id, "messages": _jsonable(messages), "tools": _jsonable(tools or [])}
        serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        turn_id = hashlib.sha256(f"{self.actor_id}:{request_hash}".encode("utf-8")).hexdigest()
        operation_id = _BACKGROUND_OPERATION_ID.get()
        cached, turn_id = self._store.load_or_claim(
            turn_id, request_hash, operation_id,
            recovery_attempt=_BACKGROUND_RECOVERY_ATTEMPT.get(),
        )
        if cached is None:
            response = self._model.query(
                messages, tools=tools,
                on_submission_started=lambda: self._store.mark_submitted(turn_id, request_hash),
            )
            if response.raw is not None:
                raise RuntimeError("CoffeeBench background model returned non-replayable raw provider state")
            cached = {
                "content": response.content,
                "thinking": response.thinking,
                "tool_calls": _jsonable(response.tool_calls),
                "stop_reason": response.stop_reason,
                "cost": response.cost,
            }
            self._store.complete(turn_id, request_hash, cached)
        return ModelResponse(
            content=cached["content"],
            thinking=cached["thinking"],
            tool_calls=[ToolCall(**call) for call in cached["tool_calls"]],
            stop_reason=cached["stop_reason"],
            cost=cached["cost"],
            raw=None,
        )

    def get_usage_stats(self):
        return self._model.get_usage_stats()


def configure_model_factory(factory: Callable[[str, str], Any]) -> None:
    """Install the process-local upstream Model factory used to create/restore actors.

    The factory is deliberately not serialized with a company checkpoint. Its
    implementation owns transport credentials and must return an upstream
    CoffeeBench ``Model`` implementation for ``(actor_id, model_id)``.
    """
    if not callable(factory):
        raise TypeError("CoffeeBench model factory must be callable")
    global _MODEL_FACTORY
    _MODEL_FACTORY = factory


def _upstream_imports():
    import sys

    root = str(_UPSTREAM_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from coffeebench import main as upstream_main
    return upstream_main


def _as_datetime(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("CoffeeBench clock values must be timezone-aware")
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("CoffeeBench clock values must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _checkpoint_directory(binding: dict[str, Any]) -> Path:
    directory = binding.get("checkpoint_directory")
    if not isinstance(directory, str) or not directory:
        raise ValueError("company binding must include the private CoffeeBench worker checkpoint directory")
    return Path(directory)


def _build_environment(seed: int, binding: dict[str, Any]):
    if _MODEL_FACTORY is None:
        raise RuntimeError("CoffeeBench model factory has not been configured")
    upstream_main = _upstream_imports()
    actor_ids = ("farmer_A", "farmer_B", "roaster_A", "roaster_B", "retailer_A", "retailer_B")
    model_id = str(binding.get("model_id", ""))
    if not model_id:
        raise ValueError("CoffeeBench company binding requires an explicit model_id")
    next_actor = iter(actor_ids)
    model_store = _ModelResponseStore(_checkpoint_directory(binding) / "model-turns.sqlite")

    # Reuse CoffeeBench's own wiring and world construction. The temporary
    # output/event files are closed and removed before this function returns.
    output_file = Path(tempfile.gettempdir()) / f"coffee-company-{uuid.uuid4().hex}.json"
    args = type("CoffeeRunArgs", (), {
        "config": None,
        "max_days": int(binding.get("max_days", 90)),
        "seed": int(seed),
        "model": "passive",  # keeps upstream context compaction disabled; factory supplies the configured actor model
        "models": None,
        "main_agent": None,
        "output": str(output_file),
    })()

    original_get_model = upstream_main.get_model

    def model_for(_model_id: str):
        try:
            actor_id = next(next_actor)
        except StopIteration as exc:
            raise RuntimeError("CoffeeBench requested more actor models than its native world defines") from exc
        return _IdempotentCoffeeModel(actor_id, _MODEL_FACTORY(actor_id, model_id), model_store)

    with _BUILD_LOCK:
        try:
            upstream_main.get_model = model_for
            with redirect_stdout(io.StringIO()):
                environment, _output_path, _events_path = upstream_main.build_run(args)
        finally:
            upstream_main.get_model = original_get_model
    # build_run creates a logger for its standard CLI path. Company-clock
    # advancement does not use the CLI stream; native state is checkpointed.
    if environment.event_logger is not None:
        environment.event_logger.close()
        environment.event_logger = None
    Path(str(output_file)[:-5] + ".events.jsonl").unlink(missing_ok=True)
    environment._company_clock_started = False
    environment._company_clock_finalized = False
    environment._company_clock_cursor = 0
    environment._company_start = None
    return environment


class _ModelSlot:
    """Pickle-safe marker replaced by a configured provider model on restore."""

    def __init__(self, model_id: str):
        self.model = model_id

    def query(self, *_args, **_kwargs):
        raise RuntimeError("CoffeeBench model was not reattached to its private checkpoint")


def _checkpoint(environment: Any, checkpoint_directory: str | Path) -> dict[str, str]:
    old: list[tuple[Any, Any, Any]] = []
    marketplace_lock = environment.marketplace.lock
    last_active = environment._last_active_day_per_agent
    spoilage = environment._spoilage_carry
    record_truth_callbacks = {actor_id: business._record_truth for actor_id, business in environment.business_apps.items()}
    try:
        # CoffeeBench's lock protects marketplace mutation only; its lock
        # state is not business data and cannot be pickled.
        environment.marketplace.lock = None
        environment._last_active_day_per_agent = dict(last_active)
        environment._spoilage_carry = {agent_id: dict(items) for agent_id, items in spoilage.items()}
        # Environment.__init__ binds equivalent local lambdas on each app;
        # the bound native method has the same contract and is pickleable.
        for business in environment.business_apps.values():
            business._record_truth = environment._record_truth
        for actor_id, agent in environment.agents.items():
            old.append((agent, agent.model, agent.compactor))
            model_id = str(getattr(agent.model, "model", ""))
            agent.model = _ModelSlot(model_id)
            agent.compactor = None
        encoded = pickle.dumps(environment, protocol=pickle.HIGHEST_PROTOCOL)
    finally:
        environment.marketplace.lock = marketplace_lock
        environment._last_active_day_per_agent = last_active
        environment._spoilage_carry = spoilage
        for actor_id, callback in record_truth_callbacks.items():
            environment.business_apps[actor_id]._record_truth = callback
        for agent, model, compactor in old:
            agent.model = model
            agent.compactor = compactor
    root = Path(checkpoint_directory).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"coffee-world-{uuid.uuid4().hex}.pickle"
    fd, temporary_name = tempfile.mkstemp(prefix=".coffee-world-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return {
        "codec": "python-pickle-v1",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "path": str(path),
    }


def _restore(
    checkpoint: dict[str, str], checkpoint_directory: str | Path,
    company_business_ids: list[str] | None = None,
):
    if _MODEL_FACTORY is None:
        raise RuntimeError("CoffeeBench model factory has not been configured")
    if checkpoint.get("codec") != "python-pickle-v1":
        raise ValueError("unsupported CoffeeBench checkpoint codec")
    root = Path(checkpoint_directory).resolve()
    path = Path(str(checkpoint.get("path", ""))).resolve()
    if not path.is_relative_to(root):
        raise ValueError("CoffeeBench checkpoint reference is outside its private worker directory")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("CoffeeBench checkpoint is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != checkpoint.get("sha256"):
        raise ValueError("CoffeeBench checkpoint checksum mismatch")
    # This is trusted coordinator-owned state, never API supplied input.
    environment = pickle.loads(payload)
    import threading
    from collections import defaultdict
    environment.marketplace.lock = threading.RLock()
    environment._last_active_day_per_agent = defaultdict(lambda: -1, environment._last_active_day_per_agent)
    environment._spoilage_carry = defaultdict(
        lambda: defaultdict(float),
        {agent_id: defaultdict(float, items) for agent_id, items in environment._spoilage_carry.items()},
    )
    model_store = _ModelResponseStore(root / "model-turns.sqlite")
    company_business_ids = set(company_business_ids or getattr(environment, "_company_business_ids", []))
    environment._company_business_ids = sorted(company_business_ids)
    for actor_id, agent in environment.agents.items():
        model_id = str(agent.model.model)
        if actor_id in company_business_ids:
            from coffeebench.models.passive_model import PassiveModel

            agent.model = PassiveModel()
        else:
            agent.model = _IdempotentCoffeeModel(
                actor_id, _MODEL_FACTORY(actor_id, model_id), model_store,
            )
        agent.compactor = None
    return environment


def initialize(
    seed: int,
    started_at: datetime | date | str,
    binding: dict[str, Any],
    actor_private_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Construct the actual CoffeeBench world and return its private checkpoint."""
    del actor_private_states  # CoffeeBench businesses have independent private books.
    actual_start = _as_datetime(started_at)
    start = datetime(actual_start.year, actual_start.month, actual_start.day, tzinfo=timezone.utc)
    mapped = binding.get("company_business_ids")
    if not isinstance(mapped, list) or not mapped or not set(mapped).issubset({
        "farmer_A", "farmer_B", "roaster_A", "roaster_B", "retailer_A", "retailer_B",
    }):
        raise ValueError("binding must explicitly map one or more native CoffeeBench business IDs")
    environment = _build_environment(int(seed), binding)
    environment._company_start = start.isoformat()
    _start_environment(environment)
    native_delivery_lags = {
        item_id: int(item.delivery_lag_days)
        for item_id, item in environment.marketplace.items.items()
    }
    return {
        "provider_id": PROVIDER_ID,
        "provider_version": PROVIDER_VERSION,
        "snapshot_schema_version": 3,
        "upstream_commit": UPSTREAM_COMMIT,
        "seed": int(seed),
        "max_days": int(binding.get("max_days", 90)),
        "started_at": start.isoformat(),
        "cursor": start.isoformat(),
        "status": "active",
        "company_business_ids": sorted(set(str(x) for x in mapped)),
        "binding": {
            "company_business_ids": sorted(set(str(x) for x in mapped)),
            "model_id": str(binding["model_id"]),
        },
        "checkpoint": _checkpoint(environment, _checkpoint_directory(binding)),
        "applied_event_ids": [],
        "event_outcomes": [],
        "native_delivery_lag_baseline": native_delivery_lags,
        "external_supply_conditions": [],
        "native_message_ids": sorted(
            str(message.id) for message in environment.marketplace.messages
        ),
    }


def _start_environment(environment: Any) -> None:
    from coffeebench.event_loop import EVENT_EOD_MECHANICS, EVENT_MORNING_OPEN
    from coffeebench.environment import BUSINESS_HOURS_END, BUSINESS_HOURS_START

    if environment._company_clock_started:
        return
    for agent in environment.agents.values():
        agent.init()
    environment._generate_demand_paths()
    environment._generate_loyalty_multipliers()
    environment._emit(
        "run_start",
        agent_ids=list(environment.agents.keys()),
        max_days=environment.max_days,
        models={actor_id: actor.model.model for actor_id, actor in environment.agents.items()},
        demand_paths=dict(environment._demand_path),
        retailer_loyalty=dict(environment._retailer_loyalty),
    )
    environment.event_loop.schedule_at(BUSINESS_HOURS_START, EVENT_MORNING_OPEN, payload={"day": 0})
    environment.event_loop.schedule_at(BUSINESS_HOURS_END, EVENT_EOD_MECHANICS, payload={"day": 0})
    environment._company_clock_started = True


async def _advance_environment(environment: Any, target_minute: int) -> None:
    """Dispatch native CoffeeBench events through a bounded virtual minute."""
    import asyncio
    from coffeebench.environment import (
        BUSINESS_HOURS_END,
        ContextOverflowError,
        NonTerminatingException,
        TerminatingException,
    )
    from coffeebench.event_loop import (
        EVENT_AGENT_WAKE,
        EVENT_EOD_MECHANICS,
        EVENT_MORNING_OPEN,
    )

    if target_minute < environment._company_clock_cursor:
        raise ValueError("CoffeeBench company clock cannot run backwards")
    if not environment._company_clock_started:
        _start_environment(environment)

    while environment.event_loop and environment.event_loop.peek_time() is not None:
        if environment.event_loop.peek_time() > target_minute:
            break
        if environment._terminated_early is not None:
            break
        bucket = environment.event_loop.pop_bucket()
        if not bucket:
            break
        bucket_vt = bucket[0].virtual_min
        environment.time_manager.virtual_min = bucket_vt
        for ev in bucket:
            if ev.kind == _COMPANY_SUPPLY_CONDITION:
                _handle_company_supply_condition(environment, ev.payload)
            elif ev.kind == _COMPANY_ACTION:
                _handle_company_action(environment, ev.payload)
            elif ev.kind == EVENT_MORNING_OPEN:
                environment._handle_morning_open(int(ev.payload.get("day", bucket_vt // 1440)))
                _apply_company_supply_wake_reasons(environment)
            elif ev.kind == EVENT_EOD_MECHANICS:
                environment._handle_eod_mechanics(int(ev.payload.get("day", bucket_vt // 1440)))

        ready: list[tuple[Any, int]] = []
        seen_agents: set[str] = set()
        for ev in bucket:
            if ev.kind != EVENT_AGENT_WAKE:
                continue
            actor_id = ev.target_agent
            if actor_id is None or actor_id in environment._bankrupt_agents or actor_id in seen_agents:
                continue
            if environment._available_at.get(actor_id) != ev.virtual_min:
                continue
            day = ev.virtual_min // 1440
            if ev.virtual_min >= day * 1440 + BUSINESS_HOURS_END:
                environment._available_at[actor_id] = day * 1440 + BUSINESS_HOURS_END
                continue
            if not environment._prepare_agent_step(actor_id, day):
                environment._available_at[actor_id] = day * 1440 + BUSINESS_HOURS_END
                continue
            seen_agents.add(actor_id)
            ready.append((ev, day))
        if not ready:
            continue
        responses = await asyncio.gather(
            *(environment.agents[ev.target_agent].step_query_async() for ev, _ in ready),
            return_exceptions=True,
        )
        for (ev, day), response in zip(ready, responses):
            actor_id = ev.target_agent
            day_close = day * 1440 + BUSINESS_HOURS_END
            if isinstance(response, ContextOverflowError):
                environment._mark_bankrupt(actor_id, day, "context_overflow", detail=str(response)[:200])
                environment._available_at[actor_id] = day_close
            elif isinstance(response, TerminatingException):
                environment._available_at[actor_id] = day_close
            elif isinstance(response, NonTerminatingException):
                environment._schedule_next_wake(actor_id, ev.virtual_min, "FORMAT_ERROR")
            elif isinstance(response, BaseException):
                raise response
            else:
                action = environment._apply_agent_step(actor_id, day, response)
                if action is None:
                    environment._available_at[actor_id] = day_close
                else:
                    environment._schedule_next_wake(actor_id, ev.virtual_min, action)
    environment._company_clock_cursor = int(target_minute)


def _condition_start_minute(condition: dict[str, Any], started_at: str) -> int:
    occurred = _as_datetime(condition["occurred_at"])
    start = _as_datetime(started_at)
    seconds = (occurred - start).total_seconds()
    if seconds < 0 or seconds % 60:
        raise ValueError("E-Commerce supply condition must fall on a CoffeeBench minute within its company horizon")
    return int(seconds // 60)


def _validate_supply_condition(value: dict[str, Any], started_at: str, until: datetime) -> dict[str, Any]:
    condition = ExternalSupplyCondition.model_validate(value).model_dump(mode="json")
    if condition["source"] != ECOMMERCE_PROVIDER_ID or condition["upstream_commit"] != ECOMMERCE_UPSTREAM_COMMIT:
        raise ValueError("E-Commerce supply condition provenance is not the pinned provider")
    occurred_at = _as_datetime(condition["occurred_at"])
    effective_until = _as_datetime(condition["effective_until"])
    if effective_until != occurred_at + timedelta(days=condition["duration_days"]):
        raise ValueError("E-Commerce condition expiry does not match its source duration")
    start_minute = _condition_start_minute(condition, started_at)
    if occurred_at > until:
        raise ValueError("E-Commerce supply condition is later than the requested CoffeeBench clock")
    condition["start_minute"] = start_minute
    condition["end_minute"] = _condition_start_minute({"occurred_at": effective_until.isoformat()}, started_at)
    return condition


def _recompute_native_delivery_lags(environment: Any) -> None:
    conditions = getattr(environment, "_company_external_supply_conditions", {})
    active = [
        Decimal(str(condition["delivery_delay_multiplier"]))
        for condition in conditions.values() if condition.get("status") == "active"
    ]
    multiplier = max(active, default=Decimal("1"))
    baseline = environment._company_native_delivery_lag_baseline
    missing = set(baseline).difference(environment.marketplace.items)
    if missing:
        raise RuntimeError("CoffeeBench native item catalog changed while a source condition was active")
    for item_id, base_lag in baseline.items():
        environment.marketplace.items[item_id].delivery_lag_days = math.ceil(Decimal(base_lag) * multiplier)


def _schedule_company_supply_wake(environment: Any, condition: dict[str, Any], *, active: bool) -> None:
    reason = (
        f"E-CommerceBench global logistics condition {condition['event_name']} "
        f"({condition['event_id']}) {'started' if active else 'ended'}; "
        f"source delivery delay multiplier {condition['delivery_delay_multiplier']}x."
    )
    pending = getattr(environment, "_company_pending_supply_wake_reasons", {})
    for actor_id in sorted(getattr(environment, "_company_business_ids", [])):
        if actor_id in environment._bankrupt_agents:
            continue
        pending.setdefault(actor_id, []).append(reason)
    environment._company_pending_supply_wake_reasons = pending


def _handle_company_supply_condition(environment: Any, payload: dict[str, Any]) -> None:
    condition = environment._company_external_supply_conditions[payload["event_id"]]
    condition["status"] = payload["status"]
    _recompute_native_delivery_lags(environment)
    _schedule_company_supply_wake(
        environment, condition, active=payload["status"] == "active",
    )


def _apply_company_supply_wake_reasons(environment: Any) -> None:
    pending = getattr(environment, "_company_pending_supply_wake_reasons", {})
    for actor_id, reasons in pending.items():
        if actor_id in environment.agents and actor_id not in environment._bankrupt_agents and reasons:
            environment._pending_wake_reason[actor_id] = " ".join(reasons)
    environment._company_pending_supply_wake_reasons = {}


def _handle_company_action(environment: Any, payload: dict[str, Any]) -> None:
    from coffeebench.models.types import ModelResponse, ToolCall

    action_id = payload["action_id"]
    pending = environment._company_pending_actions.pop(action_id)
    actor_id = pending["actor_id"]
    if actor_id not in environment._company_business_ids:
        raise ValueError("CoffeeBench company action actor is not explicitly mapped to this company")
    agent = environment.agents[actor_id]
    if getattr(agent.model, "model", None) != "passive":
        raise RuntimeError("mapped CoffeeBench company actors must not run an independent model policy")
    minute = int(environment.time_manager.get_virtual_min())
    day = minute // 1440
    preflight_error = _company_payment_preflight_error(environment, pending)
    if preflight_error:
        # Preserve the company payment evidence, but do not call Coffee's
        # pay_invoice tool when its current native balance differs.
        native_result = {"status": "inconclusive", "message": preflight_error}
        status = "inconclusive"
    else:
        response = ModelResponse(
            tool_calls=[ToolCall(
                id=action_id,
                name=pending["native_action"],
                input=pending["arguments"],
            )],
            stop_reason="tool_use",
        )
        action_name = environment._apply_agent_step(actor_id, day, response)
        if action_name is not None:
            environment._schedule_next_wake(actor_id, minute, action_name)
        observation = agent.messages[-1].get("content", "") if agent.messages else ""
        # Agent.step_apply appends a tool-result message after the assistant turn.
        if len(agent.messages) >= 2 and agent.messages[-1].get("role") == "tool":
            observation = agent.messages[-1].get("content", "")
        try:
            native_result = json.loads(observation)
        except (TypeError, ValueError):
            native_result = {"status": "inconclusive", "message": "CoffeeBench returned a non-JSON tool result"}
        status = "applied" if isinstance(native_result, dict) and native_result.get("status") == "success" else "inconclusive"
    if pending["native_action"] == "pay_invoice" and status == "applied":
        native_result, status = _verify_company_payment_readback(environment, pending, native_result)
    if pending["native_action"] == "send_message" and status == "applied":
        from coffeebench.event_loop import EVENT_AGENT_WAKE, WAKE_DM_RECEIVED

        expected = pending["arguments"]
        native_message_id = native_result.get("message_id")
        matches = [message for message in environment.marketplace.messages if message.id == native_message_id]
        message = matches[0] if len(matches) == 1 else None
        message_verified = bool(
            message is not None
            and message.sender_id == actor_id
            and message.recipient_id == expected["recipient"]
            and message.title == str(expected["title"]).strip()
            and message.body == expected["body"]
            and message.ref_listing_id == expected.get("ref_listing_id")
            and message.ref_offer_id == expected.get("ref_offer_id")
        )
        recipient_id = expected["recipient"]
        wake_reason = environment._pending_wake_reason.get(recipient_id, "")
        available_at = environment._available_at.get(recipient_id)
        wake_scheduled = bool(
            recipient_id not in environment._bankrupt_agents
            and WAKE_DM_RECEIVED in wake_reason
            and any(
                event.kind == EVENT_AGENT_WAKE
                and event.target_agent == recipient_id
                and event.virtual_min == available_at
                for _minute, _counter, event in environment.event_loop._heap
            )
        )
        native_result["conversation_receipt"] = {
            "native_message_id": native_message_id,
            "sender_id": actor_id,
            "recipient_id": recipient_id,
            "message_verified": message_verified,
            "counterparty_wake_scheduled": wake_scheduled,
        }
        if not message_verified or not wake_scheduled:
            status = "inconclusive"
    outcome = {
        "action_id": action_id,
        "evidence_id": pending["evidence_id"],
        "mapping_id": pending["mapping_id"],
        "actor_id": actor_id,
        "native_action": pending["native_action"],
        "arguments": pending["arguments"],
        "supporting_evidence": pending.get("supporting_evidence", []),
        "company_time": pending["company_time"],
        "native_applied_at_minute": minute,
        "status": status,
        "native_result": native_result,
        "upstream_commit": UPSTREAM_COMMIT,
    }
    environment._company_action_receipts[action_id] = {
        "request_hash": pending["request_hash"],
        "outcome": outcome,
    }
    environment._company_action_outcomes.append(outcome)


def _company_payment_preflight_error(environment: Any, pending: dict[str, Any]) -> str | None:
    if pending.get("native_action") != "pay_invoice":
        return None
    evidence = pending.get("supporting_evidence")
    if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict):
        return "native invoice payment lacks one exact submitted ERP payment proof"
    proof = evidence[0]
    invoice_id = pending.get("arguments", {}).get("invoice_id")
    if (
        proof.get("invoice_id") != invoice_id
        or proof.get("evidence_id") != pending.get("evidence_id")
        or not isinstance(proof.get("payment_entry"), str)
        or not proof["payment_entry"]
        or type(proof.get("docstatus")) is not int
        or proof["docstatus"] != 1
        or proof.get("currency") != "USD"
    ):
        return "native invoice payment proof does not identify the submitted USD payment for this invoice"
    try:
        expected_amount = Decimal(str(proof["amount"]))
    except (KeyError, ArithmeticError, ValueError):
        return "native invoice payment proof has no valid amount"
    if not expected_amount.is_finite() or expected_amount <= 0:
        return "native invoice payment proof amount is not positive and finite"
    actor = environment.business_apps.get(pending["actor_id"])
    matches = [] if actor is None else [
        invoice for invoice in actor.accounts_payable if invoice.id == invoice_id
    ]
    if len(matches) != 1:
        return "native invoice payment proof does not identify one open CoffeeBench payable"
    invoice = matches[0]
    if invoice.payer != pending["actor_id"] or invoice.paid or invoice.returned or invoice.bad_debt:
        return "native CoffeeBench invoice is no longer an open payable for the mapped company actor"
    native_amount = Decimal(str(invoice.net_outstanding))
    if not native_amount.is_finite() or native_amount <= 0 or native_amount != expected_amount:
        return "current CoffeeBench invoice balance differs from the submitted ERP payment amount"
    return None


def _verify_company_payment_readback(
    environment: Any, pending: dict[str, Any], native_result: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    proof = pending["supporting_evidence"][0]
    try:
        expected_amount = Decimal(str(proof["amount"]))
        paid_amount = Decimal(str(native_result["paid_amount"]))
    except (KeyError, ArithmeticError, ValueError):
        native_result["payment_readback"] = {"verified": False, "reason": "native paid_amount is missing or invalid"}
        return native_result, "inconclusive"
    actor = environment.business_apps[pending["actor_id"]]
    matches = [invoice for invoice in actor.accounts_payable if invoice.id == proof["invoice_id"]]
    invoice_paid = len(matches) == 1 and matches[0].paid and matches[0].paid_date is not None
    verified = paid_amount.is_finite() and paid_amount == expected_amount and invoice_paid
    native_result["payment_readback"] = {
        "verified": verified,
        "payment_entry": proof["payment_entry"],
        "evidence_id": proof["evidence_id"],
        "invoice_id": proof["invoice_id"],
        "currency": proof["currency"],
        "erp_amount": str(expected_amount),
        "native_paid_amount": str(paid_amount),
        "native_invoice_paid": invoice_paid,
    }
    if not verified:
        native_result["payment_readback"]["reason"] = "native payment receipt does not match the ERP payment proof"
        return native_result, "inconclusive"
    return native_result, "applied"


def _queue_company_actions(
    environment: Any,
    snapshot: dict[str, Any],
    values: list[dict[str, Any]],
    *,
    until: datetime,
) -> list[dict[str, Any]]:
    from coffeebench.event_loop import MINUTES_PER_DAY

    receipts = snapshot.setdefault("company_action_receipts", {})
    environment._company_action_receipts = receipts
    environment._company_action_outcomes = []
    environment._company_pending_actions = getattr(environment, "_company_pending_actions", {})
    environment._company_business_ids = list(snapshot["company_business_ids"])
    horizon = int(environment.max_days) * MINUTES_PER_DAY
    immediate: list[dict[str, Any]] = []
    batch_requests: dict[str, str] = {}
    start = _as_datetime(snapshot["started_at"])
    for raw in sorted(values, key=lambda item: (str(item.get("company_time", "")), str(item.get("action_id", "")))):
        action = dict(raw)
        action_id = str(action.get("action_id", ""))
        if not action_id:
            raise ValueError("CoffeeBench company action requires a stable action_id")
        if action.get("benchmark") != "coffeebench":
            raise ValueError("CoffeeBench worker received an action for a different benchmark")
        if action.get("native_action") not in {
            "post_listing", "make_offer", "withdraw_offer", "accept_offer",
            "set_retail_price", "return_shipment", "produce_item", "roast", "pay_invoice",
            "send_message",
        }:
            raise ValueError("CoffeeBench company action names no supported native mutation")
        actor_id = action.get("actor_id")
        if actor_id not in snapshot["company_business_ids"]:
            raise ValueError("CoffeeBench company action actor is not explicitly mapped")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("CoffeeBench native action arguments must be an object")
        requested_at = _as_datetime(action["company_time"])
        delta_seconds = (requested_at - start).total_seconds()
        if delta_seconds < 0:
            raise ValueError("CoffeeBench action time precedes its initialized horizon")
        scheduled_minute = int(delta_seconds // 60)
        canonical = json.dumps(action, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if action_id in batch_requests:
            if batch_requests[action_id] != request_hash:
                raise ValueError("CoffeeBench action ID was repeated with different evidence in one batch")
            continue
        batch_requests[action_id] = request_hash
        prior = receipts.get(action_id)
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise ValueError("CoffeeBench action ID was reused with different evidence")
            immediate.append(prior["outcome"])
            continue
        if requested_at < _as_datetime(snapshot["cursor"]):
            outcome = {
                "action_id": action_id,
                "evidence_id": action.get("evidence_id"),
                "mapping_id": action.get("mapping_id"),
                "actor_id": actor_id,
                "native_action": action["native_action"],
                "company_time": requested_at.isoformat(),
                "native_applied_at_minute": None,
                "status": "inconclusive",
                "reason": "evidence predates the CoffeeBench native clock cursor; native state was not rewound",
                "upstream_commit": UPSTREAM_COMMIT,
            }
            receipts[action_id] = {"request_hash": request_hash, "outcome": outcome}
            immediate.append(outcome)
            continue
        if scheduled_minute > int((until - start).total_seconds() // 60):
            raise ValueError("CoffeeBench action is later than the requested provider clock")
        if scheduled_minute >= horizon or actor_id in getattr(environment, "_bankrupt_agents", set()):
            outcome = {
                "action_id": action_id,
                "evidence_id": action.get("evidence_id"),
                "mapping_id": action.get("mapping_id"),
                "actor_id": actor_id,
                "native_action": action["native_action"],
                "company_time": requested_at.isoformat(),
                "native_applied_at_minute": None,
                "status": "inconclusive",
                "reason": (
                    "CoffeeBench native horizon has finished" if scheduled_minute >= horizon
                    else "mapped CoffeeBench business is bankrupt in the native environment"
                ),
                "upstream_commit": UPSTREAM_COMMIT,
            }
            receipts[action_id] = {"request_hash": request_hash, "outcome": outcome}
            immediate.append(outcome)
            continue
        pending = {
            **action,
            "request_hash": request_hash,
            "actor_id": actor_id,
            "arguments": arguments,
            "company_time": requested_at.isoformat(),
            "scheduled_minute": scheduled_minute,
        }
        already_pending = environment._company_pending_actions.get(action_id)
        if already_pending is not None:
            if already_pending.get("request_hash") != request_hash:
                raise ValueError("CoffeeBench action ID is pending with different evidence")
            continue
        environment._company_pending_actions[action_id] = pending
        environment.event_loop.schedule_at(
            scheduled_minute, _COMPANY_ACTION, payload={"action_id": action_id},
        )
    return immediate


def queue_company_action(
    snapshot: dict[str, Any], action: dict[str, Any], binding: dict[str, Any],
) -> dict[str, Any]:
    """Persist a native action in Coffee's event queue without advancing its clock."""
    updated = deepcopy(snapshot)
    cursor = _as_datetime(updated["cursor"])
    if updated.get("status") == "finished":
        outcomes = _reject_finished_actions(updated, [action])
        return {"snapshot": updated, "events": [], "outcomes": outcomes, "erp_intents": []}
    environment = _restore(
        updated["checkpoint"], _checkpoint_directory(binding), updated["company_business_ids"],
    )
    immediate = _queue_company_actions(
        environment, updated, [action], until=max(cursor, _as_datetime(action["company_time"])),
    )
    action_id = str(action["action_id"])
    pending = getattr(environment, "_company_pending_actions", {}).get(action_id)
    outcomes = list(immediate)
    if pending is not None:
        outcomes.append({
            "action_id": action_id,
            "evidence_id": pending["evidence_id"],
            "mapping_id": pending["mapping_id"],
            "actor_id": pending["actor_id"],
            "native_action": pending["native_action"],
            "company_time": pending["company_time"],
            "native_applied_at_minute": None,
            "status": "queued",
            "reason": "native CoffeeBench action is queued for the next shared company clock advance",
            "upstream_commit": UPSTREAM_COMMIT,
        })
    updated["checkpoint"] = _checkpoint(environment, _checkpoint_directory(binding))
    # Deliberately leave cursor, provider time, and all economy state untouched.
    return {"snapshot": updated, "events": [], "outcomes": outcomes, "erp_intents": []}


def _reject_finished_actions(snapshot: dict[str, Any], values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts = snapshot.setdefault("company_action_receipts", {})
    outcomes = []
    for action in values:
        action_id = str(action.get("action_id", ""))
        if not action_id:
            raise ValueError("CoffeeBench company action requires a stable action_id")
        if action.get("benchmark") != "coffeebench" or action.get("actor_id") not in snapshot["company_business_ids"]:
            raise ValueError("CoffeeBench finished action must target an explicitly mapped company actor")
        if action.get("native_action") not in {
            "post_listing", "make_offer", "withdraw_offer", "accept_offer",
            "set_retail_price", "return_shipment", "produce_item", "roast", "pay_invoice",
            "send_message",
        }:
            raise ValueError("CoffeeBench finished action names no supported native mutation")
        canonical = json.dumps(action, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        prior = receipts.get(action_id)
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise ValueError("CoffeeBench action ID was reused with different evidence")
            outcomes.append(prior["outcome"])
            continue
        outcome = {
            "action_id": action_id,
            "evidence_id": action.get("evidence_id"),
            "mapping_id": action.get("mapping_id"),
            "actor_id": action.get("actor_id"),
            "native_action": action.get("native_action"),
            "company_time": action.get("company_time"),
            "native_applied_at_minute": None,
            "status": "inconclusive",
            "reason": "CoffeeBench native horizon has finished",
            "upstream_commit": UPSTREAM_COMMIT,
        }
        receipts[action_id] = {"request_hash": request_hash, "outcome": outcome}
        outcomes.append(outcome)
    return outcomes


def _finalize_pending_company_actions(
    environment: Any, *, target_minute: int | None = None,
) -> list[dict[str, Any]]:
    outcomes = []
    for action_id, pending in list(getattr(environment, "_company_pending_actions", {}).items()):
        if target_minute is not None and int(pending.get("scheduled_minute", 0)) > target_minute:
            continue
        outcome = {
            "action_id": action_id,
            "evidence_id": pending["evidence_id"],
            "mapping_id": pending["mapping_id"],
            "actor_id": pending["actor_id"],
            "native_action": pending["native_action"],
            "company_time": pending["company_time"],
            "native_applied_at_minute": None,
            "status": "inconclusive",
            "reason": "CoffeeBench ended before the mapped native action could be applied",
            "upstream_commit": UPSTREAM_COMMIT,
        }
        environment._company_action_receipts[action_id] = {
            "request_hash": pending["request_hash"], "outcome": outcome,
        }
        outcomes.append(outcome)
        environment._company_pending_actions.pop(action_id, None)
    return outcomes


def _queue_external_supply_conditions(
    environment: Any, snapshot: dict[str, Any], values: list[dict[str, Any]],
    *, until: datetime,
) -> None:
    from coffeebench.event_loop import MINUTES_PER_DAY

    _upgrade_snapshot_for_supply_conditions(snapshot, environment)
    existing = snapshot.setdefault("external_supply_conditions", [])
    by_id = {condition["event_id"]: condition for condition in existing}
    environment._company_external_supply_conditions = by_id
    environment._company_native_delivery_lag_baseline = dict(snapshot["native_delivery_lag_baseline"])
    environment._company_business_ids = list(snapshot["company_business_ids"])
    environment._company_pending_supply_wake_reasons = getattr(
        environment, "_company_pending_supply_wake_reasons", {},
    )
    validated = [_validate_supply_condition(value, snapshot["started_at"], until) for value in values]
    for condition in sorted(validated, key=lambda item: (_as_datetime(item["occurred_at"]), item["event_id"])):
        event_id = condition["event_id"]
        prior = by_id.get(event_id)
        if prior:
            comparable_prior = {key: prior[key] for key in condition}
            if comparable_prior != condition:
                raise ValueError("E-Commerce source event ID was reused for different Coffee condition facts")
            continue
        start_minute = max(condition["start_minute"], int(environment._company_clock_cursor))
        end_minute = condition["end_minute"]
        if start_minute >= int(environment.max_days) * MINUTES_PER_DAY:
            condition["status"] = "outside_horizon"
        elif end_minute <= start_minute:
            condition["status"] = "expired"
        else:
            condition["status"] = "pending"
        by_id[event_id] = condition
        existing.append(condition)
        if condition["status"] != "pending":
            continue
        environment.event_loop.schedule_at(
            start_minute, _COMPANY_SUPPLY_CONDITION,
            payload={"event_id": event_id, "status": "active"},
        )
        if end_minute < int(environment.max_days) * MINUTES_PER_DAY:
            environment.event_loop.schedule_at(
                end_minute, _COMPANY_SUPPLY_CONDITION,
                payload={"event_id": event_id, "status": "expired"},
            )


def _upgrade_snapshot_for_supply_conditions(snapshot: dict[str, Any], environment: Any) -> None:
    version = snapshot.get("snapshot_schema_version", 1)
    if version not in (1, 2, 3):
        raise ValueError("unsupported CoffeeBench private snapshot schema")
    if "native_delivery_lag_baseline" not in snapshot:
        if snapshot.get("external_supply_conditions") or getattr(
            environment, "_company_external_supply_conditions", None,
        ):
            raise RuntimeError("CoffeeBench source-condition snapshot has no recoverable native baseline")
        snapshot["native_delivery_lag_baseline"] = {
            item_id: int(item.delivery_lag_days)
            for item_id, item in environment.marketplace.items.items()
        }
    if "external_supply_conditions" not in snapshot:
        snapshot["external_supply_conditions"] = []
    if not isinstance(snapshot["external_supply_conditions"], list):
        raise ValueError("CoffeeBench private source-condition state must be a list")
    if "native_message_ids" not in snapshot:
        # Older trusted checkpoints predate email export. Treat messages
        # already in that checkpoint as historical so migration never sends
        # old marketplace mail into a live employee inbox.
        snapshot["native_message_ids"] = sorted(
            str(message.id) for message in environment.marketplace.messages
        )
    if not isinstance(snapshot["native_message_ids"], list) or any(
        not isinstance(message_id, str) or not message_id
        for message_id in snapshot["native_message_ids"]
    ):
        raise ValueError("CoffeeBench native-message source IDs are invalid")
    snapshot["snapshot_schema_version"] = 3


def _native_message_events(environment: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Export actual upstream counterparty-to-company messages as source events."""
    mapped = set(snapshot["company_business_ids"])
    known = set(snapshot.get("native_message_ids", []))
    start = _as_datetime(snapshot["started_at"])
    events = []
    current_ids = []
    for message in environment.marketplace.messages:
        message_id = str(message.id)
        current_ids.append(message_id)
        if message_id in known:
            continue
        if message.recipient_id not in mapped or message.sender_id in mapped:
            continue
        if isinstance(message.sent_at, bool) or not isinstance(message.sent_at, int) or message.sent_at < 0:
            raise ValueError("CoffeeBench native message has an invalid source minute")
        occurred_at = start + timedelta(minutes=message.sent_at)
        event_id = hashlib.sha256(
            f"{UPSTREAM_COMMIT}:marketplace_message:{message_id}".encode("utf-8")
        ).hexdigest()
        payload = {
            "native_message_id": message_id,
            "sender_id": str(message.sender_id),
            "recipient_id": str(message.recipient_id),
            "title": str(message.title),
            "body": str(message.body),
            "sent_at_virtual_minute": message.sent_at,
            "ref_listing_id": message.ref_listing_id,
            "ref_offer_id": message.ref_offer_id,
            "ref_deal_id": message.ref_deal_id,
            "native_source": "coffeebench/marketplace.py:Message",
        }
        events.append({
            "schema_version": 1,
            "company_id": "company",
            "event_id": event_id,
            "event_type": "COFFEE_NATIVE_MESSAGE_RECEIVED",
            "simulated_time": occurred_at.isoformat(),
            "source": PROVIDER_ID,
            "upstream_commit": UPSTREAM_COMMIT,
            "entity_type": "marketplace_message",
            "entity_id": message_id,
            "payload": payload,
            "visibility": [str(message.recipient_id)],
        })
    snapshot["native_message_ids"] = sorted(set(current_ids))
    return sorted(events, key=lambda event: (event["simulated_time"], event["event_id"]))


def _source_records(environment: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    mapped = set(snapshot["company_business_ids"])
    start = _as_datetime(snapshot["started_at"])
    events: list[dict[str, Any]] = []
    seen = set(snapshot.get("source_record_ids", []))

    def active_source_conditions(minute: int) -> list[dict[str, Any]]:
        return [{key: condition[key] for key in (
            "event_id", "source", "upstream_commit", "event_name", "occurred_at",
            "effective_until", "duration_days", "store_type", "affected_categories",
            "delivery_delay_multiplier",
        )} for condition in snapshot.get("external_supply_conditions", [])
            if int(condition["start_minute"]) <= int(minute) < int(condition["end_minute"])]

    def add(
        kind: str,
        record_type: str,
        record_id: str,
        party: str,
        amount: float,
        minute: int,
        source_ref: str,
        native_record: dict[str, Any],
        *,
        source_conditions: list[dict[str, Any]] | None = None,
    ):
        if not math.isfinite(float(amount)) or amount < 0:
            raise ValueError("CoffeeBench native transaction amount is invalid")
        identity = f"{UPSTREAM_COMMIT}:{record_type}:{record_id}:{kind}"
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if event_id in seen:
            return
        at = start + timedelta(minutes=int(minute))
        event = {
            "id": event_id,
            "source": PROVIDER_ID,
            "upstream_commit": UPSTREAM_COMMIT,
            "kind": kind,
            "party": party,
            "currency": "USD",
            "amount": format(Decimal(str(amount)).quantize(Decimal("0.01")), "f"),
            "source_ref": source_ref,
            "occurred_at": at.isoformat(),
            "native_record_type": record_type,
            "native_record_id": str(record_id),
            "native_record": native_record,
        }
        if source_conditions is not None:
            event["active_source_condition_ids"] = [condition["event_id"] for condition in source_conditions]
            event["active_source_conditions"] = source_conditions
        events.append(event)
        seen.add(event_id)

    import math
    from decimal import Decimal
    for deal in environment.marketplace.deals:
        participants = {deal.seller_id, deal.buyer_id}
        company_participants = participants.intersection(mapped)
        if not company_participants or participants.issubset(mapped):
            continue
        company_side = sorted(company_participants)[0]
        party = deal.buyer_id if company_side == deal.seller_id else deal.seller_id
        add(
            "order", "deal", deal.id, party, deal.total_price, deal.deal_at, deal.id,
            {
                "deal_id": deal.id,
                "listing_id": deal.listing_id,
                "offer_id": deal.offer_id,
                "seller_id": deal.seller_id,
                "buyer_id": deal.buyer_id,
                "item_id": deal.item_id,
                "quantity": deal.qty,
                "unit_price": format(Decimal(str(deal.unit_price)).quantize(Decimal("0.01")), "f"),
                "total_price": format(Decimal(str(deal.total_price)).quantize(Decimal("0.01")), "f"),
                "payment_terms_days": deal.payment_terms_days,
                "accepted_at_virtual_minute": deal.deal_at,
                "delivery_at_virtual_minute": deal.delivery_at,
                "invoice_id": deal.invoice_id,
                "upstream_source": "coffeebench/marketplace.py",
            },
            source_conditions=active_source_conditions(deal.deal_at),
        )

    # A pending buyer offer is a source-backed business proposal, not a deal.
    # Import it only when the mapped company is the seller that may accept it;
    # keep the upstream price, quantity, terms, parties, listing and posted time
    # intact so the ERP draft can represent the exact offer without scheduling
    # delivery or treating it as accepted.
    listings = {
        listing.id: listing
        for listing in getattr(environment.marketplace, "listings", [])
    }
    for offer in getattr(environment.marketplace, "offers", []):
        if offer.status != "pending" or offer.seller_id not in mapped:
            continue
        listing = listings.get(offer.listing_id)
        if listing is None:
            raise ValueError("CoffeeBench pending offer references a missing native listing")
        total = Decimal(str(offer.offered_price)) * Decimal(str(offer.qty))
        add(
            "offer", "offer", offer.id, offer.buyer_id, total, offer.posted_at, offer.id,
            {
                "offer_id": offer.id,
                "listing_id": listing.id,
                "buyer_id": offer.buyer_id,
                "seller_id": offer.seller_id,
                "item_id": listing.item_id,
                "quantity": offer.qty,
                "unit_price": format(Decimal(str(offer.offered_price)), "f"),
                "total_price": format(total.quantize(Decimal("0.01")), "f"),
                "payment_terms_days": offer.payment_terms_days,
                "offer_message": offer.message,
                "offer_status": offer.status,
                "offer_posted_at_virtual_minute": offer.posted_at,
                "listing_quantity": listing.qty,
                "listing_asking_price": format(Decimal(str(listing.asking_price)), "f"),
                "listing_payment_terms_days": listing.payment_terms_days,
                "listing_posted_at_virtual_minute": listing.posted_at,
                "upstream_source": "coffeebench/marketplace.py:Offer",
            },
        )

    invoices: dict[str, Any] = {}
    for agent_id, business in environment.business_apps.items():
        if agent_id not in mapped:
            continue
        for invoice in (*business.accounts_receivable, *business.accounts_payable):
            invoices[invoice.id] = invoice
    for invoice in invoices.values():
        company_sides = {invoice.issuer, invoice.payer}.intersection(mapped)
        if len(company_sides) != 1:
            continue
        company_side = next(iter(company_sides))
        party = invoice.payer if company_side == invoice.issuer else invoice.issuer
        invoice_fields = {
            "invoice_id": invoice.id,
            "issuer_id": invoice.issuer,
            "payer_id": invoice.payer,
            "amount": format(Decimal(str(invoice.amount)).quantize(Decimal("0.01")), "f"),
            "issue_day": invoice.issue_date,
            "due_day": invoice.due_date,
            "reference": invoice.reference,
            "credited_amount": format(Decimal(str(invoice.credited_amount)).quantize(Decimal("0.01")), "f"),
            "late_fees_accrued": format(Decimal(str(invoice.late_fees_accrued)).quantize(Decimal("0.01")), "f"),
            "paid": invoice.paid,
            "paid_day": invoice.paid_date,
            "returned": invoice.returned,
            "bad_debt": invoice.bad_debt,
            "upstream_source": "coffeebench/typings.py:Invoice",
        }
        add("invoice", "invoice", invoice.id, party, invoice.amount, invoice.issue_date * 1440, invoice.id, invoice_fields)
        if invoice.paid and invoice.paid_date is not None:
            paid_amount = invoice.amount + invoice.late_fees_accrued - invoice.credited_amount
            add(
                "payment", "invoice_payment", invoice.id, party, paid_amount,
                invoice.paid_date * 1440, invoice.id,
                {
                    **invoice_fields,
                    "paid_amount": format(Decimal(str(paid_amount)).quantize(Decimal("0.01")), "f"),
                    "cash_from_party_id": invoice.payer,
                    "cash_to_party_id": invoice.issuer,
                    "upstream_source": "coffeebench/business_app.py:pay_invoice",
                },
            )
    snapshot["source_record_ids"] = sorted(seen)
    return sorted(events, key=lambda event: (event["occurred_at"], event["id"]))


def _native_evaluation(environment: Any, business_ids: list[str]) -> list[dict[str, Any]]:
    """Read the pinned CoffeeBench audit for company-adapted native books."""
    rows = []
    for actor_id in sorted(set(business_ids)):
        business = environment.business_apps[actor_id]
        if not getattr(environment, "_company_clock_finalized", False):
            rows.append({
                "evaluation_type": "company_adapted_native_projection",
                "provider_id": PROVIDER_ID,
                "upstream_commit": UPSTREAM_COMMIT,
                "actor_id": actor_id,
                "role": business.role,
                "status": "incomplete",
                "score_metric": "audit.annual.true_net_income",
                "score": None,
                "reason": "CoffeeBench defines its canonical score at the completed native horizon",
            })
            continue
        audit = environment._compute_audit_for(actor_id)
        annual = audit["annual"]
        rows.append({
            "evaluation_type": "company_adapted_native_projection",
            "provider_id": PROVIDER_ID,
            "upstream_commit": UPSTREAM_COMMIT,
            "actor_id": actor_id,
            "role": business.role,
            "status": "final",
            "completed": actor_id not in environment._bankrupt_agents,
            "bankrupt_day": environment._bankrupt_day.get(actor_id),
            "bankrupt_reason": environment._bankrupt_reason.get(actor_id),
            "score_metric": "audit.annual.true_net_income",
            "score": annual["true_net_income"],
            "audit": audit,
        })
    return rows


def events_between(
    snapshot: dict[str, Any],
    start: datetime | date | str,
    end: datetime | date | str,
    binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """CoffeeBench has no exogenous date catalogue; transactions are endogenous.

    Native actor and economy state can only advance through :func:`advance`.
    Keeping this query pure prevents a calendar read from running actors.
    """
    del snapshot, binding
    begin, finish = _as_datetime(start), _as_datetime(end)
    if finish < begin:
        raise ValueError("CoffeeBench event range cannot run backwards")
    return []


async def advance(
    snapshot: dict[str, Any],
    until: datetime | date | str,
    binding: dict[str, Any],
    erp_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance upstream CoffeeBench actors/economy to a company clock boundary.

    Persist the returned snapshot on every successful call, including ticks
    with no matching transaction. The actor model factory is worker-local;
    native model calls are awaited here and never run during initialization.
    """
    del erp_snapshot  # external CoffeeBench books cannot read company ERP data.
    finish = _as_datetime(until)
    cursor = _as_datetime(snapshot["cursor"])
    if finish < cursor:
        raise ValueError("CoffeeBench provider clock cannot run backwards")
    updated = deepcopy(snapshot)
    if updated.get("status") == "finished":
        updated["cursor"] = finish.isoformat()
        values = binding.get("company_actions", [])
        if not isinstance(values, list):
            raise ValueError("CoffeeBench company_actions must be a list")
        return {
            "snapshot": updated, "events": [],
            "outcomes": _reject_finished_actions(updated, values),
            "erp_intents": [], "next_due": None,
        }
    environment = _restore(
        updated["checkpoint"], _checkpoint_directory(binding),
        updated["company_business_ids"],
    )
    virtual_delta = max(0, int((finish - _as_datetime(updated["started_at"])).total_seconds() // 60))
    horizon = int(environment.max_days) * 1440
    horizon_finished = virtual_delta >= horizon
    target_minute = min(virtual_delta, horizon - 1)
    condition_status_before = {
        condition["event_id"]: condition.get("status")
        for condition in updated.get("external_supply_conditions", [])
    }
    conditions = binding.get("external_supply_conditions", [])
    if not isinstance(conditions, list):
        raise ValueError("CoffeeBench external_supply_conditions must be a list")
    _queue_external_supply_conditions(environment, updated, conditions, until=finish)
    company_actions = binding.get("company_actions", [])
    if not isinstance(company_actions, list):
        raise ValueError("CoffeeBench company_actions must be a list")
    existing_action_outcomes = _queue_company_actions(
        environment, updated, company_actions, until=finish,
    )
    await _advance_environment(environment, target_minute)
    pending_action_outcomes = _finalize_pending_company_actions(
        environment, target_minute=target_minute,
    )
    communication_events = _native_message_events(environment, updated)
    if horizon_finished and not environment._company_clock_finalized:
        from coffeebench.environment import BUSINESS_HOURS_END
        from coffeebench.event_loop import MINUTES_PER_DAY

        last_day = max(0, environment.max_days - 1)
        environment.time_manager.virtual_min = last_day * MINUTES_PER_DAY + BUSINESS_HOURS_END
        final_deliveries = environment._process_deliveries(environment.max_days)
        if final_deliveries:
            environment._emit("deliveries_processed", day=last_day, count=len(final_deliveries))
        environment._company_clock_finalized = True
    if horizon_finished:
        for condition in updated.get("external_supply_conditions", []):
            if condition.get("status") in {"active", "pending"}:
                condition["status"] = "world_finished"
        _recompute_native_delivery_lags(environment)
    source_records = _source_records(environment, updated)
    native_lags = {
        item_id: int(item.delivery_lag_days)
        for item_id, item in environment.marketplace.items.items()
    }
    condition_outcomes = [{
        "source_event_id": condition["event_id"],
        "source": condition["source"],
        "upstream_commit": condition["upstream_commit"],
        "event_name": condition["event_name"],
        "status": condition["status"],
        "native_delivery_lag_days": native_lags,
    } for condition in updated.get("external_supply_conditions", [])
        if condition_status_before.get(condition["event_id"]) != condition["status"]]
    updated["checkpoint"] = _checkpoint(environment, _checkpoint_directory(binding))
    updated["cursor"] = finish.isoformat()
    requested_bindings = sorted(set(str(x) for x in binding.get("company_business_ids", [])))
    if requested_bindings != updated["company_business_ids"]:
        raise ValueError("CoffeeBench company-business mapping cannot change after initialization")
    updated["binding"] = {
        "company_business_ids": requested_bindings,
        "model_id": str(binding.get("model_id", updated["binding"]["model_id"])),
    }
    next_virtual_minute = environment.event_loop.peek_time()
    next_due = None
    if not horizon_finished and next_virtual_minute is not None and next_virtual_minute < horizon:
        next_due = (_as_datetime(updated["started_at"]) + timedelta(minutes=int(next_virtual_minute))).isoformat()
    if horizon_finished or next_virtual_minute is None:
        updated["status"] = "finished"
    action_outcomes = (
        existing_action_outcomes + list(environment._company_action_outcomes)
        + pending_action_outcomes
    )
    evaluation_outcomes = (
        _native_evaluation(environment, updated["company_business_ids"])
        if updated.get("status") == "finished" else []
    )
    outcomes = [{
        "event_id": record["id"],
        "source": PROVIDER_ID,
        "source_ref": record["source_ref"],
        "native_record_type": record["native_record_type"],
        "native_record_id": record["native_record_id"],
        "upstream_commit": UPSTREAM_COMMIT,
    } for record in source_records] + condition_outcomes + action_outcomes + evaluation_outcomes
    intents = [{key: record[key] for key in (
        "id", "source", "kind", "party", "currency", "amount", "source_ref", "occurred_at",
        "native_record_type", "native_record_id", "upstream_commit",
        "native_record",
    )} for record in source_records]
    return {
        "snapshot": updated,
        "events": communication_events,
        "outcomes": outcomes,
        "erp_intents": intents,
        "next_due": next_due,
    }


def apply_event(
    snapshot: dict[str, Any],
    event: dict[str, Any],
    erp_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Record delivery of one immutable native source record to the coordinator."""
    del erp_snapshot  # Coffee books are external; ERP documents belong to the coordinator.
    if event.get("source") != PROVIDER_ID or event.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("CoffeeBench event provenance does not match the pinned provider")
    event_id = str(event.get("id", ""))
    if not event_id:
        raise ValueError("CoffeeBench native source record requires a stable ID")
    updated = deepcopy(snapshot)
    prior = next((row["outcome"] for row in updated.get("event_outcomes", []) if row["event_id"] == event_id), None)
    if prior is not None:
        return updated, [prior], []
    outcome = {
        "event_id": event_id,
        "native_record_type": event["native_record_type"],
        "native_record_id": event["native_record_id"],
        "upstream_commit": UPSTREAM_COMMIT,
        "accepted": True,
    }
    updated.setdefault("event_outcomes", []).append({"event_id": event_id, "outcome": outcome})
    updated.setdefault("applied_event_ids", []).append(event_id)
    # These are source claims/intents only. Parent verifies the mapping against
    # company ERP records and owns all canonical event and document writes.
    intent = {key: event[key] for key in (
        "id", "source", "kind", "party", "currency", "amount", "source_ref", "occurred_at",
        "native_record_type", "native_record_id", "upstream_commit",
        "native_record",
    )}
    return updated, [outcome], [intent]


__all__ = [
    "UPSTREAM_COMMIT", "PROVIDER_ID", "PROVIDER_VERSION", "configure_model_factory",
    "initialize", "events_between", "advance", "apply_event",
]
