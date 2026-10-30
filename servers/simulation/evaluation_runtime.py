"""Build the durable evaluation service from the engine's frozen-input hooks."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Any

from .evaluation_service import EvaluationService
from .store import SQLiteStore


def build_evaluation_service(engine: Any, store: SQLiteStore, evidence_directory: str | Path) -> EvaluationService:
    """Bind the durable service to explicit capture/evaluate/verify engine hooks.

    The evaluator receives only the private immutable capture. There is no
    fallback to the legacy live-reading ``engine.evaluate`` path.
    """
    required = (
        "capture_evaluation_inputs", "evaluate_captured", "verify_evaluation_inputs",
        "evaluation_identity",
    )
    missing = [name for name in required if not callable(getattr(engine, name, None))]
    if missing:
        raise RuntimeError("engine is missing frozen evaluation hooks: " + ",".join(missing))

    def identity_resolver(run_id: str, generation: int, snapshot: Any) -> dict[str, Any]:
        identity = engine.evaluation_identity(run_id, generation, snapshot)
        if not isinstance(identity, dict):
            raise TypeError("evaluation identity hook must return an object")
        return identity

    lock_directory = Path(evidence_directory).parent / "evaluation-locks"
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        lock_directory.chmod(0o700)

    @contextmanager
    def evaluation_lock(run_id: str):
        """Serialize duplicate evaluation requests without blocking company work."""
        lock_path = lock_directory / f"{run_id}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def try_evaluation_lock(run_id: str):
        lock_path = lock_directory / f"{run_id}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def capture(run_id: str, generation: int):
        if engine.store.is_company_experiment(run_id):
            with engine._operation_guard("company-clock"):
                with engine._operation_guard(run_id):
                    return engine.capture_evaluation_inputs(run_id, generation)
        with engine._operation_guard(run_id):
            return engine.capture_evaluation_inputs(run_id, generation)

    def verify(run_id: str, snapshot: Any):
        with engine._operation_guard(run_id):
            return engine.verify_evaluation_inputs(run_id, snapshot)

    service = EvaluationService(
        store,
        evidence_directory,
        snapshotter=capture,
        evaluator=lambda run_id, snapshot: engine.evaluate_captured(run_id, snapshot),
        version_verifier=verify,
        identity_resolver=identity_resolver,
        guard_factory=evaluation_lock,
    )
    service.reconcile_interrupted(try_evaluation_lock)
    return service
