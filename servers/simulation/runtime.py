"""Private host runtime; the agent never receives these configuration files."""
from __future__ import annotations

import hashlib
import importlib.metadata
import imaplib
import json
import logging
import os
import queue
from pathlib import Path
import subprocess
import sqlite3
import threading
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

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
from .clock_rate import InvalidKernelVirtualTimeRate, kernel_virtual_time_rate
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
    sqlite_busy_timeout_seconds: float = Field(default=30, gt=0)
    mailbox_idle_renewal_seconds: float = Field(default=30, gt=0)
    company_clock_observation_seconds: float = Field(default=1, gt=0)
    erp_compose: Path
    erp_base_url: str = "http://192.0.2.10:8130"
    erp_credentials_root: Path
    retail_operator_users: list[str] = Field(default_factory=list, min_length=0)
    company_binding: dict[str, Any] | None = None
    company_profile_file: Path = Path(__file__).resolve().parents[2] / "deploy/single-host/company-identities.json"
    company_application_services_file: Path = Path("/etc/company-workplace/application-services.json")
    company_pack_credentials_file: Path = Path("/etc/company-workplace/secrets/midpoint-connector-secrets.json")
    coffee_handoff_delivery: Literal["browser", "api"] = "browser"
    experiment_chat_delivery: Literal["browser", "api"] = "browser"
    employee_chat_credentials_file: Path | None = None
    rocketchat_base_url: str = "https://chat.company.example.test"
    background_model_id: str = "gpt-6-luna"
    company_background_enabled: bool = True
    shared_company_clock_enabled: bool = True
    shared_company_clock_file: Path = Path("/data/theagentcompany/enterprise/state/company-clock/time")
    shared_company_clock_state: Path | None = None
    kernel_company_clock_enabled: bool = False
    kernel_company_clock_url: str = "http://10.0.2.2:8133"
    kernel_company_clock_token_file: Path = Path("/run/secrets/clock-api-token")
    employee_cua_driver_file: Path = Path("/data/theagentcompany/enterprise/tools/cua-driver/bin/cua-driver")
    employee_cua_inference_url: str = "http://10.0.2.2:8143"
    employee_cua_inference_token_file: Path = Path("/data/theagentcompany/enterprise/actors/secrets/codex-inference-token")
    employee_cua_model: str = "gpt-6.1-terra"
    employee_desktop_view_only: bool = True
    employee_desktop_image: str | None = None
    employee_desktop_ca_file: Path | None = None
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


class KernelCompanyClockConfigurationError(RuntimeError):
    """The running guest does not satisfy the native kernel clock contract."""


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


def employee_cua_model() -> Any:
    from pydantic import SecretStr

    from .codex_remote import CodexRemoteModel, CodexRemoteSettings

    config_path = Path(os.environ.get(
        "SIMULATION_CONFIG", str(RuntimeConfig.model_fields["issue_config_path"].default)
    ))
    config = RuntimeConfig.model_validate(private_json(config_path))
    url = config.employee_cua_inference_url
    token_file = config.employee_cua_inference_token_file
    model_name = config.employee_cua_model
    token = private_secret(token_file)
    return CodexRemoteModel(CodexRemoteSettings(
        url=url,
        bearer_token=SecretStr(token),
        model=model_name,
        reasoning_effort="low",
    ))


def employee_desktop_manager(config: RuntimeConfig | None = None):
    """Use the coordinator's private settings for host-local employee browsers."""
    from .employee_desktops import (
        DEFAULT_SELKIES_IMAGE,
        EMPLOYEE_DESKTOP_CA_FILE_ENV,
        EMPLOYEE_DESKTOP_IMAGE_ENV,
        EmployeeDesktopRuntime,
    )
    from .employee_desktop_proxy import CaddyEmployeeDesktopProxy

    if config is None:
        config_path = os.environ.get(
            "SIMULATION_CONFIG", str(RuntimeConfig.model_fields["issue_config_path"].default))
        config = RuntimeConfig.model_validate(private_json(Path(config_path)))
    desktop_image = os.environ.get(EMPLOYEE_DESKTOP_IMAGE_ENV, "").strip() or config.employee_desktop_image
    desktop_ca_file = os.environ.get(EMPLOYEE_DESKTOP_CA_FILE_ENV, "").strip() or config.employee_desktop_ca_file
    manager = EmployeeDesktopRuntime(
        config.company_profile_file, config.state_root / "employee-desktops",
        caddy_proxy=CaddyEmployeeDesktopProxy(),
        image=desktop_image or DEFAULT_SELKIES_IMAGE,
        view_only=config.employee_desktop_view_only,
        cua_driver_file=config.employee_cua_driver_file,
        ca_file=Path(desktop_ca_file) if desktop_ca_file else None,
    )
    manager.start_idle_shutdown()
    return manager


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
                     ("fastapi", "pydantic", "simpy", "numpy", "requests", "IMAPClient")},
        "actor_mode": "upstream_model_backed",
        "deterministic_components": ["simpy", "ecommercebench_negotiation", "ecommercebench_market"],
        "agent_execution": "not_measured",
    }


class MailWorkers:
    """Native mailbox notifications wake one coalescing run processor.

    No worker advances simulation time. A failed mailbox watcher is reported
    and stopped; its siblings and unrelated API operations remain available.
    Transient IMAP transport/authentication failures reconnect after the next
    listener renewal interval; programming/configuration failures remain visible.
    """
    def __init__(self, engine: SimulationEngine, actors: list[dict], *,
                 mailbox_idle_renewal_seconds: float = 30):
        self.engine = engine
        self.actors = [actor for actor in actors if actor.get("kind") != "agent"]
        self.mailbox_idle_renewal_seconds = mailbox_idle_renewal_seconds
        self.changed = threading.Event()
        self.stopped = threading.Event()
        self.errors: dict[str, str] = {}
        self.threads: list[threading.Thread] = []
        self._employee_inbox_worker_lock = threading.Lock()
        self._employee_inbox_queues: dict[str, queue.Queue[dict[str, Any] | None]] = {}
        self.employee_inbox_coalesced_events: dict[str, int] = {}


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
        component = "mailbox:" + actor["actor_id"]
        while not self.stopped.is_set():
            try:
                renewal_seconds = _company_clock_interval(
                    self.mailbox_idle_renewal_seconds, self.engine
                )
                changed = self.engine.mail.wait_for_activity(actor, timeout=renewal_seconds)
                self.errors.pop(component, None)
                if changed:
                    self.changed.set()
                    if actor.get("kind") == "employee":
                        self._enqueue_employee_inbox(actor)
            except (OSError, imaplib.IMAP4.error) as error:
                if self.stopped.is_set():
                    return
                self._failure(component, error)
                self.stopped.wait(_company_clock_interval(
                    self.mailbox_idle_renewal_seconds, self.engine
                ))
            except Exception as error:
                self._failure(component, error)
                return

    def _enqueue_employee_inbox(self, actor: dict[str, Any]) -> None:
        actor_id = actor["actor_id"]
        with self._employee_inbox_worker_lock:
            if self.stopped.is_set():
                return
            event_queue = self._employee_inbox_queues.get(actor_id)
            if event_queue is None:
                event_queue = queue.Queue(maxsize=1)
                self._employee_inbox_queues[actor_id] = event_queue
                worker = threading.Thread(
                    target=self._refresh_employee_inboxes,
                    args=(actor_id, event_queue),
                    name="simulation-employee-inbox-" + str(abs(hash(actor_id))),
                    daemon=True,
                )
                self.threads.append(worker)
                worker.start()
            try:
                event_queue.put_nowait(actor)
            except queue.Full:
                # One pending refresh observes the latest inbox state; more
                # IDLE notifications while this employee is busy are coalesced.
                self.employee_inbox_coalesced_events[actor_id] = (
                    self.employee_inbox_coalesced_events.get(actor_id, 0) + 1
                )

    def _refresh_employee_inboxes(
        self, actor_id: str, event_queue: queue.Queue[dict[str, Any] | None]
    ) -> None:
        while True:
            actor = event_queue.get()
            if actor is None:
                event_queue.task_done()
                return
            component = "employee_inbox:" + actor_id
            try:
                refreshed = self.engine.mail.refresh_employee_inbox_if_running(actor)
                if refreshed:
                    self.errors.pop(component, None)
                else:
                    self.errors.pop(component, None)
            except Exception as error:
                self._failure(component, error)
            finally:
                event_queue.task_done()
            if self.stopped.is_set():
                return


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
        if any(key.startswith("employee_inbox:") for key in self.errors):
            components["employee_inboxes"] = "failed"
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
        startup_threads = tuple(self.threads)
        for thread in startup_threads:
            thread.start()
        self.changed.set()

    def stop(self) -> None:
        self.stopped.set()
        self.changed.set()
        self.engine.mail.stop_activity_waits()
        with self._employee_inbox_worker_lock:
            inbox_queues = list(self._employee_inbox_queues.values())
        for event_queue in inbox_queues:
            try:
                event_queue.put_nowait(None)
            except queue.Full:
                # The worker will exit after finishing its already queued event.
                pass
        for thread in self.threads:
            thread.join()


class CompanyClockWorker:
    """Resume the persisted company clock and dispatch due SimPy events.

    The worker sleeps on an event and checks at a bounded one-second cadence so
    the configured shared company source advances without busy looping. A
    never-started company remains idle until an API operation wakes it.
    """
    def __init__(self, engine: SimulationEngine, *, shared_time_enabled: bool = False,
                 observation_seconds: float = 1):
        self.engine = engine
        self.observation_seconds = observation_seconds
        self.shared_time_enabled = shared_time_enabled
        self.time_provider = None
        self.wake_event = threading.Event()
        self.dispatch_event = threading.Event()
        self.stopped = threading.Event()
        self.error: str | None = None
        self.dispatch_error: str | None = None
        self.thread: threading.Thread | None = None
        self.dispatch_thread: threading.Thread | None = None
        self.on_company_time = None

    def wake(self) -> None:
        self.wake_event.set()

    def _run(self) -> None:
        while not self.stopped.is_set():
            self.wake_event.wait(timeout=_company_clock_interval(
                self.observation_seconds, self.engine
            ))
            self.wake_event.clear()
            if self.stopped.is_set():
                return
            try:
                company = self.engine.company_state()
                if company is None:
                    if self.on_company_time is not None:
                        self.on_company_time()
                    continue
                if self.shared_time_enabled and self.time_provider is None:
                    raise RuntimeError("shared company clock publisher is unavailable")
                self.engine.tick_company_clock(
                    self.time_provider() if self.time_provider is not None else None,
                )
                self.error = None
                self.dispatch_event.set()
                if self.on_company_time is not None:
                    self.on_company_time()
            except Exception as error:
                self.error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("company clock worker failed exception_type=%s stack=%s", type(error).__name__, frames)

    def _dispatch(self) -> None:
        while not self.stopped.is_set():
            self.dispatch_event.wait(timeout=_company_clock_interval(
                self.observation_seconds, self.engine
            ))
            self.dispatch_event.clear()
            if self.stopped.is_set():
                return
            try:
                self.engine.dispatch_company_events()
                self.dispatch_error = None
            except Exception as error:
                self.dispatch_error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("company event dispatcher failed exception_type=%s stack=%s",
                          type(error).__name__, frames)

    def start(self) -> None:
        if self.thread is None:
            self.dispatch_thread = threading.Thread(
                target=self._dispatch, name="simulation-company-dispatcher", daemon=True,
            )
            self.dispatch_thread.start()
            self.thread = threading.Thread(target=self._run, name="simulation-company-clock", daemon=True)
            self.thread.start()
            self.wake()

    def stop(self) -> None:
        self.stopped.set()
        self.wake_event.set()
        self.dispatch_event.set()
        if self.thread is not None:
            self.thread.join()
        if self.dispatch_thread is not None:
            self.dispatch_thread.join()


class SharedCompanyClockPublisherWorker:
    """Own the shared follow-file publisher for the app lifespan."""

    def __init__(self, clock):
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None

    def _run(self) -> None:
        try:
            self.clock.publish_forever(self.stop_event)
        except Exception as error:
            self.error = error
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            LOG.error("shared company clock publisher failed exception_type=%s stack=%s",
                      type(error).__name__, frames)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="shared-company-clock-publisher", daemon=True)
            self.thread.start()

    def now(self) -> datetime:
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        return self.clock.now()

    def set_speed(self, speed: float, idempotency_key: str) -> dict[str, float]:
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        return self.clock.set_speed(speed, idempotency_key)

    def speed(self) -> float:
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        return self.clock.speed()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        if self.thread is None or not self.thread.is_alive():
            self.clock.close()


def _company_clock_source(path: Path) -> str:
    if not path.exists():
        return "real"
    source = path.read_text(encoding="utf-8").strip()
    if source not in {"real", "shared", "kernel", "transition:real", "transition:shared"}:
        raise ValueError("persisted company clock source is invalid")
    return source


def _kernel_company_clock_speed() -> int:
    """Read the active kernel rate used to scale company-time observations."""
    try:
        return kernel_virtual_time_rate(require_kernel=True)
    except InvalidKernelVirtualTimeRate as error:
        raise KernelCompanyClockConfigurationError(
            "guest kernel virtual-time-rate capability reported an invalid rate"
        ) from error
    except (OSError, ValueError) as error:
        raise KernelCompanyClockConfigurationError(
            "guest kernel virtual-time-rate capability is unavailable"
        ) from error


def _company_clock_interval(seconds: float, engine: SimulationEngine) -> float:
    """Keep a configured real interval stable across accelerated kernel timers."""
    if getattr(engine, "company_clock_mode", None) != "kernel":
        return seconds
    return seconds * _kernel_company_clock_speed()


def _kernel_company_clock_now() -> datetime:
    """Confirm the patched guest kernel rate control is active, then read UTC."""
    _kernel_company_clock_speed()
    return datetime.now(timezone.utc)


def _persist_company_clock_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(source + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def build_app(config_path: str | Path):
    config = RuntimeConfig.model_validate(private_json(Path(config_path)))
    config.issue_config_path = Path(config_path).resolve()
    config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = SQLiteStore(config.state_root / "state.sqlite", busy_timeout_seconds=config.sqlite_busy_timeout_seconds)
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
    from .guest_kernel_clock import GuestKernelClockClient
    environment_client = GuestKernelClockClient(
        config.kernel_company_clock_url, config.kernel_company_clock_token_file, private_secret)
    engine.company_environments = environment_client
    try:
        engine.employee_desktops = employee_desktop_manager(config)
    except ValueError as error:
        LOG.error("Employee desktop configuration is invalid exception_type=%s", type(error).__name__)
        engine.employee_desktops = None
    gateway.set_employee_desktops(engine.employee_desktops)
    if engine.employee_desktops is not None or config.experiment_chat_delivery == "api":
        from .experiment_chat import ExperimentChatDispatch
        employee_chat_credentials = config.employee_chat_credentials_file or config.mail_credentials_file.with_name(
            "employee-chat-credentials.json")
        engine.experiment_chat_dispatch = ExperimentChatDispatch(
            config.company_profile_file,
            employee_chat_credentials,
            config.state_root,
            engine.employee_desktops,
            config.rocketchat_base_url,
            delivery=config.experiment_chat_delivery,
        )
    else:
        engine.experiment_chat_dispatch = None
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
    engine.company_clock_mode = "shared" if config.shared_company_clock_enabled else "real"
    evaluation_evidence_directory = config.state_root / "evaluation-evidence"
    engine.evaluation_service = build_evaluation_service(
        engine, store, evaluation_evidence_directory,
    )
    engine.viv_smtp_receipts = gateway.viv_smtp_receipts
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
            engine.resend_marketplace_handoff = issue_workflow.service.resend_marketplace_handoff
    company_packs = None
    if config.company_binding is not None:
        from .application_services import apply_company_applications
        from .company_packs import CompanyPackService
        from servers.company.owncloud import OwnCloudClient

        def pack_owncloud_factory():
            profile = json.loads(config.company_profile_file.read_text())
            owncloud = profile["company"]["background"]["owncloud"]
            credentials = private_json(config.company_pack_credentials_file)["owncloud"]
            if credentials["username"] != owncloud["service_owner"]["owncloud_uid"]:
                raise ValueError("Company pack ownCloud credentials do not match the canonical service owner")
            return OwnCloudClient(owncloud["base_url"], credentials["username"], credentials["app_password"]), owncloud

        def apply_pack_applications(applications):
            apply_company_applications(applications, config.company_application_services_file)
            engine.company_pack_configuration_error = None

        company_packs = CompanyPackService(
            config.state_root / "company-packs", engine=engine,
            native=ERPNextSiteAdmin(config.erp_compose, config.erp_credentials_root.parent),
            binding=config.company_binding,
            profile_path=config.company_profile_file, owncloud_factory=pack_owncloud_factory,
            erp_public_url=config.erp_public_url,
            apply_applications=apply_pack_applications,
        )
        engine.company_packs = company_packs
    app = create_app(engine, private_json(config.principals_file))
    workers = MailWorkers(engine, config.mail["actors"],
                          mailbox_idle_renewal_seconds=config.mailbox_idle_renewal_seconds)
    company_clock = CompanyClockWorker(engine, shared_time_enabled=config.shared_company_clock_enabled,
                                       observation_seconds=config.company_clock_observation_seconds)
    engine.company_clock_wake = company_clock.wake
    shared_clock_publisher = None
    guest_kernel_clock = None
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
            "enabled": config.company_background_enabled, "available": False, "initialized": False,
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

    feedback_workflow = None
    feedback_configuration_error = None
    if config.company_binding is not None and config.actor_workers.supplier_url is not None:
        from .company_feedback_runtime import CompanyFeedbackRuntime
        try:
            feedback_workflow = CompanyFeedbackRuntime(
                config, store, private_json(config.principals_file), worker=background_worker,
                provider_mail_observer=provider_mail_observer, wake_handoffs=workers.changed.set,
            )
            engine.company_feedback = feedback_workflow
            engine.company_native_message_bridge = feedback_workflow.deliver_native_messages
            engine.company_feedback_activity = feedback_workflow.activity
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
        nonlocal shared_clock_publisher, guest_kernel_clock
        if config.kernel_company_clock_enabled:
            _kernel_company_clock_now()
        initialize_company_runtime(engine, config.company_binding)
        shared_clock_publisher = None
        engine.company_time_provider = None
        engine.company_clock_speed_setter = None
        engine.company_clock_speed_reader = None
        mode_path = config.state_root / "company-clock-source"
        desired_clock_source = "shared" if config.shared_company_clock_enabled else "real"
        kernel_mode_ready = False
        try:
            previous_clock_source = _company_clock_source(mode_path)
            company = engine.company_state()
            was_shared = previous_clock_source in {"shared", "transition:shared", "transition:real"}
            was_kernel = previous_clock_source == "kernel"
            if config.kernel_company_clock_enabled:
                if (company is not None and
                        engine._time(company["simulated_time"]) > datetime.now(timezone.utc)):
                    raise KernelCompanyClockConfigurationError(
                        "guest kernel time precedes persisted company time; seed the guest at or after company time"
                    )
                kernel_now = lambda: datetime.now(timezone.utc)
                # Persisting the source is part of clock-state initialization. If
                # it fails, use the existing unavailable-source handling below.
                _persist_company_clock_source(mode_path, "kernel")
                kernel_mode_ready = True
                engine.company_time_provider = kernel_now
                engine.company_clock_mode = "kernel"
                company_clock.time_provider = kernel_now
                company_clock.shared_time_enabled = False
                # Kernel time already includes the accelerated elapsed time. Do not
                # recover or rebase the previous shared-clock epoch here.
                try:
                    from .guest_kernel_clock import GuestKernelClockClient
                    guest_kernel_clock = GuestKernelClockClient(
                        config.kernel_company_clock_url, config.kernel_company_clock_token_file,
                        private_secret,
                    )
                    engine.company_clock_speed_setter = guest_kernel_clock.set_speed
                    engine.company_clock_speed_reader = guest_kernel_clock.speed
                except Exception as control_error:
                    engine.company_clock_speed_setter = None
                    engine.company_clock_speed_reader = None
                    engine.company_clock_control_error = type(control_error).__name__
                    frames = [(frame.filename, frame.lineno, frame.name)
                              for frame in traceback.extract_tb(control_error.__traceback__)]
                    LOG.error("kernel company clock speed control unavailable exception_type=%s stack=%s",
                              type(control_error).__name__, frames)
            elif desired_clock_source == "shared":
                from .shared_clock import SharedCompanyClock
                transition_instant = None
                if company is not None and not was_shared:
                    # Reserve the elapsed real-time interval before moving the
                    # anchor into the shared source's epoch domain.
                    real_now = datetime.now(timezone.utc)
                    real_anchor = engine._time(company["wall_anchor"])
                    # ``wall_anchor`` may be in the future after a source
                    # correction; cap elapsed time there as tick does.
                    transition_instant = engine._time(company["simulated_time"]) + (
                        max(real_now, real_anchor) - real_anchor
                    )
                initial_instant = transition_instant
                if initial_instant is None:
                    initial_instant = (engine._time(company["simulated_time"]) if company is not None
                                       else None) if was_shared else datetime.now(timezone.utc)
                clock_state_path = config.shared_company_clock_state or (config.state_root / "company-clock.json")
                shared_clock = SharedCompanyClock(
                    clock_state_path, config.shared_company_clock_file,
                    initial_instant=initial_instant,
                    reset_to_initial=not was_shared,
                    require_existing_state=was_shared,
                )
                shared_clock_publisher = SharedCompanyClockPublisherWorker(shared_clock)
                engine.company_time_provider = shared_clock_publisher.now
                engine.company_clock_speed_setter = shared_clock_publisher.set_speed
                engine.company_clock_speed_reader = shared_clock_publisher.speed
                engine.company_clock_mode = "shared"
                company_clock.time_provider = shared_clock_publisher.now
                _persist_company_clock_source(mode_path, "transition:shared")
                if company is not None:
                    shared_now = shared_clock.now()
                    engine.store.update_company(
                        "company", simulated_time=shared_now, wall_anchor=shared_now,
                    )
                _persist_company_clock_source(mode_path, "shared")
                shared_clock_publisher.start()
            elif was_shared:
                # Recover the shared epoch through the same durable publisher
                # state, then atomically rebase both persisted time fields onto
                # the real clock so no interval is replayed or discarded.
                from .shared_clock import SharedCompanyClock
                clock_state_path = config.shared_company_clock_state or (config.state_root / "company-clock.json")
                shared_clock = SharedCompanyClock(
                    clock_state_path, config.shared_company_clock_file,
                    initial_instant=(engine._time(company["simulated_time"]) if company is not None else None),
                    require_existing_state=True,
                )
                try:
                    shared_now = shared_clock.now()
                    _persist_company_clock_source(mode_path, "transition:real")
                    if company is not None:
                        real_now = datetime.now(timezone.utc)
                        engine.store.update_company(
                            "company", simulated_time=shared_now, wall_anchor=real_now,
                        )
                finally:
                    shared_clock.close()
                _persist_company_clock_source(mode_path, "real")
                engine.company_clock_mode = "real"
            elif was_kernel:
                if company is not None:
                    engine.store.update_company(
                        "company", wall_anchor=datetime.now(timezone.utc),
                    )
                _persist_company_clock_source(mode_path, "real")
                engine.company_clock_mode = "real"
            else:
                engine.company_clock_mode = "real"
        except Exception as error:
            if config.kernel_company_clock_enabled and kernel_mode_ready:
                # A speed-control/configuration failure must not stop local guest
                # time from advancing the company runtime.
                kernel_now = lambda: datetime.now(timezone.utc)
                engine.company_time_provider = kernel_now
                engine.company_clock_speed_setter = None
                engine.company_clock_speed_reader = None
                engine.company_clock_mode = "kernel"
                company_clock.time_provider = kernel_now
                company_clock.shared_time_enabled = False
                company_clock.error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("kernel company clock control initialization failed exception_type=%s stack=%s",
                          type(error).__name__, frames)
            else:
                def unavailable_company_time(failure=error):
                    raise RuntimeError("configured company clock source is unavailable") from failure

                engine.company_time_provider = unavailable_company_time
                engine.company_clock_speed_setter = None
                engine.company_clock_speed_reader = None
                company_clock.time_provider = unavailable_company_time
                company_clock.shared_time_enabled = True
                company_clock.error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("company clock source initialization failed exception_type=%s stack=%s",
                          type(error).__name__, frames)
                if isinstance(error, KernelCompanyClockConfigurationError):
                    LOG.error("kernel company clock configuration failed: %s", error)
        if company_packs is not None:
            try:
                company_packs.reconcile_applications()
            except Exception as error:
                engine.company_pack_configuration_error = type(error).__name__
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("company pack application reconciliation failed exception_type=%s stack=%s",
                          type(error).__name__, frames)
        if config.workers_enabled:
            workers.start()
            company_clock.start()
            if company_background is not None:
                company_background.start()
        if company_packs is not None:
            company_packs.start()
        try:
            yield
        finally:
            if company_packs is not None:
                company_packs.signal_stop()
            # Stop new work in every worker before joining any of them. This
            # prevents ongoing producers from starving a draining DB operation.
            workers.stopped.set()
            workers.changed.set()
            company_clock.stopped.set()
            company_clock.wake()
            if company_background is not None:
                company_background.stopped.set()
                company_background.wake()
            if company_packs is not None:
                company_packs.stop()
            workers.stop()
            if company_background is not None:
                company_background.stop()
            company_clock.stop()
            if shared_clock_publisher is not None:
                shared_clock_publisher.stop()
            if guest_kernel_clock is not None:
                guest_kernel_clock.close()
            environment_client.close()
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
        if getattr(engine, "company_pack_configuration_error", None):
            components["company_pack_applications"] = engine.company_pack_configuration_error
        if config.company_binding is not None and provider_experiments_unavailable:
            components['provider_experiments'] = provider_experiments_unavailable['code']
        if feedback_configuration_error:
            components["company_feedback"] = feedback_configuration_error
        if feedback_workflow is not None:
            if feedback_workflow.error:
                components["company_feedback"] = feedback_workflow.error
            with store.transaction(write=False) as connection:
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
        if getattr(engine, "company_clock_control_error", None):
            components["company_clock_speed_control"] = engine.company_clock_control_error
        if shared_clock_publisher is not None:
            if shared_clock_publisher.error is not None:
                components["shared_company_clock_publisher"] = type(shared_clock_publisher.error).__name__
            else:
                try:
                    shared_clock_publisher.speed()
                except Exception as error:
                    components["shared_company_clock_publisher"] = type(error).__name__
        if company_clock.dispatch_error:
            components["company_event_dispatch"] = company_clock.dispatch_error
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
