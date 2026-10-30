"""Public input contracts. Private scenario facts are never response models."""
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRunMetadata(Input):
    """Versioned references for a swappable harness; never accepts prompt/tool bodies."""

    harness_id: str | None = Field(default=None, max_length=120)
    harness_version: str | None = Field(default=None, max_length=120)
    harness_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    model_provider: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=200)
    model_version: str | None = Field(default=None, max_length=200)
    prompt_id: str | None = Field(default=None, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=200)
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    tool_config_id: str | None = Field(default=None, max_length=200)
    tool_config_version: str | None = Field(default=None, max_length=200)
    tool_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class StartScenario(Input):
    scenario_id: Literal["A1", "B1", "C1", "D1", "E1", "S01", "TAU_RETAIL"]
    task_id: str | None = Field(default=None, min_length=1, max_length=128,
                                pattern=r"^[A-Za-z0-9_-]+$")
    seed: int = Field(default=0, ge=0)
    agent_id: str = Field(default="viv", min_length=1)
    agent_configuration: dict[str, Any] = Field(default_factory=dict)
    agent_metadata: AgentRunMetadata = Field(default_factory=AgentRunMetadata)

    @model_validator(mode="after")
    def retail_scenario_requires_task(self):
        if self.scenario_id == "TAU_RETAIL" and self.task_id is None:
            raise ValueError("TAU_RETAIL requires an upstream task_id")
        if self.scenario_id != "TAU_RETAIL" and self.task_id is not None:
            raise ValueError("task_id is only valid for TAU_RETAIL")
        return self


class HumanIntervention(Input):
    intervention_type: Literal["rescue", "correction", "manual_action"]
    category: Literal[
        "reasoning", "communication", "tool_use", "policy", "state_recovery", "other",
    ]
    effort_seconds: int | None = Field(default=None, ge=0)
    related_case_id: str | None = Field(default=None, min_length=1, max_length=200,
                                        pattern=r"^[A-Za-z0-9_.:-]+$")
    related_event_id: str | None = Field(default=None, min_length=1, max_length=200,
                                         pattern=r"^[A-Za-z0-9_.:-]+$")


class ModelUsageRecord(Input):
    """Trusted harness telemetry for one measured model invocation."""

    provider: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str | None = Field(default=None, max_length=200)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def cost_currency_pair(self):
        if (self.cost is None) != (self.currency is None):
            raise ValueError("cost and currency must be supplied together")
        return self


class CanonicalEvent(Input):
    event_id: str = Field(min_length=1)
    run_id: UUID
    scenario_id: str = Field(min_length=1)
    simulated_time: AwareDatetime
    event_type: Literal[
        "SHIPMENT_DELAYED", "DELIVERY_ARRIVED", "EMPLOYEE_RESPONSE_READY",
        "PRODUCTION_DEADLINE", "CUSTOMER_REQUIREMENTS_CHANGED",
        "SUPPLIER_PRICING_CHANGED", "PAYMENT_POSTED",
        "SUPPLIER_MARKET_DEMAND", "SUPPLIER_STOCK_REPLENISHED",
        "SUPPLIER_SUPPLY_DISRUPTED", "SUPPLIER_DELIVERY_OUTCOME",
    ]
    source: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    related_entities: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: list[str] = Field(default_factory=list)


class AdvanceClock(Input):
    run_id: UUID
    until: AwareDatetime


class PauseClock(Input):
    run_id: UUID
    paused: bool


class PauseCompany(Input):
    paused: bool


class StartCompany(Input):
    pass


class RelatedDocument(Input):
    doctype: Literal["Sales Order", "Purchase Order", "Sales Invoice", "Purchase Invoice"]
    name: str = Field(min_length=1)


class SendMessage(Input):
    run_id: UUID
    case_id: str = Field(min_length=1)
    to_actor_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    related_document: RelatedDocument | None = None
    in_reply_to: str | None = Field(default=None, min_length=3, max_length=998)


class InvoiceFinding(Input):
    purchase_order_id: str = Field(min_length=1)
    purchase_invoice_id: str = Field(min_length=1)
    quantity_difference: Decimal
    receipt_ids: list[str] = Field(default_factory=list)


class ProductionFinding(Input):
    item_code: str = Field(min_length=1)
    required_quantity: Decimal = Field(gt=0)
    available_quantity: Decimal = Field(ge=0)
    shortage_quantity: Decimal = Field(ge=0)


class CustomerEvidenceFinding(Input):
    sales_order_id: str = Field(min_length=1)
    missing_evidence: Literal["serial_numbers"]


class UpdateCase(Input):
    status: Literal["open", "in_review", "closed"] | None = None
    eta: date | None = None
    notes: str | None = None
    customer_order_id: str | None = Field(default=None, min_length=1)
    invoice_finding: InvoiceFinding | None = None
    production_finding: ProductionFinding | None = None
    customer_evidence: CustomerEvidenceFinding | None = None


class ApprovalDecision(Input):
    run_id: UUID
    case_id: str = Field(min_length=1)
    operation: Literal["create_goods_receipt", "post_payment", "amend_purchase_order", "approve_expedited_shipping"]
    entity_id: str = Field(min_length=1)
    entity_version: str = Field(min_length=1)
    decision: Literal["approved", "denied"]
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None


class ApprovalRequestEmail(Input):
    operation: Literal["create_goods_receipt", "post_payment", "amend_purchase_order", "approve_expedited_shipping"]
    entity_id: str = Field(min_length=1)
    entity_version: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    quantity: Decimal | None = Field(default=None, gt=0)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    quote_message_id: str | None = Field(default=None, min_length=1)
    delivery_commitment: date | None = None


class GoodsReceiptActionParameters(Input):
    case_id: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)


class ShippingCommitActionParameters(Input):
    case_id: str = Field(min_length=1)
    quote_message_id: str = Field(min_length=1)


class ERPAction(Input):
    run_id: UUID
    entity_id: str = Field(min_length=1)
    approval_id: str | None = None
    parameters: GoodsReceiptActionParameters | ShippingCommitActionParameters
