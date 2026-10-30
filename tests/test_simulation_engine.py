from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from servers.simulation.engine import ActorTurnUncertain, EngineConflict, MailDeliveryUncertain, SimulationEngine
from servers.simulation.models import (AdvanceClock, ApprovalDecision, CanonicalEvent,
    ERPAction, SendMessage, StartScenario, UpdateCase)
from servers.simulation.reference import _advance_day, _require_message
from servers.simulation.businesses import initialize_supplier_business
from servers.simulation.store import IdempotencyConflict, SQLiteStore, StoreNotFound
from servers.simulation.actor_client import ActorWorkerError
from servers.simulation.actor_protocol import (
    ActorEvaluateResponse, ActorTurnResponse, SupplierKernelStatePatch, SupplierStatePatch,
)


class FakeMail:
    """External transport boundary; persistence and workflow remain real."""

    def __init__(self):
        self.sent = []
        self.send_calls = []
        self.fail_after_submission = False
        self.accept_before_failure = False
        self.accepted_receipts = {}
        self.inbound = {}
        self.viv_poll_calls = []

    def send(self, sender, recipient, subject, body, message_id, run_id, generation,
             *, in_reply_to=None, automated=False, on_submission_started=lambda: None):
        self.send_calls.append(message_id)
        on_submission_started()
        if self.fail_after_submission:
            if self.accept_before_failure:
                self.accepted_receipts[message_id] = {"message_id": message_id, "subject": subject,
                    "run_id": run_id, "generation": generation, "mailbox_uid": "1",
                    "recipient_actor_id": recipient["actor_id"]}
            raise ConnectionError("simulated post-submission network loss")
        item = {"sender_actor_id": sender["actor_id"], "recipient_actor_id": recipient["actor_id"],
                "subject": subject, "body": body, "message_id": message_id,
                "run_id": run_id, "generation": generation, "in_reply_to": in_reply_to,
                "automated": automated}
        self.sent.append(item)
        return {"message_id": message_id, "subject": subject, "run_id": run_id,
                "generation": generation, "mailbox_uid": "1", "recipient_actor_id": recipient["actor_id"]}

    def find_receipt(self, _recipient, message_id, _subject, _run_id, _generation):
        return self.accepted_receipts.get(message_id)

    def poll(self, actor, *_args):
        return self.inbound.pop(actor["actor_id"], [])

    def poll_viv(self, **_kwargs):
        self.viv_poll_calls.append(_kwargs)
        return []


class FakeERP:
    def __init__(self):
        self.review_requests = []
        self.invoice_state = "Draft"
        self.po_version = "po-v1"
        self.fail_purchase_order_read = False
        self.receipts = {}
        self.receipt_calls = []
        self.fail_after_receipt_commit = False
        self.stock_balance = 8000

    def assess_invoice(self, _invoice):
        return {"lines": [{"purchase_order_id": "PO-1", "billed_qty": 10000,
                            "unreceived_billed_qty": 0 if self.receipts else 2000,
                            "receipt_ids": ["PR-1", *(["GR-1"] if self.receipts else [])]}]}

    def get_purchase_order(self, order_id):
        if self.fail_purchase_order_read:
            raise TypeError("unexpected adapter contract failure")
        return {"name": order_id, "modified": self.po_version, "currency": "GBP", "docstatus": 1,
                "items": [{"name": "PO-ROW-1", "item_code": "ITEM-1", "qty": 10000,
                           "received_qty": 10000 if self.receipts else 8000, "rate": "50", "warehouse": "WH-1"}]}

    def get_purchase_receipt(self, receipt_id):
        if receipt_id in self.receipts:
            return self.receipts[receipt_id]
        return {"name": receipt_id, "items": [{"received_qty": 8000}]}

    def create_goods_receipt(self, purchase_order_id, quantity, idempotency_key, verified_approval):
        self.receipt_calls.append((purchase_order_id, quantity, idempotency_key, verified_approval))
        if idempotency_key in self.receipts:
            return self.receipts[idempotency_key]
        assert self.po_version == verified_approval.expected_modified
        receipt_id = "GR-1"
        receipt = {"name": receipt_id, "docstatus": 1, "purchase_order": purchase_order_id,
                   "posting_date": verified_approval.posting_date,
                   "posting_time": verified_approval.posting_time,
                   "items": [{"purchase_order": purchase_order_id, "purchase_order_item": "PO-ROW-1",
                              "item_code": "ITEM-1", "qty": str(quantity), "received_qty": str(quantity),
                              "warehouse": "WH-1"}]}
        self.receipts[idempotency_key] = receipt
        self.receipts[receipt_id] = receipt
        self.po_version = "po-v2"
        if self.fail_after_receipt_commit:
            self.fail_after_receipt_commit = False
            raise TimeoutError("native receipt accepted before response loss")
        return receipt

    def read_payment_status(self, invoice):
        return {"purchase_invoice_id": invoice, "payments": []}

    def get_stock_balance(self, item_code, warehouse):
        assert item_code == "ITEM-1"
        assert warehouse == "WH-1"
        return self.stock_balance

    def get_purchase_invoice(self, invoice_id):
        return {"name": invoice_id, "modified": "pi-v1", "currency": "GBP",
                "outstanding_amount": "500000", "workflow_state": self.invoice_state, "docstatus": 0}

    def request_invoice_review(self, invoice_id, idempotency_key):
        self.review_requests.append((invoice_id, idempotency_key))
        self.invoice_state = "Review Requested"
        return self.get_purchase_invoice(invoice_id)

    def get_sales_order(self, order_id):
        return {"name": order_id}

    def read_sales_invoice_refunds(self, _sales_order_id):
        return []


class FakeSiteAdmin:
    def __init__(self):
        self.resets = 0

    def create(self, _run_id):
        return {"site_name": "owned-test-site", "documents": {
            "supplier": "SUP-1", "item": "ITEM-1", "warehouse": "WH-1",
            "purchase_order": {"name": "PO-1"}, "purchase_invoice": {"name": "PI-1"},
            "purchase_receipt": {"name": "PR-1"}, "customer_order": {"name": "SO-1"},
            "customer": {"name": "CUST-1"},
        }}

    def reset(self, run_id):
        self.resets += 1
        return self.create(run_id)


def actor_records():
    return [
        {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "agent"},
        {"actor_id": "requester", "email": "requester@example.test", "kind": "employee"},
        {"actor_id": "manager", "email": "manager@example.test", "kind": "approver"},
        {"actor_id": "supplier", "email": "supplier@example.test", "kind": "supplier",
         "private_state": {"seed": 4, "supplier_id": "SUP-1", "shipment_id": "SHIP-1",
                           "dispatch_quantity": 10000, "readable_unit": "components",
                           "dispatch_evidence_ids": ["MAN-1"],
                           "pricing_config": {"family": "cooperative", "reservation_price": "40",
                             "maximum_offer": "100", "kappa_b": 1, "stance": 0.5,
                             "opening_harshness": 0.5, "maximum_rounds": 5,
                             "minor_unit": 2, "minimum_offer": 0}}},
        {"actor_id": "customer", "email": "customer@example.test", "kind": "customer",
         "private_state": {"request_type": "damage", "order_id": "SO-1",
                           "damage_description": "two components arrived damaged", "serial_number": None}},
        {"actor_id": "warehouse", "email": "warehouse@example.test", "kind": "warehouse",
         "private_state": {"shipment_id": "SHIP-1", "received_quantity": 8000}},
        {"actor_id": "logistics", "email": "logistics@example.test", "kind": "logistics",
         "private_state": {"shipment_id": "SHIP-1", "quantity": 2000,
                           "location": "remote distribution centre"}},
    ]


def make_engine(tmp_path: Path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    mail = FakeMail()
    erp = FakeERP()
    engine = SimulationEngine(store, mail, lambda _binding: erp, FakeSiteAdmin(), actor_records())
    engine.actor_turn_client = ScriptedActorTurnClient()
    return engine, store, mail, erp


class ScriptedActorTurnClient:
    """Explicit test boundary for isolated upstream actor-worker RPCs."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = []

    def turn(self, request):
        self.requests.append(request.model_dump(mode="json"))
        body = self.responses.get(request.actor_type, "I am checking the requested information.")
        return ActorTurnResponse(
            turn_id=request.turn_id, status="completed", body=body,
            state=request.state or ({"system_messages": [], "messages": []}
                                    if request.actor_type == "customer" else None),
            supplier_private_state_patch=(SupplierStatePatch(kernel_state=SupplierKernelStatePatch())
                                          if request.actor_type == "supplier" else None),
            conversation_pointer="scripted-worker-test" if request.actor_type == "supplier" else None,
        )

    def evaluate(self, request):
        return ActorEvaluateResponse(
            evaluator="tau2-action+substring-communicate", action_reward=0,
            communicate_reward=0, reward=0, action_checks=[], communicate_checks=[],
        )


def _receipt_request(case_id: str, version: str = "po-v1") -> str:
    return ("APPROVAL REQUEST\noperation=create_goods_receipt\nentity_id=PO-1\n"
            f"entity_version={version}\ncase_id={case_id}\nquantity=2000")


def _send_manager_request(engine, run, body: str, key: str):
    return engine.send_message(SendMessage(run_id=run["run_id"], case_id=run["manifest"]["case_id"],
        to_actor_id="manager", subject="Finance approval request", body=body), key, "viv")


def test_run_start_and_message_are_persisted_and_member_scoped(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-1", "controller")
    assert run["status"] == "running"
    case_id = run["manifest"]["case_id"]

    sent = engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id,
        to_actor_id="supplier", subject="Dispatch evidence", body="Please send dispatch evidence."),
        "message-1", "viv")

    assert sent["message_id"] == mail.sent[1]["message_id"]
    assert mail.sent[0]["body"] == run["manifest"]["agent_instruction"]
    assert all(item["status"] == "completed" for item in store.list_outbox(run["run_id"], 0))
    with pytest.raises(EngineConflict, match="not a member"):
        engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id,
            to_actor_id="supplier", subject="x", body="y"), "message-2", "outsider")


def test_initial_s01_instruction_exposes_business_references_but_not_private_timeline_facts(tmp_path):
    engine, _store, mail, _erp = make_engine(tmp_path)
    engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-business-refs", "controller")

    body = mail.sent[0]["body"]
    assert "Case reference: S01-case" in body
    assert "Purchase order reference: PO-1" in body
    assert "Purchase invoice reference: PI-1" in body
    assert "Purchase receipt reference: PR-1" in body
    assert "Customer order reference: SO-1" in body
    assert "remote distribution centre" not in body
    assert "Day 14" not in body


def test_agent_inbox_uses_native_mail_adapter_with_only_sent_message_ids_as_reply_refs(tmp_path):
    engine, _store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-inbox", "controller")
    engine.inbox(run["run_id"], "viv")
    assert mail.viv_poll_calls[-1] == {"run_id": str(run["run_id"]), "generation": 0, "known_ids": set()}
    engine.send_message(SendMessage(run_id=run["run_id"], case_id=run["manifest"]["case_id"],
        to_actor_id="supplier", subject="Dispatch", body="Please send evidence."), "mail-ref", "viv")
    engine.inbox(run["run_id"], "viv")
    assert len(mail.viv_poll_calls[-1]["known_ids"]) == 1


def test_confirmed_agent_message_schedules_one_policy_reply_and_imap_echo_does_not_duplicate(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-policy-reply", "controller")
    sent = engine.send_message(SendMessage(run_id=run["run_id"], case_id=run["manifest"]["case_id"],
        to_actor_id="supplier", subject="Dispatch evidence", body="Please provide the dispatch manifest."),
        "supplier-request", "viv")
    existing = [event for event in store.list_events(run["run_id"], 0)
                if event["event_type"] == "EMPLOYEE_RESPONSE_READY"
                and event["payload"].get("response_to_outbox_id")]
    assert len(existing) == 1

    mail.inbound["supplier"] = [{"mailbox_uid": "1", "message_id": "<provider-copy@example.test>",
        "in_reply_to": sent["message_id"], "references": sent["message_id"],
        "subject": "Re: Dispatch evidence", "body": "Provider side echo.", "auto_submitted": "no"}]
    engine.pump(run["run_id"])

    all_responses = [event for event in store.list_events(run["run_id"], 0)
                     if event["event_type"] == "EMPLOYEE_RESPONSE_READY"
                     and event["payload"].get("response_to_outbox_id")]
    assert len(all_responses) == 1
    engine.advance(AdvanceClock(run_id=run["run_id"], until=datetime.fromisoformat(run["simulated_time"])),
                   "process-single-policy-response")
    replies = [item for item in store.list_outbox(run["run_id"], 0)
               if item["private_payload"].get("from_actor_id") == "supplier"
               and item["private_payload"].get("to_actor_id") == "viv"]
    assert len(replies) == 1


def test_external_viv_reply_to_initial_mail_remains_correlated_through_imap(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-inbound-viv", "controller")
    instruction = next(item for item in store.list_outbox(run["run_id"], 0)
                       if item["private_payload"].get("from_actor_id") == "requester")
    mail.inbound["requester"] = [{"mailbox_uid": "1", "message_id": "<viv-reply@example.test>",
        "in_reply_to": instruction["private_payload"]["message_id"],
        "references": instruction["private_payload"]["message_id"],
        "subject": "Re: Please investigate", "body": "I am checking the purchase receipt.",
        "auto_submitted": "no"}]

    engine.pump(run["run_id"])

    events = [event for event in store.list_events(run["run_id"], 0)
              if event["payload"].get("question") == "I am checking the purchase receipt."]
    assert len(events) == 1
    assert events[0]["payload"]["actor_id"] == "requester"
    assert events[0]["payload"]["from_actor_id"] == "viv"


def test_advance_processes_due_events_once_and_keeps_clock_monotonic(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-1", "controller")
    day7 = datetime.fromisoformat(run["simulated_time"]) + timedelta(days=6)
    result = engine.advance(AdvanceClock(run_id=run["run_id"], until=day7), "advance-1")
    rows = store.list_events(run["run_id"], 0)
    delay = next(event for event in rows if event["event_type"] == "SHIPMENT_DELAYED")
    assert delay["status"] == "completed"
    assert sum(event["status"] == "completed" for event in rows) == 2
    assert engine.advance(AdvanceClock(run_id=run["run_id"], until=day7), "advance-2")["simulated_time"] == result["simulated_time"]
    with pytest.raises(EngineConflict, match="backwards"):
        engine.advance(AdvanceClock(run_id=run["run_id"], until=datetime.fromisoformat(run["simulated_time"]) - timedelta(days=1)), "advance-back")


def test_customer_response_discloses_missing_serial_only_after_request(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    engine.actor_turn_client = ScriptedActorTurnClient({
        "customer": "I did not include the serial number with my complaint."
    })
    run = engine.start(StartScenario(scenario_id="E1", agent_id="viv"), "start-e1", "controller")
    event = {
        "event_id": "customer-reply-1", "run_id": run["run_id"], "scenario_id": "E1",
        "simulated_time": run["simulated_time"], "event_type": "EMPLOYEE_RESPONSE_READY",
        "source": "mail", "entity_type": "case", "entity_id": run["manifest"]["case_id"],
        "payload": {"actor_id": "customer", "from_actor_id": "viv", "question": "What is the serial number?",
                    "subject": "Serial details", "message_id": "<request@example.test>",
                    "visible_context": {"case_id": run["manifest"]["case_id"], "order_id": "SO-1"}},
        "visibility": ["customer"],
    }
    from servers.simulation.models import CanonicalEvent
    store.enqueue_event(CanonicalEvent.model_validate(event), 0)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=datetime.fromisoformat(run["simulated_time"])), "process-customer")
    outbox = next(item for item in store.list_outbox(run["run_id"], 0)
                  if "did not include the serial number" in item["private_payload"].get("body", ""))
    assert "did not include the serial number" in outbox["private_payload"]["body"]
    assert outbox["private_payload"]["to_actor_id"] == "viv"


def test_evaluator_does_not_pass_from_simulator_progress_alone(tmp_path):
    engine, _store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-1", "controller")
    result = engine.evaluate(run["run_id"])
    assert result["checks"]["invoice_finding_correct"] is False
    assert result["checks"]["review_requested"] is False
    assert result["agent_performance"] == "incomplete"


def test_s01_evaluator_does_not_count_initial_instruction_as_final_summary(tmp_path):
    engine, _store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-requester-employee", "controller")

    result = engine.evaluate(run["run_id"])

    assert result["checks"]["final_stakeholder_email_confirmed"] is False


def test_s01_correct_production_finding_and_notice_pass_against_initial_native_stock(tmp_path):
    engine, _store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "s01-shortage", "controller")
    case_id = run["manifest"]["case_id"]
    result = engine.evaluate(run["run_id"])
    assert result["checks"]["production_shortage_identified"] is False
    assert result["checks"]["production_shortage_notified"] is False

    item_id, warehouse_id = "ITEM-1", "WH-1"
    engine.advance(AdvanceClock(run_id=run["run_id"],
        until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=4)), "s01-shortage-day5")
    stock = engine.erp_read(run["run_id"], "get_stock_balance", item_id, "viv")
    engine.update_case(run["run_id"], case_id, UpdateCase(production_finding={
        "item_code": item_id, "required_quantity": 10000,
        "available_quantity": stock["actual_qty"], "shortage_quantity": 1000,
    }), "wrong-shortage", "viv")
    assert engine.evaluate(run["run_id"])["checks"]["production_shortage_identified"] is False
    engine.update_case(run["run_id"], case_id, UpdateCase(production_finding={
        "item_code": item_id, "required_quantity": 10000,
        "available_quantity": stock["actual_qty"], "shortage_quantity": 2000,
    }), "correct-shortage", "viv")
    engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id, to_actor_id="requester",
        subject="Production shortage identified",
        body=f"Production requires 10,000 units of {item_id}; native stock at {warehouse_id} is 8,000, leaving a 2,000-unit shortage."),
        "notify-shortage", "viv")

    result = engine.evaluate(run["run_id"])
    assert result["checks"]["production_shortage_identified"] is True
    assert result["checks"]["production_shortage_notified"] is True


def test_s01_wrong_historical_finding_fails_even_when_final_native_stock_reaches_10000(tmp_path):
    engine, _store, _mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"),
                       "s01-wrong-historical-shortage", "controller")
    initial = datetime.fromisoformat(run["manifest"]["initial_time"])
    deadline = initial + timedelta(days=4)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=deadline), "s01-wrong-finding-day5")
    engine.erp_read(run["run_id"], "get_stock_balance", "ITEM-1", "viv")
    engine.update_case(run["run_id"], run["manifest"]["case_id"], UpdateCase(production_finding={
        "item_code": "ITEM-1", "required_quantity": 10000,
        "available_quantity": 9000, "shortage_quantity": 1000,
    }), "s01-wrong-finding", "viv")
    erp.stock_balance = 10000
    engine.advance(AdvanceClock(run_id=run["run_id"], until=initial + timedelta(days=13)),
                   "s01-final-native-stock")

    result = engine.evaluate(run["run_id"])

    assert erp.get_stock_balance("ITEM-1", "WH-1") == 10000
    assert result["checks"]["production_shortage_identified"] is False


def test_s01_payment_grant_requires_completed_native_receipt_and_reconciled_case(tmp_path):
    engine, store, mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "s01-payment-grant", "controller")
    run_id, case_id = run["run_id"], run["manifest"]["case_id"]
    arrival = datetime.fromisoformat(run["simulated_time"]) + timedelta(days=13)
    engine.advance(AdvanceClock(run_id=run_id, until=arrival), "s01-payment-arrival")
    _send_manager_request(engine, run, _receipt_request(case_id), "s01-receipt-approval")
    engine.advance(AdvanceClock(run_id=run_id, until=arrival + timedelta(hours=1)), "s01-receipt-grant")
    receipt_decision = next(entry["private_record"] for entry in store.list_audit(run_id, 0)
        if entry["private_record"].get("event") == "approval_decision"
        and entry["private_record"].get("operation") == "create_goods_receipt")
    receipt = engine.erp_action(ERPAction(run_id=run_id, entity_id="PO-1",
        approval_id=receipt_decision["approval_id"],
        parameters={"case_id": case_id, "quantity": "2000"}),
        "create_goods_receipt", "s01-post-receipt", "viv")
    warehouse = store.get_actor(run_id, 0, "warehouse")
    assert warehouse["private_state"]["received_quantity"] == 10000
    assert warehouse["private_state"]["pending_quantity"] == 0
    observation = next(entry["private_record"] for entry in store.list_audit(run_id, 0)
        if entry["private_record"].get("event") == "warehouse_observation_refreshed")
    assert observation["receipt_id"] == receipt["receipt_id"]
    assert observation["received_quantity"] == "10000"
    assert observation["pending_quantity"] == "0"
    warehouse_request = engine.send_message(SendMessage(run_id=run_id, case_id=case_id,
        to_actor_id="warehouse", subject="Current receiving record",
        body="Please confirm the current received quantity for order PO-1."),
        "warehouse-post-receipt-query", "viv")
    mail.inbound["warehouse"] = [{"mailbox_uid": "9", "message_id": "<warehouse-current@example.test>",
        "in_reply_to": warehouse_request["message_id"], "references": warehouse_request["message_id"],
        "subject": "Re: Current receiving record", "body": "Please confirm the current received quantity.",
        "auto_submitted": "no"}]
    engine.pump(run_id)
    engine.advance(AdvanceClock(run_id=run_id, until=datetime.fromisoformat(
        store.get_run(run_id)["simulated_time"])), "warehouse-current-response")
    warehouse_reply = next(item for item in mail.sent
        if item["sender_actor_id"] == "warehouse" and item["recipient_actor_id"] == "viv"
        and item["in_reply_to"] == warehouse_request["message_id"])
    assert "10000 units received" in warehouse_reply["body"]
    engine.update_case(run_id, case_id, UpdateCase(status="in_review", invoice_finding={
        "purchase_order_id": "PO-1", "purchase_invoice_id": "PI-1",
        "quantity_difference": 0, "receipt_ids": ["PR-1", receipt["receipt_id"]],
    }), "s01-resolved-finding", "viv")
    payment_body = ("APPROVAL REQUEST\noperation=post_payment\nentity_id=PI-1\n"
        f"entity_version=pi-v1\ncase_id={case_id}\namount=500000\ncurrency=GBP")
    payment_run = engine.store.get_run(run_id)
    _send_manager_request(engine, payment_run, payment_body, "s01-payment-approval")
    engine.advance(AdvanceClock(run_id=run_id,
        until=arrival + timedelta(hours=2)), "s01-payment-grant")

    payment_decision = next(entry["private_record"] for entry in store.list_audit(run_id, 0)
        if entry["private_record"].get("event") == "approval_decision"
        and entry["private_record"].get("operation") == "post_payment")
    assert payment_decision["decision"] == "approved", payment_decision.get("reason")
    assert payment_decision["approved_by"] == "manager"
    assert payment_decision["origin"] == "simulation_policy"
    assert erp.read_payment_status("PI-1")["payments"] == []
    assert engine.evaluate(run_id)["checks"]["payment_approval_granted"] is True


def test_finance_manager_denies_prose_approval_and_receipt_before_physical_arrival(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-manager-early", "controller")
    mail.inbound["manager"] = [{"mailbox_uid": "1", "body": "I approve", "from": "manager@example.test"}]

    _send_manager_request(engine, run, _receipt_request("S01-case"), "manager-early-receipt")
    _send_manager_request(engine, run, "I approve this receipt. Please proceed.", "manager-prose")
    engine.advance(AdvanceClock(run_id=run["run_id"],
        until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=1)), "manager-early-review")

    decisions = [entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                 if entry["private_record"].get("event") == "approval_decision"]
    assert len(decisions) == 2
    assert all(decision["decision"] == "denied" for decision in decisions)
    assert decisions[0]["approved_by"] == "manager"
    assert decisions[0]["origin"] == "simulation_policy"
    assert "physical arrival" in decisions[0]["reason"]
    assert not any(item["sender_actor_id"] == "manager" and "APPROVED" in item["body"]
                   for item in mail.sent)
    with pytest.raises(EngineConflict, match="simulated manager"):
        engine.approve(ApprovalDecision(run_id=run["run_id"], case_id="S01-case",
            operation="create_goods_receipt", entity_id="PO-1", entity_version="po-v1",
            decision="approved", amount=2000), "forged-api-approval", "manager")


def test_manager_adapter_failure_remains_operational_error_not_policy_denial(tmp_path):
    engine, store, _mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-manager-error", "controller")
    _send_manager_request(engine, run, _receipt_request("S01-case"), "manager-adapter-error")
    erp.fail_purchase_order_read = True

    with pytest.raises(TypeError, match="adapter contract failure"):
        engine.advance(AdvanceClock(run_id=run["run_id"],
            until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=1)), "manager-adapter-error-review")

    decisions = [entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                 if entry["private_record"].get("event") == "approval_decision"]
    assert decisions == []


def test_manager_grant_is_grounded_in_arrival_and_versioned_native_order(tmp_path):
    engine, store, mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-manager-arrived", "controller")
    day14 = datetime.fromisoformat(run["simulated_time"]) + timedelta(days=13)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=day14), "arrival-before-approval")

    _send_manager_request(engine, run, _receipt_request("S01-case"), "manager-receipt-v1")
    engine.advance(AdvanceClock(run_id=run["run_id"], until=day14 + timedelta(days=1)), "manager-approve-v1")
    first = next(entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                 if entry["private_record"].get("event") == "approval_decision")
    assert first["decision"] == "approved"
    assert first["quantity"] == 2000
    assert first["entity_version"] == "po-v1"
    assert first["policy_version"] == "finance-manager-fixture-v2"
    first_manager_message = next(item for item in mail.sent
        if item["sender_actor_id"] == "manager" and first["approval_id"] in item["body"])
    assert first_manager_message["recipient_actor_id"] == "viv"

    erp.po_version = "po-v2"
    stale_action = ERPAction(run_id=run["run_id"], entity_id="PO-1", approval_id=first["approval_id"],
        parameters={"case_id": "S01-case", "quantity": "2000"})
    with pytest.raises(EngineConflict, match="changed since finance approval"):
        engine.erp_action(stale_action, "create_goods_receipt", "stale-post", "viv")
    assert erp.receipt_calls == []

    _send_manager_request(engine, run, _receipt_request("S01-case", "po-v2"), "manager-receipt-v2")
    engine.advance(AdvanceClock(run_id=run["run_id"], until=day14 + timedelta(days=2)), "manager-approve-v2")
    decisions = [entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                 if entry["private_record"].get("event") == "approval_decision"]
    assert decisions[-1]["decision"] == "approved"
    assert decisions[-1]["approval_id"] != first["approval_id"]
    assert decisions[-1]["entity_version"] == "po-v2"

    action = ERPAction(run_id=run["run_id"], entity_id="PO-1", approval_id=decisions[-1]["approval_id"],
        parameters={"case_id": "S01-case", "quantity": "2000"})
    erp.fail_after_receipt_commit = True
    with pytest.raises(TimeoutError, match="accepted before response loss"):
        engine.erp_action(action, "create_goods_receipt", "post-receipt", "viv")
    posted = engine.erp_action(action, "create_goods_receipt", "post-receipt", "viv")
    assert posted["receipt_id"] == "GR-1"
    assert posted["docstatus"] == 1
    assert len(erp.receipt_calls) == 2
    assert engine.erp_action(action, "create_goods_receipt", "post-receipt", "viv") == posted
    assert len(erp.receipt_calls) == 2


def test_correlated_viv_email_to_manager_is_policy_evaluated_but_forged_manager_sender_is_ignored(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-manager-email", "controller")
    run_id = str(run["run_id"])
    case_id = run["manifest"]["case_id"]
    arrival = datetime.fromisoformat(run["simulated_time"]) + timedelta(days=13)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=arrival), "manager-email-arrival")
    body = _receipt_request(case_id)

    mail.inbound["manager"] = [{"mailbox_uid": "8", "message_id": "<forged-manager@example.test>",
        "from_header": "Finance Manager <manager@example.test>", "run_id": run_id,
        "generation": 0, "subject": "APPROVAL REQUEST", "body": body,
        "auto_submitted": "no"}]
    engine.pump(run["run_id"])
    assert not any(entry["private_record"].get("event") == "approval_decision"
                   for entry in store.list_audit(run["run_id"], 0))

    mail.inbound["manager"] = [{"mailbox_uid": "9", "message_id": "<viv-request@example.test>",
        "from_header": "Viv <agent@agent.example.test>", "run_id": run_id,
        "generation": 0, "subject": "Finance approval request", "body": body,
        "auto_submitted": "no"}]
    engine.pump(run["run_id"])
    received = [entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                if entry["private_record"].get("event") == "manager_request_received"]
    assert len(received) == 1

    engine.advance(AdvanceClock(run_id=run["run_id"], until=arrival + timedelta(hours=1)),
                   "manager-email-policy-review")
    decision = next(entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                    if entry["private_record"].get("event") == "approval_decision")
    assert decision["decision"] == "approved"
    assert decision["approved_by"] == "manager"
    assert decision["inbound_message_id"] == "<viv-request@example.test>"
    assert decision["origin"] == "simulation_policy"
def test_manager_policy_does_not_authorize_refunds(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="E1", agent_id="viv"), "start-manager-refund", "controller")
    body = ("APPROVAL REQUEST\noperation=refund\nentity_id=SO-1\nentity_version=so-v1\n"
            f"case_id={run['manifest']['case_id']}\namount=100\ncurrency=GBP")
    _send_manager_request(engine, run, body, "manager-refund")
    engine.advance(AdvanceClock(run_id=run["run_id"],
        until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=1)), "manager-deny-refund")

    decision = next(entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                    if entry["private_record"].get("event") == "approval_decision")
    assert decision["decision"] == "denied"
    assert decision["approval_id"] is None


def test_manager_rejects_payment_before_receipts_and_resolved_discrepancy(tmp_path):
    engine, store, _mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-manager-payment", "controller")
    body = ("APPROVAL REQUEST\noperation=post_payment\nentity_id=PI-1\n"
            f"entity_version=pi-v1\ncase_id={run['manifest']['case_id']}\n"
            "amount=500000\ncurrency=GBP")
    _send_manager_request(engine, run, body, "manager-payment-incomplete")
    engine.advance(AdvanceClock(run_id=run["run_id"],
        until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=1)), "manager-deny-payment")

    decision = next(entry["private_record"] for entry in store.list_audit(run["run_id"], 0)
                    if entry["private_record"].get("event") == "approval_decision")
    assert decision["decision"] == "denied"
    assert "matching posted receipts" in decision["reason"]
    assert erp.read_payment_status("PI-1")["payments"] == []


def test_a1_evaluator_requires_correct_recorded_finding_and_native_review(tmp_path):
    engine, _store, _mail, _erp = make_engine(tmp_path)
    engine.environment_manifest = {"source_sha256": {"engine.py": "recorded"}}
    engine.environment_manifest_provider = lambda: {"source_sha256": {"engine.py": "current"}}
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-a1-pass", "controller")
    for operation, entity in (("get_purchase_order", "PO-1"), ("get_purchase_invoice", "PI-1"),
                              ("get_purchase_receipt", "PR-1")):
        engine.erp_read(run["run_id"], operation, entity, "viv")
    engine.update_case(run["run_id"], run["manifest"]["case_id"],
        UpdateCase(invoice_finding={"purchase_order_id": "PO-1", "purchase_invoice_id": "PI-1",
                                    "quantity_difference": "2000", "receipt_ids": ["PR-1"]}),
        "finding-1", "viv")
    engine.request_review(run["run_id"], "PI-1", "review-1", "viv")

    result = engine.evaluate(run["run_id"])
    assert result["agent_performance"] == "pass"
    assert result["checks"]["invoice_finding_correct"] is True
    assert result["environment_drift"] is True
    with pytest.raises(StoreNotFound, match="not found"):
        engine.erp_read(run["run_id"], "assess_invoice", "PI-1", "viv")


def test_invoice_review_key_conflict_is_rejected_before_second_erp_call(tmp_path):
    engine, _store, _mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-review-key", "controller")
    engine.request_review(run["run_id"], "PI-1", "same-review", "viv")
    with pytest.raises(IdempotencyConflict):
        engine.request_review(run["run_id"], "PI-OTHER", "same-review", "viv")
    assert len(erp.review_requests) == 1


def test_uncertain_mail_is_reconciled_without_blind_resubmission(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-mail", "controller")
    mail.fail_after_submission = True
    request = SendMessage(run_id=run["run_id"], case_id=run["manifest"]["case_id"],
        to_actor_id="supplier", subject="Records", body="Please send shipment evidence.")
    with pytest.raises(MailDeliveryUncertain):
        engine.send_message(request, "uncertain-1", "viv")
    before = len(mail.sent)
    assert engine.pump(run["run_id"])["uncertain"]
    assert len(mail.sent) == before
    item = next(item for item in store.list_outbox(run["run_id"], 0)
                if item["private_payload"].get("subject") == "Records")
    assert item["status"] == "uncertain"


def test_same_idempotency_key_after_worker_reconciliation_returns_completed_receipt_without_resubmit(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-retry-completed", "controller")
    request = SendMessage(run_id=run["run_id"], case_id=run["manifest"]["case_id"],
        to_actor_id="supplier", subject="Records", body="Please send the shipment evidence.")
    mail.fail_after_submission = True
    mail.accept_before_failure = True
    with pytest.raises(MailDeliveryUncertain):
        engine.send_message(request, "stable-send-key", "viv")
    item = next(item for item in store.list_outbox(run["run_id"], 0)
                if item["private_payload"].get("subject") == "Records")
    assert item["status"] == "uncertain"

    mail.fail_after_submission = False
    engine.pump(run["run_id"])
    completed = store.get_outbox(run["run_id"], 0, item["outbox_id"])
    assert completed["status"] == "completed"
    receipt = engine.send_message(request, "stable-send-key", "viv")

    assert receipt == completed["outcome"]
    assert mail.send_calls.count(item["private_payload"]["message_id"]) == 1
    assert store.get_outbox(run["run_id"], 0, item["outbox_id"])["status"] == "completed"


def test_uncertain_actor_turn_retry_reuses_exact_private_request_snapshot(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="E1", agent_id="viv"), "start-actor-turn", "controller")
    case_id = run["manifest"]["case_id"]
    event = store.enqueue_event(CanonicalEvent(
        event_id="customer-email-1", run_id=run["run_id"], scenario_id="E1",
        simulated_time=datetime.fromisoformat(run["simulated_time"]),
        event_type="EMPLOYEE_RESPONSE_READY", source="agent", entity_type="case",
        entity_id=case_id, payload={"actor_id": "customer", "from_actor_id": "viv",
            "message_id": "<agent-question@example.test>", "subject": "Serial number",
            "question": "Can you share the serial number?", "visible_context": {"case_id": case_id}},
        visibility=["customer"],
    ), 0)

    class UncertainThenComplete:
        def __init__(self):
            self.requests = []

        def turn(self, request):
            self.requests.append(request.model_dump(mode="json"))
            if len(self.requests) == 1:
                raise ActorWorkerError("customer", None, "worker_transport_uncertain")
            return ActorTurnResponse(turn_id=request.turn_id, status="completed",
                body="I do not have the serial number.", state={"messages": [], "system_messages": []})

    client = UncertainThenComplete()
    engine.actor_turn_client = client
    customer = store.get_actor(run["run_id"], 0, "customer")
    with pytest.raises(ActorTurnUncertain, match="customer actor turn remains pending"):
        engine._process_upstream_actor_event(run, event, customer)
    original_snapshot = client.requests[0]

    changed = store.get_actor(run["run_id"], 0, "customer")
    changed_private = changed["private_state"]
    changed_private["tau2_task"]["user_scenario"]["instructions"]["known_info"] = "changed canonical fact"
    store.put_actor(run["run_id"], 0, "customer", kind=changed["kind"],
        email=changed["email"], private_state=changed_private)

    engine._process_upstream_actor_event(run, event, store.get_actor(run["run_id"], 0, "customer"))
    assert client.requests == [original_snapshot, original_snapshot]
    persisted_event = next(row for row in store.list_events(run["run_id"], 0)
                           if row["event_id"] == event["event_id"])
    assert persisted_event["status"] == "completed"
    audit = [row["private_record"] for row in store.list_audit(run["run_id"], 0)]
    intent = next(row for row in audit if row.get("event") == "actor_turn_started")
    assert intent["request_snapshot"] == original_snapshot
    assert "changed canonical fact" not in str(intent["request_snapshot"])


def test_reset_recreates_clock_events_and_same_key_does_not_reset_again(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    mail = FakeMail()
    erp = FakeERP()
    site_admin = FakeSiteAdmin()
    engine = SimulationEngine(store, mail, lambda _binding: erp, site_admin, actor_records())
    configuration = {"model": "model-x", "prompt": "full prompt payload", "environment_version": "env-7",
                     "code_hashes": {"harness": "sha256:123"}}
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv", agent_configuration=configuration),
                       "start-reset", "controller")
    first_message = engine.send_message(SendMessage(run_id=run["run_id"],
        case_id=run["manifest"]["case_id"], to_actor_id="supplier", subject="Before reset",
        body="Please send dispatch evidence."), "reused-key", "viv")
    result = engine.reset(run["run_id"], "reset-1", "controller")
    assert result["generation"] == 1
    recorded = store.get_run(run["run_id"])["manifest"]["agent_configuration"]
    assert recorded == {"model": "model-x", "prompt_sha256": sha256(
        b'"full prompt payload"').hexdigest(), "environment_version": "env-7",
        "code_hashes": {"harness": "sha256:123"}}
    assert "full prompt payload" not in str(store.get_run(run["run_id"])["manifest"])
    assert len(store.pending_events(run["run_id"], 1)) == 5
    assert engine.reset(run["run_id"], "reset-1", "controller") == result
    assert site_admin.resets == 1
    second_message = engine.send_message(SendMessage(run_id=run["run_id"],
        case_id=run["manifest"]["case_id"], to_actor_id="supplier", subject="After reset",
        body="Please send the current dispatch evidence."), "reused-key", "viv")
    assert first_message["message_id"] != second_message["message_id"]
    assert store.get_outbox(run["run_id"], 1,
        next(item["outbox_id"] for item in store.list_outbox(run["run_id"], 1)
             if item["private_payload"].get("subject") == "After reset"))["status"] == "completed"


def test_reset_status_is_durable_before_external_restore_and_blocks_case_reads(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    mail = FakeMail()
    erp = FakeERP()

    class ObservedResetAdmin(FakeSiteAdmin):
        engine = None
        observed_status = None
        blocked_case_read = False

        def reset(self, run_id):
            self.resets += 1
            self.observed_status = store.get_run(run_id)["status"]
            try:
                self.engine.case(run_id, "S01-case")
            except EngineConflict:
                self.blocked_case_read = True
            return self.create(run_id)

    site_admin = ObservedResetAdmin()
    engine = SimulationEngine(store, mail, lambda _binding: erp, site_admin, actor_records())
    site_admin.engine = engine
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-reset-lock", "controller")
    engine.reset(run["run_id"], "reset-lock", "controller")
    assert site_admin.observed_status == "resetting"
    assert site_admin.blocked_case_read is True


def test_distinct_engine_instances_cannot_cross_reset_side_effect_boundary(tmp_path):
    store = SQLiteStore(tmp_path / "simulation.sqlite")
    mail = FakeMail()
    erp = FakeERP()
    reset_entered = Event()
    allow_reset = Event()

    class BlockingResetAdmin(FakeSiteAdmin):
        def reset(self, run_id):
            reset_entered.set()
            assert allow_reset.wait(timeout=3)
            return self.create(run_id)

    admin = BlockingResetAdmin()
    first = SimulationEngine(store, mail, lambda _binding: erp, admin, actor_records())
    second = SimulationEngine(store, mail, lambda _binding: erp, admin, actor_records())
    run = first.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-file-lock", "controller")
    errors = []
    def reset_first():
        try:
            first.reset(run["run_id"], "reset-one", "controller")
        except Exception as exc:
            errors.append(exc)

    reset_thread = Thread(target=reset_first)
    reset_thread.start()
    try:
        assert reset_entered.wait(timeout=3)
        with pytest.raises(EngineConflict, match="another operation currently owns"):
            second.reset(run["run_id"], "reset-two", "controller")
        assert admin.resets == 0
    finally:
        allow_reset.set()
        reset_thread.join(timeout=3)

    assert not reset_thread.is_alive()
    assert errors == []
    assert store.get_run(run["run_id"])["generation"] == 1


def test_standalone_scenarios_seed_their_required_timeline_and_customer_email(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    b1 = engine.start(StartScenario(scenario_id="B1", agent_id="viv"), "start-b1", "controller")
    b1_events = store.pending_events(b1["run_id"], 0)
    assert [(event["event_type"], datetime.fromisoformat(event["simulated_time"]).date())
            for event in b1_events] == [
                ("SHIPMENT_DELAYED", datetime.fromisoformat(b1["simulated_time"]).date() + timedelta(days=1)),
                ("DELIVERY_ARRIVED", datetime.fromisoformat(b1["simulated_time"]).date() + timedelta(days=4)),
            ]

    d1 = engine.start(StartScenario(scenario_id="D1", agent_id="viv"), "start-d1", "controller")
    assert [event["event_type"] for event in store.pending_events(d1["run_id"], 0)] == ["SUPPLIER_PRICING_CHANGED"]


def test_canonical_supplier_market_event_updates_only_its_private_actor_and_audits_outcome(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run_id = uuid4()
    run = store.create_run(run_id, StartScenario(scenario_id="D1", seed=17, agent_id="viv"))
    private_state = initialize_supplier_business({
        "supplier_id": "SUP-1", "seed": 17,
        "pricing_config": {"reservation_price": "45", "maximum_offer": "100",
                            "minor_unit": 2, "family": "Candid", "stance": "neutral",
                            "kappa_b": 1.0, "opening_harshness": 0.5, "maximum_rounds": 5},
    }, seed=17, supplier_id="SUP-1")
    store.put_actor(run_id, 0, "supplier", kind="supplier", email=None, private_state=private_state)
    event = CanonicalEvent(event_id="market-demand-bridge", run_id=run_id, scenario_id="D1",
        simulated_time=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        event_type="SUPPLIER_MARKET_DEMAND", source="market_simulator",
        entity_type="supplier", entity_id="SUP-1", payload={"quantity": 2500})

    with store.transaction() as connection:
        outcome = engine._apply_event(run_id, run, event.model_dump(mode="json"), connection)

    actor = store.get_actor(run_id, 0, "supplier")
    market_audit = next(entry["private_record"] for entry in store.list_audit(run_id, 0)
                        if entry["private_record"].get("event") == "supplier_market_state_updated")
    assert outcome == {"processed": True, "event_type": "SUPPLIER_MARKET_DEMAND"}
    assert actor["private_state"]["market_state"]["available_quantity"] == 9500
    assert market_audit["outcome"]["served_quantity"] == 2500


def test_canonical_supplier_market_event_rejects_another_supplier_identity_without_state_change(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run_id = uuid4()
    run = store.create_run(run_id, StartScenario(scenario_id="D1", seed=17, agent_id="viv"))
    private_state = initialize_supplier_business({
        "supplier_id": "SUP-1", "seed": 17,
        "pricing_config": {"reservation_price": "45", "maximum_offer": "100",
                            "minor_unit": 2, "family": "Candid", "stance": "neutral",
                            "kappa_b": 1.0, "opening_harshness": 0.5, "maximum_rounds": 5},
    }, seed=17, supplier_id="SUP-1")
    store.put_actor(run_id, 0, "supplier", kind="supplier", email=None, private_state=private_state)
    event = CanonicalEvent(event_id="market-demand-wrong-target", run_id=run_id, scenario_id="D1",
        simulated_time=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        event_type="SUPPLIER_MARKET_DEMAND", source="market_simulator",
        entity_type="supplier", entity_id="SUP-2", payload={"quantity": 2500})

    with pytest.raises(EngineConflict, match="configured supplier identity"):
        with store.transaction() as connection:
            engine._apply_event(run_id, run, event.model_dump(mode="json"), connection)

    assert store.get_actor(run_id, 0, "supplier")["private_state"]["market_state"]["available_quantity"] == 12_000

    e1 = engine.start(StartScenario(scenario_id="E1", agent_id="viv"), "start-e1-mail", "controller")
    customer_message = next(item["private_payload"] for item in store.list_outbox(e1["run_id"], 0)
                            if item["private_payload"].get("from_actor_id") == "customer")
    assert "damaged" in customer_message["body"]
    assert "serial" in customer_message["body"].casefold()
    assert "SO-1" not in customer_message["body"]


def test_s01_expedite_offer_is_private_scenario_fact_until_supplier_is_asked(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", seed=17, agent_id="viv"),
                       "s01-expedite-private", "controller")
    supplier = store.get_actor(run["run_id"], 0, "supplier")
    offer = supplier["private_state"]["expedite_proposal"]
    assert offer == {
        "quote_id": "EXP-0017-001", "shipment_id": supplier["private_state"]["shipment_id"],
        "fee_amount": "750.00", "fee_cap_amount": "1000.00",
        "fee_currency": "GBP", "service": "priority freight",
        "delivery_commitment": (datetime.fromisoformat(run["simulated_time"]) + timedelta(days=13)).date().isoformat(),
    }
    instruction = next(item["private_payload"] for item in store.list_outbox(run["run_id"], 0)
        if item["private_payload"].get("to_actor_id") == "viv"
        and item["private_payload"].get("from_actor_id") == "requester")
    assert offer["quote_id"] not in instruction["body"]
    assert offer["fee_amount"] not in instruction["body"]


def test_reference_matches_production_deadline_in_actual_notice_body(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", seed=17, agent_id="viv"),
                       "s01-production-notice-reference", "controller")
    initial = datetime.fromisoformat(store.get_run(run["run_id"])["manifest"]["initial_time"])
    engine.advance(AdvanceClock(run_id=run["run_id"], until=initial + timedelta(days=4)),
                   "s01-production-notice")
    sent = next(message for message in reversed(mail.sent)
                if message["sender_actor_id"] == "logistics"
                and message["subject"] == "Production deadline is due")
    logistics = store.get_actor(run["run_id"], 0, "logistics")
    rendered = {"message_id": sent["message_id"], "from": logistics["email"],
                "subject": sent["subject"], "body": sent["body"]}

    assert "Production deadline" not in sent["body"]
    assert "Production requires 10,000 units" in sent["body"]
    assert _require_message([rendered], contains="Production requires", from_actor="logistics",
                            engine=engine, run_id=str(run["run_id"])) == rendered


def test_reference_retry_uses_manifest_clock_without_rewinding_a_resumed_run(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", seed=17, agent_id="viv"),
                       "s01-reference-resume-clock", "controller")
    initial = datetime.fromisoformat(store.get_run(run["run_id"])["manifest"]["initial_time"])
    resumed_at = initial + timedelta(days=4)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=resumed_at), "s01-reference-resume-day5")
    supplier = store.get_actor(run["run_id"], 0, "supplier")
    historical_reply = {"message_id": "<historical-supplier@example.test>",
        "from": supplier["email"], "subject": "Re: Dispatch evidence request",
        "body": "Supporting dispatch records: dispatch-record-001."}
    engine.viv_activity_version = lambda: 0
    engine.wait_for_viv_activity = lambda *_args, **_kwargs: False
    engine.inbox = lambda _run_id, _actor_id: [historical_reply]

    result, messages = _advance_day(engine, str(run["run_id"]), initial, 2,
        "s01-reference-resume-day2", expected_from="supplier", contains="Supporting dispatch records")

    assert result["already_advanced"] is True
    assert messages == [historical_reply]
    assert store.get_run(run["run_id"])["simulated_time"] == resumed_at.isoformat()


def test_payment_event_is_not_marked_complete_without_erp_reconciliation(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="A1", agent_id="viv"), "start-payment", "controller")
    event = CanonicalEvent(event_id="payment-event", run_id=run["run_id"], scenario_id="A1",
        simulated_time=run["simulated_time"], event_type="PAYMENT_POSTED", source="controller",
        entity_type="purchase_invoice", entity_id="PI-1")
    engine.enqueue_event(event, "payment-event-key")
    with pytest.raises(EngineConflict, match="verified against ERPNext"):
        engine.advance(AdvanceClock(run_id=run["run_id"], until=datetime.fromisoformat(run["simulated_time"])), "advance-payment")
    assert store.pending_events(run["run_id"], 0)[0]["event_id"] == "payment-event"


def test_logistics_location_is_disclosed_only_after_day_four_question_response(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"), "start-logistics", "controller")
    case_id = run["manifest"]["case_id"]
    engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id,
        to_actor_id="logistics", subject="Shipment location", body="Where is the shipment?"),
        "ask-logistics", "viv")
    sent_request = mail.sent[-1]
    mail.inbound["logistics"] = [{"mailbox_uid": "8", "message_id": "<supplier-reply@example.test>",
        "in_reply_to": sent_request["message_id"], "references": sent_request["message_id"],
        "subject": "Re: Shipment location", "body": "Where is the shipment?",
        "auto_submitted": "no"}]
    engine.pump(run["run_id"])
    pending = store.pending_events(run["run_id"], 0)
    response_event = next(event for event in pending if event["event_type"] == "EMPLOYEE_RESPONSE_READY")
    assert datetime.fromisoformat(response_event["simulated_time"]) == datetime.fromisoformat(run["simulated_time"]) + timedelta(days=3)

    engine.advance(AdvanceClock(run_id=run["run_id"], until=datetime.fromisoformat(run["simulated_time"])), "hold-day-one")
    assert not any("remote distribution centre" in message["body"] for message in mail.sent)
    day4 = datetime.fromisoformat(run["simulated_time"]) + timedelta(days=3)
    engine.advance(AdvanceClock(run_id=run["run_id"], until=day4), "advance-day-four")
    assert any("remote distribution centre" in message["body"] for message in mail.sent)




def test_delivery_arrival_updates_logistics_location_only_after_physical_arrival(tmp_path):
    engine, store, mail, erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"),
                       "start-arrival-location", "controller")
    run_id, case_id = run["run_id"], run["manifest"]["case_id"]
    initial = datetime.fromisoformat(run["simulated_time"])
    logistics = store.get_actor(run_id, 0, "logistics")
    assert logistics["private_state"]["location"] == "remote distribution centre"

    engine.advance(AdvanceClock(run_id=run_id, until=initial + timedelta(days=13)),
                   "arrival-updates-location")

    logistics = store.get_actor(run_id, 0, "logistics")
    assert logistics["private_state"]["shipment_status"] == "arrived"
    assert logistics["private_state"]["location"] == "WH-1"
    warehouse = store.get_actor(run_id, 0, "warehouse")
    assert warehouse["private_state"]["received_quantity"] == 8000
    assert warehouse["private_state"]["pending_quantity"] == 2000
    assert erp.get_purchase_order("PO-1")["items"][0]["received_qty"] == 8000
    request = engine.send_message(SendMessage(run_id=run_id, case_id=case_id,
        to_actor_id="logistics", subject="Arrival location", body="Where is shipment SHIP-1 now?"),
        "arrival-location-query", "viv")
    mail.inbound["logistics"] = [{"mailbox_uid": "11", "message_id": "<logistics-arrival-location@example.test>",
        "in_reply_to": request["message_id"], "references": request["message_id"],
        "subject": "Re: Arrival location", "body": "Where is shipment SHIP-1 now?",
        "auto_submitted": "no"}]
    engine.pump(run_id)
    engine.advance(AdvanceClock(run_id=run_id, until=datetime.fromisoformat(
        store.get_run(run_id)["simulated_time"])), "arrival-location-response")

    reply = next(item for item in mail.sent
        if item["sender_actor_id"] == "logistics" and item["recipient_actor_id"] == "viv"
        and item["in_reply_to"] == request["message_id"])
    assert "at WH-1" in reply["body"]
    assert "remote distribution centre" not in reply["body"]
    assert erp.get_purchase_order("PO-1")["items"][0]["received_qty"] == 8000


def test_actor_reply_outbox_retains_originating_case_identity(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv"),
                       "actor-reply-case-link", "controller")
    case_id = run["manifest"]["case_id"]
    engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id,
        to_actor_id="supplier", subject="Dispatch evidence", body="Please send dispatch records."),
        "actor-reply-case-request", "viv")
    engine.advance(AdvanceClock(run_id=run["run_id"],
        until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=1)),
        "actor-reply-case-day2")

    reply = next(item for item in store.list_outbox(run["run_id"], 0, status="completed")
        if item["private_payload"].get("from_actor_id") == "supplier"
        and item["private_payload"].get("to_actor_id") == "viv")
    assert reply["private_payload"]["case_id"] == case_id


def test_case_order_link_requires_run_bound_native_sales_order_and_agent_action(tmp_path):
    engine, store, _mail, _erp = make_engine(tmp_path)

    class SiteWithCustomerOrder(FakeSiteAdmin):
        def create(self, run_id):
            binding = super().create(run_id)
            binding["documents"]["customer_order"] = {"name": "SO-1"}
            return binding

    engine.site_admin = SiteWithCustomerOrder()
    run = engine.start(StartScenario(scenario_id="E1", agent_id="viv"), "start-order-link", "controller")
    case_id = run["manifest"]["case_id"]
    assert "customer_order_id" not in engine.case(run["run_id"], case_id)
    with pytest.raises(EngineConflict, match="not part of this run"):
        engine.update_case(run["run_id"], case_id,
            UpdateCase(customer_order_id="SO-OTHER"),
            "link-wrong", "viv")
    linked = engine.update_case(run["run_id"], case_id,
        UpdateCase(customer_order_id="SO-1"),
        "link-right", "viv")
    assert linked["customer_order_id"] == "SO-1"
    assert any(entry["private_record"].get("event") == "case_updated"
               and entry["private_record"].get("actor_id") == "viv"
               for entry in store.list_audit(run["run_id"], 0))


def test_e1_requires_grounded_unresolved_serial_finding_before_evaluation_pass(tmp_path):
    engine, store, mail, _erp = make_engine(tmp_path)
    engine.actor_turn_client = ScriptedActorTurnClient({
        "customer": "I did not include the serial number for the damaged components."
    })

    class SiteWithCustomerOrder(FakeSiteAdmin):
        def create(self, run_id):
            binding = super().create(run_id)
            binding["documents"]["customer_order"] = {"name": "SO-1"}
            return binding

    engine.site_admin = SiteWithCustomerOrder()
    mail.poll_viv = lambda *, run_id, generation, known_ids: [
        {"message_id": item["message_id"], "subject": item["subject"], "body": item["body"],
         "from": item["sender_actor_id"], "to": item["recipient_actor_id"],
         "in_reply_to": item["in_reply_to"], "references": item["in_reply_to"] or "",
         "run_id": item["run_id"], "generation": item["generation"], "mailbox_uid": str(index)}
        for index, item in enumerate(mail.sent, start=1)
        if item["recipient_actor_id"] == "viv" and item["run_id"] == run_id
        and item["generation"] == generation]
    run = engine.start(StartScenario(scenario_id="E1", agent_id="viv"),
                       "e1-grounded-finding", "controller")
    case_id = run["manifest"]["case_id"]
    request = engine.send_message(SendMessage(run_id=run["run_id"], case_id=case_id,
        to_actor_id="customer", subject="Missing serial numbers",
        body="Please provide the serial numbers for the damaged components."),
        "e1-grounded-request", "viv")
    now = datetime.fromisoformat(store.get_run(run["run_id"])["simulated_time"])
    engine.advance(AdvanceClock(run_id=run["run_id"], until=now), "e1-grounded-response")
    reply = next(message for message in mail.sent if message["recipient_actor_id"] == "viv"
                 and message.get("in_reply_to") == request["message_id"])
    assert "did not include the serial number" in reply["body"]
    assert engine.evaluate(run["run_id"])["checks"]["customer_evidence_finding_recorded"] is False

    engine.update_case(run["run_id"], case_id, UpdateCase(customer_order_id="SO-1",
        status="in_review", customer_evidence={"sales_order_id": "SO-1",
            "missing_evidence": "serial_numbers"}), "e1-record-finding-before-read", "viv")
    assert engine.evaluate(run["run_id"])["checks"]["customer_evidence_finding_recorded"] is False
    engine.inbox(run["run_id"], "viv")
    evaluation = engine.evaluate(run["run_id"])
    assert evaluation["checks"]["customer_evidence_finding_recorded"] is True
    assert evaluation["checks"]["unresolved_evidence_case_in_review"] is True
    assert evaluation["checks"]["no_refund"] is True

    engine.update_case(run["run_id"], case_id, UpdateCase(status="closed"), "e1-close-unresolved", "viv")
    closed = engine.evaluate(run["run_id"])
    assert closed["checks"]["customer_evidence_finding_recorded"] is True
    assert closed["checks"]["unresolved_evidence_case_in_review"] is False
    assert closed["agent_performance"] == "incomplete"


def test_agent_case_api_exposes_current_company_date_without_pending_timeline(tmp_path):
    from fastapi.testclient import TestClient
    from servers.simulation.app import Principal, create_app

    engine, _store, mail, _erp = make_engine(tmp_path)
    run = engine.start(StartScenario(scenario_id="S01", agent_id="viv",
        agent_configuration={"start_time": "2026-10-06T09:00:00+00:00"}),
        "case-as-of-api", "controller")
    app = create_app(engine, {"agent-token": Principal(actor_id="viv", role="agent")})

    with TestClient(app) as client:
        response = client.get(f"/cases/S01-case?run_id={run['run_id']}",
                              headers={"Authorization": "Bearer agent-token"})

    assert response.status_code == 200
    assert response.json()["as_of"] == "2026-10-06T09:00:00+00:00"
    assert "pending_events" not in response.json()
    assert "timeline" not in response.json()
    assert "Company date (UTC): 2026-10-06" in mail.sent[0]["body"]
    assert "Day 14" not in mail.sent[0]["body"]
