"""Full S01 scripted-reference integration using real engine and fake provider edges."""

from decimal import Decimal
from pathlib import Path

import pytest
import requests

from servers.simulation.erpnext import VerifiedERPApproval, VerifiedShippingApproval
from servers.simulation.engine import SimulationEngine
from servers.simulation.reference import run_s01_reference
from servers.simulation.mail import SMTP4DevAPIError
from servers.simulation.store import SQLiteStore
from test_simulation_engine import FakeSiteAdmin, actor_records


class ImmediateProviderMail:
    """Mail transport boundary with immediate receipts and correlated inbox reads."""

    def __init__(self, actors, transient_read_kind="read_timeout"):
        self.transient_read_kind = transient_read_kind
        self.addresses = {actor["actor_id"]: actor["email"] for actor in actors}
        self.sent = []
        self.manager_request_operations = {}
        self.held_receipt_reply = None
        self.receipt_reply_released = False
        self.receipt_wait_count = 0
        self.transient_retry_wait_count = 0
        self.awaiting_transient_retry_wait = False
        self.shipping_approval_visible_before_receipt_release = False
        self.initial_message_id = None
        self.initial_submission_attempts = 0
        self.initial_submission_lost = False
        self.transient_receipt_read_raised = False
        self.notification_wait_count = 0

    def send(self, sender, recipient, subject, body, message_id, run_id, generation,
             *, in_reply_to=None, automated=False, on_submission_started=lambda: None):
        on_submission_started()
        if (sender["actor_id"] == "requester" and recipient["actor_id"] == "viv"
                and subject == "Please investigate the business case"):
            self.initial_submission_attempts += 1
        row = {"sender_actor_id": sender["actor_id"], "recipient_actor_id": recipient["actor_id"],
               "subject": subject, "body": body, "message_id": message_id, "run_id": run_id,
               "generation": generation, "in_reply_to": in_reply_to, "automated": automated}
        self.sent.append(row)
        if (sender["actor_id"] == "requester" and recipient["actor_id"] == "viv"
                and subject == "Please investigate the business case"):
            self.initial_message_id = message_id
            if not self.initial_submission_lost:
                self.initial_submission_lost = True
                raise ConnectionError("simulated response loss after provider accepted initial SMTP message")
        if recipient["actor_id"] == "manager" and body.startswith("APPROVAL REQUEST\n"):
            operation = next((line.partition("=")[2] for line in body.splitlines()
                              if line.startswith("operation=")), None)
            self.manager_request_operations[message_id] = operation
        if (sender["actor_id"] == "manager" and recipient["actor_id"] == "viv"
                and in_reply_to is not None
                and self.manager_request_operations.get(in_reply_to) == "create_goods_receipt"):
            self.held_receipt_reply = message_id
        return {"message_id": message_id, "subject": subject, "run_id": run_id,
                "generation": generation, "mailbox_uid": str(len(self.sent)),
                "recipient_actor_id": recipient["actor_id"]}

    def find_receipt(self, _recipient, message_id, subject, run_id, generation):
        if message_id == self.initial_message_id and not self.transient_receipt_read_raised:
            self.transient_receipt_read_raised = True
            self.awaiting_transient_retry_wait = True
            if self.transient_read_kind == "wrapped_connection_error":
                error = SMTP4DevAPIError("read message summary", 503)
                error.__cause__ = requests.ConnectionError("simulated transient capture connection error")
                raise error
            raise requests.ReadTimeout("simulated transient receipt lookup read timeout")
        return next(({"message_id": row["message_id"], "subject": row["subject"],
                      "run_id": row["run_id"], "generation": row["generation"],
                      "mailbox_uid": str(index), "recipient_actor_id": row["recipient_actor_id"]}
                     for index, row in enumerate(self.sent, 1)
                     if row["message_id"] == message_id and row["subject"] == subject
                     and row["run_id"] == run_id and row["generation"] == generation), None)

    def poll(self, _actor, _after_uid, _run_id, _generation, _known_ids):
        return []

    def release_receipt_reply(self, _after_version, _timeout):
        self.notification_wait_count += 1
        if self.awaiting_transient_retry_wait:
            self.transient_retry_wait_count += 1
            self.awaiting_transient_retry_wait = False
        if self.held_receipt_reply is not None and not self.receipt_reply_released:
            assert self.shipping_approval_visible_before_receipt_release
            self.receipt_reply_released = True
            self.receipt_wait_count += 1
        return self.receipt_reply_released

    def poll_viv(self, *, run_id, generation, known_ids):
        del known_ids
        visible = [row for row in self.sent
            if row["recipient_actor_id"] == "viv" and row["run_id"] == run_id
            and row["generation"] == generation
            and (row["message_id"] != self.held_receipt_reply or self.receipt_reply_released)]
        if any(row["sender_actor_id"] == "manager" and row["message_id"] != self.held_receipt_reply
               and self.manager_request_operations.get(row["in_reply_to"]) == "approve_expedited_shipping"
               and "Finance decision: APPROVED" in row["body"] for row in visible):
            self.shipping_approval_visible_before_receipt_release = True
        return [{"message_id": row["message_id"], "subject": row["subject"], "body": row["body"],
                 "from": self.addresses[row["sender_actor_id"]],
                 "to": self.addresses[row["recipient_actor_id"]],
                 "in_reply_to": row["in_reply_to"],
                 "references": row["in_reply_to"] or "", "run_id": row["run_id"],
                 "generation": row["generation"], "mailbox_uid": str(index),
                 "webmail_url": f"https://mail.example.test/messages/{row['message_id'].strip('<>')}"}
                for index, row in enumerate(self.sent, 1)
                if row in visible]


class ReferenceERPBoundary:
    """Small, stateful native ERP contract fake; policy and decisions remain real."""

    def __init__(self):
        self.po_id = "PO-1"
        self.pi_id = "PI-1"
        self.pr_id = "PR-1"
        self.supplier_id = "SUP-1"
        self.item_id = "ITEM-1"
        self.warehouse_id = "WH-1"
        self.po_version = "po-v1"
        self.ordered_qty = "10000"
        self.rate = "50"
        self.received_qty = Decimal("8000")
        self.stock_qty = Decimal("8000")
        self.invoice_state = "Draft"
        self.receipt_by_key = {}
        self.receipts = {}
        self.shipping_by_key = {}
        self.shipping_orders = {}
        self.receipt_grants = []
        self.shipping_grants = []
        self.review_calls = []
        self.invoice = {"name": self.pi_id, "modified": "pi-v1", "docstatus": 1,
            "workflow_state": self.invoice_state, "currency": "GBP", "outstanding_amount": "500000",
            "grand_total": "500000", "items": [{"purchase_order": self.po_id,
                "purchase_order_item": "PO-ROW-1", "purchase_receipt": self.pr_id,
                "item_code": self.item_id, "qty": "10000"}]}
        self.initial_receipt = {"name": self.pr_id, "docstatus": 1, "posting_date": "2026-10-06",
            "posting_time": "09:00:00", "items": [{"purchase_order": self.po_id,
                "purchase_order_item": "PO-ROW-1", "item_code": self.item_id,
                "qty": "8000", "received_qty": "8000", "warehouse": self.warehouse_id}]}

    def get_purchase_order(self, order_id):
        if order_id in self.shipping_orders:
            return self.shipping_orders[order_id]
        if order_id != self.po_id:
            raise LookupError("test ERP order not found")
        return {"name": self.po_id, "modified": self.po_version, "docstatus": 1,
            "status": "To Receive and Bill", "supplier": self.supplier_id, "currency": "GBP",
            "items": [{"name": "PO-ROW-1", "item_code": self.item_id, "qty": self.ordered_qty,
                "received_qty": str(self.received_qty), "rate": self.rate, "warehouse": self.warehouse_id,
                "schedule_date": "2026-10-06"}]}

    def get_purchase_invoice(self, invoice_id):
        assert invoice_id == self.pi_id
        return {**self.invoice, "workflow_state": self.invoice_state}

    def assess_invoice(self, invoice_id):
        assert invoice_id == self.pi_id
        outstanding = max(Decimal("10000") - self.received_qty, Decimal(0))
        return {"lines": [{"purchase_order_id": self.po_id, "purchase_invoice_id": self.pi_id,
            "billed_qty": Decimal("10000"), "unreceived_billed_qty": outstanding,
            "receipt_ids": [self.pr_id, *(["GR-1"] if self.receipts else [])]}]}

    def get_purchase_receipt(self, receipt_id):
        if receipt_id == self.pr_id:
            return self.initial_receipt
        if receipt_id in self.receipts:
            return self.receipts[receipt_id]
        raise LookupError("test ERP receipt not found")

    def request_invoice_review(self, invoice_id, idempotency_key):
        assert invoice_id == self.pi_id
        self.review_calls.append((invoice_id, idempotency_key))
        self.invoice_state = "Review Requested"
        return self.get_purchase_invoice(invoice_id)

    def read_payment_status(self, invoice_id):
        assert invoice_id == self.pi_id
        return {"purchase_invoice_id": invoice_id, "payments": []}

    def get_stock_balance(self, item_id, warehouse_id):
        assert (item_id, warehouse_id) == (self.item_id, self.warehouse_id)
        return self.stock_qty

    def get_sales_order(self, order_id):
        assert order_id == "SO-1"
        return {"name": order_id, "docstatus": 1}

    def get_customer(self, customer_id):
        return {"name": customer_id}

    def read_sales_invoice_refunds(self, sales_order_id):
        assert sales_order_id == "SO-1"
        return []

    def get_supplier(self, supplier_id):
        assert supplier_id == self.supplier_id
        return {"name": supplier_id}

    def create_goods_receipt(self, purchase_order_id, quantity, idempotency_key, verified_approval):
        assert isinstance(verified_approval, VerifiedERPApproval)
        self.receipt_grants.append(verified_approval)
        assert purchase_order_id == self.po_id
        assert quantity == Decimal("2000")
        assert verified_approval.expected_modified == self.po_version
        if idempotency_key in self.receipt_by_key:
            return self.receipt_by_key[idempotency_key]
        receipt = {"name": "GR-1", "docstatus": 1, "purchase_order": self.po_id,
            "posting_date": verified_approval.posting_date, "posting_time": verified_approval.posting_time,
            "items": [{"purchase_order": self.po_id, "purchase_order_item": "PO-ROW-1",
                "item_code": self.item_id, "qty": "2000", "received_qty": "2000",
                "warehouse": self.warehouse_id}]}
        self.receipt_by_key[idempotency_key] = receipt
        self.receipts[receipt["name"]] = receipt
        self.received_qty += quantity
        self.stock_qty += quantity
        self.po_version = "po-v2"
        return receipt

    def commit_expedited_shipping(self, verified_approval, idempotency_key):
        assert isinstance(verified_approval, VerifiedShippingApproval)
        self.shipping_grants.append(verified_approval)
        assert verified_approval.purchase_order_id == self.po_id
        assert verified_approval.expected_modified == self.po_version
        assert verified_approval.supplier == self.supplier_id
        assert verified_approval.amount == Decimal("750.00")
        if idempotency_key in self.shipping_by_key:
            return self.shipping_by_key[idempotency_key]
        order_id = "PO-SHIP-1"
        order = {"name": order_id, "modified": "ship-v1", "docstatus": 1,
            "status": "To Receive and Bill", "supplier": self.supplier_id, "company": "Example Ltd",
            "transaction_date": verified_approval.posting_date, "currency": "GBP",
            "total": "750.00", "grand_total": "750.00",
            "items": [{"name": "PO-SHIP-ROW-1", "item_code": "EXP-SHIP-SVC", "qty": "1",
                "rate": "750.00", "amount": "750.00", "warehouse": None,
                "schedule_date": verified_approval.delivery_commitment, "uom": "Service",
                "stock_uom": "Service", "conversion_factor": "1", "stock_qty": "1"}],
            "approval_id": verified_approval.approval_id, "quote_id": verified_approval.quote_id,
            "shipment_id": verified_approval.shipment_id, "fee_amount": "750.00",
            "fee_currency": "GBP", "delivery_commitment": verified_approval.delivery_commitment}
        self.shipping_orders[order_id] = order
        self.shipping_by_key[idempotency_key] = order
        return order


@pytest.mark.parametrize("transient_read_kind", ["read_timeout", "wrapped_connection_error"])
def test_scripted_s01_reference_recovers_transient_receipt_read_without_resending(tmp_path, transient_read_kind):
    actors = actor_records()
    actors = [actor for actor in actors if actor["actor_id"] != "viv"]
    actors.insert(0, {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "agent"})
    supplier = next(actor for actor in actors if actor["actor_id"] == "supplier")
    supplier["private_state"]["pricing_config"]["stance"] = "neutral"
    mail = ImmediateProviderMail(actors, transient_read_kind=transient_read_kind)
    erp = ReferenceERPBoundary()
    store = SQLiteStore(tmp_path / "reference.sqlite")
    engine = SimulationEngine(store, mail, lambda _binding: erp, FakeSiteAdmin(), actors)
    # The production runner uses event notifications. This deterministic fake
    # provider delivers messages in the same call, so no waiter call is needed.
    engine.viv_activity_version = lambda: 0
    engine.wait_for_viv_activity = mail.release_receipt_reply
    report_path = tmp_path / "private" / "s01-reference.json"

    report = run_s01_reference(engine, start_key="test-s01-reference", controller_actor_id="controller",
                               seed=17, report_path=report_path)

    assert report["trajectory"] == "scripted_reference"
    assert report["actor_mode"] == "scripted_reference"
    assert report["agent_execution"] == "not_measured"
    assert report["model_cost"] is None
    assert report["evaluation"]["agent_performance"] == "pass"
    assert report["evaluation"]["checks"] and all(report["evaluation"]["checks"].values())
    assert report["evaluation"]["environment_drift"] is None
    assert len(erp.receipt_grants) == len(erp.shipping_grants) == 1
    assert mail.receipt_wait_count == 1
    assert mail.transient_retry_wait_count == 1
    assert mail.notification_wait_count >= 2
    assert mail.initial_submission_attempts == 1
    assert mail.transient_receipt_read_raised
    assert mail.shipping_approval_visible_before_receipt_release
    assert mail.receipt_reply_released
    assert erp.receipt_grants[0].posting_date == "2026-10-19"
    assert erp.shipping_orders["PO-SHIP-1"]["grand_total"] == "750.00"
    assert report["native_business_state"]["shipping_invoice_or_payment_posted_by_reference"] is False
    assert report_path.stat().st_mode & 0o077 == 0
    assert all(message["run_id"] == report["run_id"] for message in mail.sent)
    audit = engine.store.list_audit(report["run_id"], 0)
    decisions = [entry["private_record"] for entry in audit
                 if entry["private_record"].get("event") == "approval_decision"]
    completed_financial_actions = [entry["private_record"].get("operation") for entry in audit
        if entry["private_record"].get("event") == "approval_action_completed"]
    assert len(decisions) == 3
    assert completed_financial_actions.count("create_goods_receipt") == 1
    assert completed_financial_actions.count("commit_expedited_shipping") == 1
    assert "post_payment" not in completed_financial_actions
    for attribute, changed in (("rate", "51"), ("ordered_qty", "9999")):
        original = getattr(erp, attribute)
        setattr(erp, attribute, changed)
        try:
            assert engine.evaluate(report["run_id"])["checks"]["component_purchase_order_unchanged"] is False
        finally:
            setattr(erp, attribute, original)


def test_mailbox_auth_failure_is_not_retried_as_a_transient_read():
    class RejectedCaptureEngine:
        wait_count = 0

        def pump(self, _run_id):
            error = SMTP4DevAPIError("list messages", 401)
            error.__cause__ = ValueError("unauthorized")
            raise error

        def inbox(self, _run_id, _actor_id):
            return []

        def viv_activity_version(self):
            return 0

        def wait_for_viv_activity(self, _after_version, _timeout):
            self.wait_count += 1

    engine = RejectedCaptureEngine()
    with pytest.raises(SMTP4DevAPIError) as raised:
        from servers.simulation.reference import _wait_for_agent_message
        _wait_for_agent_message(engine, "run", "viv", from_actor="requester",
                                contains="instruction", timeout_seconds=10)
    assert raised.value.status == 401
    assert engine.wait_count == 0
