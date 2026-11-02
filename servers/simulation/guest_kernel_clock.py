"""Private API client for speed control of the VM's accelerated guest clock."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import requests
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt


class ClockSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    speed: StrictInt = Field(gt=0)
    epoch_ns: StrictInt
    mode: Literal["guest-kernel"]


class SpeedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    speed: StrictInt = Field(gt=0)


class RealTimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    epoch_ns: StrictInt = Field(gt=0)


class DeadlineRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    issue_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline_at: AwareDatetime


class GuestKernelClockClient:
    """Keep speed control independent from the local guest clock source."""

    def __init__(self, base_url: str, token_file: Path, token_reader: Callable[[Path], str]):
        self.token_file = token_file
        self.token_reader = token_reader
        self.base_url = base_url.rstrip("/")
        self.client = requests.Session()

    def _request(self, method: str, path: str, *, idempotency_key: str | None = None,
                 speed: int | None = None, payload: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token_reader(self.token_file)}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = self.client.request(
            method, f"{self.base_url}{path}", headers=headers,
            json=payload if payload is not None else ({"speed": speed} if speed is not None else None), timeout=None,
        )
        response.raise_for_status()
        return response.json()

    def speed(self) -> int:
        return ClockSnapshot.model_validate(self._request("GET", "/clock")).speed

    def set_speed(self, speed: int, idempotency_key: str) -> dict[str, int]:
        receipt = SpeedReceipt.model_validate(self._request(
            "POST", "/clock/speed", idempotency_key=idempotency_key, speed=speed,
        ))
        if receipt.speed != speed:
            raise ValueError("Guest clock API confirmed a different speed")
        return {"speed": receipt.speed}

    def real_now(self) -> datetime:
        snapshot = RealTimeSnapshot.model_validate(self._request("GET", "/clock/real-time"))
        seconds, nanoseconds = divmod(snapshot.epoch_ns, 1_000_000_000)
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanoseconds // 1_000)

    def register_deadline(self, issue_key: str, deadline: datetime) -> None:
        registration = DeadlineRegistration(issue_key=issue_key, deadline_at=deadline)
        self._request("POST", "/issue-deadlines", payload=registration.model_dump(mode="json"))

    def close(self) -> None:
        self.client.close()
