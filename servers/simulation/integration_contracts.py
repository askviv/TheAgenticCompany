"""Shared private envelopes for native benchmark adapters, never agent tools."""
from __future__ import annotations

from typing import Any, Literal
from decimal import Decimal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CompanyIssueRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    employee_id: str = Field(alias="employeeId", min_length=1)
    actor_id: str = Field(alias="actorId", min_length=1)
    responsible_role: str = Field(alias="responsibleRole", min_length=1)
    requested_outcome: str = Field(alias="requestedOutcome", min_length=1)


def resolve_company_issue_route(profile: dict[str, Any]) -> tuple[CompanyIssueRoute, dict[str, Any]]:
    """Resolve the explicit role assignment from the authoritative company JSON."""
    route = CompanyIssueRoute.model_validate(profile["company"]["issueRouting"]["logistics_disruption"])
    employees = [employee for employee in profile["employees"] if employee["id"] == route.employee_id]
    if len(employees) != 1 or route.responsible_role not in employees[0]["erpRoles"]:
        raise ValueError("Company issue route has no unique employee with its declared role")
    return route, employees[0]


class IntegrationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    company_id: Literal["company"] = "company"
    provider_id: Literal["coffeebench", "ecommercebench"]


class BackgroundInitializeRequest(IntegrationEnvelope):
    seed: int = Field(ge=0)
    started_at: AwareDatetime
    binding: dict[str, Any]
    actor_private_states: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BackgroundAdvanceRequest(IntegrationEnvelope):
    operation_id: str = Field(min_length=1, max_length=240)
    until: AwareDatetime
    binding: dict[str, Any]


class BackgroundResult(IntegrationEnvelope):
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    cursor: AwareDatetime
    status: Literal["active", "finished"] = "active"
    next_due: AwareDatetime | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    outcomes: list[dict[str, Any]] = Field(default_factory=list)
    erp_intents: list[dict[str, Any]] = Field(default_factory=list)


class FinanceSourceRecord(BaseModel):
    """A source fact awaiting native ERP reconciliation, not a payment command."""
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    id: str = Field(min_length=1)
    source: Literal["sakanaai-coffeebench-native-economy", "qwenlm-ecommercebench-calendar", "qwenlm-ecommercebench-native-economy"]
    kind: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    occurred_at: AwareDatetime
    party: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(ge=0, allow_inf_nan=False)


class CompanySourceEvent(BaseModel):
    """An upstream business event owned by the company, independent of experiments."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    company_id: Literal["company"] = "company"
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    simulated_time: AwareDatetime
    source: str = Field(min_length=1)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: list[str] = Field(default_factory=list)


class ExternalSupplyCondition(BaseModel):
    """A source-grounded global logistics condition passed into CoffeeBench."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    event_id: str = Field(min_length=1)
    source: Literal["qwenlm-ecommercebench-calendar"]
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    occurred_at: AwareDatetime
    effective_until: AwareDatetime
    duration_days: int = Field(ge=1)
    event_name: str = Field(min_length=1)
    store_type: str = Field(min_length=1)
    affected_categories: Literal["all"]
    delivery_delay_multiplier: Decimal = Field(ge=1, allow_inf_nan=False)
