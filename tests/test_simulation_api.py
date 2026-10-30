from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from servers.simulation.app import Principal, create_app
from servers.simulation.erpnext import ERPNextError
from servers.simulation.engine import MailDeliveryUncertain
from servers.simulation.models import CanonicalEvent, ERPAction, StartScenario
from servers.simulation.store import SQLiteStore


TOKENS = {
    "controller-secret": {"actor_id": "sim-controller", "role": "controller"},
    "evaluator-secret": {"actor_id": "private-evaluator", "role": "evaluator"},
    "manager-secret": {"actor_id": "manager-42", "role": "approver"},
    "agent-a-secret": {"actor_id": "agent-a", "role": "agent"},
    "agent-b-secret": {"actor_id": "agent-b", "role": "agent"},
}


class StoreBackedEngine:
    """Small engine boundary backed by the real durable store for auth tests."""

    def __init__(self, store):
        self.store = store
        self.erp_reads = []
        self.erp_actions = []
        self.approvals = []

    def list_pending(self, run_id):
        run = self.store.get_run(run_id)
        return self.store.pending_events(run_id, run["generation"])

    def inbox(self, run_id, actor_id):
        run = self.store.get_run(run_id)
        private = self.store.get_actor(run_id, run["generation"], actor_id)["private_state"]
        return {"messages": private.get("messages", [])}

    def case(self, run_id, case_id):
        run = self.store.get_run(run_id)
        return self.store.get_case(run_id, run["generation"], case_id)["visible"]

    def erp_read(self, run_id, operation, entity_id, actor_id):
        self.erp_reads.append((str(run_id), operation, entity_id, actor_id))
        return {"run_id": str(run_id), "operation": operation, "entity_id": entity_id}

    def erp_action(self, model, operation, key, actor_id):
        self.erp_actions.append((model.model_dump(mode="json"), operation, key, actor_id))
        return {"operation": operation, "entity_id": model.entity_id}

    def approve(self, model, key, actor_id):
        self.approvals.append((model.model_dump(mode="json"), key, actor_id))
        return {"decision": model.decision, "approved_by": actor_id}

    def evaluate(self, run_id):
        return {"run_id": str(run_id), "score": 1}

    def record_human_intervention(self, run_id, model, key, actor_id):
        self.store.append_audit(run_id, 0, {"event": "human_intervention_recorded", "actor_id": actor_id,
            "intervention_type": model.intervention_type, "category": model.category})
        return {"recorded": True, "idempotency_key": key}

    def record_model_usage(self, run_id, model, key, actor_id):
        self.store.append_audit(run_id, 0, {"event": "model_usage_recorded", "actor_id": actor_id,
            **model.model_dump(mode="json", exclude_none=True)})
        return {"recorded": True, "idempotency_key": key}

    def trajectory(self, run_id):
        run = self.store.get_run(run_id)
        return {"run": {"run_id": str(run_id), "generation": run["generation"]},
                "events": self.store.list_events(run_id, run["generation"]),
                "audit": self.store.list_audit(run_id, run["generation"]),
                "outbox": self.store.list_outbox(run_id, run["generation"])}


def setup_api(tmp_path):
    store = SQLiteStore(tmp_path / "api.sqlite")
    run_a, run_b = uuid4(), uuid4()
    store.create_run(run_a, StartScenario(scenario_id="C1", agent_id="agent-a"))
    store.create_run(run_b, StartScenario(scenario_id="C1", agent_id="agent-b"))
    for run_id, agent_id, supplier_id, qty in (
        (run_a, "agent-a", "supplier-a", 10_000),
        (run_b, "agent-b", "supplier-b", 9_000),
    ):
        store.put_case(run_id, 0, "case-1", {"status": "open", "purchase_order_id": "PO-1"})
        store.put_actor(
            run_id, 0, agent_id, kind="agent", email=None,
            private_state={"messages": [{"body": "visible thread message"}]},
        )
        store.put_actor(
            run_id, 0, supplier_id, kind="supplier", email="supplier@example.test",
            private_state={"reservation_price": qty},
        )
        store.enqueue_event(CanonicalEvent(
            event_id=f"shipment-{supplier_id}", run_id=run_id, scenario_id="C1",
            simulated_time=datetime(2026, 10, 29, tzinfo=timezone.utc),
            event_type="SHIPMENT_DELAYED", source="scenario", entity_type="shipment",
            entity_id=f"shipment-{supplier_id}", payload={"hidden_delay_days": qty},
            visibility=["controller"],
        ), 0)
    engine = StoreBackedEngine(store)
    app = create_app(engine, TOKENS)
    return TestClient(app), engine, run_a, run_b


def headers(token, *, idempotency_key=None):
    result = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        result["Idempotency-Key"] = idempotency_key
    return result


def test_agent_reads_only_owned_visible_case_and_own_inbox(tmp_path):
    client, _engine, run_a, run_b = setup_api(tmp_path)

    own_case = client.get(f"/cases/case-1?run_id={run_a}", headers=headers("agent-a-secret"))
    own_inbox = client.get(f"/agents/agent-a/inbox?run_id={run_a}", headers=headers("agent-a-secret"))
    other_case = client.get(f"/cases/case-1?run_id={run_b}", headers=headers("agent-a-secret"))
    other_inbox = client.get(f"/agents/agent-b/inbox?run_id={run_b}", headers=headers("agent-a-secret"))

    assert own_case.status_code == 200
    assert own_case.json() == {"status": "open", "purchase_order_id": "PO-1"}
    assert own_inbox.status_code == 200
    assert own_inbox.json() == {"messages": [{"body": "visible thread message"}]}
    assert "reservation_price" not in own_inbox.text
    assert other_case.status_code == 404 and other_case.json()["detail"] == "Run not found"
    assert other_inbox.status_code == 404 and "not found" in other_inbox.json()["detail"].casefold()


def test_agent_cannot_read_hidden_events_or_control_time_reset_or_evaluation(tmp_path):
    client, _engine, run_a, _run_b = setup_api(tmp_path)
    event = {
        "event_id": "agent-injected", "run_id": str(run_a), "scenario_id": "C1",
        "simulated_time": "2026-10-30T00:00:00Z", "event_type": "SHIPMENT_DELAYED",
        "source": "agent", "entity_type": "shipment", "entity_id": "s-1",
        "payload": {}, "visibility": [],
    }
    actions = [
        client.get(f"/events?run_id={run_a}", headers=headers("agent-a-secret")),
        client.post("/events", json=event, headers=headers("agent-a-secret", idempotency_key="evt-1")),
        client.post("/clock/advance", json={"run_id": str(run_a), "until": "2026-10-30T00:00:00Z"}, headers=headers("agent-a-secret", idempotency_key="clk-1")),
        client.post("/clock/pause", json={"run_id": str(run_a), "paused": True}, headers=headers("agent-a-secret", idempotency_key="pause-1")),
        client.post(f"/scenarios/{run_a}/reset", headers=headers("agent-a-secret", idempotency_key="reset-1")),
        client.post(f"/scenarios/{run_a}/evaluate", headers=headers("agent-a-secret")),
        client.post(f"/mail/pump?run_id={run_a}", headers=headers("agent-a-secret", idempotency_key="pump-1")),
    ]
    assert [response.status_code for response in actions] == [403] * len(actions)
    assert all("hidden_delay_days" not in response.text for response in actions)


def test_controller_can_list_private_events_but_agent_erp_reads_are_run_scoped_and_allowlisted(tmp_path):
    client, engine, run_a, run_b = setup_api(tmp_path)

    denied_events = client.get(f"/events?run_id={run_a}", headers=headers("agent-a-secret"))
    allowed_events = client.get(f"/events?run_id={run_a}", headers=headers("controller-secret"))
    allowed_erp = client.get(
        f"/runs/{run_a}/erp/get_purchase_order/PO-1", headers=headers("agent-a-secret")
    )
    cross_run_erp = client.get(
        f"/runs/{run_b}/erp/get_purchase_order/PO-1", headers=headers("agent-a-secret")
    )
    unknown_erp = client.get(
        f"/runs/{run_a}/erp/submit_payment/PI-1", headers=headers("agent-a-secret")
    )

    assert denied_events.status_code == 403
    assert allowed_events.status_code == 200
    assert allowed_events.json()[0]["entity_id"] == "shipment-supplier-a"
    assert allowed_events.json()[0]["related_entities"] == []
    assert allowed_erp.status_code == 200
    assert cross_run_erp.status_code == 404
    assert unknown_erp.status_code == 404
    assert len(engine.erp_reads) == 1
    assert engine.erp_reads[0][0] == str(run_a)


def test_invoice_assessment_is_not_agent_erp_read_even_when_run_is_owned(tmp_path):
    client, engine, run_a, _run_b = setup_api(tmp_path)

    agent = client.get(
        f"/runs/{run_a}/erp/assess_invoice/PI-1", headers=headers("agent-a-secret")
    )
    evaluator = client.get(
        f"/runs/{run_a}/erp/assess_invoice/PI-1", headers=headers("evaluator-secret")
    )

    assert agent.status_code == 403
    assert evaluator.status_code == 200
    assert engine.erp_reads == [(str(run_a), "assess_invoice", "PI-1", "private-evaluator")]


def test_agent_stock_balance_read_is_allowlisted_and_run_scoped(tmp_path):
    client, engine, run_a, run_b = setup_api(tmp_path)

    own = client.get(
        f"/runs/{run_a}/erp/get_stock_balance/ITEM-1", headers=headers("agent-a-secret")
    )
    other_run = client.get(
        f"/runs/{run_b}/erp/get_stock_balance/ITEM-1", headers=headers("agent-a-secret")
    )

    assert own.status_code == 200
    assert own.json()["operation"] == "get_stock_balance"
    assert other_run.status_code == 404
    assert engine.erp_reads == [(str(run_a), "get_stock_balance", "ITEM-1", "agent-a")]


def test_goods_receipt_action_route_requires_owned_run_and_matching_body_run(tmp_path):
    client, engine, run_a, run_b = setup_api(tmp_path)
    action = {
        "run_id": str(run_a), "entity_id": "PO-1", "approval_id": "grant-1",
        "parameters": {"case_id": "case-1", "quantity": "8000"},
    }
    accepted = client.post(
        f"/runs/{run_a}/erp/actions/create_goods_receipt", json=action,
        headers=headers("agent-a-secret", idempotency_key="goods-receipt-1"),
    )
    cross_run = client.post(
        f"/runs/{run_b}/erp/actions/create_goods_receipt", json=action,
        headers=headers("agent-a-secret", idempotency_key="goods-receipt-2"),
    )
    mismatch = client.post(
        f"/runs/{run_b}/erp/actions/create_goods_receipt", json=action,
        headers=headers("controller-secret", idempotency_key="goods-receipt-3"),
    )
    unsupported = client.post(
        f"/runs/{run_a}/erp/actions/post_payment", json=action,
        headers=headers("agent-a-secret", idempotency_key="goods-receipt-4"),
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"operation": "create_goods_receipt", "entity_id": "PO-1"}
    assert cross_run.status_code == 404
    assert mismatch.status_code == 409
    assert unsupported.status_code == 404
    assert engine.erp_actions == [
        (action, "create_goods_receipt", "goods-receipt-1", "agent-a")
    ]


@pytest.mark.parametrize("parameters", [
    {"quantity": "8000"},
    {"case_id": "case-private-probe"},
    {"case_id": "case-private-probe", "quantity": "0"},
])
def test_goods_receipt_invalid_typed_parameters_return_redacted_422_without_engine_effects(
    tmp_path, parameters,
):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    body = {
        "run_id": str(run_a), "entity_id": "PO-1", "approval_id": "grant-1",
        "parameters": parameters,
    }

    response = client.post(
        f"/runs/{run_a}/erp/actions/create_goods_receipt", json=body,
        headers=headers("agent-a-secret", idempotency_key="invalid-receipt-action"),
    )

    assert response.status_code == 422
    assert "case-private-probe" not in response.text
    assert '"input"' not in response.text
    assert engine.erp_actions == []


def test_shipping_commit_action_route_is_run_scoped_and_uses_typed_union_parameters(tmp_path):
    client, engine, run_a, run_b = setup_api(tmp_path)
    action = {
        "run_id": str(run_a), "entity_id": "SHIP-001", "approval_id": "grant-shipping-1",
        "parameters": {"case_id": "case-1", "quote_message_id": "supplier-quote-1"},
    }

    accepted = client.post(
        f"/runs/{run_a}/erp/actions/commit_expedited_shipping", json=action,
        headers=headers("agent-a-secret", idempotency_key="shipping-commit-1"),
    )
    cross_run = client.post(
        f"/runs/{run_b}/erp/actions/commit_expedited_shipping", json=action,
        headers=headers("agent-a-secret", idempotency_key="shipping-commit-2"),
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"operation": "commit_expedited_shipping", "entity_id": "SHIP-001"}
    assert cross_run.status_code == 404
    assert engine.erp_actions == [(action, "commit_expedited_shipping", "shipping-commit-1", "agent-a")]


@pytest.mark.parametrize("parameters", [
    {"quote_message_id": "private-quote-probe"},
    {"case_id": "case-1"},
])
def test_shipping_commit_missing_typed_parameters_return_redacted_422_without_engine_effects(
    tmp_path, parameters,
):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    action = {
        "run_id": str(run_a), "entity_id": "SHIP-001", "approval_id": "grant-shipping-1",
        "parameters": parameters,
    }

    response = client.post(
        f"/runs/{run_a}/erp/actions/commit_expedited_shipping", json=action,
        headers=headers("agent-a-secret", idempotency_key="invalid-shipping-commit"),
    )

    assert response.status_code == 422
    assert "private-quote-probe" not in response.text
    assert '"input"' not in response.text
    assert engine.erp_actions == []


@pytest.mark.parametrize(("operation", "parameters"), [
    ("create_goods_receipt", {"case_id": "case-private", "quote_message_id": "quote-private"}),
    ("commit_expedited_shipping", {"case_id": "case-private", "quantity": "2000"}),
])
def test_erp_action_union_variant_must_match_operation_before_engine_call(
    tmp_path, operation, parameters,
):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    entity_id = "PO-1" if operation == "create_goods_receipt" else "SHIP-001"
    action = {
        "run_id": str(run_a), "entity_id": entity_id, "approval_id": "grant-1",
        "parameters": parameters,
    }

    response = client.post(
        f"/runs/{run_a}/erp/actions/{operation}", json=action,
        headers=headers("agent-a-secret", idempotency_key="wrong-action-parameter-variant"),
    )

    assert response.status_code == 422
    assert "case-private" not in response.text
    assert "quote-private" not in response.text
    assert '"input"' not in response.text
    assert engine.erp_actions == []


def test_approver_can_only_submit_grant_with_configured_manager_identity(tmp_path):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    approval = {
        "run_id": str(run_a), "case_id": "case-1", "operation": "amend_purchase_order",
        "entity_id": "PO-1", "entity_version": "v1", "decision": "approved",
        "amount": "50.00", "currency": "GBP",
    }
    accepted = client.post("/approvals", json=approval, headers=headers("manager-secret", idempotency_key="approval-1"))
    agent_denied = client.post("/approvals", json=approval, headers=headers("agent-a-secret", idempotency_key="approval-2"))
    controller_allowed = client.post("/approvals", json=approval, headers=headers("controller-secret", idempotency_key="approval-3"))
    pump_denied = client.post(f"/mail/pump?run_id={run_a}", headers=headers("manager-secret", idempotency_key="pump-2"))

    assert accepted.status_code == 200
    assert accepted.json()["approved_by"] == "manager-42"
    assert engine.approvals[0][2] == "manager-42"
    assert agent_denied.status_code == 403
    assert controller_allowed.status_code == 200
    assert pump_denied.status_code == 403


def test_missing_mutation_key_auth_failures_and_validation_errors_are_safe(tmp_path):
    client, _engine, run_a, _run_b = setup_api(tmp_path)
    no_auth = client.get(f"/cases/case-1?run_id={run_a}")
    unknown = client.get(f"/cases/case-1?run_id={run_a}", headers=headers("not-configured"))
    missing_key = client.post(f"/scenarios/{run_a}/reset", headers=headers("controller-secret"))
    invalid_body = client.post(
        "/scenarios/start", json={"scenario_id": "C1", "seed": -100, "agent_configuration": {"secret": "never-echo"}},
        headers=headers("controller-secret", idempotency_key="start-invalid"),
    )

    assert no_auth.status_code == 401
    assert unknown.status_code == 401
    assert missing_key.status_code == 400
    assert invalid_body.status_code == 422
    assert "never-echo" not in invalid_body.text
    assert "input" not in invalid_body.text


def test_non_ascii_bearer_token_is_rejected_without_internal_error_or_echo(tmp_path):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    response = client.get(
        f"/runs/{run_a}/erp/get_purchase_order/PO-1",
        headers=[(b"authorization", b"Bearer caf\xe9-unconfigured")],
    )

    assert response.status_code == 401
    assert "Invalid bearer credentials" == response.json()["detail"]
    assert engine.erp_reads == []


def test_metrics_and_trajectory_are_privileged_and_inputs_are_safe(tmp_path):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    payload = {"intervention_type": "correction", "category": "policy", "effort_seconds": 45}
    agent_intervention = client.post(f"/scenarios/{run_a}/human-interventions", json=payload,
        headers=headers("agent-a-secret", idempotency_key="human-1"))
    accepted_intervention = client.post(f"/scenarios/{run_a}/human-interventions", json=payload,
        headers=headers("controller-secret", idempotency_key="human-1"))
    usage = {"provider": "provider", "model_id": "model-v3", "model_version": "3.1",
             "input_tokens": 25, "output_tokens": 7}
    accepted_usage = client.post(f"/scenarios/{run_a}/model-usage", json=usage,
        headers=headers("controller-secret", idempotency_key="usage-1"))
    invalid_cost = client.post(f"/scenarios/{run_a}/model-usage",
        json={"provider": "provider", "model_id": "model-v3", "cost": "1.23"},
        headers=headers("controller-secret", idempotency_key="usage-invalid"))
    raw_prompt = client.post("/scenarios/start", json={"scenario_id": "C1", "seed": 1,
        "agent_metadata": {"prompt": "private raw prompt"}},
        headers=headers("controller-secret", idempotency_key="start-raw-prompt"))
    denied_trajectory = client.get(f"/scenarios/{run_a}/trajectory", headers=headers("agent-a-secret"))
    controller_trajectory = client.get(f"/scenarios/{run_a}/trajectory", headers=headers("controller-secret"))
    evaluator_trajectory = client.get(f"/scenarios/{run_a}/trajectory", headers=headers("evaluator-secret"))

    assert agent_intervention.status_code == 403
    assert accepted_intervention.status_code == 200
    assert accepted_usage.status_code == 200
    assert invalid_cost.status_code == 422
    assert "1.23" not in invalid_cost.text
    assert raw_prompt.status_code == 422
    assert "private raw prompt" not in raw_prompt.text
    assert denied_trajectory.status_code == 403
    assert controller_trajectory.status_code == 403
    assert evaluator_trajectory.status_code == 200
    assert [row["private_record"]["event"] for row in evaluator_trajectory.json()["audit"]].count(
        "human_intervention_recorded") == 1
    assert [row["private_record"]["event"] for row in evaluator_trajectory.json()["audit"]].count(
        "model_usage_recorded") == 1


def test_provider_error_body_is_not_returned_or_logged_and_has_correlation_id(tmp_path, caplog):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    secret = "provider response with raw credential"

    def fail(*_args):
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            raise ERPNextError("read Purchase Order", 502) from error

    engine.erp_read = fail
    with TestClient(client.app, raise_server_exceptions=False) as safe_client:
        response = safe_client.get(
            f"/runs/{run_a}/erp/get_purchase_order/PO-1", headers=headers("agent-a-secret")
        )
    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in caplog.text
    assert response.headers.get("X-Correlation-ID")
    assert response.json()["correlation_id"] == response.headers["X-Correlation-ID"]
    assert "servers/simulation/app.py:" in caplog.text


def test_uncertain_mail_delivery_has_pending_safe_response_and_operator_diagnostics(tmp_path, caplog):
    client, engine, run_a, _run_b = setup_api(tmp_path)
    secret = "provider body must stay private"

    def uncertain(*_args):
        try:
            raise TimeoutError(secret)
        except TimeoutError as error:
            raise MailDeliveryUncertain("SMTP accepted submission but receipt timed out") from error

    engine.send_message = uncertain
    with TestClient(client.app, raise_server_exceptions=False) as safe_client:
        response = safe_client.post(
            "/messages",
            json={"run_id": str(run_a), "case_id": "case-1", "to_actor_id": "supplier-a",
                  "subject": "Status", "body": "Can you confirm?"},
            headers=headers("agent-a-secret", idempotency_key="mail-uncertain"),
        )

    assert response.status_code == 503
    assert "reconcile the mailbox receipt" in response.json()["detail"]
    assert response.headers["X-Correlation-ID"] == response.json()["correlation_id"]
    assert secret not in response.text and secret not in caplog.text
    assert "TimeoutError" in caplog.text and "servers/simulation/app.py:" in caplog.text


def test_factory_requires_explicit_principals():
    class Engine:
        pass

    with pytest.raises(ValueError, match="explicit principal map"):
        create_app(Engine(), {})
    app = create_app(Engine(), {"token": Principal(actor_id="controller", role="controller")})
    assert app.title == "Enterprise Simulation API"
