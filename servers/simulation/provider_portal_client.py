"""Private case-registration client for the employee-facing provider portal."""

from __future__ import annotations

import requests
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .provider_contracts import ProviderPortalCaseRegistration


class _RegisteredMailCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_actor_id: str = Field(min_length=1)
    responsible_actor_id: str = Field(min_length=1)
    root_message_id: str = Field(pattern=r"^<[^<>\s]+>$")
    after_uid: int = Field(ge=0, strict=True)
    uidvalidity: int | None = Field(ge=1, strict=True)


class _RegisteredMailReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_actor_id: str = Field(min_length=1)
    message_id: str = Field(pattern=r"^<[^<>\s]+>$")
    sender_actor_id: str = Field(min_length=1)
    recipient_actor_id: str = Field(min_length=1)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(default="", max_length=998)
    body: str = Field(min_length=1, max_length=20000)
    received_at: str = Field(min_length=1, max_length=64)
    mailbox_uid: int = Field(gt=0, strict=True)
    uidvalidity: int = Field(gt=0, strict=True)


class _RegisteredMailReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    case_id: str = Field(min_length=1)
    message_id: str = Field(pattern=r"^<[^<>\s]+>$")
    reply_message_id: str = Field(pattern=r"^<[^<>\s]+>$")


class ProviderPortalClientError(RuntimeError):
    def __init__(self, status_code: int | None, code: str):
        self.status_code, self.code = status_code, code
        super().__init__(f"provider portal operation failed ({code})")


class ProviderPortalClient:
    def __init__(self, base_url: str, bearer_token: SecretStr | str, *, timeout_seconds: float = 15,
                 session: requests.Session | None = None):
        token = bearer_token.get_secret_value() if isinstance(bearer_token, SecretStr) else bearer_token
        if not base_url or not token or timeout_seconds <= 0:
            raise ValueError("provider portal connection is incomplete")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def register_case(self, registration: ProviderPortalCaseRegistration | dict) -> dict:
        request = ProviderPortalCaseRegistration.model_validate(registration)
        try:
            response = self.session.post(
                self.base_url + "/internal/cases",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderPortalClientError(None, "portal_transport_uncertain") from exc
        if response.status_code != 200:
            raise ProviderPortalClientError(response.status_code, "case_registration_failed")
        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderPortalClientError(response.status_code, "invalid_portal_response") from exc
        if result != {"case_id": request.case_id, "status": "registered"}:
            raise ProviderPortalClientError(response.status_code, "invalid_portal_response")
        return result

    def registered_mail_cases(self) -> list[dict]:
        """Read exact registered thread roots and durable cursors."""
        try:
            response = self.session.get(
                self.base_url + "/internal/registered-mail-cases",
                headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderPortalClientError(None, "registered_mail_lookup_failed") from exc
        if response.status_code != 200:
            raise ProviderPortalClientError(response.status_code, "registered_mail_lookup_failed")
        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderPortalClientError(response.status_code, "invalid_registered_mail_response") from exc
        try:
            if not isinstance(result, dict) or set(result) != {"cases"} or not isinstance(result["cases"], list):
                raise ValueError("invalid cases envelope")
            cases = [_RegisteredMailCase.model_validate(item).model_dump(mode="json") for item in result["cases"]]
        except Exception as exc:
            raise ProviderPortalClientError(response.status_code, "invalid_registered_mail_response") from exc
        return cases

    def process_registered_email(self, reply: dict) -> dict:
        request = _RegisteredMailReply.model_validate(reply)
        try:
            response = self.session.post(
                self.base_url + "/internal/observe-mail",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + self.token},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ProviderPortalClientError(None, "mail_observation_uncertain") from exc
        if response.status_code != 200:
            raise ProviderPortalClientError(response.status_code, "mail_observation_failed")
        try:
            receipt = _RegisteredMailReceipt.model_validate(response.json())
        except Exception as exc:
            raise ProviderPortalClientError(response.status_code, "invalid_mail_observation_response") from exc
        if receipt.status != "processed" or receipt.message_id != request.message_id:
            raise ProviderPortalClientError(response.status_code, "invalid_mail_observation_response")
        return receipt.model_dump(mode="json")
