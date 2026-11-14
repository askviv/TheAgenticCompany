"""Authenticated RPC client and host endpoint for the existing Codex CLI model."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
import shlex
import logging
from typing import Any, Literal

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Base64Bytes, ConfigDict, Field, SecretStr

from servers.simulation.codex_model import (
    CodexAuthenticationError, CodexCLIModel, CodexCLISettings, CodexConfigurationError,
    CodexModelAccessError, CodexModelError, CodexOutputSchemaError, CodexRateLimitError,
    ModelReply, CodexNoDesktopActionsError,
    model_calls_enabled, set_model_calls_enabled, CodexModelCallsStopped, CodexModelCallsDisabled,
)
from servers.simulation.codex_device_auth import (
    CodexDeviceAuthManager,
    create_codex_auth_router,
)


class CodexRemoteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(min_length=1)
    bearer_token: SecretStr = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(default="low", pattern="^(low|medium|high|xhigh|max)$")


DesktopEnvironmentKey = Literal[
    "DISPLAY", "HOME", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS",
    "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
]
CUA_TOOLS = [
    "list_apps", "list_windows", "get_window_state", "get_desktop_state", "get_guide",
    "bring_to_front", "set_window_frame",
    "get_screen_size", "get_cursor_position", "move_cursor", "click", "double_click",
    "right_click", "drag", "scroll", "type_text", "set_value", "press_key", "hotkey", "run_steps",
    "set_agent_cursor_enabled", "get_agent_cursor_state", "clipboard_read", "clipboard_write",
]


def employee_cua_mcp(container: str, environment: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Native Codex MCP configuration for one isolated employee desktop."""
    remote = ["sudo", "docker", "exec", "--interactive", "--user", "ubuntu"]
    for key, value in {**environment, "DISPLAY": ":20", "CUA_DRIVER_PERMISSION_MODE": "standard"}.items():
        remote.extend(["--env", f"{key}={value}"])
    remote.extend([container, "/opt/employee-cua/cua-driver", "mcp"])
    return {"employee_cua": {
        "command": "/usr/bin/ssh",
        "args": ["-T", "-i", str(Path.home() / ".ssh/tac-guest-ed25519"), "-o", "BatchMode=yes",
                 "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "UserKnownHostsFile=" + str(Path.home() / ".ssh/tac-guest-known-hosts"),
                 "-p", os.environ.get("CODEX_CUA_GUEST_SSH_PORT", "22220"), "ubuntu@127.0.0.1", shlex.join(remote)],
        "required": True,
        "default_tools_approval_mode": "approve",
        "enabled_tools": CUA_TOOLS,
    }}


class ModelControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool



class CodexInferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    desktop_container: str | None = Field(default=None, pattern=r"^tac-employee-desktop-[a-f0-9]{24}$")
    desktop_environment: dict[DesktopEnvironmentKey, str] = Field(default_factory=dict)


class CodexInferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    content: str | None
    tool_calls: list[dict[str, str]]
    usage: dict[str, int] | None
    model: str
    reasoning_effort: str


class CodexJudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    prompt: str
    model: str = Field(min_length=1)
    output_schema: dict[str, Any]
    image_bytes: Base64Bytes | None = None
    image_filename: str | None = None


_ERROR_TYPES = {
    "configuration": CodexConfigurationError,
    "authentication": CodexAuthenticationError,
    "model_access": CodexModelAccessError,
    "rate_limit": CodexRateLimitError,
    "output_schema": CodexOutputSchemaError,
    "inference": CodexModelError,
    "model_calls_stopped": CodexModelCallsStopped,
    "no_desktop_actions": CodexNoDesktopActionsError,
}
_SAFE_ERRORS = {
    CodexConfigurationError: "configuration", CodexAuthenticationError: "authentication",
    CodexModelAccessError: "model_access", CodexRateLimitError: "rate_limit",
    CodexOutputSchemaError: "output_schema", CodexModelError: "inference",
    CodexNoDesktopActionsError: "no_desktop_actions",
}


class CodexInferenceRejected(CodexModelError):
    """The inference endpoint rejected the request before invoking Codex."""


class CodexRemoteModel:
    """Drop-in completion transport; actor policy remains in the caller."""

    def __init__(self, settings: CodexRemoteSettings, *, session=None, request_timeout=None):
        self.settings = settings
        self.request_timeout = request_timeout
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def complete(self, messages: list[dict[str, Any]], tools=None, *, on_submission_started=None,
                 employee_id: str | None = None, desktop_environment: dict[str, str] | None = None) -> dict:
        container = None
        if employee_id is not None:
            from .employee_desktops import EmployeeDesktopRuntime
            container = EmployeeDesktopRuntime._container_name(employee_id)
        payload = CodexInferenceRequest(messages=messages, tools=tools or [], desktop_container=container,
                                       desktop_environment=desktop_environment or {}).model_dump(mode="json")
        if on_submission_started is not None:
            on_submission_started()
        try:
            response = self.session.post(
                self.settings.url.rstrip("/") + "/v1/infer",
                json=payload,
                headers={"Authorization": "Bearer " + self.settings.bearer_token.get_secret_value()},
                **({"timeout": self.request_timeout()} if self.request_timeout is not None else {}),
            )
        except requests.RequestException as error:
            raise CodexModelError("Codex inference transport ended without a confirmed result") from error
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
                code = detail.get("error") if isinstance(detail, dict) else None
            except (ValueError, AttributeError):
                detail = code = None
            error_type = _ERROR_TYPES.get(code, CodexModelError)
            if response.status_code == 423 and code == "model_calls_disabled":
                error_type = CodexModelCallsDisabled
            if (response.status_code == 401 and detail == "unauthorized"
                    or response.status_code == 422 and isinstance(detail, list)):
                error_type = CodexInferenceRejected
            raise error_type(f"Codex inference service failed ({error_type.__name__})") from RuntimeError(
                f"remote inference returned HTTP {response.status_code}"
            )
        try:
            result = CodexInferenceResponse.model_validate(response.json())
            if result.model != self.settings.model or result.reasoning_effort != self.settings.reasoning_effort:
                raise ValueError("remote model binding changed")
            ModelReply(content=result.content, tool_calls=result.tool_calls)
            return result.model_dump()
        except Exception as error:
            raise CodexModelError("Codex inference service returned an invalid response") from error


def create_codex_transport(settings):
    if isinstance(settings, CodexRemoteSettings):
        return CodexRemoteModel(settings)
    return CodexCLIModel(settings)


def create_codex_inference_app(settings: CodexCLISettings, bearer_token: SecretStr, *, employee_model: str | None = None) -> FastAPI:
    """Expose only structured inference; all prompts and results stay out of logs."""
    auth_manager = CodexDeviceAuthManager(settings)
    app = FastAPI(
        title="Codex inference transport",
        docs_url=None,
        redoc_url=None,
        lifespan=auth_manager.lifespan,
    )
    model = CodexCLIModel(settings)
    desktop_model = CodexCLIModel(settings.model_copy(update={"model": employee_model})) if employee_model else model
    token = bearer_token.get_secret_value()
    if not token:
        raise ValueError("Codex inference bearer token is required")
    app.include_router(create_codex_auth_router(token, manager=auth_manager))

    @app.get("/v1/model-control")
    def model_control_status(authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"enabled": model_calls_enabled(settings)}

    @app.post("/v1/model-control")
    def model_control(request: ModelControl, authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(status_code=401, detail="unauthorized")
        return set_model_calls_enabled(settings, request.enabled)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/infer", response_model=CodexInferenceResponse)
    def infer(request: CodexInferenceRequest, authorization: str | None = Header(default=None)):
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            return (desktop_model if request.desktop_container is not None else model).complete(
                request.messages, tools=request.tools,
                mcp_servers=employee_cua_mcp(request.desktop_container, request.desktop_environment)
                if request.desktop_container is not None else None,
            )
        except CodexModelCallsDisabled as error:
            raise HTTPException(status_code=423, detail={"error": "model_calls_disabled"}) from error
        except CodexModelCallsStopped as error:
            raise HTTPException(status_code=423, detail={"error": "model_calls_stopped"}) from error
        except CodexModelError as error:
            logging.getLogger(__name__).exception(
                "Codex inference failed exception_type=%s", type(error).__name__
            )
            code = _SAFE_ERRORS.get(type(error), "inference")
            raise HTTPException(status_code=502, detail={"error": code}) from error

    @app.post("/v1/judge")
    def judge(request: CodexJudgeRequest, authorization: str | None = Header(default=None)):
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        if (request.image_bytes is None) != (request.image_filename is None):
            raise HTTPException(status_code=422, detail={"error": "invalid_image"})
        if request.model != settings.model:
            raise HTTPException(status_code=409, detail={"error": "model_binding_mismatch"})
        try:
            return model.complete_structured_prompt(
                request.prompt, request.output_schema, image=request.image_bytes,
                image_filename=request.image_filename,
            )
        except CodexModelCallsStopped as error:
            raise HTTPException(status_code=423, detail={"error": "model_calls_stopped"}) from error
        except CodexModelError as error:
            logging.getLogger(__name__).exception(
                "Codex judge failed exception_type=%s", type(error).__name__
            )
            code = _SAFE_ERRORS.get(type(error), "inference")
            raise HTTPException(status_code=502, detail={"error": code}) from error

    return app


def host_app_from_environment():
    import os

    token = os.environ["CODEX_INFERENCE_BEARER_TOKEN"]
    return create_codex_inference_app(CodexCLISettings(
        executable=os.getenv("CODEX_EXECUTABLE", "codex"),
        model=os.environ["CODEX_MODEL"],
        codex_home=os.environ["CODEX_HOME"],
        timeout_seconds=None,
        reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "low"),
    ), SecretStr(token), employee_model=os.getenv("CODEX_CUA_MODEL"))
