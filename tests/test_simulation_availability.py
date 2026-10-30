from datetime import datetime, timezone

import pytest

from servers.simulation.availability import AvailabilityCalendar, response_time


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_working_calendar_delays_weekend_and_holiday_without_advancing_clock():
    calendar = AvailabilityCalendar.model_validate({
        "time_zone": "Europe/London",
        "windows": [{"weekdays": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"}],
        "unavailable_dates": ["2026-11-02"],
    })
    assert calendar.next_available(at("2026-10-30T18:00")) == at("2026-11-03T09:00")
    assert calendar.next_available(at("2026-10-29T11:30")) == at("2026-10-29T11:30")


def test_calendar_honors_local_summer_time_and_next_window():
    calendar = AvailabilityCalendar.model_validate({
        "time_zone": "Europe/London",
        "windows": [{"weekdays": [0], "start": "09:00", "end": "12:00"}],
    })
    assert calendar.next_available(at("2026-07-05T18:00")) == at("2026-07-06T08:00")
    assert calendar.next_available(at("2026-07-06T11:00")) == at("2026-07-13T08:00")


def test_absent_calendar_does_not_invent_an_availability_restriction():
    instant = at("2026-11-01T03:00")
    assert response_time({}, instant) == instant
    with pytest.raises(ValueError):
        AvailabilityCalendar.model_validate({"windows": []})


def test_calendar_and_escalation_are_persisted_together_without_mail_submission(tmp_path):
    from uuid import uuid4

    from servers.simulation.engine import SimulationEngine
    from servers.simulation.models import CanonicalEvent, StartScenario
    from servers.simulation.store import SQLiteStore

    store = SQLiteStore(tmp_path / "calendar.sqlite")
    run_id = uuid4()
    store.create_run(run_id, StartScenario(scenario_id="C1"))
    store.update_run(run_id, 0, simulated_time="2026-10-30T18:00:00+00:00")
    store.put_actor(run_id, 0, "warehouse", kind="warehouse", email="warehouse@example.test",
        private_state={"availability_calendar": {
            "windows": [{"weekdays": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"}],
        }, "escalation_actor_id": "manager"})
    store.put_actor(run_id, 0, "manager", kind="approver", email="manager@example.test", private_state={})
    # Exercise scheduling/storage only: no scenario initialization, provider,
    # clock advancement, actor turn or mail send is invoked.
    engine = SimulationEngine(store, None, None, None, [])
    event = CanonicalEvent(event_id="incoming-request", run_id=run_id, scenario_id="C1",
        simulated_time="2026-10-30T18:00:00+00:00", event_type="EMPLOYEE_RESPONSE_READY",
        source="mail_gateway", entity_type="case", entity_id="case-1",
        payload={"message_id": "<integration@example.test>"})
    actor = store.get_actor(run_id, 0, "warehouse")
    run = store.get_run(run_id)
    engine._enqueue_actor_event(event, run, actor)
    pending = store.pending_events(run_id, 0)
    assert len(pending) == 1
    assert datetime.fromisoformat(pending[0]["simulated_time"].replace("Z", "+00:00")) == at("2026-11-02T09:00")
    notice = store.list_outbox(run_id, 0)[0]
    assert notice["status"] == "pending" and not notice["submission_started"]
    assert notice["private_payload"]["to_actor_id"] == "viv"
    assert "manager@example.test" in notice["private_payload"]["body"]
    assert store.get_run(run_id)["simulated_time"] == "2026-10-30T18:00:00+00:00"
