"""Synchronous RPC client for isolated, durably idempotent actor workers."""

from __future__ import annotations

import re

import requests
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator

from servers.simulation.actor_protocol import (
    ActorEvaluateRequest, ActorEvaluateResponse, ActorTurnRequest, ActorTurnResponse,
    Tau2RetailEvaluateRequest, Tau2RetailSeedRequest,
    Tau2RetailFeedbackActionRequest, Tau2RetailFeedbackInitializeRequest, Tau2RetailFeedbackReconcileRequest,
    Tau2RetailFeedbackEvaluateRequest,
)


class ActorWorkerError(RuntimeError):
    """A worker operation failed; the message intentionally excludes provider data."""

    def __init__(self, actor_type: str, status_code: int | None, code: str):
        self.actor_type = actor_type
        self.status_code = status_code
        self.code = code
        super().__init__(f"{actor_type} actor worker operation failed ({code})")


class ActorWorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_url: AnyHttpUrl | None = None
    customer_token: SecretStr | None = None
    supplier_url: AnyHttpUrl | None = None
    supplier_token: SecretStr | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def configured_workers_need_timeout(self):
        configured = any((self.customer_url, self.customer_token,
                          self.supplier_url, self.supplier_token))
        if configured and self.request_timeout_seconds is None:
            raise ValueError("actor worker endpoints require an explicit request timeout")
        return self


class ActorTurnClient:
    def __init__(self, settings: ActorWorkerSettings, *, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def _post(self, actor_type: str, path: str, request: BaseModel) -> requests.Response:
        base_url = getattr(self.settings, f"{actor_type}_url")
        token = getattr(self.settings, f"{actor_type}_token")
        if base_url is None or token is None or self.settings.request_timeout_seconds is None:
            raise ActorWorkerError(actor_type, None, "worker_not_configured")
        try:
            return self.session.post(
                str(base_url).rstrip("/") + path,
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ActorWorkerError(actor_type, None, "worker_transport_uncertain") from exc

    def _get(self, actor_type: str, path: str) -> requests.Response:
        base_url = getattr(self.settings, f"{actor_type}_url")
        token = getattr(self.settings, f"{actor_type}_token")
        if base_url is None or token is None or self.settings.request_timeout_seconds is None:
            raise ActorWorkerError(actor_type, None, "worker_not_configured")
        try:
            return self.session.get(
                str(base_url).rstrip("/") + path,
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ActorWorkerError(actor_type, None, "worker_transport_uncertain") from exc

    def retail_catalog(self) -> dict:
        response = self._get("customer", "/v1/retail/catalog")
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if not isinstance(result, dict) or result.get("domain") != "retail":
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response")
        return result

    def retail_manifest(self, task_id: str) -> dict:
        from urllib.parse import quote

        response = self._get("customer", f"/v1/retail/tasks/{quote(task_id, safe='')}/manifest")
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if not isinstance(result, dict) or result.get("task_id") != task_id:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response")
        return result

    def retail_seed_email(self, request: Tau2RetailSeedRequest) -> ActorTurnResponse:
        response = self._post("customer", "/v1/retail/seed-email", request)
        if response.status_code not in (200, 202):
            raise self._http_error("customer", response)
        try:
            result = ActorTurnResponse.model_validate(response.json())
        except Exception as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if result.turn_id != request.turn_id:
            raise ActorWorkerError("customer", response.status_code, "worker_turn_id_mismatch")
        if (response.status_code == 202) != (result.status == "started_uncertain"):
            raise ActorWorkerError("customer", response.status_code, "worker_status_mismatch")
        return result

    def retail_evaluate(self, request: Tau2RetailEvaluateRequest) -> dict:
        response = self._post("customer", "/v1/retail/evaluate", request)
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if not isinstance(result, dict) or result.get("task_id") != request.task_id:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response")
        return result

    def apply_retail_feedback(self, task_id: str, experiment_id: str, action: dict) -> dict:
        request = Tau2RetailFeedbackActionRequest(task_id=task_id, experiment_id=experiment_id, action=action)
        response = self._post("customer", "/v1/retail/feedback/apply", request)
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if result.get("action_id") != request.action.get("action_id"):
            raise ActorWorkerError("customer", response.status_code, "worker_action_id_mismatch")
        return result

    def initialize_retail_feedback(self, task_id: str, experiment_id: str, observed_db: dict) -> dict:
        request = Tau2RetailFeedbackInitializeRequest(
            task_id=task_id, experiment_id=experiment_id, observed_db=observed_db,
        )
        response = self._post("customer", "/v1/retail/feedback/initialize", request)
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if result.get("task_id") != request.task_id or result.get("experiment_id") != request.experiment_id:
            raise ActorWorkerError("customer", response.status_code, "worker_task_id_mismatch")
        return result

    def reconcile_retail_feedback(self, task_id: str, experiment_id: str, action: dict) -> dict | None:
        request = Tau2RetailFeedbackReconcileRequest(task_id=task_id, experiment_id=experiment_id, action=action)
        response = self._post("customer", "/v1/retail/feedback/reconcile", request)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if result.get("action_id") != request.action.get("action_id"):
            raise ActorWorkerError("customer", response.status_code, "worker_action_id_mismatch")
        return result

    def evaluate_retail_feedback(
        self, task_id: str, experiment_id: str, observed_db: dict,
        unmapped: list[dict], trajectory: list[dict],
    ) -> dict:
        request = Tau2RetailFeedbackEvaluateRequest(
            task_id=task_id, experiment_id=experiment_id, observed_db=observed_db,
            unmapped=unmapped, trajectory=trajectory,
        )
        response = self._post("customer", "/v1/retail/feedback/evaluate", request)
        if response.status_code != 200:
            raise self._http_error("customer", response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ActorWorkerError("customer", response.status_code, "invalid_worker_response") from exc
        if result.get("task_id") != request.task_id:
            raise ActorWorkerError("customer", response.status_code, "worker_task_id_mismatch")
        return result

    @staticmethod
    def _http_error(actor_type: str, response: requests.Response) -> ActorWorkerError:
        if response.status_code == 409:
            return ActorWorkerError(actor_type, 409, "turn_id_payload_conflict")
        code = "worker_unavailable" if response.status_code >= 500 else "worker_rejected_turn"
        parsing_error = None
        try:
            detail = response.json().get("detail")
            if isinstance(detail, dict):
                safe_code = detail.get("code")
                safe_type = detail.get("error_type")
                if isinstance(safe_code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", safe_code):
                    code = safe_code
                    if isinstance(safe_type, str) and safe_type.isidentifier():
                        code += ":" + safe_type
        except (ValueError, AttributeError) as error:
            parsing_error = error
        failure = ActorWorkerError(actor_type, response.status_code, code)
        failure.__cause__ = parsing_error
        return failure

    def turn(self, request: ActorTurnRequest) -> ActorTurnResponse:
        response = self._post(request.actor_type, "/v1/turn", request)
        if response.status_code not in (200, 202):
            raise self._http_error(request.actor_type, response)
        try:
            result = ActorTurnResponse.model_validate(response.json())
        except Exception as exc:
            raise ActorWorkerError(request.actor_type, response.status_code, "invalid_worker_response") from exc
        if result.turn_id != request.turn_id:
            raise ActorWorkerError(request.actor_type, response.status_code, "worker_turn_id_mismatch")
        if (response.status_code == 202) != (result.status == "started_uncertain"):
            raise ActorWorkerError(request.actor_type, response.status_code, "worker_status_mismatch")
        if result.status == "failed":
            raise ActorWorkerError(request.actor_type, response.status_code, result.error_type or "turn_failed")
        return result

    def evaluate(self, request: ActorEvaluateRequest) -> ActorEvaluateResponse:
        response = self._post(request.actor_type, "/v1/evaluate", request)
        if response.status_code != 200:
            raise self._http_error(request.actor_type, response)
        try:
            return ActorEvaluateResponse.model_validate(response.json())
        except Exception as exc:
            raise ActorWorkerError(request.actor_type, response.status_code, "invalid_worker_response") from exc
