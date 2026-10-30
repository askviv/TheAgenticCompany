from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from servers.simulation.clock import ordered_due_events


UTC = timezone.utc


def event(event_id: str, when: datetime, sequence: int) -> dict:
    return {
        "event_id": event_id,
        "simulated_time": when.isoformat(),
        "sequence": sequence,
    }


def test_equal_time_events_use_sequence_then_event_id_and_include_target_boundary():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    boundary = now + timedelta(days=2)
    pending = [
        event("z-last", boundary, 4),
        event("b-same-sequence", boundary, 2),
        event("a-same-sequence", boundary, 2),
        event("future", boundary + timedelta(microseconds=1), 0),
    ]

    assert ordered_due_events(now, boundary, pending) == [
        "a-same-sequence",
        "b-same-sequence",
        "z-last",
    ]


def test_offset_timestamps_normalize_to_same_utc_instant_for_ordering():
    current = datetime.fromisoformat("2026-01-01T01:00:00+01:00")
    target = datetime.fromisoformat("2026-01-01T02:00:00+01:00")
    pending = [
        {
            "event_id": "sequence-two",
            "simulated_time": "2026-01-01T00:00:00Z",
            "sequence": 2,
        },
        {
            "event_id": "sequence-one",
            "simulated_time": "2026-01-01T01:00:00+01:00",
            "sequence": 1,
        },
    ]

    assert ordered_due_events(current, target, pending) == [
        "sequence-one",
        "sequence-two",
    ]


def test_late_events_keep_original_time_order_and_reconstruct_same_replay():
    now = datetime(2026, 1, 5, tzinfo=UTC)
    pending = [
        event("late-after", now - timedelta(days=1), 1),
        event("late-before", now - timedelta(days=3), 90),
        event("due-now", now, 0),
    ]
    original = [item.copy() for item in pending]

    first_run = ordered_due_events(now, now, pending)
    reconstructed_run = ordered_due_events(now, now, pending)

    assert first_run == ["late-before", "late-after", "due-now"]
    assert reconstructed_run == first_run
    assert pending == original


def test_backward_clock_advance_is_rejected():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="target must not precede"):
        ordered_due_events(now, now - timedelta(seconds=1), [])


def test_naive_datetime_and_invalid_event_shape_are_rejected_by_pydantic():
    with pytest.raises(ValidationError):
        ordered_due_events(
            datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC), []
        )
    with pytest.raises(ValidationError):
        ordered_due_events(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            [{"event_id": "missing-fields"}],
        )
