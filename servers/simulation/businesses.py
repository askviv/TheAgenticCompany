"""Supplier commercial tools over selective upstream benchmark components."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from servers.simulation.ecommerce_supplier import apply_market_event, initialize_market


def initialize_supplier_business(
    private_state: dict[str, Any], *, seed: int, supplier_id: str,
) -> dict[str, Any]:
    """Attach deterministic upstream market and negotiation config to one actor."""
    state = deepcopy(private_state)
    state["seed"] = int(seed)
    state["supplier_id"] = str(supplier_id)
    state.setdefault("kernel_state", {"turns": []})
    state["kernel_state"].setdefault("turns", [])
    state.setdefault("pricing_config", {
        "family": "Candid", "reservation_price": "50.00", "maximum_offer": "100.00",
        "kappa_b": 1.0, "stance": "neutral", "opening_harshness": 0.5,
        "maximum_rounds": 5, "minimum_offer": "0.00", "minor_unit": 2,
    })
    state.setdefault("market_config", {"store_type": "auto_hardware", "opening_stock": 12_000,
                                        "base_lead_time_days": 4})
    state = initialize_market(state, seed=seed)
    return state


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a decimal amount") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def apply_future_pricing_disruption(
    private_state: dict[str, Any], *, disruption_id: str,
    cost_increase: Decimal | str | int,
) -> dict[str, Any]:
    """Raise only the supplier's future quote floor; accepted ERP terms are external."""
    if not disruption_id:
        raise ValueError("disruption_id is required")
    increase = _decimal(cost_increase, "cost_increase")
    updated = deepcopy(private_state)
    pricing = updated["pricing_config"]
    applied = updated.setdefault("applied_disruptions", [])
    if disruption_id in applied:
        return updated
    floor = _decimal(pricing["reservation_price"], "pricing_config.reservation_price")
    pricing["reservation_price"] = format(floor + increase, "f")
    applied.append(disruption_id)
    return updated


def apply_supplier_market_event(
    private_state: dict[str, Any], event: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a supplier-owned market event through E-CommerceBench's actual environment."""
    event_type = str(event.get("event_type", ""))
    if event_type == "SUPPLIER_PRICING_CHANGED":
        event_id = str(event.get("event_id", ""))
        if not event_id:
            raise ValueError("supplier pricing event requires event_id")
        state = deepcopy(private_state)
        prior = next((row for row in state.get("pricing_event_outcomes", [])
                      if row.get("event_id") == event_id), None)
        if prior:
            return state, deepcopy(prior["outcome"])
        increase = _decimal((event.get("payload") or {}).get("cost_increase"), "cost_increase")
        state = apply_future_pricing_disruption(state, disruption_id=event_id, cost_increase=increase)
        outcome = {"event_id": event_id, "event_type": event_type,
                   "future_quote_cost_increase": format(increase, "f"),
                   "contract_terms_changed": False}
        state.setdefault("pricing_event_outcomes", []).append({"event_id": event_id, "outcome": outcome})
        return state, outcome
    return apply_market_event(private_state, event)


def respond_supplier(
    question: str,
    private_state: dict[str, Any],
    visible_context: dict[str, Any],
    *,
    model: Any,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Lazily load the CoffeeBench adapter only in the isolated supplier worker."""
    from servers.simulation.coffeebench_supplier import respond_supplier as coffee_response

    return coffee_response(question, private_state, visible_context, model=model, messages=messages)


__all__ = [
    "apply_future_pricing_disruption",
    "apply_supplier_market_event", "initialize_supplier_business", "respond_supplier",
]
