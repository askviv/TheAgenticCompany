"""Server-side client for the authenticated persistent-company coordinator API."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urljoin

import requests

_DEFAULT_TIMEOUT = object()


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
    def __init__(self, base_url: str, token: str, *, timeout: float | None = None,
                 session: requests.Session | None = None,
                 session_factory: Any = requests.Session):
        if not base_url or not token:
            raise ValueError("coordinator URL and controller token are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.session = session
        self.session_factory = session_factory

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
                 request_timeout: float | None | object = _DEFAULT_TIMEOUT,
                 **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request_session = self.session if self.session is not None else self.session_factory()
        timeout = self.timeout if request_timeout is _DEFAULT_TIMEOUT else request_timeout
        try:
            response = request_session.request(
                method, urljoin(self.base_url, endpoint.lstrip("/")), headers=headers,
                timeout=timeout, **kwargs,
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
            code = f"http_{status}"
            if error.response is not None:
                try:
                    detail = error.response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", detail):
                    code = detail
            raise CoordinatorError(endpoint, code,correlation_id) from error
        except requests.RequestException as error:
            raise CoordinatorError(endpoint, "transport_error") from error
        except ValueError as error:
            raise CoordinatorError(endpoint, "invalid_response") from error
        finally:
            if self.session is None:
                request_session.close()
        if not isinstance(payload, dict):
            raise CoordinatorError(endpoint, "invalid_response")
        return payload

    def company_environments(self) -> dict[str, Any]:
        return self._request("GET", "/company/environments")

    def clone_company_environment(self, name: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/environments/clone",
                             idempotency_key=idempotency_key, json={"name": name})

    def switch_company_environment(self, identifier: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", f"/company/environments/{quote(identifier, safe='')}/switch",
                             idempotency_key=idempotency_key, json={})

    def rename_company_environment(self, identifier: str, name: str) -> dict[str, Any]:
        return self._request("POST", f"/company/environments/{quote(identifier, safe='')}/rename",
                             json={"name": name})

    def company_packs(self) -> dict[str, Any]:
        return self._request("GET", "/company/packs")

    def company_pack(self, pack_id: str) -> dict[str, Any]:
        return self._request("GET", f"/company/packs/{quote(pack_id, safe='')}")

    def upload_company_pack(self, archive: Any, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/packs", data=archive,
                             idempotency_key=idempotency_key)

    def import_company_pack(self, pack_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", f"/company/packs/{quote(pack_id, safe='')}/import",
                             idempotency_key=idempotency_key, json={})

    def company_pack_catalog(self) -> dict[str, Any]:
        return self._request("GET", "/company/packs/catalog")

    def company(self) -> dict[str, Any]:
        return self._request("GET", "/company")

    def company_desktops(self) -> dict[str, Any]:
        return self._request(
            "GET", "/company/desktops", request_timeout=None,
        )

    def start_company_desktop(self, employee_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST", "/company/desktops/start",
            idempotency_key=idempotency_key, request_timeout=None,
            json={"employee_id": employee_id},
        )

    def stop_company_desktop(self, employee_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST", "/company/desktops/stop",
            idempotency_key=idempotency_key, request_timeout=None,
            json={"employee_id": employee_id},
        )

    def dispatch_experiment_chat(
        self, *, idempotency_key: str, role: str, employee_id: str,
        room_id: str, message_text: str, mention_account_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/company/experiment-chat/dispatch",
            idempotency_key=idempotency_key, request_timeout=None,

            json={
                "role": role,
                "employee_id": employee_id,
                "room_id": room_id,
                "message_text": message_text,
                "mention_account_id": mention_account_id,
            },
        )

    def start_provider_experiment(self, provider_id: str, task_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/provider-experiments", idempotency_key=idempotency_key,
                             request_timeout=None,
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

    def company_clock(self) -> dict[str, Any]:
        return self._request("GET", "/company/clock")

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

    def set_company_clock_speed(self, speed: float, *, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/company/clock/speed", idempotency_key=idempotency_key,
                             json={"speed": speed})

    def start_experiment(self, scenario_id: str, *, idempotency_key: str, task_id: str | None = None) -> dict[str, Any]:
        payload = {"scenario_id": scenario_id}
        if task_id is not None: payload["task_id"] = task_id
        return self._request("POST", "/company/experiments", idempotency_key=idempotency_key,
                             request_timeout=None, json=payload)

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
