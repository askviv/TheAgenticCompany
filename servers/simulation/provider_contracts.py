"""Private contracts for τ native external-provider operations."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ProviderId = Literal["tau2-airline", "tau2-telecom", "tau2-banking"]


class ProviderSourceFact(BaseModel):
    """A source-anchored private customer fact, never a task instruction."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    label: Literal["customer_identity", "customer_request", "customer_fact"]
    text: str = Field(min_length=1, max_length=2000)
    source_task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_json_pointer: str = Field(min_length=1, max_length=500)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    company_id: Literal["company"] = "company"
    provider_id: ProviderId
    operation_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    task_id: str = Field(min_length=1, max_length=240)
    company_actor_id: str = Field(min_length=1, max_length=200)
    company_account_reference: str = Field(min_length=1, max_length=240)
    company_issue_reference: str = Field(min_length=1, max_length=240)
    message_id: str = Field(min_length=1, max_length=998)
    in_reply_to: str | None = Field(default=None, max_length=998)
    subject: str = Field(default="", max_length=998)
    body: str = Field(min_length=1, max_length=50000)


class ProviderPortalCaseRegistration(BaseModel):
    """Private company-task binding used to register an employee portal case."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1, max_length=240)
    provider_id: ProviderId
    task_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    responsible_actor_id: str = Field(min_length=1, max_length=200)
    responsible_email: str = Field(min_length=3, max_length=320)
    company_account_reference: str = Field(min_length=1, max_length=240)
    issue_reference: str = Field(min_length=1, max_length=240)
    subject: str = Field(min_length=1, max_length=998)
    business_description: str = Field(min_length=1, max_length=20000)
    requested_outcome: str = Field(min_length=1, max_length=10000)
    private_factual_context: list[ProviderSourceFact] = Field(default_factory=list)
    created_at: str = Field(min_length=1, max_length=64)


class ProviderTaskCatalogEntry(BaseModel):
    """Task identifier and curated neutral business topic; excludes τ descriptions."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider_id: ProviderId
    task_id: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=10000)
    classification: Literal["financial", "operational"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    requested_outcome: str | None = Field(default=None, min_length=1, max_length=10000)
    role_fit: Literal["personal_customer", "business_customer", "mixed_customer"] | None = None
    role_fit_note: str | None = Field(default=None, max_length=1000)
    financial_context_status: str | None = Field(default=None, max_length=100)


class ProviderTaskCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tasks: list[ProviderTaskCatalogEntry]


class ProviderCasePrepareRequest(BaseModel):
    """Trusted Library Run binding for no-generation native task setup."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    company_id: Literal["company"] = "company"
    provider_id: ProviderId
    task_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    company_actor_id: str = Field(min_length=1, max_length=200)
    # Company-owned correlation values only. These are not claimed to be source
    # provider account or issue identifiers.
    company_account_reference: str = Field(min_length=1, max_length=240)
    company_issue_reference: str = Field(min_length=1, max_length=240)


class ProviderPortalForm(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    native_user_tool: str = Field(min_length=1, max_length=160)
    source_tool_name: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=2000)
    argument_schema: dict[str, Any]


class ProviderBusinessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    entity_type: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    fields: dict[str, Any]


class ProviderBusinessAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    entity_type: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    fields: dict[str, Any] = Field(default_factory=dict)
    records: list[ProviderBusinessRecord] = Field(default_factory=list)


class ProviderCasePrepareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: ProviderId
    task_id: str
    conversation_id: str
    status: Literal["prepared", "already_prepared", "conflict"]
    initialization_status: Literal["applied", "already_applied", "conflict"]
    initialization_revision: str
    company_account_reference: str
    company_issue_reference: str
    business_description: str
    subject: str
    requested_outcome: str = "Review the linked provider account and respond to the customer request."
    private_factual_context: list[ProviderSourceFact] = Field(default_factory=list)
    account: ProviderBusinessAccount | None = None
    forms: list[ProviderPortalForm] = Field(default_factory=list)


class ProviderEvaluationSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider_id: ProviderId
    conversation_id: str = Field(min_length=1, max_length=240)


class ProviderEvaluationSnapshotReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    capture_ref: str = Field(min_length=1, max_length=240)
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: ProviderId
    task_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    identity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_state_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderEvaluationSnapshotCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    capture_ref: str = Field(min_length=1, max_length=240)
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderEvaluationSnapshotVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    capture_ref: str
    capture_sha256: str
    valid: bool


class NativeProviderReference(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    domain: Literal["telecom", "banking_knowledge"]
    entity_type: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=140)


class ProviderBillEventEvidence(BaseModel):
    """Actual source bill change observed after an upstream native write."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    evidence_id: str = Field(min_length=1, max_length=240)
    event: Literal["refuel_charge_added"]
    company_id: Literal["company"]
    provider_id: Literal["tau2-telecom"]
    task_id: str
    conversation_id: str
    company_actor_id: str
    company_account_reference: str
    company_issue_reference: str
    native_state_revision: str | None = None
    native_references: list[NativeProviderReference] = Field(min_length=3, max_length=3)
    provider_bill_status: Literal["Draft"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    charge_line_index: int = Field(ge=0)
    charge_amount: Decimal = Field(gt=0, max_digits=21, decimal_places=6)
    charge_description: str = Field(min_length=1, max_length=1000)
    provider_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bill_total_due: Decimal = Field(max_digits=21, decimal_places=6)
    bill_period_start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bill_period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    bill_due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProviderBankStateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    evidence_id: str = Field(min_length=1, max_length=240)
    event: Literal["banking_records_changed"]
    company_id: Literal["company"]
    provider_id: Literal["tau2-banking"]
    task_id: str
    conversation_id: str
    company_actor_id: str
    company_account_reference: str
    company_issue_reference: str
    native_state_revision: str
    native_references: list[NativeProviderReference] = Field(min_length=1)
    changed_field_count: int = Field(ge=1)


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: ProviderId
    company_id: Literal["company"] = "company"
    operation_id: str
    conversation_id: str
    task_id: str
    company_actor_id: str
    company_account_reference: str
    company_issue_reference: str
    received_message_id: str
    status: Literal["completed", "started_uncertain"]
    body: str | None = None
    native_source: str
    native_state_revision: str | None = None
    evidence: list[ProviderBillEventEvidence | ProviderBankStateEvidence] = Field(default_factory=list)
    initialization_status: Literal["applied", "already_applied", "conflict"]
    in_reply_to: str
    error_code: str | None = None


class ProviderEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source: Literal["company_erp", "company_bank_portal", "company_provider_portal", "company_email"]
    reference: str = Field(min_length=1, max_length=240)
    doctype: str | None = Field(default=None, max_length=140)
    name: str | None = Field(default=None, max_length=140)


class ProviderFinancialExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source_type: Literal["erp_payment_entry", "bank_transaction"]
    source_reference: ProviderEvidenceReference
    docstatus: Literal[1] | None = None
    status: Literal["posted"]
    amount: Decimal = Field(gt=0, max_digits=21, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    account_reference: str = Field(min_length=1, max_length=240)
    occurred_at: str = Field(min_length=1, max_length=64)


class CompanyProviderPortalAction(BaseModel):
    """Trusted host evidence for a company's real Banking portal action."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: Literal["tau2-banking", "tau2-telecom"]
    operation_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    company_id: Literal["company"] = "company"
    company_actor_id: str = Field(min_length=1, max_length=200)
    company_account_reference: str = Field(min_length=1, max_length=240)
    company_issue_reference: str = Field(min_length=1, max_length=240)
    mapped_provider_user_id: str | None = Field(default=None, max_length=140)
    native_user_tool: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    portal_event: Literal[
        "portal_form_submitted", "portal_action_confirmed", "portal_lookup_completed",
        "posted_financial_execution", "support_request_submitted",
    ]
    evidence_references: list[ProviderEvidenceReference] = Field(min_length=1)
    financial_execution: ProviderFinancialExecutionEvidence | None = None
    correlation_id: str = Field(min_length=1, max_length=240)


class ProviderPortalFormSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    client_action_id: str = Field(min_length=1, max_length=160)
    form_id: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CompanyProviderFinancialAction(BaseModel):
    """ERP or bank-posting evidence authorizing one native Banking write."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: Literal["tau2-banking"]
    operation_id: str = Field(min_length=1, max_length=240)
    conversation_id: str = Field(min_length=1, max_length=240)
    company_id: Literal["company"] = "company"
    company_actor_id: str = Field(min_length=1, max_length=200)
    company_account_reference: str = Field(min_length=1, max_length=240)
    company_issue_reference: str = Field(min_length=1, max_length=240)
    mapped_provider_user_id: str = Field(min_length=1, max_length=140)
    native_agent_tool: Literal[
        "pay_credit_card_from_checking_9182", "transfer_funds_between_bank_accounts_7291",
    ]
    arguments: dict[str, Any]
    financial_execution: ProviderFinancialExecutionEvidence
    evidence_references: list[ProviderEvidenceReference] = Field(min_length=1)
    correlation_id: str = Field(min_length=1, max_length=240)


class ProviderBankActionReceipt(BaseModel):
    """Private receipt for an applied source-native Banking action."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: Literal["tau2-banking", "tau2-telecom"]
    operation_id: str
    conversation_id: str
    status: Literal["applied", "rejected"]
    native_tool: str
    native_result_hash: str
    native_state_revision: str
    source_references: list[ProviderEvidenceReference]


class CompanyPaymentEvidence(BaseModel):
    """Host-verified posted payment evidence for a τ Telecom bill request.

    The provider worker does not resolve ERPNext documents itself. The host
    adapter must populate this from an actual submitted Payment Entry and its
    linked company payable record.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    company_id: Literal["company"] = "company"
    company_actor_id: str = Field(min_length=1, max_length=200)
    company_account_reference: str = Field(min_length=1, max_length=240)
    company_issue_reference: str = Field(min_length=1, max_length=240)
    erp_doctype: Literal["Payment Entry"]
    erp_name: str = Field(min_length=1, max_length=140)
    docstatus: Literal[1]
    provider_bill_id: str = Field(min_length=1, max_length=140)
    amount: Decimal = Field(gt=0, max_digits=21, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    document_modified: str = Field(min_length=1, max_length=64)
    native_references: list[dict[str, Any]] = Field(min_length=1)


class ProviderPaymentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: Literal["tau2-telecom"]
    conversation_id: str
    source_task_id: str
    erp_doctype: Literal["Payment Entry"]
    erp_name: str
    provider_bill_id: str
    provider_bill_status: Literal["Paid"]
    amount: Decimal
    currency: str
    native_tool: Literal["make_payment"] = "make_payment"
    native_result: str
    native_state_revision: str
    native_references: list[dict[str, Any]] = Field(default_factory=list)


class ProviderEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    provider_id: ProviderId
    conversation_id: str
    task_id: str
    evaluation_mode: Literal["company_adapted_native_tau_criteria"]
    status: Literal["completed", "inconclusive"]
    native_reward_info: dict[str, Any] | None = None
    reason_code: str | None = None
    native_state_revision: str | None = None
