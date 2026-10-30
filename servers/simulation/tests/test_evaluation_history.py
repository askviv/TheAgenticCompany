"""Durable evaluation invariants using synthetic evidence; no benchmark actors."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import threading
from uuid import uuid4

import pytest

from servers.simulation.evaluation_service import EvaluationService
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore


def service(store, root, state, calls, lock=None):
    @contextmanager
    def guard(_run):
        with lock or threading.RLock():
            yield

    def evaluate(_run, snapshot):
        calls.append(snapshot['value'])
        return {'checks': {'invoice_settled': snapshot['value'] == 7},
                'problem_evaluation': {'evaluation': {
                    'reward_status': 'complete', 'reward': snapshot['value'] / 10,
                    'components': {'DB': {'status': 'evaluated', 'reward': snapshot['value'] / 10}}}}}

    return EvaluationService(
        store, root, snapshotter=lambda _run, generation: {'value': state[0], 'generation': generation},
        evaluator=evaluate, version_verifier=lambda *_: True,
        identity_resolver=lambda *_: {'benchmark': 'tau2-bench', 'domain': 'retail',
                                     'benchmark_version': 'pinned-fixture', 'evaluation_mode': 'company_adapted'},
        guard_factory=guard)


def create_run(store):
    run = str(uuid4())
    store.create_run(run, StartScenario(scenario_id='A1', seed=23))
    return run


def test_saved_history_survives_reopen_and_changed_evidence_is_separate(tmp_path):
    path = tmp_path / 'coordinator.sqlite'
    store = SQLiteStore(path)
    run = create_run(store)
    state, calls = [3], []
    first = service(store, tmp_path / 'evidence', state, calls).evaluate(run, 'controller', 'first').record
    state[0] = 7
    reopened = service(SQLiteStore(path), tmp_path / 'evidence', state, calls)
    assert reopened.evaluate(run, 'controller', 'first').record == first
    second = reopened.evaluate(run, 'controller', 'second').record
    assert second.evaluation_id != first.evaluation_id
    assert second.snapshot_sha256 != first.snapshot_sha256
    assert first.scores['problem_evaluation.evaluation.reward'] == 0.3
    assert second.scores['problem_evaluation.evaluation.reward'] == 0.7
    assert calls == [3, 7]
    history = reopened.list(run)
    assert [r.evaluation_id for r in history] == [first.evaluation_id, second.evaluation_id]
    assert reopened.latest(run) == second
    assert reopened.list(create_run(store)) == []


def test_concurrent_idempotent_requests_judge_once_and_scores_cannot_be_mutated(tmp_path):
    store = SQLiteStore(tmp_path / 'coordinator.sqlite')
    run = create_run(store)
    calls = []
    svc = service(store, tmp_path / 'evidence', [7], calls, threading.RLock())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: svc.evaluate(run, 'controller', 'same').record, range(2)))
    assert results[0] == results[1]
    assert calls == [7]
    assert len(svc.list(run)) == 1
    for table, operation in [('evaluation_outcomes', 'UPDATE evaluation_outcomes SET scores_json=\'{}\''),
                             ('evaluation_outcomes', 'DELETE FROM evaluation_outcomes'),
                             ('evaluation_reservations', 'DELETE FROM evaluation_reservations'),
                             ('evaluation_lifecycle_events', 'DELETE FROM evaluation_lifecycle_events')]:
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction() as db:
                db.execute(operation)
    assert svc.records.get_record(results[0].evaluation_id) == results[0]


def test_restart_after_judge_started_never_repeats_uncertain_judging(tmp_path):
    path = tmp_path / 'coordinator.sqlite'
    store = SQLiteStore(path)
    run = create_run(store)
    calls = []
    svc = service(store, tmp_path / 'evidence', [7], calls)
    reserved, _ = svc.records.reserve(run, 'controller', 'interrupted')
    ref, digest = svc.records.write_artifact(reserved.evaluation_id, 'snapshot', {'value': 7, 'generation': 0})
    svc.records.append_event(reserved.evaluation_id, 'snapshot_captured', {
        'snapshot_ref': ref, 'snapshot_sha256': digest,
        'identity': {'benchmark': 'tau2-bench', 'domain': 'retail', 'benchmark_version': 'pinned-fixture',
                     'evaluation_mode': 'company_adapted'}})
    svc.records.append_event(reserved.evaluation_id, 'evaluator_started')
    reopened = service(SQLiteStore(path), tmp_path / 'evidence', [7], calls)
    assert reopened.latest(run).lifecycle == 'evaluator_started'
    result = reopened.evaluate(run, 'controller', 'interrupted').record
    assert result.status == 'incomplete'
    assert result.lifecycle == 'uncertain'
    assert result.scores == {}
    assert calls == []
    assert reopened.evaluate(run, 'controller', 'interrupted').record == result


def test_history_keeps_prior_generation_but_latest_is_current_and_viv_cannot_read(tmp_path):
    from fastapi.testclient import TestClient
    from servers.simulation.app import create_app

    store = SQLiteStore(tmp_path / 'state.sqlite')
    run = create_run(store)
    svc = service(store, tmp_path / 'evidence', [7], [])
    saved = svc.evaluate(run, 'controller', 'old-key').record
    store.reset_run(run, 0, {'run': {'status': 'running', 'manifest': {}, 'erp_binding': {}}},
                    idempotency_scope='fixture', idempotency_key='reset')
    engine = type('FixtureEngine', (), {'store': store, 'evaluation_service': svc})()
    client = TestClient(create_app(engine, {
        'controller-token': {'actor_id': 'controller', 'role': 'controller'},
        'viv-token': {'actor_id': 'viv', 'role': 'agent'}}))
    controller = {'Authorization': 'Bearer controller-token'}
    path = f'/runs/{run}/evaluations'
    assert client.get(path, headers=controller).json()['evaluations'][0]['evaluation_id'] == saved.evaluation_id
    assert client.get(path + '/latest', headers=controller).json() == {'evaluation': None}
    assert client.get(path + '?generation=0', headers=controller).json()['evaluations'][0]['generation'] == 0
    assert client.post(path + '?generation=0', headers={**controller, 'Idempotency-Key': 'new-key'}).status_code == 409
    assert client.post(path, headers={**controller, 'Idempotency-Key': 'old-key'}).json()['evaluation']['generation'] == 0
    for endpoint in [path, path + '/latest', f'/evaluations/{saved.evaluation_id}']:
        assert client.get(endpoint, headers={'Authorization': 'Bearer viv-token'}).status_code == 403
        assert client.get(endpoint).status_code == 401


def test_public_saved_result_does_not_depend_on_private_evidence_files(tmp_path):
    store = SQLiteStore(tmp_path / 'state.sqlite')
    run = create_run(store)
    svc = service(store, tmp_path / 'evidence', [7], [])
    result = svc.evaluate(run, 'controller', 'saved').record
    # Evidence is stored separately: a report can still show the saved outcome
    # when private evidence storage is temporarily offline.
    (tmp_path / 'evidence' / result.result_ref).unlink()
    (tmp_path / 'evidence' / result.snapshot_ref).unlink()
    assert svc.records.get_record(result.evaluation_id).public_dict() == result.public_dict()
    assert svc.latest(run).public_dict() == result.public_dict()
