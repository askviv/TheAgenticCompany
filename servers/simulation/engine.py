"""Run-scoped simulation orchestration over the existing durable adapters.

This coordinator keeps private actor state in the store and exposes only the
visible case, native mailbox messages, and permitted ERP reads to callers.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from hashlib import sha256
import json
from functools import wraps
import fcntl
import inspect
import os
import re
from threading import RLock, local
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import ValidationError

from servers.simulation.actor_client import ActorTurnClient, ActorWorkerError
from servers.simulation.actor_protocol import ActorTurnRequest, ActorTurnResponse
from servers.simulation.actor_protocol import ActorEvaluateRequest
from servers.simulation.problem_adapters import PreparedProblem, ProblemAdapter
from servers.simulation.actor_worker_store import canonical_request_hash
from servers.simulation import customers
from servers.simulation.availability import response_time
from servers.simulation.businesses import (
    apply_supplier_market_event,
    initialize_supplier_business,
)
from servers.simulation.clock import ordered_due_events
from servers.simulation.metrics import evaluation_metrics
from servers.simulation.models import (
    AdvanceClock, ApprovalDecision, CanonicalEvent, PauseClock, SendMessage,
    StartScenario, UpdateCase, ERPAction, GoodsReceiptActionParameters,
    ShippingCommitActionParameters, ApprovalRequestEmail, HumanIntervention,
    ModelUsageRecord,
)
from servers.simulation.store import SQLiteStore, StoreConflict, StoreNotFound


class EngineConflict(StoreConflict):
    """A requested workflow transition conflicts with simulation state."""


class MailDeliveryUncertain(RuntimeError):
    """External message submission crossed SMTP boundary without a receipt."""


class ActorTurnUncertain(RuntimeError):
    """An upstream actor turn remains pending and can be reconciled by retry."""


class _CapturedEvaluationERP:
    """Read-only view over ERP records captured before evaluation began."""
    _OPERATIONS = frozenset({"assess_invoice", "get_purchase_invoice", "read_payment_status",
                             "get_purchase_order", "get_purchase_receipt", "read_sales_invoice_refunds"})

    def __init__(self, records: dict[str, Any]):
        self.records = records

    def __getattr__(self, operation: str):
        if operation not in self._OPERATIONS:
            raise AttributeError(operation)

        def read(entity_id: str):
            key = f"{operation}:{entity_id}"
            if key not in self.records:
                raise EngineConflict(f"native evaluation evidence was not captured for {operation}")
            return deepcopy(self.records[key])
        return read


class _FrozenEvaluationStore:
    """Read-only, in-memory evaluation evidence captured under the run guard."""
    def __init__(self, records: dict[str, Any]):
        self.records = records

    def get_run(self, run_id, **_kwargs):
        if str(run_id) != self.records["run"]["run_id"]:
            raise StoreNotFound("run not found")
        return deepcopy(self.records["run"])

    def get_case(self, run_id, generation, case_id, **_kwargs):
        value = self.records["cases"].get(str(case_id))
        if str(run_id) != self.records["run"]["run_id"] or generation != self.records["run"]["generation"] or value is None:
            raise StoreNotFound("case not found")
        return deepcopy(value)

    def list_events(self, run_id, generation, **_kwargs):
        self._assert_generation(run_id, generation)
        return deepcopy(self.records["events"])

    def list_audit(self, run_id, generation, **_kwargs):
        self._assert_generation(run_id, generation)
        return deepcopy(self.records["audit"])

    def list_outbox(self, run_id, generation, *, status=None, **_kwargs):
        self._assert_generation(run_id, generation)
        rows = self.records["outbox"]
        return deepcopy(rows if status is None else [row for row in rows if row["status"] == status])

    def list_actors(self, run_id, generation, **_kwargs):
        self._assert_generation(run_id, generation)
        return deepcopy(self.records["actors"])

    def list_private_messages(self, run_id, generation, **_kwargs):
        self._assert_generation(run_id, generation)
        return deepcopy(self.records["private_messages"])

    def is_company_experiment(self, run_id):
        if str(run_id) != self.records["run"]["run_id"]:
            raise StoreNotFound("run not found")
        return bool(self.records["is_company_experiment"])

    def company_experiments(self, company_id):
        if company_id != "company":
            return []
        return deepcopy(self.records["company_experiments"])

    def _assert_generation(self, run_id, generation):
        run = self.records["run"]
        if str(run_id) != run["run_id"] or generation != run["generation"]:
            raise StoreNotFound("run generation not found")


_SCENARIO_POLICY = {
    "version": "finance-manager-fixture-v2",
    "manager_response_delay_hours": 1,
    "receipt_quantity": 2000,
    "payment_billed_quantity": 10000,
    "payment_received_quantity": 10000,
    "expedited_change_budget": {"amount": "1000", "currency": "GBP"},
}

_E1_INITIAL_CUSTOMER_MESSAGE = (
    "The components in our recent delivery arrived damaged. Please advise how to "
    "resolve this. I have not included the serial numbers."
)


def _serialize_run(method):
    """Serialize operations by run across threads and coordinator processes."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bound = inspect.signature(method).bind(self, *args, **kwargs)
        bound.apply_defaults()
        if method.__name__ == "start":
            model, key, principal, company_id = (bound.arguments[name] for name in
                ("model", "key", "principal_actor_id", "shared_company_id"))
            scope = f"tac-company:{company_id}:{principal}:{key.strip()}" if company_id else f"tac-simulation:{principal}:{key.strip()}"
            run_id = uuid5(NAMESPACE_URL, scope)
            company_scoped = bool(company_id)
        else:
            first = next(value for name, value in bound.arguments.items() if name != "self")
            run_id = getattr(first, "run_id", first)
            company_scoped = self.store.is_company_experiment(run_id)
        if company_scoped:
            with self._operation_guard("company-clock"):
                with self._operation_guard(run_id):
                    return method(self, *args, **kwargs)
        with self._operation_guard(run_id):
            return method(self, *args, **kwargs)
    return wrapped


class SimulationEngine:
    """Coordinate persisted runs while leaving ERP and mail truth external."""

    def __init__(self, store: SQLiteStore, mail: Any, erp_factory: Any,
                 site_admin: Any, actors: list[dict[str, Any]]):
        self.store = store
        self.mail = mail
        self.erp_factory = erp_factory
        self.site_admin = site_admin
        self.actors = deepcopy(actors)
        self.actor_turn_client: ActorTurnClient | None = None
        self.problem_adapters: dict[str, ProblemAdapter] = {}
        self.support_actor_id = "requester"
        self.support_address = "support@example.com"
        self._actor_config = {str(a["actor_id"]): a for a in self.actors}
        self._locks_guard = RLock()
        self._run_locks: dict[str, RLock] = {}
        self._operation_depth = local()
        self._evaluation_local = local()
        self._lock_directory = self.store.db_path.parent / "locks"
        self._lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _run_lock(self, run_id: UUID | str) -> RLock:
        rid = str(run_id)
        with self._locks_guard:
            return self._run_locks.setdefault(rid, RLock())

    @contextmanager
    def _operation_guard(self, run_id: UUID | str):
        """Hold a run-scoped OS lock; nested calls in this thread reuse it."""
        rid = str(run_id)
        depths = getattr(self._operation_depth, "by_run", None)
        if depths is None:
            depths = self._operation_depth.by_run = {}
        with self._run_lock(rid):
            if depths.get(rid, 0):
                depths[rid] += 1
                try:
                    yield
                finally:
                    depths[rid] -= 1
                return

            lock_path = self._lock_directory / f"{rid}.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise EngineConflict("another operation currently owns this simulation run") from exc
                depths[rid] = 1
                try:
                    yield
                finally:
                    depths.pop(rid, None)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _scope(run_id: UUID | str, generation: int, actor_id: str, operation: str) -> str:
        return f"{run_id}:{generation}:{actor_id}:{operation}"

    def _assert_running(self, connection: Any, run_id: UUID | str, generation: int) -> dict[str, Any]:
        current = self.store.get_run(run_id, connection=connection)
        if current["generation"] != generation:
            raise EngineConflict("operation targets a stale run generation")
        if current["status"] != "running":
            raise EngineConflict(f"run is {current['status']}; operation is not available")
        return current

    @staticmethod
    def _key(value: str) -> str:
        if not value or not value.strip():
            raise ValueError("idempotency key is required")
        return value.strip()

    @staticmethod
    def _time(value: Any) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("simulated time must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def _run(self, run_id: UUID | str, *, allow_initializing: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        allowed = {"running"}
        if allow_initializing:
            allowed.add("initializing")
        if run["status"] not in allowed:
            raise EngineConflict(f"run is {run['status']}; operation is not available")
        return run

    def _member(self, run_id: UUID | str, actor_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        try:
            actor = self.store.get_actor(run_id, run["generation"], actor_id)
        except StoreNotFound as exc:
            raise EngineConflict("actor is not a member of this run") from exc
        if actor["kind"] == "agent" and run["agent_id"] != actor_id:
            raise EngineConflict("agent actor does not own this run")
        return actor

    def _check_agent_owner(self, run_id: UUID | str, actor_id: str) -> None:
        configured = self._actor_config.get(actor_id)
        if configured and configured.get("kind") == "agent":
            self._member(run_id, actor_id)

    def _erp(self, run: dict[str, Any]) -> Any:
        captured = getattr(self._evaluation_local, "capture", None)
        if captured is not None and captured.get("run_id") == str(run["run_id"]):
            return _CapturedEvaluationERP(captured["native_records"])
        return self.erp_factory(run["erp_binding"])

    @staticmethod
    def _evaluation_json_hash(value: Any) -> str:
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str).encode("utf-8")).hexdigest()

    def evaluation_identity(self, run_id: UUID | str, generation: int, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["generation"] != generation:
            raise EngineConflict("evaluation generation changed during reservation")
        if run.get("manifest", {}).get("source_kind") == "company_provider":
            experiments = getattr(self, "provider_experiment_store", None)
            worker = getattr(self, "provider_experiment_worker", None)
            if experiments is None or worker is None:
                raise EngineConflict("native provider evaluation is not configured")
            subject = experiments.get(str(run_id))
            if subject is None or subject["status"] != "registered" or not subject.get("upstream_commit"):
                raise EngineConflict("native provider case is not available for evaluation")
            case = json.loads(subject["case_json"])
            expected_identity = self._evaluation_json_hash({
                "provider_id": subject["provider_id"], "task_id": subject["task_id"],
                "conversation_id": subject["conversation_id"], "company_id": "company",
                "company_actor_id": case["responsible_actor_id"],
                "company_account_reference": case["company_account_reference"],
                "company_issue_reference": case["issue_reference"],
            })
            source = snapshot if isinstance(snapshot, dict) else {}
            for field, expected in (("provider_id", subject["provider_id"]),
                                    ("task_id", subject["task_id"]),
                                    ("conversation_id", subject["conversation_id"]),
                                    ("upstream_commit", subject["upstream_commit"]),
                                    ("identity_fingerprint", expected_identity)):
                if field in source and source[field] != expected:
                    raise EngineConflict("native provider evaluation identity changed")
            domain = {"tau2-airline": "airline", "tau2-telecom": "telecom",
                      "tau2-banking": "banking_knowledge"}[subject["provider_id"]]
            return {"company_id": "company", "benchmark": "tau2-bench", "domain": domain,
                    "benchmark_version": subject["upstream_commit"],
                    "evaluation_mode": "company_adapted"}
        scenario = run["scenario_id"]
        native_company_id = (run.get("erp_binding", {}).get("documents", {}).get("company")
                             or "company")
        if scenario == "TAU_RETAIL":
            prepared = PreparedProblem.model_validate(run["manifest"]["prepared_problem"])
            adapted = self.store.is_company_experiment(run_id)
            return {"company_id": "company" if adapted else native_company_id,
                    "benchmark": "tau2-bench", "domain": "retail",
                    "benchmark_version": prepared.origin.commit,
                    "evaluation_mode": "company_adapted" if adapted else "native"}
        manifest = run["manifest"].get("environment") or {}
        current_environment_provider = getattr(self, "environment_manifest_provider", None)
        current_environment = (current_environment_provider()
                               if callable(current_environment_provider) else manifest)
        fingerprint = ((snapshot or {}).get("evaluator_fingerprint")
                       or (current_environment or {}).get("source_tree_sha256")
                       or self._evaluation_json_hash(current_environment or manifest))
        adapted = self.store.is_company_experiment(run_id)
        return {"company_id": "company" if adapted else native_company_id,
                "benchmark": "company-scenarios", "domain": scenario,
                "benchmark_version": str(fingerprint),
                "evaluation_mode": "company_adapted"}

    def _evaluation_native_specs(self, run: dict[str, Any], trajectory: dict[str, Any]) -> list[tuple[str, str]]:
        docs = run.get("erp_binding", {}).get("documents", {})
        scenario = run["scenario_id"]
        specs: set[tuple[str, str]] = set()
        invoice = docs.get("purchase_invoice")
        order = docs.get("purchase_order")
        customer_order = docs.get("customer_order")
        if scenario in {"A1", "S01"} and invoice:
            invoice_id = _document_id(invoice)
            specs.update({("assess_invoice", invoice_id), ("get_purchase_invoice", invoice_id),
                          ("read_payment_status", invoice_id)})
        if scenario in {"B1", "S01", "C1", "D1", "E1", "A1"} and order:
            specs.add(("get_purchase_order", _document_id(order)))
        if scenario in {"E1", "S01"} and customer_order:
            specs.add(("read_sales_invoice_refunds", _document_id(customer_order)))
        if scenario == "S01":
            for audit in trajectory.get("audit", []):
                record = audit.get("private_record", {})
                if record.get("event") == "approval_action_completed" and record.get("operation") == "commit_expedited_shipping":
                    linked = record.get("result", {}).get("purchase_order_id")
                    if linked:
                        specs.add(("get_purchase_order", str(linked)))
                if record.get("event") == "approval_action_completed" and record.get("operation") == "create_goods_receipt":
                    receipt_id = record.get("result", {}).get("receipt_id")
                    if receipt_id:
                        specs.add(("get_purchase_receipt", str(receipt_id)))
        return sorted((operation, entity) for operation, entity in specs if entity and entity != "None")

    def _capture_evaluation_native(self, run: dict[str, Any], trajectory: dict[str, Any]) -> dict[str, Any]:
        if run["scenario_id"] == "TAU_RETAIL":
            prepared = PreparedProblem.model_validate(run["manifest"]["prepared_problem"])
            adapter = self.problem_adapters[run["scenario_id"]]
            capture = getattr(adapter, "capture_evaluation_evidence", None)
            if not callable(capture):
                raise RuntimeError("τ retail evaluator does not support frozen evidence capture")
            return {"mode": "tau_retail", "evidence": capture(prepared=prepared), "native_records": {}}
        records = {}
        adapter = self.erp_factory(run["erp_binding"])
        specs = self._evaluation_native_specs(run, trajectory)
        refs = self._evaluation_native_refs(run, specs)
        version_reader = getattr(self.site_admin, "capture_company_evaluation_evidence", None)
        if refs and not callable(version_reader):
            raise RuntimeError("ERP site admin cannot capture a consistent native evaluation version manifest")
        version_before = (version_reader(refs, versions_only=True)["version_manifest"] if refs else {})
        for operation, entity in specs:
            reader = getattr(adapter, operation, None)
            if not callable(reader):
                raise RuntimeError(f"ERP adapter cannot capture evaluation evidence for {operation}")
            value = reader(entity)
            records[f"{operation}:{entity}"] = value
        version_after = (version_reader(refs, versions_only=True)["version_manifest"] if refs else {})
        return {"mode": "legacy_erp", "native_records": records,
                "version_manifest": version_before,
                "capture_consistent": self._evaluation_json_hash(version_before)
                    == self._evaluation_json_hash(version_after)}

    @staticmethod
    def _evaluation_native_refs(run: dict[str, Any], specs: list[tuple[str, str]]) -> list[dict[str, str]]:
        doctypes = {
            "assess_invoice": "Purchase Invoice", "get_purchase_invoice": "Purchase Invoice",
            "read_payment_status": "Purchase Invoice", "get_purchase_order": "Purchase Order",
            "get_purchase_receipt": "Purchase Receipt", "read_sales_invoice_refunds": "Sales Order",
        }
        refs = {(doctypes[operation], entity) for operation, entity in specs}
        docs = run.get("erp_binding", {}).get("documents", {})
        # The native evidence service requires financial roots and follows
        # linked documents and GL rows in one read view.
        for field, doctype in (("purchase_invoice", "Purchase Invoice"), ("sales_invoice", "Sales Invoice")):
            entity = _document_id(docs.get(field))
            if entity:
                refs.add((doctype, entity))
        return [{"doctype": doctype, "name": name} for doctype, name in sorted(refs)]

    def _capture_evaluation_store(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id, generation = run["run_id"], run["generation"]
        safe_run = deepcopy(run)
        safe_run.get("erp_binding", {}).pop("credentials_ref", None)
        case_ids = {str(run.get("manifest", {}).get("case_id"))}
        cases = {case_id: self.store.get_case(run_id, generation, case_id)
                 for case_id in case_ids if case_id and case_id != "None"}
        return {
            "run": safe_run,
            "cases": cases,
            "events": self.store.list_events(run_id, generation),
            "audit": self.store.list_audit(run_id, generation),
            "outbox": self.store.list_outbox(run_id, generation),
            "actors": self.store.list_actors(run_id, generation),
            "private_messages": self.store.list_private_messages(run_id, generation),
            "is_company_experiment": self.store.is_company_experiment(run_id),
            "company_experiments": self.store.company_experiments("company"),
        }

    def capture_evaluation_inputs(self, run_id: UUID | str, generation: int) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["generation"] != generation:
            raise EngineConflict("evaluation generation does not match the current run")
        if run.get("manifest", {}).get("source_kind") == "company_provider":
            experiments = getattr(self, "provider_experiment_store", None)
            worker = getattr(self, "provider_experiment_worker", None)
            if experiments is None or worker is None:
                raise EngineConflict("native provider evaluation is not configured")
            subject = experiments.get(str(run_id))
            if subject is None or subject["status"] != "registered":
                raise EngineConflict("native provider case is not available for evaluation")
            capture = worker.capture_evaluation(subject["provider_id"], subject["conversation_id"])
            case = json.loads(subject["case_json"])
            expected_identity = self._evaluation_json_hash({
                "provider_id": subject["provider_id"], "task_id": subject["task_id"],
                "conversation_id": subject["conversation_id"], "company_id": "company",
                "company_actor_id": case["responsible_actor_id"],
                "company_account_reference": case["company_account_reference"],
                "company_issue_reference": case["issue_reference"],
            })
            if (capture.get("provider_id") != subject["provider_id"]
                    or capture.get("task_id") != subject["task_id"]
                    or capture.get("conversation_id") != subject["conversation_id"]
                    or capture.get("upstream_commit") != subject["upstream_commit"]
                    or capture.get("identity_fingerprint") != expected_identity):
                raise EngineConflict("native provider evaluation snapshot does not match the case")
            return {
                "run_id": str(run_id), "generation": generation,
                "identity": self.evaluation_identity(run_id, generation, capture),
                "provider_capture": capture,
                "capture_consistent": True,
                "evaluator_fingerprint": (getattr(self, "environment_manifest_provider", lambda: {})()
                                          or {}).get("source_tree_sha256"),
            }
        trajectory = self.trajectory(run_id)
        trajectory.pop("prior_generations", None)
        trajectory["private_messages"] = self.store.list_private_messages(run_id, generation)
        native = self._capture_evaluation_native(run, trajectory)
        capture_consistent = native.get("capture_consistent", True)
        source_records = self._capture_evaluation_store(run)
        feedback = None
        feedback_reader = getattr(self, "company_feedback_summary", None)
        if self.store.is_company_experiment(run_id) and callable(feedback_reader):
            feedback = feedback_reader(str(run_id))
        environment = run["manifest"].get("environment")
        current_environment_provider = getattr(self, "environment_manifest_provider", None)
        evaluation_environment = (current_environment_provider()
                                  if callable(current_environment_provider) else environment)
        return {"run_id": str(run_id), "generation": generation,
                "identity": self.evaluation_identity(run_id, generation, None),
                "trajectory": trajectory,
                "trajectory_sha256": self._evaluation_json_hash(trajectory),
                "native": native,
                "native_sha256": self._evaluation_json_hash(native),
                "source_records": source_records,
                "source_records_sha256": self._evaluation_json_hash(source_records),
                "capture_consistent": capture_consistent,
                "feedback": feedback,
                "environment": environment,
                "evaluator_fingerprint": (evaluation_environment or {}).get("source_tree_sha256")}

    def evaluate_captured(self, run_id: UUID | str, snapshot: dict[str, Any]) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run.get("manifest", {}).get("source_kind") == "company_provider":
            experiments = getattr(self, "provider_experiment_store", None)
            worker = getattr(self, "provider_experiment_worker", None)
            capture = snapshot.get("provider_capture")
            if experiments is None or worker is None or not isinstance(capture, dict):
                raise EngineConflict("native provider evaluation snapshot is unavailable")
            subject = experiments.get(str(run_id))
            if subject is None or subject["status"] != "registered":
                raise EngineConflict("native provider case is not available for evaluation")
            from .provider_contracts import ProviderEvaluationResult

            result = ProviderEvaluationResult.model_validate(worker.evaluate_captured(
                subject["provider_id"], subject["conversation_id"],
                capture["capture_ref"], capture["capture_sha256"],
            ))
            if (result.provider_id != subject["provider_id"] or result.task_id != subject["task_id"]
                    or result.conversation_id != subject["conversation_id"]):
                raise EngineConflict("native provider evaluation result does not match the case")
            native = dict(result.native_reward_info or {})
            if result.status == "inconclusive":
                native["reward_status"] = "inconclusive"
            return {
                "problem_evaluation": {"evaluation": native},
                "mapping_gaps": ([result.reason_code] if result.reason_code else []),
                "native_provider_evaluation": {
                    "provider_id": result.provider_id, "task_id": result.task_id,
                    "evaluation_mode": result.evaluation_mode,
                    "native_state_revision": result.native_state_revision,
                    "upstream_commit": subject["upstream_commit"],
                },
                "warnings": ["provider_assistant_scored", "not_viv_performance_score"],
            }
        frozen_store = _FrozenEvaluationStore(snapshot["source_records"])
        run = frozen_store.get_run(run_id)
        if str(run_id) != snapshot.get("run_id") or run["generation"] != snapshot.get("generation"):
            raise EngineConflict("evaluation capture no longer matches the run generation")
        self._evaluation_local.store = frozen_store
        self._evaluation_local.capture = {"run_id": str(run_id), **snapshot["native"]}
        self._evaluation_local.feedback = snapshot.get("feedback")
        try:
            # Run the native scoring code against immutable captured state;
            # model judgments likewise receive only the pre-captured evidence.
            return self.evaluate.__wrapped__(self, run_id)
        finally:
            self._evaluation_local.capture = None
            self._evaluation_local.store = None
            self._evaluation_local.feedback = None

    def verify_evaluation_inputs(self, run_id: UUID | str, snapshot: dict[str, Any]) -> bool:
        run = self.store.get_run(run_id)
        if run["generation"] != snapshot.get("generation"):
            return False
        if run.get("manifest", {}).get("source_kind") == "company_provider":
            experiments = getattr(self, "provider_experiment_store", None)
            worker = getattr(self, "provider_experiment_worker", None)
            capture = snapshot.get("provider_capture")
            if experiments is None or worker is None or not isinstance(capture, dict):
                return False
            subject = experiments.get(str(run_id))
            if subject is None:
                return False
            source_matches = worker.verify_evaluation_capture(
                subject["provider_id"], subject["conversation_id"],
                capture["capture_ref"], capture["capture_sha256"],
            )
            current_environment_provider = getattr(self, "environment_manifest_provider", None)
            current_environment = (current_environment_provider()
                                   if callable(current_environment_provider) else None)
            return (source_matches
                    and (current_environment or {}).get("source_tree_sha256")
                    == snapshot.get("evaluator_fingerprint"))
        if not snapshot.get("capture_consistent", True):
            return False
        current_environment_provider = getattr(self, "environment_manifest_provider", None)
        current_environment = current_environment_provider() if callable(current_environment_provider) else None
        current_fingerprint = (current_environment or {}).get("source_tree_sha256")
        return current_fingerprint == snapshot.get("evaluator_fingerprint")

    def _erp_owner_id(self, run: dict[str, Any]) -> str:
        """Native adapter approval is bound to its site; experiment grants remain run scoped."""
        if not self.store.is_company_experiment(run["run_id"]):
            return str(run["run_id"])
        site_name = str(run["erp_binding"].get("site_name", ""))
        match = re.fullmatch(r"erp-sim-([0-9a-f]{32})\.localhost", site_name)
        if not match:
            raise EngineConflict("company ERP binding has no canonical native site identity")
        from uuid import UUID
        return str(UUID(hex=match.group(1)))

    def company_state(self) -> dict[str, Any] | None:
        return self.store.get_company("company")

    def start_company(self, binding: dict[str, Any], key: str) -> dict[str, Any]:
        key = self._key(key)
        site_name = str(binding.get("site_name", ""))
        if not re.fullmatch(r"erp-sim-[0-9a-f]{32}\.localhost", site_name):
            raise EngineConflict("company ERP binding has no canonical native site identity")
        required = {"company", "supplier", "item", "warehouse", "purchase_order", "purchase_receipt",
                    "purchase_invoice", "customer", "customer_order"}
        documents = binding.get("documents")
        if not isinstance(documents, dict) or not required.issubset(documents):
            raise EngineConflict("company ERP binding is missing native baseline documents")
        if not binding.get("credentials_ref"):
            raise EngineConflict("company ERP credentials reference is unavailable")
        now = datetime.now(timezone.utc)
        def operation(connection):
            existing = self.store.get_company("company", connection=connection)
            if existing:
                if existing["erp_binding"] != binding:
                    raise EngineConflict("configured company ERP binding does not match the persisted company site")
                return existing
            return self.store.start_company("company", now, now, binding, connection=connection)
        result = self.store.execute_idempotent("company.start", key, {"binding": binding}, operation)
        wake = getattr(self, "company_clock_wake", None)
        if wake:
            wake()
        return self.store.get_company("company") or result

    def pause_company(self, paused: bool, key: str, *, now: datetime | None = None) -> dict[str, Any]:
        key = self._key(key)
        with self._operation_guard("company-clock"):
            wall_now = self._time(now or datetime.now(timezone.utc))
            if paused:
                self.tick_company_clock(wall_now)
            request = {"paused": bool(paused)}
            def operation(connection):
                company = self.store.get_company("company", connection=connection)
                if company is None:
                    raise EngineConflict("company has not been started")
                return self.store.update_company("company", status="paused" if paused else "running",
                                                 paused=paused, wall_anchor=wall_now,
                                                 connection=connection)
            result = self.store.execute_idempotent("company.pause", key, request, operation)
        wake = getattr(self, "company_clock_wake", None)
        if wake:
            wake()
        return result

    def tick_company_clock(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Advance every company experiment to the common wall-clock instant.

        The persisted wall anchor is advanced only after all run processors
        succeed. A partial retry therefore reuses each child's same SimPy
        target and engine idempotency receipt.
        """
        now = self._time(now or datetime.now(timezone.utc))
        with self._operation_guard("company-clock"):
            company = self.store.get_company("company")
            if company is None or company["paused"]:
                return company
            before = self._time(company["simulated_time"])
            anchor = self._time(company["wall_anchor"])
            target = before + max(now - anchor, timedelta(0))
            experiments = [item for item in self.store.company_experiments("company")
                           if item["status"] == "running"]
            runs = {item["experiment_id"]: self.store.get_run(item["experiment_id"]) for item in experiments}
            while True:
                for run in runs.values():
                    self._schedule_policy_responses(run)
                due = []
                for experiment_id, run in runs.items():
                    for event in self.store.list_events(experiment_id, run["generation"], status="pending"):
                        due_time = max(self._time(event["simulated_time"]), self._time(run["simulated_time"]))
                        if due_time <= target:
                            due.append((due_time, experiment_id, event["sequence"], event["event_id"]))
                if not due:
                    for experiment_id, run in runs.items():
                        if self._time(run["simulated_time"]) < target:
                            self.store.update_run(experiment_id, run["generation"], simulated_time=target)
                    break
                due_time, experiment_id, _sequence, event_id = min(due)
                for other_id, run in runs.items():
                    if other_id != experiment_id and self._time(run["simulated_time"]) < due_time:
                        self.store.update_run(other_id, run["generation"], simulated_time=due_time)
                        runs[other_id] = self.store.get_run(other_id)
                event_key = f"company-clock:{target.isoformat()}:{experiment_id}:{event_id}"
                self.advance(AdvanceClock(run_id=UUID(experiment_id), until=due_time), event_key,
                             company_tick=True, company_event_id=event_id)
                runs[experiment_id] = self.store.get_run(experiment_id)
            return self.store.update_company("company", simulated_time=target, wall_anchor=now)

    def start_company_experiment(self, model: StartScenario, key: str, principal_actor_id: str) -> dict[str, Any]:
        self.tick_company_clock()
        with self._operation_guard("company-clock"):
            company = self.store.get_company("company")
            if company is None:
                raise EngineConflict("company has not been started")
            return self.start(model, key, principal_actor_id, shared_company_id="company")

    @_serialize_run
    def start(self, model: StartScenario, key: str, principal_actor_id: str,
              shared_company_id: str | None = None) -> dict[str, Any]:
        key = self._key(key)
        configured_agent = self._actor_config.get(model.agent_id)
        if configured_agent is None or configured_agent.get("kind") != "agent":
            raise EngineConflict("agent_id must name a configured agent actor")
        if model.scenario_id == "TAU_RETAIL" and not shared_company_id:
            raise EngineConflict("TAU_RETAIL experiments use the shared company ERP site")
        required_kinds = {
            "A1": set(), "B1": {"warehouse", "logistics"},
            "C1": {"supplier"}, "D1": {"supplier"},
            "E1": {"customer"},
            "S01": {"warehouse", "logistics", "approver", "supplier", "customer"},
            "TAU_RETAIL": {"customer"},
        }[model.scenario_id]
        configured_kinds = {str(a.get("kind")) for a in self.actors}
        if missing := required_kinds - configured_kinds:
            raise EngineConflict(f"configured actor directory is missing required roles: {', '.join(sorted(missing))}")
        if not any(a.get("actor_id") == "requester" and a.get("email") for a in self.actors):
            raise EngineConflict("configured requester actor is required to deliver the normal employee instruction")
        run_scope = (f"tac-company:{shared_company_id}:{principal_actor_id}:{key}" if shared_company_id
                     else f"tac-simulation:{principal_actor_id}:{key}")
        run_id = uuid5(NAMESPACE_URL, run_scope)
        if shared_company_id and model.agent_configuration.get("start_time") is not None:
            raise EngineConflict("company experiments use the shared company clock")
        request = {"scenario": model.model_dump(mode="json"), "principal_actor_id": principal_actor_id}
        if shared_company_id:
            request["shared_company_id"] = shared_company_id

        def reserve(connection):
            try:
                return self.store.get_run(run_id, connection=connection)
            except StoreNotFound:
                self.store.create_run(run_id, model, connection=connection)
                return self.store.update_run(run_id, 0, status="initializing", connection=connection)

        scope = f"run.start:{principal_actor_id}:{shared_company_id}" if shared_company_id else f"run.start:{principal_actor_id}"
        reserved = self.store.execute_idempotent(scope, key, request, reserve)
        # A prior failed initialization leaves a durable initializing run. Site
        # creation is run-UUID-scoped and the provider owns idempotent resumption.
        run_id = UUID(reserved["run_id"])
        run = self._run(run_id, allow_initializing=True)
        if run["status"] != "running":
            company = self.store.get_company(shared_company_id) if shared_company_id else None
            if shared_company_id and company is None:
                raise EngineConflict("company has not been started")
            binding = run["erp_binding"] or (company["erp_binding"] if company else self.site_admin.create(str(run_id)))
            default_time = company["simulated_time"] if company else "2026-10-06T09:00:00+00:00"
            now = self._time(model.agent_configuration.get("start_time", default_time))
            snapshot = self._initial_snapshot(model, binding, now, run_id)
            if model.scenario_id != "TAU_RETAIL":
                self._capture_native_baseline(snapshot)
            with self.store.transaction() as connection:
                current = self.store.get_run(run_id, connection=connection)
                if current["status"] == "initializing":
                    self.store.update_run(run_id, current["generation"], erp_binding=binding,
                                          simulated_time=now, manifest=snapshot["run"]["manifest"],
                                          status="running", connection=connection)
                    for case in snapshot["cases"]:
                        self.store.put_case(run_id, 0, case["case_id"], case["visible"], connection=connection)
                    for actor in snapshot["actors"]:
                        self.store.put_actor(run_id, 0, actor["actor_id"], kind=actor["kind"],
                                             email=actor.get("email"), private_state=actor["private_state"],
                                             connection=connection)
                    for event in snapshot["events"]:
                        self.store.enqueue_event(CanonicalEvent.model_validate(event), 0, connection=connection)
                    requester = next((actor for actor in snapshot["actors"] if actor["actor_id"] == "requester"), None)
                    if requester is not None and model.scenario_id != "TAU_RETAIL":
                        outbox_id = "initial-instruction"
                        self.store.enqueue_outbox(run_id, 0, outbox_id,
                            {"from_actor_id": requester["actor_id"], "to_actor_id": model.agent_id,
                             "subject": "Please investigate the business case",
                             "body": snapshot["run"]["manifest"]["agent_instruction"],
                             "message_id": _message_id(str(run_id), 0, outbox_id),
                             "case_id": snapshot["cases"][0]["case_id"], "in_reply_to": None,
                             "automated": False, "simulated_time": snapshot["run"]["simulated_time"]},
                            actor_id=requester["actor_id"], connection=connection)
                    customer = next((actor for actor in snapshot["actors"] if actor["actor_id"] == "customer"), None)
                    if customer is not None and model.scenario_id == "E1":
                        outbox_id = "customer-initial-complaint"
                        self.store.enqueue_outbox(run_id, 0, outbox_id,
                            {"from_actor_id": customer["actor_id"], "to_actor_id": model.agent_id,
                             "subject": "Customer complaint about damaged components",
                             "body": _E1_INITIAL_CUSTOMER_MESSAGE,
                             "message_id": _message_id(str(run_id), 0, outbox_id),
                             "case_id": snapshot["cases"][0]["case_id"], "in_reply_to": None,
                             "automated": True, "simulated_time": snapshot["run"]["simulated_time"]},
                            actor_id=customer["actor_id"], connection=connection)
                    if customer is not None and model.scenario_id == "TAU_RETAIL":
                        prepared = PreparedProblem.model_validate(snapshot["run"]["manifest"]["prepared_problem"])
                        outbox_id = "retail-customer-initial-email"
                        self.store.enqueue_outbox(run_id, 0, outbox_id,
                            {"from_actor_id": customer["actor_id"], "to_actor_id": prepared.support_actor_id,
                             "subject": prepared.initial_email.subject,
                             "body": prepared.initial_email.body,
                             "message_id": prepared.initial_email.message_id,
                             "delivery_address": self.support_address,
                             "case_id": snapshot["cases"][0]["case_id"], "in_reply_to": None,
                             "automated": True, "simulated_time": snapshot["run"]["simulated_time"]},
                            actor_id=customer["actor_id"], connection=connection)
                    self.store.append_audit(run_id, 0, {"event": "run_initialized", "scenario_id": model.scenario_id}, connection=connection)
                    if shared_company_id:
                        self.store.link_company_experiment(shared_company_id, run_id, connection=connection)
        self.pump(run_id)
        return self._run(run_id)

    def _initial_snapshot(self, model: StartScenario, binding: dict[str, Any], now: datetime, run_id: UUID) -> dict[str, Any]:
        if model.scenario_id == "TAU_RETAIL":
            return self._initial_problem_snapshot(model, binding, now, run_id)
        docs = binding.get("documents", {})
        required_by_scenario = {
            "A1": ("purchase_order", "purchase_invoice", "purchase_receipt", "supplier", "item", "warehouse"),
            "B1": ("purchase_order", "purchase_receipt", "supplier", "warehouse"),
            "C1": ("purchase_order", "supplier"),
            "D1": ("purchase_order", "supplier"),
            "E1": ("customer_order", "customer"),
            "S01": ("purchase_order", "purchase_invoice", "purchase_receipt", "supplier", "item", "warehouse", "customer_order", "customer"),
        }
        for document_name in required_by_scenario[model.scenario_id]:
            if _document_id(docs.get(document_name)) is None:
                raise EngineConflict(f"native ERP binding is missing required {document_name} document")
        if not any(actor.get("actor_id") == "requester" and actor.get("email") for actor in self.actors):
            raise EngineConflict("configured requester actor is required to deliver the normal employee instruction")
        po = docs.get("purchase_order", {})
        invoice = docs.get("purchase_invoice", {})
        receipt = docs.get("purchase_receipt", {})
        scenario_id = model.scenario_id
        case_id = f"{scenario_id}-case"
        titles = {
            "A1": "Review the supplier invoice discrepancy",
            "B1": "Investigate the delayed shipment",
            "C1": "Reconcile the supplier delivery dispute",
            "D1": "Review the supplier's revised quotation",
            "E1": "Investigate the customer's damage complaint",
            "S01": "Investigate the supplier invoice and delivery discrepancy",
        }
        visible = {"case_id": case_id, "scenario_id": scenario_id, "status": "open",
                   "title": titles[scenario_id],
                   "purchase_order_id": _document_id(po), "purchase_invoice_id": _document_id(invoice),
                   "purchase_receipt_id": _document_id(receipt), "eta": None, "notes": ""}
        actors = []
        supplier_id = _document_id(docs.get("supplier"))
        actor_ids = {configured["actor_id"] for configured in self.actors}
        for configured in self.actors:
            kind = configured.get("kind", "employee")
            private_state = deepcopy(configured.get("private_state", {}))
            # None explicitly denotes an always-open calendar. Operators may
            # supply real weekly windows without changing the actor runtime.
            private_state.setdefault("availability_calendar", None)
            private_state.setdefault("response_policy_version", "company-role-v1")
            if kind not in {"agent", "controller"}:
                contact = "requester" if configured["actor_id"] == "manager" else "manager"
                if contact in actor_ids and contact != configured["actor_id"]:
                    private_state.setdefault("escalation_actor_id", contact)
            if kind == "supplier":
                private_state.setdefault("available_day", 2 if model.scenario_id == "S01" else 1)
                private_state.setdefault("seed", model.seed)
                if supplier_id is not None:
                    private_state["supplier_id"] = supplier_id
                private_state.setdefault("shipment_id", "SHIP-001")
                private_state.setdefault("dispatch_quantity", 10000)
                private_state.setdefault("readable_unit", "components")
                private_state.setdefault("dispatch_evidence_ids", ["dispatch-record-001"])
                if model.scenario_id == "S01":
                    private_state.setdefault("expedite_proposal", {
                        "quote_id": f"EXP-{model.seed:04d}-001",
                        "shipment_id": private_state["shipment_id"],
                        "fee_amount": "750.00", "fee_cap_amount": "1000.00",
                        "fee_currency": "GBP", "service": "priority freight",
                        "delivery_commitment": (now + timedelta(days=13)).date().isoformat(),
                    })
                private_state.setdefault("pricing_config", {
                    "family": "Candid", "reservation_price": "50", "maximum_offer": "100",
                    "kappa_b": 1.0, "stance": "neutral", "opening_harshness": 0.5,
                    "maximum_rounds": 5, "minimum_offer": "0", "minor_unit": 2,
                })
                private_state.setdefault("contract_unit_price", "50")
                private_state = initialize_supplier_business(
                    private_state, seed=model.seed, supplier_id=str(private_state["supplier_id"]),
                )
            elif kind == "warehouse":
                private_state.setdefault("available_day", 3 if model.scenario_id == "S01" else 1)
                private_state.setdefault("shipment_id", "SHIP-001")
                private_state.setdefault("received_quantity", 8000)
                private_state.setdefault("pending_quantity", 2000)
            elif kind == "logistics":
                private_state.setdefault("available_day", 4 if model.scenario_id == "S01" else 1)
                private_state.setdefault("shipment_id", "SHIP-001")
                private_state.setdefault("quantity", 2000)
                private_state.setdefault("location", "remote distribution centre")
                private_state.setdefault("location_reveal_day", 4)
            elif kind == "customer" and model.scenario_id in {"E1", "S01"}:
                private_state["request_type"] = "damage" if model.scenario_id == "E1" else "delivery_change"
                private_state.setdefault("available_day", 9 if model.scenario_id == "S01" else 1)
                if docs.get("customer_order"):
                    private_state["order_id"] = _document_id(docs["customer_order"])
                if docs.get("customer"):
                    private_state["customer_id"] = _document_id(docs["customer"])
                if model.scenario_id == "E1":
                    private_state.setdefault("damage_description", "components arrived damaged")
                    private_state.setdefault("serial_number", None)
                elif model.scenario_id == "S01":
                    private_state.pop("damage_description", None)
                    private_state.pop("serial_number", None)
                    private_state.setdefault("requested_earlier_delivery_date", (now + timedelta(days=11)).date().isoformat())
                private_state["tau2_task"] = customers.build_customer_task(
                    run_id=str(run_id), generation=0, scenario_id=model.scenario_id,
                    private_state=private_state,
                    initial_message=(
                        {"body": _E1_INITIAL_CUSTOMER_MESSAGE}
                        if model.scenario_id == "E1" else None
                    ),
                )
            actors.append({"actor_id": configured["actor_id"], "kind": kind,
                           "email": configured.get("email"), "private_state": private_state})
        # Private actor facts remain in actor state; visible case has business
        # record identifiers only and does not reveal the scenario timeline.
        instructions = {
            "A1": "Please review the supplier invoice against the purchase order and receiving records. Open a review if the records do not reconcile.",
            "B1": "Please investigate the delayed delivery, confirm its revised schedule and keep the relevant stakeholders informed.",
            "C1": "Please investigate the delivery discrepancy and ask the supplier for supporting dispatch evidence.",
            "D1": "Please review the supplier's revised quotation against the existing purchase order and route any proposed contract change for authorization.",
            "E1": "Please investigate the customer's complaint, request any missing evidence and link the case to the relevant order before recommending next steps.",
            "S01": "Please investigate the blocked supplier invoice, resolve the delivery discrepancy, coordinate with the relevant teams and make sure the order can be closed correctly. Escalate decisions requiring approval.",
        }
        business_references = [f"Case reference: {case_id}"]
        for label, key in (("Purchase order", "purchase_order"),
                           ("Purchase invoice", "purchase_invoice"),
                           ("Purchase receipt", "purchase_receipt"),
                           ("Customer order", "customer_order")):
            reference = _document_id(docs.get(key))
            if reference:
                business_references.append(f"{label} reference: {reference}")
        instruction = (instructions[scenario_id] + f"\n\nCompany date (UTC): {now.date().isoformat()}"
                       + "\n\nBusiness record references:\n" + "\n".join(business_references))
        manifest = {"case_id": case_id, "initial_time": now.isoformat(),
                    "agent_instruction": instruction,
                    "agent_configuration": _safe_agent_configuration(model.agent_configuration),
                    "agent_metadata": model.agent_metadata.model_dump(mode="json", exclude_none=True),
                    "environment": deepcopy(getattr(self, "environment_manifest", {})),
                    "private_scenario_policy": deepcopy(_SCENARIO_POLICY)}
        events = self._scenario_events(model, now, binding, case_id, run_id)
        return {"run": {"status": "running", "simulated_time": now.isoformat(), "paused": False,
                         "manifest": manifest, "erp_binding": binding},
                "cases": [{"case_id": case_id, "visible": visible}], "actors": actors, "events": events}

    def _initial_problem_snapshot(self, model: StartScenario, binding: dict[str, Any], now: datetime,
                                  run_id: UUID) -> dict[str, Any]:
        adapter = self.problem_adapters.get(model.scenario_id)
        if adapter is None:
            raise EngineConflict(f"problem adapter is not configured for {model.scenario_id}")
        customer = next((actor for actor in self.actors if actor.get("kind") == "customer"
                         and actor.get("email")), None)
        if customer is None:
            raise EngineConflict("configured customer actor with email is required for τ retail")
        support_actor_id = getattr(self, "support_actor_id", "requester")
        support = next((actor for actor in self.actors
                        if actor.get("actor_id") == support_actor_id
                        and actor.get("kind") not in {"agent", "customer", "controller"}
                        and actor.get("email")), None)
        if support is None:
            raise EngineConflict("configured customer support employee mailbox is required for τ retail")
        support_address = getattr(self, "support_address", None)
        if not isinstance(support_address, str) or not support_address.strip():
            raise EngineConflict("configured customer support delivery address is required for τ retail")
        prepared = adapter.prepare(
            run_id=str(run_id), generation=0, task_id=str(model.task_id),
            projection_as_of=now, customer_actor_id=str(customer["actor_id"]),
            support_actor_id=str(support_actor_id),
        )
        if prepared.run_id != str(run_id) or prepared.task_id != model.task_id:
            raise ValueError("problem adapter returned a mismatched run or task binding")
        case_id = f"{model.scenario_id}-{model.task_id}"
        instruction = ("Please respond to the customer's email using the company's existing customer support "
                       "and ERP records. Follow company policy and communicate with the customer by email.")
        visible = {"case_id": case_id, "scenario_id": model.scenario_id,
                   "status": "open", "title": f"Customer support: {model.task_id}",
                   "notes": ""}
        actors = []
        for configured in self.actors:
            private_state = deepcopy(configured.get("private_state", {}))
            private_state.setdefault("availability_calendar", None)
            private_state.setdefault("response_policy_version", "company-role-v1")
            if configured["actor_id"] == customer["actor_id"]:
                private_state.update(prepared.customer_private_state())
            actors.append({"actor_id": configured["actor_id"], "kind": configured.get("kind", "employee"),
                           "email": configured.get("email"), "private_state": private_state})
        manifest = {"case_id": case_id, "initial_time": now.isoformat(),
                    "agent_instruction": instruction,
                    "agent_configuration": _safe_agent_configuration(model.agent_configuration),
                    "agent_metadata": model.agent_metadata.model_dump(mode="json", exclude_none=True),
                    "environment": deepcopy(getattr(self, "environment_manifest", {})),
                    "prepared_problem": prepared.model_dump(mode="json")}
        return {"run": {"status": "running", "simulated_time": now.isoformat(), "paused": False,
                         "manifest": manifest, "erp_binding": binding},
                "cases": [{"case_id": case_id, "visible": visible}], "actors": actors, "events": []}

    def _capture_native_baseline(self, snapshot: dict[str, Any]) -> None:
        """Persist read-only native starting terms for later objective checks."""
        binding = snapshot["run"]["erp_binding"]
        documents = binding.get("documents", {})
        po_id = _document_id(documents.get("purchase_order"))
        if not po_id:
            return
        adapter = self.erp_factory(binding)
        order = adapter.get_purchase_order(po_id)
        supplier = next((actor for actor in snapshot["actors"] if actor["kind"] == "supplier"), None)
        if supplier is not None:
            ordered_quantity = sum((Decimal(str(line.get("qty", 0) or 0))
                                    for line in order.get("items", [])), Decimal(0))
            if ordered_quantity > 0:
                supplier["private_state"]["contract_quantity"] = (
                    int(ordered_quantity) if ordered_quantity == ordered_quantity.to_integral_value()
                    else str(ordered_quantity)
                )
        snapshot["run"]["manifest"]["native_baseline"] = {
            "purchase_order": {
                "name": _document_id(order), "modified": _native_version(order),
                "company": order.get("company"), "supplier": order.get("supplier"),
                "currency": order.get("currency"),
                "items": [{key: line.get(key) for key in (
                    "name", "item_code", "qty", "received_qty", "rate", "warehouse",
                    "uom", "stock_uom", "conversion_factor", "stock_qty")}
                          for line in order.get("items", [])],
            }
        }
        item_id = _document_id(documents.get("item"))
        warehouse_id = _document_id(documents.get("warehouse"))
        if item_id and warehouse_id:
            snapshot["run"]["manifest"]["native_baseline"]["stock_balance"] = {
                "item_code": item_id, "warehouse": warehouse_id,
                "actual_qty": str(adapter.get_stock_balance(item_id, warehouse_id)),
            }
        invoice_id = _document_id(documents.get("purchase_invoice"))
        if invoice_id:
            snapshot["run"]["manifest"]["native_baseline"]["purchase_invoice_assessment"] = adapter.assess_invoice(invoice_id)

    def _scenario_events(self, model: StartScenario, now: datetime, binding: dict[str, Any], case_id: str, run_id: UUID) -> list[dict[str, Any]]:
        if model.scenario_id not in {"B1", "D1", "S01"}:
            return []
        po = binding.get("documents", {}).get("purchase_order", {})
        order_id = _document_id(po)
        supplier_id = _document_id(binding.get("documents", {}).get("supplier"))
        item_id = _document_id(binding.get("documents", {}).get("item"))
        logistics = next((actor for actor in self.actors if actor.get("kind") == "logistics"), {})
        shipment_id = logistics.get("private_state", {}).get("shipment_id", "SHIP-001")
        if model.scenario_id == "B1":
            schedule = [
                (2, "SHIPMENT_DELAYED", "logistics", {"revised_eta_days": 5, "shipment_id": shipment_id}),
                (5, "DELIVERY_ARRIVED", "warehouse", {"quantity": 2000, "shipment_id": shipment_id}),
            ]
        elif model.scenario_id == "D1":
            schedule = [(2, "SUPPLIER_PRICING_CHANGED", "supplier", {"cost_increase": "5.00"})]
        else:
            schedule = [
                (5, "PRODUCTION_DEADLINE", "logistics", {
                    "item_code": item_id, "required_quantity": 10000,
                    "warehouse": _document_id(binding.get("documents", {}).get("warehouse")),
                }),
                (7, "SHIPMENT_DELAYED", "logistics", {"revised_eta_days": 14, "shipment_id": shipment_id}),
                (9, "CUSTOMER_REQUIREMENTS_CHANGED", "customer", {}),
                (11, "SUPPLIER_PRICING_CHANGED", "supplier", {"cost_increase": "5.00"}),
                (14, "DELIVERY_ARRIVED", "warehouse", {"quantity": 2000, "shipment_id": shipment_id}),
            ]
        events = []
        for day, event_type, source, payload in schedule:
            event_id = f"{model.scenario_id.lower()}-day-{day}-{event_type.lower()}"
            events.append({"event_id": event_id, "run_id": str(run_id),
                           "scenario_id": model.scenario_id, "simulated_time": now + timedelta(days=day-1),
                           "event_type": event_type,
                           "source": source,
                           "entity_type": ("shipment" if event_type in {"SHIPMENT_DELAYED", "DELIVERY_ARRIVED"}
                                           else "supplier" if event_type == "SUPPLIER_PRICING_CHANGED"
                                           else "purchase_order"),
                           "entity_id": (shipment_id if event_type in {"SHIPMENT_DELAYED", "DELIVERY_ARRIVED"}
                                         else supplier_id if event_type == "SUPPLIER_PRICING_CHANGED"
                                         else (order_id or case_id)),
                           "related_entities": [case_id], "payload": payload, "visibility": [source]})
        return events

    @_serialize_run
    def enqueue_event(self, model: CanonicalEvent, key: str) -> dict[str, Any]:
        key = self._key(key)
        run = self._run(model.run_id)
        def operation(connection):
            current = self._assert_running(connection, model.run_id, run["generation"])
            if model.scenario_id != current["scenario_id"]:
                raise EngineConflict("event scenario_id does not match its run")
            event = self.store.enqueue_event(model, run["generation"], connection=connection)
            self.store.append_audit(model.run_id, run["generation"], {"event": "canonical_event_enqueued", "event_id": model.event_id}, connection=connection)
            return event
        return self.store.execute_idempotent(self._scope(model.run_id, run["generation"], "controller", "event"),
                                             key, model.model_dump(mode="json"), operation)

    @_serialize_run
    def list_pending(self, run_id: UUID) -> list[dict[str, Any]]:
        run = self._run(run_id)
        return self.store.pending_events(run_id, run["generation"])

    @_serialize_run
    def pause(self, model: PauseClock, key: str) -> dict[str, Any]:
        key = self._key(key); run = self._run(model.run_id)
        def operation(conn):
            self._assert_running(conn, model.run_id, run["generation"])
            return self.store.update_run(model.run_id, run["generation"], paused=model.paused, connection=conn)
        return self.store.execute_idempotent(self._scope(model.run_id, run["generation"], "controller", "pause"),
                                             key, model.model_dump(mode="json"), operation)

    @_serialize_run
    def advance(self, model: AdvanceClock, key: str, *, company_tick: bool = False,
                company_event_id: str | None = None) -> dict[str, Any]:
        key = self._key(key); run = self._run(model.run_id)
        if self.store.is_company_experiment(model.run_id):
            company = self.store.get_company("company")
            if not company_tick:
                raise EngineConflict("company experiments advance on the shared company clock")
            if company is None or company["paused"]:
                raise EngineConflict("company clock is paused")
        self._schedule_policy_responses(run)
        def advance_tx(connection):
            current = self.store.get_run(model.run_id, connection=connection)
            current = self._assert_running(connection, model.run_id, current["generation"])
            if current["paused"]:
                raise EngineConflict("simulation clock is paused")
            before = self._time(current["simulated_time"])
            target = self._time(model.until)
            if target < before:
                raise EngineConflict("simulation clock cannot move backwards")
            pending = self.store.pending_events(model.run_id, current["generation"])
            clock_events = [{"event_id": event["event_id"], "simulated_time": event["simulated_time"],
                             "sequence": event["sequence"]} for event in pending]
            due_ids = ordered_due_events(before, target, clock_events)
            if company_event_id is not None:
                due_ids = [event_id for event_id in due_ids if event_id == company_event_id]
            # State transitions and event receipts are committed atomically;
            # external email submission happens only after the transaction.
            for event_id in due_ids:
                event = next(item for item in pending if item["event_id"] == event_id)
                if event["event_type"] == "EMPLOYEE_RESPONSE_READY":
                    actor_id = event.get("payload", {}).get("actor_id")
                    actor = (self.store.get_actor(model.run_id, current["generation"], actor_id,
                                                  connection=connection)
                             if actor_id else None)
                    if actor and event.get("source") == "mail_gateway":
                        # Inbound employee replies are handled by pump() outside
                        # the writer transaction, where the exact native message
                        # can be opened in that employee's browser first.
                        continue
                    if actor and (actor["kind"] == "approver"
                                  or actor["kind"] in {"customer", "supplier"}):
                        # Manager ERP reads and upstream actor turns occur
                        # outside this writer transaction. pump() owns them.
                        continue
                outcome = self._apply_event(model.run_id, current, event, connection)
                self.store.complete_event(model.run_id, current["generation"], event_id, outcome, connection=connection)
            return self.store.update_run(model.run_id, current["generation"], simulated_time=target, connection=connection)
        result = self.store.execute_idempotent(self._scope(model.run_id, run["generation"], "controller", "advance"),
                                               key, model.model_dump(mode="json"), advance_tx)
        self.pump(model.run_id, company_tick=company_tick, company_event_id=company_event_id)
        return result

    def _apply_event(self, run_id: UUID, run: dict[str, Any], event: dict[str, Any], connection: Any) -> dict[str, Any]:
        payload = event["payload"]
        if event["event_type"] == "EMPLOYEE_RESPONSE_READY":
            self._queue_actor_response(run_id, run, event, connection)
        elif event["event_type"] in {
            "SUPPLIER_PRICING_CHANGED", "SUPPLIER_MARKET_DEMAND",
            "SUPPLIER_STOCK_REPLENISHED", "SUPPLIER_SUPPLY_DISRUPTED",
            "SUPPLIER_DELIVERY_OUTCOME",
        }:
            supplier = self._actor_by_kind(run_id, run["generation"], "supplier")
            supplier_id = str(supplier["private_state"].get("supplier_id", ""))
            if event.get("entity_type") != "supplier" or str(event.get("entity_id")) != supplier_id:
                raise EngineConflict("supplier market event must target the configured supplier identity")
            state, market_outcome = apply_supplier_market_event(
                supplier["private_state"], event,
            )
            self.store.put_actor(run_id, run["generation"], supplier["actor_id"], kind=supplier["kind"], email=supplier["email"], private_state=state, connection=connection)
            self.store.append_audit(run_id, run["generation"], {
                "event": "supplier_market_state_updated", "event_id": event["event_id"],
                "supplier_actor_id": supplier["actor_id"], "outcome": market_outcome,
            }, connection=connection)
        elif event["event_type"] == "PAYMENT_POSTED":
            raise EngineConflict("PAYMENT_POSTED is not accepted until verified against ERPNext outside the event transaction")
        elif event["event_type"] == "CUSTOMER_REQUIREMENTS_CHANGED":
            customer = self._actor_by_kind(run_id, run["generation"], "customer")
            state = customer["private_state"]
            requested_date = state.get("requested_earlier_delivery_date")
            if not state.get("tau2_task") or not state.get("order_id") or not requested_date:
                raise EngineConflict("customer delivery-change task is missing its run-owned facts")
            body = (
                "Our delivery requirement has changed. We now need the order by "
                f"{requested_date}. Can you confirm whether the delivery plan can be moved forward?"
            )
            state["tau2_task"] = customers.add_initial_customer_message(state["tau2_task"], body)
            context = self.store.get_case(
                run_id, run["generation"], event["related_entities"][0], connection=connection
            )["visible"]
            message_id = _message_id(str(run_id), run["generation"], event["event_id"] + ":customer")
            self.store.enqueue_outbox(run_id, run["generation"], event["event_id"] + ":customer-message",
                {"from_actor_id": customer["actor_id"], "to_actor_id": run["agent_id"],
                 "subject": "Updated delivery requirement", "body": body,
                 "message_id": message_id, "case_id": context["case_id"], "in_reply_to": None,
                 "automated": True, "simulated_time": event["simulated_time"]},
                event_id=event["event_id"], actor_id=customer["actor_id"], connection=connection)
            self.store.append_audit(run_id, run["generation"], {
                "event": "customer_requirement_disclosed", "actor_id": customer["actor_id"],
                "message_id": message_id, "disclosed_facts": ["requested_earlier_delivery_date"],
            }, connection=connection)
            self.store.put_actor(run_id, run["generation"], customer["actor_id"], kind=customer["kind"],
                                 email=customer["email"], private_state=state, connection=connection)
        elif event["event_type"] == "PRODUCTION_DEADLINE":
            logistics = self._actor_by_kind(run_id, run["generation"], "logistics")
            required_quantity = int(event["payload"].get("required_quantity", 0))
            item_code = event["payload"].get("item_code", "the scheduled item")
            self._enqueue_actor_notice(run_id, run, logistics, event,
                "Production deadline is due",
                f"Production requires {required_quantity:,} units of {item_code} by {self._time(event['simulated_time']).date().isoformat()}. Please confirm the native warehouse balance and coordinate the delivery plan.",
                connection)
        elif event["event_type"] in {"SHIPMENT_DELAYED", "DELIVERY_ARRIVED"}:
            # Physical shipment state is coordinator truth, not an ERP write.
            for actor in self.store.list_actors(run_id, run["generation"], connection=connection):
                state = actor["private_state"]
                if actor["kind"] in {"logistics", "warehouse"} and state.get("shipment_id") == event["entity_id"]:
                    state["shipment_status"] = "delayed" if event["event_type"] == "SHIPMENT_DELAYED" else "arrived"
                    state.update(payload)
                    if actor["kind"] == "logistics" and event["event_type"] == "DELIVERY_ARRIVED":
                        state["location"] = _document_id(
                            run["erp_binding"].get("documents", {}).get("warehouse"))
                    self.store.put_actor(run_id, run["generation"], actor["actor_id"], kind=actor["kind"], email=actor["email"], private_state=state, connection=connection)
            case_id = event["related_entities"][0]
            if event["event_type"] == "SHIPMENT_DELAYED":
                logistics = self._actor_by_kind(run_id, run["generation"], "logistics")
                revised_eta = (self._time(event["simulated_time"]) +
                               timedelta(days=int(payload.get("revised_eta_days", 0)))).date().isoformat()
                self._enqueue_actor_notice(run_id, run, logistics, event, "Shipment delay and revised ETA",
                    f"Shipment {payload.get('shipment_id')} for case {case_id} is delayed. The revised ETA is {revised_eta}.",
                    connection)
            elif event["event_type"] == "DELIVERY_ARRIVED":
                warehouse = self._actor_by_kind(run_id, run["generation"], "warehouse")
                po_id = _document_id(run["erp_binding"].get("documents", {}).get("purchase_order"))
                self._enqueue_actor_notice(run_id, run, warehouse, event, "Shipment arrival confirmed",
                    f"Warehouse records confirm {int(payload.get('quantity', 0))} units arrived for purchase order {po_id}, case {case_id}.",
                    connection)
        self.store.append_audit(run_id, run["generation"], {"event": "event_processed", "event_id": event["event_id"], "event_type": event["event_type"]}, connection=connection)
        return {"processed": True, "event_type": event["event_type"]}

    def _enqueue_actor_notice(self, run_id: UUID, run: dict[str, Any], actor: dict[str, Any],
                              event: dict[str, Any], subject: str, body: str,
                              connection: Any) -> None:
        case_id = event["related_entities"][0]
        outbox_id = event["event_id"] + ":notice"
        message_id = _message_id(str(run_id), run["generation"], outbox_id)
        self.store.enqueue_outbox(run_id, run["generation"], outbox_id,
            {"from_actor_id": actor["actor_id"], "to_actor_id": run["agent_id"],
             "subject": subject, "body": body, "message_id": message_id,
             "case_id": case_id, "in_reply_to": None, "automated": True,
             "simulated_time": event["simulated_time"]},
            event_id=event["event_id"], actor_id=actor["actor_id"], connection=connection)
        self.store.append_audit(run_id, run["generation"], {
            "event": "scenario_notice_prepared", "actor_id": actor["actor_id"],
            "source_event_id": event["event_id"], "message_id": message_id,
        }, connection=connection)

    def _actor_by_kind(self, run_id: UUID, generation: int, kind: str) -> dict[str, Any]:
        matches = [a for a in self.store.list_actors(run_id, generation) if a["kind"] == kind]
        if not matches:
            raise StoreNotFound(f"actor kind {kind} is not configured for this run")
        return matches[0]

    def _actor_turn_request(self, run: dict[str, Any], actor: dict[str, Any],
                            event: dict[str, Any]) -> ActorTurnRequest:
        request = event["payload"]
        allowed_senders = {run["agent_id"]}
        if run["scenario_id"] == "TAU_RETAIL":
            allowed_senders.add(self._tau_retail_support_actor_id(run))
        if request.get("from_actor_id") not in allowed_senders:
            raise EngineConflict("upstream actor turns must answer a message from this run's agent or configured retail support actor")
        if not request.get("message_id") or not isinstance(request.get("question"), str):
            raise EngineConflict("upstream actor turn is missing its correlated message")
        context = deepcopy(request.get("visible_context") or {})
        context.setdefault("case_id", event["entity_id"])
        context["subject"] = request.get("subject", "")
        context["simulated_time"] = event["simulated_time"]
        private = actor["private_state"]
        if actor["kind"] == "customer":
            if run["scenario_id"] == "TAU_RETAIL":
                context["support_actor_id"] = private.get("support_actor_id")
            scenario = private.get("tau2_task")
            if not isinstance(scenario, dict):
                raise EngineConflict("customer actor is missing its native τ-bench Task")
            state = private.get("tau2_user_state")
        elif actor["kind"] == "supplier":
            grant = self._verified_expedite_grant(
                UUID(run["run_id"]), run["generation"], actor,
                request["question"], request.get("response_to_outbox_id"), event["entity_id"],
            )
            if grant is not None:
                context["expedite_authorization"] = grant
            po_id = _document_id(run["erp_binding"].get("documents", {}).get("purchase_order"))
            context.setdefault("purchase_order_id", po_id)
            context.setdefault("readable_unit_price", private.get("contract_unit_price"))
            context.setdefault("order_quantity", private.get("contract_quantity"))
            scenario = {"supplier_private_state": private}
            state = None
        else:
            raise EngineConflict("only customer and supplier turns use the upstream actor worker")
        return ActorTurnRequest(
            turn_id=f"{run['run_id']}:{run['generation']}:{event['event_id']}",
            run_id=run["run_id"], generation=run["generation"],
            actor_id=actor["actor_id"], actor_type=actor["kind"],
            scenario=scenario, state=state,
            inbound_message={
                "message_id": request["message_id"],
                "subject": request.get("subject", ""),
                "body": request["question"],
            },
            visible_context=context,
        )

    def _process_upstream_actor_event(self, run: dict[str, Any], event: dict[str, Any],
                                      actor: dict[str, Any]) -> None:
        """Call the isolated actor worker without holding SQLite's writer lock."""
        run_id = UUID(run["run_id"])
        generation = run["generation"]
        turn_id = f"{run['run_id']}:{generation}:{event['event_id']}"
        # A provider call may finish after the coordinator loses its response.
        # Its idempotency key must keep the exact original prompt/context even
        # if canonical actor state or the case changes before reconciliation.
        started = next((row["private_record"] for row in self.store.list_audit(run_id, generation)
            if row["private_record"].get("event") == "actor_turn_started"
            and row["private_record"].get("turn_id") == turn_id), None)
        if started is not None:
            snapshot = started.get("request_snapshot")
            if not isinstance(snapshot, dict):
                raise EngineConflict("persisted actor turn intent is missing its request snapshot")
            turn_request = ActorTurnRequest.model_validate(snapshot)
        else:
            turn_request = self._actor_turn_request(run, actor, event)
        request_hash = canonical_request_hash(turn_request.model_dump(mode="json"))
        with self.store.transaction() as connection:
            current = self.store.get_run(run_id, connection=connection)
            self._assert_running(connection, run_id, generation)
            still_pending = any(row["event_id"] == event["event_id"]
                                for row in self.store.pending_events(run_id, generation))
            if not still_pending:
                return
            already_recorded = any(
                row["private_record"].get("event") == "actor_turn_started"
                and row["private_record"].get("turn_id") == turn_request.turn_id
                for row in self.store.list_audit(run_id, generation)
            )
            if not already_recorded:
                self.store.append_audit(run_id, generation, {
                    "event": "actor_turn_started", "turn_id": turn_request.turn_id,
                    "actor_id": actor["actor_id"], "actor_type": actor["kind"],
                    "event_id": event["event_id"], "request_sha256": request_hash,
                    "request_snapshot": turn_request.model_dump(mode="json"),
                }, connection=connection)
        if self.actor_turn_client is None:
            raise ActorTurnUncertain(f"{actor['kind']} actor worker is not configured")
        try:
            outcome = self.actor_turn_client.turn(turn_request)
        except ActorWorkerError as exc:
            raise ActorTurnUncertain(
                f"{actor['kind']} actor turn remains pending ({exc.code})"
            ) from exc
        if outcome.status != "completed" or not outcome.body:
            raise ActorTurnUncertain(f"{actor['kind']} actor turn remains pending")
        if actor["kind"] == "customer" and outcome.state is None:
            raise ActorTurnUncertain("τ-bench customer worker returned no persisted UserState")

        with self.store.transaction() as connection:
            current = self.store.get_run(run_id, connection=connection)
            self._assert_running(connection, run_id, generation)
            pending = {row["event_id"]: row for row in self.store.pending_events(run_id, generation)}
            latest_event = pending.get(event["event_id"])
            if latest_event is None:
                return
            latest_actor = self.store.get_actor(
                run_id, generation, actor["actor_id"], connection=connection,
            )
            private = latest_actor["private_state"]
            if latest_actor["kind"] == "customer":
                private["tau2_user_state"] = outcome.state
            elif outcome.supplier_private_state_patch is not None:
                # The worker cannot replace supplier economics, policy, or
                # coordinator event facts. Only the kernel conversation state
                # is mutable by the supplier model.
                private["kernel_state"] = outcome.supplier_private_state_patch.kernel_state.model_dump(mode="json")
            self.store.put_actor(
                run_id, generation, latest_actor["actor_id"], kind=latest_actor["kind"],
                email=latest_actor["email"], private_state=private, connection=connection,
            )
            self._queue_actor_response(
                run_id, current, latest_event, connection,
                prepared_response=outcome, turn_id=turn_request.turn_id,
            )
            self.store.complete_event(run_id, generation, event["event_id"], {
                "processed": True, "event_type": event["event_type"],
                "actor_turn_id": turn_request.turn_id,
            }, connection=connection)
            self.store.append_audit(run_id, generation, {
                "event": "event_processed", "event_id": event["event_id"],
                "event_type": event["event_type"], "actor_turn_id": turn_request.turn_id,
            }, connection=connection)
            if outcome.usage is not None:
                usage = outcome.usage.model_dump(mode="json", exclude_none=True)
                if usage.get("model_id") and any(
                    usage.get(field) is not None
                    for field in ("input_tokens", "output_tokens", "cost")
                ):
                    usage_id = str(uuid5(NAMESPACE_URL, f"{turn_request.turn_id}:usage"))
                    self.store.append_audit(run_id, generation, {
                        "event": "model_usage_recorded", "usage_id": usage_id,
                        "usage_scope": "environment", "actor_id": actor["actor_id"], "turn_id": turn_request.turn_id,
                        **usage,
                    }, connection=connection)

    def _queue_actor_response(self, run_id: UUID, run: dict[str, Any], event: dict[str, Any],
                              connection: Any, *, prepared_response: ActorTurnResponse | None = None,
                              turn_id: str | None = None) -> None:
        request = event["payload"]
        actor = self.store.get_actor(run_id, run["generation"], request["actor_id"], connection=connection)
        private = actor["private_state"]
        context = request.get("visible_context", {})
        context.setdefault("case_id", event["entity_id"])
        if actor["kind"] == "supplier":
            expedite_grant = self._verified_expedite_grant(run_id, run["generation"], actor,
                request.get("question", ""), request.get("response_to_outbox_id"), event["entity_id"])
            if expedite_grant is not None:
                context["expedite_authorization"] = expedite_grant
            context.setdefault("purchase_order_id", context.get("purchase_order_id") or event["entity_id"])
            context.setdefault("readable_unit_price", private.get("contract_unit_price", "50"))
            context.setdefault("order_quantity", private.get("contract_quantity", 0))
            context.setdefault("interaction_id", event["event_id"])
        if actor["kind"] in {"customer", "supplier"}:
            if prepared_response is None:
                raise EngineConflict("customer and supplier responses require a completed upstream actor turn")
            response = prepared_response.model_dump(mode="json")
        else:
            event_time = self._time(event["simulated_time"])
            elapsed = event_time - self._time(run["manifest"].get("initial_time", run["simulated_time"]))
            response = self._employee_response(actor["kind"], request["question"], private, context,
                                              day=int(elapsed.total_seconds() // 86400) + 1)
        message_id = _message_id(str(run_id), run["generation"], event["event_id"])
        self.store.enqueue_outbox(run_id, run["generation"], event["event_id"] + ":reply",
            {"from_actor_id": actor["actor_id"], "to_actor_id": request["from_actor_id"],
             "subject": f"Re: {request['subject']}", "body": response["body"], "message_id": message_id,
             "case_id": event["entity_id"], "in_reply_to": request["message_id"], "automated": True,
             "simulated_time": event["simulated_time"]}, event_id=event["event_id"], actor_id=actor["actor_id"], connection=connection)
        self.store.append_audit(run_id, run["generation"], {
            "event": "actor_response_prepared", "actor_id": actor["actor_id"],
            "request_event_id": event["event_id"],
            "request_outbox_id": request.get("response_to_outbox_id"),
            "actor_turn_id": turn_id,
            "conversation_pointer": response.get("conversation_pointer"),
            "disclosed_facts": response.get("disclosed_facts", []),
            "required_actions": response.get("required_actions", []),
            "quote": response.get("quote"),
        }, connection=connection)
        quote = response.get("quote")
        if (actor["kind"] == "supplier" and expedite_grant is not None
                and isinstance(quote, dict) and quote.get("type") == "expedited_shipping"
                and quote.get("status") == "accepted"
                and quote.get("approval_id") == expedite_grant["approval_id"]
                and quote.get("quote_id") == expedite_grant["quote_id"]):
            self.store.append_audit(run_id, run["generation"], {
                "event": "expedited_shipping_commitment", "actor_id": actor["actor_id"],
                "approval_id": expedite_grant["approval_id"], "quote_id": quote["quote_id"],
                "shipment_id": quote["shipment_id"], "fee_amount": quote["fee_amount"],
                "fee_currency": quote["fee_currency"],
                "delivery_commitment": quote["delivery_commitment"],
                "service": quote.get("service"),
                "service_purchase_order_id": expedite_grant["service_purchase_order_id"],
                "request_outbox_id": request.get("response_to_outbox_id"),
                "response_message_id": message_id, "simulated_time": event["simulated_time"],
            }, connection=connection)

    def _verified_expedite_grant(self, run_id: UUID, generation: int, supplier: dict[str, Any],
                                 question: str, request_outbox_id: str | None,
                                 case_id: str) -> dict[str, Any] | None:
        match = re.search(r"\bapproval reference:\s*([0-9a-f-]{36})\b", question, re.IGNORECASE)
        if match is None or request_outbox_id is None:
            return None
        records = [row["private_record"] for row in self.store.list_audit(run_id, generation)]
        grant = next((record for record in records if record.get("event") == "approval_decision"
            and record.get("approval_id") == match.group(1)
            and record.get("decision") == "approved" and record.get("approved_by") == "manager"
            and record.get("origin") == "simulation_policy"
            and record.get("operation") == "approve_expedited_shipping"
            and record.get("case_id") == case_id), None)
        if grant is None:
            return None
        native_commitment = next((record for record in reversed(records)
            if record.get("event") == "approval_action_completed"
            and record.get("operation") == "commit_expedited_shipping"
            and record.get("approval_id") == grant.get("approval_id")), None)
        if native_commitment is None:
            return None
        committed_result = native_commitment.get("result", {})
        if (committed_result.get("quote_id") != grant.get("entity_version")
                or committed_result.get("shipment_id") != grant.get("entity_id")
                or Decimal(str(committed_result.get("fee_amount", "NaN"))) != Decimal(str(grant.get("amount", "-1")))
                or committed_result.get("fee_currency") != grant.get("currency")
                or committed_result.get("delivery_commitment") != grant.get("delivery_commitment")):
            return None
        request_item = next((item for item in self.store.list_outbox(run_id, generation, status="completed")
            if item["outbox_id"] == request_outbox_id
            and item["private_payload"].get("from_actor_id") == self._run(run_id)["agent_id"]
            and item["private_payload"].get("to_actor_id") == supplier["actor_id"]
            and item["private_payload"].get("case_id") == case_id), None)
        if request_item is None:
            return None
        proposal = {
            "approval_id": grant["approval_id"], "quote_id": grant["entity_version"],
            "shipment_id": grant["entity_id"], "fee_amount": grant["amount"],
            "fee_currency": grant["currency"], "delivery_commitment": grant["delivery_commitment"],
            "service": grant.get("service"), "service_purchase_order_id": committed_result.get("purchase_order_id"),
        }
        if not all(str(value) in question for value in (
                proposal["approval_id"], proposal["quote_id"], proposal["shipment_id"],
                proposal["fee_amount"], proposal["fee_currency"], proposal["delivery_commitment"])):
            return None
        if proposal["quote_id"] not in str(self.store.get_case(run_id, generation, case_id)["visible"].get("notes", "")):
            return None
        return proposal

    @staticmethod
    def _employee_response(kind: str, question: str, private: dict[str, Any], context: dict[str, Any], *, day: int) -> dict[str, Any]:
        if kind == "logistics" and any(word in question.casefold() for word in ("where", "location", "shipment", "dispatch")):
            if day < int(private.get("location_reveal_day", 4)):
                return {"body": "I am still checking the distribution records and do not have a confirmed location yet.", "disclosed_facts": []}
            return {"body": f"The logistics record for shipment {private['shipment_id']} shows {private['quantity']} units at {private['location']}.", "disclosed_facts": ["shipment_location"]}
        if kind == "warehouse":
            return {"body": f"The warehouse record for order {context.get('purchase_order_id', '')} confirms {private.get('received_quantity', 'the recorded')} units received.", "disclosed_facts": ["received_quantity"]}
        if kind == "employee":
            return {"body": "Thank you, I have received your update.", "disclosed_facts": []}
        return {"body": "I am checking the relevant business records. Please clarify which order or delivery record you need.", "disclosed_facts": []}

    def _validate_coffee_related_document(self, related_document, recipient_actor_id: str) -> None:
        """Require an exact persisted Coffee source mapping for linked email."""
        if related_document is None:
            return
        reference = related_document.model_dump(mode="json")
        with self.store.transaction() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_record_mappings'"
            ).fetchone()
            if not table:
                raise ValueError("Related CoffeeBench document has no registered company source mapping")
            rows = [
                json.loads(row["mapping_json"])
                for row in db.execute(
                    "SELECT mapping_json FROM company_record_mappings WHERE doctype=? AND name=?",
                    (reference["doctype"], reference["name"]),
                )
            ]
        if len(rows) != 1:
            raise ValueError("Related email document must identify exactly one mapped company record")
        mapping = rows[0]
        email_route = (mapping.get("native_context") or {}).get("email_reply")
        if (
            mapping.get("benchmark") != "coffeebench"
            or mapping.get("company_id") != "company"
            or mapping.get("erp_doctype") != reference["doctype"]
            or mapping.get("erp_name") != reference["name"]
            or not isinstance(email_route, dict)
            or recipient_actor_id not in email_route.get("allowed_recipient_actor_ids", [])
        ):
            raise ValueError("Related email document is not mapped to this CoffeeBench recipient")

    @_serialize_run
    def send_message(self, model: SendMessage, key: str, sender_id: str) -> dict[str, Any]:
        key = self._key(key); run = self._run(model.run_id)
        sender = self._member(model.run_id, sender_id)
        recipient = self._member(model.run_id, model.to_actor_id)
        case = self.store.get_case(model.run_id, run["generation"], model.case_id)
        self._validate_coffee_related_document(model.related_document, recipient["actor_id"])
        outbox_id = str(uuid5(NAMESPACE_URL, f"{model.run_id}:{run['generation']}:{sender_id}:message:{key}"))
        message_id = _message_id(str(model.run_id), run["generation"], outbox_id)
        in_reply_to = model.in_reply_to
        if in_reply_to:
            prior_message = next((
                item["private_payload"]
                for item in self.store.list_outbox(
                    model.run_id, run["generation"], status="completed"
                )
                if item["private_payload"].get("message_id") == in_reply_to
            ), None)
            if (
                prior_message is None
                or prior_message.get("to_actor_id") != recipient["actor_id"]
                or prior_message.get("related_document")
                != (model.related_document.model_dump(mode="json") if model.related_document else None)
            ):
                raise ValueError("Email reply must match a completed message for the same mapped document and recipient")
        if run["scenario_id"] == "TAU_RETAIL" and recipient["kind"] == "customer":
            support_actor_id = self._tau_retail_support_actor_id(run)
            seed = next((item for item in self.store.list_outbox(model.run_id, run["generation"], status="completed")
                         if item["private_payload"].get("from_actor_id") == recipient["actor_id"]
                         and item["private_payload"].get("to_actor_id") == support_actor_id), None)
            if seed is not None and in_reply_to is None:
                in_reply_to = seed["private_payload"].get("message_id")
        private_payload = {"from_actor_id": sender_id, "to_actor_id": model.to_actor_id,
                           "subject": model.subject, "body": model.body,
                           "message_id": message_id, "case_id": model.case_id,
                           "in_reply_to": in_reply_to, "automated": False,
                           "related_document": model.related_document.model_dump(mode="json")
                           if model.related_document else None,
                           "simulated_time": run["simulated_time"]}
        request = model.model_dump(mode="json") | {"sender_id": sender_id}
        def reserve(connection):
            self._assert_running(connection, model.run_id, run["generation"])
            self.store.get_case(model.run_id, run["generation"], model.case_id, connection=connection)
            item = self.store.enqueue_outbox(model.run_id, run["generation"], outbox_id, private_payload,
                                             actor_id=sender_id, connection=connection)
            self.store.append_audit(model.run_id, run["generation"], {"event": "message_queued", "outbox_id": outbox_id, "case_id": case["case_id"], "sender_actor_id": sender_id, "recipient_actor_id": recipient["actor_id"]}, connection=connection)
            return item
        item = self.store.execute_idempotent(self._scope(model.run_id, run["generation"], sender_id, "send"),
                                             key, request, reserve)
        # Idempotency stores the reservation result; a later worker may have
        # reconciled this outbox while the caller was disconnected. Always
        # dispatch from the authoritative current row, never the cached snapshot.
        item = self.store.get_outbox(model.run_id, run["generation"], outbox_id)
        result = self._deliver_outbox(run, item)
        self._schedule_policy_responses(run)
        return result

    def _deliver_outbox(self, run: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        if item["status"] == "completed":
            return item["outcome"]
        payload = item["private_payload"]
        sender = self._member(run["run_id"], payload["from_actor_id"])
        recipient = self._member(run["run_id"], payload["to_actor_id"])
        sender_ref = self._mail_actor(sender); recipient_ref = self._mail_actor(recipient)
        if item["submission_started"] or item["status"] == "uncertain":
            receipt = self.mail.find_receipt(recipient_ref, payload["message_id"], payload["subject"], run["run_id"], run["generation"])
            if receipt is None:
                raise MailDeliveryUncertain("message submission is uncertain; native receipt was not found")
        else:
            def started():
                self.store.mark_outbox_submission_started(run["run_id"], run["generation"], item["outbox_id"])
            try:
                receipt = self.mail.send(sender_ref, recipient_ref, payload["subject"], payload["body"],
                    payload["message_id"], run["run_id"], run["generation"],
                    in_reply_to=payload.get("in_reply_to"), automated=payload.get("automated", False),
                    **({"delivery_address": payload["delivery_address"]}
                       if payload.get("delivery_address") else {}),
                    on_submission_started=started)
            except Exception as exc:
                current = self.store.get_outbox(run["run_id"], run["generation"], item["outbox_id"])
                try:
                    self.store.mark_outbox_failure(run["run_id"], run["generation"], item["outbox_id"],
                        error_type=type(exc).__name__[:100], pre_send=not current["submission_started"])
                except Exception as report_error:
                    exc.add_note(f"outbox failure record also failed: {type(report_error).__name__}")
                if current["submission_started"]:
                    raise MailDeliveryUncertain("message submission crossed SMTP boundary without a confirmed receipt") from exc
                raise
        completed = self.store.complete_outbox(run["run_id"], run["generation"], item["outbox_id"], receipt)
        return completed["outcome"]

    @_serialize_run
    def pump(self, run_id: UUID | str, *, company_tick: bool = False,
             company_event_id: str | None = None) -> dict[str, Any]:
        run = self._run(run_id)
        if self.store.is_company_experiment(run_id):
            company = self.store.get_company("company")
            if company is None or company["paused"]:
                return {"delivered": [], "uncertain": [], "inbound_messages": 0}
        self._schedule_policy_responses(run)
        delivered, uncertain = [], []
        polled = []
        for actor in self.store.list_actors(run_id, run["generation"]):
            if actor["kind"] in {"agent", "controller"} or not actor.get("email"):
                continue
            polled.extend(self._poll_actor_mail(run, actor))
        self._process_due_events(run, company_event_id=company_event_id if company_tick else None)
        run = self._run(run_id)
        for item in self.store.pending_outbox(run_id, run["generation"]):
            try:
                self._deliver_outbox(run, item)
                delivered.append(item["outbox_id"])
            except MailDeliveryUncertain:
                persisted = self.store.get_outbox(run_id, run["generation"], item["outbox_id"])
                uncertain.append({"outbox_id": item["outbox_id"], "error_type": persisted["error_type"]})
            except Exception:
                # Provider errors are recorded by the outbox owner and remain
                # observable; stop here rather than marking a false success.
                raise
        return {"delivered": delivered, "uncertain": uncertain, "inbound_messages": len(polled)}

    def _process_due_events(self, run: dict[str, Any], *, company_event_id: str | None = None) -> None:
        current = self.store.get_run(run["run_id"])
        now = self._time(current["simulated_time"])
        pending = self.store.pending_events(run["run_id"], current["generation"])
        clock_events = [{"event_id": event["event_id"], "simulated_time": event["simulated_time"],
                         "sequence": event["sequence"]} for event in pending]
        for event_id in ordered_due_events(now, now, clock_events):
            if company_event_id is not None and event_id != company_event_id:
                continue
            event = next(item for item in pending if item["event_id"] == event_id)
            actor_id = event.get("payload", {}).get("actor_id")
            if event["event_type"] == "EMPLOYEE_RESPONSE_READY" and actor_id:
                actor = self.store.get_actor(run["run_id"], current["generation"], actor_id)
            else:
                actor = None
            if actor is not None and actor["kind"] == "approver":
                # Native ERP reads for the policy decision happen before opening
                # the writer transaction; the committed decision and email are
                # then persisted together with the event receipt.
                response, decision = self._manager_decision(current, actor, event)
                with self.store.transaction() as connection:
                    latest = self.store.get_run(run["run_id"], connection=connection)
                    self._assert_running(connection, run["run_id"], latest["generation"])
                    self._queue_manager_response(run["run_id"], current, actor, event,
                                                 response, decision, connection)
                    self.store.complete_event(run["run_id"], current["generation"], event_id,
                        {"processed": True, "decision": decision["decision"]}, connection=connection)
                    self.store.append_audit(run["run_id"], current["generation"],
                        {"event": "event_processed", "event_id": event_id,
                         "event_type": event["event_type"]}, connection=connection)
                continue
            if actor is not None and actor["kind"] in {"customer", "supplier"}:
                self._process_upstream_actor_event(current, event, actor)
                continue
            if (actor is not None
                    and event["event_type"] == "EMPLOYEE_RESPONSE_READY"
                    and event.get("source") == "mail_gateway"):
                # This event represents a received message being handled, not
                # background arrival. Let the employee browser display that
                # exact native message before committing the response.
                self.mail.open_employee_message_for_response(
                    self._mail_actor(actor), event["payload"]["message_id"],
                )
            with self.store.transaction() as connection:
                current = self.store.get_run(run["run_id"], connection=connection)
                event = next(item for item in pending if item["event_id"] == event_id)
                outcome = self._apply_event(UUID(run["run_id"]), current, event, connection)
                self.store.complete_event(run["run_id"], current["generation"], event_id,
                                          outcome, connection=connection)

    def _schedule_policy_responses(self, run: dict[str, Any]) -> None:
        actors = {actor["actor_id"]: actor for actor in self.store.list_actors(run["run_id"], run["generation"])}
        existing = {event["event_id"] for event in self.store.list_events(run["run_id"], run["generation"])}
        policy = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)
        for item in self.store.list_outbox(run["run_id"], run["generation"], status="completed"):
            payload = item["private_payload"]
            if payload.get("from_actor_id") != run["agent_id"]:
                continue
            actor = actors.get(payload.get("to_actor_id"))
            if actor is None or actor["kind"] == "agent":
                continue
            if actor["kind"] == "approver" and actor["actor_id"] != "manager":
                continue
            if actor["kind"] not in {"approver", "supplier", "customer", "warehouse", "logistics", "employee"}:
                continue
            event_id = "actor-response-" + sha256(item["outbox_id"].encode()).hexdigest()[:24]
            if event_id in existing:
                continue
            when = self._time(payload.get("simulated_time", run["simulated_time"]))
            if actor["actor_id"] == "manager":
                due = when + timedelta(hours=int(policy["manager_response_delay_hours"]))
            else:
                initial = self._time(run["manifest"].get("initial_time", run["simulated_time"]))
                due = max(when + timedelta(hours=float(actor["private_state"].get("response_delay_hours", 0))),
                          initial + timedelta(days=int(actor["private_state"].get("available_day", 1)) - 1))
            case_id = payload.get("case_id")
            event = CanonicalEvent(event_id=event_id, run_id=UUID(run["run_id"]),
                scenario_id=run["scenario_id"],
                simulated_time=due,
                event_type="EMPLOYEE_RESPONSE_READY", source="simulation_policy", entity_type="case",
                entity_id=case_id or run["manifest"]["case_id"], related_entities=[case_id or run["manifest"]["case_id"]],
                payload={"actor_id": actor["actor_id"], "approval_request_outbox_id": item["outbox_id"] if actor["actor_id"] == "manager" else None,
                         "response_to_outbox_id": item["outbox_id"],
                         "from_actor_id": run["agent_id"], "subject": payload.get("subject", "Approval request"),
                         "message_id": payload.get("message_id"), "question": payload.get("body", ""),
                         "visible_context": self.store.get_case(run["run_id"], run["generation"],
                             case_id or run["manifest"]["case_id"])["visible"]}, visibility=[actor["kind"]])
            self._enqueue_actor_event(event, run, actor)

    def _enqueue_actor_event(self, event: CanonicalEvent, run: dict[str, Any],
                             actor: dict[str, Any], connection=None) -> None:
        """Apply the actor's working calendar and persist any escalation notice."""
        if connection is None:
            with self.store.transaction() as transaction:
                self._enqueue_actor_event(event, run, actor, transaction)
            return
        requested_time = event.simulated_time
        due = response_time(actor["private_state"], requested_time)
        self.store.enqueue_event(event.model_copy(update={"simulated_time": due}),
                                 run["generation"], connection=connection)
        if due == requested_time:
            return
        self.store.append_audit(run["run_id"], run["generation"], {
            "event": "actor_response_calendar_applied", "actor_id": actor["actor_id"],
            "event_id": event.event_id, "requested_time": requested_time.isoformat(),
            "available_time": due.isoformat(),
        }, connection=connection)
        escalation_id = actor["private_state"].get("escalation_actor_id")
        if escalation_id is None:
            return
        escalation = self.store.get_actor(run["run_id"], run["generation"],
                                          escalation_id, connection=connection)
        if not escalation.get("email"):
            raise EngineConflict("configured escalation contact has no company mailbox")
        outbox_id = event.event_id + ":availability"
        self.store.enqueue_outbox(run["run_id"], run["generation"], outbox_id, {
            "from_actor_id": actor["actor_id"], "to_actor_id": run["agent_id"],
            "subject": "Availability and escalation contact",
            "body": (f"I am next available at {due.isoformat()}. For urgent matters, "
                     f"please contact {escalation['email']}. This notice does not authorize any action."),
            "case_id": event.entity_id,
            "message_id": _message_id(str(run["run_id"]), run["generation"], outbox_id),
            "in_reply_to": event.payload.get("message_id"), "automated": True,
            "simulated_time": run["simulated_time"],
        }, event_id=event.event_id, actor_id=actor["actor_id"], connection=connection)
        self.store.append_audit(run["run_id"], run["generation"], {
            "event": "escalation_contact_notified", "actor_id": actor["actor_id"],
            "escalation_actor_id": escalation_id, "event_id": event.event_id,
        }, connection=connection)

    def _manager_decision(self, run: dict[str, Any], manager: dict[str, Any],
                          event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Evaluate an agent's structured request against scenario facts and ERP."""
        outbox_id = event["payload"].get("approval_request_outbox_id")
        inbound_message_id = event["payload"].get("inbound_message_id")
        if event["payload"].get("request_source") == "inbound_email":
            request_item = {"status": "completed", "outbox_id": f"email:{inbound_message_id}",
                "private_payload": {"body": event["payload"].get("question", ""),
                    "case_id": event["entity_id"], "from_actor_id": run["agent_id"],
                    "to_actor_id": manager["actor_id"]}}
        else:
            request_item = self.store.get_outbox(run["run_id"], run["generation"], outbox_id)
        request = request_item["private_payload"]
        policy = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)
        fields, error = _parse_approval_request(request.get("body", ""))
        decision = "denied"
        reason = error or "The request does not meet the scenario's finance approval policy."
        approval_id = None
        evidence: dict[str, Any] = {}
        if (request_item["status"] == "completed"
                and request.get("from_actor_id") == run["agent_id"]
                and request.get("to_actor_id") == manager["actor_id"]
                and request.get("case_id") == event["entity_id"]
                and fields is not None and fields.get("case_id") == event["entity_id"]):
            try:
                approval = self._evaluate_manager_request(run, manager, event, request, fields, policy)
            except EngineConflict as exc:
                reason = str(exc)
            else:
                approval_id = str(uuid5(NAMESPACE_URL,
                    f"{run['run_id']}:{run['generation']}:{manager['actor_id']}:{request_item['outbox_id']}:{approval['operation']}:{approval['entity_id']}:{approval['entity_version']}"))
                decision = "approved"
                reason = "The request meets the recorded scenario finance policy."
                evidence = approval
        response = {"approval_id": approval_id, "decision": decision, "reason": reason,
                    "operation": fields.get("operation") if fields else None,
                    "entity_id": fields.get("entity_id") if fields else None}
        body = (f"Finance decision: {decision.upper()}. {reason}")
        if approval_id:
            body += f" Approval reference: {approval_id}."
        audit = {"event": "approval_decision", "approval_id": approval_id,
                 "decision": decision, "approved_by": manager["actor_id"],
                 "origin": "simulation_policy", "policy_version": policy["version"],
                 "request_outbox_id": outbox_id,
                 "inbound_message_id": inbound_message_id,
                 "request_id": request_item["outbox_id"],
                 "case_id": request.get("case_id"), "simulated_time": event["simulated_time"], **evidence}
        return {"body": body, "disclosed_facts": []}, {**audit, **response}

    def _evaluate_manager_request(self, run: dict[str, Any], manager: dict[str, Any],
                                  event: dict[str, Any], request: dict[str, Any],
                                  fields: dict[str, str], policy: dict[str, Any]) -> dict[str, Any]:
        operation = fields["operation"]
        entity_id = fields["entity_id"]
        entity_version = fields["entity_version"]
        case = self.store.get_case(run["run_id"], run["generation"], event["entity_id"])["visible"]
        adapter = self._erp(run)
        docs = run["erp_binding"].get("documents", {})
        if operation == "create_goods_receipt":
            order_id = _document_id(docs.get("purchase_order"))
            order = adapter.get_purchase_order(order_id)
            if entity_id != order_id or case.get("purchase_order_id") != order_id:
                raise EngineConflict("goods receipt must target this case's bound purchase order")
            if _native_version(order) != entity_version:
                raise EngineConflict("purchase order version is stale or unavailable")
            quantity = int(fields.get("quantity", "-1"))
            policy_quantity = int(policy["receipt_quantity"])
            items = order.get("items", [])
            received = sum((Decimal(str(item.get("received_qty", 0) or 0)) for item in items), Decimal(0))
            outstanding = sum((Decimal(str(item.get("qty", 0) or 0)) - Decimal(str(item.get("received_qty", 0) or 0)) for item in items), Decimal(0))
            warehouse = self._actor_by_kind(run["run_id"], run["generation"], "warehouse")
            shipment_id = warehouse["private_state"].get("shipment_id")
            arrived = any(row["event_type"] == "DELIVERY_ARRIVED" and row["status"] == "completed"
                and row["source"] == "warehouse" and row["entity_id"] == shipment_id
                and int(row["payload"].get("quantity", -1)) == policy_quantity
                and self._time(row["simulated_time"]) <= self._time(event["simulated_time"])
                for row in self.store.list_events(run["run_id"], run["generation"]))
            if not arrived or quantity != policy_quantity or received != Decimal("8000") or outstanding != Decimal(policy_quantity):
                raise EngineConflict("physical arrival and the exact remaining purchase order quantity are not verified")
            arrival_event = next(row for row in self.store.list_events(run["run_id"], run["generation"])
                if row["event_type"] == "DELIVERY_ARRIVED" and row["status"] == "completed"
                and row["source"] == "warehouse" and row["entity_id"] == shipment_id
                and int(row["payload"].get("quantity", -1)) == policy_quantity
                and self._time(row["simulated_time"]) <= self._time(event["simulated_time"]))
            return {"operation": operation, "entity_id": entity_id, "entity_version": entity_version,
                    "quantity": quantity, "currency": None,
                    "arrival_time": self._time(arrival_event["simulated_time"]).astimezone(timezone.utc).isoformat()}

        if operation == "post_payment":
            invoice_id = _document_id(docs.get("purchase_invoice"))
            invoice = adapter.get_purchase_invoice(invoice_id)
            order_id = _document_id(docs.get("purchase_order"))
            order = adapter.get_purchase_order(order_id)
            if entity_id != invoice_id or case.get("purchase_invoice_id") != invoice_id or case.get("purchase_order_id") != order_id:
                raise EngineConflict("payment must target the linked purchase invoice and purchase order")
            if _native_version(invoice) != entity_version:
                raise EngineConflict("purchase invoice version is stale or unavailable")
            assessment = adapter.assess_invoice(invoice_id)
            billed = sum((Decimal(str(line.get("billed_qty", line.get("qty", 0)) or 0)) for line in assessment["lines"]), Decimal(0))
            received = sum((Decimal(str(item.get("received_qty", 0) or 0)) for item in order.get("items", [])), Decimal(0))
            finding = case.get("invoice_finding") or {}
            if (billed != Decimal(policy["payment_billed_quantity"])
                    or received != Decimal(policy["payment_received_quantity"]) or received != billed
                    or any(Decimal(str(line.get("unreceived_billed_qty", 0) or 0)) != 0 for line in assessment["lines"])
                    or not finding or Decimal(str(finding.get("quantity_difference", -1))) != 0
                    or case.get("status") not in {"in_review", "closed"}
                    or adapter.read_payment_status(invoice_id).get("payments")):
                raise EngineConflict("10,000 billed units, matching posted receipts, a resolved case discrepancy and no prior payment are required")
            amount = Decimal(fields.get("amount", "NaN"))
            currency = fields.get("currency")
            expected_amount = Decimal(str(invoice.get("outstanding_amount", invoice.get("grand_total", "NaN"))))
            expected_currency = invoice.get("currency")
            if not expected_amount.is_finite() or amount != expected_amount or not expected_currency or currency != expected_currency:
                raise EngineConflict("payment amount and currency must match the native invoice balance")
            return {"operation": operation, "entity_id": entity_id, "entity_version": entity_version,
                    "amount": str(amount), "currency": currency}

        if operation == "approve_expedited_shipping":
            quote_message_id = fields.get("quote_message_id")
            supplier_ids = {actor["actor_id"] for actor in self.store.list_actors(run["run_id"], run["generation"])
                            if actor["kind"] == "supplier"}
            quote_item = next((item for item in self.store.list_outbox(run["run_id"], run["generation"], status="completed")
                if item["private_payload"].get("message_id") == quote_message_id
                and item["private_payload"].get("from_actor_id") in supplier_ids
                and item["private_payload"].get("to_actor_id") == run["agent_id"]
                and item["private_payload"].get("case_id") == event["entity_id"]), None)
            if quote_item is None:
                raise EngineConflict("the shipping proposal must be a confirmed supplier email for this case")
            quote_event_id = quote_item.get("event_id", "").removesuffix(":reply")
            proposal = next((entry["private_record"].get("quote") for entry in self.store.list_audit(run["run_id"], run["generation"])
                if entry["private_record"].get("event") == "actor_response_prepared"
                and entry["private_record"].get("request_event_id") == quote_event_id), None)
            if (not proposal or proposal.get("type") != "expedited_shipping"
                    or proposal.get("authorization_required") is not True):
                raise EngineConflict("the referenced supplier email has no pending expedited-shipping proposal")
            read_ids = {entry["private_record"].get("message_id") for entry in self.store.list_audit(run["run_id"], run["generation"])
                        if entry["private_record"].get("event") == "agent_inbox_read"
                        and entry["private_record"].get("actor_id") == run["agent_id"]}
            if quote_message_id not in read_ids or quote_message_id not in str(case.get("notes", "")):
                raise EngineConflict("the supplier proposal must be read and linked to the case before authorization")
            fee = Decimal(str(proposal.get("fee_amount", "NaN")))
            currency = proposal.get("fee_currency")
            try:
                requested_delivery = self._time(fields["delivery_commitment"] + "T00:00:00+00:00").date().isoformat()
            except (KeyError, ValueError):
                raise EngineConflict("the proposed delivery date must be included in the authorization request") from None
            if (entity_id != proposal.get("shipment_id") or entity_version != proposal.get("quote_id")
                    or Decimal(fields.get("amount", "NaN")) != fee
                    or fields.get("currency") != currency
                    or requested_delivery != proposal.get("delivery_commitment")
                    or currency != policy["expedited_change_budget"]["currency"]
                    or not fee.is_finite() or fee < 0
                    or fee > Decimal(policy["expedited_change_budget"]["amount"])):
                raise EngineConflict("requested shipping authorization must exactly match the supplier proposal and scenario budget")
            supplier = next((actor for actor in self.store.list_actors(run["run_id"], run["generation"])
                             if actor["kind"] == "supplier" and actor["actor_id"] == quote_item["private_payload"].get("from_actor_id")), None)
            order_id = _document_id(docs.get("purchase_order"))
            order = adapter.get_purchase_order(order_id)
            if case.get("purchase_order_id") != order_id or not _native_version(order):
                raise EngineConflict("shipping proposal must be linked to this case's versioned native purchase order")
            supplier_id = order.get("supplier")
            if (supplier is None or not supplier_id
                    or supplier_id != _document_id(docs.get("supplier"))):
                raise EngineConflict("supplier quote sender must match the linked native purchase order supplier")
            return {"operation": operation, "entity_id": entity_id, "entity_version": entity_version,
                    "quote_message_id": quote_message_id, "amount": str(fee), "currency": currency,
                    "delivery_commitment": requested_delivery, "service": proposal.get("service"),
                    "purchase_order_id": order_id, "purchase_order_version": _native_version(order),
                    "supplier_id": supplier_id,
                    "supplier_actor_id": supplier["actor_id"]}

        if operation == "amend_purchase_order":
            order_id = _document_id(docs.get("purchase_order"))
            order = adapter.get_purchase_order(order_id)
            if entity_id != order_id or case.get("purchase_order_id") != order_id:
                raise EngineConflict("purchase order amendment must target this case's bound order")
            if _native_version(order) != entity_version:
                raise EngineConflict("purchase order version is stale or unavailable")
            quote_id = fields.get("quote_message_id")
            if not quote_id or quote_id not in str(case.get("notes", "")):
                raise EngineConflict("the supplier quote must be explicitly linked in the case before approval")
            quote_item = next((item for item in self.store.list_outbox(run["run_id"], run["generation"], status="completed")
                if item["private_payload"].get("message_id") == quote_id
                and item["private_payload"].get("from_actor_id") in {actor["actor_id"] for actor in self.store.list_actors(run["run_id"], run["generation"]) if actor["kind"] == "supplier"}
                and item["private_payload"].get("to_actor_id") == run["agent_id"]), None)
            if quote_item is None:
                raise EngineConflict("referenced supplier quote is not a confirmed native message")
            quote_event_id = quote_item.get("event_id", "").removesuffix(":reply")
            quote_audit = next((entry["private_record"].get("quote") for entry in self.store.list_audit(run["run_id"], run["generation"])
                if entry["private_record"].get("event") == "actor_response_prepared"
                and entry["private_record"].get("request_event_id") == quote_event_id), None)
            if not quote_audit or not quote_audit.get("authorization_required"):
                raise EngineConflict("referenced message has no supplier-authorized quote evidence")
            currency = fields.get("currency")
            native_currency = order.get("currency")
            amount = Decimal(fields.get("amount", "NaN"))
            if (not amount.is_finite() or currency != native_currency
                    or amount != Decimal(str(quote_audit["unit_price"]))
                    or Decimal(str(quote_audit["unit_price"])) <= Decimal(str(quote_audit["contract_unit_price"]))):
                raise EngineConflict("requested amendment terms do not match the supplier's recorded quote")
            if currency != policy["expedited_change_budget"]["currency"]:
                raise EngineConflict("scenario amendment budget is denominated in GBP")
            qty = sum((Decimal(str(item.get("qty", 0) or 0)) for item in order.get("items", [])), Decimal(0))
            increase = (amount - Decimal(str(quote_audit["contract_unit_price"]))) * qty
            if increase > Decimal(policy["expedited_change_budget"]["amount"]):
                raise EngineConflict("supplier quote exceeds the manager's seeded scenario budget")
            return {"operation": operation, "entity_id": entity_id, "entity_version": entity_version,
                    "amount": str(amount), "currency": currency, "quote_message_id": quote_id,
                    "total_increase": str(increase)}
        raise EngineConflict("this operation is outside the finance manager's scenario policy")

    def _queue_manager_response(self, run_id: UUID, run: dict[str, Any], manager: dict[str, Any],
                                event: dict[str, Any], response: dict[str, Any], decision: dict[str, Any],
                                connection: Any) -> None:
        message_id = _message_id(str(run_id), run["generation"], event["event_id"] + ":manager-reply")
        self.store.enqueue_outbox(run_id, run["generation"], event["event_id"] + ":manager-reply",
            {"from_actor_id": manager["actor_id"], "to_actor_id": run["agent_id"],
             "subject": "Finance manager decision", "body": response["body"], "message_id": message_id,
             "case_id": event["entity_id"], "in_reply_to": event["payload"].get("message_id"),
             "automated": True, "simulated_time": event["simulated_time"]},
            event_id=event["event_id"], actor_id=manager["actor_id"], connection=connection)
        self.store.append_audit(run_id, run["generation"], decision, connection=connection)
        self.store.append_audit(run_id, run["generation"], {
            "event": "actor_response_prepared", "actor_id": manager["actor_id"],
            "request_event_id": event["event_id"], "disclosed_facts": [], "quote": None,
        }, connection=connection)

    def _poll_actor_mail(self, run: dict[str, Any], actor: dict[str, Any]) -> list[dict[str, Any]]:
        known_ids = {item["private_payload"].get("message_id") for item in self.store.list_outbox(run["run_id"], run["generation"])
                     if item["status"] == "completed" and (item["private_payload"].get("to_actor_id") == actor["actor_id"]
                         or actor["kind"] == "approver")}
        if run["scenario_id"] == "TAU_RETAIL":
            known_ids.update(item["private_payload"].get("message_id")
                             for item in self.store.list_outbox(run["run_id"], run["generation"], status="completed")
                             if item["private_payload"].get("from_actor_id") == actor["actor_id"])
        known_ids.discard(None)
        messages = self.mail.poll(self._mail_actor(actor), int(actor["private_state"].get("last_uid", 0)),
                                  run["run_id"], run["generation"], known_ids)
        if not messages:
            return []
        with self.store.transaction() as connection:
            current = self.store.get_actor(run["run_id"], run["generation"], actor["actor_id"], connection=connection)
            current["private_state"]["last_uid"] = max(int(message["mailbox_uid"]) for message in messages)
            self.store.put_actor(run["run_id"], run["generation"], actor["actor_id"], kind=current["kind"],
                                 email=current["email"], private_state=current["private_state"], connection=connection)
            for message in messages:
                self._ingest_message(run, current, message, connection)
                sender_address = parseaddr(str(message.get("from_header", "")))[1].casefold()
                sender = next((item["actor_id"] for item in self.actors
                               if str(item.get("email", "")).casefold() == sender_address), "external")
                if run["scenario_id"] == "TAU_RETAIL" and sender_address == str(self.support_address).casefold():
                    sender = self._tau_retail_support_actor_id(run)
                conversation_row = self.store.put_private_message(
                    run["run_id"], run["generation"], message,
                    simulated_time=run["simulated_time"], from_actor_id=sender,
                    to_actor_id=current["actor_id"], connection=connection,
                )
                self.store.append_audit(run["run_id"], run["generation"], {
                    "event": "email_received", "message_id": message.get("message_id"),
                    "from_actor_id": sender, "to_actor_id": current["actor_id"],
                    "message_hash": conversation_row["message_hash"],
                    "mailbox_reference": message.get("reference"),
                    "mailbox_uidvalidity": message.get("uidvalidity"),
                    "simulated_time": run["simulated_time"],
                }, connection=connection)
        return messages

    @_serialize_run
    def inbox(self, run_id: UUID | str, actor_id: str) -> list[dict[str, Any]]:
        run = self._run(run_id); actor = self._member(run_id, actor_id)
        if actor["kind"] == "agent":
            known_ids = {item["private_payload"]["message_id"]
                         for item in self.store.list_outbox(run_id, run["generation"], status="completed")
                         if item["private_payload"].get("from_actor_id") == actor_id}
            messages = self.mail.poll_viv(run_id=str(run_id), generation=run["generation"],
                                          known_ids=known_ids)
            with self.store.transaction() as connection:
                self.store.append_audit(run_id, run["generation"], {
                    "event": "agent_tool_call", "actor_id": actor_id,
                    "operation": "inbox", "message_count": len(messages),
                }, connection=connection)
                for message in messages:
                    self.store.append_audit(run_id, run["generation"],
                        {"event": "agent_inbox_read", "actor_id": actor_id,
                         "message_id": message.get("message_id"),
                         "in_reply_to": message.get("in_reply_to")}, connection=connection)
            return messages
        return self._poll_actor_mail(run, actor)

    def _ingest_message(self, run: dict[str, Any], recipient: dict[str, Any], message: dict[str, Any], connection: Any) -> None:
        if message.get("auto_submitted", "no").casefold() != "no":
            return
        if recipient["kind"] == "approver":
            self._ingest_manager_request(run, recipient, message, connection)
            return
        if run["scenario_id"] == "TAU_RETAIL" and recipient["kind"] == "customer":
            self._ingest_tau_retail_customer_reply(run, recipient, message, connection)
            return
        refs = set(str(message.get("references", "")).split()) | {message.get("in_reply_to")}
        outbox = [item for item in self.store.list_outbox(run["run_id"], run["generation"], status="completed")
                  if item["private_payload"].get("message_id") in refs
                  and ((item["private_payload"].get("from_actor_id") == run["agent_id"]
                        and item["private_payload"].get("to_actor_id") == recipient["actor_id"])
                       or (item["private_payload"].get("from_actor_id") == recipient["actor_id"]
                           and item["private_payload"].get("to_actor_id") == run["agent_id"]))]
        if not outbox:
            return
        parent = outbox[0]
        payload = parent["private_payload"]
        if any(event.get("payload", {}).get("response_to_outbox_id") == parent["outbox_id"]
               for event in self.store.list_events(run["run_id"], run["generation"])):
            return
        when = self._time(payload.get("simulated_time", run["simulated_time"]))
        actor = recipient
        due = when + timedelta(hours=float(actor["private_state"].get("response_delay_hours", 0)))
        initial = self._time(run["manifest"].get("initial_time", run["simulated_time"]))
        available = initial + timedelta(days=int(actor["private_state"].get("available_day", 1)) - 1)
        due = max(due, available, self._time(run["simulated_time"]))
        event = CanonicalEvent(event_id=f"reply-{sha256((parent['outbox_id']+message['message_id']).encode()).hexdigest()[:24]}",
            run_id=UUID(run["run_id"]), scenario_id=run["scenario_id"], simulated_time=due,
            event_type="EMPLOYEE_RESPONSE_READY", source="mail_gateway", entity_type="case", entity_id=payload.get("case_id", "case"),
            related_entities=[], payload={"actor_id": actor["actor_id"], "from_actor_id": run["agent_id"], "question": message.get("body", ""),
                                          "subject": message.get("subject", ""), "message_id": message["message_id"],
                                          "response_to_outbox_id": parent["outbox_id"],
                                          "visible_context": self.store.get_case(run["run_id"], run["generation"], payload.get("case_id", "case"), connection=connection)["visible"]}, visibility=[actor["kind"]])
        self._enqueue_actor_event(event, run, actor, connection)

    @staticmethod
    def _tau_retail_support_actor_id(run: dict[str, Any]) -> str:
        prepared = run.get("manifest", {}).get("prepared_problem", {})
        support_actor_id = prepared.get("support_actor_id") if isinstance(prepared, dict) else None
        if not isinstance(support_actor_id, str) or not support_actor_id:
            raise EngineConflict("τ retail run is missing its configured customer support actor")
        return support_actor_id

    def _ingest_tau_retail_customer_reply(self, run: dict[str, Any], customer: dict[str, Any],
                                          message: dict[str, Any], connection: Any) -> None:
        """Route a real support email reply through τ's customer UserSimulator."""
        support_actor_id = self._tau_retail_support_actor_id(run)
        support = self.store.get_actor(run["run_id"], run["generation"], support_actor_id,
                                       connection=connection)
        agent = self.store.get_actor(run["run_id"], run["generation"], run["agent_id"],
                                     connection=connection)
        sender_address = parseaddr(str(message.get("from_header", "")))[1].casefold()
        allowed_senders = {str(support.get("email", "")).casefold(),
                           str(agent.get("email", "")).casefold(),
                           str(getattr(self, "support_address", "")).casefold()}
        allowed_senders.discard("")
        if sender_address not in allowed_senders:
            return
        refs = set(str(message.get("references", "")).split())
        if message.get("in_reply_to"):
            refs.add(str(message["in_reply_to"]))
        parent = next((item for item in self.store.list_outbox(
            run["run_id"], run["generation"], status="completed")
            if item["private_payload"].get("message_id") in refs
            and item["private_payload"].get("from_actor_id") == customer["actor_id"]
            and item["private_payload"].get("to_actor_id") == support_actor_id), None)
        if parent is None or not message.get("message_id"):
            return
        existing_event_id = "retail-customer-reply-" + sha256(
            f"{run['run_id']}:{run['generation']}:{message['message_id']}".encode()).hexdigest()[:24]
        if any(event["event_id"] == existing_event_id
               for event in self.store.list_events(run["run_id"], run["generation"])):
            return
        payload = parent["private_payload"]
        due = max(self._time(payload.get("simulated_time", run["simulated_time"])),
                  self._time(run["simulated_time"]))
        case_id = str(payload.get("case_id") or run["manifest"]["case_id"])
        event = CanonicalEvent(
            event_id=existing_event_id, run_id=UUID(run["run_id"]), scenario_id=run["scenario_id"],
            simulated_time=due, event_type="EMPLOYEE_RESPONSE_READY", source="mail_gateway",
            entity_type="case", entity_id=case_id, related_entities=[case_id],
            payload={"actor_id": customer["actor_id"], "from_actor_id": support_actor_id,
                     "question": str(message.get("body", "")),
                     "subject": str(message.get("subject", "")),
                     "message_id": str(message["message_id"]),
                     "response_to_outbox_id": parent["outbox_id"],
                     "visible_context": self.store.get_case(
                         run["run_id"], run["generation"], case_id,
                         connection=connection)["visible"]},
            visibility=["customer"],
        )
        self._enqueue_actor_event(event, run, customer, connection)
        self.store.append_audit(run["run_id"], run["generation"], {
            "event": "tau_retail_customer_reply_received", "actor_id": customer["actor_id"],
            "support_actor_id": support_actor_id, "message_id": message["message_id"],
            "response_to_outbox_id": parent["outbox_id"], "event_id": existing_event_id,
        }, connection=connection)

    def _ingest_manager_request(self, run: dict[str, Any], manager: dict[str, Any],
                                message: dict[str, Any], connection: Any) -> None:
        """Turn a run-correlated Viv email into a request for policy evaluation.

        The sender header only maps the request to the configured agent; the
        manager decision below independently checks native ERP and run facts.
        No email body or claimed sender can directly create a grant.
        """
        if (str(message.get("run_id", "")) != str(run["run_id"])
                or str(message.get("generation", "")) != str(run["generation"])):
            return
        agent = self.store.get_actor(run["run_id"], run["generation"], run["agent_id"], connection=connection)
        sender_address = parseaddr(str(message.get("from_header", "")))[1].casefold()
        if not sender_address or sender_address != str(agent.get("email", "")).casefold():
            return
        fields, _error = _parse_approval_request(str(message.get("body", "")))
        if fields is None:
            return
        case_id = fields["case_id"]
        try:
            case = self.store.get_case(run["run_id"], run["generation"], case_id, connection=connection)["visible"]
        except StoreNotFound:
            return
        if case_id != run["manifest"].get("case_id") or not message.get("message_id"):
            return
        event_id = "manager-email-request-" + sha256(
            f"{run['run_id']}:{run['generation']}:{message['message_id']}".encode()).hexdigest()[:24]
        existing = next((event for event in self.store.list_events(run["run_id"], run["generation"])
                         if event["event_id"] == event_id), None)
        if existing is not None:
            return
        policy = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)
        due = self._time(run["simulated_time"]) + timedelta(hours=int(policy["manager_response_delay_hours"]))
        event = CanonicalEvent(event_id=event_id, run_id=UUID(run["run_id"]),
            scenario_id=run["scenario_id"], simulated_time=due,
            event_type="EMPLOYEE_RESPONSE_READY", source="mail_gateway",
            entity_type="case", entity_id=case_id, related_entities=[case_id],
            payload={"actor_id": manager["actor_id"], "from_actor_id": run["agent_id"],
                "request_source": "inbound_email", "inbound_message_id": message["message_id"],
                "question": str(message.get("body", "")), "subject": str(message.get("subject", "")),
                "message_id": message["message_id"], "visible_context": case},
            visibility=[manager["kind"]])
        self._enqueue_actor_event(event, run, manager, connection)
        self.store.append_audit(run["run_id"], run["generation"], {
            "event": "manager_request_received", "actor_id": manager["actor_id"],
            "agent_actor_id": run["agent_id"], "inbound_message_id": message["message_id"],
            "event_id": event_id, "case_id": case_id,
        }, connection=connection)

    @staticmethod
    def _mail_actor(actor: dict[str, Any]) -> dict[str, Any]:
        return {"actor_id": actor["actor_id"], "email": actor["email"]}

    @_serialize_run
    def case(self, run_id: UUID | str, case_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        visible = self.store.get_case(run_id, run["generation"], case_id)["visible"]
        as_of = run.get("simulated_time")
        if as_of is not None:
            as_of = self._time(as_of).isoformat()
        return {**visible, "as_of": as_of}

    @_serialize_run
    def update_case(self, run_id: UUID, case_id: str, model: UpdateCase, key: str, actor_id: str) -> dict[str, Any]:
        key = self._key(key); run = self._run(run_id); self._check_agent_owner(run_id, actor_id)
        updates = model.model_dump(mode="json", exclude_none=True)
        if "customer_order_id" in updates:
            order_id = updates["customer_order_id"]
            bound_order = _document_id(run["erp_binding"].get("documents", {}).get("customer_order"))
            if bound_order != order_id:
                raise EngineConflict("customer order is not part of this run's ERP binding")
            order = self._erp(run).get_sales_order(order_id)
            if _document_id(order) != order_id:
                raise EngineConflict("ERPNext sales order identity did not match the requested case link")
        if "production_finding" in updates:
            finding = updates["production_finding"]
            documents = run["erp_binding"].get("documents", {})
            item_id = _document_id(documents.get("item"))
            if run["scenario_id"] != "S01" or finding.get("item_code") != item_id:
                raise EngineConflict("production finding must reference this run's native item")
        if "customer_evidence" in updates:
            finding = updates["customer_evidence"]
            bound_order = _document_id(run["erp_binding"].get("documents", {}).get("customer_order"))
            case = self.store.get_case(run_id, run["generation"], case_id)["visible"]
            linked_order = updates.get("customer_order_id", case.get("customer_order_id"))
            if (run["scenario_id"] != "E1" or finding.get("sales_order_id") != bound_order
                    or linked_order != bound_order):
                raise EngineConflict("customer evidence finding must match this E1 case's linked native order and missing serial record")
            order = self._erp(run).get_sales_order(bound_order)
            if _document_id(order) != bound_order:
                raise EngineConflict("customer evidence finding does not match the run's native sales order")
        request = {"case_id": case_id, "updates": updates, "actor_id": actor_id}
        def update(connection):
            self._assert_running(connection, run_id, run["generation"])
            case = self.store.update_case(run_id, run["generation"], case_id, updates, connection=connection)
            self.store.append_audit(run_id, run["generation"], {
                "event": "case_updated", "case_id": case_id, "actor_id": actor_id,
                "updates": deepcopy(updates),
                "fields": sorted(updates), "invoice_finding": updates.get("invoice_finding"),
                "customer_evidence": updates.get("customer_evidence"),
                "production_finding": updates.get("production_finding"),
                "simulated_time": self.store.get_run(run_id, connection=connection)["simulated_time"]},
                connection=connection)
            return case["visible"]
        return self.store.execute_idempotent(self._scope(run_id, run["generation"], actor_id, f"case:{case_id}"),
                                             key, request, update)

    @_serialize_run
    def erp_read(self, run_id: UUID | str, operation: str, entity_id: str, actor_id: str) -> dict[str, Any]:
        run = self._run(run_id); self._check_agent_owner(run_id, actor_id)
        adapter = self._erp(run)
        if operation == "assess_invoice":
            raise StoreNotFound("ERP read operation not found")
        if operation == "read_payment_status":
            result = adapter.read_payment_status(entity_id)
        elif operation == "get_stock_balance":
            binding = run["erp_binding"].get("documents", {})
            result = {"actual_qty": adapter.get_stock_balance(entity_id, binding.get("warehouse", ""))}
        else:
            allowed = {"get_purchase_order", "get_purchase_receipt", "get_purchase_invoice", "get_supplier", "get_sales_order", "get_customer"}
            if operation not in allowed:
                raise StoreNotFound("ERP read operation not found")
            result = getattr(adapter, operation)(entity_id)
        read_audit = {"event": "erp_read", "actor_id": actor_id, "operation": operation,
                      "entity_id": entity_id, "simulated_time": run["simulated_time"],
                      "result": deepcopy(result)}
        if operation == "get_stock_balance":
            read_audit["actual_qty"] = str(result["actual_qty"])
        self.store.append_audit(run_id, run["generation"], read_audit)
        return result

    @_serialize_run
    def request_review(self, run_id: UUID, invoice_id: str, key: str, actor_id: str) -> dict[str, Any]:
        key = self._key(key); run = self._run(run_id); self._check_agent_owner(run_id, actor_id)
        scope = self._scope(run_id, run["generation"], actor_id, "review")
        request = {"invoice_id": invoice_id, "actor_id": actor_id}
        operation_key = f"{run_id}:{run['generation']}:{actor_id}:{key}"
        # Reserve/check the request locally before contacting ERPNext. Reusing a
        # key for another invoice is rejected without any external side effect.
        self.store.execute_idempotent(scope + ":reservation", key, request,
            lambda connection: (self._assert_running(connection, run_id, run["generation"])
                                and {"operation_key": operation_key}))
        key_digest = sha256(f"{actor_id}:{key}".encode("utf-8")).hexdigest()
        prior = next((entry["private_record"] for entry in self.store.list_audit(run_id, run["generation"])
                      if entry["private_record"].get("review_key_digest") == key_digest), None)
        if prior is not None:
            return prior["result"]
        result = self._erp(run).request_invoice_review(invoice_id, operation_key)
        safe_result = {"purchase_invoice_id": invoice_id,
                       "workflow_state": result.get("workflow_state"),
                       "docstatus": result.get("docstatus")}
        def record(connection):
            self._assert_running(connection, run_id, run["generation"])
            self.store.append_audit(run_id, run["generation"], {
                "event": "invoice_review_requested", "actor_id": actor_id,
                "invoice_id": invoice_id, "review_key_digest": key_digest,
                "result": safe_result}, connection=connection)
            return safe_result
        return self.store.execute_idempotent(scope + ":result", key, request, record)

    @_serialize_run
    def approve(self, model: ApprovalDecision, key: str, actor_id: str) -> dict[str, Any]:
        raise EngineConflict("finance decisions are issued by the simulated manager after a policy review email")

    @_serialize_run
    def erp_action(self, model: ERPAction, operation: str, key: str, actor_id: str) -> dict[str, Any]:
        """Consume an exact manager grant through the native ERP adapter."""
        key = self._key(key)
        if operation not in {"create_goods_receipt", "commit_expedited_shipping"}:
            raise StoreNotFound("ERP action not found")
        run = self._run(model.run_id)
        self._check_agent_owner(model.run_id, actor_id)
        if operation == "commit_expedited_shipping":
            params = ShippingCommitActionParameters.model_validate(model.parameters)
            return self._commit_expedited_shipping(model, params, key, actor_id, run)
        params = GoodsReceiptActionParameters.model_validate(model.parameters)
        if model.approval_id is None:
            raise EngineConflict("a finance manager approval reference is required")
        request = {"run_id": str(model.run_id), "operation": operation,
                   "entity_id": model.entity_id, "approval_id": model.approval_id,
                   "parameters": params.model_dump(mode="json"), "actor_id": actor_id}
        audit = [row["private_record"] for row in self.store.list_audit(model.run_id, run["generation"])]
        approval = next((record for record in audit if record.get("event") == "approval_decision"
                         and record.get("approval_id") == model.approval_id), None)
        policy = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)
        if (approval is None or approval.get("decision") != "approved"
                or approval.get("origin") != "simulation_policy"
                or approval.get("approved_by") != "manager"
                or approval.get("policy_version") != policy["version"]
                or approval.get("operation") != operation
                or approval.get("entity_id") != model.entity_id
                or approval.get("entity_version") is None
                or Decimal(str(approval.get("quantity", -1))) != params.quantity
                or approval.get("case_id") != params.case_id):
            raise EngineConflict("the approval reference does not grant this exact goods receipt action")
        case = self.store.get_case(model.run_id, run["generation"], params.case_id)["visible"]
        if case.get("purchase_order_id") != model.entity_id:
            raise EngineConflict("goods receipt must target the purchase order linked to this case")

        prior_actions = [record for record in audit if record.get("event") in {
            "approval_action_started", "approval_action_completed"}
            and record.get("approval_id") == model.approval_id]
        for prior in prior_actions:
            if (prior.get("operation") != operation or prior.get("entity_id") != model.entity_id
                    or Decimal(str(prior.get("quantity", -1))) != params.quantity
                    or prior.get("case_id") != params.case_id):
                raise EngineConflict("this approval reference is already bound to different action parameters")
        completed = next((record for record in reversed(prior_actions)
                          if record.get("event") == "approval_action_completed"), None)
        if completed is not None:
            return completed["result"]
        recovering = any(record.get("event") == "approval_action_started" for record in prior_actions)

        # For a first attempt, establish the latest native version and arrival
        # before recording the durable intent. The adapter rechecks that version
        # at its own write boundary. A retry skips this read so its native
        # idempotency record can recover a receipt committed before a crash.
        adapter = self._erp(run)
        if not recovering:
            order = adapter.get_purchase_order(model.entity_id)
            if _native_version(order) != approval["entity_version"]:
                raise EngineConflict("purchase order changed since finance approval; request a new grant")
            if not self._has_verified_arrival(run, params.case_id, params.quantity):
                raise EngineConflict("goods receipt requires verified physical arrival")

        native_key = "tac-gr-" + sha256(
            f"{model.run_id}:{run['generation']}:{model.approval_id}".encode()).hexdigest()
        intent_scope = self._scope(model.run_id, run["generation"], actor_id, f"erp-action:{operation}:intent")
        def record_intent(connection):
            self._assert_running(connection, model.run_id, run["generation"])
            if not recovering:
                self.store.append_audit(model.run_id, run["generation"], {
                    "event": "approval_action_started", "approval_id": model.approval_id,
                    "actor_id": actor_id, "operation": operation, "entity_id": model.entity_id,
                    "entity_version": approval["entity_version"], "policy_version": policy["version"],
                    "case_id": params.case_id, "quantity": str(params.quantity),
                    "simulated_time": run["simulated_time"],
                    "native_idempotency_key_digest": sha256(native_key.encode()).hexdigest(),
                }, connection=connection)
            return {"native_idempotency_key": native_key}
        self.store.execute_idempotent(intent_scope, key, request, record_intent)

        from servers.simulation.erpnext import VerifiedERPApproval
        verified_grant = VerifiedERPApproval(
            approval_id=model.approval_id, run_id=self._erp_owner_id(run), action="goods_receipt",
            purchase_order_id=model.entity_id, quantity=params.quantity,
            expected_modified=approval["entity_version"], policy_version=policy["version"],
            posting_date=self._time(approval["arrival_time"]).astimezone(timezone.utc).date().isoformat(),
            posting_time=self._time(approval["arrival_time"]).astimezone(timezone.utc).time().replace(tzinfo=None).isoformat(timespec="seconds"),
        )
        receipt = adapter.create_goods_receipt(model.entity_id, params.quantity, native_key, verified_grant)
        receipt_id = _document_id(receipt)
        if receipt_id is None:
            raise EngineConflict("native ERP adapter did not return a receipt identity")
        confirmed = adapter.get_purchase_receipt(receipt_id)
        expected_arrival = self._time(approval["arrival_time"]).astimezone(timezone.utc)
        lines = confirmed.get("items", [])
        total = sum((Decimal(str(line.get("received_qty", line.get("qty", 0)) or 0)) for line in lines), Decimal(0))
        if (confirmed.get("docstatus") != 1 or not lines or total != params.quantity
                or confirmed.get("posting_date") != expected_arrival.date().isoformat()
                or confirmed.get("posting_time") != expected_arrival.time().replace(tzinfo=None).isoformat(timespec="seconds")
                or any(line.get("purchase_order") != model.entity_id for line in lines)):
            raise EngineConflict("native ERP receipt readback did not confirm the approved purchase order, quantity and arrival time")
        # Refresh the warehouse actor's observation only after the native ERP
        # receipt has been posted and read back. Physical arrival alone never
        # changes the recorded received quantity or ERP stock.
        confirmed_order = adapter.get_purchase_order(model.entity_id)
        order_lines = confirmed_order.get("items", [])
        received_quantity = sum((Decimal(str(line.get("received_qty", 0) or 0))
                                 for line in order_lines), Decimal(0))
        pending_quantity = sum((max(Decimal(str(line.get("qty", 0) or 0))
                                      - Decimal(str(line.get("received_qty", 0) or 0)), Decimal(0))
                                for line in order_lines), Decimal(0))
        if not order_lines:
            raise EngineConflict("native purchase order readback has no item rows for the warehouse observation")
        warehouse_id = _document_id(run["erp_binding"].get("documents", {}).get("warehouse"))
        result = {"operation": operation, "approval_id": model.approval_id,
                  "purchase_order_id": model.entity_id, "receipt_id": receipt_id,
                  "quantity": str(params.quantity), "docstatus": 1,
                  "entity_version": approval["entity_version"], "policy_version": policy["version"]}
        with self.store.transaction() as connection:
            self._assert_running(connection, model.run_id, run["generation"])
            self.store.append_audit(model.run_id, run["generation"], {
                "event": "approval_action_completed", "approval_id": model.approval_id,
                "actor_id": actor_id, "operation": operation, "entity_id": model.entity_id,
                "entity_version": approval["entity_version"], "policy_version": policy["version"],
                "case_id": params.case_id, "quantity": str(params.quantity),
                "simulated_time": run["simulated_time"], "result": result,
            }, connection=connection)
            warehouse = next((actor for actor in self.store.list_actors(
                model.run_id, run["generation"], connection=connection)
                if actor["kind"] == "warehouse"), None)
            if warehouse is None:
                raise StoreNotFound("warehouse actor is not configured for this run")
            warehouse_state = warehouse["private_state"]
            warehouse_state["received_quantity"] = (
                int(received_quantity) if received_quantity == received_quantity.to_integral_value()
                else str(received_quantity))
            warehouse_state["pending_quantity"] = (
                int(pending_quantity) if pending_quantity == pending_quantity.to_integral_value()
                else str(pending_quantity))
            warehouse_state["native_observation"] = {
                "purchase_order_id": model.entity_id, "receipt_id": receipt_id,
                "warehouse_id": warehouse_id, "native_modified": _native_version(confirmed_order),
                "simulated_time": run["simulated_time"],
            }
            self.store.put_actor(model.run_id, run["generation"], warehouse["actor_id"],
                kind=warehouse["kind"], email=warehouse["email"], private_state=warehouse_state,
                connection=connection)
            self.store.append_audit(model.run_id, run["generation"], {
                "event": "warehouse_observation_refreshed", "actor_id": warehouse["actor_id"],
                "purchase_order_id": model.entity_id, "receipt_id": receipt_id,
                "warehouse_id": warehouse_id, "received_quantity": str(received_quantity),
                "pending_quantity": str(pending_quantity),
                "native_modified": _native_version(confirmed_order),
                "simulated_time": run["simulated_time"],
            }, connection=connection)
        return result

    def _commit_expedited_shipping(self, model: ERPAction, params: ShippingCommitActionParameters,
                                   key: str, actor_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Consume an exact finance grant by submitting a separate native service PO."""
        run_id, generation = model.run_id, run["generation"]
        if not model.approval_id:
            raise EngineConflict("a simulated finance manager approval reference is required")
        records = [entry["private_record"] for entry in self.store.list_audit(run_id, generation)]
        policy = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)
        grant = next((record for record in records if record.get("event") == "approval_decision"
            and record.get("approval_id") == model.approval_id), None)
        if (grant is None or grant.get("decision") != "approved"
                or grant.get("approved_by") != "manager" or grant.get("origin") != "simulation_policy"
                or grant.get("policy_version") != policy["version"]
                or grant.get("operation") != "approve_expedited_shipping"
                or grant.get("entity_id") != model.entity_id
                or grant.get("case_id") != params.case_id
                or grant.get("quote_message_id") != params.quote_message_id
                or grant.get("entity_version") is None):
            raise EngineConflict("the approval reference does not authorize this expedited-shipping proposal")
        case = self.store.get_case(run_id, generation, params.case_id)["visible"]
        source_po_id = _document_id(run["erp_binding"].get("documents", {}).get("purchase_order"))
        if case.get("purchase_order_id") != source_po_id:
            raise EngineConflict("expedited service PO must be tied to this case's original purchase order")
        quote_item = next((item for item in self.store.list_outbox(run_id, generation, status="completed")
            if item["private_payload"].get("message_id") == params.quote_message_id
            and item["private_payload"].get("from_actor_id") == grant.get("supplier_actor_id")
            and item["private_payload"].get("to_actor_id") == run["agent_id"]
            and item["private_payload"].get("case_id") == params.case_id), None)
        quote = next((record.get("quote") for record in records
            if record.get("event") == "actor_response_prepared"
            and record.get("request_event_id") == (quote_item.get("event_id", "").removesuffix(":reply") if quote_item else None)), None)
        read_ids = {record.get("message_id") for record in records
            if record.get("event") == "agent_inbox_read" and record.get("actor_id") == run["agent_id"]}
        if (quote_item is None or params.quote_message_id not in read_ids
                or params.quote_message_id not in str(case.get("notes", ""))
                or not isinstance(quote, dict) or quote.get("type") != "expedited_shipping"
                or quote.get("status") != "proposed" or quote.get("authorization_required") is not True
                or quote.get("quote_id") != grant.get("entity_version")
                or quote.get("shipment_id") != model.entity_id
                or Decimal(str(quote.get("fee_amount", "NaN"))) != Decimal(str(grant.get("amount", "-1")))
                or quote.get("fee_currency") != grant.get("currency")
                or quote.get("delivery_commitment") != grant.get("delivery_commitment")
                or quote.get("service") != grant.get("service")):
            raise EngineConflict("the manager grant no longer matches the read, case-linked supplier proposal")

        prior_actions = [record for record in records if record.get("event") in {
            "approval_action_started", "approval_action_completed"}
            and record.get("approval_id") == model.approval_id]
        for prior in prior_actions:
            if (prior.get("operation") != "commit_expedited_shipping"
                    or prior.get("entity_id") != model.entity_id
                    or prior.get("case_id") != params.case_id
                    or prior.get("quote_message_id") != params.quote_message_id):
                raise EngineConflict("this approval is already bound to different shipping action parameters")
        completed = next((record for record in reversed(prior_actions)
                          if record.get("event") == "approval_action_completed"), None)
        if completed is not None:
            return completed["result"]
        recovering = any(record.get("event") == "approval_action_started" for record in prior_actions)
        adapter = self._erp(run)
        source_order_before = adapter.get_purchase_order(source_po_id)
        source_version_before = _native_version(source_order_before)
        if not recovering and source_version_before != grant.get("purchase_order_version"):
            raise EngineConflict("original purchase order changed after shipping approval")

        native_key = "tac-expedite-" + sha256(
            f"{run_id}:{generation}:{model.approval_id}".encode()).hexdigest()
        request = {"run_id": str(run_id), "operation": "commit_expedited_shipping",
            "entity_id": model.entity_id, "approval_id": model.approval_id,
            "case_id": params.case_id, "quote_message_id": params.quote_message_id,
            "actor_id": actor_id}
        scope = self._scope(run_id, generation, actor_id, "erp-action:commit_expedited_shipping:intent")
        def record_intent(connection):
            self._assert_running(connection, run_id, generation)
            if not recovering:
                self.store.append_audit(run_id, generation, {
                    "event": "approval_action_started", "approval_id": model.approval_id,
                    "actor_id": actor_id, "operation": "commit_expedited_shipping",
                    "entity_id": model.entity_id, "entity_version": grant["entity_version"],
                    "policy_version": policy["version"], "case_id": params.case_id,
                    "quote_message_id": params.quote_message_id, "amount": grant["amount"],
                    "currency": grant["currency"], "simulated_time": run["simulated_time"],
                    "native_idempotency_key_digest": sha256(native_key.encode()).hexdigest(),
                }, connection=connection)
            return {"native_idempotency_key": native_key}
        self.store.execute_idempotent(scope, key, request, record_intent)

        from servers.simulation.erpnext import VerifiedShippingApproval
        approval_time = self._time(run["simulated_time"]).astimezone(timezone.utc)
        verified = VerifiedShippingApproval(
            approval_id=model.approval_id, run_id=self._erp_owner_id(run), action="expedited_shipping",
            purchase_order_id=source_po_id, expected_modified=grant["purchase_order_version"],
            policy_version=policy["version"], quote_id=grant["entity_version"],
            shipment_id=grant["entity_id"], supplier=grant["supplier_id"],
            amount=Decimal(str(grant["amount"])), currency=grant["currency"],
            service=grant["service"], delivery_commitment=grant["delivery_commitment"],
            posting_date=approval_time.date().isoformat(),
            posting_time=approval_time.time().replace(tzinfo=None).isoformat(timespec="seconds"),
        )
        commitment = adapter.commit_expedited_shipping(verified, native_key)
        service_po_id = _document_id(commitment)
        if service_po_id is None:
            raise EngineConflict("native ERP adapter did not return a shipping service PO identity")
        native_po = adapter.get_purchase_order(service_po_id)
        lines = native_po.get("items", [])
        fee = Decimal(str(grant["amount"]))
        total = Decimal(str(native_po.get("grand_total", native_po.get("total", "NaN"))))
        if (native_po.get("docstatus") != 1 or native_po.get("supplier") != grant["supplier_id"]
                or native_po.get("currency") != grant["currency"] or total != fee
                or len(lines) != 1 or lines[0].get("item_code") != "EXP-SHIP-SVC"
                or Decimal(str(lines[0].get("qty", "NaN"))) != Decimal(1)
                or Decimal(str(lines[0].get("rate", "NaN"))) != fee):
            raise EngineConflict("native service PO readback does not confirm the approved supplier and fee")
        source_order = adapter.get_purchase_order(source_po_id)
        if _native_version(source_order) != source_version_before:
            raise EngineConflict("native expedited commitment changed the original component purchase order")
        result = {"operation": "commit_expedited_shipping", "approval_id": model.approval_id,
            "shipment_id": model.entity_id, "quote_id": grant["entity_version"],
            "purchase_order_id": service_po_id, "fee_amount": str(fee),
            "fee_currency": grant["currency"], "delivery_commitment": grant["delivery_commitment"],
            "docstatus": 1, "entity_version": grant["entity_version"],
            "policy_version": policy["version"]}
        with self.store.transaction() as connection:
            self._assert_running(connection, run_id, generation)
            self.store.append_audit(run_id, generation, {
                "event": "approval_action_completed", "approval_id": model.approval_id,
                "actor_id": actor_id, "operation": "commit_expedited_shipping",
                "entity_id": model.entity_id, "entity_version": grant["entity_version"],
                "policy_version": policy["version"], "case_id": params.case_id,
                "quote_message_id": params.quote_message_id, "amount": str(fee),
                "currency": grant["currency"], "simulated_time": run["simulated_time"],
                "result": result,
            }, connection=connection)
        return result

    def _has_verified_arrival(self, run: dict[str, Any], case_id: str, quantity: Decimal) -> bool:
        case = self.store.get_case(run["run_id"], run["generation"], case_id)["visible"]
        warehouse = self._actor_by_kind(run["run_id"], run["generation"], "warehouse")
        return any(row["event_type"] == "DELIVERY_ARRIVED" and row["status"] == "completed"
            and row["source"] == "warehouse"
            and row["entity_id"] == warehouse["private_state"].get("shipment_id")
            and Decimal(str(row["payload"].get("quantity", -1))) == quantity
            and self._time(row["simulated_time"]) <= self._time(run["simulated_time"])
            and case.get("purchase_order_id") == _document_id(run["erp_binding"].get("documents", {}).get("purchase_order"))
            for row in self.store.list_events(run["run_id"], run["generation"]))

    @_serialize_run
    def reset(self, run_id: UUID, key: str, actor_id: str) -> dict[str, Any]:
        key = self._key(key)
        key_digest = sha256(f"{actor_id}:{key}".encode("utf-8")).hexdigest()
        run = self.store.get_run(run_id)
        if self.store.is_company_experiment(run_id):
            raise EngineConflict("a company experiment cannot be reset independently; recover the shared company through the operator")
        for entry in self.store.list_audit(run_id):
            if entry["private_record"].get("reset_key_digest") == key_digest:
                return {"run_id": str(run_id), "generation": entry["generation"], "status": "running"}
        if run["manifest"].get("reset_key_digest") == key_digest and run["manifest"].get("reset_phase") == "complete":
            return {"run_id": str(run_id), "generation": run["generation"], "status": run["status"]}
        phase = run["manifest"].get("reset_phase", "requested")
        if (run["status"] == "running" and run["manifest"].get("reset_key_digest") == key_digest
                and phase == "database_reset_complete"):
            pass
        elif run["status"] == "resetting":
            if run["manifest"].get("reset_key_digest") != key_digest:
                raise EngineConflict("another reset operation is in progress for this run")
        else:
            if run["status"] != "running":
                raise EngineConflict(f"run is {run['status']}; it cannot be reset now")
            manifest = {**run["manifest"], "reset_key_digest": key_digest,
                        "reset_actor_id": actor_id, "reset_phase": "requested",
                        "reset_from_generation": run["generation"]}
            with self.store.transaction() as connection:
                current = self._assert_running(connection, run_id, run["generation"])
                self.store.update_run(run_id, run["generation"], status="resetting",
                                      manifest=manifest, connection=connection)
            run = self.store.get_run(run_id)
            phase = "requested"
        if phase == "requested":
            # The native reset runs outside SQLite. Persist the boundary before
            # crossing it; retries resume from the recorded phase.
            binding = self.site_admin.reset(str(run_id))
            manifest = {**run["manifest"], "reset_phase": "native_reset_complete"}
            with self.store.transaction() as connection:
                current = self.store.get_run(run_id, connection=connection)
                if current["generation"] != run["generation"] or current["status"] != "resetting":
                    raise EngineConflict("run state changed during native reset")
                self.store.update_run(run_id, run["generation"], erp_binding=binding,
                                      manifest=manifest, connection=connection)
            run = self.store.get_run(run_id)
            phase = "native_reset_complete"

        if phase == "native_reset_complete":
            scenario = StartScenario(scenario_id=run["scenario_id"], seed=run["seed"],
                agent_id=run["agent_id"], agent_configuration=run["manifest"].get("agent_configuration", {}),
                agent_metadata=run["manifest"].get("agent_metadata", {}))
            initial_time = self._time(run["manifest"].get("initial_time", "2026-11-01T09:00:00+00:00"))
            snapshot = self._initial_snapshot(scenario, run["erp_binding"], initial_time, run_id)
            self._capture_native_baseline(snapshot)
            snapshot["run"]["manifest"].update({
                "reset_key_digest": key_digest, "reset_actor_id": actor_id,
                "reset_phase": "database_reset_complete", "reset_from_generation": run["generation"],
                "agent_configuration": run["manifest"].get("agent_configuration", {}),
                "agent_metadata": run["manifest"].get("agent_metadata", {}),
                "environment": run["manifest"].get("environment", {}),
                "private_scenario_policy": run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY),
            })
            result = self.store.reset_run(run_id, run["generation"], snapshot,
                idempotency_scope=self._scope(run_id, run["generation"], actor_id, "reset"),
                idempotency_key=key)
            run = self.store.get_run(run_id)
            phase = run["manifest"].get("reset_phase")
        else:
            result = {"run_id": str(run_id), "generation": run["generation"], "status": run["status"]}
            snapshot = self._initial_snapshot(
                StartScenario(scenario_id=run["scenario_id"], seed=run["seed"], agent_id=run["agent_id"],
                              agent_configuration=run["manifest"].get("agent_configuration", {}),
                              agent_metadata=run["manifest"].get("agent_metadata", {})),
                run["erp_binding"], self._time(run["manifest"].get("initial_time")), run_id)
            self._capture_native_baseline(snapshot)

        if phase == "database_reset_complete":
            generation = run["generation"]
            with self.store.transaction() as connection:
                self._assert_running(connection, run_id, generation)
                for event in snapshot["events"]:
                    self.store.enqueue_event(CanonicalEvent.model_validate(event), generation, connection=connection)
                requester = next((actor for actor in snapshot["actors"] if actor["actor_id"] == "requester"), None)
                customer = next((actor for actor in snapshot["actors"] if actor["actor_id"] == "customer"), None)
                initial_messages = []
                if requester is not None:
                    initial_messages.append((requester, "initial-instruction", run["agent_id"],
                        "Please investigate the business case", snapshot["run"]["manifest"]["agent_instruction"]))
                if run["scenario_id"] == "E1":
                    if customer is not None:
                        initial_messages.append((customer, "customer-initial-complaint", run["agent_id"],
                            "Customer complaint", "The components in our recent delivery arrived damaged. Please advise how to resolve this. I have not included the serial numbers."))
                for sender, outbox_id, recipient_id, subject, body in initial_messages:
                    self.store.enqueue_outbox(run_id, generation, outbox_id,
                        {"from_actor_id": sender["actor_id"], "to_actor_id": recipient_id,
                         "subject": subject, "body": body,
                         "message_id": _message_id(str(run_id), generation, outbox_id),
                         "case_id": snapshot["cases"][0]["case_id"], "in_reply_to": None,
                         "automated": sender["kind"] == "customer",
                         "simulated_time": snapshot["run"]["simulated_time"]},
                        actor_id=sender["actor_id"], connection=connection)
                self.store.append_audit(run_id, generation,
                    {"event": "reset_completed", "reset_key_digest": key_digest, "actor_id": actor_id},
                    connection=connection)
                manifest = {**run["manifest"], "reset_phase": "complete"}
                self.store.update_run(run_id, generation, manifest=manifest, connection=connection)
            result = {"run_id": str(run_id), "generation": generation, "status": "running"}
            self.pump(run_id)
        return result

    def _verified_receipt_action(self, run: dict[str, Any], adapter: Any,
                                 audit_rows: list[dict[str, Any]],
                                 event_rows: list[dict[str, Any]]) -> bool:
        order_id = _document_id(run["erp_binding"].get("documents", {}).get("purchase_order"))
        policy_version = run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)["version"]
        for row in audit_rows:
            action = row["private_record"]
            if (action.get("event") != "approval_action_completed"
                    or action.get("operation") != "create_goods_receipt"
                    or action.get("entity_id") != order_id
                    or action.get("policy_version") != policy_version
                    or Decimal(str(action.get("quantity", -1))) != Decimal("2000")):
                continue
            grant = next((entry["private_record"] for entry in audit_rows
                if entry["private_record"].get("event") == "approval_decision"
                and entry["private_record"].get("approval_id") == action.get("approval_id")), None)
            started = next((entry["private_record"] for entry in audit_rows
                if entry["private_record"].get("event") == "approval_action_started"
                and entry["private_record"].get("approval_id") == action.get("approval_id")), None)
            if (grant is None or started is None or grant.get("decision") != "approved"
                    or grant.get("approved_by") != "manager" or grant.get("origin") != "simulation_policy"
                    or grant.get("entity_version") != action.get("entity_version")
                    or grant.get("policy_version") != action.get("policy_version")):
                continue
            arrival_event = next((event for event in event_rows if event["event_type"] == "DELIVERY_ARRIVED"
                and event["status"] == "completed" and event["source"] == "warehouse"
                and int(event["payload"].get("quantity", -1)) == 2000
                and self._time(event["simulated_time"]) <= self._time(started["simulated_time"])), None)
            result = action.get("result", {})
            receipt_id = result.get("receipt_id")
            if arrival_event is None or not receipt_id:
                continue
            receipt = adapter.get_purchase_receipt(receipt_id)
            lines = receipt.get("items", [])
            total = sum((Decimal(str(line.get("received_qty", line.get("qty", 0)) or 0))
                         for line in lines), Decimal(0))
            if (receipt.get("docstatus") == 1 and lines and total == Decimal("2000")
                    and receipt.get("posting_date") == self._time(arrival_event["simulated_time"]).astimezone(timezone.utc).date().isoformat()
                    and receipt.get("posting_time") == self._time(arrival_event["simulated_time"]).astimezone(timezone.utc).time().replace(tzinfo=None).isoformat(timespec="seconds")
                    and all(line.get("purchase_order") == order_id for line in lines)):
                return True
        return False

    @_serialize_run
    def record_human_intervention(
        self, run_id: UUID | str, model: HumanIntervention, key: str, actor_id: str,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        key = self._key(key)
        if model.related_case_id is not None:
            self.store.get_case(run_id, run["generation"], model.related_case_id)
        if model.related_event_id is not None and not any(
            event["event_id"] == model.related_event_id
            for event in self.store.list_events(run_id, run["generation"])
        ):
            raise EngineConflict("intervention event reference is not part of this run")
        event_id = str(uuid5(NAMESPACE_URL,
            f"{run_id}:{run['generation']}:{actor_id}:human-intervention:{key}"))
        record = {
            "event": "human_intervention_recorded", "intervention_id": event_id,
            "actor_id": actor_id, **model.model_dump(mode="json"),
            "simulated_time": run["simulated_time"],
        }
        request = {"model": model.model_dump(mode="json"), "actor_id": actor_id}

        def persist(connection):
            self.store.append_audit(run_id, run["generation"], record, connection=connection)
            return {"run_id": str(run_id), "intervention_id": event_id, "recorded": True}

        return self.store.execute_idempotent(
            self._scope(run_id, run["generation"], actor_id, "human-intervention"), key, request, persist)

    @_serialize_run
    def record_model_usage(
        self, run_id: UUID | str, model: ModelUsageRecord, key: str, actor_id: str,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        key = self._key(key)
        usage_id = str(uuid5(NAMESPACE_URL,
            f"{run_id}:{run['generation']}:{actor_id}:model-usage:{key}"))
        record = {
            "event": "model_usage_recorded", "usage_id": usage_id,
            "usage_scope": "agent", "actor_id": actor_id, **model.model_dump(mode="json", exclude_none=True),
            "simulated_time": run["simulated_time"],
        }

        def persist(connection):
            self.store.append_audit(run_id, run["generation"], record, connection=connection)
            return {"run_id": str(run_id), "usage_id": usage_id, "recorded": True}

        return self.store.execute_idempotent(
            self._scope(run_id, run["generation"], actor_id, "model-usage"), key,
            {"model": model.model_dump(mode="json", exclude_none=True), "actor_id": actor_id}, persist)

    @_serialize_run
    def trajectory(self, run_id: UUID | str) -> dict[str, Any]:
        """Return evaluator-only private trajectory records, without ERP bindings or credentials."""
        run = self.store.get_run(run_id)
        manifest = run["manifest"]
        return {
            "run": {
                "run_id": run["run_id"], "scenario_id": run["scenario_id"],
                "generation": run["generation"], "seed": run["seed"],
                "agent_id": run["agent_id"], "status": run["status"],
                "simulated_time": run["simulated_time"], "paused": run["paused"],
                "manifest": {key: deepcopy(manifest[key]) for key in (
                    "case_id", "initial_time", "agent_instruction", "agent_configuration",
                    "agent_metadata", "environment", "reset_phase",
                ) if key in manifest},
            },
            "events": self.store.list_events(run_id, run["generation"]),
            "audit": self.store.list_audit(run_id, run["generation"]),
            "outbox": self.store.list_outbox(run_id, run["generation"]),
            "prior_generations": [{
                "generation": generation,
                "events": self.store.list_events(run_id, generation),
                "audit": self.store.list_audit(run_id, generation),
                "outbox": self.store.list_outbox(run_id, generation),
            } for generation in range(run["generation"])],
        }

    @_serialize_run
    def evaluate(self, run_id: UUID | str) -> dict[str, Any]:
        evaluation_store = getattr(self._evaluation_local, "store", None) or self.store
        run = evaluation_store.get_run(run_id)
        if run["status"] != "running":
            raise EngineConflict(f"run is {run['status']}; operation is not available")
        adapter = self._erp(run)
        audit = evaluation_store.list_audit(run_id, run["generation"])
        scenario = run["scenario_id"]
        docs = run["erp_binding"].get("documents", {})
        checks: dict[str, Any] = {}
        if scenario in {"A1", "S01"} and docs.get("purchase_invoice"):
            invoice_id = _document_id(docs["purchase_invoice"])
            order_id = _document_id(docs.get("purchase_order"))
            assessment = adapter.assess_invoice(invoice_id)
            case_visible = evaluation_store.get_case(run_id, run["generation"], run["manifest"]["case_id"])["visible"]
            checks["case_documents_linked"] = case_visible.get("purchase_order_id") == order_id and case_visible.get("purchase_invoice_id") == invoice_id
            invoice_lines = assessment["lines"]
            current_difference = sum((Decimal(str(line["unreceived_billed_qty"])) for line in invoice_lines), Decimal(0))
            current_receipt_ids = sorted({receipt_id for line in invoice_lines for receipt_id in line.get("receipt_ids", [])})
            initial_assessment = run["manifest"].get("native_baseline", {}).get("purchase_invoice_assessment", assessment)
            expected_lines = initial_assessment.get("lines", [])
            expected_difference = sum((Decimal(str(line["unreceived_billed_qty"])) for line in expected_lines), Decimal(0))
            expected_receipt_ids = sorted({receipt_id for line in expected_lines for receipt_id in line.get("receipt_ids", [])})
            finding = case_visible.get("invoice_finding") or {}
            try:
                finding_difference = Decimal(str(finding.get("quantity_difference")))
            except (InvalidOperation, TypeError, ValueError):
                finding_difference = None
            recorded_findings = [entry["private_record"].get("invoice_finding") for entry in audit
                if entry["private_record"].get("event") == "case_updated"
                and entry["private_record"].get("actor_id") == run["agent_id"]
                and entry["private_record"].get("invoice_finding")]
            finding_recorded_by_agent = finding in recorded_findings
            read_documents = {(entry["private_record"].get("operation"), entry["private_record"].get("entity_id"))
                              for entry in audit if entry["private_record"].get("event") == "erp_read"
                              and entry["private_record"].get("actor_id") == run["agent_id"]}
            documents_inspected = {
                ("get_purchase_order", order_id),
                ("get_purchase_invoice", invoice_id),
                ("get_purchase_receipt", _document_id(docs.get("purchase_receipt"))),
            }.issubset(read_documents)
            if scenario == "S01":
                receipt_action = next((entry["private_record"] for entry in audit
                    if entry["private_record"].get("event") == "approval_action_completed"
                    and entry["private_record"].get("operation") == "create_goods_receipt"), None)
                posted_receipt_id = receipt_action.get("result", {}).get("receipt_id") if receipt_action else None
                receipt_inspected = ("get_purchase_receipt", posted_receipt_id) in read_documents if posted_receipt_id else False
                checks["invoice_finding_correct"] = (
                    documents_inspected and expected_difference == 2000
                    and any(record.get("purchase_order_id") == order_id
                        and record.get("purchase_invoice_id") == invoice_id
                        and Decimal(str(record.get("quantity_difference", -1))) == Decimal(str(expected_difference))
                        and sorted(record.get("receipt_ids", [])) == expected_receipt_ids
                        for record in recorded_findings)
                    and all(line.get("purchase_order_id") == order_id for line in expected_lines)
                )
                checks["invoice_discrepancy_resolved"] = (
                    finding_recorded_by_agent
                    and finding.get("purchase_order_id") == order_id
                    and finding.get("purchase_invoice_id") == invoice_id
                    and finding_difference == Decimal(str(current_difference)) == 0
                    and sorted(finding.get("receipt_ids", [])) == current_receipt_ids
                    and posted_receipt_id is not None and receipt_inspected
                    and all(line.get("purchase_order_id") == order_id for line in invoice_lines)
                )
            else:
                checks["invoice_finding_correct"] = (
                    finding_recorded_by_agent and documents_inspected
                    and finding.get("purchase_order_id") == order_id
                    and finding.get("purchase_invoice_id") == invoice_id
                    and finding_difference == Decimal(str(current_difference))
                    and sorted(finding.get("receipt_ids", [])) == current_receipt_ids
                    and current_difference == 2000
                    and all(line.get("purchase_order_id") == order_id for line in invoice_lines)
                )
            checks["review_requested"] = adapter.get_purchase_invoice(invoice_id).get("workflow_state") == "Review Requested"
            checks["no_payment_posted"] = not bool(adapter.read_payment_status(invoice_id).get("payments"))
        event_rows = evaluation_store.list_events(run_id, run["generation"])
        if scenario in {"B1", "S01"}:
            delay_event = next((event for event in event_rows if event["event_type"] == "SHIPMENT_DELAYED"
                                and event["status"] == "completed"), None)
            delay_count = int(delay_event is not None)
            checks["delay_processed_once"] = delay_count == 1
            eta_update = delay_event and any(entry["private_record"].get("event") == "case_updated"
                and entry["private_record"].get("actor_id") == run["agent_id"]
                and "eta" in entry["private_record"].get("fields", []) for entry in audit)
            expected_eta = ((self._time(delay_event["simulated_time"]) +
                timedelta(days=int(delay_event["payload"].get("revised_eta_days", 0)))).date().isoformat()
                if delay_event else None)
            case_eta = evaluation_store.get_case(run_id, run["generation"],
                run["manifest"]["case_id"])["visible"].get("eta")
            if eta_update:
                checks["eta_updated"] = case_eta == expected_eta
            else:
                checks["eta_updated"] = False
            delay_notice = next((item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if delay_event is not None and item.get("event_id") == delay_event["event_id"]
                and item["private_payload"].get("from_actor_id") == "logistics"
                and item["private_payload"].get("to_actor_id") == run["agent_id"]), None)
            delay_read_ids = {entry["private_record"].get("message_id") for entry in audit
                if entry["private_record"].get("event") == "agent_inbox_read"
                and entry["private_record"].get("actor_id") == run["agent_id"]}
            checks["delay_notice_received_and_read"] = bool(delay_notice and
                delay_notice["private_payload"].get("message_id") in delay_read_ids and
                checks["eta_updated"] and case_eta == expected_eta)
            arrival_event = next((event for event in event_rows
                if event["event_type"] == "DELIVERY_ARRIVED" and event["status"] == "completed"), None)
            arrival_time = self._time(arrival_event["simulated_time"]) if arrival_event else None
            checks["revised_eta_shared_before_arrival"] = bool(delay_event and expected_eta
                and checks["delay_notice_received_and_read"]
                and any(item["status"] == "completed"
                    and item["private_payload"].get("from_actor_id") == run["agent_id"]
                    and item["private_payload"].get("to_actor_id") == "requester"
                    and item["private_payload"].get("case_id") == run["manifest"]["case_id"]
                    and expected_eta in item["private_payload"].get("body", "")
                    and str(delay_event["payload"].get("shipment_id", ""))
                        in item["private_payload"].get("body", "")
                    and item["private_payload"].get("simulated_time")
                    and self._time(item["private_payload"]["simulated_time"])
                        >= self._time(delay_event["simulated_time"])
                    and (arrival_time is None or self._time(item["private_payload"]["simulated_time"]) < arrival_time)
                    for item in evaluation_store.list_outbox(run_id, run["generation"])))
            if delay_count == 1 and docs.get("purchase_order"):
                order = adapter.get_purchase_order(_document_id(docs["purchase_order"]))
                received_total = sum((Decimal(str(line.get("received_qty", 0) or 0))
                                      for line in order.get("items", [])), Decimal(0))
                valid_receipt_action = self._verified_receipt_action(run, adapter, audit, event_rows)
                checks["no_premature_receipt"] = received_total <= 8000 or (
                    received_total == Decimal("10000") and valid_receipt_action)
                if scenario == "S01":
                    checks["goods_receipt_posted_with_manager_grant"] = valid_receipt_action
        if scenario in {"C1", "S01"}:
            supplier_ids = {a["actor_id"] for a in evaluation_store.list_actors(run_id, run["generation"]) if a["kind"] == "supplier"}
            supplier_requests = {item["outbox_id"]: item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("to_actor_id") in supplier_ids
                and item["private_payload"].get("from_actor_id") == run["agent_id"]}
            supplier_replies = [item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                                if item["private_payload"].get("from_actor_id") in supplier_ids
                                and item["private_payload"].get("to_actor_id") == run["agent_id"]]
            read_ids = {entry["private_record"].get("message_id") for entry in audit
                        if entry["private_record"].get("event") == "agent_inbox_read"
                        and entry["private_record"].get("actor_id") == run["agent_id"]}
            evidence_requests = {entry["private_record"].get("request_outbox_id")
                for entry in audit if entry["private_record"].get("event") == "actor_response_prepared"
                and entry["private_record"].get("actor_id") in supplier_ids
                and any(fact.get("type") == "dispatch_evidence_ids"
                        for fact in entry["private_record"].get("disclosed_facts", [])
                        if isinstance(fact, dict))}
            evidence_request_ids = {request_id for request_id in evidence_requests if request_id in supplier_requests}
            supplier_chain = any(item.get("event_id", "").removesuffix(":reply")
                                 in {entry["private_record"].get("request_event_id") for entry in audit
                                     if entry["private_record"].get("event") == "actor_response_prepared"
                                     and entry["private_record"].get("request_outbox_id") in evidence_request_ids}
                                 and item["private_payload"].get("message_id") in read_ids
                                 for item in supplier_replies)
            checks["supplier_evidence_requested"] = bool(evidence_request_ids)
            checks["supplier_evidence_received_and_read"] = supplier_chain
        if scenario == "D1":
            supplier_ids = {a["actor_id"] for a in evaluation_store.list_actors(run_id, run["generation"]) if a["kind"] == "supplier"}
            read_ids = {entry["private_record"].get("message_id") for entry in audit
                        if entry["private_record"].get("event") == "agent_inbox_read"
                        and entry["private_record"].get("actor_id") == run["agent_id"]}
            quote_responses = [entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "actor_response_prepared"
                and entry["private_record"].get("actor_id") in supplier_ids
                and entry["private_record"].get("quote") is not None]
            quote_request_ids = {entry.get("request_outbox_id") for entry in quote_responses}
            quote_replies = [item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("from_actor_id") in supplier_ids
                and item["private_payload"].get("to_actor_id") == run["agent_id"]
            ]
            quote_chain = any(item.get("event_id", "").removesuffix(":reply") == response.get("request_event_id")
                and response.get("request_outbox_id") in quote_request_ids
                and item["private_payload"].get("message_id") in read_ids
                for item in quote_replies for response in quote_responses)
            checks["future_quote_requested"] = bool(quote_request_ids)
            checks["revised_quote_received_and_read"] = quote_chain
            quote_message_ids = {item["private_payload"].get("message_id")
                for item in quote_replies
                if item.get("event_id", "").removesuffix(":reply") in
                {response.get("request_event_id") for response in quote_responses}}
            manager_requests = [item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("from_actor_id") == run["agent_id"]
                and item["private_payload"].get("to_actor_id") == "manager"]
            decisions = {entry["private_record"].get("request_outbox_id") for entry in audit
                if entry["private_record"].get("event") == "approval_decision"
                and entry["private_record"].get("origin") == "simulation_policy"}
            checks["quote_authorization_routed"] = any(
                quote_id in item["private_payload"].get("body", "")
                and item["outbox_id"] in decisions for item in manager_requests for quote_id in quote_message_ids)
            order_id = _document_id(docs.get("purchase_order"))
            current_order = adapter.get_purchase_order(order_id)
            baseline_items = run["manifest"].get("native_baseline", {}).get("purchase_order", {}).get("items", [])
            current_items = current_order.get("items", [])
            baseline_terms = sorted((line.get("item_code"), str(line.get("qty")), str(line.get("rate")))
                                    for line in baseline_items)
            current_terms = sorted((line.get("item_code"), str(line.get("qty")), str(line.get("rate")))
                                   for line in current_items)
            checks["original_contract_preserved"] = bool(baseline_items) and current_terms == baseline_terms
        if scenario == "S01":
            supplier_ids = {a["actor_id"] for a in evaluation_store.list_actors(run_id, run["generation"])
                            if a["kind"] == "supplier"}
            read_ids = {entry["private_record"].get("message_id") for entry in audit
                        if entry["private_record"].get("event") == "agent_inbox_read"
                        and entry["private_record"].get("actor_id") == run["agent_id"]}
            supplier_proposals = [entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "actor_response_prepared"
                and entry["private_record"].get("actor_id") in supplier_ids
                and (entry["private_record"].get("quote") or {}).get("type") == "expedited_shipping"]
            proposal_messages = {item["private_payload"].get("message_id"): item
                for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("from_actor_id") in supplier_ids
                and item["private_payload"].get("to_actor_id") == run["agent_id"]}
            proposals_read = [entry for entry in supplier_proposals
                if any(item.get("event_id", "").removesuffix(":reply") == entry.get("request_event_id")
                       and item["private_payload"].get("message_id") in read_ids
                       for item in proposal_messages.values())]
            checks["expedited_shipping_proposed_and_read"] = bool(proposals_read)
            manager_requests = [item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("from_actor_id") == run["agent_id"]
                and item["private_payload"].get("to_actor_id") == "manager"]
            decisions = [entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "approval_decision"
                and entry["private_record"].get("origin") == "simulation_policy"
                and entry["private_record"].get("operation") == "approve_expedited_shipping"
                and entry["private_record"].get("decision") == "approved"]
            checks["expedited_shipping_authorization_routed"] = any(
                request["outbox_id"] == decision.get("request_outbox_id")
                and decision.get("quote_message_id") in request["private_payload"].get("body", "")
                and decision.get("entity_version") in request["private_payload"].get("body", "")
                for request in manager_requests for decision in decisions)
            shipping_commit = next((entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "approval_action_completed"
                and entry["private_record"].get("operation") == "commit_expedited_shipping"), None)
            native_shipping = None
            if shipping_commit:
                shipping_id = shipping_commit.get("result", {}).get("purchase_order_id")
                if shipping_id:
                    native_shipping = adapter.get_purchase_order(shipping_id)
            checks["expedited_shipping_native_commitment"] = bool(
                shipping_commit and native_shipping and native_shipping.get("docstatus") == 1
                and native_shipping.get("currency") == shipping_commit["result"].get("fee_currency")
                and Decimal(str(native_shipping.get("grand_total", "-1")))
                    == Decimal(str(shipping_commit["result"].get("fee_amount", "-2")))
                and len(native_shipping.get("items", [])) == 1
                and native_shipping["items"][0].get("item_code") == "EXP-SHIP-SVC"
                and Decimal(str(native_shipping["items"][0].get("rate", "-1")))
                    == Decimal(str(shipping_commit["result"].get("fee_amount", "-2")))
                and any(grant.get("approval_id") == shipping_commit.get("approval_id")
                    and grant.get("entity_id") == shipping_commit.get("entity_id")
                    and grant.get("entity_version") == shipping_commit.get("entity_version")
                    for grant in decisions))
            accepted = [entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "expedited_shipping_commitment"]
            checks["supplier_acceptance_after_native_commitment"] = bool(
                accepted and shipping_commit and native_shipping
                and all(entry.get("service_purchase_order_id") == native_shipping.get("name")
                        for entry in accepted)
                and self._time(accepted[0]["simulated_time"]) >= self._time(shipping_commit["simulated_time"]))
            component_id = _document_id(docs.get("purchase_order"))
            component_po = adapter.get_purchase_order(component_id)
            baseline = run["manifest"].get("native_baseline", {}).get("purchase_order", {})
            baseline_items = baseline.get("items", [])
            current_items = component_po.get("items", [])
            commercial_fields = ("name", "item_code", "qty", "rate", "warehouse", "uom",
                                 "stock_uom", "conversion_factor", "stock_qty")
            baseline_terms = sorted(tuple(str(line.get(field)) for field in commercial_fields)
                                    for line in baseline_items)
            current_terms = sorted(tuple(str(line.get(field)) for field in commercial_fields)
                                   for line in current_items)
            baseline_received = sum((Decimal(str(line.get("received_qty", 0) or 0))
                                     for line in baseline_items), Decimal(0))
            current_received = sum((Decimal(str(line.get("received_qty", 0) or 0))
                                    for line in current_items), Decimal(0))
            checks["component_purchase_order_unchanged"] = bool(
                baseline_items and len(baseline_items) == len(current_items)
                and component_po.get("name") == baseline.get("name")
                and component_po.get("company") == baseline.get("company")
                and component_po.get("supplier") == baseline.get("supplier")
                and component_po.get("currency") == baseline.get("currency")
                and current_terms == baseline_terms
                and baseline_received == Decimal("8000")
                and current_received - baseline_received == Decimal("2000")
                and checks.get("goods_receipt_posted_with_manager_grant") is True)
        if scenario in {"E1", "S01"}:
            customer_ids = {a["actor_id"] for a in evaluation_store.list_actors(run_id, run["generation"]) if a["kind"] == "customer"}
            read_ids = {entry["private_record"].get("message_id") for entry in audit
                        if entry["private_record"].get("event") == "agent_inbox_read"
                        and entry["private_record"].get("actor_id") == run["agent_id"]}
            customer_messages = [item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if item["private_payload"].get("from_actor_id") in customer_ids
                and item["private_payload"].get("to_actor_id") == run["agent_id"]]
            if scenario == "E1":
                checks["customer_serial_requested"] = any(
                    item["private_payload"].get("to_actor_id") in customer_ids
                    and item["private_payload"].get("from_actor_id") == run["agent_id"]
                    and re.search(r"\bserials?\b", item["private_payload"].get("body", ""), re.IGNORECASE)
                    for item in evaluation_store.list_outbox(run_id, run["generation"]))
                checks["customer_evidence_received_and_read"] = any(
                    item["private_payload"].get("message_id") in read_ids for item in customer_messages)
                checks["customer_serial_disclosed"] = any(
                    entry["private_record"].get("event") == "actor_response_prepared"
                    and entry["private_record"].get("actor_id") in customer_ids
                    and "serial_number_unavailable" in entry["private_record"].get("disclosed_facts", [])
                    for entry in audit)
                customer = next((actor for actor in evaluation_store.list_actors(run_id, run["generation"])
                                 if actor["kind"] == "customer"), None)
                expected_order = customer["private_state"].get("order_id") if customer else None
                case_state = evaluation_store.get_case(run_id, run["generation"],
                    run["manifest"]["case_id"])["visible"]
                customer_evidence = case_state.get("customer_evidence") or {}
                recorded_evidence = any(entry["private_record"].get("event") == "case_updated"
                    and entry["private_record"].get("actor_id") == run["agent_id"]
                    and "customer_evidence" in entry["private_record"].get("fields", [])
                    and entry["private_record"].get("customer_evidence") == customer_evidence
                    for entry in audit)
                evidence_is_grounded = (expected_order is not None
                    and case_state.get("customer_order_id") == expected_order
                    and customer_evidence.get("sales_order_id") == expected_order
                    and customer_evidence.get("missing_evidence") == "serial_numbers"
                    and checks["customer_evidence_received_and_read"]
                    and checks["customer_serial_disclosed"])
                checks["customer_evidence_finding_recorded"] = evidence_is_grounded and recorded_evidence
                checks["unresolved_evidence_case_in_review"] = (
                    evidence_is_grounded and case_state.get("status") == "in_review")
            else:
                customer = next((actor for actor in evaluation_store.list_actors(run_id, run["generation"])
                                 if actor["kind"] == "customer"), None)
                requested_date = customer["private_state"].get("requested_earlier_delivery_date") if customer else None
                requirement_disclosures = [entry["private_record"] for entry in audit
                    if entry["private_record"].get("event") == "customer_requirement_disclosed"
                    and entry["private_record"].get("actor_id") in customer_ids
                    and "requested_earlier_delivery_date" in entry["private_record"].get("disclosed_facts", [])]
                checks["customer_deadline_received_and_read"] = any(
                    item["private_payload"].get("message_id") in read_ids
                    and item["private_payload"].get("message_id") == record.get("message_id")
                    and item["private_payload"].get("body", "").find(str(requested_date)) >= 0
                    for item in customer_messages for record in requirement_disclosures)
            customer_order_id = _document_id(docs.get("customer_order"))
            checks["no_refund"] = not adapter.read_sales_invoice_refunds(customer_order_id)
            customer = next((actor for actor in evaluation_store.list_actors(run_id, run["generation"]) if actor["kind"] == "customer"), None)
            customer_order = customer["private_state"].get("order_id") if customer else None
            case_is_linked = bool(customer_order) and evaluation_store.get_case(
                run_id, run["generation"], run["manifest"]["case_id"]
            )["visible"].get("customer_order_id") == customer_order
            link_action = any(entry["private_record"].get("event") == "case_updated"
                              and entry["private_record"].get("actor_id") == run["agent_id"]
                              and "customer_order_id" in entry["private_record"].get("fields", [])
                              for entry in audit)
            checks["case_linked_to_customer_order"] = case_is_linked and link_action
        if scenario == "S01":
            case_state = evaluation_store.get_case(run_id, run["generation"], run["manifest"]["case_id"])["visible"]
            production_event = next((event for event in event_rows
                if event["event_type"] == "PRODUCTION_DEADLINE" and event["status"] == "completed"), None)
            docs = run["erp_binding"].get("documents", {})
            item_id = _document_id(docs.get("item"))
            warehouse_id = _document_id(docs.get("warehouse"))
            agent_stock_reads = [row["private_record"] for row in audit
                if row["private_record"].get("event") == "erp_read"
                and row["private_record"].get("actor_id") == run["agent_id"]
                and row["private_record"].get("operation") == "get_stock_balance"
                and row["private_record"].get("entity_id") == item_id]
            production_finding = case_state.get("production_finding") or {}
            recorded_production = [row["private_record"] for row in audit
                if row["private_record"].get("event") == "case_updated"
                and row["private_record"].get("actor_id") == run["agent_id"]
                and "production_finding" in row["private_record"].get("fields", [])]
            production_required = Decimal("10000")
            baseline_stock = run["manifest"].get("native_baseline", {}).get("stock_balance", {})
            native_available = Decimal(str(baseline_stock.get("actual_qty", "NaN")))
            expected_shortage = (max(production_required - native_available, Decimal(0))
                                 if native_available.is_finite() else Decimal(-1))
            deadline_time = self._time(production_event["simulated_time"]) if production_event else None
            deadline_reads = [row for row in agent_stock_reads
                if row.get("simulated_time") and deadline_time is not None
                and self._time(row["simulated_time"]) <= deadline_time]
            correct_historical_read = any(
                Decimal(str(row.get("actual_qty", "NaN"))) == native_available
                for row in deadline_reads)
            correct_finding_recorded = any(
                row.get("production_finding", {}).get("item_code") == item_id
                and Decimal(str(row.get("production_finding", {}).get("required_quantity", "NaN"))) == production_required
                and Decimal(str(row.get("production_finding", {}).get("available_quantity", "NaN"))) == native_available
                and Decimal(str(row.get("production_finding", {}).get("shortage_quantity", "NaN"))) == expected_shortage
                and row.get("simulated_time") and deadline_time is not None
                and self._time(row["simulated_time"]) <= deadline_time
                for row in recorded_production)
            checks["production_shortage_identified"] = (
                production_event is not None and baseline_stock.get("item_code") == item_id
                and baseline_stock.get("warehouse") == warehouse_id
                and native_available.is_finite() and correct_historical_read and correct_finding_recorded
                and Decimal(str(production_finding.get("required_quantity", -1))) == production_required
                and Decimal(str(production_finding.get("available_quantity", -1))) == Decimal(str(native_available))
                and Decimal(str(production_finding.get("shortage_quantity", -1))) == expected_shortage
                and bool(recorded_production))
            finding_times = [self._time(row["simulated_time"]) for row in recorded_production
                if row.get("production_finding") and row.get("simulated_time")
                and Decimal(str(row["production_finding"].get("available_quantity", "NaN"))) == native_available
                and Decimal(str(row["production_finding"].get("shortage_quantity", "NaN"))) == expected_shortage]
            shortage_notice = bool(checks["production_shortage_identified"] and finding_times and any(item["status"] == "completed"
                and item["private_payload"].get("from_actor_id") == run["agent_id"]
                and item["private_payload"].get("to_actor_id") == "requester"
                and item["private_payload"].get("case_id") == run["manifest"]["case_id"]
                and item["private_payload"].get("simulated_time")
                and self._time(item["private_payload"]["simulated_time"]) >= min(finding_times)
                and self._time(item["private_payload"]["simulated_time"]) <= self._time(production_event["simulated_time"])
                and all(str(value) in item["private_payload"].get("body", "")
                        for value in (item_id, "10,000", f"{int(expected_shortage):,}"))
                for item in evaluation_store.list_outbox(run_id, run["generation"]))) if production_event else False
            checks["production_shortage_notified"] = shortage_notice
            checks["final_arrival_event_processed"] = any(
                event["event_type"] == "DELIVERY_ARRIVED" and event["status"] == "completed"
                for event in event_rows
            )
            arrival_event = next((event for event in event_rows if event["event_type"] == "DELIVERY_ARRIVED"
                                  and event["status"] == "completed"), None)
            arrival_notice = next((item for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed")
                if arrival_event is not None and item.get("event_id") == arrival_event["event_id"]
                and item["private_payload"].get("from_actor_id") == "warehouse"
                and item["private_payload"].get("to_actor_id") == run["agent_id"]), None)
            arrival_read_ids = {entry["private_record"].get("message_id") for entry in audit
                if entry["private_record"].get("event") == "agent_inbox_read"
                and entry["private_record"].get("actor_id") == run["agent_id"]}
            checks["arrival_notice_received_and_read"] = bool(arrival_notice and
                arrival_notice["private_payload"].get("message_id") in arrival_read_ids)
            checks["case_closed"] = evaluation_store.get_case(run_id, run["generation"], run["manifest"]["case_id"])["visible"]["status"] == "closed"
            receipt_action = next((entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "approval_action_completed"
                and entry["private_record"].get("operation") == "create_goods_receipt"), None)
            receipt_id = receipt_action.get("result", {}).get("receipt_id") if receipt_action else None
            shipping_action = next((entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "approval_action_completed"
                and entry["private_record"].get("operation") == "commit_expedited_shipping"), None)
            shipping_po_id = (shipping_action or {}).get("result", {}).get("purchase_order_id")
            case_state = evaluation_store.get_case(run_id, run["generation"], run["manifest"]["case_id"])["visible"]
            invoice_id = _document_id(docs.get("purchase_invoice"))
            native_invoice = adapter.get_purchase_invoice(invoice_id)
            payment_grants = [entry["private_record"] for entry in audit
                if entry["private_record"].get("event") == "approval_decision"
                and entry["private_record"].get("decision") == "approved"
                and entry["private_record"].get("operation") == "post_payment"
                and entry["private_record"].get("approved_by") == "manager"
                and entry["private_record"].get("origin") == "simulation_policy"
                and entry["private_record"].get("policy_version") == run["manifest"].get("private_scenario_policy", _SCENARIO_POLICY)["version"]]
            valid_payment_grant = any(
                grant.get("entity_id") == invoice_id
                and grant.get("entity_version") == _native_version(native_invoice)
                and grant.get("currency") == native_invoice.get("currency")
                and Decimal(str(grant.get("amount", "-1"))) == Decimal(str(native_invoice.get("outstanding_amount", native_invoice.get("grand_total", "-2"))))
                and receipt_action is not None
                and self._time(grant["simulated_time"]) >= self._time(receipt_action["simulated_time"])
                and self._time(grant["simulated_time"]) <= self._time(run["manifest"].get("initial_time", run["simulated_time"])) + timedelta(days=14) - timedelta(microseconds=1)
                for grant in payment_grants)
            checks["payment_approval_granted"] = valid_payment_grant and not bool(adapter.read_payment_status(invoice_id).get("payments"))
            checks["final_stakeholder_email_confirmed"] = any(
                item["status"] == "completed"
                and item["private_payload"].get("from_actor_id") == run["agent_id"]
                and item["private_payload"].get("to_actor_id") == "requester"
                and item["private_payload"].get("case_id") == run["manifest"]["case_id"]
                and receipt_id is not None
                and receipt_id in item["private_payload"].get("body", "")
                and shipping_po_id is not None
                and shipping_po_id in item["private_payload"].get("body", "")
                and "not posted a freight invoice" in item["private_payload"].get("body", "").casefold()
                and _document_id(docs.get("purchase_order")) in item["private_payload"].get("body", "")
                and case_state.get("status") == "closed"
                and valid_payment_grant
                and self._time(item["private_payload"].get("simulated_time", run["simulated_time"]))
                    >= min((self._time(grant["simulated_time"]) for grant in payment_grants
                            if grant.get("entity_id") == invoice_id), default=self._time(run["simulated_time"]))
                and self._time(item["private_payload"].get("simulated_time", run["simulated_time"]))
                    >= self._time(receipt_action["simulated_time"])
                for item in evaluation_store.list_outbox(run_id, run["generation"])
            )
        customer_domain_evaluation = None
        problem_evaluation = None
        if scenario in self.problem_adapters:
            prepared = PreparedProblem.model_validate(run["manifest"]["prepared_problem"])
            customer_ids = {actor["actor_id"] for actor in evaluation_store.list_actors(run_id, run["generation"])
                            if actor["kind"] == "customer"}
            messages_by_id: dict[str, tuple[datetime, str, dict[str, str]]] = {}
            for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed"):
                payload = item["private_payload"]
                message_id = payload.get("message_id")
                if not message_id:
                    continue
                if (payload.get("from_actor_id") in customer_ids
                        and payload.get("to_actor_id") in {prepared.support_actor_id, run["agent_id"]}):
                    messages_by_id[message_id] = (
                        self._time(payload.get("simulated_time", run["simulated_time"])), item["outbox_id"],
                        {"role": "user", "content": payload["body"]})
                elif (payload.get("from_actor_id") in {prepared.support_actor_id, run["agent_id"]}
                      and payload.get("to_actor_id") in customer_ids):
                    messages_by_id[message_id] = (
                        self._time(payload.get("simulated_time", run["simulated_time"])), item["outbox_id"],
                        {"role": "assistant", "content": payload["body"]})
            for record in evaluation_store.list_private_messages(run_id, run["generation"]):
                if not record.get("message_id"):
                    continue
                sender, recipient = record.get("from_actor_id"), record.get("to_actor_id")
                if sender in customer_ids and recipient == prepared.support_actor_id:
                    role = "user"
                elif sender in {prepared.support_actor_id, run["agent_id"]} and recipient in customer_ids:
                    role = "assistant"
                else:
                    continue
                messages_by_id[record["message_id"]] = (
                    self._time(record.get("simulated_time", run["simulated_time"])),
                    f"received:{record['message_id']}",
                    {"role": role, "content": str(record.get("body", ""))})
            evaluation_trajectory = [message for _, _, message in sorted(
                messages_by_id.values(), key=lambda row: (row[0], row[1]))]
            captured = getattr(self._evaluation_local, "capture", None)
            frozen = captured.get("native", {}).get("evidence") if captured else None
            evaluate_captured = getattr(self.problem_adapters[scenario], "evaluate_captured", None)
            if frozen is not None and callable(evaluate_captured):
                problem_evaluation = evaluate_captured(
                    prepared=prepared, trajectory=evaluation_trajectory, captured=frozen,
                )
            else:
                problem_evaluation = self.problem_adapters[scenario].evaluate(
                    prepared=prepared, trajectory=evaluation_trajectory,
                )
            native_evaluation = problem_evaluation.get("evaluation", {})
            checks["tau_reward_complete"] = (
                native_evaluation.get("reward_status") == "complete"
                and native_evaluation.get("reward") == 1.0
            )

        if scenario in {"E1", "S01"}:
            customer = next((actor for actor in evaluation_store.list_actors(run_id, run["generation"])
                             if actor["kind"] == "customer"), None)
            if customer is not None and customer["private_state"].get("tau2_task"):
                if self.actor_turn_client is None:
                    raise ActorTurnUncertain("τ-bench customer evaluator worker is not configured")
                trajectory: list[tuple[datetime, str, dict[str, Any]]] = []
                for item in evaluation_store.list_outbox(run_id, run["generation"], status="completed"):
                    payload = item["private_payload"]
                    if payload.get("from_actor_id") == customer["actor_id"] and payload.get("to_actor_id") == run["agent_id"]:
                        trajectory.append((self._time(payload.get("simulated_time", run["simulated_time"])),
                            item["outbox_id"], {"role": "user", "content": payload["body"]}))
                    elif payload.get("from_actor_id") == run["agent_id"] and payload.get("to_actor_id") == customer["actor_id"]:
                        trajectory.append((self._time(payload.get("simulated_time", run["simulated_time"])),
                            item["outbox_id"], {"role": "assistant", "content": payload["body"],
                                "tool_calls": [{"id": item["outbox_id"], "name": "send_message",
                                                "arguments": {"to_actor_id": customer["actor_id"]}}]}))
                expected_order_id = customer["private_state"].get("order_id")
                for entry in audit:
                    record = entry["private_record"]
                    if (record.get("event") == "erp_read"
                            and record.get("actor_id") == run["agent_id"]
                            and record.get("operation") == "get_sales_order"
                            and record.get("entity_id") == expected_order_id):
                        trajectory.append((self._time(record.get("simulated_time", run["simulated_time"])),
                            f"erp-read:{record['entity_id']}:{record.get('simulated_time', '')}",
                            {"role": "assistant", "tool_calls": [{
                                "name": "get_sales_order", "arguments": {"entity_id": record["entity_id"]},
                            }]}))
                request = ActorEvaluateRequest(
                    run_id=str(run_id), generation=run["generation"],
                    actor_id=customer["actor_id"], task=customer["private_state"]["tau2_task"],
                    trajectory=[message for _, _, message in sorted(trajectory, key=lambda row: (row[0], row[1]))],
                )
                try:
                    customer_domain_evaluation = self.actor_turn_client.evaluate(request).model_dump(mode="json")
                except ActorWorkerError as exc:
                    raise ActorTurnUncertain(f"τ-bench customer evaluation failed ({exc.code})") from exc

        passed = bool(checks) and all(checks.values())
        current_environment_provider = getattr(self, "environment_manifest_provider", None)
        current_environment = current_environment_provider() if callable(current_environment_provider) else None
        recorded_environment = run["manifest"].get("environment")
        environment_drift = (current_environment != recorded_environment
                             if current_environment is not None and recorded_environment is not None else None)
        result = {"run_id": str(run_id), "scenario_id": scenario, "infrastructure": "connected",
                "agent_performance": "pass" if passed else "incomplete", "checks": checks,
                "environment_drift": environment_drift, "runtime_environment": current_environment,
                "customer_domain_evaluation": customer_domain_evaluation,
                "metrics": evaluation_metrics(checks=checks, audit=audit, run=run)}
        if problem_evaluation is not None:
            result["problem_evaluation"] = problem_evaluation
        if evaluation_store.is_company_experiment(run_id):
            peers = [item["experiment_id"] for item in evaluation_store.company_experiments("company")
                     if item["experiment_id"] != str(run_id)]
            result["evaluation_context"] = {
                "mode": "shared_company",
                "shared_erp_site": run["erp_binding"].get("site_name"),
                "other_experiments": peers,
                "native_state_may_include_cross_experiment_effects": bool(peers),
                "criteria": "original scenario checks applied to current shared native ERP state",
            }
        feedback_reader = getattr(self, "company_feedback_summary", None)
        if evaluation_store.is_company_experiment(run_id) and callable(feedback_reader):
            if getattr(self._evaluation_local, "capture", None) is not None:
                if self._evaluation_local.feedback is not None:
                    result["company_feedback"] = deepcopy(self._evaluation_local.feedback)
            else:
                result["company_feedback"] = feedback_reader(str(run_id))
            native_status = (problem_evaluation or {}).get("evaluation", {}).get("reward_status")
            if result["company_feedback"]["attribution"] == "inconclusive" or native_status == "inconclusive":
                result["agent_performance"] = "inconclusive"
            elif native_status == "partial":
                result["agent_performance"] = "unevaluated"
        return result


def _safe_agent_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    """Keep runtime knobs and hashes, never persist prompt/tool/credential bodies."""
    sensitive_terms = ("prompt", "instruction", "tool", "secret", "password", "credential", "token",
                       "api_key", "access_key", "private_key", "authorization", "auth",
                       "body", "content", "message", "conversation", "history", "payload", "raw")
    credential_terms = ("secret", "password", "credential", "token", "api_key", "access_key",
                        "private_key", "authorization", "auth")

    def digest(value: Any) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(canonical.encode("utf-8")).hexdigest()

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for child_key, child in value.items():
                key = str(child_key)
                if any(term in key.casefold() for term in sensitive_terms):
                    lowered = key.casefold()
                    if any(term in lowered for term in credential_terms):
                        result[f"{key}_redacted"] = True
                    else:
                        digest_key = key if key.endswith("_sha256") else f"{key}_sha256"
                        is_digest = (key.endswith("_sha256") and isinstance(child, str)
                                     and re.fullmatch(r"[0-9a-fA-F]{64}", child) is not None)
                        result[digest_key] = child if is_digest else digest(child)
                else:
                    result[key] = clean(child)
            return result
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value

    return clean(configuration)


def _document_id(document: Any) -> str | None:
    if isinstance(document, str):
        return document
    if isinstance(document, dict):
        return document.get("name") or document.get("id")
    return None


def _native_version(document: dict[str, Any]) -> str | None:
    """Use only version metadata supplied by the native ERP document."""
    value = document.get("version") or document.get("modified")
    return None if value is None else str(value)


def _parse_approval_request(body: str) -> tuple[dict[str, str] | None, str | None]:
    """Parse a narrow, auditable email request; prose claims are never grants."""
    lines = body.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "APPROVAL REQUEST":
        return None, "Please send a structured APPROVAL REQUEST with operation and native entity version."
    accepted = {"operation", "entity_id", "entity_version", "case_id", "quantity", "amount", "currency", "quote_message_id", "delivery_commitment"}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-z_]+)=([^\r\n]+)", line.strip())
        if match is None or match.group(1) not in accepted or match.group(1) in fields:
            return None, "The approval request fields are invalid or duplicated."
        fields[match.group(1)] = match.group(2).strip()
    required = {"operation", "entity_id", "entity_version", "case_id"}
    if not required.issubset(fields):
        return None, "The approval request needs operation, entity_id, entity_version and case_id."
    if fields["operation"] not in {"create_goods_receipt", "post_payment", "amend_purchase_order", "approve_expedited_shipping"}:
        return None, "The requested operation is outside the finance manager's scenario policy."
    if not all(fields[name] for name in required):
        return None, "The approval request contains an empty required field."
    try:
        request = ApprovalRequestEmail.model_validate(fields)
    except ValidationError:
        return None, "The approval request fields do not match the required scenario format."
    return request.model_dump(mode="json", exclude_none=True), None


def _message_id(run_id: str, generation: int, operation_id: str) -> str:
    digest = sha256(f"{run_id}:{generation}:{operation_id}".encode()).hexdigest()[:32]
    return f"<tac-{digest}@simulation.local>"


