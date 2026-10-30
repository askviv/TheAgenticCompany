"""CoffeeBench's upstream native tool-use agent with supplier-private tools."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from functools import wraps
from pathlib import Path
import sys
from typing import Any

from servers.simulation.ecommerce_supplier import kernel_quote, supplier_market_quote


_UPSTREAM_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "coffeebench" / "upstream"
if str(_UPSTREAM_ROOT) not in sys.path:
    sys.path.insert(0, str(_UPSTREAM_ROOT))

from coffeebench.agent import Agent  # noqa: E402
from coffeebench.models.types import ModelResponse, ToolCall, ToolSpec  # noqa: E402


class SupplierAgentError(RuntimeError):
    """Safe supplier actor failure; detailed model errors stay at worker boundary."""


class CodexCoffeeModel:
    """Adapt the shared Codex CLI structured-turn transport to Coffee's Model protocol."""

    def __init__(self, transport: Any, *, model_id: str):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("configured Codex model identifier is required")
        self.transport = transport
        self.model = model_id
        self.cost = 0.0
        self.n_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._input_tokens_complete = True
        self._output_tokens_complete = True

    def query(
        self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None,
        *, on_submission_started=None,
    ) -> ModelResponse:
        descriptors = None if not tools else [
            {"type": "function", "function": {
                "name": tool.name, "description": tool.description,
                "parameters": tool.input_schema,
            }}
            for tool in tools
        ]
        # CoffeeBench keeps its native ToolCall dataclasses in the persisted
        # transcript. Reuse its source-specific provider serialization before
        # crossing the JSON/CLI boundary; repr/default conversion would change
        # the conversation contract and conceal unsupported fields.
        submission_hook = (
            {"on_submission_started": on_submission_started}
            if on_submission_started is not None else {}
        )
        outcome = self.transport.complete(
            _serialize_messages(messages), tools=descriptors, **submission_hook,
        )
        self.n_calls += 1
        usage = outcome.get("usage")
        if not isinstance(usage, dict):
            self._input_tokens_complete = False
            self._output_tokens_complete = False
        else:
            for field, attr, complete_attr in (
                ("input_tokens", "_input_tokens", "_input_tokens_complete"),
                ("output_tokens", "_output_tokens", "_output_tokens_complete"),
            ):
                value = usage.get(field)
                if type(value) is not int or value < 0:
                    setattr(self, complete_attr, False)
                else:
                    setattr(self, attr, getattr(self, attr) + value)
        calls = []
        for raw in outcome.get("tool_calls", []):
            import json

            try:
                arguments = json.loads(raw.get("arguments_json", "{}"))
            except (TypeError, ValueError) as exc:
                raise SupplierAgentError("InvalidToolArguments") from exc
            if not isinstance(arguments, dict):
                raise SupplierAgentError("InvalidToolArguments")
            calls.append(ToolCall(id=str(raw["id"]), name=str(raw["name"]), input=arguments))
        return ModelResponse(
            content=str(outcome.get("content") or ""), tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn", raw=None,
        )

    def get_usage_stats(self) -> dict[str, Any]:
        return {
            "n_model_calls": self.n_calls,
            "model_cost": None,
            "input_tokens": self._input_tokens if self.n_calls and self._input_tokens_complete else None,
            "output_tokens": self._output_tokens if self.n_calls and self._output_tokens_complete else None,
        }


def _decimal(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid {field}") from exc
    if not amount.is_finite():
        raise ValueError(f"invalid {field}")
    return amount


def _serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Coffee's provider-agnostic transcript into JSON-safe records."""
    serialized: list[dict[str, Any]] = []
    for message in messages:
        row = dict(message)
        calls = row.get("tool_calls")
        if calls is not None:
            row["tool_calls"] = [
                {"id": call.id, "name": call.name, "input": call.input}
                if isinstance(call, ToolCall) else call
                for call in calls
            ]
        serialized.append(row)
    return serialized


class CoffeeSupplierAgent:
    """Run one supplier turn through CoffeeBench's actual Agent query/apply loop.

    The worker persists returned `messages` privately. The upstream agent never
    receives buyer ERP reads except fields explicitly supplied in visible_context.
    """

    def __init__(self, model: Any, *, max_steps: int = 12):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.model = model
        self.max_steps = max_steps

    def respond(
        self,
        question: str,
        private_state: dict[str, Any],
        visible_context: dict[str, Any],
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("supplier question must be non-empty")
        if not isinstance(private_state, dict) or not isinstance(visible_context, dict):
            raise TypeError("supplier private_state and visible_context must be objects")

        accessed_facts: dict[str, Any] = {}
        tool_errors: list[BaseException] = []
        result: dict[str, Any] = {"body": None, "quote": None, "required_actions": []}

        def record(facts: dict[str, Any]) -> None:
            accessed_facts.update(facts)

        def get_dispatch_record() -> dict[str, Any]:
            """Read this supplier's dispatch record and cite its evidence IDs."""
            refs = [str(ref) for ref in private_state.get("dispatch_evidence_ids", [])]
            quantity = int(private_state["dispatch_quantity"])
            shipment_id = str(private_state["shipment_id"])
            facts = {
                "dispatch_quantity": {"value": quantity, "unit": private_state["readable_unit"]},
                "shipment_id": shipment_id,
                "dispatch_evidence_ids": refs,
                "supplier_id": private_state.get("supplier_id"),
            }
            reported_quantity = visible_context.get("reported_received_quantity")
            if reported_quantity is not None:
                facts["buyer_reported_received_quantity"] = {
                    "value": reported_quantity, "unit": private_state["readable_unit"],
                    "source": "buyer_visible_receipt_context",
                }
            record(facts)
            return {"dispatch": facts}

        def get_supply_quote(proposed_price: str | None = None) -> dict[str, Any]:
            """Ask the supplier's private availability and negotiation models for a quote."""
            quantity = int(visible_context.get("order_quantity", 0))
            market = supplier_market_quote(private_state, quantity, day_value=visible_context.get("simulated_time"))
            quote, updated = kernel_quote(
                private_state, proposed_price,
                cost_multiplier=market["upstream_cost_multiplier"],
            )
            private_state.update(updated)
            unit = str(private_state["readable_unit"])
            quote.update(market)
            quote["unit"] = unit
            quote["contract_unit_price"] = str(visible_context.get("readable_unit_price", ""))
            contract_price = _decimal(quote["contract_unit_price"], "visible contract unit price")
            quote_price = quote.get("unit_price")
            offered_value = proposed_price if quote["decision"] == "Accept" else quote_price
            resulting = contract_price if offered_value is None else _decimal(offered_value, "supplier unit price")
            quote["authorization_required"] = offered_value is not None and resulting != contract_price
            if quote["decision"] == "Accept":
                quote["unit_price"] = format(resulting, "f")
            result["quote"] = quote
            facts = {
                "supplier_available_quantity": market["available_quantity"],
                "supplier_expected_lead_time_days": market["expected_lead_time_days"],
                "supplier_quote_decision": quote["decision"],
                "supplier_quoted_unit_price": format(resulting, "f"),
                "supplier_contract_unit_price": format(contract_price, "f"),
            }
            record(facts)
            actions = []
            if quote["authorization_required"]:
                actions.append({"type": "purchase_order_change_authorization", "required": True})
            if market["unavailable_quantity"]:
                actions.append({"type": "supplier_replenishment_or_split_delivery", "quantity": market["unavailable_quantity"]})
            result["required_actions"] = actions
            return {"quote": quote, "private_facts": facts, "required_actions": actions}

        def get_expedited_shipping_proposal() -> dict[str, Any]:
            """Read the supplier's shipment-specific expedited service proposal."""
            proposal = private_state["expedite_proposal"]
            if str(proposal["shipment_id"]) != str(private_state["shipment_id"]):
                raise ValueError("supplier shipping proposal does not match its shipment record")
            amount = _decimal(proposal["fee_amount"], "expedited-shipping fee")
            cap = _decimal(proposal["fee_cap_amount"], "expedited-shipping fee cap")
            currency = str(proposal["fee_currency"])
            if (currency != "GBP" or amount < 0 or amount > cap or cap > Decimal("1000")
                    or amount != amount.quantize(Decimal("0.01"))
                    or cap != cap.quantize(Decimal("0.01"))):
                raise ValueError("supplier expedited-shipping fee is invalid")
            quote_id = str(proposal["quote_id"])
            service = str(proposal["service"])
            delivery_date = str(proposal["delivery_commitment"])
            from datetime import date

            if not quote_id or not service or date.fromisoformat(delivery_date).isoformat() != delivery_date:
                raise ValueError("supplier expedited-shipping delivery date is invalid")
            quote = {
                "type": "expedited_shipping",
                "quote_id": quote_id,
                "shipment_id": str(proposal["shipment_id"]),
                "fee_amount": format(amount, "f"),
                "fee_currency": currency,
                "service": service,
                "delivery_commitment": delivery_date,
                "status": "proposed",
                "authorization_required": True,
            }
            result["quote"] = quote
            facts = {key: value for key, value in quote.items() if key not in {"authorization_required"}}
            record(facts)
            actions = [{"type": "manager_authorization", "operation": "approve_expedited_shipping",
                        "native_action": "commit_expedited_shipping",
                        "entity_id": quote["shipment_id"], "quote_id": quote["quote_id"],
                        "fee_amount": quote["fee_amount"], "fee_currency": quote["fee_currency"]}]
            result["required_actions"] = actions
            return {"quote": quote, "private_facts": facts, "required_actions": actions}

        def confirm_expedited_shipping(approval_id: str) -> dict[str, Any]:
            """Confirm a supplier service only when coordinator context carries the exact native grant."""
            proposal_result = get_expedited_shipping_proposal()
            proposal = proposal_result["quote"]
            authorization = visible_context.get("expedite_authorization")
            if not isinstance(authorization, dict):
                raise ValueError("coordinator authorization is missing")
            for field in ("quote_id", "shipment_id", "fee_amount", "fee_currency", "delivery_commitment", "service"):
                if str(authorization.get(field)) != str(proposal[field]):
                    raise ValueError("coordinator authorization does not match supplier terms")
            if str(authorization.get("approval_id")) != approval_id or not approval_id:
                raise ValueError("coordinator authorization reference does not match")
            accepted = dict(proposal, status="accepted", authorization_required=False, approval_id=approval_id)
            result["quote"] = accepted
            result["required_actions"] = []
            record({"expedite_approval_id": approval_id, "expedite_status": "accepted"})
            return {"quote": accepted, "approval_id": approval_id, "private_facts": accessed_facts}

        def stage_reply(body: str, disclosed_facts: list, required_actions: list | None = None) -> dict[str, Any]:
            """Stage a supplier response for coordinator-controlled Mailu delivery."""
            if not body.strip():
                raise ValueError("supplier reply body must be non-empty")
            if not isinstance(disclosed_facts, list) or any(not isinstance(fact, str) for fact in disclosed_facts):
                raise ValueError("disclosed_facts must be a list of fact identifiers")
            if required_actions is not None and (
                    not isinstance(required_actions, list)
                    or any(not isinstance(action, dict) for action in required_actions)):
                raise ValueError("required_actions must be a list of action objects")
            unknown = set(disclosed_facts) - set(accessed_facts)
            if unknown:
                raise ValueError("supplier reply cites facts that were not read in this turn")
            result["body"] = body.strip()
            result["disclosed_facts"] = sorted(set(disclosed_facts))
            if required_actions is not None:
                result["required_actions"] = required_actions
            return {"status": "staged_for_coordinator_delivery"}

        def safe_tool(function):
            @wraps(function)
            def invoke(*args, **kwargs):
                try:
                    return function(*args, **kwargs)
                except Exception as exc:
                    tool_errors.append(exc)
                    # Keep Coffee's recovery observation useful without exposing
                    # raw private inputs or provider messages to the model.
                    raise ValueError(type(exc).__name__) from exc
            return invoke

        tools = [safe_tool(function) for function in (
            get_dispatch_record, get_supply_quote, get_expedited_shipping_proposal,
            confirm_expedited_shipping, stage_reply,
        )]
        prompt = (
            "You are the supplier's independent commercial representative. Answer the inbound message using "
            "the supplier's own records and the buyer-visible purchase-order context. Read a relevant record "
            "with the available tools before making factual claims. If buyer receipt and supplier dispatch "
            "quantities differ, attribute both claims to their respective records and preserve the discrepancy. "
            "Quote tools provide the supplier's economic decision; any changed "
            "purchase-order term requires the buyer's separate authorization. Expedited service is only "
            "confirmed after the coordinator supplies a matching verified manager grant. Never claim an ERP "
            "posting or send email yourself. Finish by calling stage_reply with the body, fact identifiers "
            "actually disclosed, and any required actions."
        )
        observation = (
            f"Inbound subject: {visible_context.get('subject', '')}\n"
            f"Inbound message: {question}\n"
            f"Visible purchase-order context: {visible_context}"
        )
        agent = Agent(model=self.model, tools=tools, instruct_prompt=prompt,
                      initial_observation=observation, name="supplier")
        if messages is None:
            agent.init()
        else:
            agent.messages = deepcopy(messages)
            agent.add_message("user", observation)

        try:
            for _ in range(self.max_steps):
                response: ModelResponse = agent.step_query()
                applied = agent.step_apply(response)
                if result["body"]:
                    break
                if applied["action_name"] == "wait_for_next_day":
                    raise SupplierAgentError("SupplierAgentDidNotStageReply")
        except SupplierAgentError:
            raise
        except Exception as exc:
            raise SupplierAgentError(type(exc).__name__) from exc
        if not result["body"]:
            raise SupplierAgentError("SupplierAgentStepLimit")
        result["messages"] = _serialize_messages(agent.messages)
        result.setdefault("disclosed_facts", [])
        result.setdefault("quote", None)
        result.setdefault("required_actions", [])
        result["_tool_errors"] = tool_errors
        return result


def respond_supplier(
    question: str,
    private_state: dict[str, Any],
    visible_context: dict[str, Any],
    *,
    model: Any,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use the vendored CoffeeBench Agent with an injected upstream Model."""
    return CoffeeSupplierAgent(model).respond(question, private_state, visible_context, messages=messages)
