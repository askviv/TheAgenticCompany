"""End-to-end scenario contracts with fake external ERP and mailbox boundaries."""

from datetime import datetime, timedelta
import re

from servers.simulation.engine import SimulationEngine
from servers.simulation.models import AdvanceClock, SendMessage, StartScenario, UpdateCase
from servers.simulation.store import SQLiteStore
from test_simulation_engine import FakeERP, FakeMail, FakeSiteAdmin, actor_records


class ProviderReceiptFakeMail(FakeMail):
    """Deterministic capture-provider receipt boundary; no live provider is used."""

    def poll_viv(self, *, run_id, generation, known_ids):
        return [
            {
                "message_id": item["message_id"],
                "subject": item["subject"],
                "body": item["body"],
                "from": item["sender_actor_id"],
                "to": item["recipient_actor_id"],
                "in_reply_to": item["in_reply_to"],
                "references": item["in_reply_to"] or "",
                "run_id": item["run_id"],
                "generation": item["generation"],
                "mailbox_uid": str(index),
                "webmail_url": "https://mail.example.test/message/" + item["message_id"].strip("<>"),
            }
            for index, item in enumerate(self.sent, start=1)
            if item["recipient_actor_id"] == "viv"
            and item["run_id"] == run_id
            and item["generation"] == generation
        ]


def make_acceptance_engine(tmp_path):
    store = SQLiteStore(tmp_path / "scenario-acceptance.sqlite")
    mail = ProviderReceiptFakeMail()
    erp = FakeERP()
    actors = actor_records()
    supplier = next(actor for actor in actors if actor["kind"] == "supplier")
    supplier["private_state"]["pricing_config"]["stance"] = "neutral"
    engine = SimulationEngine(store, mail, lambda _binding: erp, FakeSiteAdmin(), actors)
    return engine, store, mail, erp


def start(engine, scenario):
    run = engine.start(
        StartScenario(scenario_id=scenario, seed=17, agent_id="viv"),
        f"acceptance-start-{scenario}",
        "controller",
    )
    return run


def advance_day(engine, run, day_number, key):
    return engine.advance(
        AdvanceClock(
            run_id=run["run_id"],
            until=datetime.fromisoformat(run["simulated_time"]) + timedelta(days=day_number - 1),
        ),
        key,
    )


def agent_messages(engine, run):
    return engine.inbox(run["run_id"], "viv")


def send(engine, run, to, subject, body, key):
    return engine.send_message(
        SendMessage(
            run_id=run["run_id"],
            case_id=run["manifest"]["case_id"],
            to_actor_id=to,
            subject=subject,
            body=body,
        ),
        key,
        "viv",
    )


def assert_scenario_passed(engine, run):
    result = engine.evaluate(run["run_id"])
    assert result["agent_performance"] == "pass", result["checks"]
    assert result["checks"] and all(result["checks"].values()), result["checks"]


def test_a1_agent_reconciles_invoice_documents_and_requests_review(tmp_path):
    engine, _store, _mail, erp = make_acceptance_engine(tmp_path)
    # Native ERP boundary response includes the invoice's visible billed line.
    original_invoice_read = erp.get_purchase_invoice

    def get_invoice_with_visible_line(invoice_id):
        return {
            **original_invoice_read(invoice_id),
            "items": [{"purchase_order": "PO-1", "item_code": "ITEM-1", "qty": 10_000}],
        }

    erp.get_purchase_invoice = get_invoice_with_visible_line
    run = start(engine, "A1")
    case_id = run["manifest"]["case_id"]

    order = engine.erp_read(run["run_id"], "get_purchase_order", "PO-1", "viv")
    invoice = engine.erp_read(run["run_id"], "get_purchase_invoice", "PI-1", "viv")
    receipt = engine.erp_read(run["run_id"], "get_purchase_receipt", "PR-1", "viv")
    billed = invoice["items"][0]["qty"]
    received = order["items"][0]["received_qty"]
    assert receipt["items"][0]["received_qty"] == received
    assert billed - received == 2_000

    engine.update_case(
        run["run_id"],
        case_id,
        UpdateCase(invoice_finding={
            "purchase_order_id": order["name"],
            "purchase_invoice_id": invoice["name"],
            "quantity_difference": billed - received,
            "receipt_ids": [receipt["name"]],
        }),
        "a1-evidence-finding",
        "viv",
    )
    engine.request_review(run["run_id"], invoice["name"], "a1-review", "viv")

    assert_scenario_passed(engine, run)


def test_b1_agent_reads_logistics_notice_updates_eta_and_reads_arrival(tmp_path):
    engine, store, _mail, _erp = make_acceptance_engine(tmp_path)
    run = start(engine, "B1")

    advance_day(engine, run, 2, "b1-delay")
    delay_notices = [
        item for item in agent_messages(engine, run)
        if item["from"] == "logistics" and "revised ETA" in item["body"]
    ]
    assert len(delay_notices) == 1
    revised_eta = re.search(r"revised ETA is (\d{4}-\d{2}-\d{2})", delay_notices[0]["body"]).group(1)
    shipment_id = re.search(r"Shipment ([^ ]+) for case", delay_notices[0]["body"]).group(1)
    engine.update_case(
        run["run_id"], run["manifest"]["case_id"], UpdateCase(eta=revised_eta),
        "b1-record-revised-eta", "viv",
    )
    eta_notice = send(
        engine, run, "requester", "Revised supplier delivery ETA",
        f"Logistics confirmed shipment {shipment_id} has a revised ETA of {revised_eta}; I have updated the case and will monitor delivery.",
        "b1-notify-requester-eta",
    )

    advance_day(engine, run, 5, "b1-arrival")
    arrival_notices = [
        item for item in agent_messages(engine, run)
        if item["from"] == "warehouse" and "units arrived" in item["body"]
    ]
    assert len(arrival_notices) == 1
    notice = next(
        item for item in store.list_outbox(run["run_id"], 0, status="completed")
        if item["private_payload"].get("message_id") == eta_notice["message_id"]
    )
    delay_event = next(
        event for event in store.list_events(run["run_id"], 0)
        if event["event_type"] == "SHIPMENT_DELAYED"
    )
    arrival_event = next(
        event for event in store.list_events(run["run_id"], 0)
        if event["event_type"] == "DELIVERY_ARRIVED"
    )
    assert revised_eta in notice["private_payload"]["body"]
    assert datetime.fromisoformat(delay_event["simulated_time"]) <= datetime.fromisoformat(
        notice["private_payload"]["simulated_time"]
    ) < datetime.fromisoformat(arrival_event["simulated_time"])

    assert_scenario_passed(engine, run)


def test_c1_agent_requests_and_reads_supplier_dispatch_evidence(tmp_path):
    engine, _store, _mail, _erp = make_acceptance_engine(tmp_path)
    run = start(engine, "C1")
    sent = send(
        engine, run, "supplier", "Dispatch evidence for PO-1",
        "Please provide the dispatch quantity and supporting shipment evidence for this purchase order.",
        "c1-request-evidence",
    )
    advance_day(engine, run, 1, "c1-process-supplier")

    replies = [
        item for item in agent_messages(engine, run)
        if item["from"] == "supplier" and item["in_reply_to"] == sent["message_id"]
    ]
    assert len(replies) == 1
    assert "10000 components dispatched" in replies[0]["body"]
    assert "MAN-1" in replies[0]["body"]

    assert_scenario_passed(engine, run)


def test_d1_agent_reads_future_quote_routes_terms_for_manager_review(tmp_path):
    engine, _store, _mail, _erp = make_acceptance_engine(tmp_path)
    run = start(engine, "D1")
    advance_day(engine, run, 2, "d1-pricing-disruption")
    request = send(
        engine, run, "supplier", "Request for revised quotation",
        "Please provide a revised unit price quotation for the existing purchase order.",
        "d1-request-quote",
    )
    advance_day(engine, run, 2, "d1-process-quote")

    replies = [
        item for item in agent_messages(engine, run)
        if item["from"] == "supplier" and item["in_reply_to"] == request["message_id"]
    ]
    assert len(replies) == 1
    amount_match = re.search(r"revised quotation is ([0-9]+\.[0-9]{2}) per", replies[0]["body"])
    assert amount_match
    amount = amount_match.group(1)
    quote_message_id = replies[0]["message_id"]
    engine.update_case(
        run["run_id"], run["manifest"]["case_id"], UpdateCase(notes=quote_message_id),
        "d1-link-quote", "viv",
    )
    send(
        engine, run, "manager", "Approval request for supplier quotation",
        ("APPROVAL REQUEST\noperation=amend_purchase_order\nentity_id=PO-1\n"
         f"entity_version=po-v1\ncase_id={run['manifest']['case_id']}\n"
         f"amount={amount}\ncurrency=GBP\nquote_message_id={quote_message_id}"),
        "d1-route-quote",
    )
    advance_day(engine, run, 3, "d1-manager-review")

    assert_scenario_passed(engine, run)


def test_e1_agent_requests_customer_serials_and_links_sales_order(tmp_path):
    engine, _store, _mail, _erp = make_acceptance_engine(tmp_path)
    run = start(engine, "E1")
    case_id = run["manifest"]["case_id"]
    initial = [
        item for item in agent_messages(engine, run)
        if item["from"] == "customer" and "arrived damaged" in item["body"]
    ]
    assert len(initial) == 1
    instructions = [
        item for item in agent_messages(engine, run)
        if item["from"] == "requester" and "Customer order reference:" in item["body"]
    ]
    assert len(instructions) == 1
    order_match = re.search(r"Customer order reference: ([^\s]+)", instructions[0]["body"])
    assert order_match
    sales_order_id = order_match.group(1)
    request = send(
        engine, run, "customer", "Serial numbers for damaged components",
        "Please provide the serial numbers for the damaged components.",
        "e1-request-serials",
    )
    advance_day(engine, run, 1, "e1-process-customer")
    replies = [
        item for item in agent_messages(engine, run)
        if item["from"] == "customer" and item["in_reply_to"] == request["message_id"]
    ]
    assert len(replies) == 1
    assert "did not include the serial number" in replies[0]["body"]

    native_order = engine.erp_read(run["run_id"], "get_sales_order", sales_order_id, "viv")
    assert native_order["name"] == sales_order_id
    engine.update_case(
        run["run_id"], case_id,
        UpdateCase(
            customer_order_id=sales_order_id,
            status="in_review",
            customer_evidence={
                "sales_order_id": sales_order_id,
                "missing_evidence": "serial_numbers",
            },
        ),
        "e1-link-sales-order", "viv",
    )

    assert_scenario_passed(engine, run)

    engine.update_case(
        run["run_id"], case_id, UpdateCase(status="closed"),
        "e1-close-unresolved-evidence", "viv",
    )
    closed = engine.evaluate(run["run_id"])
    assert closed["checks"]["unresolved_evidence_case_in_review"] is False
    assert closed["agent_performance"] == "incomplete"
