"""Private host runtime; the agent never receives these configuration files."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sqlite3
import threading
import traceback
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .actor_client import ActorTurnClient, ActorWorkerSettings
from .app import create_app
from .background_evaluation import (
    BackgroundEvaluationService,
    background_evaluation_guard,
    coffee_evaluation_provider,
    ecommerce_evaluation_provider,
)
from .background_runtime import BackgroundWorkerClient
from .company_background import CompanyBackgroundService
from .engine import SimulationEngine
from .evaluation_runtime import build_evaluation_service
from .erpnext import ERPNextAdapter, ERPNextSiteAdmin
from .erpnext import ERPNextRetailAdmin
from .mail import MailuGateway
from .problem_adapters import TauRetailProblemAdapter
from .store import SQLiteStore

LOG = logging.getLogger(__name__)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    state_root: Path
    erp_compose: Path
    erp_base_url: str = "http://192.0.2.10:8130"
    erp_credentials_root: Path
    retail_operator_users: list[str] = Field(default_factory=list, min_length=0)
    company_binding: dict[str, Any] | None = None
    company_profile_file: Path = Path(__file__).resolve().parents[2] / "deploy/single-host/company-identities.json"
    background_model_id: str = "gpt-6-luna"
    company_background_enabled: bool = True
    company_issues_enabled: bool = True
    issue_config_path: Path = Path("/data/theagentcompany/enterprise/config/runtime.json")
    erp_public_url: str = "https://erp.company.example.test"
    mail: dict[str, Any]
    mail_credentials_file: Path
    principals_file: Path
    workers_enabled: bool = True
    actor_workers: ActorWorkerSettings = Field(default_factory=ActorWorkerSettings)
    support_actor_id: str = "requester"
    support_address: str = "support@example.com"
    host: str = "192.0.2.10"
    port: int = Field(default=8131, gt=0, le=65535)


class ERPServiceCredentials(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    site_name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    receipt_api_key: str = Field(min_length=1)
    receipt_api_secret: str = Field(min_length=1)
    shipping_api_key: str = Field(min_length=1)
    shipping_api_secret: str = Field(min_length=1)


def private_json(path: Path) -> dict:
    if path.stat().st_mode & 0o077:
        raise PermissionError("Runtime configuration must be private to its owner")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Runtime configuration must contain a JSON object")
    return value


def private_secret(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise PermissionError("Runtime secret file must be private to its owner")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("Runtime secret file is empty")
    return value


def _provider_case_employee(profile: dict[str, Any], mail_config: dict[str, Any],
                            mail_credentials: dict[str, Any]) -> dict[str, str]:
    company = profile.get("company")
    employee_id = company.get("providerCaseResponsibleEmployeeId") if isinstance(company, dict) else None
    employees = profile.get("employees")
    employee = next((row for row in employees if isinstance(row, dict) and row.get("id") == employee_id), None)
    if not isinstance(employee, dict) or not isinstance(employee.get("email"), str):
        raise ValueError("provider case responsible employee is not configured in the company profile")
    email = employee["email"]
    actor = next((row for row in mail_config.get("actors", [])
                  if isinstance(row, dict) and isinstance(row.get("email"), str)
                  and row["email"].casefold() == email.casefold()), None)
    if actor is None or actor.get("actor_id") == "viv":
        raise ValueError("provider case employee is not a managed company mailbox")
    if not any(isinstance(key, str) and key.casefold() == email.casefold() for key in mail_credentials):
        raise ValueError("provider case employee mailbox credentials are unavailable")
    return {"actor_id": str(actor["actor_id"]), "email": email, "employee_id": employee_id}


def _erp_service_credentials(path: Path) -> ERPServiceCredentials:
    try:
        return ERPServiceCredentials.model_validate(private_json(path))
    except ValidationError as error:
        raise RuntimeError(
            "ERP credential file is missing required coordinator, receipt-writer, or shipping-writer credentials"
        ) from error


def initialize_company_runtime(engine: SimulationEngine, binding: dict[str, Any] | None) -> dict[str, Any] | None:
    """Ensure the configured company metadata exists without provisioning ERP or experiments."""
    if binding is None:
        return None
    return engine.start_company(binding, "configured-company-v1")


def environment_manifest() -> dict:
    root = Path(__file__).resolve().parents[2]
    files = sorted(Path(__file__).parent.glob("*.py"))
    # Actor behavior depends on upstream data and locks as well as Python code.
    # Exclude generated environments/caches rather than hashing installed files.
    for source in (root / "third_party/ecommercebench", root / "third_party/coffeebench",
                   Path(__file__).parent / "tau2_actor_runtime"):
        files += sorted(path for path in source.rglob("*") if path.is_file()
                        and path.suffix in {".py", ".json", ".yaml", ".yml", ".csv", ".toml", ".lock", ".txt"}
                        and not {".venv", "__pycache__", ".git"}.intersection(path.parts))
    files.append(root / "servers/company/scenarios.py")
    deployment = root / "deploy/enterprise-simulation"
    for pattern in ("*.py", "*.yml", "*.service"):
        files += sorted(deployment.rglob(pattern))
    requirements = Path(__file__).parent / "requirements.txt"
    if requirements.is_file():
        files.append(requirements)
    source_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in files}
    source_revision = None
    source_dirty = None
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
        )
    except FileNotFoundError:
        revision = None
    if revision is not None and revision.returncode == 0:
        source_revision = revision.stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        )
        source_dirty = bool(status.stdout.strip())
    source_tree_sha256 = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "source_sha256": source_hashes,
        "source_tree_sha256": source_tree_sha256,
        "source_revision": source_revision,
        "source_dirty": source_dirty,
        "packages": {name: importlib.metadata.version(name) for name in
                     ("fastapi", "pydantic", "simpy", "numpy", "requests", "IMAPClient", "signalrcore")},
        "actor_mode": "upstream_model_backed",
        "deterministic_components": ["simpy", "ecommercebench_negotiation", "ecommercebench_market"],
        "agent_execution": "not_measured",
    }


class MailWorkers:
    """Native mailbox notifications wake one coalescing run processor.

    No worker advances simulation time. A failed mailbox watcher is reported
    and stopped; its siblings and unrelated API operations remain available.
    Restarting the service after repairing that provider restarts its watcher.
    """
    def __init__(self, engine: SimulationEngine, actors: list[dict], *, hub_connection_factory=None,
                 viv_mailbox_name: str = "Default"):
        self.engine = engine
        self.actors = [actor for actor in actors if actor.get("kind") != "agent"]
        self.viv_mailbox_name = viv_mailbox_name
        self._hub_connection_factory = hub_connection_factory
        self.changed = threading.Event()
        self.stopped = threading.Event()
        self.errors: dict[str, str] = {}
        self.threads: list[threading.Thread] = []
        self._hub_connection = None
        self._hub_closed = threading.Event()
        self._hub_connected = threading.Event()
        self._viv_activity_condition = threading.Condition()
        self._viv_activity_sequence = 0

    def viv_activity_version(self) -> int:
        with self._viv_activity_condition:
            return self._viv_activity_sequence

    def wait_for_viv_activity(self, after_version: int, timeout: float = 30) -> bool:
        """Wait for a native Viv mailbox notification newer than a cursor."""
        with self._viv_activity_condition:
            notified = self._viv_activity_condition.wait_for(
                lambda: self._viv_activity_sequence > after_version or self.stopped.is_set(),
                timeout=timeout,
            )
            return notified and self._viv_activity_sequence > after_version

    def _signal_viv_activity(self) -> None:
        with self._viv_activity_condition:
            self._viv_activity_sequence += 1
            self._viv_activity_condition.notify_all()
        self.changed.set()

    def _failure(self, component: str, error: BaseException) -> None:
        self.errors[component] = type(error).__name__
        frames = [(frame.filename, frame.lineno, frame.name)
                  for frame in traceback.extract_tb(error.__traceback__)]
        causes = []
        cause = error.__cause__ or error.__context__
        while cause is not None:
            causes.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        LOG.error("simulation worker failed component=%s exception_type=%s causes=%s stack=%s",
                  component, type(error).__name__, causes, frames)

    def _watch(self, actor: dict) -> None:
        try:
            while not self.stopped.is_set():
                if self.engine.mail.wait_for_activity(actor, timeout=30):
                    self.changed.set()
        except Exception as error:
            self._failure("mailbox:" + actor["actor_id"], error)

    def _viv_notification(self, arguments: Any) -> None:
        # signalrcore 1.0.2 invokes handlers with HubMessage.arguments (a list),
        # even when the hub method has only one parameter.
        mailbox = arguments[0] if isinstance(arguments, (list, tuple)) and len(arguments) == 1 else arguments
        if isinstance(mailbox, str) and mailbox.casefold() == self.viv_mailbox_name.casefold():
            self._signal_viv_activity()

    def _watch_viv(self) -> None:
        try:
            api_base = self.engine.mail.config.smtp4dev_api_base_url.rstrip("/")
            if self._hub_connection_factory is None:
                from signalrcore.hub_connection_builder import HubConnectionBuilder

                connection = (
                    HubConnectionBuilder()
                    .with_url(api_base + "/hubs/notifications")
                    .with_automatic_reconnect({
                        "type": "raw",
                        "keep_alive_interval": 15,
                        "reconnect_interval": 5,
                        "max_attempts": 10,
                    })
                    .build()
                )
            else:
                connection = self._hub_connection_factory(api_base + "/hubs/notifications")
            self._hub_connection = connection
            connection.on("messageschanged", self._viv_notification)
            connection.on_open(self._viv_opened)
            connection.on_close(self._viv_closed_callback)
            connection.on_error(self._viv_error)
            connection.start()
            self._hub_closed.wait()
            if not self.stopped.is_set() and "smtp4dev_notifications" not in self.errors:
                self._failure("smtp4dev_notifications", ConnectionError("SignalR connection closed"))
        except Exception as error:
            if not self.stopped.is_set() and "smtp4dev_notifications" not in self.errors:
                self._failure("smtp4dev_notifications", error)

    def _viv_opened(self) -> None:
        self._hub_connected.set()
        self.errors.pop("smtp4dev_notifications", None)
        self._signal_viv_activity()

    def _viv_closed_callback(self) -> None:
        self._hub_closed.set()

    def _viv_error(self, error: Any) -> None:
        # Transport callbacks can carry provider-specific text. Keep diagnostics
        # to exception classes and stack locations only.
        self.errors["smtp4dev_notifications"] = "SignalRTransportError"
        if isinstance(error, BaseException):
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            causes = []
            cause = error.__cause__ or error.__context__
            while cause is not None:
                causes.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            LOG.error("simulation worker failed component=smtp4dev_notifications exception_type=%s causes=%s stack=%s",
                      type(error).__name__, causes, frames)
        else:
            LOG.error("simulation worker failed component=smtp4dev_notifications callback_type=%s",
                      type(error).__name__)

    def _process(self) -> None:
        while not self.stopped.is_set():
            self.changed.wait()
            self.changed.clear()
            if self.stopped.is_set():
                return
            issue_observer = getattr(self.engine, "company_issue_observe", None)
            if issue_observer is not None:
                issue_observer()
            try:
                active_runs = self.engine.store.list_active_runs()
            except Exception as error:
                self._failure("run:processor", error)
                continue
            self.errors.pop("run:processor", None)
            for run in active_runs:
                try:
                    self.engine.pump(run["run_id"])
                    self.errors.pop("run:" + run["run_id"], None)
                except Exception as error:
                    self._failure("run:" + run["run_id"], error)

            feedback_observer = getattr(self.engine, "company_feedback_observe", None)
            if feedback_observer is not None:
                feedback_observer()

    def health_components(self) -> dict[str, str]:
        components = {}
        if any(key.startswith("mailbox:") for key in self.errors):
            components["mailu_mailboxes"] = "failed"
        if "smtp4dev_notifications" in self.errors:
            components["viv_notifications"] = "failed"
        if any(key.startswith("run:") for key in self.errors):
            components["run_processor"] = "failed"
        try:
            for run in self.engine.store.list_active_runs():
                outbox = self.engine.store.list_outbox(run["run_id"], run["generation"])
                if any(item["status"] == "uncertain" or
                       (item["status"] == "pending" and item["submission_started"])
                       for item in outbox):
                    components["mail_delivery"] = "uncertain"
                    break
        except Exception as error:
            if "run:processor" not in self.errors:
                self._failure("run:processor", error)
            components["run_processor"] = "failed"
        return components

    def start(self) -> None:
        self.threads = [threading.Thread(target=self._process, name="simulation-mail-processor", daemon=True)]
        self.threads += [threading.Thread(target=self._watch, args=(actor,),
                                        name="simulation-mail-" + actor["actor_id"], daemon=True)
                         for actor in self.actors]
        if hasattr(self.engine.mail, "config") and hasattr(self.engine.mail.config, "smtp4dev_api_base_url"):
            self.threads.append(threading.Thread(target=self._watch_viv,
                                                 name="simulation-viv-notifications", daemon=True))
        for thread in self.threads:
            thread.start()
        self.changed.set()

    def stop(self) -> None:
        self.stopped.set()
        self.changed.set()
        if self._hub_connection is not None:
            try:
                self._hub_connection.stop()
            except Exception as error:
                self._failure("smtp4dev_notifications_stop", error)
        for thread in self.threads:
            thread.join(timeout=1)


class CompanyClockWorker:
    """Resume the persisted company clock and dispatch due SimPy events.

    The worker sleeps on an event and checks at a bounded one-second cadence so
    wall time remains the default company clock without busy looping. A paused
    or never-started company remains idle until an API operation wakes it.
    """
    def __init__(self, engine: SimulationEngine):
        self.engine = engine
        self.wake_event = threading.Event()
        self.stopped = threading.Event()
        self.error: str | None = None
        self.thread: threading.Thread | None = None
        self.on_company_time = None

    def wake(self) -> None:
        self.wake_event.set()

    def _run(self) -> None:
        while not self.stopped.is_set():
            self.wake_event.wait(timeout=1.0)
            self.wake_event.clear()
            if self.stopped.is_set():
                return
            try:
                company = self.engine.company_state()
                if company is None or company["paused"]:
                    if self.on_company_time is not None:
                        self.on_company_time()
                    continue
                self.engine.tick_company_clock()
                self.error = None
                if self.on_company_time is not None:
                    self.on_company_time()
            except Exception as error:
                self.error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("company clock worker failed exception_type=%s stack=%s", type(error).__name__, frames)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="simulation-company-clock", daemon=True)
            self.thread.start()
            self.wake()

    def stop(self) -> None:
        self.stopped.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


def build_app(config_path: str | Path):
    config = RuntimeConfig.model_validate(private_json(Path(config_path)))
    config.issue_config_path = Path(config_path).resolve()
    config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = SQLiteStore(config.state_root / "state.sqlite")
    gateway = MailuGateway(config.mail, private_json(config.mail_credentials_file))
    cache: dict[str, ERPNextAdapter] = {}

    def erp_factory(binding: dict) -> ERPNextAdapter:
        path = Path(binding["credentials_ref"]).resolve(strict=True)
        if not path.is_relative_to(config.erp_credentials_root.resolve()):
            raise PermissionError("ERP credential reference is outside the configured secret store")
        credentials = _erp_service_credentials(path)
        site = binding["site_name"]
        if credentials.site_name != site:
            raise PermissionError("ERP credential reference belongs to a different site")
        if site not in cache:
            cache[site] = ERPNextAdapter(
                config.erp_base_url,
                credentials.api_key,
                credentials.api_secret,
                config.state_root / (site + ".idempotency.sqlite"),
                site,
                receipt_api_key=credentials.receipt_api_key,
                receipt_api_secret=credentials.receipt_api_secret,
                shipping_api_key=credentials.shipping_api_key,
                shipping_api_secret=credentials.shipping_api_secret,
            )
        return cache[site]

    engine = SimulationEngine(store, gateway, erp_factory,
                              ERPNextSiteAdmin(config.erp_compose, config.erp_credentials_root.parent),
                              config.mail["actors"])
    engine.company_binding = config.company_binding
    engine.support_actor_id = config.support_actor_id
    engine.support_address = config.support_address
    engine.actor_turn_client = ActorTurnClient(config.actor_workers)
    from .provider_experiment_client import ProviderExperimentWorkerClient
    from .provider_experiments import ProviderExperimentStore
    provider_worker_client = ProviderExperimentWorkerClient(config.actor_workers)
    engine.provider_experiment_worker = provider_worker_client
    try:
        engine.provider_experiment_store = ProviderExperimentStore(store)
    except sqlite3.Error as error:
        engine.provider_experiment_store = None
        frames = [(frame.filename, frame.lineno, frame.name)
                  for frame in traceback.extract_tb(error.__traceback__)]
        LOG.error("provider experiment persistence unavailable exception_type=%s stack=%s",
                  type(error).__name__, frames)
    engine.problem_adapters = {
        "TAU_RETAIL": TauRetailProblemAdapter(
            engine.actor_turn_client,
            ERPNextRetailAdmin(config.erp_compose, config.erp_credentials_root.parent,
                               operator_users=config.retail_operator_users),
        ),
    }
    engine.environment_manifest = environment_manifest()
    engine.environment_manifest_provider = environment_manifest
    evaluation_evidence_directory = config.state_root / "evaluation-evidence"
    engine.evaluation_service = build_evaluation_service(
        engine, store, evaluation_evidence_directory,
    )
    issue_workflow = None
    issue_configuration_error = None
    if config.company_binding is not None and config.company_issues_enabled:
        from .issue_handoff_runtime import CompanyIssueWorkflow
        try:
            issue_workflow = CompanyIssueWorkflow(config, mail=gateway, store=store)
        except (OSError, ValueError, KeyError, TypeError) as error:
            issue_configuration_error = type(error).__name__
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            LOG.error("company issue configuration unavailable exception_type=%s stack=%s",
                      type(error).__name__, frames)
        if issue_workflow is not None:
            def issue_action(method):
                try:
                    method()
                except Exception as error:
                    issue_workflow.report_failure(error)
            engine.company_issue_reconcile = lambda: issue_action(issue_workflow.reconcile)
            engine.company_issue_observe = lambda: issue_action(issue_workflow.observe_mail)
            engine.company_issue_activity = issue_workflow.service.activity
    app = create_app(engine, private_json(config.principals_file))
    workers = MailWorkers(engine, config.mail["actors"],
                          viv_mailbox_name=config.mail.get("smtp4dev_mailbox_name", "Default"))
    company_clock = CompanyClockWorker(engine)
    engine.company_clock_wake = company_clock.wake
    background_worker = None
    if config.actor_workers.supplier_url is not None and config.actor_workers.supplier_token is not None:
        background_worker = BackgroundWorkerClient(
            config.actor_workers.supplier_url, config.actor_workers.supplier_token,
            timeout_seconds=config.actor_workers.request_timeout_seconds,
        )
    company_background = None
    background_unavailable: dict[str, str] | None = None
    if not config.company_background_enabled:
        background_unavailable = {"code": "background_disabled_by_configuration"}
    elif config.company_binding is None:
        background_unavailable = {"code": "company_binding_not_configured"}
    elif config.actor_workers.supplier_url is None or config.actor_workers.supplier_token is None:
        background_unavailable = {"code": "background_worker_not_configured"}
    else:
        site_admin = ERPNextSiteAdmin(config.erp_compose, config.erp_credentials_root.parent)
        assert background_worker is not None
        try:
            company_background = CompanyBackgroundService.from_runtime(
                engine, store=store, worker=background_worker, finance_adapter=site_admin.finance_source_adapter(),
                profile_path=config.company_profile_file, background_model_id=config.background_model_id,
                company_binding=config.company_binding,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            background_unavailable = {"code": "invalid_company_background_configuration"}
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            LOG.error("company background configuration unavailable exception_type=%s stack=%s",
                      type(error).__name__, frames)
    if company_background is not None:
        engine.company_background_status = company_background.status
        engine.company_background_activity = company_background.activity
        engine.company_background_wake = company_background.wake
        company_clock.on_company_time = company_background.wake
    else:
        engine.company_background_status = lambda: {
            "enabled": config.company_background_enabled, "available": False,
            "errors": {"configuration": background_unavailable or {"code": "not_configured"}},
            "providers": {}, "obligations": [], "obligations_error": None,
        }
        engine.company_background_activity = lambda: CompanyBackgroundService.activity_for_store(store)
    provider_experiments = None
    portal_client = None
    provider_mail_observer = None
    provider_experiments_unavailable: dict[str, str] | None = None
    try:
        portal_config_path = Path(config_path).resolve().parent / "provider-portal.json"
        portal_config = private_json(portal_config_path)
        portal_internal_url = portal_config.get("internal_url")
        portal_public_url = portal_config.get("public_url")
        if not isinstance(portal_internal_url, str) or not isinstance(portal_public_url, str):
            raise ValueError("provider portal URLs are not configured")
        profile = json.loads(config.company_profile_file.read_text(encoding="utf-8"))
        mail_credentials = private_json(config.mail_credentials_file)
        employee = _provider_case_employee(profile, config.mail, mail_credentials)
        integration_key = private_secret(Path(portal_config["integration_key_file"]))
        from .provider_experiments import ProviderExperimentService
        from .provider_portal_client import ProviderPortalClient

        if engine.provider_experiment_store is None:
            raise ValueError("provider experiment persistence is unavailable")
        if config.actor_workers.customer_url is None or config.actor_workers.customer_token is None:
            raise ValueError("native provider worker is not configured")
        portal_client = ProviderPortalClient(portal_internal_url, integration_key)
        from .provider_mail_observer import RegisteredProviderMailObserver
        from .mail import SPECIAL_VIV_ID
        provider_mail_observer = RegisteredProviderMailObserver(
            portal_client, gateway, config.mail['actors'], SPECIAL_VIV_ID,
        ).observe
        provider_experiments = ProviderExperimentService(
            store, provider_worker_client, portal_client, employee, portal_url=portal_public_url,
            experiment_store=engine.provider_experiment_store,
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        provider_experiments_unavailable = {"code": "invalid_provider_experiment_configuration"}
        frames = [(frame.filename, frame.lineno, frame.name)
                  for frame in traceback.extract_tb(error.__traceback__)]
        LOG.error("provider experiment configuration unavailable exception_type=%s stack=%s",
                  type(error).__name__, frames)
    if provider_experiments is not None:
        engine.provider_experiment_service = provider_experiments
    else:
        engine.provider_experiment_unavailable = provider_experiments_unavailable or {
            "code": "provider_experiments_unavailable"}
    engine.viv_activity_version = workers.viv_activity_version
    engine.wait_for_viv_activity = workers.wait_for_viv_activity

    feedback_workflow = None
    feedback_configuration_error = None
    if config.company_binding is not None and config.actor_workers.supplier_url is not None:
        from .company_feedback_runtime import CompanyFeedbackRuntime
        try:
            feedback_workflow = CompanyFeedbackRuntime(
                config, store, private_json(config.principals_file), worker=background_worker,
                provider_mail_observer=provider_mail_observer,
            )
            engine.company_feedback = feedback_workflow
            engine.company_native_message_bridge = feedback_workflow.deliver_native_messages
            engine.company_feedback_activity = feedback_workflow.pipeline.activity
            engine.company_feedback_summary = lambda experiment_id: feedback_workflow.pipeline.report(experiment_id,evaluate_native=False)
            def feedback_observe():
                try:
                    feedback_workflow.observe_mail()
                except Exception as error:
                    feedback_workflow.error = type(error).__name__
                    frames = [(frame.filename, frame.lineno, frame.name)
                              for frame in traceback.extract_tb(error.__traceback__)]
                    LOG.error("company evidence observation failed exception_type=%s stack=%s", type(error).__name__, frames)
            engine.company_feedback_observe = feedback_observe
        except (OSError, ValueError, KeyError, TypeError) as error:
            feedback_configuration_error = type(error).__name__
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            LOG.error("company feedback configuration failed exception_type=%s stack=%s", type(error).__name__, frames)

    background_evaluation_providers = {}
    if background_worker is not None:
        background_evaluation_providers["coffeebench"] = coffee_evaluation_provider(background_worker)
    if feedback_workflow is not None:
        ecommerce_adapter = feedback_workflow.pipeline.adapters.get("ecommercebench")
        if ecommerce_adapter is not None:
            background_evaluation_providers["ecommercebench"] = ecommerce_evaluation_provider(ecommerce_adapter)
    engine.background_evaluation_service = (
        BackgroundEvaluationService(
            store,
            evaluation_evidence_directory,
            providers=background_evaluation_providers,
            guard_factory=background_evaluation_guard(evaluation_evidence_directory),
        )
        if background_evaluation_providers else None
    )

    @asynccontextmanager
    async def lifespan(_app):
        initialize_company_runtime(engine, config.company_binding)
        if config.workers_enabled:
            workers.start()
            company_clock.start()
            if company_background is not None:
                company_background.start()
        try:
            yield
        finally:
            workers.stop()
            if company_background is not None:
                company_background.stop()
            company_clock.stop()
            engine.actor_turn_client.session.close()
            provider_worker_client.session.close()
            if portal_client is not None:
                portal_client.session.close()
            for adapter in cache.values():
                adapter.session.close()
                if adapter.receipt_session is not None:
                    adapter.receipt_session.close()
                if adapter.shipping_session is not None:
                    adapter.shipping_session.close()
            if background_worker is not None:
                background_worker.session.close()

    app.router.lifespan_context = lifespan
    app.state.engine = engine

    @app.get("/health")
    def health():
        components = workers.health_components()
        if config.company_binding is not None and provider_experiments_unavailable:
            components['provider_experiments'] = provider_experiments_unavailable['code']
        if feedback_configuration_error:
            components["company_feedback"] = feedback_configuration_error
        if feedback_workflow is not None:
            if feedback_workflow.error:
                components["company_feedback"] = feedback_workflow.error
            with store.transaction() as connection:
                outstanding = connection.execute("SELECT count(*) FROM company_feedback_actions WHERE stage IN ('uncertain','submitting') OR last_error IS NOT NULL").fetchone()[0]
                feedback_health = connection.execute("SELECT result_json FROM company_feedback_runtime_reports WHERE provider='workflow_health'").fetchone()
                provider_mail_health = connection.execute(
                    "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='component_provider_mail'"
                ).fetchone()
            if feedback_health and json.loads(feedback_health['result_json'])['status'] == 'failed':
                components["company_feedback"] = json.loads(feedback_health['result_json'])['error_type']
            if outstanding:
                components["company_feedback"] = "native_action_needs_reconciliation"
            if provider_mail_health:
                components['provider_mail'] = json.loads(provider_mail_health['result_json'])['status']
        if issue_configuration_error:
            components["company_issues"] = issue_configuration_error
        if issue_workflow is not None:
            pending = issue_workflow.service.store.pending()
            if issue_workflow.error or any(row["last_error"] or row["stage"] in ("submitting", "uncertain") for row in pending):
                components["company_issues"] = issue_workflow.error or "handoff_needs_reconciliation"
        if config.company_binding is None:
            components["company_binding"] = "NotConfigured"
        if company_clock.error:
            components["company_clock"] = company_clock.error
        if company_background is not None:
            background_status = company_background.status()
            if background_status["errors"] or background_status["obligations_error"]:
                components["company_background"] = {
                    "errors": background_status["errors"],
                    "obligations_error": background_status["obligations_error"],
                }
        elif background_unavailable is not None:
            components["company_background"] = background_unavailable
        return {"status": "degraded" if components else "ok", "components": components}

    return app, config


def main() -> None:
    import uvicorn
    app, config = build_app(os.environ["SIMULATION_CONFIG"])
    uvicorn.run(app, host=config.host, port=config.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
