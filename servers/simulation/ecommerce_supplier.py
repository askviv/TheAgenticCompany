"""Thin private-supplier adapter over the selected E-CommerceBench kernel."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from third_party.ecommercebench.upstream.tools.ecommerce_env import EcommerceEnv, StoreState


UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
DEFAULT_STORE_TYPE = "auto_hardware"
DEFAULT_OPENING_STOCK = 12_000


def _market_date(value: Any, fallback: str = "2026-01-01") -> date:
    if value is None:
        return date.fromisoformat(fallback)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _upstream_environment(seed: int, day: date) -> EcommerceEnv:
    return EcommerceEnv(seed=seed, start_date=day.isoformat(), max_day=1, per_store_scale=0.1)


def _market_product(environment: EcommerceEnv, store_type: str) -> str:
    products = sorted(
        product_id
        for product_id, product in environment.products.items()
        if product.get("store_type") == store_type
    )
    if not products:
        raise ValueError(f"upstream E-CommerceBench has no products for store type {store_type!r}")
    return products[0]


def initialize_market(private_state: dict[str, Any], *, seed: int) -> dict[str, Any]:
    """Attach reproducible adapter configuration to one supplier's private state."""
    config = private_state.setdefault("market_config", {})
    store_type = str(config.setdefault("store_type", DEFAULT_STORE_TYPE))
    day = _market_date(config.get("market_date"))
    environment = _upstream_environment(seed, day)
    product_id = str(config.setdefault("product_id", _market_product(environment, store_type)))
    product = environment.products.get(product_id)
    if product is None or product.get("store_type") != store_type:
        raise ValueError("supplier market product does not belong to the configured upstream store type")
    market = private_state.setdefault("market_state", {})
    market.setdefault("available_quantity", int(config.get("opening_stock", DEFAULT_OPENING_STOCK)))
    market.setdefault("market_date", day.isoformat())
    market.setdefault("applied_event_ids", [])
    market.setdefault("delivery_records", [])
    private_state["business_model"] = {
        "model_id": "ecommercebench-private-market",
        "source_commit": UPSTREAM_COMMIT,
        "actor_mode": "supplier-private-market",
        "store_type": store_type,
        "product_id": product_id,
    }
    return private_state


def market_conditions(private_state: dict[str, Any], *, day_value: Any = None) -> dict[str, Any]:
    """Read upstream demand/disruption effects for this supplier's market date."""
    business = private_state["business_model"]
    day = _market_date(day_value, private_state["market_state"].get("market_date", "2026-01-01"))
    environment = _upstream_environment(int(private_state["seed"]), day)
    environment._process_events_for_date(day)
    store_type = business["store_type"]
    active = environment.active_events
    cost_multipliers = [
        Decimal(1) + Decimal(str((event.get("_supply_effects") or {}).get("cost_increase", 0)))
        for event in active
        if store_type in ((event.get("_supply_effects") or {}).get("affected_categories", []))
        or (event.get("_supply_effects") or {}).get("affected_categories") == "all"
    ]
    cost_multiplier = Decimal(1)
    for multiplier in cost_multipliers:
        cost_multiplier *= multiplier
    return {
        "market_date": day.isoformat(),
        "demand_factor": environment._get_event_demand_factor(store_type),
        "delivery_delay_multiplier": environment._get_event_delivery_delay(store_type),
        "cost_multiplier": format(cost_multiplier, "f"),
        "active_event_names": [event.get("event_name", "") for event in active],
    }


def consume_market_demand(private_state: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use E-CommerceBench's actual daily sales path to consume supplier-private stock."""
    event_id = str(event.get("event_id", ""))
    if not event_id:
        raise ValueError("canonical supplier demand event requires event_id")
    state = initialize_market(deepcopy(private_state), seed=int(private_state["seed"]))
    market = state["market_state"]
    if event_id in market["applied_event_ids"]:
        result = next(item["outcome"] for item in market.get("event_outcomes", []) if item["event_id"] == event_id)
        return state, result

    day = _market_date(event.get("simulated_time"), market.get("market_date", "2026-01-01"))
    business = state["business_model"]
    environment = _upstream_environment(int(state["seed"]), day)
    product_id = business["product_id"]
    product = environment.products[product_id]
    store = StoreState("supplier-private-market", business["store_type"], "Supplier external demand", day, 0.0)
    before = int(market["available_quantity"])
    store.inventory[product_id] = before
    store.prices[product_id] = float(product["reference_price"])
    environment.stores[store.store_id] = store
    environment._process_events_for_date(day)
    conditions = market_conditions(state, day_value=day)
    environment._process_sales_for_date(day)
    after = int(store.inventory[product_id])
    outcome = {
        "event_id": event_id,
        "event_type": "SUPPLIER_MARKET_DEMAND",
        "market_date": day.isoformat(),
        "demand_served": before - after,
        "available_quantity": after,
        "upstream_demand_factor": conditions["demand_factor"],
        "upstream_delay_multiplier": conditions["delivery_delay_multiplier"],
        "upstream_events": conditions["active_event_names"],
    }
    market["available_quantity"] = after
    market["market_date"] = day.isoformat()
    market["last_market_conditions"] = conditions
    market.setdefault("event_outcomes", []).append({"event_id": event_id, "outcome": outcome})
    market["applied_event_ids"].append(event_id)
    return state, outcome


def apply_market_event(private_state: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    event_id = str(event.get("event_id", ""))
    event_type = str(event.get("event_type", ""))
    state = initialize_market(deepcopy(private_state), seed=int(private_state["seed"]))
    market = state["market_state"]
    prior = next((item for item in market.get("event_outcomes", []) if item["event_id"] == event_id), None)
    if prior:
        return state, prior["outcome"]
    if event_type == "SUPPLIER_MARKET_DEMAND":
        return consume_market_demand(state, event)
    if event_type == "SUPPLIER_STOCK_REPLENISHED":
        quantity = event.get("payload", {}).get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("supplier replenishment requires a positive whole quantity")
        market["available_quantity"] += quantity
        outcome = {"event_id": event_id, "event_type": event_type, "replenished_quantity": quantity,
                   "available_quantity": market["available_quantity"]}
    elif event_type == "SUPPLIER_SUPPLY_DISRUPTED":
        day = _market_date(event.get("simulated_time"), market.get("market_date", "2026-01-01"))
        conditions = market_conditions(state, day_value=day)
        market["market_date"] = day.isoformat()
        market["last_market_conditions"] = conditions
        outcome = {"event_id": event_id, "event_type": event_type, **conditions}
    elif event_type == "SUPPLIER_DELIVERY_OUTCOME":
        payload = event.get("payload", {})
        if str(payload.get("shipment_id", "")) != str(state.get("shipment_id", "")):
            raise ValueError("supplier delivery outcome must match its private shipment record")
        record = {"event_id": event_id, "shipment_id": payload["shipment_id"],
                  "on_time": payload.get("on_time"), "delivered_quantity": payload.get("delivered_quantity")}
        market.setdefault("delivery_records", []).append(record)
        outcome = {"event_id": event_id, "event_type": event_type, **record}
    else:
        raise ValueError("unsupported supplier market event")
    market.setdefault("event_outcomes", []).append({"event_id": event_id, "outcome": outcome})
    market.setdefault("applied_event_ids", []).append(event_id)
    return state, outcome


def supplier_market_quote(private_state: dict[str, Any], order_quantity: int, *, day_value: Any = None) -> dict[str, Any]:
    market = private_state["market_state"]
    conditions = market_conditions(private_state, day_value=day_value or market.get("market_date"))
    available = int(market["available_quantity"])
    requested = max(0, int(order_quantity))
    base_lead_time = int(private_state.get("market_config", {}).get("base_lead_time_days", 4))
    delay_multiplier = Decimal(str(conditions["delivery_delay_multiplier"]))
    lead_time = int((Decimal(base_lead_time) * delay_multiplier).to_integral_value(rounding="ROUND_CEILING"))
    return {
        "requested_quantity": requested,
        "available_quantity": available,
        "quoted_quantity": min(requested, available),
        "unavailable_quantity": max(0, requested - available),
        "expected_lead_time_days": lead_time,
        "upstream_cost_multiplier": conditions["cost_multiplier"],
        "upstream_event_names": conditions["active_event_names"],
    }


def kernel_quote(
    private_state: dict[str, Any], proposed_price: str | None, *, cost_multiplier: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the vendored E-Commerce counterpart kernel from a JSON turn transcript."""
    from third_party.ecommercebench.counterpart import CounterpartKernel

    pricing = private_state["pricing_config"]
    history = private_state.setdefault("kernel_state", {}).setdefault("turns", [])
    floor = Decimal(str(pricing["reservation_price"]))
    if cost_multiplier is not None:
        floor *= Decimal(cost_multiplier)
    kernel = CounterpartKernel(
        family=pricing["family"], role="seller", r_b=float(floor),
        kappa_b=float(pricing["kappa_b"]), eta_b=pricing["stance"],
        d_0=float(pricing["opening_harshness"]), seed=int(private_state["seed"]),
        K=int(pricing["maximum_rounds"]), p_min=float(pricing.get("minimum_offer", 0)),
        p_max=float(pricing["maximum_offer"]),
    )
    for index, turn in enumerate(history, start=1):
        prior = turn.get("buyer_offer")
        kernel.get_action(index, None if prior is None else float(prior))
    action = kernel.get_action(len(history) + 1, None if proposed_price is None else float(proposed_price))
    history.append({"buyer_offer": proposed_price})
    return {"decision": action.decision, "unit_price": None if action.price is None else format(Decimal(str(action.price)).quantize(Decimal("0.01")), "f"),
            "strategic_cue": action.strategic_cue, "sentiment_cue": action.sentiment_cue}, private_state
