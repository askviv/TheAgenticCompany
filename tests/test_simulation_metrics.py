from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from servers.simulation.engine import SimulationEngine
from servers.simulation.metrics import evaluation_metrics
from servers.simulation.models import HumanIntervention, ModelUsageRecord, StartScenario
from servers.simulation.store import SQLiteStore, IdempotencyConflict


def test_metrics_report_partial_measurement_and_explain_failed_checks():
    audit = [
        {"private_record": {"event": "erp_read", "actor_id": "agent-x"}},
        {"private_record": {"event": "message_queued", "sender_actor_id": "agent-x"}},
        {"private_record": {"event": "model_usage_recorded", "usage_scope": "agent", "provider": "provider-a",
                             "model_id": "model-b", "model_version": "2026-10",
                             "input_tokens": 10, "output_tokens": 4, "cost": "0.03", "currency": "GBP"}},
        {"private_record": {"event": "human_intervention_recorded", "intervention_type": "correction",
                             "effort_seconds": 30}},
    ]
    run = {"agent_id": "agent-x", "simulated_time": "2026-10-07T09:00:00+00:00",
           "manifest": {"initial_time": "2026-10-06T09:00:00+00:00",
                        "agent_metadata": {"model_id": "model-b", "prompt_sha256": "a" * 64}}}

    result = evaluation_metrics(checks={"review_requested": False, "no_payment_posted": True}, audit=audit, run=run)

    assert result["tool_calls"]["count"] == 2
    assert result["tool_calls"]["by_type"] == {"erp_read": 1, "message_queued": 1}
    assert result["elapsed_simulated_seconds"] == 86_400
    assert result["model_usage"]["input_tokens"] == 10
    assert result["model_usage"]["cost_by_currency"] == {"GBP": "0.03"}
    assert result["human_intervention"] == {
        "status": "recorded_events_only", "count": 1, "effort_seconds": 30,
        "effort_measured_subtotal_seconds": 30,
        "effort_status": "measured", "types": {"correction": 1},
    }
    assert result["failure_explanations"]["review_requested"]
    assert result["categories"]["policy_compliance"]["complete"] is False


def test_unmeasured_usage_and_effort_remain_null_not_zero():
    run = {"agent_id": "agent-x", "simulated_time": None, "manifest": {}}
    result = evaluation_metrics(checks={}, audit=[], run=run)
    assert result["model_usage"]["status"] == "not_measured"
    assert result["model_usage"]["cost_status"] == "not_measured"
    assert result["model_usage"]["input_tokens"] is None
    assert result["human_intervention"]["effort_seconds"] is None
    assert result["elapsed_simulated_seconds"] is None


def test_partial_measurements_are_labeled_and_do_not_claim_a_total():
    audit = [
        {"private_record": {"event": "model_usage_recorded", "usage_scope": "agent", "provider": "p", "model_id": "m",
                             "cost": "0.10", "currency": "GBP"}},
        {"private_record": {"event": "model_usage_recorded", "usage_scope": "agent", "provider": "p", "model_id": "m"}},
        {"private_record": {"event": "human_intervention_recorded", "intervention_type": "rescue",
                             "effort_seconds": 15}},
        {"private_record": {"event": "human_intervention_recorded", "intervention_type": "manual_action"}},
    ]
    result = evaluation_metrics(checks={}, audit=audit,
        run={"agent_id": "a", "manifest": {}, "simulated_time": None})
    assert result["model_usage"]["cost_status"] == "partially_measured"
    assert result["model_usage"]["cost_by_currency"] == {"GBP": "0.10"}
    assert result["human_intervention"]["effort_seconds"] is None
    assert result["human_intervention"]["effort_status"] == "partially_measured"
    assert result["human_intervention"]["effort_measured_subtotal_seconds"] == 15


def test_usage_model_requires_cost_currency_pair():
    with pytest.raises(ValueError):
        ModelUsageRecord(provider="p", model_id="m", cost=Decimal("0.1"))
    with pytest.raises(ValueError):
        ModelUsageRecord(provider="p", model_id="m", currency="GBP")


class _Mail:
    def send(self, *args, **kwargs):
        return {"message_id": kwargs.get("message_id"), "mailbox_uid": 1}

    def find_receipt(self, *args, **kwargs):
        return None

    def poll(self, *args, **kwargs):
        return []


class _ERP:
    def get_purchase_order(self, *_args):
        return {"name": "PO-1", "modified": "v1", "items": []}


class _Admin:
    def create(self, _run_id):
        return {"site_name": "site", "documents": {"purchase_order": "PO-1", "supplier": "SUP-1"}}

    def reset(self, run_id):
        return self.create(run_id)


def _actors():
    return [
        {"actor_id": "agent-x", "kind": "agent", "email": "agent@example.test", "private_state": {}},
        {"actor_id": "requester", "kind": "employee", "email": "requester@example.test", "private_state": {}},
        {"actor_id": "supplier", "kind": "supplier", "email": "supplier@example.test", "private_state": {}},
    ]


def test_telemetry_and_interventions_are_append_only_idempotent_and_run_scoped(tmp_path):
    store = SQLiteStore(tmp_path / "metrics.sqlite")
    engine = SimulationEngine(store, _Mail(), lambda _binding: _ERP(), _Admin(), _actors())
    metadata = {
        "harness_id": "test-harness", "harness_version": "2",
        "model_provider": "provider", "model_id": "model", "model_version": "v3",
        "prompt_id": "case-prompt", "prompt_version": "7", "prompt_sha256": "a" * 64,
        "tool_config_id": "erp-mail", "tool_config_version": "1", "tool_config_sha256": "b" * 64,
    }
    run = engine.start(StartScenario(scenario_id="C1", agent_id="agent-x",
        agent_configuration={"start_time": "2026-10-06T09:00:00+00:00", "prompt": "never persist this"},
        agent_metadata=metadata), "metrics-start", "controller")
    run_id = run["run_id"]
    manifest = store.get_run(run_id)["manifest"]
    assert "never persist this" not in str(manifest)
    assert manifest["agent_configuration"]["prompt_sha256"] == sha256(b'"never persist this"').hexdigest()
    assert manifest["agent_metadata"] == metadata

    usage = ModelUsageRecord(provider="provider", model_id="model", model_version="v3",
                             input_tokens=20, output_tokens=10)
    intervention = HumanIntervention(intervention_type="rescue", category="state_recovery")
    first_usage = engine.record_model_usage(run_id, usage, "usage-key", "controller")
    assert engine.record_model_usage(run_id, usage, "usage-key", "controller") == first_usage
    with pytest.raises(IdempotencyConflict):
        engine.record_model_usage(run_id, ModelUsageRecord(provider="other", model_id="model"),
                                  "usage-key", "controller")
    intervention_result = engine.record_human_intervention(run_id, intervention, "help-key", "controller")
    assert intervention_result["recorded"] is True
    audit = store.list_audit(run_id, 0)
    assert [row["private_record"]["event"] for row in audit].count("model_usage_recorded") == 1
    assert [row["private_record"]["event"] for row in audit].count("human_intervention_recorded") == 1
    assert "never persist this" not in str(audit)
    report = engine.evaluate(run_id)
    assert report["agent_performance"] == "incomplete"
    assert report["metrics"]["model_usage"]["status"] == "measured"
    assert report["metrics"]["model_usage"]["cost_status"] == "not_measured"
    assert report["metrics"]["human_intervention"]["count"] == 1
    exported = engine.trajectory(run_id)
    assert "erp_binding" not in exported["run"]
    assert "private_scenario_policy" not in exported["run"]["manifest"]

    engine.reset(run_id, "reset-metrics", "controller")
    reset_manifest = store.get_run(run_id)["manifest"]
    assert reset_manifest["agent_metadata"] == metadata
    assert "never persist this" not in str(reset_manifest)


def test_simulator_usage_is_separate_from_the_tested_agent_and_unknown_history():
    result = evaluation_metrics(checks={}, run={"agent_id": "viv", "manifest": {}}, audit=[
        {"private_record": {"event": "model_usage_recorded", "usage_scope": "environment",
                            "input_tokens": 10, "output_tokens": 3, "cost": "0.25", "currency": "GBP"}},
        {"private_record": {"event": "model_usage_recorded", "input_tokens": 7}},
    ])
    assert result["model_usage"]["status"] == "not_measured"
    assert result["model_usage"]["cost_by_currency"] is None
    assert result["environment_model_usage"]["cost_by_currency"] == {"GBP": "0.25"}
    assert result["unattributed_model_usage"]["input_tokens"] == 7


def test_corrupt_recorded_time_is_visible_instead_of_reported_as_unmeasured():
    with pytest.raises(ValueError, match="invalid recorded timestamp") as failure:
        evaluation_metrics(checks={}, audit=[], run={"agent_id": "a", "manifest": {"initial_time": "broken"}})
    assert failure.value.__cause__ is not None


def test_missing_revised_quote_counts_as_incomplete_investigation():
    result = evaluation_metrics(checks={"revised_quote_received_and_read": False}, audit=[],
        run={"agent_id": "agent", "manifest": {}})
    category = result["categories"]["investigation_completeness"]
    assert category["status"] == "measured"
    assert category["total"] == 1 and category["complete"] is False
    assert result["failure_explanations"]["revised_quote_received_and_read"]


def test_failed_run_can_record_recovery_effort_and_export_its_private_history(tmp_path):
    from uuid import uuid4

    store = SQLiteStore(tmp_path / "failed-history.sqlite")
    run_id = uuid4()
    store.create_run(run_id, StartScenario(scenario_id="C1"))
    store.update_run(run_id, 0, status="failed")
    engine = SimulationEngine(store, None, None, None, [])
    engine.record_human_intervention(run_id,
        HumanIntervention(intervention_type="correction", category="state_recovery", effort_seconds=45),
        "recovery-effort", "controller")
    engine.record_model_usage(run_id, ModelUsageRecord(provider="codex-cli", model_id="model"),
                             "late-usage", "controller")
    exported = engine.trajectory(run_id)
    assert exported["run"]["status"] == "failed"
    assert [row["private_record"]["event"] for row in exported["audit"]] == [
        "human_intervention_recorded", "model_usage_recorded",
    ]
    assert store.get_run(run_id)["status"] == "failed"


def test_inbox_tool_count_is_per_request_not_per_message(tmp_path):
    from uuid import uuid4

    class InboxTransport:
        def poll_viv(self, **kwargs):
            return [{"message_id": "<one@example.test>"}, {"message_id": "<two@example.test>"}]

    store = SQLiteStore(tmp_path / "inbox.sqlite")
    run_id = uuid4()
    store.create_run(run_id, StartScenario(scenario_id="C1", agent_id="viv"))
    store.put_actor(run_id, 0, "viv", kind="agent", email="viv@example.test", private_state={})
    engine = SimulationEngine(store, InboxTransport(), None, None, [])
    assert len(engine.inbox(run_id, "viv")) == 2
    audit = store.list_audit(run_id, 0)
    result = evaluation_metrics(checks={}, audit=audit, run=store.get_run(run_id))
    assert result["tool_calls"]["count"] == 1
    assert sum(row["private_record"]["event"] == "agent_inbox_read" for row in audit) == 2
