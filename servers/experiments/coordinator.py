"""Server-side client for the authenticated persistent-company coordinator API."""

from __future__ import annotations

import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import quote, urljoin

import requests


class CoordinatorError(RuntimeError):
    """A coordinator operation failed without exposing response payloads."""

    def __init__(self, operation: str, code: str, correlation_id: str | None = None):
        self.operation = operation
        self.code = code
        self.correlation_id = correlation_id
        detail=f"Simulation coordinator {operation} failed ({code})"
        if correlation_id: detail+=f"; correlation_id={correlation_id}"
        super().__init__(detail+'.')


class SimulationCoordinatorClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0,
                 session: requests.Session | None = None):
        if not base_url or not token:
            raise ValueError("coordinator URL and controller token are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()
        self._lock=threading.RLock()

    @classmethod
    def from_env(cls) -> "SimulationCoordinatorClient":
        token_path = Path(os.environ.get(
            "SIMULATION_CONTROLLER_TOKEN_FILE", "/run/secrets/simulation-controller-token"))
        if os.name == "posix" and token_path.stat().st_mode & 0o077:
            raise PermissionError("Simulation controller token file must be private to its owner")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("SIMULATION_CONTROLLER_TOKEN_FILE is empty")
        return cls(base_url=os.environ.get("SIMULATION_COORDINATOR_URL","http://192.0.2.10:8131"),
                   token=token)

    def _request(self, method: str, endpoint: str, *, idempotency_key: str | None = None,
                 **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with self._lock:
                response = self.session.request(
                    method, urljoin(self.base_url, endpoint.lstrip("/")), headers=headers,
                    timeout=self.timeout, **kwargs,
                )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise CoordinatorError(endpoint, "timeout") from error
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else "unknown"
            correlation_id=None
            if error.response is not None and isinstance(status,int) and status>=500:
                candidate=error.response.headers.get('X-Correlation-ID')
                if not candidate:
                    try: candidate=error.response.json().get('correlation_id')
                    except (ValueError,AttributeError): candidate=None
                if isinstance(candidate,str) and re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",candidate):
                    correlation_id=candidate
            raise CoordinatorError(endpoint, f"http_{status}",correlation_id) from error
        except requests.RequestException as error:
            raise CoordinatorError(endpoint, "transport_error") from error
        except ValueError as error:
            raise CoordinatorError(endpoint, "invalid_response") from error
        if not isinstance(payload, dict):
            raise CoordinatorError(endpoint, "invalid_response")
        return payload

    def company(self) -> dict[str, Any]:
        return self._request("GET", "/company")

    def provider_catalog(self) -> dict[str, Any]:
        return self._request("GET", "/company/provider-experiments/catalog")

    def start_provider_experiment(self, provider_id: str, task_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/provider-experiments", idempotency_key=idempotency_key,
                             json={"provider_id": provider_id, "task_id": task_id})

    def provider_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/provider-experiments/{quote(experiment_id, safe='')}")

    def provider_evaluations(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/provider-experiments/{quote(experiment_id, safe='')}/evaluations")

    def latest_provider_evaluation(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/provider-experiments/{quote(experiment_id, safe='')}/evaluations/latest")

    def provider_evaluation(self, experiment_id: str, evaluation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/provider-experiments/{quote(experiment_id, safe='')}/evaluations/{quote(evaluation_id, safe='')}")

    def evaluate_provider(self, experiment_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", f"/company/provider-experiments/{quote(experiment_id, safe='')}/evaluations",
                             idempotency_key=idempotency_key, json={})

    def start_company_evaluation(self, provider_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/company/evaluations/{quote(provider_id, safe='')}",
            idempotency_key=idempotency_key, json={},
        )

    def company_evaluations(self, provider_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/evaluations/{quote(provider_id, safe='')}")

    def latest_company_evaluation(self, provider_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/evaluations/{quote(provider_id, safe='')}/latest")

    def start_company(self, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/start", idempotency_key=idempotency_key, json={})

    def pause_company(self, paused: bool, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/pause", idempotency_key=idempotency_key,
                             json={"paused": paused})

    def control_background_provider(self, provider_id: str, enabled: bool, *, idempotency_key: str) -> dict[str, Any]:
        if provider_id not in {"coffeebench", "ecommercebench"}:
            raise ValueError("unsupported background provider")
        return self._request("POST", f"/company/background/{quote(provider_id, safe='')}/control",
                             idempotency_key=idempotency_key, json={"enabled": enabled})

    def start_experiment(self, scenario_id: str, *, idempotency_key: str, task_id: str | None = None) -> dict[str, Any]:
        payload = {"scenario_id": scenario_id}
        if task_id is not None: payload["task_id"] = task_id
        return self._request("POST", "/company/experiments", idempotency_key=idempotency_key,
                             json=payload)

    def start_evaluation(self, run_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/runs/{quote(run_id, safe='')}/evaluations", idempotency_key=idempotency_key, json={}
        )

    def evaluate(self, run_id: str, *, idempotency_key: str) -> dict[str, Any]:
        """Use the legacy evaluate alias with the durable service's request key."""
        return self._request(
            "POST", f"/scenarios/{quote(run_id, safe='')}/evaluate",
            idempotency_key=idempotency_key,
        )

    def evaluations(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/runs/{quote(run_id, safe='')}/evaluations")

    def latest_evaluation(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/runs/{quote(run_id, safe='')}/evaluations/latest")

    def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/evaluations/{quote(evaluation_id, safe='')}")
