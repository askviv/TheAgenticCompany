"""On-demand, per-employee Selkies browser sessions.

The coordinator is the only process allowed to use the local Docker daemon.
Desktop ports and the Chrome DevTools port bind to host loopback only. The
profile file is the identity authority: every listed employee may receive an
isolated desktop, and no identity is inferred from request data beyond its ID.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from servers.simulation.employee_desktop_proxy import CaddyEmployeeDesktopProxy


DEFAULT_SELKIES_IMAGE = "tac-selkies-desktop:2.1.0-ubuntu26.04-socat1"
MANAGED_LABEL = "tac.employee-desktop.managed"
EMPLOYEE_LABEL = "tac.employee-desktop.employee-id"
_CDP_READY = threading.Event()
_IDLE_SHUTDOWN_SECONDS = 10 * 60
_IDLE_RECONCILE_SECONDS = 60
_LOGGER = logging.getLogger(__name__)
_IDLE_COORDINATORS_LOCK = threading.Lock()
_IDLE_COORDINATORS: dict[str, "EmployeeDesktopRuntime"] = {}


class EmployeeDesktopError(RuntimeError):
    """Safe error raised when an employee desktop cannot be provisioned."""


class EmployeeDesktopImageUnavailable(EmployeeDesktopError):
    """The pinned local desktop image has not been provisioned on this host."""


class EmployeeDesktopRuntime:
    """Create/reuse durable Selkies desktops for identities in a company profile.

    Caddy terminates trusted TLS and routes the employee subfolder directly to
    the employee's private Docker bridge address. The browser password is
    returned separately and must only be sent over an authenticated
    coordinator response; it is never placed in a URL.
    """

    def __init__(
        self,
        company_profile_path: str | Path,
        state_root: str | Path,
        *,
        caddy_proxy: CaddyEmployeeDesktopProxy | None = None,
        docker_binary: str = "docker",
        image: str = DEFAULT_SELKIES_IMAGE,
    ):
        if image != DEFAULT_SELKIES_IMAGE:
            raise ValueError("image must be the built socat derivative of the pinned official Selkies image")
        self.company_profile_path = Path(company_profile_path)
        self.state_root = Path(state_root)
        self.caddy_proxy = caddy_proxy
        self.docker_binary = docker_binary
        self.image = image
        self._idle_workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._idle_workers_lock = threading.Lock()
        self._idle_reconcile_thread: threading.Thread | None = None
        self._idle_reconcile_wakeup = threading.Event()

    def list_sessions(self) -> list[dict[str, str]]:
        """List profile identities and managed desktop status without secrets."""
        employees = self._employees()
        rows = self._docker(
            "ps", "--all", "--filter", f"label={MANAGED_LABEL}=true", "--format",
            f'{{{{.Names}}}}|{{{{.State}}}}|{{{{.Label "{EMPLOYEE_LABEL}"}}}}',
        ).stdout.splitlines()
        actual: dict[str, str] = {}
        employee_by_container = {
            self._container_name(employee["id"]): self._employee_key(employee["id"]) for employee in employees
        }
        for row in rows:
            try:
                name, state, employee_id = row.split("|", 2)
            except ValueError as error:
                raise EmployeeDesktopError("Docker returned invalid employee desktop status data") from error
            expected_employee_id = employee_by_container.get(name)
            if expected_employee_id is not None:
                if employee_id != expected_employee_id:
                    raise EmployeeDesktopError("employee desktop container name has conflicting identity metadata")
                actual[name] = state
        sessions = []
        for employee in employees:
            container = self._container_name(employee["id"])
            state = actual.get(container)
            status = "running" if state == "running" else "stopped"
            sessions.append({
                "id": employee["id"],
                "name": employee["displayName"],
                "role": employee.get("jobTitle") or employee.get("department") or "Employee",
                "status": status,
            })
        return sessions

    def list_viewers(self) -> list[dict[str, str]]:
        """Return URLs for healthy, already-running sessions and reconcile Caddy routes.

        This method never starts a container. It repairs missing dynamic Caddy
        routes after a Caddy restart and keeps per-session Basic credentials
        inside the Caddy admin config rather than returning them to the browser.
        """
        if self.caddy_proxy is None:
            raise EmployeeDesktopError("trusted HTTPS employee desktop proxy is not configured")
        viewers: list[dict[str, str]] = []
        for session in self.list_sessions():
            if session["status"] != "running":
                continue
            employee_id = session["id"]
            with self._lifecycle_lock(employee_id, blocking=False) as acquired:
                if not acquired:
                    continue
                container = self._container_name(employee_id)
                inspect = self._inspect_optional(container)
                if inspect is None or not inspect.get("State", {}).get("Running"):
                    continue
                self._validate_managed_container(inspect, employee_id)
                self._validate_mount(inspect, self._volume_name(employee_id))
                network = self._network_name(employee_id)
                self._validate_network_isolated(network, container, employee_id)
                container_ip = self._container_ip(inspect, network)
                if not self._container_port_open(container, 8080):
                    continue
                credentials = self._credentials(employee_id, create=False)
                viewing_url = self.caddy_proxy.url_for(employee_id, container_ip, credentials)
                final_inspect = self._inspect(container)
                self._validate_managed_container(final_inspect, employee_id)
                if not final_inspect.get("State", {}).get("Running"):
                    self.caddy_proxy.remove(employee_id)
                    continue
                viewers.append({"employee_id": employee_id, "viewing_url": viewing_url})
        return viewers

    def ensure_session(self, employee_id: str) -> dict[str, Any]:
        """Start or reuse a single durable employee session, idempotently."""
        employees = {item["id"]: item for item in self._employees()}
        if employee_id not in employees:
            raise KeyError(employee_id)
        if self.caddy_proxy is None:
            raise EmployeeDesktopError("trusted HTTPS employee desktop proxy is not configured")
        self._ensure_private_state_root()

        with self._lifecycle_lock(employee_id):
            container = self._container_name(employee_id)
            volume = self._volume_name(employee_id)
            network = self._network_name(employee_id)
            inspect = self._inspect_optional(container)
            was_running = bool(inspect and (inspect.get("State") or {}).get("Running"))
            if inspect is None:
                self._require_image()
            self._ensure_volume(volume, employee_id)
            self._ensure_network(network, employee_id)
            if inspect is None:
                credentials = self._credentials(employee_id)
                env_file = self._write_env_file(credentials, employee_id)
                try:
                    self._create_container(container, volume, network, employee_id, env_file)
                finally:
                    env_file.unlink(missing_ok=True)
                inspect = self._inspect(container)
            else:
                self._validate_managed_container(inspect, employee_id)
                self._validate_mount(inspect, volume)
                credentials = self._credentials(employee_id, create=False)

            self._validate_network_isolated(network, container, employee_id)
            if not inspect.get("State", {}).get("Running"):
                self._docker("start", container)
                inspect = self._inspect(container)
            self._validate_managed_container(inspect, employee_id)
            self._validate_mount(inspect, volume)
            ports = self._published_ports(inspect)
            cdp_port = ports.get("9223")
            if cdp_port is None:
                raise EmployeeDesktopError("desktop container is missing its private CDP port mapping")
            container_ip = self._container_ip(inspect, network)

            if not self._container_port_open(container, 9222):
                self._wait_for_display(container)
                self._launch_chrome(container)
            if not self._container_port_open(container, 9223):
                self._launch_cdp_relay(container)
            cdp_document = self._wait_for_cdp(container, cdp_port)
            cdp_endpoint = self._rewrite_cdp_endpoint(cdp_document, cdp_port)
            final_inspect = self._inspect(container)
            self._validate_managed_container(final_inspect, employee_id)
            self._validate_network_isolated(network, container, employee_id)
            if not final_inspect.get("State", {}).get("Running"):
                raise EmployeeDesktopError("employee desktop stopped while starting")
            # Start the idle period only after the desktop has fully started.
            # A successful retry of an already-running desktop preserves the
            # previous activity time instead of refreshing the idle deadline.
            if not was_running:
                self._write_idle_timestamp(employee_id, time.time())
            else:
                self._ensure_idle_timestamp(employee_id)
            viewing_url = self.caddy_proxy.url_for(employee_id, container_ip, credentials)
            viewing_parts = urlsplit(viewing_url)
            if (viewing_parts.scheme != "https" or not viewing_parts.netloc
                    or viewing_parts.username is not None or viewing_parts.password is not None):
                raise EmployeeDesktopError("viewing URL builder must return an HTTPS URL without credentials")
            session = {
                "employee_id": employee_id,
                "status": "running",
                "viewing_url": viewing_url,
                "viewing_credentials": {
                    "username": credentials["username"],
                    "password": credentials["password"],
                },
                "cdp_endpoint": cdp_endpoint,
            }
        self._ensure_idle_worker(employee_id)
        return session

    @contextmanager
    def employee_activity(self, employee_id: str):
        """Start the desktop and renew its idle period after an employee action.

        The action lock spans startup, the caller's browser work, and the
        completion timestamp write. Idle shutdown takes the same lock, so it
        cannot stop Chrome while an action is in progress.
        """
        with self.employee_action_lock(employee_id):
            desktop = self.ensure_session(employee_id)
            action_failure: BaseException | None = None
            try:
                yield desktop
            except BaseException as error:
                action_failure = error
                raise
            finally:
                try:
                    self._write_idle_timestamp(employee_id, time.time())
                    self._wake_idle_worker(employee_id)
                except BaseException as cleanup_error:
                    if action_failure is None:
                        raise
                    action_failure.add_note(
                        "Employee desktop idle timestamp update failed: "
                        + type(cleanup_error).__name__
                    )
                    _LOGGER.error(
                        "Employee desktop activity cleanup failed for employee key %s",
                        self._employee_key(employee_id),
                        exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )

    @contextmanager
    def employee_action_lock(self, employee_id: str):
        """Serialize browser actions for one employee across host processes.

        This lock is independent of the global provisioning lock, so a mail or
        chat action never holds up unrelated employee desktop lifecycle work.
        """
        if employee_id not in {employee["id"] for employee in self._employees()}:
            raise KeyError(employee_id)
        self._ensure_private_state_root()
        lock_name = hashlib.sha256(employee_id.encode("utf-8")).hexdigest()
        lock_path = self.state_root / f"employee-action-{lock_name}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def start_idle_shutdown(self) -> None:
        """Reconcile existing containers and keep their ten-minute timers alive.

        The coordinator calls this once when constructing its manager. Running
        containers from earlier service lifetimes receive a first-activity
        baseline, while existing timestamps remain durable across restarts.
        """
        key = str(self.state_root.resolve())
        with _IDLE_COORDINATORS_LOCK:
            coordinator = _IDLE_COORDINATORS.get(key)
            if coordinator is not None and coordinator is not self:
                return
            _IDLE_COORDINATORS[key] = self
            with self._idle_workers_lock:
                if self._idle_reconcile_thread and self._idle_reconcile_thread.is_alive():
                    return
                thread = threading.Thread(
                    target=self._reconcile_idle_workers,
                    name="employee-desktop-idle-reconcile",
                    daemon=True,
                )
                self._idle_reconcile_thread = thread
                thread.start()

    def _reconcile_idle_workers(self) -> None:
        while True:
            try:
                for session in self.list_sessions():
                    if session["status"] == "running":
                        self._ensure_idle_worker(session["id"])
            except Exception:
                _LOGGER.exception("Employee desktop idle reconciliation failed")
            self._idle_reconcile_wakeup.wait(_IDLE_RECONCILE_SECONDS)

    def _ensure_idle_worker(self, employee_id: str) -> None:
        coordinator = self._idle_coordinator()
        if coordinator is not self:
            coordinator._ensure_idle_worker_local(employee_id)
            return
        self._ensure_idle_worker_local(employee_id)

    def _ensure_idle_worker_local(self, employee_id: str) -> None:
        with self._idle_workers_lock:
            current = self._idle_workers.get(employee_id)
            if current and current[0].is_alive():
                return
            wake = threading.Event()
            worker = threading.Thread(
                target=self._idle_shutdown_worker,
                args=(employee_id, wake),
                name="employee-desktop-idle-" + self._employee_key(employee_id)[:12],
                daemon=True,
            )
            self._idle_workers[employee_id] = (worker, wake)
            worker.start()

    def _wake_idle_worker(self, employee_id: str) -> None:
        coordinator = self._idle_coordinator()
        if coordinator is not self:
            coordinator._wake_idle_worker_local(employee_id)
            return
        self._wake_idle_worker_local(employee_id)

    def _wake_idle_worker_local(self, employee_id: str) -> None:
        with self._idle_workers_lock:
            current = self._idle_workers.get(employee_id)
            if current:
                current[1].set()

    def _idle_coordinator(self) -> "EmployeeDesktopRuntime":
        with _IDLE_COORDINATORS_LOCK:
            return _IDLE_COORDINATORS.get(str(self.state_root.resolve()), self)

    def _idle_shutdown_worker(self, employee_id: str, wake: threading.Event) -> None:
        try:
            while True:
                wake.clear()
                with self.employee_action_lock(employee_id):
                    with self._lifecycle_lock(employee_id):
                        container = self._container_name(employee_id)
                        inspect = self._inspect_optional(container)
                        if inspect is None:
                            return
                        self._validate_managed_container(inspect, employee_id)
                        if not (inspect.get("State") or {}).get("Running"):
                            return
                        last_activity = self._ensure_idle_timestamp(employee_id)
                        deadline = last_activity + _IDLE_SHUTDOWN_SECONDS

                remaining = deadline - time.time()
                if remaining > 0:
                    wake.wait(remaining)
                    continue

                # Lock ordering matches employee_activity (action, lifecycle).
                # Re-read the timestamp only after both locks are held so an
                # action that completed at the deadline renews the timer.
                with self.employee_action_lock(employee_id):
                    with self._lifecycle_lock(employee_id):
                        inspect = self._inspect_optional(container)
                        if inspect is None:
                            return
                        self._validate_managed_container(inspect, employee_id)
                        if not (inspect.get("State") or {}).get("Running"):
                            return
                        last_activity = self._ensure_idle_timestamp(employee_id)
                        if time.time() < last_activity + _IDLE_SHUTDOWN_SECONDS:
                            continue
                        self._stop_session_locked(employee_id)
                        return
        except Exception:
            _LOGGER.exception(
                "Employee desktop idle shutdown failed for employee key %s",
                self._employee_key(employee_id),
            )
        finally:
            with self._idle_workers_lock:
                current = self._idle_workers.get(employee_id)
                if current and current[0] is threading.current_thread():
                    self._idle_workers.pop(employee_id, None)

    def stop_session(self, employee_id: str) -> dict[str, str]:
        """Stop the named desktop without removing its container or profile volume."""
        employees = {item["id"] for item in self._employees()}
        if employee_id not in employees:
            raise KeyError(employee_id)
        self._ensure_private_state_root()
        with self._lifecycle_lock(employee_id):
            return self._stop_session_locked(employee_id)

    def _stop_session_locked(self, employee_id: str) -> dict[str, str]:
        container = self._container_name(employee_id)
        if self.caddy_proxy is not None:
            self.caddy_proxy.remove(employee_id)
        inspect = self._inspect_optional(container)
        if inspect is not None:
            self._validate_managed_container(inspect, employee_id)
            self._validate_mount(inspect, self._volume_name(employee_id))
            if inspect.get("State", {}).get("Running"):
                self._docker("stop", container)
                inspect = self._inspect(container)
                self._validate_managed_container(inspect, employee_id)
                if inspect.get("State", {}).get("Running"):
                    raise EmployeeDesktopError("employee desktop did not stop")
        return {"employee_id": employee_id, "status": "stopped"}

    def _employees(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.company_profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EmployeeDesktopError("company identity profile is unavailable or invalid") from error
        employees = data.get("employees") if isinstance(data, dict) else None
        if not isinstance(employees, list):
            raise EmployeeDesktopError("company identity profile has no employees list")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in employees:
            if not isinstance(row, dict):
                raise EmployeeDesktopError("company identity profile contains an invalid employee")
            employee_id = row.get("id")
            name = row.get("displayName")
            if not isinstance(employee_id, str) or not employee_id:
                raise EmployeeDesktopError("company identity profile contains an invalid employee ID")
            try:
                employee_id.encode("utf-8")
            except UnicodeEncodeError as error:
                raise EmployeeDesktopError("company identity profile contains an invalid employee ID") from error
            if employee_id in seen:
                raise EmployeeDesktopError("company identity profile contains duplicate employee IDs")
            if not isinstance(name, str) or not name.strip():
                raise EmployeeDesktopError("company identity profile contains an employee without a name")
            seen.add(employee_id)
            result.append(row)
        return result

    def _ensure_private_state_root(self) -> None:
        try:
            self.state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.state_root.chmod(0o700)
        except OSError as error:
            raise EmployeeDesktopError("private employee desktop state directory is unavailable") from error

    @contextmanager
    def _lifecycle_lock(self, employee_id: str, *, blocking: bool = True):
        """Serialize lifecycle changes for this employee, not other employees."""
        lock_name = hashlib.sha256(employee_id.encode("utf-8")).hexdigest()
        lock_path = self.state_root / f"employee-lifecycle-{lock_name}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                locked = True
            except BlockingIOError:
                if blocking:
                    raise
                yield False
                return
            yield True
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _credentials_path(self, employee_id: str) -> Path:
        return self.state_root / ("employee-" + self._employee_key(employee_id) + ".json")

    def _idle_timestamp_path(self, employee_id: str) -> Path:
        return self.state_root / ("employee-idle-" + self._employee_key(employee_id) + ".json")

    def _read_idle_timestamp(self, employee_id: str) -> float | None:
        path = self._idle_timestamp_path(employee_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise EmployeeDesktopError("employee desktop idle state is unreadable") from error
        timestamp = value.get("last_activity") if isinstance(value, dict) else None
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise EmployeeDesktopError("employee desktop idle state is invalid")
        if timestamp < 0 or timestamp == float("inf") or timestamp != timestamp:
            raise EmployeeDesktopError("employee desktop idle state is invalid")
        return float(timestamp)

    def _write_idle_timestamp(self, employee_id: str, timestamp: float) -> None:
        self._ensure_private_state_root()
        path = self._idle_timestamp_path(employee_id)
        fd, temporary = tempfile.mkstemp(prefix=".employee-idle-", dir=self.state_root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"last_activity": timestamp}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _ensure_idle_timestamp(self, employee_id: str) -> float:
        timestamp = self._read_idle_timestamp(employee_id)
        if timestamp is None:
            timestamp = time.time()
            self._write_idle_timestamp(employee_id, timestamp)
        return timestamp

    def _credentials(self, employee_id: str, *, create: bool = True) -> dict[str, str]:
        path = self._credentials_path(employee_id)
        if path.exists():
            try:
                info = path.stat()
                if info.st_mode & 0o077:
                    raise PermissionError("credential file permissions are too broad")
                value = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(value, dict) or not isinstance(value.get("username"), str)
                        or not isinstance(value.get("password"), str) or len(value["password"]) < 32):
                    raise ValueError("invalid credential file")
                return {"username": value["username"], "password": value["password"]}
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise EmployeeDesktopError("private desktop credentials are unreadable or invalid") from error
        if not create:
            raise EmployeeDesktopError("private desktop credentials are missing for an existing container")
        credentials = {
            "username": "employee-" + self._employee_key(employee_id)[:12],
            "password": secrets.token_urlsafe(36),
        }
        fd, temporary = tempfile.mkstemp(prefix=".employee-desktop-", dir=self.state_root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(credentials, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return credentials

    def existing_session(self, employee_id: str) -> dict[str, str] | None:
        """Return the CDP endpoint for an already-running profile employee.

        This is read-only: it never starts a container, launches Chrome, or
        changes proxy routes. Missing and stopped sessions return ``None``.
        """
        if employee_id not in {employee["id"] for employee in self._employees()}:
            raise KeyError(employee_id)
        self._ensure_private_state_root()
        with self._lifecycle_lock(employee_id):
            return self._existing_session_locked(employee_id)

    def _existing_session_locked(self, employee_id: str) -> dict[str, str] | None:
        container = self._container_name(employee_id)
        inspect = self._inspect_optional(container)
        if inspect is None:
            return None
        self._validate_managed_container(inspect, employee_id)
        self._validate_mount(inspect, self._volume_name(employee_id))
        if not inspect.get("State", {}).get("Running"):
            return None
        network = self._network_name(employee_id)
        self._validate_network_isolated(network, container, employee_id)
        self._container_ip(inspect, network)
        cdp_port = self._published_ports(inspect).get("9223")
        if cdp_port is None:
            raise EmployeeDesktopError("desktop container is missing its private CDP port mapping")
        document = self._cdp_document(cdp_port)
        endpoint = self._rewrite_cdp_endpoint(document, cdp_port)
        return {"employee_id": employee_id, "cdp_endpoint": endpoint}

    def _write_env_file(self, credentials: dict[str, str], employee_id: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=".selkies-env-", dir=self.state_root)
        path = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write("SELKIES_ENABLE_BASIC_AUTH=true\n")
                output.write("SELKIES_BASIC_AUTH_USER=" + credentials["username"] + "\n")
                output.write("SELKIES_BASIC_AUTH_PASSWORD=" + credentials["password"] + "\n")
                output.write("PASSWD=" + credentials["password"] + "\n")
                output.write("SELKIES_MODE=websockets\n")
                output.write("SELKIES_FRAME_ANCESTORS='self'\n")
                if self.caddy_proxy is not None:
                    output.write("SELKIES_SUBFOLDER=" + self.caddy_proxy.subfolder_for(employee_id) + "\n")
                    output.write("SELKIES_ENABLE_HTTPS=false\n")
                else:
                    output.write("SELKIES_ENABLE_HTTPS=true\n")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _ensure_volume(self, volume: str, employee_id: str) -> None:
        result = self._docker("volume", "inspect", volume, check=False)
        if result.returncode == 0:
            try:
                detail = json.loads(result.stdout)[0]
            except (ValueError, IndexError, TypeError) as error:
                raise EmployeeDesktopError("employee desktop volume metadata is invalid") from error
            labels = detail.get("Labels") or {}
            if labels.get(MANAGED_LABEL) != "true" or labels.get(EMPLOYEE_LABEL) != self._employee_key(employee_id):
                raise EmployeeDesktopError("employee desktop volume name is already owned by another resource")
            return
        self._docker("volume", "create", "--label", f"{MANAGED_LABEL}=true",
                     "--label", f"{EMPLOYEE_LABEL}={self._employee_key(employee_id)}", volume)

    def _require_image(self) -> None:
        result = self._docker("image", "inspect", self.image, check=False)
        if result.returncode == 0:
            return
        self._docker("info")
        raise EmployeeDesktopImageUnavailable(
            "Employee desktop image is unavailable; rerun coordinator bootstrap to provision it"
        )

    def _ensure_network(self, network: str, employee_id: str) -> None:
        result = self._docker("network", "inspect", network, check=False)
        if result.returncode == 0:
            try:
                detail = json.loads(result.stdout)[0]
            except (ValueError, IndexError, TypeError) as error:
                raise EmployeeDesktopError("employee desktop network metadata is invalid") from error
            labels = detail.get("Labels") or {}
            if (labels.get(MANAGED_LABEL) != "true" or labels.get(EMPLOYEE_LABEL) != self._employee_key(employee_id)
                    or detail.get("Driver") != "bridge"):
                raise EmployeeDesktopError("employee desktop network is not isolated or has unexpected ownership")
            return
        self._docker("network", "create", "--driver", "bridge",
                     "--label", f"{MANAGED_LABEL}=true",
                     "--label", f"{EMPLOYEE_LABEL}={self._employee_key(employee_id)}", network)

    def _create_container(self, container: str, volume: str, network: str,
                          employee_id: str, env_file: Path) -> None:
        if self.caddy_proxy is None:
            raise EmployeeDesktopError("trusted HTTPS employee desktop proxy is not configured")
        arguments = [
            "create", "--name", container,
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{EMPLOYEE_LABEL}={self._employee_key(employee_id)}",
            "--restart", "unless-stopped",
            "--init", "--shm-size", "2g",
            "--publish", "127.0.0.1::9223/tcp",
            "--network", network,
            "--volume", f"{volume}:/home/ubuntu",
            "--env-file", str(env_file),
        ]
        for hostname, address in self.caddy_proxy.host_mappings():
            arguments.extend(("--add-host", f"{hostname}:{address}"))
        arguments.append(self.image)
        self._docker(*arguments)

    def _launch_chrome(self, container: str) -> None:
        # The official Selkies desktop image installs Google Chrome and patches
        # its launcher with the container-compatible sandbox/password-store flags.
        self._docker(
            "exec", "--user", "ubuntu", "--env", "DISPLAY=:20", "--detach", container,
            "google-chrome", "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-port=9222",
            "--user-data-dir=/home/ubuntu/.employee-browser-profile", "about:blank",
        )

    def _launch_cdp_relay(self, container: str) -> None:
        self._docker("exec", "--user", "ubuntu", "--detach", container,
                     "socat", "TCP-LISTEN:9223,bind=0.0.0.0,reuseaddr,fork", "TCP:127.0.0.1:9222")

    def _container_port_open(self, container: str, port: int) -> bool:
        result = self._docker("exec", "--user", "ubuntu", container,
                              "nc", "-z", "127.0.0.1", str(port), check=False)
        if result.returncode in {0, 1}:
            return result.returncode == 0
        raise EmployeeDesktopError(f"employee desktop port {port} probe failed") from subprocess.CalledProcessError(
            result.returncode, [self.docker_binary, "exec", container, "nc", "-z", "127.0.0.1", str(port)],
        )

    def _wait_for_display(self, container: str) -> None:
        while True:
            probe = self._docker(
                "exec", "--user", "ubuntu", "--env", "DISPLAY=:20", container,
                "xdpyinfo", "-display", ":20", check=False,
            )
            if probe.returncode == 0:
                return
            if probe.returncode != 1:
                raise EmployeeDesktopError("X11 display readiness probe failed") from subprocess.CalledProcessError(
                    probe.returncode, [self.docker_binary, "exec", container, "xdpyinfo"],
                )
            state = self._inspect(container).get("State") or {}
            if not state.get("Running"):
                detail = state.get("Error") or f"exit code {state.get('ExitCode', 'unknown')}"
                raise EmployeeDesktopError(f"employee desktop exited before X11 became ready: {detail}")
            supervision = self._docker(
                "exec", "--user", "ubuntu", container,
                "/command/s6-svstat", "/run/service/xvfb", check=False,
            )
            if supervision.returncode == 0:
                status_text = supervision.stdout.strip()
                service_state = status_text.split(maxsplit=1)[0] if status_text else ""
                if service_state == "down" and ("exitcode" in status_text or "signal" in status_text):
                    raise EmployeeDesktopError("Selkies Xvfb service exited before the display became ready")
                if service_state == "down" and "want down" in status_text:
                    raise EmployeeDesktopError("Selkies Xvfb service is not scheduled to start")
                if service_state not in {"up", "down"}:
                    raise EmployeeDesktopError("Selkies Xvfb service returned an invalid status")
            else:
                service_dir = self._docker(
                    "exec", "--user", "ubuntu", container,
                    "test", "-d", "/run/service/xvfb", check=False,
                )
                if service_dir.returncode == 0:
                    raise EmployeeDesktopError("Selkies Xvfb supervision status could not be read")
            _CDP_READY.wait(0.1)

    def _wait_for_cdp(self, container: str, port: int) -> dict[str, Any]:
        last_error: BaseException | None = None
        while True:
            try:
                document = self._cdp_document(port)
                if isinstance(document.get("webSocketDebuggerUrl"), str):
                    return document
            except (URLError, HTTPError, TimeoutError, OSError, ValueError) as error:
                last_error = error
            container_state = self._inspect(container).get("State") or {}
            if not container_state.get("Running"):
                detail = container_state.get("Error") or f"exit code {container_state.get('ExitCode', 'unknown')}"
                raise EmployeeDesktopError(f"employee desktop exited before Chrome DevTools became ready: {detail}") from last_error
            if not self._chrome_process_running(container):
                raise EmployeeDesktopError("Chrome exited before its DevTools endpoint became available") from last_error
            _CDP_READY.wait(0.1)

    def _chrome_process_running(self, container: str) -> bool:
        result = self._docker("exec", "--user", "ubuntu", container,
                              "pgrep", "-f", "remote-debugging-port=9222", check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise EmployeeDesktopError("Chrome process status could not be determined") from subprocess.CalledProcessError(
            result.returncode, [self.docker_binary, "exec", container, "pgrep"],
        )

    @staticmethod
    def _cdp_document(port: int) -> dict[str, Any]:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version") as response:
            document = json.loads(response.read().decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Chrome DevTools returned an invalid response")
        return document

    @staticmethod
    def _rewrite_cdp_endpoint(document: dict[str, Any], port: int) -> str:
        value = document.get("webSocketDebuggerUrl")
        if not isinstance(value, str):
            raise EmployeeDesktopError("Chrome DevTools endpoint is missing")
        parts = urlsplit(value)
        if parts.scheme not in {"ws", "wss"} or not parts.path.startswith("/devtools/browser/"):
            raise EmployeeDesktopError("Chrome DevTools endpoint has an unexpected format")
        return urlunsplit(("ws", f"127.0.0.1:{port}", parts.path, parts.query, ""))

    def _inspect_optional(self, container: str) -> dict[str, Any] | None:
        result = self._docker("inspect", container, check=False)
        if result.returncode != 0:
            # Only a missing named container means first provisioning. Distinguish
            # daemon and permission errors by querying the engine itself.
            self._docker("info")
            return None
        try:
            details = json.loads(result.stdout)
            if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict):
                raise ValueError("invalid inspect result")
            return details[0]
        except (ValueError, TypeError) as error:
            raise EmployeeDesktopError("employee desktop container metadata is invalid") from error

    def _inspect(self, container: str) -> dict[str, Any]:
        result = self._docker("inspect", container)
        try:
            return json.loads(result.stdout)[0]
        except (ValueError, IndexError, TypeError) as error:
            raise EmployeeDesktopError("employee desktop container metadata is invalid") from error

    def _validate_managed_container(self, inspect: dict[str, Any], employee_id: str) -> None:
        labels = (inspect.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(EMPLOYEE_LABEL) != self._employee_key(employee_id):
            raise EmployeeDesktopError("employee desktop container name is already owned by another resource")
        if (inspect.get("Config") or {}).get("Image") != self.image:
            raise EmployeeDesktopError("employee desktop container uses a different image than configured")

    @staticmethod
    def _validate_mount(inspect: dict[str, Any], volume: str) -> None:
        mounts = inspect.get("Mounts") or []
        if not any(row.get("Type") == "volume" and row.get("Name") == volume
                   and row.get("Destination") == "/home/ubuntu" for row in mounts):
            raise EmployeeDesktopError("employee desktop container does not use its persistent profile volume")

    def _validate_network_isolated(self, network: str, container: str, employee_id: str) -> None:
        result = self._docker("network", "inspect", network)
        try:
            detail = json.loads(result.stdout)[0]
        except (ValueError, IndexError, TypeError) as error:
            raise EmployeeDesktopError("employee desktop network metadata is invalid") from error
        labels = detail.get("Labels") or {}
        if (labels.get(MANAGED_LABEL) != "true" or labels.get(EMPLOYEE_LABEL) != self._employee_key(employee_id)
                or detail.get("Driver") != "bridge"):
            raise EmployeeDesktopError("employee desktop network is not isolated or has unexpected ownership")
        attached = detail.get("Containers") or {}
        if any(item.get("Name") != container for item in attached.values()):
            raise EmployeeDesktopError("employee desktop network is shared with another container")

    @staticmethod
    def _container_ip(inspect: dict[str, Any], network: str) -> str:
        attachments = (inspect.get("NetworkSettings") or {}).get("Networks") or {}
        if set(attachments) != {network}:
            raise EmployeeDesktopError("employee desktop is attached to an unexpected Docker network")
        attachment = attachments.get(network) or {}
        ip = attachment.get("IPAddress") or attachment.get("GlobalIPv6Address")
        try:
            address = ipaddress.ip_address(ip)
        except (TypeError, ValueError) as error:
            raise EmployeeDesktopError("employee desktop has no valid network address") from error
        if (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified):
            raise EmployeeDesktopError("employee desktop has no valid network address")
        return str(address)

    @staticmethod
    def _published_ports(inspect: dict[str, Any]) -> dict[str, int]:
        ports = ((inspect.get("NetworkSettings") or {}).get("Ports") or {})
        web_bindings = ports.get("8080/tcp")
        if web_bindings:
            raise EmployeeDesktopError("employee desktop web port must not be published on the host")
        result: dict[str, int] = {}
        for container_port in ("9223/tcp",):
            bindings = ports.get(container_port)
            if not isinstance(bindings, list) or len(bindings) != 1:
                continue
            binding = bindings[0]
            if binding.get("HostIp") != "127.0.0.1":
                raise EmployeeDesktopError("employee desktop port is not bound to its configured interface")
            try:
                result[container_port.split("/")[0]] = int(binding["HostPort"])
            except (KeyError, TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _container_name(employee_id: str) -> str:
        digest = hashlib.sha256(employee_id.encode("utf-8")).hexdigest()[:24]
        return "tac-employee-desktop-" + digest

    @staticmethod
    def _employee_key(employee_id: str) -> str:
        return hashlib.sha256(employee_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _volume_name(employee_id: str) -> str:
        digest = hashlib.sha256(employee_id.encode("utf-8")).hexdigest()[:24]
        return "tac-employee-profile-" + digest

    @staticmethod
    def _network_name(employee_id: str) -> str:
        digest = hashlib.sha256(employee_id.encode("utf-8")).hexdigest()[:24]
        return "tac-employee-net-" + digest

    def _docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.docker_binary, *args], capture_output=True, text=True,
            )
        except OSError as error:
            raise EmployeeDesktopError("Docker operation could not be completed") from error
        if check and result.returncode != 0:
            command_error = subprocess.CalledProcessError(result.returncode, [self.docker_binary, *args])
            raise EmployeeDesktopError("Docker operation failed") from command_error
        return result
