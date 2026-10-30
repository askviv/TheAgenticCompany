import json
import hashlib

from servers.simulation.coffee_feedback import (
    CoffeeFeedbackAdapter,
    mapping_context_from_source,
    translate,
)
from servers.simulation.background_runtime import BackgroundWorkerClient
from servers.simulation.coffeebench_background import PROVIDER_ID, UPSTREAM_COMMIT
from types import SimpleNamespace


def _mapping(**changes):
    mapping = {
        "mapping_id": "map-1",
        "company_id": "company",
        "benchmark": "coffeebench",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": "item-price-1",
        "source_entity_type": "Item Price",
        "erp_doctype": "Item Price",
        "erp_name": "price-1",
        "experiment_id": None,
        "source_time": None,
        "company_time": "2026-10-30T12:00:00Z",
        "native_context": {
            "native_action": "set_retail_price",
            "events": ["on_submit"],
            "actor_id": "retailer_A",
            "native_ids": {"item_id": "roasted_coffee_kg"},
            "document_fields": {"price_per_unit": "price_list_rate"},
        },
    }
    mapping.update(changes)
    return mapping


def _evidence(**changes):
    evidence = {
        "evidence_id": "evidence-1",
        "company_id": "company",
        "erp_doctype": "Item Price",
        "erp_name": "price-1",
        "event": "on_submit",
        "actor": "employee-1",
        "occurred_at": "2026-10-30T12:00:00Z",
        "company_time": "2026-10-30T12:00:00Z",
        "document": {"price_list_rate": 18.75},
        "evidence_refs": ["erp://Item Price/price-1"],
        "message_id": None,
        "in_reply_to": None,
    }
    evidence.update(changes)
    return evidence


def test_translate_uses_explicit_native_item_id_and_exact_erp_field():
    translated = translate(_mapping(), _evidence())

    assert translated["status"] == "supported"
    assert translated["action"] == {
        "action_id": hashlib.sha256(b"map-1:evidence-1").hexdigest(),
        "mapping_id": "map-1",
        "evidence_id": "evidence-1",
        "benchmark": "coffeebench",
        "native_action": "set_retail_price",
        "actor_id": "retailer_A",
        "company_time": "2026-10-30T12:00:00Z",
        "arguments": {"item_id": "roasted_coffee_kg", "price_per_unit": 18.75},
        "supporting_evidence": [],
    }
    assert translated["coverage"] == [
        f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
        "BusinessApp.set_retail_price",
    ]


def test_translate_is_repeatable_for_same_mapping_and_evidence():
    first = translate(_mapping(), _evidence())
    second = translate(_mapping(), _evidence())

    assert first == second


def test_translate_refuses_to_guess_missing_native_context_or_fields():
    no_mapping = translate(_mapping(native_context=None), _evidence())
    missing_field = translate(
        _mapping(
            native_context={
                "native_action": "set_retail_price",
                "events": ["on_submit"],
                "actor_id": "retailer_A",
                "native_ids": {"item_id": "roasted_coffee_kg"},
                "document_fields": {"price_per_unit": "missing_field"},
            }
        ),
        _evidence(),
    )

    assert no_mapping["status"] == "inconclusive"
    assert missing_field["status"] == "inconclusive"
    assert no_mapping["action"] is None
    assert missing_field["action"] is None


def test_translate_reports_wrong_source_document_or_unmapped_action():
    other_document = translate(_mapping(), _evidence(erp_name="other"))
    unsupported = translate(
        _mapping(
            native_context={
                "native_action": "make_a_new_coffee_action",
                "events": ["on_submit"],
            }
        ),
        _evidence(),
    )

    assert other_document["status"] == "unmapped"
    assert unsupported["status"] == "unmapped"


def test_payment_entry_translates_only_exact_full_payment_of_native_ap_invoice():
    native_source = {
        "id": "source-invoice-event",
        "source": PROVIDER_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "kind": "invoice",
        "source_ref": "invoice-7",
        "occurred_at": "2026-10-30T12:00:00Z",
        "party": "counterparty",
        "currency": "USD",
        "amount": "100.00",
        "native_record_type": "invoice",
        "native_record_id": "invoice-7",
        "native_record": {
            "invoice_id": "invoice-7",
            "issuer_id": "farmer_A",
            "payer_id": "roaster_A",
            "amount": "100.00",
            "late_fees_accrued": "5.00",
            "credited_amount": "5.00",
            "paid": False,
            "returned": False,
        },
    }
    purchase_invoice = {
        "doctype": "Purchase Invoice",
        "name": "PINV-7",
        "custom_source_system": "coffeebench",
        "custom_source_id": "invoice-7",
        "custom_source_snapshot": json.dumps(native_source),
    }
    context = mapping_context_from_source(
        native_source, purchase_invoice, ["roaster_A"]
    )
    mapping = {
        "mapping_id": "map-invoice-7",
        "company_id": "company",
        "benchmark": "coffeebench",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": native_source["id"],
        "source_entity_type": "invoice",
        "erp_doctype": "Purchase Invoice",
        "erp_name": "PINV-7",
        "company_time": "2026-10-30T12:00:00Z",
        "native_context": context,
    }
    payment = _evidence(
        erp_doctype="Payment Entry",
        erp_name="PE-7",
        document={
            "docstatus": 1,
            "paid_from_account_currency": "USD",
            "paid_to_account_currency": "USD",
            "references": [
                {
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": "PINV-7",
                    "allocated_amount": 100.0,
                }
            ],
        },
    )

    translated = translate(mapping, payment)
    partial = translate(
        mapping,
        _evidence(
            erp_doctype="Payment Entry",
            erp_name="PE-8",
            document={
                "references": [
                    {
                        "reference_doctype": "Purchase Invoice",
                        "reference_name": "PINV-7",
                        "allocated_amount": 99.0,
                    }
                ]
            },
        ),
    )

    assert context["native_action"] == "pay_invoice"
    assert context["native_ids"] == {"invoice_id": "invoice-7"}
    assert translated["status"] == "supported"
    assert translated["action"]["arguments"] == {"invoice_id": "invoice-7"}
    assert translated["action"]["supporting_evidence"] == [{
        "invoice_id": "invoice-7", "payment_entry": "PE-7", "amount": "100.0",
        "currency": "USD", "docstatus": 1, "evidence_id": payment["evidence_id"],
    }]
    assert partial["status"] == "inconclusive"
    assert partial["action"] is None


def test_email_reply_translates_to_native_counterparty_message_from_mapped_invoice():
    native_source = {
        "id": "source-invoice-email-1",
        "source": PROVIDER_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "native_record_type": "invoice",
        "native_record_id": "invoice-email-1",
        "native_record": {
            "invoice_id": "invoice-email-1",
            "issuer_id": "farmer_A",
            "payer_id": "roaster_A",
            "amount": "100.00",
            "late_fees_accrued": "0.00",
            "credited_amount": "0.00",
            "paid": False,
            "returned": False,
        },
        "currency": "USD",
        "amount": "100.00",
        "kind": "invoice",
        "source_ref": "invoice-email-1",
        "occurred_at": "2026-10-30T12:00:00Z",
        "party": "farmer_A",
    }
    purchase_invoice = {
        "doctype": "Purchase Invoice",
        "name": "PINV-email-1",
        "custom_source_system": "coffeebench",
        "custom_source_id": "invoice-email-1",
        "custom_source_snapshot": json.dumps(native_source),
    }
    context = mapping_context_from_source(
        native_source, purchase_invoice, ["roaster_A"]
    )
    context["email_reply"]["allowed_mailbox_actor_ids"] = ["trusted-company-mailbox"]
    context["email_reply"]["allowed_recipient_actor_ids"] = ["farmer-mailbox"]
    mapping = {
        "mapping_id": "email-map-1",
        "company_id": "company",
        "benchmark": "coffeebench",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": native_source["id"],
        "source_entity_type": "invoice",
        "erp_doctype": "Purchase Invoice",
        "erp_name": "PINV-email-1",
        "company_time": "2026-10-30T12:00:00Z",
        "message_ids": ["<native-thread-1@example.test>"],
        "native_context": context,
    }
    evidence = _evidence(
        evidence_id="email-evidence-1",
        erp_doctype="Communication",
        erp_name="COMM-1",
        event="email_reply",
        actor="trusted-company-mailbox",
        authenticated_sender_actor_id="trusted-company-mailbox",
        recipient_actor_id="farmer-mailbox",
        message_id="<company-reply-2@example.test>",
        in_reply_to="<native-thread-1@example.test>",
        document={
            "subject": "RE: Invoice invoice-email-1",
            "body": "We will release the invoice after matching the delivered quantity.",
            "from": "untrusted-header-value@example.invalid",
        },
    )

    translated = translate(mapping, evidence)
    unrelated_thread = translate(
        mapping, {**evidence, "in_reply_to": "<unmapped-thread@example.test>"}
    )
    external_mailbox = translate(mapping, {**evidence, "actor": "supplier-mailbox"})
    spoofed_sender = translate(
        mapping, {**evidence, "authenticated_sender_actor_id": None}
    )
    internal_handoff = translate(mapping, {**evidence, "recipient_actor_id": "viv"})

    assert translated["status"] == "supported"
    assert translated["action"]["native_action"] == "send_message"
    assert translated["action"]["actor_id"] == "roaster_A"
    assert translated["action"]["arguments"] == {
        "recipient": "farmer_A",
        "title": "RE: Invoice invoice-email-1",
        "body": "We will release the invoice after matching the delivered quantity.",
    }
    assert (
        translated["action"]["action_id"]
        == hashlib.sha256(b"email-map-1:email-evidence-1").hexdigest()
    )
    assert "untrusted-header-value@example.invalid" not in str(translated["action"])
    assert unrelated_thread["status"] == "unmapped"
    assert unrelated_thread["action"] is None
    assert external_mailbox["status"] == "unmapped"
    assert external_mailbox["action"] is None
    assert spoofed_sender["status"] == "unmapped"
    assert "authenticated" in spoofed_sender["reason"]
    assert spoofed_sender["action"] is None
    assert internal_handoff["status"] == "unmapped"
    assert "recipient" in internal_handoff["reason"]
    assert internal_handoff["action"] is None

    adapter = CoffeeFeedbackAdapter(
        apply_action=lambda _action: {},
        reconcile_action=lambda _action: None,
        evaluate_native=lambda actors: [{"actor_ids": actors}],
    )
    report = adapter.evaluate([mapping], [evidence])
    assert report["translations"][0]["status"] == "supported"
    assert report["translations"][0]["action"]["native_action"] == "send_message"
    assert report["native_evaluations"] == [{"actor_ids": ["roaster_A"]}]


def test_authenticated_initial_email_requires_exact_mapped_document_and_counterparty():
    native_source = {
        "id": "source-invoice-initial-mail",
        "source": PROVIDER_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "native_record_type": "invoice",
        "native_record_id": "invoice-initial-mail",
        "native_record": {
            "invoice_id": "invoice-initial-mail", "issuer_id": "farmer_A",
            "payer_id": "roaster_A", "amount": "50.00", "late_fees_accrued": "0.00",
            "credited_amount": "0.00", "paid": False, "returned": False,
        },
        "currency": "USD", "amount": "50.00", "kind": "invoice",
    }
    erp_document = {
        "doctype": "Purchase Invoice", "name": "PINV-initial-mail",
        "custom_source_system": "coffeebench", "custom_source_id": "invoice-initial-mail",
        "custom_source_snapshot": json.dumps(native_source),
    }
    context = mapping_context_from_source(native_source, erp_document, ["roaster_A"])
    context["email_reply"]["allowed_mailbox_actor_ids"] = ["viv"]
    context["email_reply"]["allowed_recipient_actor_ids"] = ["coffee_farmer_A"]
    mapping = {
        "mapping_id": "initial-mail-map", "company_id": "company",
        "benchmark": "coffeebench", "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": native_source["id"], "source_entity_type": "invoice",
        "erp_doctype": "Purchase Invoice", "erp_name": "PINV-initial-mail",
        "company_time": "2026-10-30T12:00:00Z", "message_ids": [],
        "native_context": context,
    }
    message = {
        "subject": "Invoice follow-up",
        "body": "Please confirm the payment date for invoice invoice-initial-mail.",
        "related_document": {"doctype": "Purchase Invoice", "name": "PINV-initial-mail"},
    }
    evidence = _evidence(
        evidence_id="initial-mail-evidence", erp_doctype="Communication",
        erp_name="<initial-mail@example.test>", event="email_delivered",
        actor="viv", authenticated_sender_actor_id="viv",
        recipient_actor_id="coffee_farmer_A", message_id="<initial-mail@example.test>",
        document=message,
    )

    translated = translate(mapping, evidence)
    missing_reference = translate(
        mapping, {**evidence, "document": {"subject": message["subject"], "body": message["body"]}}
    )
    wrong_recipient = translate(mapping, {**evidence, "recipient_actor_id": "coffee_farmer_B"})
    unauthenticated = translate(mapping, {**evidence, "authenticated_sender_actor_id": None})

    assert translated["status"] == "supported"
    assert translated["action"]["arguments"] == {
        "recipient": "farmer_A", "title": "Invoice follow-up", "body": message["body"],
    }
    assert missing_reference["status"] == "unmapped"
    assert wrong_recipient["status"] == "unmapped"
    assert unauthenticated["status"] == "unmapped"


def test_coffee_feedback_adapter_reports_native_metric_and_projection_label():
    adapter = CoffeeFeedbackAdapter(
        apply_action=lambda action: {
            "action_id": action["action_id"],
            "accepted": True,
            "reconciled": True,
            "native_state_ref": "coffee-native:action-1",
            "outcome": {"status": "applied"},
        },
        reconcile_action=lambda action: None,
        evaluate_native=lambda actor_ids: [
            {
                "actor_id": actor_ids[0],
                "evaluation_type": "company_adapted_native_projection",
                "score_metric": "audit.annual.true_net_income",
                "score": 16.5,
            }
        ],
    )
    evaluation = adapter.evaluate([_mapping()], [_evidence()])

    assert (
        evaluation["projection_label"]
        == "company-adapted CoffeeBench native projection; ERP is authoritative"
    )
    assert (
        evaluation["native_evaluations"][0]["score_metric"]
        == "audit.annual.true_net_income"
    )
    assert evaluation["native_evaluations"][0]["score"] == 16.5


def test_pending_offer_mapping_requires_exact_native_offer_and_sales_order_facts():
    offer = SimpleNamespace(
        id="offer-9",
        listing_id="listing-9",
        buyer_id="retailer_A",
        seller_id="roaster_A",
        offered_price=12.5,
        qty=4,
        payment_terms_days=30,
        status="pending",
    )
    listing = SimpleNamespace(
        id="listing-9", seller_id="roaster_A", item_id="roasted_coffee_kg"
    )
    environment = SimpleNamespace(
        marketplace=SimpleNamespace(offers=[offer], listings=[listing]),
        business_apps={},
    )
    erp_order = {
        "doctype": "Sales Order",
        "native_offer_id": "offer-9",
        "payment_terms_days": 30,
        "items": [
            {"native_coffee_item_id": "roasted_coffee_kg", "qty": 4, "rate": 12.5}
        ],
    }

    context = mapping_context_from_source(environment, erp_order, ["roaster_A"])
    mismatch = mapping_context_from_source(
        environment,
        {**erp_order, "items": [{**erp_order["items"][0], "qty": 5}]},
        ["roaster_A"],
    )
    offer.status = "accepted"
    already_accepted = mapping_context_from_source(
        environment, erp_order, ["roaster_A"]
    )

    assert context["native_action"] == "accept_offer"
    assert context["native_ids"] == {"offer_id": "offer-9"}
    assert context["actor_id"] == "roaster_A"
    assert mismatch is None
    assert already_accepted is None


def test_imported_pending_offer_maps_only_exact_native_sales_order_terms():
    import json

    from servers.simulation.coffee_feedback import UPSTREAM_COMMIT

    source = {
        "id": "offer-source-9",
        "source": PROVIDER_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "kind": "offer",
        "currency": "USD",
        "native_record_type": "offer",
        "native_record_id": "offer-9",
        "native_record": {
            "offer_id": "offer-9", "listing_id": "listing-9",
            "buyer_id": "retailer_A", "seller_id": "roaster_A",
            "item_id": "roasted_coffee_kg", "quantity": 4,
            "unit_price": "12.50", "total_price": "50.00",
            "payment_terms_days": 30, "offer_status": "pending",
        },
    }
    erp = {
        "doctype": "Sales Order",
        "name": "SO-9",
        "custom_source_system": "coffeebench",
        "custom_source_id": "offer-9",
        "custom_native_offer_id": "offer-9",
        "custom_native_payment_terms_days": 30,
        "custom_source_snapshot": json.dumps(source),
        "items": [{
            "custom_native_coffee_item_id": "roasted_coffee_kg",
            "qty": 4.0,
            "rate": 12.5,
        }],
    }

    context = mapping_context_from_source(source, erp, ["roaster_A"])

    assert context["native_action"] == "accept_offer"
    assert context["events"] == ["on_submit"]
    assert context["actor_id"] == "roaster_A"
    assert context["native_ids"] == {"offer_id": "offer-9"}
    assert context["native_source_facts"]["payment_terms_days"] == 30
    assert context["email_reply"]["counterparty_id"] == "retailer_A"
    for invalid in (
        {**erp, "custom_native_offer_id": "offer-10"},
        {**erp, "custom_native_payment_terms_days": 45},
        {**erp, "items": [{**erp["items"][0], "rate": 12.51}]},
    ):
        assert mapping_context_from_source(source, invalid, ["roaster_A"]) is None


def test_source_deal_email_context_uses_exact_native_counterparty_and_references():
    deal_source = {
        "id": "deal-source",
        "source": PROVIDER_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "native_record_type": "deal",
        "native_record_id": "deal-1",
        "kind": "order",
        "native_record": {
            "deal_id": "deal-1",
            "seller_id": "roaster_A",
            "buyer_id": "retailer_A",
            "listing_id": "listing-1",
            "offer_id": "offer-1",
            "item_id": "roasted_coffee_kg",
        },
    }
    erp_order = {
        "doctype": "Sales Order",
        "custom_source_system": "coffeebench",
        "custom_source_id": "deal-1",
        "custom_source_snapshot": json.dumps(deal_source),
    }

    context = mapping_context_from_source(deal_source, erp_order, ["roaster_A"])
    ambiguous = mapping_context_from_source(
        deal_source, erp_order, ["roaster_A", "retailer_A"]
    )

    assert context["email_reply"]["actor_id"] == "roaster_A"
    assert context["email_reply"]["native_ids"] == {
        "recipient": "retailer_A",
        "ref_listing_id": "listing-1",
        "ref_offer_id": "offer-1",
    }
    assert ambiguous is None


def test_background_worker_client_validates_action_receipt_and_native_evaluation_shapes():
    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def json(self):
            return self.value

    class Session:
        trust_env = True

        def __init__(self):
            self.calls = []

        def post(self, url, *, json, headers, timeout):
            self.calls.append((url, json, headers, timeout))
            if url.endswith("/company-action"):
                return Response(
                    {
                        "action_id": "action-1",
                        "accepted": True,
                        "reconciled": True,
                        "native_state_ref": "coffee-native:1",
                        "outcome": {"status": "applied"},
                    }
                )
            if url.endswith("/company-action/reconcile"):
                return Response(None)
            return Response(
                [{"actor_id": "retailer_A", "status": "incomplete", "score": None}]
            )

    session = Session()
    client = BackgroundWorkerClient(
        "https://worker.example", "private-token", timeout_seconds=4, session=session
    )
    action = {
        "action_id": "action-1",
        "mapping_id": "map-1",
        "evidence_id": "evidence-1",
        "benchmark": "coffeebench",
        "native_action": "set_retail_price",
        "actor_id": "retailer_A",
        "company_time": "2026-10-30T12:00:30Z",
        "arguments": {"item_id": "roasted_coffee_kg", "price_per_unit": 18.75},
    }

    receipt = client.apply_company_action(action)
    assert client.reconcile_company_action(action) is None
    assert client.evaluate_coffee_businesses(["retailer_A"]) == [
        {"actor_id": "retailer_A", "status": "incomplete", "score": None}
    ]
    assert receipt["accepted"] and receipt["reconciled"]
    assert [call[0].rsplit("/", 1)[-1] for call in session.calls] == [
        "company-action",
        "reconcile",
        "evaluate",
    ]
    assert all(
        call[2]["Authorization"] == "Bearer private-token" for call in session.calls
    )
