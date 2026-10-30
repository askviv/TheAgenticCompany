"""Private evidence contracts: company records are facts, benchmark state is a projection."""

from __future__ import annotations
from typing import Any, Literal, Protocol
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Benchmark = Literal["coffeebench", "ecommercebench", "tau_retail"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RecordMapping(Contract):
    mapping_id: str = Field(min_length=1)
    company_id: Literal["company"] = "company"
    benchmark: Benchmark
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_record_id: str = Field(min_length=1)
    source_entity_type: str = Field(min_length=1)
    erp_doctype: str = Field(min_length=1)
    erp_name: str = Field(min_length=1)
    experiment_id: str | None = None
    source_time: AwareDatetime | None = None
    company_time: AwareDatetime
    native_context: dict[str, Any] = Field(default_factory=dict)
    message_ids: list[str] = Field(default_factory=list)


class CompanyEvidence(Contract):
    evidence_id: str = Field(min_length=1)
    company_id: Literal["company"] = "company"
    erp_doctype: str = Field(min_length=1)
    erp_name: str = Field(min_length=1)
    event: Literal[
        "on_submit",
        "on_cancel",
        "on_update_after_submit",
        "on_update",
        "on_trash",
        "email_delivered",
        "email_reply",
    ]
    actor: str = Field(min_length=1)
    occurred_at: AwareDatetime
    company_time: AwareDatetime | None = None
    document: dict[str, Any]
    before_document: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(min_length=1)
    message_id: str | None = None
    authenticated_sender_actor_id: str | None = None
    recipient_actor_id: str | None = None
    in_reply_to: str | None = None
    experiment_id: str | None = None


class TranslatedAction(Contract):
    action_id: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    benchmark: Benchmark
    native_action: str = Field(min_length=1)
    actor_id: str | None = None
    arguments: dict[str, Any]
    supporting_evidence: list[dict[str, Any]] = Field(default_factory=list)
    company_time: AwareDatetime | None = None


class TranslationResult(Contract):
    status: Literal["supported", "unmapped", "inconclusive"]
    action: TranslatedAction | None = None
    reason: str
    coverage: list[str] = Field(default_factory=list)


class NativeActionReceipt(Contract):
    action_id: str
    accepted: bool
    reconciled: bool
    native_state_ref: str
    outcome: dict[str, Any] = Field(default_factory=dict)


class FeedbackAdapter(Protocol):
    def translate(
        self, mapping: RecordMapping, evidence: CompanyEvidence
    ) -> TranslationResult: ...
    def apply(self, action: TranslatedAction) -> NativeActionReceipt: ...
    def reconcile(self, action: TranslatedAction) -> NativeActionReceipt | None: ...
    def evaluate(
        self, mappings: list[RecordMapping], evidence: list[CompanyEvidence]
    ) -> dict[str, Any]: ...


class NativeDocumentEvent(Contract):
    event: Literal[
        "on_submit", "on_cancel", "on_update_after_submit", "on_update", "on_trash"
    ]
    erp_timezone: str
    linked_company: str | None = None
    document: dict[str, Any]


def read_scalar(document: dict[str, Any], path: str) -> Any:
    """Read an explicitly mapped native document field; never infer missing facts."""
    value: Any = document
    for component in path.split("."):
        if isinstance(value, dict):
            value = value[component]
        elif (
            isinstance(value, list)
            and component.isdecimal()
            and int(component) < len(value)
        ):
            value = value[int(component)]
        else:
            raise KeyError(path)
    if value is None or isinstance(value, (dict, list)):
        raise ValueError(path)
    return value


def read_evidence_scalar(evidence: dict[str, Any], path: str) -> Any:
    """Resolve exact native fields or an explicitly recorded predecessor."""
    if path.startswith("before_document."):
        prior = evidence.get("before_document")
        if not isinstance(prior, dict):
            raise KeyError(path)
        return read_scalar(prior, path.removeprefix("before_document."))
    return read_scalar(evidence["document"], path)
