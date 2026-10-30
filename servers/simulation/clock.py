"""Deterministic, in-memory SimPy clock primitives for controller-driven runs.

The coordinator owns persistence, delivery receipts, event idempotency, and the
privileged advance boundary. This module takes a pending-event snapshot and does
not expose administrative event inspection or clock controls to agents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import simpy
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter


class _ClockWindow(BaseModel):
    current: AwareDatetime
    target: AwareDatetime


class _ScheduledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1)
    simulated_time: AwareDatetime
    sequence: int = Field(strict=True)


_PENDING_EVENTS = TypeAdapter(list[_ScheduledEvent])


def ordered_due_events(
    current: datetime,
    target: datetime,
    pending: list[dict[str, Any]],
) -> list[str]:
    """Return due event IDs by original timestamp, sequence, then event ID.

    Both interval endpoints are inclusive. Late events retain their original ID
    and timestamp ordering but are scheduled at ``current`` in SimPy. A fresh
    environment is reconstructed for every call, making an identical input
    snapshot replay deterministically. Durable state and processed-event receipts
    remain coordinator responsibilities.
    """
    window = _ClockWindow(current=current, target=target)
    now = window.current.astimezone(timezone.utc)
    end = window.target.astimezone(timezone.utc)
    if end < now:
        raise ValueError("target must not precede current simulated time")

    events = _PENDING_EVENTS.validate_python(pending)
    ordered: list[tuple[datetime, int, str]] = []
    seen_ids: set[str] = set()
    for event in events:
        if event.event_id in seen_ids:
            raise ValueError(f"duplicate pending event_id: {event.event_id}")
        seen_ids.add(event.event_id)
        due = event.simulated_time.astimezone(timezone.utc)
        if due <= end:
            ordered.append((due, event.sequence, event.event_id))

    ordered.sort(key=lambda event: (event[0], event[1], event[2]))
    environment = simpy.Environment()
    dispatched: list[str] = []

    def deliver(due: datetime, event_id: str):
        effective_due = max(due, now)
        yield environment.timeout((effective_due - now).total_seconds())
        dispatched.append(event_id)

    for due, _sequence, event_id in ordered:
        environment.process(deliver(due, event_id))
    environment.run()
    return dispatched
