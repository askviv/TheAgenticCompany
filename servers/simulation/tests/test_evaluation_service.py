from contextlib import contextmanager, nullcontext
from decimal import Decimal
import json
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from servers.simulation.evaluation_service import EvaluationService
from servers.simulation.evaluation_store import CriterionResult
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore, StoreConflict


def _run(store):
    run_id = str(uuid4())
    store.create_run(run_id, StartScenario(scenario_id="A1", seed=23))
    return run_id


def _service(store, directory, *, snapshotter=None, evaluator=None, verifier=None, calls=None):
    calls = calls if calls is not None else []

    def snapshotter_default(run_id, generation):
        return {"run_id": run_id, "generation": generation, "native_versions": {"invoice": "modified-1"},
                "trajectory": [{"body": "private evaluator evidence"}]}

    def evaluator_default(run_id, snapshot):
        calls.append((run_id, snapshot["generation"]))
        return {
            "checks": {"native_document_read": True, "required_action": False},
            "agent_performance": "incomplete",
            "metrics": {"categories": {"investigation": {
                "passed": 1, "total": 2, "complete": False, "status": "measured",
            }}},
            "problem_evaluation": {"evaluation": {
                "reward_status": "complete", "reward": 0.0,
                "components": {"DB": {"status": "evaluated", "reward": 0.0}},
            }},
            "body": "sensitive body stays in private result artifact",
            "prompt": "private evaluator prompt",
            "reasoning": "private reasoning must not enter score DTO",
        }

    return EvaluationService(
        store, directory,
        snapshotter=snapshotter or snapshotter_default,
        evaluator=evaluator or evaluator_default,
        version_verifier=verifier or (lambda _run_id, _snapshot: True),
        identity_resolver=lambda _run_id, _generation, _snapshot: {
            "benchmark": "tau2-bench", "domain": "retail",
            "benchmark_version": "7aad-pinned", "evaluation_mode": "company_adapted",
        },
        guard_factory=lambda _run_id: nullcontext(),
    )


def test_evaluation_persists_native_scale_score_and_immutable_private_artifacts(tmp_path):
    store = SQLiteStore(tmp_path / "evaluation.sqlite")
    run_id = _run(store)
    calls = []
    service = _service(store, tmp_path / "private-evidence", calls=calls)

    first = service.evaluate(run_id, "controller", "eval-1")
    replay = service.evaluate(run_id, "controller", "eval-1")
    record = first.record

    assert record.status == "completed"  # A completed evaluation may score the task 0.
    assert record.benchmark == "tau2-bench" and record.domain == "retail"
    assert record.evaluation_mode == "company_adapted"
    assert record.scores["problem_evaluation.evaluation.reward"] == 0.0
    assert record.scores["problem_evaluation.evaluation.components.DB.reward"] == 0.0
    assert record.scores["metrics.categories.investigation.passed"] == 1
    assert "combined_percentage" not in record.scores
    assert [(row.name, row.passed) for row in record.criterion_results] == [
        ("native_document_read", True), ("required_action", False),
        ("DB", None),
    ]
    assert "body" not in record.model_dump_json().casefold()
    assert "prompt" not in record.model_dump_json().casefold()
    assert "reasoning" not in record.model_dump_json().casefold()
    assert replay.record == record
    assert len(calls) == 1

    snapshot_path = Path(service.records.snapshot_directory / record.snapshot_ref)
    result_path = Path(service.records.snapshot_directory / record.result_ref)
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert "private evaluator evidence" in snapshot_path.read_text()
    assert "sensitive body stays in private result artifact" in result_path.read_text()

    public_record = record.public_dict()
    result_path.unlink()
    assert service.get_record(record.evaluation_id).public_dict() == public_record
    assert service.records.latest_record(run_id) == record
    assert service.list(run_id) == [record]

    store.reset_run(run_id, 0, {"run": {"status": "running", "manifest": {}, "erp_binding": {}}},
                    idempotency_scope="test-reset", idempotency_key="reset-1")
    old_generation_replay = service.evaluate(run_id, "controller", "eval-1")
    assert old_generation_replay.record.generation == 0
    assert old_generation_replay.record.evaluation_id == record.evaluation_id
    with pytest.raises(StoreConflict):
        service.evaluate(run_id, "controller", "eval-1", requested_generation=1)


def test_version_drift_persists_incomplete_without_scoring_snapshot(tmp_path):
    store = SQLiteStore(tmp_path / "version-drift.sqlite")
    run_id = _run(store)
    service = _service(store, tmp_path / "private-evidence", verifier=lambda _run, _snapshot: False)

    response = service.evaluate(run_id, "evaluator", "eval-drift")

    assert response.record.status == "incomplete"
    assert response.record.scores == {}
    assert response.record.criterion_results == []
    assert response.record.error_type == "EvidenceVersionMismatch"
    staged = service.records.read_artifact(response.record.result_ref, response.record.result_sha256)
    assert staged["result"]["problem_evaluation"]["evaluation"]["reward"] == 0.0
    assert response.record.result_sha256 is not None


def test_uncertain_evaluator_failure_is_persisted_and_never_blindly_repeated(tmp_path):
    store = SQLiteStore(tmp_path / "uncertain.sqlite")
    run_id = _run(store)
    calls = []

    def uncertain_evaluator(_run, _snapshot):
        calls.append("started")
        raise RuntimeError("provider response is ambiguous and must not be retained")

    service = _service(store, tmp_path / "private-evidence", evaluator=uncertain_evaluator)
    first = service.evaluate(run_id, "evaluator", "eval-uncertain")
    replay = service.evaluate(run_id, "evaluator", "eval-uncertain")

    assert first.record.status == "failed"
    assert first.record.lifecycle == "uncertain"
    assert first.record.error_type == "RuntimeError"
    assert first.record.scores == {}
    assert "ambiguous" not in first.record.model_dump_json()
    assert replay.record == first.record
    assert calls == ["started"]


def test_snapshot_failure_is_persisted_as_failed_before_return(tmp_path):
    store = SQLiteStore(tmp_path / "snapshot-failed.sqlite")
    run_id = _run(store)
    evaluator_calls = []

    def broken_snapshot(_run, _generation):
        raise OSError("private source failure text is not persisted")

    service = _service(store, tmp_path / "private-evidence", snapshotter=broken_snapshot,
                       evaluator=lambda *_args: evaluator_calls.append("called") or {})
    first = service.evaluate(run_id, "controller", "eval-failed")
    replay = service.evaluate(run_id, "controller", "eval-failed")

    assert first.record.status == "failed"
    assert first.record.error_type == "OSError"
    assert first.record.completed_at is not None
    assert "private source failure text" not in first.record.model_dump_json()
    assert replay.record == first.record
    assert evaluator_calls == []


def test_restart_reconciler_recovers_fsynced_result_before_marking_interrupted(tmp_path):
    store = SQLiteStore(tmp_path / "restart.sqlite")
    run_id = _run(store)
    service = _service(store, tmp_path / "private-evidence")
    record, _ = service.records.reserve(run_id, "controller", "restart-window")
    snapshot_ref, snapshot_hash = service.records.write_artifact(
        record.evaluation_id, "snapshot", {"run_id": run_id, "generation": 0},
    )
    service.records.append_event(record.evaluation_id, "snapshot_captured", {
        "snapshot_ref": snapshot_ref, "snapshot_sha256": snapshot_hash,
        "identity": {"benchmark": "tau2-bench", "domain": "retail",
                     "benchmark_version": "7aad-pinned", "evaluation_mode": "company_adapted"},
    })
    service.records.append_event(record.evaluation_id, "evaluator_started", {})
    _result_ref, result_hash = service.records.write_artifact(record.evaluation_id, "result", {
        "result": {"problem_evaluation": {"evaluation": {"reward": 0.0}}},
        "status": "completed", "scores": {"problem_evaluation.evaluation.reward": 0.0},
        "criterion_results": [], "error_type": None,
        "public_summary": {"warnings": [], "mapping_gaps": [],
                           "native_scale": {"problem_evaluation.evaluation.reward": "native_reward_0_to_1"},
                           "failure": None},
    })

    @contextmanager
    def available_lock(_run_id):
        yield True

    assert service.reconcile_interrupted(available_lock) == 1
    recovered = service.get_record(record.evaluation_id)
    assert recovered.status == "completed"
    assert recovered.scores["problem_evaluation.evaluation.reward"] == 0.0
    events = service.records.events(record.evaluation_id)
    assert any(event.event == "evaluator_result_staged"
               and event.metadata["result_sha256"] == result_hash for event in events)



@pytest.mark.parametrize('benchmark,native,key,value,status', [
    ('coffeebench', {'native_evaluations': [{'actor_id': 'roaster_A', 'status': 'final',
        'score_metric': 'audit.annual.true_net_income', 'score': -19.25}]},
     'coffeebench.roaster_A.audit.annual.true_net_income', -19.25, 'completed'),
    ('coffeebench', {'native_evaluations': [{'actor_id': 'roaster_A', 'status': 'incomplete',
        'score_metric': 'audit.annual.true_net_income', 'score': None}]},
     'coffeebench.roaster_A.audit.annual.true_net_income', None, 'incomplete'),
    ('ecommercebench', {'native_status': 'finished', 'native_analysis': {
        'profitability': {'final_balance': 123.45}, 'negotiation_quality': {'%Oracle': 37.25},
        'reasoning': 'private supplier narrative'}},
     'ecommercebench.negotiation_quality.%Oracle', 37.25, 'completed'),
    ('ecommercebench', {'native_status': 'incomplete', 'native_analysis': {
        'profitability': {'final_balance': 123.45}}},
     'ecommercebench.profitability.final_balance', 123.45, 'incomplete'),
])
def test_actual_feedback_report_shape_preserves_native_values_and_horizon(
        tmp_path, benchmark, native, key, value, status):
    from datetime import datetime, timezone
    from servers.simulation.company_feedback import CompanyFeedback
    from servers.simulation.feedback_contracts import RecordMapping

    store = SQLiteStore(tmp_path / 'state.sqlite')
    run = _run(store)

    class NativeAdapter:
        def evaluate(self, mappings, evidence):
            assert len(mappings) == 1 and evidence == []
            return native

    pipeline = CompanyFeedback(store, {benchmark: NativeAdapter()})
    pipeline.map_record(RecordMapping(
        mapping_id='source-map', benchmark=benchmark, upstream_commit='a' * 40,
        source_record_id='native-1', source_entity_type='invoice',
        erp_doctype='Sales Invoice', erp_name='SI-1', experiment_id=run,
        company_time=datetime(2026, 10, 30, tzinfo=timezone.utc)),
        {'doctype': 'Sales Invoice', 'name': 'SI-1', 'docstatus': 1})
    report = pipeline.report(run)
    svc = _service(store, tmp_path / 'private',
                   snapshotter=lambda *_: {'company_feedback': report},
                   evaluator=lambda _run, snapshot: snapshot)
    first = svc.evaluate(run, 'controller', 'native-result').record
    metric = 'company_feedback.native_benchmark_result.' + key
    assert first.status == status
    assert first.scores[metric] == value
    assert first.scores[metric] is None if value is None else type(first.scores[metric]) is type(value)
    assert 'private supplier narrative' not in first.public_dict().__str__()
    assert not any('combined' in field for field in first.scores)
    reopened = _service(SQLiteStore(store.db_path), tmp_path / 'private')
    assert reopened.get_record(first.evaluation_id).public_dict() == first.public_dict()


def test_invalid_native_result_is_saved_failed_without_repeating_judging(tmp_path):
    store = SQLiteStore(tmp_path / 'state.sqlite')
    run = _run(store)
    calls = []

    def invalid_result(_run, _snapshot):
        calls.append('judged')
        return {'company_feedback': {'native_benchmark_result': [
            {'benchmark': 'coffeebench', 'result': {'native_evaluations': [
                {'actor_id': 'roaster_A', 'status': 'final',
                 'score_metric': 'audit.annual.true_net_income',
                 'score': 'private non-numeric provider response'}]}}]}}

    svc = _service(store, tmp_path / 'private', evaluator=invalid_result)
    record = svc.evaluate(run, 'controller', 'bad-native').record
    assert record.status == 'failed'
    assert record.scores == {}
    assert record.failure['code'] == 'native_evaluation_contract_failed'
    assert record.error_type == 'TypeError'
    assert 'private non-numeric provider response' not in record.model_dump_json()
    assert svc.evaluate(run, 'controller', 'bad-native').record == record
    assert calls == ['judged']


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), Decimal("NaN")],
                         ids=["nan", "positive-infinity", "negative-infinity", "decimal-nan"])
def test_nonfinite_native_scalar_is_durable_failed_ungraded_and_never_rejudged(tmp_path, invalid):
    store = SQLiteStore(tmp_path / "state.sqlite")
    run = _run(store)
    calls = []

    def evaluator(_run, _snapshot):
        calls.append("judged")
        return {"company_feedback": {"native_benchmark_result": [{
            "benchmark": "ecommercebench",
            "result": {"native_status": "finished", "native_analysis": {
                "profitability": {"final_balance": invalid},
            }},
        }]}}

    service = _service(store, tmp_path / "private-evidence", evaluator=evaluator)
    first = service.evaluate(run, "controller", "nonfinite-native")
    record = first.record

    assert record.status == "failed"
    assert record.scores == {}
    assert record.criterion_results == []
    assert record.error_type == "ValueError"
    assert record.failure == {"code": "native_evaluation_contract_failed",
                              "exception_type": "ValueError"}
    public_json = json.dumps(record.public_dict(), allow_nan=False)
    assert "final_balance" not in public_json
    assert "NaN" not in public_json and "Infinity" not in public_json
    artifact_path = service.records.snapshot_directory / record.result_ref
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert "final_balance" in artifact_path.read_text()

    reopened = _service(SQLiteStore(store.db_path), tmp_path / "private-evidence",
                        evaluator=lambda *_: pytest.fail("terminal replay must not rerun evaluator"))
    replay = reopened.evaluate(run, "controller", "nonfinite-native")
    assert replay.record.evaluation_id == record.evaluation_id
    assert replay.record.public_dict() == record.public_dict()
    assert calls == ["judged"]


@pytest.mark.parametrize("field", ["score", "value", "actual", "expected", "maximum", "max_score", "weight"])
def test_criterion_result_rejects_nonfinite_numeric_values(field):
    with pytest.raises(ValueError):
        CriterionResult(name="native_metric", **{field: float("inf")})


def test_finite_decimal_native_metric_retains_exact_decimal_text(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    run = _run(store)
    service = _service(
        store, tmp_path / "private-evidence",
        evaluator=lambda *_: {"company_feedback": {"native_benchmark_result": [{
            "benchmark": "ecommercebench",
            "result": {"native_status": "finished", "native_analysis": {
                "profitability": {"final_balance": Decimal("0.00")},
            }},
        }]}},
    )
    record = service.evaluate(run, "controller", "finite-decimal").record
    score_key = "company_feedback.native_benchmark_result.ecommercebench.profitability.final_balance"
    assert record.status == "completed"
    assert record.scores[score_key] == "0.00"
    json.dumps(record.public_dict(), allow_nan=False)
