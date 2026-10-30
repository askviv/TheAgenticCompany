"""Versioned, secret-free envelope shared by isolated upstream actor workers."""

from __future__ import annotations

import hmac
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def bearer_token_matches(authorization: str | None, expected: str | None) -> bool:
    """Constant-time bearer comparison shared by worker apps."""
    if expected is None or authorization is None:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class InboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=998)
    subject: str = Field(max_length=998)
    body: str = Field(min_length=1)


class ActorTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    turn_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=200)
    actor_type: Literal["customer", "supplier"]
    scenario: dict[str, Any]
    state: dict[str, Any] | None = None
    inbound_message: InboundMessage
    visible_context: dict[str, Any] = Field(default_factory=dict)


class ActorUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class SupplierKernelStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turns: list[dict[str, Any]] = Field(default_factory=list)


class SupplierStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kernel_state: SupplierKernelStatePatch


class ActorTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    turn_id: str = Field(min_length=1, max_length=240)
    status: Literal["completed", "started_uncertain", "failed"]
    body: str | None = None
    state: dict[str, Any] | None = None
    supplier_private_state_patch: SupplierStatePatch | None = None
    conversation_pointer: str | None = Field(default=None, max_length=240)
    quote: dict[str, Any] | None = None
    disclosed_facts: list[str] = Field(default_factory=list)
    required_actions: list[dict[str, Any]] = Field(default_factory=list)
    usage: ActorUsage | None = None
    error_type: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_status_content(self):
        if self.status == "completed" and not self.body:
            raise ValueError("completed actor turns require a nonempty body")
        if self.status != "completed" and self.body is not None:
            raise ValueError("incomplete actor turns cannot return a message body")
        return self


class ActorEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=200)
    actor_type: Literal["customer"] = "customer"
    task: dict[str, Any]
    trajectory: list[dict[str, Any]]


class ActorEvaluateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator: Literal["tau2-action+substring-communicate"]
    action_reward: float = Field(ge=0, le=1)
    communicate_reward: float = Field(ge=0, le=1)
    reward: float = Field(ge=0, le=1)
    action_checks: list[dict[str, Any]]
    communicate_checks: list[dict[str, Any]]


class Tau2RetailSeedRequest(BaseModel):
    """Request a first customer email from the pinned τ retail UserSimulator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    turn_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=240)
    message_id: str = Field(min_length=1, max_length=998)


class Tau2RetailEvaluateRequest(BaseModel):
    """Private evaluation input containing company-observed state and email transcript."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=240)
    observed_db: dict[str, Any]
    unmapped: list[dict[str, Any]] = Field(default_factory=list)
    trajectory: list[dict[str, Any]]


class Tau2RetailFeedbackActionRequest(BaseModel):
    """Private company evidence action against an ERP-projected τ RetailDB."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=240)
    experiment_id: str = Field(min_length=1, max_length=240)
    action: dict[str, Any]


class Tau2RetailFeedbackInitializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=240)
    experiment_id: str = Field(min_length=1, max_length=240)
    observed_db: dict[str, Any]


class Tau2RetailFeedbackReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=240)
    experiment_id: str = Field(min_length=1, max_length=240)
    action: dict[str, Any]


class Tau2RetailFeedbackEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=240)
    experiment_id: str = Field(min_length=1, max_length=240)
    observed_db: dict[str, Any]
    unmapped: list[dict[str, Any]] = Field(default_factory=list)
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
