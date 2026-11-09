"""Reconcile CoffeeBench source facts into native, reviewable ERP documents.

This is an import boundary: it creates draft orders/invoices and source-linked
payment evidence and bank statement lines. It never posts a Payment Entry.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
import json
import re
from email import policy
from email.parser import Parser
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from finance_handoffs import delegate_finance_task, finance_task_message_id, send_finance_queues
from source_records import ensure_source_fields, get_source_document, insert_source_document
from native_accounts import ensure_simulated_bank, map_party_to_currency, submit_imported_bank_transaction


SOURCE_SYSTEM = "coffeebench"
COFFEE_PROVIDER_ID = "sakanaai-coffeebench-native-economy"
ECOMMERCE_PROVIDER_ID = "qwenlm-ecommercebench-calendar"
SOURCE_ITEMS = {
    "green_coffee_kg": "Green coffee beans (per kg)",
    "roasted_coffee_kg": "Roasted coffee beans (per kg)",
    "green_specialty_kg": "Specialty green coffee beans (per kg)",
    "roasted_specialty_kg": "Roasted specialty coffee beans (per kg)",
}


def ensure_finance_source_contract() -> None:
    """Install the standard source identity fields used by this adapter.

    Call during an explicit schema/setup transaction before importing source
    facts. Frappe may issue DDL while adding these fields.
    """
    for doctype, anchor in (
        ("Item", "item_name"), ("Customer", "customer_name"), ("Supplier", "supplier_name"),
        ("Sales Order", "terms"), ("Purchase Order", "terms"),
        ("Sales Invoice", "remarks"), ("Purchase Invoice", "remarks"), ("Issue", "subject"),
        ("Bank Transaction", "description"),
    ):
        ensure_source_fields(doctype, anchor)
    _ensure_offer_fields()


def _ensure_offer_fields() -> None:
    """Keep Coffee pending-offer provenance in native, queryable ERP fields."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    fields = {
        "Sales Order": [
            {"fieldname": "custom_native_offer_id", "label": "CoffeeBench Offer ID",
             "fieldtype": "Data", "insert_after": "custom_source_snapshot",
             "read_only": 1, "hidden": 1, "no_copy": 1},
            {"fieldname": "custom_native_payment_terms_days", "label": "CoffeeBench Offered Payment Terms (Days)",
             "fieldtype": "Int", "insert_after": "custom_native_offer_id",
             "read_only": 1, "hidden": 1, "no_copy": 1},
        ],
        "Sales Order Item": [
            {"fieldname": "custom_native_coffee_item_id", "label": "CoffeeBench Item ID",
             "fieldtype": "Data", "insert_after": "item_code",
             "read_only": 1, "hidden": 1, "no_copy": 1},
        ],
    }
    for doctype, entries in fields.items():
        meta = frappe.get_meta(doctype)
        installed_in_batch = set()
        for field in entries:
            anchor = field["insert_after"]
            if not meta.has_field(anchor) and anchor not in installed_in_batch:
                raise RuntimeError(f"Native {doctype} lacks the offer-source field anchor: {anchor}")
            installed_in_batch.add(field["fieldname"])
    create_custom_fields(fields, update=True)
    for doctype, entries in fields.items():
        meta = frappe.get_meta(doctype)
        if any(not meta.has_field(field["fieldname"]) for field in entries):
            raise RuntimeError(f"Native {doctype} offer-source fields were not installed")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"CoffeeBench source {field} is not a valid amount") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"CoffeeBench source {field} must be a finite non-negative amount")
    return result


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    raise ValueError("CoffeeBench source date is missing or invalid")


def _company_currency(company: str) -> str:
    currency = frappe.db.get_value("Company", company, "default_currency")
    if not currency:
        raise RuntimeError("Native ERP company has no default currency")
    return currency


def _conversion_rate(currency: str, company_currency: str, posting_date: str, direction: str) -> Decimal:
    if currency == company_currency:
        return Decimal("1")
    from erpnext.setup.utils import get_exchange_rate

    rate = get_exchange_rate(currency, company_currency, posting_date, args=direction)
    if not rate:
        raise RuntimeError("Native ERP has no exchange rate for the source posting date")
    return _decimal(rate, "exchange rate")


def _item(record: dict[str, Any], item_map: dict[str, str], context: dict[str, Any]) -> tuple[str, Decimal, Decimal]:
    facts = record.get("native_record") or {}
    item_id = str(facts.get("item_id", ""))
    item_name = SOURCE_ITEMS.get(item_id)
    if not item_name:
        raise ValueError("CoffeeBench source item is outside the pinned upstream catalog")
    item_code = item_map.get(item_id) or item_id
    qty = _decimal(facts.get("quantity"), "quantity")
    unit_price = _decimal(facts.get("unit_price"), "unit price")
    if not item_id or qty <= 0:
        raise ValueError("A CoffeeBench order requires a source item and positive quantity")
    if not frappe.db.exists("Item", item_code):
        group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        uom = frappe.db.get_value("UOM", {"name": "Kg"}, "name")
        if not group or not uom:
            raise RuntimeError("Native ERP lacks the Item Group or unit needed for a source item")
        ensure_source_fields("Item", "item_name")
        insert_source_document(
            "Item", {"item_code": item_code, "item_name": item_name, "item_group": group,
                     "stock_uom": uom, "is_stock_item": 1},
            source_system=SOURCE_SYSTEM, source_id=item_id, record_type="item",
            task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
            snapshot={"item_id": item_id, "source": SOURCE_SYSTEM},
        )
    return item_code, qty, unit_price


def _source_values(record: dict[str, Any]) -> dict[str, Any]:
    # Preserve the entire upstream record as immutable provenance.
    return dict(record)


def _ensure_party(identity: str, doctype: str, context: dict[str, Any], currency: str) -> dict[str, str]:
    def attach_directory_contact(party: dict[str, str]) -> dict[str, str]:
        _ensure_counterparty_contact(identity, doctype, party["name"], context)
        return party

    configured = context["party_map"].get(identity)
    if configured:
        if configured.get("doctype") != doctype or not frappe.db.exists(doctype, configured.get("name")):
            raise ValueError("Configured CoffeeBench counterparty mapping does not resolve to the required native party")
        party = frappe.get_doc(doctype, configured["name"])
        map_party_to_currency(party, currency, company=context["company"], newly_created=False)
        return attach_directory_contact(configured)
    name_field = "supplier_name" if doctype == "Supplier" else "customer_name"
    existing = frappe.db.get_value(doctype, {name_field: identity}, "name")
    if existing:
        party = frappe.get_doc(doctype, existing)
        map_party_to_currency(party, currency, company=context["company"], newly_created=False)
        return attach_directory_contact({"doctype": doctype, "name": existing})
    group_doctype = "Supplier Group" if doctype == "Supplier" else "Customer Group"
    group = frappe.db.get_value(group_doctype, {"is_group": 0}, "name")
    if not group:
        raise RuntimeError(f"Native ERP has no leaf {group_doctype} for source counterparties")
    values = ({"supplier_name": identity, "supplier_group": group, "supplier_type": "Company",
               "default_currency": currency}
              if doctype == "Supplier" else
              {"customer_name": identity, "customer_group": group, "customer_type": "Company",
               "default_currency": currency})
    ensure_source_fields(doctype, "supplier_name" if doctype == "Supplier" else "customer_name")
    doc, _ = insert_source_document(
        doctype, values, source_system=SOURCE_SYSTEM, source_id=identity, record_type="counterparty",
        task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
        snapshot={"business_id": identity, "source": SOURCE_SYSTEM},
    )
    map_party_to_currency(doc, currency, company=context["company"], newly_created=True)
    return attach_directory_contact({"doctype": doctype, "name": doc.name})


def _ensure_counterparty_contact(
    business_id: str, party_doctype: str, party_name: str, context: dict[str, Any],
) -> str:
    """Attach the configured simulator mailbox as a native party Contact once."""
    directory = context.get("counterparty_mail_directory")
    record = directory.get(business_id) if isinstance(directory, dict) else None
    if not isinstance(record, dict):
        raise ValueError("CoffeeBench counterparty has no explicit company mail-directory mapping")
    actor_id, email, display_name = (
        record.get("actor_id"), record.get("email"), record.get("display_name")
    )
    if (
        not isinstance(actor_id, str) or not actor_id
        or not isinstance(email, str) or "@" not in email
        or not isinstance(display_name, str) or not display_name
        or record.get("kind") != "counterparty"
    ):
        raise ValueError("CoffeeBench counterparty mail-directory entry is invalid")
    parents = sorted({
        row["parent"] for row in frappe.get_all(
            "Contact Email", filters={"email_id": email}, fields=["parent"]
        )
    })
    if len(parents) > 1:
        raise ValueError("CoffeeBench counterparty email is already linked to multiple native Contacts")
    if parents:
        contact = frappe.get_doc("Contact", parents[0])
        linked_parties = {
            (link.link_doctype, link.link_name) for link in contact.links
        }
        if (party_doctype, party_name) not in linked_parties:
            raise ValueError("CoffeeBench counterparty email is already linked to a different native party")
        return contact.name

    contact = frappe.get_doc({
        "doctype": "Contact",
        "first_name": display_name,
        "email_ids": [{"email_id": email, "is_primary": 1}],
        "links": [{"link_doctype": party_doctype, "link_name": party_name}],
    })
    contact.insert(ignore_permissions=True)
    return contact.name


def _insert_order(record: dict[str, Any], context: dict[str, Any]) -> tuple[str, bool]:
    facts = record.get("native_record") or {}
    buyer_is_company = facts.get("buyer_id") in context["company_business_ids"]
    seller_is_company = facts.get("seller_id") in context["company_business_ids"]
    if buyer_is_company == seller_is_company:
        return "", False
    doctype = "Purchase Order" if buyer_is_company else "Sales Order"
    direction = "for_buying" if buyer_is_company else "for_selling"
    party_key = "seller_id" if buyer_is_company else "buyer_id"
    party_identity = facts.get(party_key)
    party_doctype = "Supplier" if buyer_is_company else "Customer"
    party_map = _ensure_party(party_identity, party_doctype, context, record["currency"])
    posting_date = _date(record["occurred_at"])
    delivery_minute = facts.get("delivery_at_virtual_minute")
    if delivery_minute is None:
        raise ValueError("CoffeeBench source order has no native delivery_at_virtual_minute")
    started_at = context.get("started_at")
    if started_at is None:
        raise ValueError("CoffeeBench order import requires its configured environment start date")
    start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    delivery_date = (start + timedelta(minutes=int(delivery_minute))).date().isoformat()
    company = context["company"]
    currency = record["currency"]
    company_currency = _company_currency(company)
    rate = _conversion_rate(currency, company_currency, posting_date, direction)
    item_code, qty, unit_price = _item(record, context["item_map"], context)
    values = {
        ("supplier" if buyer_is_company else "customer"): party_map["name"],
        "company": company,
        "transaction_date": posting_date,
        "currency": currency,
        "conversion_rate": float(rate),
        "items": [{"item_code": item_code, "qty": float(qty), "rate": float(unit_price),
                    "schedule_date": delivery_date,
                    **({"delivery_date": delivery_date} if doctype == "Sales Order" else {})}],
    }
    if doctype == "Sales Order":
        values["delivery_date"] = delivery_date
    ensure_source_fields(doctype, "terms")
    doc, inserted = insert_source_document(
        doctype, values, source_system=SOURCE_SYSTEM, source_id=str(record["native_record_id"]), record_type="order",
        task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
        snapshot=_source_values(record),
    )
    return doc.name, inserted


def _insert_offer(record: dict[str, Any], context: dict[str, Any]) -> tuple[str, bool]:
    """Import an actual pending buyer offer as a draft seller Sales Order."""
    facts = record.get("native_record") or {}
    if (
        facts.get("offer_status") != "pending"
        or facts.get("seller_id") not in context["company_business_ids"]
        or facts.get("buyer_id") in context["company_business_ids"]
        or facts.get("offer_id") != record.get("native_record_id")
    ):
        raise ValueError("CoffeeBench offer is not a pending offer to the mapped company seller")
    buyer = facts.get("buyer_id")
    if not isinstance(buyer, str) or not buyer:
        raise ValueError("CoffeeBench pending offer has no native buyer")
    payment_terms = facts.get("payment_terms_days")
    if isinstance(payment_terms, bool) or not isinstance(payment_terms, int) or payment_terms < 0:
        raise ValueError("CoffeeBench pending offer has invalid native payment terms")
    party = _ensure_party(buyer, "Customer", context, record["currency"])
    posting_date = _date(record["occurred_at"])
    rate = _conversion_rate(
        record["currency"], _company_currency(context["company"]), posting_date, "for_selling"
    )
    item_code, qty, unit_price = _item(record, context["item_map"], context)
    if unit_price != unit_price.quantize(Decimal("0.01")):
        raise ValueError("CoffeeBench offer unit price exceeds native ERP currency precision")
    values = {
        "customer": party["name"],
        "company": context["company"],
        "transaction_date": posting_date,
        "currency": record["currency"],
        "conversion_rate": float(rate),
        "custom_native_offer_id": facts["offer_id"],
        "custom_native_payment_terms_days": payment_terms,
        "items": [{
            "item_code": item_code,
            "qty": float(qty),
            "rate": float(unit_price),
            "custom_native_coffee_item_id": facts["item_id"],
        }],
    }
    doc, inserted = insert_source_document(
        "Sales Order", values, source_system=SOURCE_SYSTEM,
        source_id=str(record["native_record_id"]), record_type="offer",
        task_id=context["company_id"], experiment_id="background",
        company_id=context["company_id"], snapshot=_source_values(record),
    )
    if facts.get("financing_requested") and context.get("finance_reviewer"):
        delegate_finance_task(
            "Sales Order", doc.name, context["finance_reviewer"], context["assistant_email"],
            f"{party['name']} is requesting credit to finance order {doc.name} "
            f"({record['currency']} {record['amount']}). Can you take care of it?",
        )
    if facts.get("requested_extra_days") and context.get("finance_reviewer"):
        delegate_finance_task(
            "Sales Order", doc.name, context["finance_reviewer"], context["assistant_email"],
            f"{party['name']} says cash is tight and asks for {facts['payment_terms_days']} days "
            f"to pay for order {doc.name}. Can you review the request before accepting it?",
            source_event_id=f"payment-terms:{record['id']}",
        )
    if facts.get("credit_limit_requested") and context.get("finance_reviewer"):
        from erpnext.selling.doctype.customer.customer import get_credit_limit, get_customer_outstanding

        limit = get_credit_limit(party["name"], context["company"])
        order_total = _decimal(doc.base_grand_total, "order total")
        if not limit and order_total >= Decimal("0.02"):
            customer = frappe.get_doc("Customer", party["name"])
            limit = float((order_total / 2).quantize(Decimal("0.01")))
            row = next((entry for entry in customer.credit_limits if entry.company == context["company"]), None)
            if row is None:
                row = customer.append("credit_limits", {"company": context["company"]})
            row.credit_limit = limit
            customer.save()
        outstanding = Decimal(str(get_customer_outstanding(party["name"], context["company"])))
        if limit and outstanding + order_total > Decimal(str(limit)):
            delegate_finance_task(
                "Sales Order", doc.name, context["finance_reviewer"], context["assistant_email"],
                f"{party['name']} is asking for a higher credit limit to place order {doc.name}. "
                "Can you review the request and proceed if you approve it?",
                source_event_id=f"credit-limit:{record['id']}",
            )
    return doc.name, inserted


def _insert_invoice(record: dict[str, Any], context: dict[str, Any]) -> tuple[str, bool]:
    facts = record.get("native_record") or {}
    issuer = facts.get("issuer_id")
    payer = facts.get("payer_id")
    company_ids = context["company_business_ids"]
    if issuer in company_ids:
        doctype, party_field, party_doctype, party_id, direction = "Sales Invoice", "customer", "Customer", payer, "for_selling"
    elif payer in company_ids:
        doctype, party_field, party_doctype, party_id, direction = "Purchase Invoice", "supplier", "Supplier", issuer, "for_buying"
    else:
        return "", False
    party_map = _ensure_party(party_id, party_doctype, context, record["currency"])
    issue_date = _date(record["occurred_at"])
    amount = _decimal(facts.get("amount"), "invoice amount")
    due_day = facts.get("due_day")
    # CoffeeBench day numbers are relative to its 90-day environment origin.
    start_date = context.get("started_at")
    if start_date is None:
        raise ValueError("CoffeeBench invoice import requires its configured environment start date")
    base = datetime.fromisoformat(str(start_date).replace("Z", "+00:00")).date()
    due_date = (base + timedelta(days=int(due_day))).isoformat()
    company = context["company"]
    currency = record["currency"]
    rate = _conversion_rate(currency, _company_currency(company), issue_date, direction)
    # Invoice lines come from the linked native draft order's source facts. Missing
    # item facts are a hard mapping error; no generic expense/revenue line is invented.
    deal_id = facts.get("reference")
    order_doctype = "Sales Order" if doctype == "Sales Invoice" else "Purchase Order"
    order = None
    if deal_id:
        order = get_source_document(
            order_doctype, source_system=SOURCE_SYSTEM, source_id=str(deal_id), record_type="order",
            task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
        )
    if order is None:
        raise ValueError("CoffeeBench invoice has no matching native source order")
    item_code, qty, unit_price = _item(order.custom_source_snapshot and json.loads(order.custom_source_snapshot), context["item_map"], context)
    invoice_rate = amount / qty
    values = {
        party_field: party_map["name"], "company": company, "posting_date": issue_date,
        "due_date": due_date,
        "currency": currency, "conversion_rate": float(rate),
        "items": [{"item_code": item_code, "qty": float(qty), "rate": float(invoice_rate)}],
    }
    if doctype == "Purchase Invoice":
        values.update({"bill_date": issue_date, "bill_no": facts.get("invoice_id")})
    ensure_source_fields(doctype, "remarks")
    doc, inserted = insert_source_document(
        doctype, values, source_system=SOURCE_SYSTEM, source_id=str(record["native_record_id"]), record_type="invoice",
        task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
        snapshot=_source_values(record),
    )
    if inserted and context.get("finance_reviewer"):
        request = (
            f"can you invoice the customer for order {order.name} and make sure it gets paid?"
            if doctype == "Sales Invoice"
            else f"can you take care of the supplier invoice for order {order.name} and make sure it gets paid?"
        )
        delegate_finance_task(
            doctype, doc.name, context["finance_reviewer"], context["assistant_email"], request,
        )
    return doc.name, inserted


def _insert_payment_evidence(record: dict[str, Any], context: dict[str, Any]) -> tuple[str, bool]:
    facts = record.get("native_record") or {}
    incoming = facts.get("cash_to_party_id") in context["company_business_ids"]
    outgoing = facts.get("cash_from_party_id") in context["company_business_ids"]
    if incoming == outgoing:
        return "", False
    party_type = "Customer" if incoming else "Supplier"
    party_id = facts["cash_from_party_id"] if incoming else facts["cash_to_party_id"]
    currency = record["currency"]
    party = _ensure_party(party_id, party_type, context, currency)
    subject = f"CoffeeBench payment evidence {record['id']}"
    ensure_source_fields("Issue", "subject")
    values = {
        "subject": subject, "description": frappe.as_json(record), "status": "Open",
        "priority": "Medium",
    }
    doc, inserted = insert_source_document(
        "Issue", values, source_system=SOURCE_SYSTEM, source_id=str(record["native_record_id"]), record_type="payment-evidence",
        task_id=context["company_id"], experiment_id="background", company_id=context["company_id"],
        snapshot=_source_values(record),
    )
    amount = _decimal(record["amount"], "paid amount")
    if amount:
        _, bank_account = ensure_simulated_bank(
            context["company"], currency, f"CoffeeBench Settlement {currency}",
            "CoffeeBench Simulated Bank",
        )
        transaction, _ = insert_source_document(
            "Bank Transaction",
            {
                "date": _date(record["occurred_at"]),
                "bank_account": bank_account,
                "company": context["company"],
                "currency": currency,
                "deposit": float(amount) if incoming else 0,
                "withdrawal": float(amount) if outgoing else 0,
                "party_type": party_type,
                "party": party["name"],
                "description": f"CoffeeBench invoice {facts['invoice_id']} · evidence {doc.name}",
                "reference_number": facts["invoice_id"],
                "transaction_id": record["id"],
            },
            source_system=SOURCE_SYSTEM, source_id=str(record["native_record_id"]),
            record_type="bank-transaction", task_id=context["company_id"],
            experiment_id="background", company_id=context["company_id"],
            snapshot=_source_values(record),
        )
        submit_imported_bank_transaction(transaction)
    return doc.name, inserted


def reconcile(source_records: list[dict[str, Any]], company_binding: dict[str, Any], as_of: str | datetime) -> dict[str, Any]:
    """Create/reuse native draft docs for CoffeeBench facts, preserving identity."""
    ensure_finance_source_contract()
    if company_binding.get("review_coffee_financing"):
        from coffee_lending import decide_applications, ensure_products

        ensure_products(company_binding["erp_company"])
    context = {
        "company": company_binding["erp_company"],
        "company_id": company_binding["company_id"],
        "company_business_ids": company_binding["company_business_ids"],
        "party_map": company_binding.get("party_map", {}),
        "item_map": company_binding.get("item_map", {}),
        "counterparty_mail_directory": company_binding.get("counterparty_mail_directory", {}),
        "started_at": company_binding["started_at"],
        "assistant_email": company_binding.get("assistant_email"),
        "finance_reviewer": company_binding.get("finance_reviewer"),
    }
    if "roaster_A" not in context["company_business_ids"]:
        raise ValueError("CoffeeBench business role mapping must explicitly identify roaster_A")
    if not frappe.db.exists("Company", context["company"]):
        raise ValueError("Configured native ERP company does not exist")
    receipts: list[dict[str, Any]] = []
    for record in sorted(source_records, key=lambda row: (row["occurred_at"], row["id"])):
        source = record.get("source")
        if source == ECOMMERCE_PROVIDER_ID:
            continue
        if source != COFFEE_PROVIDER_ID:
            raise ValueError("Finance source batch contains an unknown provider identity")
        kind = record.get("kind")
        facts = record.get("native_record") or {}
        if kind == "order":
            name, created = _insert_order(record, context)
            doctype = "Purchase Order" if facts.get("buyer_id") in context["company_business_ids"] else "Sales Order"
        elif kind == "offer":
            name, created = _insert_offer(record, context)
            doctype = "Sales Order"
        elif kind == "invoice":
            name, created = _insert_invoice(record, context)
            doctype = "Purchase Invoice" if facts.get("payer_id") in context["company_business_ids"] else "Sales Invoice"
        elif kind == "payment":
            name, created = _insert_payment_evidence(record, context)
            doctype = "Issue"
        else:
            continue
        if name:
            receipts.append({"source_id": record["id"], "doctype": doctype, "name": name, "created": created})
    if company_binding.get("review_coffee_financing"):
        decide_applications(context)
    if company_binding.get("coffee_billing_behaviours"):
        coffee_billing_state(context["company"], context["counterparty_mail_directory"],
                            company_binding["coffee_billing_behaviours"], handoff=context)
    return {"as_of": _date(as_of), "receipts": receipts}


def _sales_invoice_mail(company: str, directory: dict[str, Any], *, queue_status, recipient_status, include_misaddressed=False):
    """Find submitted CoffeeBench invoice emails addressed to the mapped buyer."""
    if not frappe.get_meta("Sales Invoice").has_field("custom_source_system"):
        return
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": 1, "custom_source_system": SOURCE_SYSTEM,
                 "custom_company_id": "company", "custom_experiment_id": "background"},
        fields=["name", "custom_source_id", "custom_source_snapshot"],
    )
    for invoice in invoices:
        facts = json.loads(invoice.custom_source_snapshot)["native_record"]
        address = directory[facts["payer_id"]]["email"]
        queues = frappe.get_all(
            "Email Queue",
            filters={"reference_doctype": "Sales Invoice", "reference_name": invoice.name,
                     "status": ["in", queue_status], "communication": ["is", "set"]},
            pluck="name",
        )
        if include_misaddressed and "reminder" in facts.get("billing_exceptions", ()):
            roots = frappe.get_all("Communication", filters={
                "reference_doctype": "Sales Invoice", "reference_name": invoice.name,
                "sent_or_received": "Sent",
            }, pluck="name")
            if roots:
                replies = frappe.get_all("Communication", filters={"in_reply_to": ["in", roots]}, pluck="name")
                if replies:
                    queues.extend(frappe.get_all("Email Queue", filters={
                        "communication": ["in", replies], "status": ["in", queue_status],
                    }, pluck="name"))
        if queues:
            recipients = {"parent": ["in", queues], "status": recipient_status}
            if not include_misaddressed or "wrong_recipient" not in facts.get("billing_exceptions", ()):
                recipients["recipient"] = address
            for queue in frappe.get_all("Email Queue Recipient", filters=recipients, pluck="parent"):
                yield invoice.custom_source_id, queue


def read_sent_sales_invoices(company: str, directory: dict[str, Any]) -> list[str]:
    """Release only submitted invoices sent to their mapped CoffeeBench buyer."""
    released = set()
    for invoice_id, queue_name in _sales_invoice_mail(
        company, directory, queue_status=["Sent", "Partially Sent"], recipient_status="Sent",
    ):
        queue = frappe.get_doc("Email Queue", queue_name)
        if _includes_invoice_print(queue, queue.reference_name):
            released.add(invoice_id)
    return sorted(released)


def _includes_invoice_print(queue, invoice_name):
    return any(
        attachment.get("print_format_attachment") == 1
        and attachment.get("doctype") == "Sales Invoice"
        and attachment.get("name") == invoice_name
        for attachment in queue.attachments_list
    )


def coffee_billing_state(company, directory, enabled, *, handoff=None):
    """Evaluate submitted Coffee invoices against their delivered mail and ERP evidence."""
    state = {"sent_invoice_ids": [], "invoice_credits": {}}
    if not frappe.get_meta("Sales Invoice").has_field("custom_source_system"):
        return state
    invoices = frappe.get_all("Sales Invoice", filters={
        "company": company, "docstatus": 1, "is_return": 0, "custom_source_system": SOURCE_SYSTEM,
        "custom_company_id": "company", "custom_experiment_id": "background",
    }, fields=["name", "custom_source_id", "custom_source_snapshot", "po_no"])
    for invoice in invoices:
        facts = json.loads(invoice.custom_source_snapshot)["native_record"]
        behaviours = set(facts.get("billing_exceptions", ())) & set(enabled)
        address = directory[facts["payer_id"]]["email"].casefold()
        queues = [
            frappe.get_doc("Email Queue", name)
            for name in frappe.get_all("Email Queue", filters={
                "reference_doctype": "Sales Invoice", "reference_name": invoice.name,
                "status": ["in", ["Sent", "Partially Sent"]], "communication": ["is", "set"],
            }, pluck="name")
        ]
        deliveries = []
        wrong_delivery = False
        for queue in queues:
            if not _includes_invoice_print(queue, invoice.name):
                continue
            sent = {row.recipient.casefold() for row in queue.recipients if row.status == "Sent"}
            if address in sent:
                deliveries.append(queue)
            elif sent:
                wrong_delivery = True

        issues = {}
        if "wrong_recipient" in behaviours and wrong_delivery and not deliveries:
            issues["wrong_recipient"] = f"can you resend invoice {invoice.name} to Accounts Payable at {address}?"
        if deliveries and "missing_po" in behaviours:
            po = f"COFFEE-{facts['reference']}"
            request_id = finance_task_message_id("Sales Invoice", invoice.name, "billing:missing_po")
            requested_at = frappe.db.get_value("Email Queue", {"message_id": request_id}, "creation")
            if invoice.po_no != po or (requested_at and all(queue.creation <= requested_at for queue in deliveries)):
                issues["missing_po"] = (
                    f"the customer needs PO reference {po} on invoice {invoice.name}. "
                    "Can you correct it and resend it?"
                )

        if behaviours & {"receiving_mismatch", "partial_payment"}:
            credits = frappe.get_all("Sales Invoice", filters={
                "company": company, "docstatus": 1, "is_return": 1, "return_against": invoice.name,
                "currency": "USD",
            }, pluck="net_total")
            credited = min(
                _decimal(facts["amount"], "invoice amount"),
                sum(_decimal(abs(Decimal(str(value))), "credit note amount") for value in credits),
            )
            if credited:
                state["invoice_credits"][invoice.custom_source_id] = str(credited)
            if deliveries and "receiving_mismatch" in behaviours:
                discrepancy = facts.get("receiving_discrepancy")
                if discrepancy:
                    expected = _decimal(discrepancy["credit_amount"], "shipment shortage credit")
                    if credited < expected:
                        issues["receiving_mismatch"] = (
                            f"the customer received {discrepancy['received_quantity']} of "
                            f"{discrepancy['expected_quantity']} units on invoice {invoice.name}. "
                            f"Can you resolve the shortage and issue the USD {expected:.2f} credit?"
                        )
                else:
                    # Existing checkpoints can contain a dispute created before
                    # shipment quantities were recorded.
                    expected = (_decimal(facts["amount"], "invoice amount") * Decimal("0.10")).quantize(Decimal("0.01"))
                    if credited < expected:
                        issues["receiving_mismatch"] = (
                            f"the customer disputes USD {expected} on invoice {invoice.name}. Can you handle it?"
                        )

        if handoff and handoff.get("finance_reviewer"):
            for issue, request in issues.items():
                delegate_finance_task(
                    "Sales Invoice", invoice.name, handoff["finance_reviewer"],
                    handoff["assistant_email"], request, source_event_id=f"billing:{issue}",
                )

        if not deliveries or issues:
            continue
        if "reminder" in behaviours:
            roots = [queue.communication for queue in deliveries]
            communications = frappe.get_all("Communication", filters={
                "in_reply_to": ["in", roots], "sent_or_received": "Sent",
            }, fields=["name", "content"])
            replies = [message.name for message in communications if _requests_payment(message.content)]
            reply_queues = frappe.get_all("Email Queue", filters={"communication": ["in", replies]},
                                          pluck="name") if replies else []
            reminder_sent = reply_queues and frappe.db.exists("Email Queue Recipient", {
                "parent": ["in", reply_queues], "recipient": address, "status": "Sent",
            })
            if not reminder_sent:
                continue
        state["sent_invoice_ids"].append(invoice.custom_source_id)
    return state


def _requests_payment(content):
    from frappe.utils import strip_html

    # Quoted correspondence is not a request from the current sender.
    authored = re.split(r"""<blockquote\b|id=["'](?:divRplyFwdMsg|appendonsend)["']""",
                        content or "", maxsplit=1, flags=re.IGNORECASE)[0]
    authored = re.sub(r"<(?:br\s*/?|/p|/div)>", "\n", authored, flags=re.IGNORECASE)
    authored = re.split(r"(?mi)^\s*(?:>|On .+ wrote:|[- ]*Original Message[- ]*)", strip_html(authored), maxsplit=1)[0]
    return bool(re.search(
        r"\b(?:payment reminder|(?:remind(?:er|ing)?|follow(?:ing)? up|chase)\b[^.!?]*\b(?:payment|outstanding|invoice)"
        r"|(?:please|could you|can you|kindly)\b[^.!?]*\b(?:pay|settle|(?:arrange|make|send|process|confirm) (?:the )?payment)"
        r"|when\b[^.!?]*\b(?:payment|pay|settle))\b",
        authored, re.IGNORECASE,
    ))


def send_pending_sales_invoices(company: str, directory: dict[str, Any], *, misroute_to: str | None = None) -> list[dict[str, Any]]:
    """Dispatch native invoice emails requested from the ERPNext invoice form."""
    queues = _sales_invoice_mail(
        company, directory, queue_status=["Not Sent", "Partially Sent"], recipient_status="Not Sent",
        include_misaddressed=True,
    )
    queue_names = sorted({name for _, name in queues})
    if misroute_to:
        for name in queue_names:
            _misroute_first_invoice(name, directory, misroute_to)
    return send_finance_queues(queue_names)


def _misroute_first_invoice(queue_name, directory, misroute_to):
    queue = frappe.get_doc("Email Queue", queue_name, for_update=True)
    if (queue.status != "Not Sent" or len(queue.recipients) != 1
            or queue.recipients[0].status != "Not Sent"
            or not _includes_invoice_print(queue, queue.reference_name)):
        return
    snapshot = frappe.db.get_value("Sales Invoice", queue.reference_name, "custom_source_snapshot")
    facts = json.loads(snapshot)["native_record"]
    if "wrong_recipient" not in facts.get("billing_exceptions", ()):
        return
    address = directory[facts["payer_id"]]["email"].casefold()
    if queue.recipients[0].recipient.casefold() != address:
        return
    earlier = frappe.get_all("Email Queue", filters={
        "name": ["!=", queue.name], "reference_doctype": "Sales Invoice",
        "reference_name": queue.reference_name,
    }, pluck="name")
    if earlier and (
        frappe.db.exists("Email Queue Recipient", {"parent": ["in", earlier], "recipient": misroute_to})
        or frappe.db.exists("Email Queue Recipient", {"parent": ["in", earlier], "status": "Sent"})
    ):
        return
    message = Parser(policy=policy.SMTP).parsestr(queue.message)
    if "To" in message:
        message.replace_header("To", misroute_to)
    else:
        message["To"] = misroute_to
    frappe.db.set_value("Email Queue", queue.name, "message", message.as_string())
    frappe.db.set_value("Email Queue Recipient", queue.recipients[0].name, "recipient", misroute_to)
    frappe.db.commit()


def read_due_obligations(company: str, as_of: str | datetime) -> list[dict[str, Any]]:
    """Read due/overdue native submitted invoices for finance operations."""
    boundary = _date(as_of)
    rows: list[dict[str, Any]] = []
    for doctype, party_field, outstanding_field in (
        ("Purchase Invoice", "supplier", "outstanding_amount"),
        ("Sales Invoice", "customer", "outstanding_amount"),
    ):
        records = frappe.get_all(
            doctype,
            filters={"company": company, "docstatus": 1, "status": ["in", ["Unpaid", "Overdue", "Partly Paid"]], "due_date": ["<=", boundary]},
            fields=["name", party_field, "currency", "grand_total", outstanding_field, "due_date", "modified", "docstatus"],
            order_by="due_date asc, name asc",
        )
        rows.extend({"doctype": doctype, **row} for row in records if _decimal(row[outstanding_field], outstanding_field) > 0)
    return rows
