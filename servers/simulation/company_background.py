"""Company-scoped worker lifecycle for CoffeeBench and E-CommerceBench."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
import traceback
from typing import Any

from .background_runtime import BackgroundWorkerClient, CompanyBackgroundRuntime
from .businesses import initialize_supplier_business
from .integration_contracts import CompanySourceEvent
from .models import CanonicalEvent

LOG = logging.getLogger(__name__)


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("company background times must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _document_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    raise ValueError("company background ERP supplier binding is invalid")


class CompanyBackgroundService:
    """Advance provider work independently of the company clock thread.

    Provider calls run on this service's thread. The company clock remains
    free to advance while model calls are in flight; due work is selected from
    each confirmed provider result and retries preserve worker operation IDs.
    """

    _PUBLIC_EVENT_PAYLOAD_KEYS = {
        "upstream_event_name", "upstream_calendar_start_date",
        "upstream_calendar_duration_days", "upstream_source", "upstream_commit",
        "market_date", "store_type",
    }

    def __init__(self, engine, *, runtime: CompanyBackgroundRuntime, finance_adapter,
                 profile: dict[str, Any], company_binding: dict[str, Any]):
        self.engine = engine
        self.runtime = runtime
        self.engine.company_background_recover = runtime.recover_pending_coffeebench
        self.engine.company_background_control = self.control
        self.finance_adapter = finance_adapter
        self.profile = profile
        self.company_binding = company_binding
        self.changed = threading.Event()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.errors: dict[str, dict[str, Any]] = {}
        self._next_due: dict[str, datetime] = {}
        self._retry_at: datetime | None = None
        self._initialized = False
        self._initialization_attempted = False
        self._initialization_errors: dict[str, dict[str, Any]] = {}
        self._initialized_providers: set[str] = set()
        self._provider_started_at: datetime | None = None
        self._provider_bindings: dict[str, dict[str, Any]] | None = None
        self._actor_private_states: dict[str, dict[str, Any]] | None = None
        self._finance_retry_at: datetime | None = None
        self._due_obligations: list[dict[str, Any]] = []
        self._obligation_error: str | None = None
        self._lock = threading.RLock()
        self._in_flight: set[str] = set()

    @classmethod
    def from_runtime(cls, engine, *, store, worker: BackgroundWorkerClient,
                     finance_adapter, profile_path: Path, background_model_id: str,
                     company_binding: dict[str, Any]) -> "CompanyBackgroundService":
        profile = json.loads(profile_path.read_text())
        background = profile["company"]["background"]
        if not isinstance(background.get("seed"), int) or background["seed"] < 0:
            raise ValueError("company background seed must be a nonnegative integer")
        coffee = background["coffee"]
        ecommerce = background["ecommerce"]
        if not background_model_id:
            raise ValueError("company background model ID is required")
        for value in (coffee.get("company_business_ids"), ecommerce.get("supplier_actor_id"),
                      ecommerce.get("store_type")):
            if not value:
                raise ValueError("company background provider bindings are incomplete")
        runtime = CompanyBackgroundRuntime(store, worker)
        background = {**background, "model_id": background_model_id}
        return cls(engine, runtime=runtime, finance_adapter=finance_adapter,
                   profile=background, company_binding=company_binding)

    def _bindings(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        supplier_id = _document_name(self.company_binding.get("documents", {}).get("supplier"))
        coffee = {
            "company_business_ids": deepcopy(self.profile["coffee"]["company_business_ids"]),
            "max_days": deepcopy(self.profile["coffee"].get("max_days", 90)),
            "model_id": self.profile["model_id"],
        }
        # The calendar owns only its existing actor/category inputs. Financial
        # projection policies must not change a persisted calendar init request.
        ecommerce = {key: deepcopy(self.profile["ecommerce"][key])
                     for key in ("supplier_actor_id", "store_type")}
        ecommerce["company_id"] = "company"
        ecommerce["supplier_id"] = supplier_id
        actors = getattr(self.engine, "_actor_config", {})
        actor_id = str(ecommerce["supplier_actor_id"])
        configured = actors.get(actor_id)
        if not isinstance(configured, dict) or configured.get("kind") != "supplier":
            raise ValueError("company background supplier actor must match a configured supplier")
        private_state = deepcopy(configured.get("private_state", {}))
        private_state.setdefault("market_config", {})["store_type"] = self.profile["ecommerce"]["store_type"]
        private_state = initialize_supplier_business(
            private_state, seed=int(self.profile["seed"]), supplier_id=supplier_id,
        )
        return {"coffeebench": coffee, "ecommercebench": ecommerce}, {actor_id: private_state}

    def _initialize(self) -> None:
        company = self.engine.company_state()
        if company is None:
            return
        bindings, actor_states = self._bindings()
        self._provider_bindings = bindings
        self._actor_private_states = actor_states
        result = self.runtime.initialize_company(
            seed=int(self.profile["seed"]), started_at=_aware(company["simulated_time"]),
            bindings=self._provider_bindings, actor_private_states=self._actor_private_states,
        )
        with self._lock:
            self._initialization_attempted = True
            self._initialization_errors = deepcopy(result["errors"])
            self._initialized_providers = set(result["providers"])
            self.errors = {key: {"code": value["code"], "status_code": value["status_code"]}
                           for key, value in result["errors"].items()}
            for provider_id, value in result["providers"].items():
                if value.get("next_due"):
                    self._next_due[provider_id] = _aware(value["next_due"])
            self._initialized = not result["errors"]
            coffee = result["providers"].get("coffeebench")
            if coffee is not None:
                self._provider_started_at = _aware(coffee["cursor"])
            self._retry_at = (_aware(company["simulated_time"]) + timedelta(seconds=30)
                              if result["errors"] else None)
        self.changed.set()

    def _finance_binding(self) -> dict[str, Any]:
        if self._provider_started_at is None:
            raise RuntimeError("CoffeeBench company start time is unavailable")
        binding = deepcopy(self.company_binding)
        documents = binding.get("documents", {})
        binding.update({
            "company_id": "company",
            "erp_company": _document_name(documents.get("company")),
            "company_business_ids": list(self.profile["coffee"]["company_business_ids"]),
            "started_at": self._provider_started_at.isoformat(),
            "counterparty_mail_directory": deepcopy(
                self.profile["coffee"]["counterparties"]
            ),
        })
        return binding

    def _fanout(self, raw_events: list[dict[str, Any]]) -> None:
        for raw in raw_events:
            event = CompanySourceEvent.model_validate(raw).model_dump(mode="json")
            targets: list[tuple[str, int]] = []
            for experiment in self.engine.store.company_experiments(event["company_id"]):
                if experiment["status"] != "running":
                    continue
                run_id = experiment["experiment_id"]
                run = self.engine.store.get_run(run_id)
                started_at = run.get("manifest", {}).get("initial_time", run["simulated_time"])
                if _aware(started_at) > _aware(event["simulated_time"]):
                    continue
                suppliers = [actor for actor in self.engine.store.list_actors(run_id, run["generation"])
                             if actor["kind"] == "supplier"
                             and str(actor["private_state"].get("supplier_id", "")) == event["entity_id"]]
                if suppliers:
                    targets.append((run_id, run["generation"]))
            self.engine.store.record_company_source_events(
                [event], event["company_id"], {event["event_id"]: targets},
            )
            # Targets are captured atomically with first persistence, so a
            # restart cannot fan a historical source event into a later run.
            for target in self.engine.store.company_source_event_targets(event["event_id"], event["company_id"]):
                run_id, generation = target["run_id"], target["generation"]
                run = self.engine.store.get_run(run_id)
                if run["generation"] != generation or run["status"] != "running":
                    continue
                suppliers = [actor for actor in self.engine.store.list_actors(run_id, generation)
                             if actor["kind"] == "supplier"
                             and str(actor["private_state"].get("supplier_id", "")) == event["entity_id"]]
                for supplier in suppliers:
                    canonical_event_id = hashlib.sha256(f"{event['event_id']}:{run_id}".encode()).hexdigest()[:32]
                    canonical = CanonicalEvent(
                        event_id=canonical_event_id, run_id=run_id, scenario_id=run["scenario_id"],
                        simulated_time=_aware(event["simulated_time"]), event_type=event["event_type"],
                        source=event["source"], entity_type=event["entity_type"], entity_id=event["entity_id"],
                        related_entities=[f"{run['scenario_id']}-case"],
                        payload={**event["payload"], "company_source_event_id": event["event_id"],
                                 "upstream_commit": event["upstream_commit"]},
                        visibility=[supplier["actor_id"]],
                    )
                    self.engine.store.enqueue_event(canonical, generation)
        native_message_bridge = getattr(self.engine, "company_native_message_bridge", None)
        if native_message_bridge is not None:
            native_message_bridge(raw_events)

    def _reconcile(self, providers: dict[str, dict[str, Any]], as_of: datetime) -> None:
        intents = [intent for provider in providers.values() for intent in provider.get("erp_intents", [])]
        self.engine.store.record_company_finance_source_intents(intents)
        pending = self.engine.store.list_unreceipted_company_finance_source_intents()
        if not pending:
            issue_reconcile = getattr(self.engine, "company_issue_reconcile", None)
            if issue_reconcile is not None:
                issue_reconcile()
            return
        result = self.finance_adapter.reconcile(pending, self._finance_binding(), as_of)
        receipts = result.get("receipts")
        if not isinstance(receipts, list):
            raise ValueError("native finance reconciler did not return source receipts")
        expected = {str(record["id"]) for record in pending}
        actual = {str(receipt.get("source_id")) for receipt in receipts}
        if actual != expected:
            raise RuntimeError("native finance reconciliation returned an incomplete source receipt set")
        self.engine.store.record_company_finance_receipts(receipts, str(result["as_of"]))
        issue_reconcile = getattr(self.engine, "company_issue_reconcile", None)
        if issue_reconcile is not None:
            issue_reconcile()

    def _read_due_obligations(self, as_of: datetime) -> None:
        obligations = self.finance_adapter.read_due_obligations(
            _document_name(self.company_binding.get("documents", {}).get("company")), as_of,
        )
        normalized = []
        for row in obligations:
            required = (row.get("doctype"), row.get("name"), row.get("due_date"), row.get("modified"))
            if any(not isinstance(value, str) or not value for value in required):
                raise ValueError("native ERP due obligation is missing its document identity or version")
            normalized.append({
                "doctype": row["doctype"], "name": row["name"], "due_date": row["due_date"],
                "modified": row["modified"], "currency": row.get("currency"),
                "grand_total": row.get("grand_total"), "outstanding_amount": row.get("outstanding_amount"),
                "party": row.get("supplier") or row.get("customer"),
            })
        self.engine.store.record_company_due_obligation_observations(normalized, as_of.isoformat())
        with self._lock:
            self._due_obligations = normalized
            self._obligation_error = None

    def _retry_finance(self, now: datetime) -> None:
        try:
            self._reconcile({}, now)
            with self._lock:
                self.errors.pop("native_finance_reconcile", None)
                self._finance_retry_at = None
        except Exception as error:
            with self._lock:
                self.errors["native_finance_reconcile"] = {"code": type(error).__name__}
                self._finance_retry_at = now + timedelta(seconds=30)
            self._log_failure("company finance reconciliation failed", error)

    @staticmethod
    def _log_failure(message: str, error: BaseException) -> None:
        frames = [(frame.filename, frame.lineno, frame.name)
                  for frame in traceback.extract_tb(error.__traceback__)]
        causes = []
        cause = error.__cause__ or error.__context__
        while cause is not None:
            causes.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        LOG.error("%s exception_type=%s causes=%s stack=%s",
                  message, type(error).__name__, causes, frames)

    def control(self, enabled: bool, key: str, *, provider_id: str = "coffeebench") -> dict[str, Any]:
        with self._lock:
            self.engine.store.set_background_provider_enabled(
                provider_id, enabled, key, request_recovery=provider_id == "coffeebench")
            if provider_id == "ecommercebench" and enabled:
                self.errors.pop(provider_id, None)
            state = self._provider_status(provider_id)
        self.wake()
        return {"provider_id": provider_id, **state}

    def _provider_status(self, provider_id: str, control: dict[str, Any] | None = None) -> dict[str, Any]:
        if control is None:
            control = self.engine.store.background_provider_control(provider_id)
        enabled = control["enabled"]
        in_flight = provider_id in self._in_flight
        status = ("stopping" if in_flight else "stopped") if not enabled else (
            "starting" if provider_id == "coffeebench" and control["recovery_requested"] else
            "error" if control["last_error"] or provider_id in self.errors else "active")
        return {"enabled": enabled, "in_flight": in_flight, "status": status}

    def _recover_requested_coffee(self) -> None:
        control = self.engine.store.background_provider_control("coffeebench")
        if not control["enabled"] or not control["recovery_requested"]:
            return
        with self._lock:
            # Stop may have arrived since the first durable read.
            if not self.engine.store.background_provider_control("coffeebench")["enabled"]:
                return
            self._in_flight.add("coffeebench")
        try:
            pending = self.engine.store.get_background_provider_pending("coffeebench")
            if pending is not None and pending["result"] is None:
                control = self.engine.store.begin_background_recovery("coffeebench", pending["operation_id"])
                result = self.runtime.recover_pending_coffeebench(
                    control["recovery_operation_id"], control["recovery_key"])
                if result["status"] not in {"completed", "completed_original", "result_pending"}:
                    code = result.get("code", "background_operation_uncertain")
                    self.engine.store.finish_background_recovery("coffeebench", code)
                    with self._lock:
                        self.errors["coffeebench"] = {"code": code}
                    return
            self.engine.store.finish_background_recovery("coffeebench")
            with self._lock:
                self.errors.pop("coffeebench", None)
        except Exception as error:
            self.engine.store.finish_background_recovery("coffeebench", type(error).__name__)
            with self._lock:
                self.errors["coffeebench"] = {"code": type(error).__name__}
            self._log_failure("CoffeeBench start failed", error)
        finally:
            with self._lock:
                self._in_flight.discard("coffeebench")

    def _advance_once(self, company: dict[str, Any], now: datetime) -> None:
        retry_due = self._retry_at is not None and self._retry_at <= now
        if not self._initialization_attempted or self._initialization_errors and retry_due:
            self._initialize()
        retry_due = self._retry_at is not None and self._retry_at <= now
        if not self._initialized_providers:
            self._retry_at = now + timedelta(seconds=30)
            return
        self._recover_requested_coffee()
        enabled = {provider: self.engine.store.background_provider_control(provider)["enabled"]
                   for provider in ("coffeebench", "ecommercebench")}
        coupling = self.profile.get("coupling", {}).get("ecommerce_to_coffee")
        due = [value for provider, value in self._next_due.items()
               if value <= now and provider not in self.errors]
        due_providers = [provider for provider, value in self._next_due.items()
                         if value <= now and provider not in self.errors
                         and "company_background" not in self.errors]
        retry_providers = [provider for provider in self.errors
                           if retry_due and provider in self._initialized_providers]
        if retry_due and "company_background" in self.errors:
            retry_providers.extend(provider for provider, value in self._next_due.items()
                                   if value <= now and provider in self._initialized_providers)
        selected = tuple(dict.fromkeys((*due_providers, *retry_providers)))
        if coupling is not None and "coffeebench" in selected:
            ecom_pending = self.engine.store.get_background_provider_pending("ecommercebench")
            if ecom_pending is not None and (
                ecom_pending["result"] is not None or retry_due
                or self.engine.store.get_background_provider_pending("coffeebench") is not None
            ):
                selected = tuple(dict.fromkeys((*selected, "ecommercebench")))
            ecom_unavailable = (
                "ecommercebench" not in self._initialized_providers
                or "ecommercebench" in self._initialization_errors
                or "ecommercebench" in self.errors
            )
            if ecom_unavailable and not retry_due:
                selected = tuple(provider for provider in selected if provider != "coffeebench")
                with self._lock:
                    self.errors["coffeebench"] = {"code": "waiting_for_ecommerce_source"}
            elif ecom_unavailable and "ecommercebench" not in self._initialized_providers:
                selected = tuple(provider for provider in selected if provider != "coffeebench")
                with self._lock:
                    self.errors["coffeebench"] = {"code": "waiting_for_ecommerce_source"}
        selected = tuple(provider for provider in selected if enabled[provider])
        if not selected:
            if retry_due:
                self._retry_at = now + timedelta(seconds=30)
            return
        if self._provider_bindings is None:
            raise RuntimeError("company background provider bindings are unavailable")
        with self._lock:
            selected = tuple(provider for provider in selected
                             if self.engine.store.background_provider_control(provider)["enabled"])
            self._in_flight.update(selected)
        if not selected:
            return
        try:
            result = self.runtime.advance_company(
                now, bindings=self._provider_bindings, provider_ids=selected,
                coupling_policy=coupling,
            )
            providers = result.get("providers", {})
            self._fanout([event for provider in providers.values() for event in provider.get("events", [])])
            self.engine.store.record_company_finance_source_intents(
                [intent for provider in providers.values() for intent in provider.get("erp_intents", [])],
            )
            # Provider results are durable until both event provenance and finance
            # intents have been committed locally. A restart before this point can
            # replay the result; event and intent IDs make that replay idempotent.
            for provider_id, operation_id in result.get("provider_operation_ids", {}).items():
                self.engine.store.acknowledge_background_provider_result(provider_id, operation_id)
            try:
                self._reconcile({}, now)
                with self._lock:
                    self.errors.pop("native_finance_reconcile", None)
                    self._finance_retry_at = None
            except Exception as error:
                with self._lock:
                    self.errors["native_finance_reconcile"] = {"code": type(error).__name__}
                    self._finance_retry_at = now + timedelta(seconds=30)
                self._log_failure("company finance reconciliation failed", error)
            if due:
                try:
                    self._read_due_obligations(now)
                except Exception as error:
                    with self._lock:
                        self._obligation_error = type(error).__name__
                    self._log_failure("company due-obligation read failed", error)
            with self._lock:
                self.errors = {key: {"code": value["code"], "status_code": value["status_code"],
                                     **{k: value[k] for k in ("pending_operation_id", "pending_until") if k in value}}
                               for key, value in result.get("provider_errors", {}).items()}
                for provider, error in self._initialization_errors.items():
                    if provider not in self._initialized_providers:
                        self.errors.setdefault(provider, {"code": error["code"], "status_code": error["status_code"]})
                if self._finance_retry_at is not None:
                    self.errors["native_finance_reconcile"] = {"code": "reconciliation_failed"}
                for provider_id, value in providers.items():
                    next_due = value.get("next_due")
                    if next_due is None:
                        self._next_due.pop(provider_id, None)
                    else:
                        self._next_due[provider_id] = _aware(next_due)
                self._retry_at = (now + timedelta(seconds=30)) if (
                    result.get("provider_errors") or self._initialization_errors
                ) else None
            self.changed.set()
        finally:
            with self._lock:
                self._in_flight.difference_update(selected)

    def _run(self) -> None:
        while not self.stopped.is_set():
            company = self.engine.company_state()
            if company is not None:
                self._recover_requested_coffee()
            if company is None or company["paused"]:
                self.changed.wait()
                self.changed.clear()
                continue
            now = _aware(company["simulated_time"])
            coupling = self.profile.get("coupling", {}).get("ecommerce_to_coffee")
            coffee_blocked = coupling is not None and (
                "ecommercebench" not in self._initialized_providers
                or "ecommercebench" in self._initialization_errors
                or "ecommercebench" in self.errors
            )
            controls = {provider: self.engine.store.background_provider_control(provider)
                        for provider in ("coffeebench", "ecommercebench")}
            coffee_control = controls["coffeebench"]
            has_due_provider = any(value <= now and provider not in self.errors
                                   and "company_background" not in self.errors
                                   and controls[provider]["enabled"]
                                   and not (coffee_blocked and provider == "coffeebench")
                                   for provider, value in self._next_due.items())
            retry_due = self._retry_at is not None and self._retry_at <= now
            finance_retry_due = self._finance_retry_at is not None and self._finance_retry_at <= now
            initialization_due = not self._initialization_attempted or (
                bool(self._initialization_errors) and retry_due
            )
            if initialization_due or has_due_provider or retry_due or (coffee_control["enabled"] and coffee_control["recovery_requested"]):
                try:
                    self._advance_once(company, now)
                except Exception as error:
                    self.errors["company_background"] = {"code": type(error).__name__}
                    frames = [(frame.filename, frame.lineno, frame.name)
                              for frame in traceback.extract_tb(error.__traceback__)]
                    LOG.error("company background worker failed exception_type=%s stack=%s",
                              type(error).__name__, frames)
                    self._retry_at = now + timedelta(seconds=30)
            elif finance_retry_due:
                self._retry_finance(now)
            with self._lock:
                due_values = [value for provider, value in self._next_due.items()
                              if provider not in self.errors
                              and controls[provider]["enabled"]
                              and not (coffee_blocked and provider == "coffeebench")]
                due_values.extend(value for value in (self._retry_at, self._finance_retry_at) if value is not None)
            due = min(due_values) if due_values else None
            timeout = None if due is None else max(0.05, (due - now).total_seconds())
            self.changed.wait(timeout=timeout)
            self.changed.clear()

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="simulation-company-background", daemon=True)
            self.thread.start()
            self.wake()

    def wake(self) -> None:
        self.changed.set()

    def stop(self) -> None:
        self.stopped.set()
        self.changed.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def status(self) -> dict[str, Any]:
        with self._lock:
            errors = deepcopy(self.errors)
            providers = {}
            for provider in self._next_due.keys() | {"coffeebench", "ecommercebench"}:
                control = self.engine.store.background_provider_control(provider)
                if control["last_error"]:
                    errors[provider] = {"code": control["last_error"]}
                value = self._next_due.get(provider)
                providers[provider] = {
                    "next_due": value.isoformat() if value else None,
                    "initialized": provider in self._initialized_providers,
                    **self._provider_status(provider, control),
                }
            return {"enabled": True, "initialized": self._initialized,
                    "providers": providers, "errors": errors,
                    "obligations": deepcopy(self._due_obligations),
                    "obligations_error": self._obligation_error}

    @classmethod
    def activity_for_store(cls, store) -> list[dict[str, Any]]:
        activity = []
        for event in store.list_company_source_events():
            payload = {key: value for key, value in event["payload"].items()
                       if key in cls._PUBLIC_EVENT_PAYLOAD_KEYS}
            activity.append({"id": event["event_id"], "timestamp": event["simulated_time"],
                             "event": event["event_type"], "description": f"{event['event_type'].replace('_', ' ').title()}.",
                             "source": event["source"], "upstream_commit": event["upstream_commit"],
                             "entity_type": event["entity_type"], "entity_id": event["entity_id"],
                             "payload": payload})
        for receipt in store.list_company_finance_receipts():
            activity.append({"id": "finance:" + receipt["source_id"], "timestamp": receipt["as_of"],
                             "event": "FINANCE_SOURCE_RECONCILED", "description": f"{receipt['doctype']} {receipt['name']} linked to background activity.",
                             "native_document": {"doctype": receipt["doctype"], "name": receipt["name"]},
                             "source_id": receipt["source_id"], "created": receipt["created"]})
        for observation in store.list_company_due_obligation_observations():
            activity.append({"id": "obligation:" + observation["observation_id"],
                             "timestamp": observation["observed_at"],
                             "event": "DUE_OBLIGATION_OBSERVED",
                             "description": f"{observation['doctype']} {observation['name']} is due or outstanding (due {observation['due_date']}).",
                             "native_document": {"doctype": observation["doctype"], "name": observation["name"]},
                             "due_date": observation["due_date"], "modified": observation["modified"]})
        return sorted(activity, key=lambda item: (str(item["timestamp"]), str(item["id"])))

    def activity(self) -> list[dict[str, Any]]:
        return self.activity_for_store(self.engine.store)


__all__ = ["CompanyBackgroundService"]
