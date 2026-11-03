"""Private, process-backed Codex CLI device authentication for the host bridge."""

from __future__ import annotations

import hmac
import logging
import os
import re
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from servers.simulation.codex_model import CodexCLISettings

_LOGGER = logging.getLogger(__name__)
_DEVICE_LOGIN_TTL = timedelta(minutes=15)
_PROCESS_STOP_TIMEOUT_SECONDS = 2
_DEVICE_CODE_LABEL_PATTERN = re.compile(r"enter this one-time code", re.IGNORECASE)
_DEVICE_CODE_PATTERN = re.compile(
    r"\b([A-Z0-9]{3,8}-[A-Z0-9]{3,8})\b", re.IGNORECASE
)
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_HTTPS_URL_PATTERN = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)

DeviceLoginState = Literal[
    "authenticated",
    "unauthenticated",
    "starting",
    "pending",
    "completed",
    "failed",
    "expired",
]


class CodexAuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authenticated: bool
    auth_method: Literal["chatgpt", "api_key"] | None
    state: DeviceLoginState
    login_id: str | None
    user_code: str | None
    verification_url: str | None
    expires_at: datetime | None
    error: str | None


@dataclass
class _DeviceLoginSession:
    login_id: str
    process: subprocess.Popen[str] | None
    expires_at: datetime
    error: str | None = None
    user_code: str | None = None
    verification_url: str | None = None
    output_lock: threading.Lock = field(default_factory=threading.Lock)


class CodexDeviceAuthManager:
    """Own one live device-login process against the configured Codex home."""

    def __init__(self, settings: CodexCLISettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._session: _DeviceLoginSession | None = None

    @asynccontextmanager
    async def lifespan(self, _app) -> AsyncIterator[None]:
        try:
            yield
        finally:
            self._stop_current_session()

    def status(self, *, expected_login_id: str | None = None) -> CodexAuthStatus:
        status_error: str | None = None
        try:
            authenticated, auth_method = _codex_login_status(self._settings)
        except _CodexAuthStatusError as error:
            _LOGGER.exception(
                "Codex login status unavailable exception_type=%s",
                type(error.__cause__ or error).__name__,
            )
            authenticated, auth_method = False, None
            status_error = "codex_status_unavailable"

        with self._lock:
            session = self._session
            if expected_login_id is not None and (
                session is None
                or not hmac.compare_digest(session.login_id, expected_login_id)
            ):
                raise HTTPException(
                    status_code=404, detail={"error": "device_login_not_found"}
                )
            if session is None:
                if status_error is not None:
                    return _status(
                        authenticated=False,
                        auth_method=None,
                        state="failed",
                        error=status_error,
                    )
                return _status(
                    authenticated=authenticated,
                    auth_method=auth_method,
                    state="authenticated" if authenticated else "unauthenticated",
                )

            process = session.process
            if session.error is not None and session.process is None:
                return _status(
                    authenticated=authenticated,
                    auth_method=auth_method,
                    state="failed",
                    session=session,
                    error=session.error,
                )
            if process is None:
                return _status(
                    authenticated=authenticated,
                    auth_method=auth_method,
                    state="failed",
                    session=session,
                    error=status_error or "device_login_process_missing",
                )
            return_code = process.poll()
            if return_code is None:
                state: DeviceLoginState = (
                    "pending" if _has_user_code(session) else "starting"
                )
                return _status(
                    authenticated=authenticated,
                    auth_method=auth_method,
                    state=state,
                    session=session,
                    error=status_error,
                )

            with session.output_lock:
                session.user_code = None
                session.verification_url = None

            if return_code == 0 and authenticated:
                return _status(
                    authenticated=True,
                    auth_method=auth_method,
                    state="completed",
                    session=session,
                )

            if return_code == 0 and status_error is not None:
                return _status(
                    authenticated=False,
                    auth_method=None,
                    state="pending",
                    session=session,
                    error=status_error,
                )

            if datetime.now(timezone.utc) >= session.expires_at and return_code != 0:
                error_code = "device_login_expired"
            elif return_code == 0:
                error_code = "device_auth_not_confirmed"
            else:
                error_code = "device_login_failed"
            if session.error is None:
                session.error = error_code
                if return_code != 0:
                    process_error = subprocess.CalledProcessError(
                        return_code,
                        [self._settings.executable, "login", "--device-auth"],
                    )
                    try:
                        raise process_error
                    except subprocess.CalledProcessError:
                        _LOGGER.exception(
                            "Codex device login process failed login_id=%s exit_code=%s error=%s",
                            session.login_id,
                            return_code,
                            error_code,
                        )
                elif status_error is None:
                    process_error = RuntimeError(
                        "Codex device login exited successfully without confirmed authentication"
                    )
                    try:
                        raise process_error
                    except RuntimeError:
                        _LOGGER.exception(
                            "Codex device login completed without authentication login_id=%s",
                            session.login_id,
                        )
            else:
                error_code = session.error
            if authenticated:
                return _status(
                    authenticated=True,
                    auth_method=auth_method,
                    state="failed",
                    session=session,
                    error=error_code,
                )
            return _status(
                authenticated=False,
                auth_method=None,
                state=("expired" if error_code == "device_login_expired" else "failed"),
                session=session,
                error=error_code,
            )

    def start(self) -> CodexAuthStatus:
        with self._lock:
            current = self._session
            if (
                current is not None
                and current.process is not None
                and current.process.poll() is None
            ):
                return _status(
                    authenticated=False,
                    auth_method=None,
                    state=(
                        "pending" if _has_user_code(current) else "starting"
                    ),
                    session=current,
                )

            started_at = datetime.now(timezone.utc)
            session = _DeviceLoginSession(
                login_id=str(uuid4()),
                process=None,
                expires_at=started_at + _DEVICE_LOGIN_TTL,
            )
            try:
                session.process = subprocess.Popen(  # noqa: S603
                    [self._settings.executable, "login", "--device-auth"],
                    cwd=str(self._settings.codex_home.parent),
                    env=_codex_auth_environment(self._settings),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as error:
                session.error = "codex_cli_start_failed"
                self._session = session
                _LOGGER.exception(
                    "Codex device login could not start exception_type=%s",
                    type(error).__name__,
                )
                return _status(
                    authenticated=False,
                    auth_method=None,
                    state="failed",
                    session=session,
                    error=session.error,
                )

            self._session = session
            if session.process.stdout is not None:
                threading.Thread(
                    target=_read_login_output,
                    args=(session.process.stdout, session),
                    daemon=True,
                    name="codex-device-login-output",
                ).start()
            return _status(
                authenticated=False,
                auth_method=None,
                state="starting",
                session=session,
            )

    def poll(self, login_id: str) -> CodexAuthStatus:
        return self.status(expected_login_id=login_id)

    def _stop_current_session(self) -> None:
        with self._lock:
            session = self._session
            if session is not None and session.process is not None:
                self._terminate_process(session.process)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        except OSError as error:
            _LOGGER.exception(
                "Codex device login terminate failed exception_type=%s",
                type(error).__name__,
            )
            return
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                return
            except OSError as error:
                _LOGGER.exception(
                    "Codex device login kill failed exception_type=%s",
                    type(error).__name__,
                )
                return
            try:
                process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _LOGGER.exception("Codex device login process did not exit after kill")
            except OSError as error:
                _LOGGER.exception(
                    "Codex device login wait after kill failed exception_type=%s",
                    type(error).__name__,
                )
        except OSError as error:
            _LOGGER.exception(
                "Codex device login wait after terminate failed exception_type=%s",
                type(error).__name__,
            )


class _CodexAuthStatusError(RuntimeError):
    pass


def _codex_auth_environment(settings: CodexCLISettings) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(settings.codex_home.parent),
        "CODEX_HOME": str(settings.codex_home),
        "NO_COLOR": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for name in (
        "SSL_CERT_FILE",
        "CODEX_CA_CERTIFICATE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _codex_login_status(
    settings: CodexCLISettings,
) -> tuple[bool, Literal["chatgpt", "api_key"] | None]:
    try:
        completed = subprocess.run(  # noqa: S603
            [settings.executable, "login", "status"],
            cwd=str(settings.codex_home.parent),
            env=_codex_auth_environment(settings),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise _CodexAuthStatusError("Codex CLI login status could not be read") from error

    output = f"{completed.stdout}\n{completed.stderr}".casefold()
    if completed.returncode == 0:
        if "logged in using chatgpt" in output or "logged in with chatgpt" in output:
            return True, "chatgpt"
        if "logged in using api key" in output or "logged in with api key" in output:
            return True, "api_key"
        return True, None
    if completed.returncode == 1 and "not logged in" in output:
        return False, None
    error = subprocess.CalledProcessError(
        completed.returncode, [settings.executable, "login", "status"]
    )
    raise _CodexAuthStatusError(
        "Codex CLI login status returned an unexpected result"
    ) from error


def _read_login_output(stream, session: _DeviceLoginSession) -> None:
    awaiting_code = False
    try:
        for line in stream:
            display_line = _ANSI_ESCAPE_PATTERN.sub("", line).strip()
            code = _device_code_from_line(display_line)
            verification_url = _device_url_from_line(display_line)
            if _DEVICE_CODE_LABEL_PATTERN.search(display_line):
                awaiting_code = True
                if code is None:
                    continue
            elif not awaiting_code:
                code = None
            if code is None and verification_url is None:
                continue
            with session.output_lock:
                if code is not None:
                    session.user_code = code
                if verification_url is not None:
                    session.verification_url = verification_url
            if code is not None:
                awaiting_code = False
    except OSError as error:
        _LOGGER.exception(
            "Codex device login output read failed exception_type=%s",
            type(error).__name__,
        )
    finally:
        try:
            stream.close()
        except OSError as error:
            _LOGGER.exception(
                "Codex device login output close failed exception_type=%s",
                type(error).__name__,
            )


def _device_code_from_line(line: str) -> str | None:
    match = _DEVICE_CODE_PATTERN.search(line.strip())
    return match.group(1).upper() if match else None


def _device_url_from_line(line: str) -> str | None:
    for match in _HTTPS_URL_PATTERN.finditer(line):
        candidate = match.group(0).rstrip(".,;)")
        parsed = urlsplit(candidate)
        if parsed.hostname == "auth.openai.com" and parsed.path == "/codex/device":
            return "https://auth.openai.com/codex/device"
    return None


def _status(
    *,
    authenticated: bool,
    auth_method: Literal["chatgpt", "api_key"] | None,
    state: DeviceLoginState,
    session: _DeviceLoginSession | None = None,
    error: str | None = None,
) -> CodexAuthStatus:
    user_code = None
    verification_url = None
    if session is not None:
        with session.output_lock:
            user_code = session.user_code
            verification_url = session.verification_url
        login_id = session.login_id
        expires_at = session.expires_at
    else:
        login_id = None
        expires_at = None
    return CodexAuthStatus(
        authenticated=authenticated,
        auth_method=auth_method,
        state=state,
        login_id=login_id,
        user_code=user_code,
        verification_url=verification_url,
        expires_at=expires_at,
        error=error,
    )


def _has_user_code(session: _DeviceLoginSession) -> bool:
    with session.output_lock:
        return session.user_code is not None


def create_codex_auth_router(
    bearer_token: str,
    *,
    manager: CodexDeviceAuthManager,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["host Codex authentication"])

    def require_authorization(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or not hmac.compare_digest(supplied, bearer_token)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    auth = [Depends(require_authorization)]

    @router.get("/status", response_model=CodexAuthStatus, dependencies=auth)
    def login_status() -> CodexAuthStatus:
        return manager.status()

    @router.post("/device/start", response_model=CodexAuthStatus, dependencies=auth)
    def start_device_login() -> CodexAuthStatus:
        return manager.start()

    @router.get(
        "/device/{login_id}", response_model=CodexAuthStatus, dependencies=auth
    )
    def poll_device_login(login_id: str) -> CodexAuthStatus:
        return manager.poll(login_id)

    return router
