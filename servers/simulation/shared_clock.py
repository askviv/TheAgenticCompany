"""Persist and publish the company business simulation clock.

The publisher owns one durable clock state and one shared follow file. The host
clock is never modified; rate calculations use ``time.monotonic_ns()`` between
checkpoints. The coordinator wires its company ticks to the same follow file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import IdempotencyConflict


class SharedCompanyClock:
    """Adjustable clock backed by a private JSON state and shared mtime file.

    The follow file's nanosecond mtime is the consumer contract. A publisher
    tick moves that timestamp to the current simulated epoch; business scheduling reads the same published instant. Infrastructure does
    not mount this file or change its operating-system clock.
    """

    DEFAULT_SPEED = 24.0
    TICK_SECONDS = 1.0

    def __init__(self, state_path: str | Path, follow_path: str | Path,
                 initial_instant: datetime | None = None, *, reset_to_initial: bool = False,
                 require_existing_state: bool = False):
        self.state_path = Path(state_path)
        self.follow_path = Path(follow_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.follow_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._lock_path.touch(mode=0o600, exist_ok=True)
        self._publisher_lock = self.state_path.with_suffix(self.state_path.suffix + ".publisher.lock").open("a+")
        try:
            fcntl.flock(self._publisher_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._publisher_lock.close()
            raise RuntimeError("another shared company clock publisher is already active") from error
        self._lock = threading.RLock()
        self.error: Exception | None = None
        state = self._load_or_initialize(initial_instant, reset_to_initial, require_existing_state)
        self._epoch_ns = int(state["epoch_ns"])
        self._speed = float(state["speed"])
        self._checkpoint_real_ns = int(state["checkpoint_real_ns"])
        self._speed_requests = dict(state.get("speed_requests", {}))
        now_real = time.time_ns()
        self._epoch_ns += int(max(0, now_real - self._checkpoint_real_ns) * self._speed)
        if initial_instant is not None:
            if initial_instant.tzinfo is None or initial_instant.utcoffset() is None:
                raise ValueError("initial company clock instant must include a timezone")
            self._epoch_ns = max(
                self._epoch_ns,
                int(initial_instant.astimezone(timezone.utc).timestamp() * 1e9),
            )
        self._epoch_ns = int(self._epoch_ns)
        self._mono_anchor_ns = time.monotonic_ns()
        self._persist_and_publish(self._mono_anchor_ns)

    def _load_or_initialize(self, initial_instant: datetime | None, reset_to_initial: bool,
                            require_existing_state: bool) -> dict[str, Any]:
        with self._locked_file():
            if self.state_path.exists():
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not {"epoch_ns", "speed", "checkpoint_real_ns"}.issubset(state) or set(state) - {
                    "epoch_ns", "speed", "checkpoint_real_ns", "speed_requests",
                }:
                    raise ValueError("shared clock state has an unsupported shape")
                if not isinstance(state["epoch_ns"], int) or not isinstance(state["checkpoint_real_ns"], int):
                    raise ValueError("shared clock epoch fields must be integers")
                if not isinstance(state["speed"], (int, float)) or not math.isfinite(state["speed"]) or state["speed"] <= 0:
                    raise ValueError("shared clock speed must be positive")
                if "speed_requests" not in state:
                    state["speed_requests"] = {}
                if not isinstance(state["speed_requests"], dict):
                    raise ValueError("shared clock speed receipts must be an object")
                if reset_to_initial:
                    if initial_instant is None or initial_instant.tzinfo is None or initial_instant.utcoffset() is None:
                        raise ValueError("source transition requires a persisted company clock instant")
                    state["epoch_ns"] = int(initial_instant.astimezone(timezone.utc).timestamp() * 1e9)
                    state["checkpoint_real_ns"] = time.time_ns()
                    self._write_state(state)
                return state
            if require_existing_state:
                raise FileNotFoundError("persisted shared company clock state is missing")
            if initial_instant is not None and (initial_instant.tzinfo is None or initial_instant.utcoffset() is None):
                raise ValueError("initial company clock instant must include a timezone")
            state = {
                "epoch_ns": (int(initial_instant.astimezone(timezone.utc).timestamp() * 1e9)
                             if initial_instant is not None else time.time_ns()),
                "speed": self.DEFAULT_SPEED,
                "checkpoint_real_ns": time.time_ns(),
                "speed_requests": {},
            }
            self._write_state(state)
            return state

    def _locked_file(self):
        """Lock the state across publisher instances and process restarts."""
        class Lock:
            def __init__(inner_self, path: Path):
                inner_self.file = path.open("a+")

            def __enter__(inner_self):
                fcntl.flock(inner_self.file.fileno(), fcntl.LOCK_EX)
                return inner_self.file

            def __exit__(inner_self, *_exc):
                fcntl.flock(inner_self.file.fileno(), fcntl.LOCK_UN)
                inner_self.file.close()

        return Lock(self._lock_path)

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _current_epoch_ns(self, monotonic_ns: int) -> int:
        elapsed = max(0, monotonic_ns - self._mono_anchor_ns)
        return self._epoch_ns + int(elapsed * self._speed)

    def _persist_and_publish(self, monotonic_ns: int | None = None) -> int:
        mono = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        epoch_ns = self._current_epoch_ns(mono)
        real_ns = time.time_ns()
        with self._locked_file():
            self._write_state({
                "epoch_ns": epoch_ns,
                "speed": self._speed,
                "checkpoint_real_ns": real_ns,
                "speed_requests": self._speed_requests,
            })
            if not self.follow_path.exists():
                fd = os.open(self.follow_path, os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    os.fchmod(fd, 0o644)
                finally:
                    os.close(fd)
            os.utime(self.follow_path, ns=(epoch_ns, epoch_ns))
        self._epoch_ns = epoch_ns
        self._mono_anchor_ns = mono
        self._checkpoint_real_ns = real_ns
        return epoch_ns

    def now(self) -> datetime:
        """Read the current company instant from the shared consumer source."""
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        stat = self.follow_path.stat()
        return datetime.fromtimestamp(stat.st_mtime_ns / 1e9, timezone.utc)

    def speed(self) -> float:
        """Return the current rate maintained by the shared clock source."""
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        with self._lock:
            return self._speed

    def set_speed(self, speed: float, idempotency_key: str) -> dict[str, float]:
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("clock speed must be a finite, positive number")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        if self.error is not None:
            raise RuntimeError("shared company clock publisher failed") from self.error
        speed = float(speed)
        request = format(speed, ".17g")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_hash = hashlib.sha256(request.encode("ascii")).hexdigest()
        with self._lock:
            previous = self._speed_requests.get(key_hash)
            if previous is not None:
                if previous != request_hash:
                    raise IdempotencyConflict("clock speed idempotency key reused with different request data")
                return {"speed": speed}
            mono = time.monotonic_ns()
            self._epoch_ns = self._current_epoch_ns(mono)
            self._mono_anchor_ns = mono
            self._speed = speed
            self._speed_requests[key_hash] = request_hash
            try:
                self._persist_and_publish(mono)
            except Exception as error:
                self.error = error
                raise
            return {"speed": speed}

    def publish_forever(self, stop: threading.Event) -> None:
        """Update the shared file at one-second real intervals until stopped."""
        while not stop.wait(self.TICK_SECONDS):
            if self.error is not None:
                raise RuntimeError("shared company clock publisher failed") from self.error
            with self._lock:
                self._persist_and_publish()

    def close(self) -> None:
        """Release the single-publisher process lock after its worker stops."""
        if not self._publisher_lock.closed:
            fcntl.flock(self._publisher_lock.fileno(), fcntl.LOCK_UN)
            self._publisher_lock.close()
