"""Company working calendars, separate from benchmark actor decision logic."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Annotated

from dateutil.tz import resolve_imaginary
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkingWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(min_length=1)
    start: time
    end: time

    @model_validator(mode="after")
    def ordered_window(self):
        if self.start.tzinfo or self.end.tzinfo or self.end <= self.start:
            raise ValueError("working windows use local times with end after start")
        return self


class AvailabilityCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_zone: str = "UTC"
    windows: list[WorkingWindow] = Field(min_length=1)
    unavailable_dates: list[date] = Field(default_factory=list)

    @field_validator("time_zone")
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value

    def next_available(self, instant: datetime) -> datetime:
        """Return this instant or the next opening, without advancing any clock."""
        if instant.tzinfo is None:
            raise ValueError("availability requires an aware datetime")
        zone = ZoneInfo(self.time_zone)
        local = instant.astimezone(zone)
        excluded = set(self.unavailable_dates)
        # Each excluded date can remove at most one weekly opening. There is
        # always an opening within this finite bound for a valid weekly calendar.
        for offset in range(7 * (len(excluded) + 1) + 1):
            day = local.date() + timedelta(days=offset)
            if day in excluded:
                continue
            openings = []
            for window in self.windows:
                if day.weekday() not in window.weekdays:
                    continue
                start = resolve_imaginary(datetime.combine(day, window.start, zone))
                end = resolve_imaginary(datetime.combine(day, window.end, zone))
                candidate = max(local, start)
                if candidate < end:
                    openings.append(candidate)
            if openings:
                return min(openings).astimezone(timezone.utc)
        raise RuntimeError("valid availability calendar has no reachable opening")


def response_time(private_state: dict, earliest: datetime) -> datetime:
    configured = private_state.get("availability_calendar")
    if configured is None:
        return earliest
    return AvailabilityCalendar.model_validate(configured).next_available(earliest)
