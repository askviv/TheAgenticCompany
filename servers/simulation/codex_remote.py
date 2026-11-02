"""Authenticated RPC client and host endpoint for the existing Codex CLI model."""

from __future__ import annotations

import hmac
import logging
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Base64Bytes, ConfigDict, Field, SecretStr

from servers.simulation.codex_model import (
    CodexAuthenticationError, CodexCLIModel, CodexCLISettings, CodexConfigurationError,
    CodexModelAccessError, CodexModelError, CodexOutputSchemaError, CodexRateLimitError,
    ModelReply,
)


class CodexRemoteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(min_length=1)
    bearer_token: SecretStr = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(default="low", pattern="^(low|medium|high|xhigh|max)$")


class CodexInferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)


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
}
_SAFE_ERRORS = {
    CodexConfigurationError: "configuration", CodexAuthenticationError: "authentication",
    CodexModelAccessError: "model_access", CodexRateLimitError: "rate_limit",
    CodexOutputSchemaError: "output_schema", CodexModelError: "inference",
}


class CodexRemoteModel:
    """Drop-in completion transport; actor policy remains in the caller."""

    def __init__(self, settings: CodexRemoteSettings, *, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def complete(self, messages: list[dict[str, Any]], tools=None, *, on_submission_started=None) -> dict:
        payload = CodexInferenceRequest(messages=messages, tools=tools or []).model_dump(mode="json")
        if on_submission_started is not None:
            on_submission_started()
        try:
            response = self.session.post(
                self.settings.url.rstrip("/") + "/v1/infer",
                json=payload,
                headers={"Authorization": "Bearer " + self.settings.bearer_token.get_secret_value()},
            )
        except requests.RequestException as error:
            raise CodexModelError("Codex inference transport ended without a confirmed result") from error
        if response.status_code != 200:
            try:
                code = response.json().get("detail", {}).get("error")
            except (ValueError, AttributeError):
                code = None
            error_type = _ERROR_TYPES.get(code, CodexModelError)
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


def create_codex_inference_app(settings: CodexCLISettings, bearer_token: SecretStr) -> FastAPI:
    """Expose only structured inference; all prompts and results stay out of logs."""
    app = FastAPI(title="Codex inference transport", docs_url=None, redoc_url=None)
    model = CodexCLIModel(settings)
    token = bearer_token.get_secret_value()
    if not token:
        raise ValueError("Codex inference bearer token is required")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/infer", response_model=CodexInferenceResponse)
    def infer(request: CodexInferenceRequest, authorization: str | None = Header(default=None)):
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            return model.complete(request.messages, tools=request.tools)
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
    ), SecretStr(token))
