"""Contract checks against the pinned τ retail runtime and source files."""

from __future__ import annotations

import json

from servers.simulation import tau2_retail


def test_pinned_catalogue_and_task_manifest_preserve_source_ids() -> None:
    assert len(tau2_retail.verify_vendored_data()) == 4
    tasks = tau2_retail.load_tasks()
    assert len(tasks) == 114
    assert tasks[0].id == "0"

    manifest = tau2_retail.task_manifest("0")
    source = json.loads(tau2_retail.TASKS_PATH.read_text())
    original_task = next(task for task in source if task["id"] == "0")
    assert manifest["task"] == original_task
    assert manifest["task"]["initial_state"] is None
    assert manifest["source_database"]["path"].endswith("/retail/db.json")
    assert manifest["original_db"]["users"]["yusuf_rossi_9620"]["user_id"] == "yusuf_rossi_9620"
    assert manifest["original_db"]["orders"]["#W2378156"]["order_id"] == "#W2378156"
    assert manifest["id_mapping"]["items"]["1151293680"] == "tau2:retail:item:1151293680"


def test_native_environment_evaluator_scores_projected_state_and_leaves_nl_unscored() -> None:
    from tau2.domains.retail.environment import get_environment

    task = tau2_retail.get_task("0")
    expected = get_environment()
    for action in task.evaluation_criteria.actions:
        expected.make_tool_call(
            tool_name=action.name,
            requestor=action.requestor,
            **action.arguments,
        )

    result = tau2_retail.evaluate_observed_state(
        "0", expected.tools.db.model_dump(mode="json")
    )
    assert result["components"]["DB"] == {"status": "evaluated", "reward": 1.0}
    assert result["components"]["NL_ASSERTION"] == {"status": "unevaluated", "reward": None}
    assert result["reward"] is None
    assert result["reference_replay"] == {"status": "completed", "failed_actions": []}


def test_dangling_upstream_source_references_are_reported_without_substitution() -> None:
    manifest = tau2_retail.task_manifest("2")
    assert manifest["source_integrity"] == {
        "status": "failed",
        "issues": [{
            "entity": "products",
            "reason": "source_record_missing",
            "source_ids": ["6086499569"],
        }],
    }
    assert "6086499569" not in manifest["original_db"]["products"]


def test_unmodified_baseline_does_not_match_upstream_reference_result() -> None:
    baseline = json.loads(tau2_retail.DB_PATH.read_text())
    # Task 0 is expected to exchange a delivered order; the unchanged baseline
    # therefore must not match the target end state.
    result = tau2_retail.evaluate_observed_state("0", baseline)
    assert result["components"]["DB"]["status"] == "evaluated"
    assert result["components"]["DB"]["reward"] == 0.0
    assert result["reward"] is None
    assert result["reference_replay"] == {"status": "completed", "failed_actions": []}


def test_shared_record_ambiguity_makes_only_criteria_affected_db_inconclusive() -> None:
    from tau2.domains.retail.environment import get_environment

    expected = get_environment()
    for action in tau2_retail.get_task("0").evaluation_criteria.actions:
        expected.make_tool_call(
            tool_name=action.name,
            requestor=action.requestor,
            **action.arguments,
        )
    target_ambiguity = {
        "entity": "orders", "source_id": "#W2378156", "field": "service_request",
        "reason": "shared_record_state_ambiguous",
    }
    inconclusive = tau2_retail.evaluate_observed_state(
        "0", expected.tools.db.model_dump(mode="json"), unmapped=[target_ambiguity],
    )
    assert inconclusive["components"]["DB"] == {"status": "inconclusive", "reward": None}
    assert inconclusive["criteria_mapping"]["db_status"] == "inconclusive"
    assert inconclusive["criteria_mapping"]["shared_record_ambiguities"] == [target_ambiguity]
    assert inconclusive["reward"] is None
    assert inconclusive["reward_status"] == "inconclusive"

    unrelated = tau2_retail.evaluate_observed_state(
        "0", expected.tools.db.model_dump(mode="json"),
        unmapped=[{**target_ambiguity, "source_id": "#UNRELATED"}],
    )
    assert unrelated["components"]["DB"] == {"status": "evaluated", "reward": 1.0}
    assert unrelated["criteria_mapping"]["db_status"] == "native_projection_complete"


def test_problem_adapter_binds_private_manifest_to_native_seed_and_observation() -> None:
    from datetime import datetime, timezone
    from servers.simulation.actor_protocol import ActorTurnResponse
    from servers.simulation.problem_adapters import TauRetailProblemAdapter

    class Worker:
        def retail_manifest(self, task_id):
            return tau2_retail.task_manifest(task_id)

        def retail_seed_email(self, request):
            return ActorTurnResponse(turn_id=request.turn_id, status="completed",
                                     body="I need help with order W2378156.",
                                     state={"turn": "seeded"})

        def initialize_retail_feedback(self, task_id, experiment_id, observed_db):
            self.initialized = (task_id, experiment_id, observed_db)
            return {"task_id": task_id, "experiment_id": experiment_id, "status": "initialized"}

        def evaluate_retail_feedback(self, task_id, experiment_id, observed_db, unmapped, trajectory):
            self.evaluation = {
                "task_id": task_id, "experiment_id": experiment_id,
                "observed_db": observed_db, "unmapped": unmapped, "trajectory": trajectory,
            }
            return {"task_id": task_id, "reward_status": "partial", "reward": None}

    class RetailAdmin:
        def seed_task(self, **values):
            return {"task_id": values["task_id"], "experiment_id": values["experiment_id"]}

        def observe_task(self, manifest, experiment_id):
            return {"task_id": manifest["task_id"], "experiment_id": experiment_id,
                    "observation": {"products": {}, "users": {}, "orders": {}},
                    "unmapped": [], "source_gaps": []}

        def capture_evaluation_evidence(self, manifest, experiment_id, import_receipt):
            observed = self.observe_task(manifest, experiment_id)
            return {**observed, "native_evidence": {"documents": {}},
                    "version_manifest": {"schema_version": 1, "sha256": "e" * 64}}

    worker = Worker()
    adapter = TauRetailProblemAdapter(worker, RetailAdmin())
    prepared = adapter.prepare(
        run_id="run-123", generation=0, task_id="0",
        projection_as_of=datetime(2026, 10, 30, tzinfo=timezone.utc),
        customer_actor_id="customer", support_actor_id="viv",
    )
    assert prepared.customer_private_state()["tau2_task"]["id"] == "0"
    assert prepared.customer_user_state == {"turn": "seeded"}
    assert prepared.initial_email.body.startswith("I need help")
    assert "evaluation_criteria" in prepared.snapshot["task"]
    assert worker.initialized == ("0", "run-123", {"products": {}, "users": {}, "orders": {}})

    result = adapter.evaluate(prepared=prepared, trajectory=[{"role": "user", "content": "Help"}])
    assert result["evaluation"]["reward_status"] == "partial"
    assert worker.evaluation["observed_db"] == {"products": {}, "users": {}, "orders": {}}
    assert worker.evaluation["trajectory"] == [{"role": "user", "content": "Help"}]


def test_tau_evaluate_captured_never_rereads_native_erp() -> None:
    from datetime import datetime, timezone
    from servers.simulation.actor_protocol import ActorTurnResponse
    from servers.simulation.problem_adapters import TauRetailProblemAdapter

    class Worker:
        def retail_manifest(self, task_id):
            return tau2_retail.task_manifest(task_id)

        def retail_seed_email(self, request):
            return ActorTurnResponse(turn_id=request.turn_id, status="completed", body="Help", state={})

        def initialize_retail_feedback(self, task_id, experiment_id, observed_db):
            return {"task_id": task_id, "experiment_id": experiment_id}

        def evaluate_retail_feedback(self, task_id, experiment_id, observed_db, unmapped, trajectory):
            self.values = (observed_db, unmapped, trajectory)
            return {"reward_status": "partial", "reward": None}

    class RetailAdmin:
        def seed_task(self, **values):
            return {"task_id": values["task_id"], "experiment_id": values["experiment_id"]}

        def observe_task(self, manifest, experiment_id):
            return {"task_id": manifest["task_id"], "experiment_id": experiment_id,
                    "observation": {"products": {}, "users": {}, "orders": {}},
                    "unmapped": [], "source_gaps": []}

        def capture_evaluation_evidence(self, manifest, experiment_id, import_receipt):
            raise AssertionError("frozen evaluation must not reread ERP")

    worker = Worker()
    adapter = TauRetailProblemAdapter(worker, RetailAdmin())
    prepared = adapter.prepare(
        run_id="run-456", generation=0, task_id="0",
        projection_as_of=datetime(2026, 10, 30, tzinfo=timezone.utc),
        customer_actor_id="customer", support_actor_id="support",
    )
    captured = {
        "task_id": "0", "experiment_id": "run-456",
        "observation": {"products": {}, "users": {}, "orders": {"frozen": True}},
        "unmapped": [{"entity": "orders", "source_id": "#W2378156", "field": "status", "reason": "native_gap"}],
        "source_gaps": [],
    }
    trajectory = [{"role": "user", "content": "Frozen email"}]
    result = adapter.evaluate_captured(prepared=prepared, trajectory=trajectory, captured=captured)
    assert result["evaluation"]["reward_status"] == "partial"
    assert worker.values == (
        captured["observation"], captured["unmapped"], trajectory,
    )
