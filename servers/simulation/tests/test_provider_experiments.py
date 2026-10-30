from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from servers.simulation.provider_experiments import ProviderExperimentService
from servers.simulation.provider_portal_client import ProviderPortalClientError
from servers.simulation.evaluation_service import EvaluationService
from servers.simulation.engine import SimulationEngine
from servers.simulation.store import SQLiteStore, StoreConflict
from servers.simulation.app import create_app
from servers.simulation.runtime import _provider_case_employee


class FakeProviderWorker:
    def __init__(self):
        self.prepare_requests = []

    def catalog(self):
        return {
            "schema_version": 1,
            "upstream_commit": "a" * 40,
            "tasks": [{"provider_id": "tau2-airline", "task_id": "flight-case-17",
                       "description": "Airline customer support case"}],
        }

    def prepare(self, request):
        self.prepare_requests.append(dict(request))
        return {
            "provider_id": request["provider_id"],
            "task_id": request["task_id"],
            "conversation_id": request["conversation_id"],
            "company_account_reference": request["company_account_reference"],
            "company_issue_reference": request["company_issue_reference"],
            "status": "prepared",
            "initialization_status": "applied",
            "initialization_revision": "native-revision-4",
            "subject": "Check a customer travel request",
            "business_description": "A customer asked the travel team for help.",
            "requested_outcome": "Review the request and reply with the available options.",
            "account": {"entity_type": "customer", "source_id": "cust-native-4",
                        "display_name": "Customer", "fields": {}, "records": []},
            "forms": [],
        }


class FakePortal:
    def __init__(self):
        self.registrations = []
        self.fail_once = True

    def register_case(self, registration):
        self.registrations.append(registration)
        if self.fail_once:
            self.fail_once = False
            raise ProviderPortalClientError(None, "portal_transport_uncertain")
        return {"case_id": registration["case_id"], "status": "registered"}


class FakeEvaluationWorker(FakeProviderWorker):
    def __init__(self):
        super().__init__()
        self.last_capture = None
        self.inconclusive = False

    def capture_evaluation(self, provider_id, conversation_id):
        experiment_id = conversation_id
        self.last_capture = {
            "capture_ref": "tau2-provider-evaluation:test-capture",
            "capture_sha256": "b" * 64,
            "provider_id": provider_id,
            "task_id": "flight-case-17",
            "conversation_id": conversation_id,
            "upstream_commit": "a" * 40,
            "identity_fingerprint": self.identity_fingerprint(experiment_id),
            "native_state_revision": "c" * 64,
        }
        return self.last_capture

    def identity_fingerprint(self, experiment_id):
        return hashlib.sha256(json.dumps({
            "provider_id": "tau2-airline", "task_id": "flight-case-17",
            "conversation_id": experiment_id, "company_id": "company",
            "company_actor_id": "david-wong",
            "company_account_reference": f"company-account:{experiment_id}",
            "company_issue_reference": f"company-issue:{experiment_id}",
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def evaluate_captured(self, provider_id, conversation_id, capture_ref, capture_sha256):
        if self.inconclusive:
            return {
                "provider_id": provider_id, "conversation_id": conversation_id,
                "task_id": "flight-case-17", "evaluation_mode": "company_adapted_native_tau_criteria",
                "status": "inconclusive", "native_state_revision": "c" * 64,
                "native_reward_info": None, "reason_code": "native_state_not_reconciled",
            }
        return {
            "provider_id": provider_id, "conversation_id": conversation_id,
            "task_id": "flight-case-17", "evaluation_mode": "company_adapted_native_tau_criteria",
            "status": "completed", "native_state_revision": "c" * 64,
            "native_reward_info": {
                "reward": -0.25,
                "db_check": {"db_match": False, "db_reward": 0.0},
                "reward_basis": ["DB", "ACTION", "ENV_ASSERTION", "NL_ASSERTION", "COMMUNICATE"],
                "reward_breakdown": {"DB": 0.0, "ACTION": -0.25},
                "action_checks": [{
                    "action": {"action_id": "private-action-id", "name": "private_tool",
                               "arguments": {"account_id": "PRIVATE_TOOL_ARGUMENT"}},
                    "action_match": False, "action_reward": 0.0, "tool_type": "write",
                }],
                "env_assertions": [{
                    "env_assertion": {"env_type": "assistant", "func_name": "private_check",
                                       "arguments": {"state": "PRIVATE_ENV_ARGUMENT"}},
                    "met": False, "reward": -0.5,
                }],
                "nl_assertions": [{"nl_assertion": "PRIVATE_ASSERTION_TEXT", "met": True,
                                   "justification": "PRIVATE_MODEL_JUSTIFICATION"}],
                "communicate_checks": [{"info": "PRIVATE_COMMUNICATION_TEXT", "met": False,
                                        "justification": "PRIVATE_COMMUNICATION_REASONING"}],
            },
        }

    def verify_evaluation_capture(self, provider_id, conversation_id, capture_ref, capture_sha256):
        return capture_ref == self.last_capture["capture_ref"] and capture_sha256 == self.last_capture["capture_sha256"]


def test_provider_experiment_replays_same_prepared_case_after_portal_uncertainty(tmp_path: Path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    worker, portal = FakeProviderWorker(), FakePortal()
    service = ProviderExperimentService(
        store, worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )
    request = {"provider_id": "tau2-airline", "task_id": "flight-case-17"}
    first = service.launch(request, "controller-1", "launch-idempotency-key")
    assert first["status"] == "portal_registration_uncertain"
    assert first["failure_code"] == "portal_transport_uncertain"
    assert first["experiment_id"] == first["conversation_id"]
    assert first["upstream_commit"] == "a" * 40

    second = service.launch(request, "controller-1", "launch-idempotency-key")
    assert second["status"] == "registered"
    assert second["handoff_email_status"] == "sent"
    assert second["experiment_id"] == first["experiment_id"]
    assert second["subject"] == "Check a customer travel request"
    assert second["portal_url"] == "https://portal.example/?case=" + first["experiment_id"]
    assert len(worker.prepare_requests) == 1
    assert len(portal.registrations) == 2
    assert portal.registrations[0] == portal.registrations[1]
    assert portal.registrations[0]["responsible_actor_id"] == "david-wong"
    assert portal.registrations[0]["company_account_reference"] == (
        "company-account:" + first["experiment_id"]
    )

    assert service.get(first["experiment_id"])["status"] == "registered"
    assert service.list()[0]["experiment_id"] == first["experiment_id"]
    subject = store.get_run(first["experiment_id"])
    assert subject["scenario_id"] == "TAU_PROVIDER"
    assert subject["manifest"]["source_kind"] == "company_provider"
    assert subject["manifest"]["task_id"] == "flight-case-17"

    with pytest.raises(StoreConflict):
        service.launch({"provider_id": "tau2-airline", "task_id": "another-task"},
                       "controller-1", "launch-idempotency-key")


def test_provider_experiment_does_not_register_when_native_initialization_conflicts(tmp_path: Path):
    class ConflictingWorker(FakeProviderWorker):
        def prepare(self, request):
            self.prepare_requests.append(dict(request))
            return {
                "provider_id": request["provider_id"], "task_id": request["task_id"],
                "conversation_id": request["conversation_id"],
                "company_account_reference": request["company_account_reference"],
                "company_issue_reference": request["company_issue_reference"],
                "status": "conflict", "initialization_status": "conflict",
                "initialization_revision": "native-revision-4",
                "subject": "case", "business_description": "case", "requested_outcome": "case",
                "account": None, "forms": [],
            }

    worker, portal = ConflictingWorker(), FakePortal()
    service = ProviderExperimentService(
        SQLiteStore(tmp_path / "state.sqlite"), worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )
    result = service.launch({"provider_id": "tau2-airline", "task_id": "flight-case-17"},
                            "controller-1", "conflicting-launch")
    assert result["status"] == "failed"
    assert result["failure_code"] == "native_initialization_conflict"
    assert portal.registrations == []


def test_provider_task_not_in_native_catalog_is_recorded_without_preparation(tmp_path: Path):
    worker, portal = FakeProviderWorker(), FakePortal()
    service = ProviderExperimentService(
        SQLiteStore(tmp_path / "state.sqlite"), worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )
    result = service.launch({"provider_id": "tau2-banking", "task_id": "not-in-catalog"},
                            "controller-1", "unknown-task")
    assert result["status"] == "failed"
    assert result["failure_code"] == "provider_task_not_found"
    assert worker.prepare_requests == []
    assert portal.registrations == []


def test_provider_responsible_employee_uses_exact_profile_id_and_managed_mailbox():
    profile = {
        "company": {"providerCaseResponsibleEmployeeId": "david-wong"},
        "employees": [
            {"id": "other", "email": "other@example.com"},
            {"id": "david-wong", "email": "david@example.com"},
        ],
    }
    mail = {"actors": [
        {"actor_id": "other", "email": "other@example.com"},
        {"actor_id": "david-wong", "email": "david@example.com"},
    ]}
    assert _provider_case_employee(profile, mail, {"david@example.com": {"password": "x"}}) == {
        "actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong",
    }
    with pytest.raises(ValueError):
        _provider_case_employee(profile, mail, {"other@example.com": {"password": "x"}})


def test_provider_experiment_catalog_and_detail_are_controller_only(tmp_path: Path):
    worker, portal = FakeProviderWorker(), FakePortal()
    service = ProviderExperimentService(
        SQLiteStore(tmp_path / "state.sqlite"), worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )

    class Engine:
        provider_experiment_service = service
        store = service.store.store
        store = service.store.store

    client = TestClient(create_app(Engine(), {
        "controller-token": {"actor_id": "controller", "role": "controller"},
        "agent-token": {"actor_id": "agent", "role": "agent"},
    }))
    catalog = client.get("/company/provider-experiments/catalog",
                         headers={"Authorization": "Bearer controller-token"})
    assert catalog.status_code == 200
    assert catalog.json()["tasks"][0]["task_id"] == "flight-case-17"

    launch = client.post("/company/provider-experiments", json={
        "provider_id": "tau2-airline", "task_id": "flight-case-17",
    }, headers={"Authorization": "Bearer controller-token", "Idempotency-Key": "api-launch"})
    assert launch.status_code == 200
    experiment_id = launch.json()["experiment_id"]
    detail = client.get(f"/company/provider-experiments/{experiment_id}",
                        headers={"Authorization": "Bearer controller-token"})
    assert detail.status_code == 200
    assert detail.json()["experiment"]["experiment_id"] == experiment_id
    hidden = client.get(f"/runs/{experiment_id}/evaluations",
                        headers={"Authorization": "Bearer controller-token"})
    assert hidden.status_code == 404
    hidden = client.get(f"/runs/{experiment_id}/evaluations",
                        headers={"Authorization": "Bearer controller-token"})
    assert hidden.status_code == 404

    denied = client.get("/company/provider-experiments/catalog",
                        headers={"Authorization": "Bearer agent-token"})
    assert denied.status_code == 403


def test_provider_evaluation_uses_durable_native_snapshot_and_shared_evaluation_ledger(tmp_path: Path):
    sqlite = SQLiteStore(tmp_path / "state.sqlite")
    worker, portal = FakeEvaluationWorker(), FakePortal()
    portal.fail_once = False
    service = ProviderExperimentService(
        sqlite, worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )
    experiment = service.launch({"provider_id": "tau2-airline", "task_id": "flight-case-17"},
                                "controller-1", "evaluation-launch")
    engine = SimulationEngine.__new__(SimulationEngine)
    engine.store = sqlite
    engine.provider_experiment_store = service.store
    engine.provider_experiment_worker = worker
    evidence_dir = tmp_path / "evaluation-evidence"
    evaluation = EvaluationService(
        sqlite, evidence_dir,
        snapshotter=engine.capture_evaluation_inputs,
        evaluator=engine.evaluate_captured,
        version_verifier=engine.verify_evaluation_inputs,
        identity_resolver=engine.evaluation_identity,
        guard_factory=lambda _run_id: nullcontext(),
    )
    class EvaluationEngine:
        provider_experiment_service = service
        store = sqlite
        evaluation_service = evaluation

    client = TestClient(create_app(EvaluationEngine(), {
        "controller-token": {"actor_id": "controller-1", "role": "controller"},
        "agent-token": {"actor_id": "agent-1", "role": "agent"},
    }))
    headers = {"Authorization": "Bearer controller-token", "Idempotency-Key": "evaluation-key"}
    started = client.post(f"/company/provider-experiments/{experiment['experiment_id']}/evaluations",
                          headers=headers)
    assert started.status_code == 200
    public = started.json()["evaluation"]
    second = evaluation.evaluate(experiment["experiment_id"], "controller-1", "evaluation-key")
    evaluations = client.get(f"/company/provider-experiments/{experiment['experiment_id']}/evaluations",
                             headers={"Authorization": "Bearer controller-token"})
    latest = client.get(f"/company/provider-experiments/{experiment['experiment_id']}/evaluations/latest",
                        headers={"Authorization": "Bearer controller-token"})
    retrieved = client.get(
        f"/company/provider-experiments/{experiment['experiment_id']}/evaluations/{public['evaluation_id']}",
        headers={"Authorization": "Bearer controller-token"},
    )
    assert evaluations.status_code == latest.status_code == retrieved.status_code == 200
    assert len(evaluations.json()["evaluations"]) == 1
    assert latest.json()["evaluation"]["evaluation_id"] == public["evaluation_id"]
    assert retrieved.json()["evaluation"]["evaluation_id"] == public["evaluation_id"]
    denied = client.get(f"/company/provider-experiments/{experiment['experiment_id']}/evaluations",
                        headers={"Authorization": "Bearer agent-token"})
    assert denied.status_code == 403
    assert public["status"] == "completed"
    assert public["benchmark"] == "tau2-bench"
    assert public["benchmark_version"] == "a" * 40
    assert public["evaluation_mode"] == "company_adapted"
    assert public["scores"]["problem_evaluation.evaluation.reward"] == -0.25
    source_pin = "a" * 40
    assert public["scores"][f"tau2.{source_pin}.db_check.db_match"] is False
    assert public["scores"][f"tau2.{source_pin}.db_check.db_reward"] == 0.0
    assert public["scores"][f"tau2.{source_pin}.reward_breakdown.ACTION"] == -0.25
    assert public["native_scale"][f"tau2.{source_pin}.reward_breakdown.ACTION"] == "native_tau_reward"
    assert public["scores"][f"tau2.{source_pin}.action_checks.0.action_match"] is False
    assert public["scores"][f"tau2.{source_pin}.action_checks.0.action_reward"] == 0.0
    assert public["scores"][f"tau2.{source_pin}.env_assertions.0.reward"] == -0.5
    assert public["scores"][f"tau2.{source_pin}.nl_assertions.0.met"] is True
    assert len(public["criterion_results"]) >= 6
    assert {"provider_assistant_scored", "not_viv_performance_score"}.issubset(public["warnings"])
    public_json = json.dumps(public)
    for private in ("PRIVATE_TOOL_ARGUMENT", "PRIVATE_ENV_ARGUMENT", "PRIVATE_ASSERTION_TEXT",
                    "PRIVATE_MODEL_JUSTIFICATION", "PRIVATE_COMMUNICATION_TEXT",
                    "PRIVATE_COMMUNICATION_REASONING", "private-action-id"):
        assert private not in public_json
    assert public["evaluation_id"] == second.record.evaluation_id
    assert public["evidence_hash"]
    assert "native_provider_evaluation" not in public["scores"]
    assert "result_ref" not in public and "idempotency_key_hash" not in public

    worker.inconclusive = True
    uncertain_experiment = service.launch(
        {"provider_id": "tau2-airline", "task_id": "flight-case-17"},
        "controller-1", "evaluation-inconclusive",
    )
    uncertain_response = client.post(
        f"/company/provider-experiments/{uncertain_experiment['experiment_id']}/evaluations",
        headers={"Authorization": "Bearer controller-token", "Idempotency-Key": "evaluation-inconclusive"},
    )
    assert uncertain_response.status_code == 200
    uncertain_public = uncertain_response.json()["evaluation"]
    assert uncertain_public["status"] == "incomplete"
    assert not any(type(value) in (int, float) for value in uncertain_public["scores"].values())
    assert uncertain_public["mapping_gaps"] == ["native_state_not_reconciled"]


def test_company_dashboard_includes_provider_case_and_delivered_correspondence_safely(tmp_path: Path):
    sqlite = SQLiteStore(tmp_path / "company.sqlite")
    worker, portal = FakeProviderWorker(), FakePortal()
    portal.fail_once = False
    service = ProviderExperimentService(
        sqlite, worker, portal,
        {"actor_id": "david-wong", "email": "david@example.com", "employee_id": "david-wong"},
        portal_url="https://portal.example/",
    )
    case = service.launch({"provider_id": "tau2-airline", "task_id": "flight-case-17"},
                          "controller-1", "dashboard-case")
    portal_store = service.portal_store
    portal_store.register_case(portal.registrations[-1])
    portal_store.claim_notification(case["experiment_id"], "<handoff@example.test>",
                                    "Travel request", "2026-10-30T10:00:00+00:00")
    portal_store.notification_submission_started(case["experiment_id"], "2026-10-30T10:00:01+00:00")
    portal_store.complete_notification(case["experiment_id"],
        {"message_id": "<handoff@example.test>", "delivered_at": "2026-10-30T10:00:02+00:00"},
        "2026-10-30T10:00:02+00:00")
    portal_store.claim_message(case["experiment_id"], "mail-client-1", "native-op-1",
                               "<reply@example.test>", "Please review my itinerary.",
                               "2026-10-30T10:05:00+00:00")
    portal_store.record_inbound_received(case["experiment_id"], "mail-client-1", {
        "mailbox_uid": 55, "received_at": "2026-10-30T10:05:01+00:00",
    })
    portal_store.complete_provider(case["experiment_id"], "mail-client-1", {
        "body": "The available itinerary has been reviewed.",
        "reply_message_id": "<response@example.test>",
        "native_response_private": "PRIVATE_NATIVE_STATE_MARKER",
    })
    portal_store.record_reply_receipt(case["experiment_id"], "mail-client-1", {
        "message_id": "<response@example.test>", "delivered_at": "2026-10-30T10:06:00+00:00",
    })

    class Engine:
        provider_experiment_service = service
        store = sqlite

        @staticmethod
        def company_state():
            return {"status": "running", "simulated_time": "2026-10-30T09:00:00+00:00",
                    "paused": False, "erp_binding": {"site_name": "erp-sim-test.localhost"}}

    client = TestClient(create_app(Engine(), {
        "controller-token": {"actor_id": "controller-1", "role": "controller"},
    }))
    payload = client.get("/company", headers={"Authorization": "Bearer controller-token"}).json()
    experiment = next(item for item in payload["experiments"]
                      if item["experiment_id"] == case["experiment_id"])
    assert experiment["kind"] == "provider_case"
    assert experiment["status"] == "running"
    assert experiment["simulated_time"] is None
    assert payload["simulated_time"] == "2026-10-30T09:00:00+00:00"
    emails = [item for item in payload["activity"]
              if item.get("experiment_id") == case["experiment_id"] and item.get("type") == "email"]
    assert [item["event"] for item in emails] == [
        "email_delivered", "email_received", "email_delivered",
    ]
    assert emails[0]["received_at"] == "2026-10-30T10:00:02+00:00"
    assert emails[1]["timestamp"] == "2026-10-30T10:05:01+00:00"
    assert emails[1]["content"] == "Please review my itinerary."
    detail = service.get(case["experiment_id"])
    detail_mail = [item for item in detail["activity"] if item["status"].startswith("email_")]
    assert [item["status"] for item in detail_mail] == [
        "email_delivered", "email_received", "email_delivered",
    ]
    assert detail_mail[1]["received_at"] == "2026-10-30T10:05:01+00:00"
    assert "PRIVATE_NATIVE_STATE_MARKER" not in json.dumps(detail)
    assert "PRIVATE_NATIVE_STATE_MARKER" not in json.dumps(payload)
    assert "native_response_json" not in json.dumps(payload)
    assert client.get(f"/runs/{case['experiment_id']}",
                      headers={"Authorization": "Bearer controller-token"}).status_code == 404
