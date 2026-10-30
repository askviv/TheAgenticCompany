from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from servers.simulation.engine import SimulationEngine
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore


def test_existing_evaluator_uses_captured_run_audit_and_clock_after_live_changes(tmp_path):
    store = SQLiteStore(Path(tmp_path) / "snapshot.sqlite")
    run_id = str(uuid4())
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    store.create_run(run_id, StartScenario(scenario_id="A1"),
                     manifest={"initial_time": start.isoformat(),
                               "environment": {"source_tree_sha256": "fixture-source"}},
                     erp_binding={"documents": {}})
    store.update_run(run_id, 0, simulated_time=start)
    store.append_audit(run_id, 0, {"event": "erp_read", "actor_id": "viv",
                                   "operation": "get_purchase_order", "entity_id": "PO-1"})

    engine = SimulationEngine(store, object(), lambda _binding: None, object(), [])
    engine.environment_manifest = {"source_tree_sha256": "fixture-source"}
    engine.environment_manifest_provider = lambda: {"source_tree_sha256": "fixture-source"}
    with engine._operation_guard(run_id):
        snapshot = engine.capture_evaluation_inputs(run_id, 0)

    later = start + timedelta(hours=3)
    store.update_run(run_id, 0, simulated_time=later)
    store.append_audit(run_id, 0, {"event": "erp_read", "actor_id": "viv",
                                   "operation": "get_purchase_invoice", "entity_id": "PI-1"})

    result = engine.evaluate_captured(run_id, snapshot)

    assert result["metrics"]["tool_calls"]["count"] == 1
    assert result["metrics"]["elapsed_simulated_seconds"] == 0
    assert snapshot["source_records"]["run"]["simulated_time"] == start.isoformat()
