"""Reconcile CoffeeBench source facts into native, reviewable ERP documents.

This is an import boundary: it creates draft orders/invoices and source-linked
payment evidence. It never posts a payment or submits a financial document.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from source_records import ensure_source_fields, get_source_document, insert_source_document
from native_accounts import map_party_to_currency


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
        for field in entries:
            if not meta.has_field(field["insert_after"]):
                raise RuntimeError(f"Native {doctype} lacks the offer-source field anchor")
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
    return doc.name, inserted


def _insert_payment_evidence(record: dict[str, Any], context: dict[str, Any]) -> tuple[str, bool]:
    facts = record.get("native_record") or {}
    # No bank account mapping is supplied by CoffeeBench. Keep a native Issue
    # as payment evidence so an operator can reconcile it; never post cash.
    company_side = facts.get("cash_to_party_id") in context["company_business_ids"] or facts.get("cash_from_party_id") in context["company_business_ids"]
    if not company_side:
        return "", False
    party_id = facts.get("cash_from_party_id") if facts.get("cash_to_party_id") in context["company_business_ids"] else facts.get("cash_to_party_id")
    side = facts.get("cash_from_party_id") if facts.get("cash_to_party_id") in context["company_business_ids"] else facts.get("cash_to_party_id")
    _ensure_party(party_id, "Supplier" if side == facts.get("cash_to_party_id") else "Customer",
                  context, record["currency"])
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
    return doc.name, inserted


def reconcile(source_records: list[dict[str, Any]], company_binding: dict[str, Any], as_of: str | datetime) -> dict[str, Any]:
    """Create/reuse native draft docs for CoffeeBench facts, preserving identity."""
    ensure_finance_source_contract()
    context = {
        "company": company_binding["erp_company"],
        "company_id": company_binding["company_id"],
        "company_business_ids": company_binding["company_business_ids"],
        "party_map": company_binding.get("party_map", {}),
        "item_map": company_binding.get("item_map", {}),
        "counterparty_mail_directory": company_binding.get("counterparty_mail_directory", {}),
        "started_at": company_binding["started_at"],
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
    return {"as_of": _date(as_of), "receipts": receipts}


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
