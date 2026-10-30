from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
import pytest

from servers.simulation.businesses import (
    apply_future_pricing_disruption,
    apply_supplier_market_event,
    initialize_supplier_business,
    respond_supplier,
)
from servers.simulation.coffeebench_supplier import CodexCoffeeModel
from servers.simulation.coffeebench_supplier import SupplierAgentError
from coffeebench.models.types import ModelResponse, ToolCall, ToolSpec


def supplier_state(*, supplier_id="supplier-1", dispatch_quantity=10_000, seed=17):
    return initialize_supplier_business({
        "supplier_id": supplier_id,
        "dispatch_quantity": dispatch_quantity,
        "readable_unit": "components",
        "shipment_id": f"shipment-{supplier_id}",
        "dispatch_evidence_ids": [f"manifest-{supplier_id}", f"bill-of-lading-{supplier_id}"],
        "seed": seed,
        "pricing_config": {
            "family": "Candid", "stance": "neutral", "kappa_b": 0.65,
            "opening_harshness": 0.45, "reservation_price": "45.00",
            "maximum_offer": "100.00", "minimum_offer": "0.00",
            "maximum_rounds": 10, "minor_unit": 2,
        },
        "kernel_state": {"turns": []},
    }, seed=seed, supplier_id=supplier_id)


def case_context(**extra):
    return {"case_id": "case-C1-D1", "purchase_order_id": "PO-1042",
            "order_quantity": 10_000, "readable_unit_price": "50.00", **extra}


class ScriptedCoffeeModel:
    """Test-only Coffee Model protocol implementation; it exercises the upstream Agent loop."""

    model = "gpt-scripted-test"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def query(self, messages, tools=None):
        self.calls += 1
        if not self.responses:
            raise AssertionError("Coffee Agent made an unexpected model call")
        return self.responses.pop(0)

    def get_usage_stats(self):
        return {"n_model_calls": self.calls, "model_cost": None}


def _call(name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=f"call-{name}", name=name, input=arguments)])


def _stage(body, facts, actions=None):
    args = {"body": body, "disclosed_facts": facts}
    if actions is not None:
        args["required_actions"] = actions
    return _call("stage_reply", **args)


def test_c1_agent_reads_supplier_dispatch_record_before_staging_discrepancy_reply():
    state = supplier_state()
    model = ScriptedCoffeeModel(
        _call("get_dispatch_record"),
        _stage("Our dispatch record shows 10000 components on shipment shipment-supplier-1 and cites manifest-supplier-1. Your receipt record reports 8000 components; our dispatch record remains 10000.",
               ["dispatch_quantity", "shipment_id", "dispatch_evidence_ids", "buyer_reported_received_quantity"]),
    )
    result = respond_supplier(
        "Please provide evidence of what you dispatched.", state,
        case_context(reported_received_quantity=8_000), model=model,
    )

    assert result["body"].startswith("Our dispatch record shows 10000")
    assert "Your receipt record reports 8000" in result["body"]
    assert "our dispatch record remains 10000" in result["body"]
    assert result["disclosed_facts"] == ["buyer_reported_received_quantity", "dispatch_evidence_ids", "dispatch_quantity", "shipment_id"]
    assert state["dispatch_quantity"] == 10_000
    assert "dispatch_quantity" not in result["body"]
    assert model.calls == 2
    json.dumps(result["messages"], allow_nan=False)


def test_supplier_agent_cannot_claim_unread_private_evidence_as_disclosed():
    state = supplier_state()
    model = ScriptedCoffeeModel(
        _stage("The supplier dispatched 10000 components.", ["dispatch_quantity"]),
        ModelResponse(content="I cannot verify that dispatch detail from the records I checked."),
    )

    with pytest.raises(SupplierAgentError, match="SupplierAgentDidNotStageReply"):
        respond_supplier("What did you dispatch?", state, case_context(), model=model)


def test_d1_quote_uses_real_counterpart_kernel_and_preserves_visible_contract_price():
    state = supplier_state()
    before = deepcopy(state["pricing_config"])
    model = ScriptedCoffeeModel(
        _call("get_supply_quote"),
        _stage("The revised supplier quote is in our quotation. Any PO change needs authorization.",
               ["supplier_available_quantity", "supplier_expected_lead_time_days",
                "supplier_quote_decision", "supplier_quoted_unit_price", "supplier_contract_unit_price"]),
    )
    result = respond_supplier("Please quote the order.", state, case_context(simulated_time="2026-11-20T00:00:00+00:00"), model=model)

    assert result["quote"]["decision"] in {"Offer", "Accept", "Reject"}
    assert result["quote"]["contract_unit_price"] == "50.00"
    assert result["quote"]["authorization_required"] is True
    assert result["quote"]["upstream_event_names"] == ["Raw Material Shortage"]
    assert result["quote"]["expected_lead_time_days"] == 12
    assert state["pricing_config"] == before
    assert result["disclosed_facts"] == [
        "supplier_available_quantity", "supplier_contract_unit_price",
        "supplier_expected_lead_time_days", "supplier_quote_decision", "supplier_quoted_unit_price",
    ]


def test_supplier_kernel_replay_is_deterministic_and_scoped_to_each_actor():
    first = supplier_state(supplier_id="supplier-A")
    second = supplier_state(supplier_id="supplier-B", dispatch_quantity=8_000, seed=17)
    messages = [
        _call("get_supply_quote"),
        _stage("The quote is attached.", ["supplier_available_quantity", "supplier_expected_lead_time_days",
                                           "supplier_quote_decision", "supplier_quoted_unit_price",
                                           "supplier_contract_unit_price"]),
    ]
    one = respond_supplier("Quote this order.", first, case_context(), model=ScriptedCoffeeModel(*messages))
    two = respond_supplier("Quote this order.", second, case_context(), model=ScriptedCoffeeModel(*messages))

    assert one["quote"] == two["quote"]
    assert first["kernel_state"]["turns"] == second["kernel_state"]["turns"]
    assert first["supplier_id"] != second["supplier_id"]
    assert one["messages"] != two["messages"] or one["quote"] == two["quote"]


def test_actual_ecommerce_environment_consumes_private_stock_and_applies_named_market_disruption():
    state = supplier_state()
    initial_stock = state["market_state"]["available_quantity"]
    demand_event = {
        "event_id": "market-demand-2026-11-20", "event_type": "SUPPLIER_MARKET_DEMAND",
        "simulated_time": "2026-11-20T00:00:00+00:00", "payload": {},
    }
    after_demand, demand = apply_supplier_market_event(state, demand_event)
    disrupted, disruption = apply_supplier_market_event(after_demand, {
        "event_id": "raw-material-shortage-2026-11-20", "event_type": "SUPPLIER_SUPPLY_DISRUPTED",
        "simulated_time": "2026-11-20T00:00:00+00:00", "payload": {},
    })

    assert demand["demand_served"] > 0
    assert demand["available_quantity"] == initial_stock - demand["demand_served"]
    assert disruption["active_event_names"] == ["Raw Material Shortage"]
    assert Decimal(disruption["cost_multiplier"]) == Decimal("1.15")
    assert disruption["delivery_delay_multiplier"] == 3.0
    assert state["market_state"]["available_quantity"] == initial_stock
    assert disrupted["market_state"]["available_quantity"] == demand["available_quantity"]


def test_upstream_market_demand_replay_is_idempotent_and_replenishment_is_private():
    state = supplier_state()
    event = {"event_id": "market-demand-once", "event_type": "SUPPLIER_MARKET_DEMAND",
             "simulated_time": "2026-01-01T00:00:00+00:00", "payload": {}}
    first, outcome = apply_supplier_market_event(state, event)
    repeated, replay = apply_supplier_market_event(first, event)
    replenished, receipt = apply_supplier_market_event(first, {
        "event_id": "market-restock", "event_type": "SUPPLIER_STOCK_REPLENISHED",
        "payload": {"quantity": 1000},
    })

    assert replay == outcome
    assert repeated["market_state"] == first["market_state"]
    assert receipt["available_quantity"] == first["market_state"]["available_quantity"] + 1000
    assert state["market_state"]["available_quantity"] == 12_000
    assert replenished["market_state"]["available_quantity"] == receipt["available_quantity"]


def test_future_supplier_price_disruption_does_not_rewrite_order_contract():
    state = supplier_state()
    updated = apply_future_pricing_disruption(state, disruption_id="future-cost-1", cost_increase="5.00")
    assert updated["pricing_config"]["reservation_price"] == "50.00"
    assert state["pricing_config"]["reservation_price"] == "45.00"
    assert case_context()["readable_unit_price"] == "50.00"
    assert apply_future_pricing_disruption(updated, disruption_id="future-cost-1", cost_increase="5.00")["pricing_config"]["reservation_price"] == "50.00"


def test_canonical_pricing_event_is_idempotent_and_changes_future_supplier_floor_only():
    state = supplier_state()
    updated, result = apply_supplier_market_event(state, {
        "event_id": "D1-future-cost", "event_type": "SUPPLIER_PRICING_CHANGED",
        "payload": {"cost_increase": "5.00"},
    })
    replay, repeated = apply_supplier_market_event(updated, {
        "event_id": "D1-future-cost", "event_type": "SUPPLIER_PRICING_CHANGED",
        "payload": {"cost_increase": "5.00"},
    })

    assert updated["pricing_config"]["reservation_price"] == "50.00"
    assert state["pricing_config"]["reservation_price"] == "45.00"
    assert result["contract_terms_changed"] is False
    assert repeated == result
    assert replay["pricing_config"]["reservation_price"] == "50.00"


def test_expedited_shipping_proposal_requires_matching_verified_grant_for_acceptance():
    state = supplier_state()
    state["expedite_proposal"] = {
        "quote_id": "EXP-S01-001", "shipment_id": state["shipment_id"],
        "fee_amount": "750.00", "fee_cap_amount": "1000.00", "fee_currency": "GBP",
        "service": "priority freight", "delivery_commitment": "2026-11-12",
    }
    proposal_model = ScriptedCoffeeModel(
        _call("get_expedited_shipping_proposal"),
        _stage("We propose priority freight by 2026-11-12 for GBP 750.00; authorization is required.",
               ["type", "quote_id", "shipment_id", "fee_amount", "fee_currency", "service",
                "delivery_commitment", "status"]),
    )
    proposal = respond_supplier("Can you expedite this shipment?", state, case_context(), model=proposal_model)
    assert proposal["quote"]["status"] == "proposed"
    assert proposal["quote"]["authorization_required"] is True
    assert proposal["required_actions"][0]["operation"] == "approve_expedited_shipping"
    assert proposal["required_actions"][0]["native_action"] == "commit_expedited_shipping"

    grant = {
        "approval_id": "grant-1", "quote_id": "EXP-S01-001",
        "shipment_id": state["shipment_id"], "fee_amount": "750.00", "fee_currency": "GBP",
        "delivery_commitment": "2026-11-12", "service": "priority freight",
    }
    accepted = respond_supplier(
        "Confirm the approved expedited service.", state,
        case_context(expedite_authorization=grant),
        model=ScriptedCoffeeModel(
            _call("confirm_expedited_shipping", approval_id="grant-1"),
            _stage("We confirm the authorized service and delivery commitment.", ["expedite_status"]),
        ),
    )
    assert accepted["quote"]["status"] == "accepted"
    assert accepted["quote"]["approval_id"] == "grant-1"
    assert accepted["required_actions"] == []

    grant["fee_amount"] = "900.00"
    mismatched = respond_supplier(
        "Confirm the approved expedited service.", state,
        case_context(expedite_authorization=grant),
        model=ScriptedCoffeeModel(
            _call("confirm_expedited_shipping", approval_id="grant-1"),
            _stage("I cannot confirm the service against the authorized terms.", ["status"]),
        ),
    )
    assert mismatched["quote"]["status"] == "proposed"
    assert mismatched["quote"]["authorization_required"] is True


def test_codex_transport_adapter_builds_native_coffee_tool_response_without_live_call():
    class Transport:
        def complete(self, messages, *, tools):
            assert messages
            assert tools[0]["function"]["name"] == "get_dispatch_record"
            return {"content": None, "tool_calls": [{"id": "call-1", "name": "get_dispatch_record", "arguments_json": "{}"}], "usage": None}

    model = CodexCoffeeModel(Transport(), model_id="codex-test")
    response = model.query([{"role": "user", "content": "question"}],
                           tools=[ToolSpec("get_dispatch_record", "Read dispatch", {"type": "object", "properties": {}})])
    # No provider is invoked in this unit; the transport surface is injected.
    assert response.content == ""
    assert response.tool_calls[0].name == "get_dispatch_record"
    assert response.tool_calls[0].input == {}
    assert model.model == "codex-test"


def test_codex_coffee_model_aggregates_complete_usage_and_marks_partial_totals_unknown():
    class Transport:
        def __init__(self, usage_records):
            self.usage_records = iter(usage_records)

        def complete(self, messages, *, tools):
            return {"content": "continue", "tool_calls": [], "usage": next(self.usage_records)}

    complete = CodexCoffeeModel(Transport([
        {"input_tokens": 5, "output_tokens": 2},
        {"input_tokens": 7, "output_tokens": 3},
    ]), model_id="codex-test")
    complete.query([{"role": "user", "content": "first"}])
    complete.query([{"role": "user", "content": "second"}])
    assert complete.get_usage_stats() == {
        "n_model_calls": 2, "model_cost": None, "input_tokens": 12, "output_tokens": 5,
    }

    partial = CodexCoffeeModel(Transport([
        {"input_tokens": 5, "output_tokens": 2},
        {"input_tokens": 7},
    ]), model_id="codex-test")
    partial.query([{"role": "user", "content": "first"}])
    partial.query([{"role": "user", "content": "second"}])
    assert partial.get_usage_stats()["input_tokens"] == 12
    assert partial.get_usage_stats()["output_tokens"] is None
