"""Shared-clock coordinator client for company background providers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

import requests
import re
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr

from servers.simulation.integration_contracts import (
    BackgroundAdvanceRequest, BackgroundInitializeRequest, BackgroundResult, CompanySourceEvent,
    ExternalSupplyCondition,
)
from servers.simulation.ecommerce_background import (
    PROVIDER_VERSION as ECOMMERCE_PROVIDER_VERSION,
    UPSTREAM_COMMIT as ECOMMERCE_UPSTREAM_COMMIT,
)
from servers.simulation.feedback_contracts import NativeActionReceipt, TranslatedAction
from servers.simulation.store import SQLiteStore, StoreConflict, StoreNotFound


class BackgroundWorkerError(RuntimeError):
    """A private background-worker RPC failed; its message excludes provider data."""

    def __init__(self, status_code: int | None, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(f"background worker operation failed ({code})")


class LegacyEcommerceHandoffIntegrityError(StoreConflict):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CoffeeEvaluationSourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    company_id: str = Field(pattern=r"^company$")
    provider_id: str = Field(pattern=r"^coffeebench$")
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    seed: int = Field(ge=0)
    started_at: str
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    money_metric_units: dict[str, str]


class NativeEvaluationCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    native_state_ref: str = Field(min_length=1, max_length=200)
    native_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_company_time: str = Field(min_length=1)
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    money_metric_units: dict[str, str]
    seed: int = Field(ge=0)
    started_at: str = Field(min_length=1)


class CoffeeCapturedEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider_id: str = Field(pattern=r"^coffeebench$")
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    native_state_ref: str = Field(min_length=1, max_length=200)
    native_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_company_time: str = Field(min_length=1)
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_evaluations: list[dict[str, Any]]


class CoffeeRecoveryInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: str = Field(pattern=r"^(started|completed)$")
    result: dict[str, Any] | None = None
    confirmed_cursor: str | None = None
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class LegacyEcommerceReceiptCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    cursor: str = Field(min_length=1)
    next_due: str | None = None
    coverage_until: str = Field(min_length=1)
    source_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_gap_event_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_ids: list[str]
    source_event_sha256: dict[str, str]
    finance_intent_hashes: dict[str, str]


class BackgroundWorkerClient:
    def __init__(
        self,
        base_url: AnyHttpUrl | str,
        bearer_token: SecretStr | str,
        *,
        timeout_seconds: float,
        session: requests.Session | None = None,
    ):
        if timeout_seconds <= 0:
            raise ValueError("background worker timeout must be positive")
        token = bearer_token.get_secret_value() if isinstance(bearer_token, SecretStr) else bearer_token
        if not token:
            raise ValueError("background worker bearer token is required")
        self.base_url = str(base_url).rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = self.session.post(
                self.base_url + path,
                json=body,
                headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise BackgroundWorkerError(None, "worker_transport_uncertain") from exc
        if response.status_code != 200:
            code = "worker_request_failed"
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                candidate = detail if isinstance(detail, str) else detail.get("error_type") if isinstance(detail, dict) else None
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", candidate):
                    code = candidate
            except ValueError as exc:
                raise BackgroundWorkerError(response.status_code, "invalid_worker_error_response") from exc
            raise BackgroundWorkerError(response.status_code, code)
        try:
            result = response.json()
        except ValueError as exc:
            raise BackgroundWorkerError(response.status_code, "invalid_worker_response") from exc
        return result

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self._post_json(path, body)
        try:
            return BackgroundResult.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_worker_response") from exc

    def initialize(self, request: BackgroundInitializeRequest) -> dict[str, Any]:
        return self._post("/v1/background/initialize", request.model_dump(mode="json"))

    def advance(self, request: BackgroundAdvanceRequest) -> dict[str, Any]:
        return self._post("/v1/background/advance", request.model_dump(mode="json"))

    def inspect_coffee_recovery(self, previous_request: BackgroundAdvanceRequest) -> dict[str, Any]:
        result = self._post_json("/v1/background/coffee/recovery/inspect", {
            "previous_request": previous_request.model_dump(mode="json"),
        })
        try:
            return CoffeeRecoveryInspection.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_coffee_recovery_inspection") from exc

    def lookup_legacy_ecommerce_receipts(
        self, target_cursor: datetime, required_event_ids: list[str], company_id: str = "company",
    ) -> list[dict[str, Any]]:
        result = self._post_json("/v1/background/ecommerce/legacy-receipts", {
            "company_id": company_id, "target_cursor": target_cursor.isoformat(),
            "required_event_ids": sorted(set(required_event_ids)),
        })
        if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
            raise BackgroundWorkerError(200, "invalid_legacy_ecommerce_receipt_response")
        try:
            candidates = [LegacyEcommerceReceiptCandidate.model_validate(item).model_dump(mode="json")
                          for item in result["candidates"]]
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_legacy_ecommerce_receipt_response") from exc
        return candidates

    def recover_coffee_advance(
        self, previous_request: BackgroundAdvanceRequest,
        recovery_request: BackgroundAdvanceRequest, *, confirmed_cursor: str,
        checkpoint_sha256: str,
    ) -> dict[str, Any]:
        return self._post("/v1/background/coffee/recovery/advance", {
            "previous_request": previous_request.model_dump(mode="json"),
            "recovery_request": recovery_request.model_dump(mode="json"),
            "confirmed_cursor": confirmed_cursor,
            "checkpoint_sha256": checkpoint_sha256,
        })

    def apply_company_action(self, action: TranslatedAction | dict[str, Any]) -> dict[str, Any]:
        translated = TranslatedAction.model_validate(action)
        result = self._post_json(
            "/v1/background/company-action", translated.model_dump(mode="json"),
        )
        try:
            return NativeActionReceipt.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_worker_action_receipt") from exc

    def reconcile_company_action(self, action: TranslatedAction | dict[str, Any]) -> dict[str, Any] | None:
        translated = TranslatedAction.model_validate(action)
        result = self._post_json(
            "/v1/background/company-action/reconcile", translated.model_dump(mode="json"),
        )
        if result is None:
            return None
        try:
            return NativeActionReceipt.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_worker_action_receipt") from exc

    def evaluate_coffee_businesses(self, actor_ids: list[str]) -> list[dict[str, Any]]:
        result = self._post_json("/v1/background/evaluate", {"actor_ids": actor_ids})
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise BackgroundWorkerError(200, "invalid_worker_evaluation_response")
        return result

    def coffee_evaluation_source_identity(self) -> dict[str, Any]:
        result = self._post_json("/v1/background/evaluation/source-identity", {})
        try:
            return CoffeeEvaluationSourceIdentity.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_background_evaluation_identity") from exc

    def capture_coffee_evaluation_snapshot(self) -> dict[str, Any]:
        result = self._post_json(
            "/v1/background/evaluation/capture", {"provider_id": "coffeebench"},
        )
        try:
            return NativeEvaluationCapture.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_background_evaluation_capture") from exc

    def evaluate_captured_coffee_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        capture_id = snapshot.get("capture_id")
        if not isinstance(capture_id, str) or not re.fullmatch(r"[0-9a-f]{64}", capture_id):
            raise ValueError("CoffeeBench captured evaluation lacks its opaque capture ID")
        result = self._post_json(
            "/v1/background/evaluation/evaluate-captured", {"capture_id": capture_id},
        )
        try:
            return CoffeeCapturedEvaluation.model_validate(result).model_dump(mode="json")
        except ValueError as exc:
            raise BackgroundWorkerError(200, "invalid_background_evaluation_result") from exc

    def verify_captured_coffee_snapshot(self, snapshot: dict[str, Any]) -> bool:
        capture_id = snapshot.get("capture_id")
        if not isinstance(capture_id, str) or not re.fullmatch(r"[0-9a-f]{64}", capture_id):
            raise ValueError("CoffeeBench captured evaluation lacks its opaque capture ID")
        result = self._post_json(
            "/v1/background/evaluation/verify-captured", {"capture_id": capture_id},
        )
        if type(result) is not bool:
            raise BackgroundWorkerError(200, "invalid_background_evaluation_verification")
        return result


class CompanyBackgroundRuntime:
    """Advance both native backgrounds once for a shared company timestamp.

    Provider operations are sequential, but each uses a durable per-provider
    operation identity. If one provider is uncertain, a retry recovers an
    already completed provider result and never starts a second whole-company
    clock or additional worker process.
    """

    PROVIDERS = ("coffeebench", "ecommercebench")
    ADVANCE_ORDER = ("ecommercebench", "coffeebench")

    def __init__(self, store: SQLiteStore, worker: BackgroundWorkerClient):
        self.store = store
        self.worker = worker

    @staticmethod
    def _recovery_operation_id(company_id: str, previous_operation_id: str, key: str) -> str:
        digest = hashlib.sha256(
            f"{company_id}\0coffeebench\0{previous_operation_id}\0{key}".encode("utf-8")
        ).hexdigest()
        return f"{company_id}:coffeebench:recovery:{digest}"

    @staticmethod
    def _canonical_hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()

    def _ecommerce_receipt(self, operation_id: str, result: dict[str, Any]) -> dict[str, Any]:
        events = [CompanySourceEvent.model_validate(item).model_dump(mode="json")
                  for item in result.get("events", [])]
        cursor = self._utc(datetime.fromisoformat(str(result["cursor"]).replace("Z", "+00:00"))).isoformat()
        return {
            "provider_id": "ecommercebench",
            "operation_id": operation_id,
            "upstream_commit": result.get("upstream_commit"),
            "cursor": cursor,
            "coverage_until": cursor,
            "next_due": result.get("next_due"),
            "result_sha256": self._canonical_hash(result),
            "source_event_ids": sorted(str(event["event_id"]) for event in events),
        }

    @staticmethod
    def _confirmed_receipt_view(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            key: receipt[key] for key in (
                "provider_id", "operation_id", "upstream_commit", "cursor", "next_due",
                "result_sha256", "source_event_ids",
            ) if key in receipt
        } | {"coverage_until": receipt.get("coverage_until") or receipt["cursor"]}

    def _verify_durable_ecommerce_handoff(
        self, company_id: str, coffee_target: datetime, binding: dict[str, Any],
    ) -> bool:
        receipt = binding.get("external_supply_source_receipt")
        conditions = binding.get("external_supply_conditions")
        policy = binding.get("external_supply_condition_policy")
        if (policy is None or not isinstance(receipt, dict) or not isinstance(conditions, list)
                or policy != {
                    "affected_categories": ["all"],
                    "target_scope": "native_coffee_market",
                    "native_effect": "delivery_lag_days",
                }
                or receipt.get("provider_id") != "ecommercebench"
                or receipt.get("upstream_commit") != ECOMMERCE_UPSTREAM_COMMIT
                or not isinstance(receipt.get("cursor"), str)
                or not isinstance(receipt.get("operation_id"), str) or not receipt["operation_id"]
                or not isinstance(receipt.get("result_sha256"), str)
                or len(receipt["result_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in receipt["result_sha256"])):
            return False
        normalized = [ExternalSupplyCondition.model_validate(item).model_dump(mode="json") for item in conditions]
        if receipt.get("conditions_sha256") != self._canonical_hash(normalized):
            return False
        stored_receipt = self.store.get_background_provider_result_receipt(
            "ecommercebench", receipt["operation_id"], require_acknowledged=True, company_id=company_id,
        )
        if stored_receipt is None:
            return False
        event_ids = receipt.get("source_event_ids")
        if (not isinstance(event_ids, list) or event_ids != sorted(set(event_ids))
                or any(not isinstance(item, str) or not item for item in event_ids)):
            return False
        source_cursor = self._utc(datetime.fromisoformat(receipt["cursor"].replace("Z", "+00:00")))
        coverage_until = coffee_target
        next_due_value = receipt.get("next_due", stored_receipt.get("next_due"))
        if source_cursor > coffee_target or next_due_value is None:
            return False
        next_due = self._utc(datetime.fromisoformat(str(next_due_value).replace("Z", "+00:00")))
        if coffee_target >= next_due:
            return False
        if source_cursor < coverage_until:
            expected_identity = self._ecommerce_source_identity_sha256(company_id)
            coverage = self.store.get_background_provider_result_coverage(
                "ecommercebench", receipt["operation_id"], coverage_until.isoformat(), company_id=company_id,
            )
            if (coverage is None or coverage["source_identity_sha256"] != expected_identity
                    or coverage["gap_event_ids_sha256"] != self._canonical_hash([])):
                return False
        elif source_cursor != coverage_until:
            return False
        if receipt.get("coverage_until") not in (None, coverage_until.isoformat(), coverage_until.strftime("%Y-%m-%dT%H:%M:%SZ")):
            return False
        if stored_receipt.get("coverage_until") not in (None, source_cursor.isoformat(), coverage_until.isoformat(),
                                                         coverage_until.strftime("%Y-%m-%dT%H:%M:%SZ")):
            return False
        persisted_events = {str(event["event_id"]): event
                            for event in self.store.list_company_source_events(company_id)}
        if any(
            event_id not in persisted_events
            or persisted_events[event_id].get("source") != "qwenlm-ecommercebench-calendar"
            or persisted_events[event_id].get("upstream_commit") != ECOMMERCE_UPSTREAM_COMMIT
            or self._utc(datetime.fromisoformat(
                str(persisted_events[event_id]["simulated_time"]).replace("Z", "+00:00")
            )) > coffee_target
            for event_id in event_ids
        ):
            return False
        stored_view = self._confirmed_receipt_view(stored_receipt)
        exact_keys = ("provider_id", "operation_id", "upstream_commit", "cursor",
                      "next_due", "result_sha256", "source_event_ids")
        if any(receipt.get(key, stored_view.get(key)) != stored_view.get(key) for key in exact_keys):
            return False
        expected_conditions = self._coffee_supply_conditions(company_id, coffee_target, [])
        expected_conditions.sort(key=lambda item: str(item["event_id"]))
        normalized.sort(key=lambda item: str(item["event_id"]))
        return normalized == expected_conditions

    def _ecommerce_source_identity_sha256(self, company_id: str) -> str:
        initialization = self.store.get_background_initialization_request("ecommercebench", company_id)
        if not isinstance(initialization, dict):
            raise LegacyEcommerceHandoffIntegrityError("legacy_source_identity_unavailable")
        init_binding = initialization.get("binding")
        private_states = initialization.get("actor_private_states")
        if not isinstance(init_binding, dict) or not isinstance(private_states, dict):
            raise LegacyEcommerceHandoffIntegrityError("legacy_source_identity_unavailable")
        supplier_actor_id = init_binding.get("supplier_actor_id")
        private_state = private_states.get(supplier_actor_id)
        if not isinstance(private_state, dict):
            raise LegacyEcommerceHandoffIntegrityError("legacy_source_identity_unavailable")
        business_model = private_state.get("business_model")
        supplier_id = private_state.get("supplier_id")
        store_type = business_model.get("store_type") if isinstance(business_model, dict) else None
        if (not isinstance(supplier_actor_id, str) or not isinstance(supplier_id, str)
                or init_binding.get("supplier_id") != supplier_id
                or not isinstance(store_type, str)):
            raise LegacyEcommerceHandoffIntegrityError("legacy_source_identity_unavailable")
        source_identity = {
            "provider_id": "ecommercebench", "provider_version": ECOMMERCE_PROVIDER_VERSION,
            "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
            "seed": int(initialization["seed"]),
            "started_at": self._utc(datetime.fromisoformat(
                str(initialization["started_at"]).replace("Z", "+00:00")
            )).isoformat(),
            "supplier_actor_id": supplier_actor_id, "supplier_id": supplier_id,
            "store_type": store_type,
        }
        return self._canonical_hash(source_identity)

    def _derive_legacy_ecommerce_handoff(
        self, company_id: str, coffee_target: datetime, binding: dict[str, Any],
        coffee_created_at: str,
    ) -> dict[str, Any]:
        """Recover legacy handoff proof from completed worker results without rewriting the request."""
        conditions = binding.get("external_supply_conditions")
        if (binding.get("external_supply_condition_policy") != {
                "affected_categories": ["all"], "target_scope": "native_coffee_market",
                "native_effect": "delivery_lag_days",
        } or not isinstance(conditions, list)):
            raise LegacyEcommerceHandoffIntegrityError("legacy_handoff_binding_invalid")
        normalized = [ExternalSupplyCondition.model_validate(item).model_dump(mode="json")
                      for item in conditions]
        normalized.sort(key=lambda item: str(item["event_id"]))
        condition_event_ids = {str(item["event_id"]) for item in normalized}
        try:
            candidates = self.worker.lookup_legacy_ecommerce_receipts(
                coffee_target, [], company_id,
            )
        except BackgroundWorkerError as exc:
            raise LegacyEcommerceHandoffIntegrityError("legacy_worker_proof_unavailable") from exc
        expected_source_identity_sha256 = self._ecommerce_source_identity_sha256(company_id)
        persisted = {str(event["event_id"]): event
                     for event in self.store.list_company_source_events(company_id)}
        coffee_created = self._utc(datetime.fromisoformat(coffee_created_at.replace("Z", "+00:00")))
        verified = []
        for candidate in candidates:
            if (candidate["upstream_commit"] != ECOMMERCE_UPSTREAM_COMMIT
                    or candidate["source_identity_sha256"] != expected_source_identity_sha256
                    or candidate["coverage_gap_event_ids_sha256"] != self._canonical_hash([])
                    or self._utc(datetime.fromisoformat(candidate["coverage_until"].replace("Z", "+00:00"))) != coffee_target
                    or self._utc(datetime.fromisoformat(candidate["cursor"].replace("Z", "+00:00"))) > coffee_target
                    or candidate.get("next_due") is None
                    or coffee_target >= self._utc(datetime.fromisoformat(candidate["next_due"].replace("Z", "+00:00")))
                    or self._utc(datetime.fromisoformat(candidate["completed_at"].replace("Z", "+00:00"))) > coffee_created):
                continue
            source_ids = candidate["source_event_ids"]
            if set(candidate["source_event_sha256"]) != set(source_ids):
                continue
            if any(
                event_id not in persisted
                or persisted[event_id]["source"] != "qwenlm-ecommercebench-calendar"
                or persisted[event_id]["upstream_commit"] != ECOMMERCE_UPSTREAM_COMMIT
                or self._utc(datetime.fromisoformat(
                    str(persisted[event_id]["simulated_time"]).replace("Z", "+00:00")
                )) > coffee_target
                for event_id in source_ids
            ):
                continue
            if any(
                candidate["source_event_sha256"].get(event_id) != self._canonical_hash(
                    CompanySourceEvent.model_validate({
                        "schema_version": 1, "company_id": company_id, **persisted[event_id],
                    }).model_dump(mode="json")
                )
                for event_id in source_ids
            ):
                continue
            if any(
                event_id not in persisted
                or self._utc(datetime.fromisoformat(
                    str(self.store.company_source_event_created_at(event_id, company_id)).replace("Z", "+00:00")
                )) > coffee_created and event_id not in source_ids
                for event_id in condition_event_ids
            ):
                continue
            if any(not self.store.has_company_finance_source_intent(intent_id, digest, company_id)
                   for intent_id, digest in candidate["finance_intent_hashes"].items()):
                continue
            signature = (
                candidate["upstream_commit"], candidate["cursor"], candidate["next_due"],
                candidate["coverage_until"], candidate["source_identity_sha256"],
                candidate["coverage_gap_event_ids_sha256"],
                candidate["result_sha256"], tuple(candidate["source_event_ids"]),
                tuple(sorted(candidate["source_event_sha256"].items())),
                tuple(sorted(candidate["finance_intent_hashes"].items())),
            )
            verified.append((signature, candidate))
        if not verified:
            raise LegacyEcommerceHandoffIntegrityError("legacy_worker_proof_not_found")
        if len({item[0] for item in verified}) != 1:
            raise LegacyEcommerceHandoffIntegrityError("legacy_worker_proof_ambiguous")

        expected_conditions = self._coffee_supply_conditions(company_id, coffee_target, [])
        expected_conditions.sort(key=lambda item: str(item["event_id"]))
        if normalized != expected_conditions:
            raise LegacyEcommerceHandoffIntegrityError("legacy_condition_facts_mismatch")

        # Equivalent duplicate operations are retained as separate immutable
        # references; their byte-identical result hashes make either canonical.
        verified.sort(key=lambda item: item[1]["operation_id"])
        for _, candidate in verified:
            self.store.record_derived_background_provider_result_receipt(
                company_id=company_id, provider_id="ecommercebench",
                operation_id=candidate["operation_id"], upstream_commit=candidate["upstream_commit"],
                cursor=self._utc(datetime.fromisoformat(candidate["cursor"].replace("Z", "+00:00"))).isoformat(),
                next_due=candidate["next_due"], result_sha256=candidate["result_sha256"],
                source_event_ids=candidate["source_event_ids"],
                coverage_until=self._utc(datetime.fromisoformat(candidate["coverage_until"].replace("Z", "+00:00"))).isoformat(),
                source_identity_sha256=candidate["source_identity_sha256"],
                coverage_gap_event_ids_sha256=candidate["coverage_gap_event_ids_sha256"],
            )
        canonical = verified[0][1]
        return {
            "provider_id": "ecommercebench", "operation_id": canonical["operation_id"],
            "upstream_commit": canonical["upstream_commit"],
            "cursor": self._utc(datetime.fromisoformat(canonical["cursor"].replace("Z", "+00:00"))).isoformat(),
            "result_sha256": canonical["result_sha256"],
            "source_event_ids": canonical["source_event_ids"],
            "conditions_sha256": self._canonical_hash(normalized),
        }

    def _verify_legacy_handoff_if_needed(
        self, request: BackgroundAdvanceRequest, created_at: str,
    ) -> bool:
        binding = request.binding
        if "external_supply_condition_policy" not in binding:
            return False
        if "external_supply_source_receipt" not in binding:
            self._derive_legacy_ecommerce_handoff(
                request.company_id, self._utc(request.until), binding, created_at,
            )
            return True
        receipt = binding["external_supply_source_receipt"]
        if not isinstance(receipt, dict) or not isinstance(receipt.get("operation_id"), str):
            raise LegacyEcommerceHandoffIntegrityError("legacy_handoff_binding_invalid")
        target = self._utc(request.until)
        source_cursor = self._utc(datetime.fromisoformat(str(receipt["cursor"]).replace("Z", "+00:00")))
        if source_cursor < target and self.store.get_background_provider_result_coverage(
            "ecommercebench", receipt["operation_id"], target.isoformat(), company_id=request.company_id,
        ) is None:
            self._derive_legacy_ecommerce_handoff(
                request.company_id, target, {
                    "external_supply_condition_policy": binding.get("external_supply_condition_policy"),
                    "external_supply_conditions": binding.get("external_supply_conditions"),
                }, created_at,
            )
            return True
        return False

    def _cover_current_ecommerce_result(
        self, company_id: str, target: datetime,
    ) -> dict[str, Any]:
        policy = {
            "affected_categories": ["all"],
            "target_scope": "native_coffee_market",
            "native_effect": "delivery_lag_days",
        }
        binding = {
            "external_supply_condition_policy": policy,
            "external_supply_conditions": self._coffee_supply_conditions(company_id, target, []),
        }
        return self._derive_legacy_ecommerce_handoff(
            company_id, target, binding, datetime.now(timezone.utc).isoformat(),
        )

    def _invoke_coffee_recovery(self, archive: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.worker.recover_coffee_advance(
                BackgroundAdvanceRequest.model_validate(archive["previous_request"]),
                BackgroundAdvanceRequest.model_validate(archive["recovery_request"]),
                confirmed_cursor=archive["confirmed_cursor"],
                checkpoint_sha256=archive["checkpoint_sha256"],
            )
            return {"status": "completed", "result": result}
        except BackgroundWorkerError as error:
            if error.status_code in (None, 202):
                return {"status": "uncertain", "code": error.code}
            raise

    def recover_pending_coffeebench(self, previous_operation_id: str, key: str) -> dict[str, Any]:
        """Explicitly supersede one uncertain Coffee request using its confirmed base."""
        if not previous_operation_id or not key:
            raise ValueError("CoffeeBench recovery requires an operation ID and idempotency key")
        company_id = "company"
        pending = self.store.get_background_provider_pending("coffeebench", company_id)
        if pending is None:
            archived = self.store.get_background_provider_recovery(previous_operation_id, company_id)
            if archived is None:
                raise StoreNotFound("CoffeeBench pending operation not found")
            expected_id = self._recovery_operation_id(company_id, previous_operation_id, key)
            if archived["recovery_operation_id"] != expected_id:
                raise StoreConflict("CoffeeBench recovery idempotency key does not match its archived attempt")
            self._verify_legacy_handoff_if_needed(
                BackgroundAdvanceRequest.model_validate(archived["previous_request"]),
                archived["previous_created_at"],
            )
            invocation = self._invoke_coffee_recovery(archived)
            if invocation["status"] != "completed":
                return {"status": "uncertain", "operation_id": expected_id, "code": invocation["code"]}
            result = invocation["result"]
            return {"status": "completed", "operation_id": expected_id, "cursor": result["cursor"]}

        if pending["operation_id"] != previous_operation_id:
            archived = self.store.get_background_provider_recovery(previous_operation_id, company_id)
            if archived is None or archived["recovery_operation_id"] != pending["operation_id"]:
                raise StoreConflict("CoffeeBench pending operation changed before recovery")
            expected_id = self._recovery_operation_id(company_id, previous_operation_id, key)
            if archived["recovery_operation_id"] != expected_id:
                raise StoreConflict("CoffeeBench recovery idempotency key does not match its archived attempt")
            self._verify_legacy_handoff_if_needed(
                BackgroundAdvanceRequest.model_validate(archived["previous_request"]),
                archived["previous_created_at"],
            )
            if pending["result"] is not None:
                return {"status": "result_pending", "operation_id": pending["operation_id"]}
            invocation = self._invoke_coffee_recovery(archived)
            if invocation["status"] != "completed":
                return {"status": "uncertain", "operation_id": pending["operation_id"],
                        "code": invocation["code"]}
            result = invocation["result"]
            self.store.persist_background_provider_result(
                "coffeebench", pending["operation_id"], pending["request"], result, company_id,
            )
            return {"status": "completed", "operation_id": pending["operation_id"], "cursor": result["cursor"]}

        if pending["result"] is not None:
            self._verify_legacy_handoff_if_needed(
                BackgroundAdvanceRequest.model_validate(pending["request"]), pending["created_at"],
            )
            return {"status": "result_pending", "operation_id": pending["operation_id"]}
        previous_request = BackgroundAdvanceRequest.model_validate(pending["request"])
        self._verify_legacy_handoff_if_needed(previous_request, pending["created_at"])
        inspection = self.worker.inspect_coffee_recovery(previous_request)
        if inspection["status"] == "completed":
            result = inspection["result"]
            self.store.persist_background_provider_result(
                "coffeebench", previous_operation_id, pending["request"], result, company_id,
            )
            return {"status": "completed_original", "operation_id": previous_operation_id,
                    "cursor": result["cursor"]}
        recovery_id = self._recovery_operation_id(company_id, previous_operation_id, key)
        recovery_request = previous_request.model_copy(update={"operation_id": recovery_id})
        archived = self.store.prepare_background_provider_recovery(
            previous_operation_id, recovery_id, recovery_request.model_dump(mode="json"),
            confirmed_cursor=inspection["confirmed_cursor"],
            checkpoint_sha256=inspection["checkpoint_sha256"], company_id=company_id,
        )
        invocation = self._invoke_coffee_recovery(archived)
        if invocation["status"] != "completed":
            return {"status": "uncertain", "operation_id": recovery_id, "code": invocation["code"]}
        result = invocation["result"]
        self.store.persist_background_provider_result(
            "coffeebench", recovery_id, recovery_request.model_dump(mode="json"), result, company_id,
        )
        return {"status": "completed", "operation_id": recovery_id, "cursor": result["cursor"]}

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("company background clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _coffee_supply_conditions(
        self, company_id: str, until: datetime, additional_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Join persisted E-Commerce source events to exact upstream effects."""
        events = self.store.list_company_source_events(company_id)
        by_id = {str(event["event_id"]): event for event in events}
        for event in additional_events:
            event = CompanySourceEvent.model_validate(event).model_dump(mode="json")
            event_id = str(event["event_id"])
            prior = by_id.get(event_id)
            fact_fields = ("source", "upstream_commit", "event_type", "simulated_time",
                           "entity_type", "entity_id", "payload", "visibility")
            if prior is not None:
                for key in fact_fields:
                    left, right = prior.get(key), event.get(key)
                    if key == "simulated_time":
                        left = self._utc(datetime.fromisoformat(str(left).replace("Z", "+00:00"))).isoformat()
                        right = self._utc(datetime.fromisoformat(str(right).replace("Z", "+00:00"))).isoformat()
                    if left != right:
                        raise ValueError("E-Commerce source event changed before condition handoff")
            by_id[event_id] = event

        conditions = []
        for event in by_id.values():
            if event.get("event_type") != "SUPPLIER_SUPPLY_DISRUPTED":
                continue
            if event.get("source") != "qwenlm-ecommercebench-calendar":
                raise ValueError("supplier disruption has an unexpected source identity")
            occurred_at = self._utc(datetime.fromisoformat(str(event["simulated_time"]).replace("Z", "+00:00")))
            if occurred_at > until:
                continue
            payload = event.get("payload", {})
            # Category-specific E-Commerce shocks do not describe CoffeeBench
            # inputs. Only upstream global logistics conditions have a direct
            # native Coffee delivery-lag counterpart.
            if payload.get("affected_categories") != "all":
                continue
            duration_days = int(payload["upstream_calendar_duration_days"])
            condition = ExternalSupplyCondition.model_validate({
                "event_id": event["event_id"],
                "source": event["source"],
                "upstream_commit": event["upstream_commit"],
                "occurred_at": occurred_at,
                "effective_until": occurred_at + timedelta(days=duration_days),
                "duration_days": duration_days,
                "event_name": payload["upstream_event_name"],
                "store_type": payload["store_type"],
                "affected_categories": payload["affected_categories"],
                "delivery_delay_multiplier": payload["delivery_delay_multiplier"],
            })
            conditions.append(condition.model_dump(mode="json"))
        return sorted(conditions, key=lambda condition: (condition["occurred_at"], condition["event_id"]))

    def initialize_company(
        self,
        *,
        seed: int,
        started_at: datetime,
        bindings: dict[str, dict[str, Any]],
        actor_private_states: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        start = self._utc(started_at)
        day_anchor = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}
        for provider_id in self.PROVIDERS:
            if provider_id not in bindings:
                raise ValueError(f"missing explicit {provider_id} company binding")
            request = BackgroundInitializeRequest(
                provider_id=provider_id,
                seed=seed,
                started_at=day_anchor,
                binding=bindings[provider_id],
                actor_private_states=actor_private_states if provider_id == "ecommercebench" else {},
            )
            request = BackgroundInitializeRequest.model_validate(
                self.store.background_initialization_request(request.model_dump(mode="json"))
            )
            try:
                results[provider_id] = self.worker.initialize(request)
            except BackgroundWorkerError as exc:
                errors[provider_id] = {"status_code": exc.status_code, "code": exc.code}
        return {"status": "partial" if errors else "complete", "providers": results, "errors": errors}

    def advance_company(
        self,
        until: datetime,
        *,
        bindings: dict[str, dict[str, Any]],
        company_id: str = "company",
        provider_ids: tuple[str, ...] | list[str] | None = None,
        coupling_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        boundary = self._utc(until)
        selected = tuple(self.PROVIDERS if provider_ids is None else provider_ids)
        if not selected or len(set(selected)) != len(selected) or any(
            provider_id not in self.PROVIDERS or provider_id not in bindings for provider_id in selected
        ):
            raise ValueError("company background provider selection is invalid")
        if coupling_policy is not None and coupling_policy != {
            "affected_categories": ["all"],
            "target_scope": "native_coffee_market",
            "native_effect": "delivery_lag_days",
        }:
            raise ValueError("unsupported E-Commerce to Coffee coupling policy")
        request_body = {
            "company_id": company_id,
            "until": boundary.isoformat(),
            "provider_bindings": bindings,
            "provider_ids": list(selected),
        }
        if coupling_policy is not None:
            request_body["coupling_policy"] = coupling_policy
        tick_id = boundary.isoformat() + ":" + ",".join(selected)
        claim = self.store.claim_background_tick(tick_id, request_body, company_id)
        if claim["status"] == "completed":
            return claim["result"]

        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}
        operation_ids: dict[str, str] = {}
        next_due_values: list[datetime] = []
        deferred = False
        ecom_result_events: list[dict[str, Any]] = []
        ecom_result_receipt: dict[str, Any] | None = None
        ecom_target: datetime | None = None
        ecom_result_confirmed = False
        if (coupling_policy is not None and "coffeebench" in selected
                and "ecommercebench" not in selected
                and self.store.get_background_provider_pending("coffeebench", company_id) is None):
            latest_ecommerce = self.store.latest_acknowledged_background_provider_result_receipt(
                "ecommercebench", company_id=company_id,
            )
            if latest_ecommerce is not None:
                latest_cursor = self._utc(datetime.fromisoformat(
                    str(latest_ecommerce["cursor"]).replace("Z", "+00:00")
                ))
                next_due_value = latest_ecommerce.get("next_due")
                source_current = (
                    next_due_value is None
                    or boundary < self._utc(datetime.fromisoformat(str(next_due_value).replace("Z", "+00:00")))
                )
                if latest_cursor <= boundary and source_current:
                    ecom_target = boundary
                    ecom_result_confirmed = True
                    if latest_cursor < boundary:
                        try:
                            ecom_result_receipt = self._cover_current_ecommerce_result(company_id, boundary)
                        except LegacyEcommerceHandoffIntegrityError as exc:
                            errors["coffeebench"] = {"status_code": None, "code": exc.code}
                    else:
                        ecom_result_receipt = self._confirmed_receipt_view(latest_ecommerce)
        for provider_id in (value for value in self.ADVANCE_ORDER if value in selected):
            pending = self.store.get_background_provider_pending(provider_id, company_id)
            pending_until: datetime | None = None
            legacy_handoff_verified = False
            if pending is not None:
                pending_request = BackgroundAdvanceRequest.model_validate(pending["request"])
                pending_until = self._utc(pending_request.until)
                if provider_id == "coffeebench" and coupling_policy is not None:
                    try:
                        legacy_handoff_verified = self._verify_legacy_handoff_if_needed(
                            pending_request, pending["created_at"],
                        )
                    except LegacyEcommerceHandoffIntegrityError as exc:
                        errors[provider_id] = {
                            "status_code": None, "code": exc.code,
                            "pending_operation_id": pending["operation_id"],
                            "pending_until": pending_until.isoformat(),
                        }
                        continue
            elif (provider_id == "coffeebench" and coupling_policy is not None
                  and "ecommercebench" not in selected and ecom_result_receipt is None):
                errors[provider_id] = errors.get(provider_id, {
                    "status_code": None, "code": "source_provider_not_current",
                })
                continue
            if (provider_id == "coffeebench" and "ecommercebench" in selected
                    and coupling_policy is not None):
                coffee_target = pending_until or boundary
                if (not ecom_result_confirmed or ecom_target is None
                        or coffee_target > ecom_target
                        or (pending is None and ecom_target != boundary)):
                    no_ecommerce_pending = (
                        self.store.get_background_provider_pending("ecommercebench", company_id) is None
                    )
                    durable_handoff = (
                        pending is not None and (
                            legacy_handoff_verified
                            or (no_ecommerce_pending and self._verify_durable_ecommerce_handoff(
                                company_id, coffee_target, pending_request.binding,
                            ))
                        )
                    )
                    if not durable_handoff:
                        errors[provider_id] = {
                            "status_code": None,
                            "code": "source_provider_not_current" if not legacy_handoff_verified
                            else "legacy_handoff_target_mismatch",
                        }
                        continue
                    ecom_target = coffee_target
                    ecom_result_confirmed = True
            if pending is not None:
                prior_request = BackgroundAdvanceRequest.model_validate(pending["request"])
                prior_until = self._utc(prior_request.until)
                if prior_until > boundary:
                    raise ValueError("pending provider target is later than the requested company clock")
                result = pending["result"]
                if result is None:
                    try:
                        recovery = (
                            self.store.get_background_provider_recovery_for_operation(
                                pending["operation_id"], company_id,
                            ) if provider_id == "coffeebench" else None
                        )
                        if recovery is not None:
                            result = self.worker.recover_coffee_advance(
                                BackgroundAdvanceRequest.model_validate(recovery["previous_request"]),
                                BackgroundAdvanceRequest.model_validate(recovery["recovery_request"]),
                                confirmed_cursor=recovery["confirmed_cursor"],
                                checkpoint_sha256=recovery["checkpoint_sha256"],
                            )
                        else:
                            result = self.worker.advance(prior_request)
                    except BackgroundWorkerError as exc:
                        errors[provider_id] = {
                            "status_code": exc.status_code, "code": exc.code,
                            "pending_operation_id": pending["operation_id"],
                            "pending_until": prior_until.isoformat(),
                        }
                        continue
                    self.store.persist_background_provider_result(
                        provider_id, pending["operation_id"], pending["request"], result, company_id,
                    )
                results[provider_id] = result
                operation_ids[provider_id] = pending["operation_id"]
                deferred = deferred or prior_until < boundary
                if provider_id == "ecommercebench":
                    ecom_target = prior_until
                    ecom_result_confirmed = True
                    ecom_result_events = list(result.get("events", []))
                    ecom_result_receipt = self._ecommerce_receipt(pending["operation_id"], result)
                next_due = result.get("next_due")
                if next_due is not None:
                    next_due_values.append(self._utc(datetime.fromisoformat(next_due.replace("Z", "+00:00"))))
                continue

            operation_id = f"{company_id}:{provider_id}:{tick_id}"
            request_binding = bindings[provider_id]
            if provider_id == "coffeebench" and coupling_policy is not None:
                conditions = self._coffee_supply_conditions(
                    company_id, boundary, ecom_result_events,
                )
                if ecom_result_receipt is None:
                    raise StoreConflict("CoffeeBench condition handoff lacks its confirmed E-Commerce result")
                handoff_receipt = {
                    **ecom_result_receipt,
                    "conditions_sha256": self._canonical_hash(conditions),
                }
                request_binding = {
                    **bindings[provider_id],
                    "external_supply_condition_policy": coupling_policy,
                    "external_supply_conditions": conditions,
                    "external_supply_source_receipt": handoff_receipt,
                }
            request = BackgroundAdvanceRequest(
                provider_id=provider_id,
                operation_id=operation_id,
                until=boundary,
                binding=request_binding,
            )
            request_data = request.model_dump(mode="json")
            self.store.persist_background_provider_pending(
                provider_id, operation_id, request_data, company_id,
            )
            try:
                result = self.worker.advance(request)
            except BackgroundWorkerError as exc:
                errors[provider_id] = {
                    "status_code": exc.status_code, "code": exc.code,
                    "pending_operation_id": operation_id,
                    "pending_until": boundary.isoformat(),
                }
                continue
            self.store.persist_background_provider_result(provider_id, operation_id, request_data, result, company_id)
            results[provider_id] = result
            operation_ids[provider_id] = operation_id
            if provider_id == "ecommercebench":
                ecom_target = boundary
                ecom_result_confirmed = True
                ecom_result_events = list(result.get("events", []))
                ecom_result_receipt = self._ecommerce_receipt(operation_id, result)
            next_due = result.get("next_due")
            if next_due is not None:
                next_due_values.append(self._utc(datetime.fromisoformat(next_due.replace("Z", "+00:00"))))
        next_due = min(next_due_values) if next_due_values else None
        combined = {
            "company_id": company_id,
            "simulated_time": boundary.isoformat(),
            "status": "partial" if errors or deferred else "complete",
            "providers": results,
            "provider_operation_ids": operation_ids,
            "provider_errors": errors,
            "next_due": None if next_due is None else next_due.isoformat(),
        }
        if errors or deferred:
            # Keep this tick claim started. A replay of the same target must
            # reconcile each provider's exact durable operation before it can
            # become a completed company tick.
            return combined
        return self.store.complete_background_tick(tick_id, request_body, combined, company_id)


__all__ = ["BackgroundWorkerError", "BackgroundWorkerClient", "CompanyBackgroundRuntime"]
