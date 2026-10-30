"""Calendar-driven E-CommerceBench market signals for a shared company run.

This provider only supplies external market stimuli. E-CommerceBench's own
retail cash, stock, sales, and accounting books are not company records; the
coordinator remains responsible for canonical event persistence and ERPNext
documents.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from typing import Any

from third_party.ecommercebench.upstream.tools.ecommerce_env import EcommerceEnv

from servers.simulation.businesses import apply_supplier_market_event
from servers.simulation.ecommerce_supplier import initialize_market


UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
PROVIDER_ID = "qwenlm-ecommercebench-calendar"
PROVIDER_VERSION = "1"


def _as_datetime(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider times must be timezone-aware")
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("provider times must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _environment(seed: int, day: date) -> EcommerceEnv:
    return EcommerceEnv(seed=seed, start_date=day.isoformat(), max_day=1, per_store_scale=0.1)


def calendar_supply_conditions(
    seed: int, store_type: str, start: datetime | date | str, end: datetime | date | str,
) -> list[dict[str, Any]]:
    """Read native supply calendar conditions for a half-open company-time range.

    This is shared by the company event provider and the persistent native retail
    projection so both observe the exact pinned calendar with the same seed.
    """
    begin, finish = _as_datetime(start), _as_datetime(end)
    if finish < begin:
        raise ValueError("provider event range cannot run backwards")
    conditions = []
    environment = _environment(int(seed), begin.date())
    day = begin.date()
    while day <= finish.date():
        at = datetime.combine(day, time.min, tzinfo=timezone.utc)
        if begin < at <= finish:
            environment._process_events_for_date(day)
            for active in environment.active_events:
                month, day_of_month = (int(part) for part in active["start_date"].split("-"))
                if (day.month, day.day) != (month, day_of_month) or not active.get("_supply_effects"):
                    continue
                supply = active["_supply_effects"]
                conditions.append({
                    "occurred_at": at.isoformat(),
                    "event_name": active["event_name"],
                    "upstream_calendar_start_date": active["start_date"],
                    "upstream_calendar_duration_days": int(active["duration_days"]),
                    "affected_categories": deepcopy(supply.get("affected_categories", [])),
                    "delivery_delay_multiplier": supply.get("delivery_delay_multiplier", 1.0),
                    "cost_increase": supply.get("cost_increase"),
                    "upstream_source": "third_party/ecommercebench/upstream/data/events.csv",
                    "upstream_commit": UPSTREAM_COMMIT,
                    "store_type": store_type,
                })
        day += timedelta(days=1)
    return conditions


def initialize(
    seed: int,
    started_at: datetime | date | str,
    binding: dict[str, Any],
    actor_private_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Create a deterministic provider snapshot from the run's supplier actor.

    ``binding`` must identify the supplier actor, run, scenario, and case. The
    actor's private state remains the durable owner of upstream market stock.
    """
    supplier_actor_id = str(binding["supplier_actor_id"])
    private_state = deepcopy(actor_private_states[supplier_actor_id])
    supplier_id = str(private_state["supplier_id"])
    if binding.get("supplier_id") != supplier_id:
        raise ValueError("provider binding supplier identity does not match its actor")
    private_state["seed"] = int(seed)
    private_state = initialize_market(private_state, seed=int(seed))
    start = _as_datetime(started_at)
    return {
        "provider_id": PROVIDER_ID,
        "provider_version": PROVIDER_VERSION,
        "upstream_commit": UPSTREAM_COMMIT,
        "seed": int(seed),
        "started_at": start.isoformat(),
        "cursor": start.isoformat(),
        "supplier_actor_id": supplier_actor_id,
        "supplier_id": supplier_id,
        "supplier_private_state": private_state,
    }


def _event(
    snapshot: dict[str, Any], binding: dict[str, Any], at: datetime,
    event_type: str, source_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    stable_key = f"{snapshot['provider_id']}:{snapshot['seed']}:{snapshot['supplier_id']}:{source_key}"
    event_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:32]
    return {
        "event_id": event_id,
        "schema_version": 1,
        "company_id": str(binding["company_id"]),
        "upstream_commit": UPSTREAM_COMMIT,
        "simulated_time": at.isoformat(),
        "event_type": event_type,
        "source": PROVIDER_ID,
        "entity_type": "supplier",
        "entity_id": snapshot["supplier_id"],
        "payload": payload,
        "visibility": [snapshot["supplier_actor_id"]],
    }


def events_between(
    snapshot: dict[str, Any],
    start: datetime | date | str,
    end: datetime | date | str,
    binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project actual upstream calendar events over ``(start, end]``.

    A daily demand event advances the supplier's private external market through
    E-CommerceBench's native sales path. Calendar disruptions are emitted on
    their native start dates and the native environment remains authoritative
    for their demand and delivery effects.
    """
    begin, finish = _as_datetime(start), _as_datetime(end)
    if finish < begin:
        raise ValueError("provider event range cannot run backwards")

    supplier_state = snapshot["supplier_private_state"]
    store_type = supplier_state["business_model"]["store_type"]
    events: list[dict[str, Any]] = []
    supply_conditions: dict[str, list[dict[str, Any]]] = {}
    for condition in calendar_supply_conditions(int(snapshot["seed"]), store_type, begin, finish):
        supply_conditions.setdefault(condition["occurred_at"], []).append(condition)
    day = begin.date()
    last_day = finish.date()
    while day <= last_day:
        at = datetime.combine(day, time.min, tzinfo=timezone.utc)
        if begin < at <= finish:
            for condition in supply_conditions.get(at.isoformat(), []):
                payload = {key: value for key, value in condition.items() if key != "occurred_at"}
                payload["upstream_event_name"] = condition["event_name"]
                events.append(_event(
                    snapshot, binding, at, "SUPPLIER_SUPPLY_DISRUPTED",
                    f"supply:{day.isoformat()}:{payload['upstream_event_name']}", payload,
                ))
            events.append(_event(
                snapshot, binding, at, "SUPPLIER_MARKET_DEMAND",
                f"demand:{day.isoformat()}",
                {"market_date": day.isoformat(), "store_type": store_type,
                 "upstream_source": "third_party/ecommercebench/upstream/tools/ecommerce_env.py",
                 "upstream_commit": UPSTREAM_COMMIT},
            ))
        day += timedelta(days=1)
    return sorted(events, key=lambda row: (
        row["simulated_time"],
        0 if row["event_type"] == "SUPPLIER_SUPPLY_DISRUPTED" else 1,
        row["event_id"],
    ))


def apply_event(
    snapshot: dict[str, Any],
    event: dict[str, Any],
    erp_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply one market stimulus; return snapshot, native outcomes, ERP intents.

    E-CommerceBench does not define an accounts-payable calendar. The provider
    therefore emits no synthetic company payment documents; real due obligations
    must be read from the bound ERP records by the coordinator.
    """
    del erp_snapshot  # ERP state is never read or changed by this market provider.
    if str(event.get("entity_id")) != str(snapshot["supplier_id"]):
        raise ValueError("market event must target this provider's supplier")
    if event.get("event_type") not in {"SUPPLIER_MARKET_DEMAND", "SUPPLIER_SUPPLY_DISRUPTED"}:
        raise ValueError(f"unsupported E-CommerceBench background event: {event.get('event_type')!r}")
    updated = deepcopy(snapshot)
    supplier_state, outcome = apply_supplier_market_event(
        updated["supplier_private_state"], event,
    )
    updated["supplier_private_state"] = supplier_state
    at = _as_datetime(event["simulated_time"])
    updated["cursor"] = max(_as_datetime(updated["cursor"]), at).isoformat()
    outcome["upstream_commit"] = UPSTREAM_COMMIT
    # The common coordinator owns ERPNext document creation and posts only
    # explicitly mapped intents. This provider currently has no AP intent.
    return updated, [outcome], []


__all__ = ["UPSTREAM_COMMIT", "PROVIDER_ID", "PROVIDER_VERSION", "initialize", "events_between",
           "calendar_supply_conditions", "apply_event"]
