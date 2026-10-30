"""Controller RPC client for the authenticated native τ provider worker."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from .actor_client import ActorWorkerSettings
from .provider_contracts import (
    ProviderCasePrepareRequest, ProviderCasePrepareResponse, ProviderEvaluationResult,
    ProviderEvaluationSnapshotCheck, ProviderEvaluationSnapshotReceipt,
    ProviderEvaluationSnapshotVerification, ProviderTaskCatalog,
)


class ProviderWorkerError(RuntimeError):
    def __init__(self, status_code: int | None, code: str, *, uncertain: bool):
        self.status_code = status_code
        self.code = code
        self.uncertain = uncertain
        super().__init__(f"native provider worker operation failed ({code})")


class ProviderExperimentWorkerClient:
    """Reuse the configured customer-worker endpoint and its private token."""

    def __init__(self, settings: ActorWorkerSettings, *, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
        base_url = self.settings.customer_url
        token = self.settings.customer_token
        if base_url is None or token is None or self.settings.request_timeout_seconds is None:
            raise ProviderWorkerError(None, "worker_not_configured", uncertain=False)
        try:
            response = self.session.request(
                method, str(base_url).rstrip("/") + path,
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                json=json_body, timeout=self.settings.request_timeout_seconds,
            )
        except requests.Timeout as exc:
            raise ProviderWorkerError(None, "worker_timeout", uncertain=True) from exc
        except requests.RequestException as exc:
            raise ProviderWorkerError(None, "worker_transport_uncertain", uncertain=True) from exc
        if response.status_code != 200:
            uncertain = response.status_code >= 500
            code = "worker_server_error" if uncertain else "worker_http_error"
            raise ProviderWorkerError(response.status_code, code, uncertain=uncertain)
        try:
            value = response.json()
        except ValueError as exc:
            raise ProviderWorkerError(response.status_code, "invalid_worker_response", uncertain=True) from exc
        if not isinstance(value, dict):
            raise ProviderWorkerError(response.status_code, "invalid_worker_response", uncertain=True)
        return value

    def catalog(self) -> dict[str, Any]:
        return ProviderTaskCatalog.model_validate(
            self._request("GET", "/v1/external-provider/catalog")
        ).model_dump(mode="json")

    def prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        parsed = ProviderCasePrepareRequest.model_validate(request)
        response = self._request("POST", "/v1/external-provider/prepare",
                                 json_body=parsed.model_dump(mode="json"))
        return ProviderCasePrepareResponse.model_validate(response).model_dump(mode="json")

    def capture_evaluation(self, provider_id: str, conversation_id: str) -> dict[str, Any]:
        path = ("/v1/external-provider/conversations/" + quote(conversation_id, safe="")
                + "/evaluation-snapshot?provider_id=" + quote(provider_id, safe=""))
        return ProviderEvaluationSnapshotReceipt.model_validate(self._request("GET", path)).model_dump(mode="json")

    def evaluate_captured(self, provider_id: str, conversation_id: str,
                          capture_ref: str, capture_sha256: str) -> dict[str, Any]:
        path = "/v1/external-provider/evaluate-captured"
        request = ProviderEvaluationSnapshotCheck(capture_ref=capture_ref, capture_sha256=capture_sha256)
        return ProviderEvaluationResult.model_validate(self._request(
            "POST", path, json_body=request.model_dump(mode="json"),
        )).model_dump(mode="json")

    def verify_evaluation_capture(self, provider_id: str, conversation_id: str,
                                  capture_ref: str, capture_sha256: str) -> bool:
        path = "/v1/external-provider/verify-evaluation-snapshot"
        request = ProviderEvaluationSnapshotCheck(capture_ref=capture_ref, capture_sha256=capture_sha256)
        value = ProviderEvaluationSnapshotVerification.model_validate(self._request(
            "POST", path, json_body=request.model_dump(mode="json"),
        ))
        return value.valid
