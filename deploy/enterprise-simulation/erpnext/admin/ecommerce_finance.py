"""Import native E-CommerceBench pending sales into the existing company ERP."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any

import frappe

from finance_sources import _company_currency, _conversion_rate, _date
from native_accounts import map_party_to_currency
from source_records import (
    ensure_source_fields,
    get_source_document,
    insert_source_document,
    source_fields,
)


SOURCE_SYSTEM = "qwenlm-ecommercebench-native-economy"
UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
MONEY_UNIT = {"currency": "GBP", "native_units_per_currency_unit": "1"}
DATE_SEMANTICS = "source_dispatch_deadline"
DATE_MEANING = "Dispatch deadline, not arrival promise"


def _ensure_marketplace_bank(company: str, currency: str, store_type: str) -> tuple[str, str]:
    """Provision a source-specific ERP ledger and Bank Account without changing defaults."""
    parent_name = frappe.db.get_value(
        "Account",
        {"company": company, "account_name": "Bank Accounts", "is_group": 1},
        "name",
    )
    if not parent_name:
        raise RuntimeError(f"Native company {company} has no Bank Accounts parent")
    parent = frappe.get_doc("Account", parent_name)
    if parent.root_type != "Asset" or parent.account_type != "Bank" or not parent.is_group:
        raise RuntimeError("Native Bank Accounts parent does not satisfy the ERPNext account contract")
    account_name = f"E-CommerceBench {store_type} Marketplace Bank"
    existing_name = frappe.db.get_value(
        "Account", {"company": company, "account_name": account_name}, "name"
    )
    if existing_name:
        account = frappe.get_doc("Account", existing_name)
        if (account.parent_account != parent_name or account.root_type != "Asset"
                or account.account_type != "Bank" or account.is_group
                or account.disabled or account.account_currency != currency):
            raise RuntimeError("Existing E-CommerceBench marketplace bank conflicts with its source mapping")
    else:
        account = frappe.get_doc({
            "doctype": "Account", "account_name": account_name, "company": company,
            "parent_account": parent_name, "root_type": "Asset",
            "report_type": "Balance Sheet", "account_type": "Bank",
            "account_currency": currency, "is_group": 0,
        })
        account.insert(ignore_permissions=True)

    bank_name = "E-CommerceBench Simulated Marketplace"
    if frappe.db.exists("Bank", bank_name):
        bank = frappe.get_doc("Bank", bank_name)
        if bank.bank_name != bank_name:
            raise RuntimeError("Existing simulated Bank master conflicts with E-Commerce source identity")
    else:
        bank = frappe.get_doc({"doctype": "Bank", "bank_name": bank_name})
        bank.insert(ignore_permissions=True)

    existing_bank_account_name = frappe.db.get_value(
        "Bank Account", {"account_name": account_name, "company": company}, "name"
    )
    if existing_bank_account_name:
        bank_account = frappe.get_doc("Bank Account", existing_bank_account_name)
        if (bank_account.bank != bank.name or bank_account.account != account.name
                or not bank_account.is_company_account or bank_account.company != company
                or bank_account.is_default):
            raise RuntimeError("Existing E-Commerce Bank Account conflicts with the dedicated company bank mapping")
    else:
        if frappe.db.exists("Bank Account", account_name):
            raise RuntimeError("E-Commerce Bank Account name conflicts with another native account")
        bank_account = frappe.get_doc({
            "doctype": "Bank Account",
            "account_name": account_name,
            "bank": bank.name,
            "is_company_account": 1,
            "company": company,
            "account": account.name,
            "is_default": 0,
        })
        bank_account.insert(ignore_permissions=True)
    return account.name, bank_account.name


def ensure_ecommerce_finance_contract() -> None:
    """Install source provenance and user-editable fulfillment fields."""
    for doctype, anchor in (
        ("Item", "item_name"),
        ("Customer", "customer_name"),
        ("Supplier", "supplier_name"),
        ("Sales Order", "terms"),
        ("Sales Invoice", "customer"),
        ("Delivery Note", "company"),
        ("Bank Transaction", "description"),
        ("Comment", "content"),
    ):
        ensure_source_fields(doctype, anchor)

    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    specs = {
        "Sales Order": [
            {
                "fieldname": "custom_ecommerce_shipment_id",
                "label": "E-Commerce Source Shipment ID",
                "fieldtype": "Data",
                "insert_after": "terms",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_ecommerce_store_id",
                "label": "E-Commerce Source Store ID",
                "fieldtype": "Data",
                "insert_after": "custom_ecommerce_shipment_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "custom_ecommerce_product_id",
                "label": "E-Commerce Source Product ID",
                "fieldtype": "Data",
                "insert_after": "custom_ecommerce_store_id",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
            },
        ],
        "Delivery Note": [
            {
                "fieldname": "custom_source_shipping_speed",
                "label": "Shipping Speed",
                "fieldtype": "Select",
                "options": "\nfast\nstandard\nslow",
                "insert_after": "company",
                "read_only": 0,
                "hidden": 0,
                "no_copy": 0,
            }
        ],
    }
    specs["Sales Order"].append(
        {
            "fieldname": "custom_ecommerce_ship_deadline",
            "label": "Source Ship Deadline",
            "fieldtype": "Date",
            "insert_after": "custom_ecommerce_product_id",
            "read_only": 1,
            "hidden": 0,
            "no_copy": 1,
        }
    )
    specs["Sales Order"].append(
        {
            "fieldname": "custom_ecommerce_date_semantics",
            "label": "Source Date Meaning",
            "fieldtype": "Data",
            "insert_after": "custom_ecommerce_ship_deadline",
            "read_only": 1,
            "hidden": 0,
            "no_copy": 1,
        }
    )
    create_custom_fields(specs, update=True)
    for doctype, fieldnames in {
        "Sales Order": (
            "custom_ecommerce_shipment_id",
            "custom_ecommerce_store_id",
            "custom_ecommerce_product_id",
            "custom_ecommerce_ship_deadline",
            "custom_ecommerce_date_semantics",
        ),
        "Delivery Note": ("custom_source_shipping_speed",),
    }.items():
        for fieldname in fieldnames:
            if not frappe.get_meta(doctype).has_field(fieldname):
                raise RuntimeError(
                    f"Native {doctype}.{fieldname} E-Commerce mapping field was not installed"
                )


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"E-Commerce source {field} is not a valid amount") from error
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "finite non-negative"
        raise ValueError(f"E-Commerce source {field} must be {qualifier}")
    return result


def _native_record(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("native_record")
    if not isinstance(value, dict):
        raise ValueError("E-Commerce source record has no native shipment facts")
    return value


def _validated_money(
    record: dict[str, Any], binding: dict[str, Any], facts: dict[str, Any]
) -> tuple[str, Decimal, Decimal]:
    money_unit = binding.get("money_unit")
    if not isinstance(money_unit, dict) or money_unit.get("currency") != "GBP":
        raise ValueError(
            "E-Commerce finance import requires the configured 1:1 native-unit to GBP mapping"
        )
    ratio = _decimal(
        money_unit.get("native_units_per_currency_unit"),
        "native units per GBP",
        positive=True,
    )
    if ratio != Decimal(MONEY_UNIT["native_units_per_currency_unit"]):
        raise ValueError(
            "E-Commerce finance import requires the configured 1:1 native-unit to GBP mapping"
        )
    if record.get("currency") != MONEY_UNIT["currency"]:
        raise ValueError(
            "E-Commerce source currency does not match the approved company mapping"
        )
    if (
        _decimal(
            record.get("native_units_per_currency_unit"),
            "source money-unit ratio",
            positive=True,
        )
        != ratio
    ):
        raise ValueError(
            "E-Commerce source money-unit ratio does not match the approved company mapping"
        )
    native_amount = _decimal(facts.get("revenue_gross"), "gross sale amount")
    company_amount = native_amount / ratio
    envelope_amount = _decimal(record.get("amount"), "company sale amount")
    if company_amount != envelope_amount:
        raise ValueError(
            "E-Commerce company amount does not match the approved native money-unit mapping"
        )
    return MONEY_UNIT["currency"], ratio, company_amount


def _source_snapshot(
    record: dict[str, Any], facts: dict[str, Any], context: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Exclude observed_at/occurred_at from immutable source facts: the pending
    # order is re-observed as company time advances. Its native sale date and
    # upstream facts remain unchanged.
    return {
        "source": SOURCE_SYSTEM,
        "upstream_commit": UPSTREAM_COMMIT,
        "source_id": str(record["id"]),
        "native_record": facts,
        "currency_mapping": MONEY_UNIT,
        "date_semantics": {
            "policy": DATE_SEMANTICS,
            "source_field": "native_record.ship_deadline",
            "erp_fields": [
                "Sales Order.delivery_date",
                "Sales Order Item.delivery_date",
            ],
            "meaning": DATE_MEANING,
        },
        **({"feedback_context": context} if context else {}),
    }


def _ensure_item(facts: dict[str, Any], company_id: str) -> tuple[str, bool]:
    product = facts.get("product")
    product_id = facts.get("product_id")
    if (
        not isinstance(product, dict)
        or not isinstance(product_id, str)
        or not product_id
        or product.get("product_id") != product_id
        or not isinstance(product.get("title"), str)
        or not product["title"]
    ):
        raise ValueError(
            "E-Commerce source product ID and native catalog facts are required"
        )
    if product.get("store_type") != facts.get("store_type"):
        raise ValueError(
            "E-Commerce source product does not match the mapped store type"
        )
    item_code = f"ECOM-{product_id}"
    snapshot = {
        "source": SOURCE_SYSTEM,
        "upstream_commit": UPSTREAM_COMMIT,
        "product": product,
        "product_id": product_id,
        "feedback_context": {
            "native_action": "process_structured_order",
            "native_actions": ["process_structured_order"],
            "native_ids": {
                "store_id": facts.get("store_id"),
                "product_id": product_id,
            },
            "source_item_code": item_code,
            "store_type": facts.get("store_type"),
            "upstream_commit": UPSTREAM_COMMIT,
        },
    }
    existing = get_source_document(
        "Item",
        source_system=SOURCE_SYSTEM,
        source_id=product_id,
        record_type="product",
        task_id=company_id,
        experiment_id="background",
        company_id=company_id,
    )
    if existing:
        existing_snapshot = json.loads(existing.custom_source_snapshot)
        # Feedback context is derived integration metadata; older mapped Items
        # predate it. Preserve their immutable upstream facts and accept only
        # that one additive context difference.
        if ({key: value for key, value in existing_snapshot.items() if key != "feedback_context"}
                != {key: value for key, value in snapshot.items() if key != "feedback_context"}
                or existing.item_code != item_code):
            raise RuntimeError(
                "Existing E-Commerce source Item conflicts with immutable upstream product facts"
            )
        return existing.name, False
    if frappe.db.exists("Item", item_code):
        raise RuntimeError(
            "E-Commerce source Item code is already occupied by an unlinked native Item"
        )
    group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    uom = frappe.db.get_value("UOM", "Nos", "name")
    if not group or not uom:
        raise RuntimeError(
            "Native ERP lacks a leaf Item Group or Nos UOM for E-Commerce source Items"
        )
    ensure_source_fields("Item", "item_name")
    item, inserted = insert_source_document(
        "Item",
        {
            "item_code": item_code,
            "item_name": product["title"],
            "item_group": group,
            "stock_uom": uom,
            "is_stock_item": 1,
            "description": product["title"],
        },
        source_system=SOURCE_SYSTEM,
        source_id=product_id,
        record_type="product",
        task_id=company_id,
        experiment_id="background",
        company_id=company_id,
        snapshot=snapshot,
    )
    return item.name, inserted


def _ensure_catalog_supplier(facts: dict[str, Any], company_id: str, currency: str) -> tuple[str, bool]:
    supplier_id, supplier_name = facts.get("supplier_id"), facts.get("supplier_name")
    if not isinstance(supplier_id, str) or not supplier_id or not isinstance(supplier_name, str) or not supplier_name:
        raise ValueError("E-Commerce native supplier catalog requires exact supplier ID and name")
    snapshot = {
        "source": SOURCE_SYSTEM,
        "upstream_commit": UPSTREAM_COMMIT,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
    }
    existing = get_source_document(
        "Supplier", source_system=SOURCE_SYSTEM, source_id=supplier_id,
        record_type="supplier", task_id=company_id, experiment_id="background",
        company_id=company_id,
    )
    if existing:
        if json.loads(existing.custom_source_snapshot) != snapshot or existing.supplier_name != supplier_name:
            raise RuntimeError("Existing E-Commerce supplier conflicts with pinned native roster")
        return existing.name, False
    if (frappe.db.exists("Supplier", {"supplier_name": supplier_name})
            or frappe.db.exists("Supplier", supplier_id)):
        raise RuntimeError("E-Commerce supplier name conflicts with an unlinked native Supplier")
    group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    if not group:
        raise RuntimeError("Native ERP lacks a leaf Supplier Group for E-Commerce suppliers")
    supplier, created = insert_source_document(
        "Supplier",
        {"name": supplier_id, "supplier_name": supplier_name, "supplier_group": group,
         "supplier_type": "Company", "default_currency": currency},
        source_system=SOURCE_SYSTEM, source_id=supplier_id, record_type="supplier",
        task_id=company_id, experiment_id="background", company_id=company_id,
        snapshot=snapshot,
    )
    return supplier.name, created


def _map_catalog_record(record: dict[str, Any], company_binding: dict[str, Any]) -> dict[str, Any]:
    if record.get("upstream_commit") != UPSTREAM_COMMIT or record.get("source") != SOURCE_SYSTEM:
        raise ValueError("E-Commerce catalog record is not from the pinned provider")
    kind = record.get("kind")
    facts = record.get("native_record")
    if not isinstance(facts, dict):
        raise ValueError("E-Commerce catalog record has no native facts")
    if kind == "catalog_product":
        store_id, product_id, product = facts.get("store_id"), facts.get("product_id"), facts.get("product")
        if (not isinstance(store_id, str) or not store_id or not isinstance(product_id, str)
                or not product_id or not isinstance(product, dict)
                or product.get("product_id") != product_id
                or not isinstance(facts.get("store_type"), str) or not facts["store_type"]):
            raise ValueError("E-Commerce product catalog record lacks exact native store and product IDs")
        if (record.get("id") != f"catalog:{store_id}:product:{product_id}"
                or record.get("party") != f"ecommercebench-marketplace:{facts['store_type']}"):
            raise ValueError("E-Commerce product catalog source identity does not match its native IDs")
        if company_binding.get("store_type") and company_binding["store_type"] != facts["store_type"]:
            raise ValueError("E-Commerce product catalog record does not match the company store type")
        return {"kind": kind, "facts": facts, "company_id": company_binding["company_id"]}
    if kind == "catalog_supplier":
        supplier_id, supplier_name, store_id, store_type = (
            facts.get("supplier_id"), facts.get("supplier_name"), facts.get("store_id"), facts.get("store_type")
        )
        if (not isinstance(supplier_id, str) or not supplier_id or not isinstance(supplier_name, str)
                or not supplier_name or not isinstance(store_id, str) or not store_id
                or not isinstance(store_type, str) or not store_type
                or record.get("id") != f"catalog:{store_id}:supplier:{supplier_id}"
                or record.get("party") != f"ecommercebench-marketplace:{store_type}"):
            raise ValueError("E-Commerce supplier catalog record lacks exact native identity")
        if company_binding.get("store_type") and company_binding["store_type"] != store_type:
            raise ValueError("E-Commerce supplier catalog record does not match the company store type")
        return {"kind": kind, "facts": facts, "company_id": company_binding["company_id"]}
    raise ValueError("unsupported E-Commerce native source record kind")


def _map_native_bank_movement(record: dict[str, Any], company_binding: dict[str, Any]) -> dict[str, Any]:
    if record.get("source") != SOURCE_SYSTEM or record.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("E-Commerce bank movement is not from the pinned native provider")
    facts = record.get("native_record")
    if not isinstance(facts, dict):
        raise ValueError("E-Commerce bank movement has no native receipt facts")
    store_id, store_type = facts.get("store_id"), facts.get("store_type")
    source_id = record.get("id")
    if (not isinstance(store_id, str) or not store_id or not isinstance(store_type, str)
            or not store_type or source_id != f"bank:{store_id}:{facts.get('action_id') or facts.get('reference')}"):
        # Opening/setup/daily movements use a source reference rather than an
        # action ID; validate those stable IDs against the movement kind below.
        expected = {
            "opening_balance": f"bank:{store_id}:opening_balance",
            "initial_store_setup": f"bank:{store_id}:initial_store_setup",
            "daily_native_cash_activity": f"bank:{store_id}:daily:{str(facts.get('reference', '')).removeprefix('native-day:')}",
        }.get(str(facts.get("movement_type")))
        if not expected or source_id != expected:
            raise ValueError("E-Commerce bank movement identity does not match native receipt facts")
    if company_binding.get("store_type") and company_binding["store_type"] != store_type:
        raise ValueError("E-Commerce bank movement does not match the company store type")
    occurred_at = facts.get("occurred_at")
    try:
        event_time = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
        amount_native = _decimal(facts.get("amount_native_units"), "native bank movement amount", positive=True)
        units = _decimal(
            company_binding.get("money_unit", {}).get("native_units_per_currency_unit"),
            "native units per GBP", positive=True,
        )
    except (TypeError, AttributeError, ValueError):
        raise
    if event_time.tzinfo is None or event_time.utcoffset() is None:
        raise ValueError("E-Commerce bank movement time must be timezone-aware")
    if (facts.get("currency") != MONEY_UNIT["currency"]
            or record.get("currency") != MONEY_UNIT["currency"]
            or units != Decimal(MONEY_UNIT["native_units_per_currency_unit"])
            or _decimal(record.get("native_units_per_currency_unit"), "source money-unit ratio", positive=True) != units
            or _decimal(record.get("amount"), "company bank movement amount") != amount_native / units):
        raise ValueError("E-Commerce bank movement does not match the approved exact GBP mapping")
    direction = facts.get("direction")
    if direction not in {"deposit", "withdrawal"}:
        raise ValueError("E-Commerce bank movement has no supported native direction")
    return {
        "source_id": source_id,
        "store_id": store_id,
        "store_type": store_type,
        "occurred_at": event_time.astimezone(timezone.utc),
        "currency": MONEY_UNIT["currency"],
        "amount": amount_native / units,
        "direction": direction,
        "movement_type": str(facts.get("movement_type", "")),
        "reference": str(facts.get("reference") or source_id),
        "facts": facts,
        "company_id": company_binding["company_id"],
        "company": company_binding["erp_company"],
    }


def _map_native_outcome(
    record: dict[str, Any], company_binding: dict[str, Any]
) -> dict[str, Any]:
    if record.get("source") != SOURCE_SYSTEM or record.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("E-Commerce outcome is not from the pinned native provider")
    facts = record.get("native_record")
    if not isinstance(facts, dict):
        raise ValueError("E-Commerce native outcome has no source facts")
    store_id, store_type = facts.get("store_id"), facts.get("store_type")
    if not isinstance(store_id, str) or not store_id or not isinstance(store_type, str) or not store_type:
        raise ValueError("E-Commerce native outcome has no exact store identity")
    if company_binding.get("store_type") and company_binding["store_type"] != store_type:
        raise ValueError("E-Commerce native outcome does not match the bound store type")
    event_type, source_id = facts.get("event_type"), record.get("id")
    if event_type == "shipment_cancelled":
        shipment_id = facts.get("shipment_id")
        expected_id = f"outcome:{store_id}:shipment_cancelled:{shipment_id}"
        if (isinstance(shipment_id, bool) or not isinstance(shipment_id, int)
                or source_id != expected_id
                or facts.get("source_shipment_id") != f"{store_id}:shipment:{shipment_id}"
                or record.get("source_ref") != f"{store_id}:shipment:{shipment_id}"):
            raise ValueError("E-Commerce cancellation identity does not match its native shipment")
        amount = None
        quantity = facts.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("E-Commerce cancellation has an invalid native quantity")
        refund_per_unit = None
        delivery_note_names = []
    elif event_type == "refund_processed":
        batch_id, quantity = facts.get("batch_id"), facts.get("quantity")
        if (isinstance(batch_id, bool) or not isinstance(batch_id, int)
                or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
                or source_id != f"outcome:{store_id}:refund:{batch_id}"):
            raise ValueError("E-Commerce refund identity does not match its native batch")
        if (not isinstance(facts.get("shipment_id"), int)
                or isinstance(facts.get("shipment_id"), bool)
                or record.get("source_ref") != f"{store_id}:shipment:{facts.get('shipment_id')}"):
            raise ValueError("E-Commerce refund does not identify its originating native shipment")
        try:
            event_time = datetime.fromisoformat(str(record.get("occurred_at", "")).replace("Z", "+00:00"))
            amount = _decimal(facts.get("refund_amount_native_units"), "native refund amount", positive=True)
            refund_per_unit = _decimal(facts.get("refund_per_unit"), "native refund unit amount", positive=True)
            escrow_reclaimed = _decimal(facts.get("escrow_reclaimed_native_units"), "native escrow refund", positive=False)
            commission_reversed = _decimal(facts.get("commission_reversed_native_units"), "native commission reversal", positive=False)
            units = _decimal(company_binding.get("money_unit", {}).get("native_units_per_currency_unit"), "native units per GBP", positive=True)
            record_amount = _decimal(record.get("amount"), "refund amount", positive=True)
            record_units = _decimal(record.get("native_units_per_currency_unit"), "source money-unit ratio", positive=True)
        except (TypeError, AttributeError, ValueError):
            raise
        if (event_time.tzinfo is None or event_time.utcoffset() is None
                or units != Decimal(MONEY_UNIT["native_units_per_currency_unit"])
                or record_units != units
                or record.get("currency") != MONEY_UNIT["currency"]
                or amount != refund_per_unit * quantity
                or amount != escrow_reclaimed + commission_reversed
                or record_amount != amount / units):
            raise ValueError("E-Commerce refund amount or time does not match the approved source facts")
        delivery_note_names = facts.get("source_delivery_note_names")
        if (not isinstance(delivery_note_names, list) or not delivery_note_names
                or any(not isinstance(name, str) or not name for name in delivery_note_names)
                or len(set(delivery_note_names)) != len(delivery_note_names)):
            raise ValueError("E-Commerce refund has no exact originating Delivery Note identities")
    else:
        raise ValueError("Unsupported E-Commerce native outcome type")
    occurred_at = datetime.fromisoformat(str(record.get("occurred_at", "")).replace("Z", "+00:00"))
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("E-Commerce native outcome time must be timezone-aware")
    return {
        "source_id": source_id,
        "event_type": event_type,
        "source_ref": record.get("source_ref"),
        "store_id": store_id,
        "store_type": store_type,
        "company_id": company_binding["company_id"],
        "company": company_binding["erp_company"],
        "occurred_at": occurred_at.astimezone(timezone.utc),
        "facts": facts,
        "quantity": quantity,
        "amount_native_units": amount,
        "refund_per_unit": refund_per_unit,
        "escrow_reclaimed_native_units": escrow_reclaimed if event_type == "refund_processed" else None,
        "commission_reversed_native_units": commission_reversed if event_type == "refund_processed" else None,
        "delivery_note_names": delivery_note_names,
    }


def _source_sales_order(mapped: dict[str, Any]):
    order = get_source_document(
        "Sales Order", source_system=SOURCE_SYSTEM, source_id=mapped["source_ref"],
        record_type="pending-sale", task_id=mapped["company_id"],
        experiment_id="background", company_id=mapped["company_id"],
    )
    facts = mapped["facts"]
    if (not order or order.company != mapped["company"]
            or order.custom_ecommerce_store_id != mapped["store_id"]
            or order.custom_ecommerce_shipment_id != str(facts.get("shipment_id"))
            or order.custom_ecommerce_product_id != facts.get("product_id")):
        return None
    return order


def _ensure_source_comment(
    *, source_id: str, company_id: str, experiment_id: str, reference_name: str,
    content: str, snapshot: dict[str, Any],
):
    existing = get_source_document(
        "Comment", source_system=SOURCE_SYSTEM, source_id=source_id,
        record_type="native-outcome-comment", task_id=company_id,
        experiment_id=experiment_id, company_id=company_id,
    )
    if existing:
        if (existing.reference_doctype != "Sales Order" or existing.reference_name != reference_name
                or existing.content != content
                or json.loads(existing.custom_source_snapshot) != snapshot):
            raise RuntimeError("Existing E-Commerce outcome comment conflicts with the native event")
        return existing, False
    comment = frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "Sales Order", "reference_name": reference_name,
        "content": content,
        **source_fields(
            source_system=SOURCE_SYSTEM, source_id=source_id,
            record_type="native-outcome-comment", task_id=company_id,
            experiment_id=experiment_id, company_id=company_id, snapshot=snapshot,
        ),
    })
    comment.insert(ignore_permissions=True)
    return comment, True


def _ensure_source_return(
    doc: Any, *, doctype: str, source_id: str, company_id: str,
    snapshot: dict[str, Any],
):
    record_type = "native-refund-return"
    existing = get_source_document(
        doctype, source_system=SOURCE_SYSTEM, source_id=source_id,
        record_type=record_type, task_id=company_id,
        experiment_id="background", company_id=company_id,
    )
    if existing:
        if json.loads(existing.custom_source_snapshot) != snapshot:
            raise RuntimeError("Existing E-Commerce native return conflicts with source refund facts")
        result = existing
        created = False
    else:
        doc.update(source_fields(
            source_system=SOURCE_SYSTEM, source_id=source_id,
            record_type=record_type, task_id=company_id,
            experiment_id="background", company_id=company_id, snapshot=snapshot,
        ))
        doc.insert(ignore_permissions=True)
        result = doc
        created = True
    if result.docstatus == 0:
        result.submit()
    elif result.docstatus != 1:
        raise RuntimeError("Existing E-Commerce native return is canceled and cannot be replayed")
    return result, created


def _outcome_document(doc: Any) -> dict[str, Any]:
    return {"doctype": doc.doctype, "name": doc.name, "docstatus": getattr(doc, "docstatus", None)}


def _pending_outcome(mapped: dict[str, Any], *reasons: str) -> dict[str, Any]:
    receipt = {
        "schema": "native_outcome_v1", "source_id": mapped["source_id"],
        "status": "pending", "documents": [], "pending_reasons": list(reasons),
    }
    if mapped["event_type"] == "refund_processed":
        receipt["refund_accounting"] = {
            "gross_refund_native_units": format(mapped["amount_native_units"], "f"),
            "escrow_reclaimed_native_units": format(mapped["escrow_reclaimed_native_units"], "f"),
            "commission_reversed_native_units": format(mapped["commission_reversed_native_units"], "f"),
        }
    return receipt


def _allocated_return_rows(mapped: dict[str, Any], order: Any) -> tuple[list[tuple[Any, Any, Decimal]], list[tuple[Any, Any, Decimal]], list[Any], list[str]]:
    """Resolve exact submitted native records before creating any return document."""
    from erpnext.controllers.sales_and_purchase_return import get_returned_qty_map_for_row
    facts = mapped["facts"]
    item_code = f"ECOM-{facts.get('product_id')}"
    if not frappe.db.exists("Item", item_code):
        return [], [], [], ["source product Item is not yet available"]
    item = frappe.get_doc("Item", item_code)
    if (item.custom_source_system != SOURCE_SYSTEM
            or item.custom_source_id != facts.get("product_id")
            or not item.is_stock_item
            or not isinstance(item.stock_uom, str) or not item.stock_uom):
        return [], [], [], ["source product Item does not establish its native stock unit"]

    def value(row: Any, field: str) -> Any:
        return row.get(field) if isinstance(row, dict) else getattr(row, field, None)

    def stock_quantity(row: Any, label: str) -> tuple[Decimal, Decimal]:
        try:
            quantity = _decimal(value(row, "qty"), f"{label} quantity", positive=True)
            factor = _decimal(value(row, "conversion_factor"), f"{label} unit conversion", positive=True)
            stock = _decimal(value(row, "stock_qty"), f"{label} stock quantity", positive=True)
        except ValueError:
            raise ValueError(f"{label} does not expose an exact stock quantity and unit conversion")
        if value(row, "stock_uom") != item.stock_uom or quantity * factor != stock:
            raise ValueError(f"{label} stock quantity does not match the source Item unit conversion")
        return stock, factor

    order_items = [row for row in order.items if row.item_code == item_code]
    if len(order_items) != 1:
        return [], [], [], ["source Sales Order has no unique source product row"]
    order_row = order_items[0]
    if value(order_row, "stock_uom") != item.stock_uom:
        return [], [], [], ["source Sales Order row uses a different stock unit than its source Item"]
    try:
        stock_quantity(order_row, "source Sales Order row")
    except ValueError as error:
        return [], [], [], [str(error)]

    remaining = Decimal(mapped["quantity"])
    delivery_allocations: list[tuple[Any, Any, Decimal]] = []
    existing_documents = []
    for name in mapped["delivery_note_names"]:
        if not frappe.db.exists("Delivery Note", name):
            if remaining > 0:
                return [], [], existing_documents, [f"source Delivery Note {name} is not yet available"]
            continue
        note = frappe.get_doc("Delivery Note", name)
        if note.company != mapped["company"] or note.docstatus != 1 or note.is_return:
            return [], [], existing_documents, [f"source Delivery Note {name} is not a submitted sale for the bound company"]
        if str(note.posting_date) > mapped["occurred_at"].date().isoformat():
            return [], [], existing_documents, [f"native refund date predates source Delivery Note {name}"]
        if not note.items or any(value(row, "stock_uom") != item.stock_uom for row in note.items):
            return [], [], existing_documents, [f"source Delivery Note {name} does not retain the source Item stock unit"]
        rows = [row for row in note.items if row.against_sales_order == order.name and row.item_code == item_code]
        if len(rows) != 1:
            return [], [], existing_documents, [f"source Delivery Note {name} does not identify one exact source order item"]
        row = rows[0]
        try:
            delivered_stock_qty, _ = stock_quantity(row, f"source Delivery Note {name} row")
        except ValueError as error:
            return [], [], existing_documents, [str(error)]
        prior_return, prior_quantity = _existing_outcome_return(
            mapped, "Delivery Note", f"{mapped['source_id']}:return_dn:{name}",
            note.name, row.name, "dn_detail",
        )
        if prior_return:
            existing_documents.append(_outcome_document(prior_return))
            remaining -= prior_quantity
            if remaining < 0:
                raise RuntimeError("Existing E-Commerce Delivery Note returns exceed the native refund quantity")
        returned = get_returned_qty_map_for_row(
            note.name, note.customer, row.name, "Delivery Note"
        )
        returned = returned or {}
        available = delivered_stock_qty - Decimal(str(returned.get("stock_qty") or 0))
        if available < 0:
            raise RuntimeError(f"Native Delivery Note {name} reports returned quantity above delivery quantity")
        allocate = min(remaining, available) if remaining > 0 else Decimal(0)
        if allocate > 0:
            delivery_allocations.append((note, row, allocate))
            remaining -= allocate
        if remaining == 0:
            break
    if remaining > 0:
        return [], [], existing_documents, ["submitted source Delivery Notes do not cover the native returned quantity"]

    invoice_rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"sales_order": order.name, "item_code": item_code},
        fields=["parent", "name", "qty", "rate", "stock_qty", "conversion_factor", "stock_uom", "sales_order", "item_code"],
        limit_page_length=0,
    )
    by_invoice: dict[str, list[dict[str, Any]]] = {}
    for row in invoice_rows:
        by_invoice.setdefault(row.get("parent"), []).append(row)
    invoices = []
    later_invoice_seen = False
    for invoice_name, rows in sorted(by_invoice.items()):
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        if invoice.company != mapped["company"] or invoice.docstatus != 1 or invoice.is_return:
            continue
        if str(invoice.posting_date) > mapped["occurred_at"].date().isoformat():
            later_invoice_seen = True
            continue
        if len(rows) != 1:
            return [], [], existing_documents, [f"submitted Sales Invoice {invoice_name} has ambiguous source item rows"]
        row = rows[0]
        if value(row, "stock_uom") != item.stock_uom:
            return [], [], existing_documents, [f"submitted Sales Invoice {invoice_name} uses a different source Item stock unit"]
        try:
            invoiced_stock_qty, factor = stock_quantity(row, f"submitted Sales Invoice {invoice_name} row")
        except ValueError as error:
            return [], [], existing_documents, [str(error)]
        rate_per_stock_unit = Decimal(str(row.get("rate"))) / factor
        if rate_per_stock_unit != mapped["refund_per_unit"]:
            return [], [], existing_documents, [f"submitted Sales Invoice {invoice_name} rate differs from the exact native refund amount"]
        prior_return, prior_quantity = _existing_outcome_return(
            mapped, "Sales Invoice", f"{mapped['source_id']}:credit_note:{invoice.name}",
            invoice.name, row.get("name"), "sales_invoice_item",
        )
        if prior_return:
            existing_documents.append(_outcome_document(prior_return))
            # The invoice allocation quantity is evaluated below from the source receipt.
        returned = get_returned_qty_map_for_row(
            invoice.name, invoice.customer, row.get("name"), "Sales Invoice"
        )
        returned = returned or {}
        available = invoiced_stock_qty - Decimal(str(returned.get("stock_qty") or 0))
        if available < 0:
            raise RuntimeError(f"Native Sales Invoice {invoice_name} reports returned quantity above invoiced quantity")
        if available > 0 or prior_return:
            invoices.append((invoice, row, available, prior_quantity))
    remaining = Decimal(mapped["quantity"])
    invoice_allocations: list[tuple[Any, Any, Decimal]] = []
    for invoice, row, available, prior_quantity in invoices:
        remaining -= prior_quantity
        if remaining < 0:
            raise RuntimeError("Existing E-Commerce credit notes exceed the native refund quantity")
        allocate = min(remaining, available) if remaining > 0 else Decimal(0)
        if allocate > 0:
            invoice_allocations.append((invoice, row, allocate))
            remaining -= allocate
        if remaining == 0:
            break
    if remaining > 0:
        if later_invoice_seen:
            return [], [], existing_documents, ["native refund date predates the submitted Sales Invoice; no ERP posting date was substituted"]
        return [], [], existing_documents, ["submitted source Sales Invoices do not cover the native refund quantity"]
    return delivery_allocations, invoice_allocations, existing_documents, []


def _return_line_quantities(doctype: str, row: Any, source_stock_units: Decimal) -> tuple[float, float]:
    """Express an exact source-stock quantity in the ERP row's native UOM."""
    factor = _decimal(row.get("conversion_factor") if isinstance(row, dict)
                      else getattr(row, "conversion_factor", None), "return unit conversion", positive=True)
    precision = frappe.get_precision(f"{doctype} Item", "qty", doc=row)
    quantity = (source_stock_units / factor).quantize(Decimal(1).scaleb(-precision))
    if quantity * factor != source_stock_units:
        raise ValueError("source return quantity cannot be represented exactly in the original ERP unit")
    return -float(quantity), -float(source_stock_units)


def _existing_outcome_return(
    mapped: dict[str, Any], doctype: str, source_id: str,
    original_name: str, original_row_name: str, reference_field: str,
):
    existing = get_source_document(
        doctype, source_system=SOURCE_SYSTEM, source_id=source_id,
        record_type="native-refund-return", task_id=mapped["company_id"],
        experiment_id="background", company_id=mapped["company_id"],
    )
    if not existing:
        return None, Decimal(0)
    snapshot = json.loads(existing.custom_source_snapshot)
    if (snapshot.get("source") != SOURCE_SYSTEM
            or snapshot.get("upstream_commit") != UPSTREAM_COMMIT
            or snapshot.get("native_record") != mapped["facts"]
            or snapshot.get("return_against") != original_name
            or existing.return_against != original_name
            or existing.docstatus not in (0, 1)):
        raise RuntimeError("Existing E-Commerce return conflicts with its native source outcome")
    rows = [row for row in existing.items if getattr(row, reference_field, None) == original_row_name]
    if len(rows) != 1:
        raise RuntimeError("Existing E-Commerce return does not retain its exact native child-row link")
    stock_qty = abs(Decimal(str(getattr(rows[0], "stock_qty", 0))))
    if stock_qty <= 0 or Decimal(str(snapshot.get("returned_quantity"))) != stock_qty:
        raise RuntimeError("Existing E-Commerce return quantity conflicts with its durable source receipt")
    if existing.docstatus == 0:
        existing.submit()
    return existing, stock_qty


def _process_native_outcome(
    record: dict[str, Any], company_binding: dict[str, Any]
) -> dict[str, Any]:
    mapped = _map_native_outcome(record, company_binding)
    order = _source_sales_order(mapped)
    if order is None:
        return _pending_outcome(mapped, "source-linked Sales Order has not been imported yet")
    event_time = mapped["occurred_at"].date().isoformat()
    facts = mapped["facts"]
    if mapped["event_type"] == "shipment_cancelled":
        comment, created = _ensure_source_comment(
            source_id=mapped["source_id"], company_id=mapped["company_id"],
            experiment_id="background", reference_name=order.name,
            content=(
                f"Native E-CommerceBench shipment {facts['source_shipment_id']} was canceled "
                f"at {mapped['occurred_at'].isoformat()}; the source sale remained unshipped. "
                "The Sales Order and its commitments were left unchanged."
            ),
            snapshot={"source": SOURCE_SYSTEM, "upstream_commit": UPSTREAM_COMMIT, "native_record": facts},
        )
        return {
            "schema": "native_outcome_v1", "source_id": mapped["source_id"],
            "status": "completed", "documents": [_outcome_document(comment)],
            "pending_reasons": [], "created": created,
        }

    delivery_allocations, invoice_allocations, documents, pending_reasons = _allocated_return_rows(mapped, order)
    if pending_reasons:
        return _pending_outcome(mapped, *pending_reasons)
    created_any = False
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return as make_delivery_return
    for note, original_row, quantity in delivery_allocations:
        native_return = make_delivery_return(note.name)
        return_rows = [row for row in native_return.items if getattr(row, "dn_detail", None) == original_row.name]
        if len(return_rows) != 1:
            return _pending_outcome(mapped, f"native return mapper cannot uniquely link Delivery Note row {original_row.name}")
        if (getattr(return_rows[0], "stock_uom", None) != getattr(original_row, "stock_uom", None)
                or Decimal(str(getattr(return_rows[0], "conversion_factor", 0)))
                != Decimal(str(getattr(original_row, "conversion_factor", 0)))):
            return _pending_outcome(mapped, "native Delivery Note return mapper changed the original unit conversion")
        native_return.items = return_rows
        try:
            return_rows[0].qty, return_rows[0].stock_qty = _return_line_quantities(
                "Delivery Note", return_rows[0], quantity
            )
        except ValueError as error:
            return _pending_outcome(mapped, str(error))
        native_return.posting_date = event_time
        if getattr(native_return, "return_against", None) != note.name:
            raise RuntimeError("Native Delivery Note return mapper did not retain its original document link")
        return_id = f"{mapped['source_id']}:return_dn:{note.name}"
        return_doc, created = _ensure_source_return(
            native_return, doctype="Delivery Note", source_id=return_id,
            company_id=mapped["company_id"],
            snapshot={"source": SOURCE_SYSTEM, "upstream_commit": UPSTREAM_COMMIT,
                      "native_record": facts, "return_against": note.name,
                      "returned_quantity": format(quantity, "f")},
        )
        created_any = created_any or created
        documents.append(_outcome_document(return_doc))

    from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return as make_invoice_return
    for invoice, original_row, quantity in invoice_allocations:
        native_return = make_invoice_return(invoice.name)
        return_rows = [row for row in native_return.items if getattr(row, "sales_invoice_item", None) == original_row.get("name")]
        if len(return_rows) != 1:
            return _pending_outcome(mapped, f"native credit note mapper cannot uniquely link Sales Invoice row {original_row.get('name')}")
        if (getattr(return_rows[0], "stock_uom", None) != original_row.get("stock_uom")
                or Decimal(str(getattr(return_rows[0], "conversion_factor", 0)))
                != Decimal(str(original_row.get("conversion_factor", 0)))):
            return _pending_outcome(mapped, "native credit note mapper changed the original unit conversion")
        native_return.items = return_rows
        try:
            return_rows[0].qty, return_rows[0].stock_qty = _return_line_quantities(
                "Sales Invoice", return_rows[0], quantity
            )
        except ValueError as error:
            return _pending_outcome(mapped, str(error))
        native_return.posting_date = event_time
        if getattr(native_return, "return_against", None) != invoice.name:
            raise RuntimeError("Native credit note mapper did not retain its original invoice link")
        return_id = f"{mapped['source_id']}:credit_note:{invoice.name}"
        return_doc, created = _ensure_source_return(
            native_return, doctype="Sales Invoice", source_id=return_id,
            company_id=mapped["company_id"],
            snapshot={"source": SOURCE_SYSTEM, "upstream_commit": UPSTREAM_COMMIT,
                      "native_record": facts, "return_against": invoice.name,
                      "returned_quantity": format(quantity, "f"),
                      "refund_amount_native_units": format(mapped["refund_per_unit"] * quantity, "f")},
        )
        created_any = created_any or created
        documents.append(_outcome_document(return_doc))
    return {
        "schema": "native_outcome_v1", "source_id": mapped["source_id"],
        "status": "completed", "documents": documents,
        "pending_reasons": [], "created": created_any,
        "refund_accounting": {
            "gross_refund_native_units": format(mapped["amount_native_units"], "f"),
            "escrow_reclaimed_native_units": format(mapped["escrow_reclaimed_native_units"], "f"),
            "commission_reversed_native_units": format(mapped["commission_reversed_native_units"], "f"),
        },
    }


def _ensure_native_bank_transaction(mapped: dict[str, Any]) -> tuple[str, bool]:
    company_id, store_type = mapped["company_id"], mapped["store_type"]
    ledger_account, bank_account = _ensure_marketplace_bank(
        mapped["company"], mapped["currency"], store_type
    )
    snapshot = {
        "source": SOURCE_SYSTEM,
        "upstream_commit": UPSTREAM_COMMIT,
        "source_id": mapped["source_id"],
        "native_record": mapped["facts"],
        "currency_mapping": MONEY_UNIT,
        "account_mapping": {
            "company_account": ledger_account,
            "bank_account": bank_account,
            "store_type": store_type,
        },
    }
    existing = get_source_document(
        "Bank Transaction", source_system=SOURCE_SYSTEM,
        source_id=mapped["source_id"], record_type="bank-transaction",
        task_id=company_id, experiment_id="background", company_id=company_id,
    )
    if existing:
        if json.loads(existing.custom_source_snapshot) != snapshot:
            raise RuntimeError("Existing E-Commerce Bank Transaction conflicts with native receipt facts")
        _submit_imported_bank_transaction(existing)
        return existing.name, False
    descriptions = {
        "opening_balance": "E-CommerceBench native opening balance",
        "initial_store_setup": "E-CommerceBench initial store setup",
        "daily_native_cash_activity": "E-CommerceBench daily native cash activity",
        "supplier_purchase": "E-CommerceBench supplier purchase",
        "shipment": "E-CommerceBench shipping cost",
        "marketplace_settlement": "E-CommerceBench marketplace settlement",
    }
    description = descriptions.get(mapped["movement_type"])
    if not description:
        raise ValueError("Unsupported E-Commerce native bank movement type")
    values = {
        "date": mapped["occurred_at"].date().isoformat(),
        "bank_account": bank_account,
        "company": mapped["company"],
        "currency": mapped["currency"],
        "deposit": float(mapped["amount"]) if mapped["direction"] == "deposit" else 0,
        "withdrawal": float(mapped["amount"]) if mapped["direction"] == "withdrawal" else 0,
        "description": description,
        "reference_number": mapped["reference"],
        "transaction_id": mapped["source_id"],
    }
    transaction, created = insert_source_document(
        "Bank Transaction", values, source_system=SOURCE_SYSTEM,
        source_id=mapped["source_id"], record_type="bank-transaction",
        task_id=company_id, experiment_id="background", company_id=company_id,
        snapshot=snapshot,
    )
    _submit_imported_bank_transaction(transaction)
    return transaction.name, created


def _submit_imported_bank_transaction(transaction: Any) -> None:
    """Match ERPNext's native bank statement import lifecycle without posting GL."""
    if transaction.docstatus == 0:
        transaction.submit()
        transaction.reload()
    if transaction.docstatus != 1 or transaction.status not in {"Unreconciled", "Reconciled"}:
        raise RuntimeError(
            "Existing E-Commerce Bank Transaction is cancelled or has an unsupported native status"
        )


def _ensure_marketplace(
    store_type: str, company: str, company_id: str, currency: str
) -> tuple[str, bool]:
    source_id = f"marketplace:{store_type}"
    snapshot = {
        "source": SOURCE_SYSTEM,
        "upstream_commit": UPSTREAM_COMMIT,
        "counterparty_type": "marketplace",
        "store_type": store_type,
    }
    existing = get_source_document(
        "Customer",
        source_system=SOURCE_SYSTEM,
        source_id=source_id,
        record_type="counterparty",
        task_id=company_id,
        experiment_id="background",
        company_id=company_id,
    )
    created = False
    if existing:
        if json.loads(existing.custom_source_snapshot) != snapshot:
            raise RuntimeError(
                "Existing E-Commerce marketplace Customer conflicts with source identity"
            )
        customer = existing
    else:
        name = f"E-CommerceBench Marketplace ({store_type})"
        if frappe.db.exists("Customer", {"customer_name": name}):
            raise RuntimeError(
                "E-Commerce marketplace Customer name conflicts with an unlinked native Customer"
            )
        group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
        if not group:
            raise RuntimeError(
                "Native ERP lacks a leaf Customer Group for E-Commerce marketplace"
            )
        ensure_source_fields("Customer", "customer_name")
        customer, created = insert_source_document(
            "Customer",
            {
                "customer_name": name,
                "customer_group": group,
                "customer_type": "Company",
                "default_currency": currency,
            },
            source_system=SOURCE_SYSTEM,
            source_id=source_id,
            record_type="counterparty",
            task_id=company_id,
            experiment_id="background",
            company_id=company_id,
            snapshot=snapshot,
        )
    map_party_to_currency(
        customer,
        currency,
        company=company,
        newly_created=created,
        preferred_name=f"E-Commerce Marketplace {currency} Receivables",
    )
    return customer.name, created


def _map_pending_sale(
    record: dict[str, Any], binding: dict[str, Any]
) -> dict[str, Any]:
    if binding.get("sales_order_date_semantics") != DATE_SEMANTICS:
        raise ValueError(
            "E-Commerce Sales Order dates require the configured source dispatch-deadline semantics"
        )
    facts = _native_record(record)
    if record.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError(
            "E-Commerce source record is not from the pinned upstream revision"
        )
    source_id = record.get("id")
    store_id, store_type = facts.get("store_id"), facts.get("store_type")
    shipment_id, product_id = facts.get("shipment_id"), facts.get("product_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("E-Commerce source sale requires a stable shipment ID")
    if (
        isinstance(shipment_id, bool)
        or not isinstance(shipment_id, int)
        or source_id != f"{store_id}:shipment:{shipment_id}"
    ):
        raise ValueError(
            "E-Commerce source sale ID does not match its native store and shipment IDs"
        )
    if (
        not isinstance(store_id, str)
        or not store_id
        or not isinstance(store_type, str)
        or not store_type
    ):
        raise ValueError("E-Commerce source sale requires exact native store identity")
    if not isinstance(product_id, str) or not product_id:
        raise ValueError("E-Commerce source sale requires an exact native product ID")
    quantity = facts.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError(
            "E-Commerce source sale requires a positive whole-unit quantity"
        )
    currency, ratio, total = _validated_money(record, binding, facts)
    unit_price = _decimal(facts.get("unit_price"), "unit price") / ratio
    native_gross = _decimal(facts.get("revenue_gross"), "gross sale amount") / ratio
    if unit_price * quantity != native_gross:
        raise ValueError(
            "E-Commerce source gross sale does not equal native unit price times quantity"
        )
    product = facts.get("product")
    if not isinstance(product, dict) or product.get("product_id") != product_id:
        raise ValueError(
            "E-Commerce source sale is missing its exact product catalog record"
        )
    sale_date = facts.get("sale_date")
    ship_deadline = facts.get("ship_deadline")
    if not isinstance(sale_date, str) or not isinstance(ship_deadline, str):
        raise ValueError(
            "E-Commerce source sale requires native sale and shipping-deadline dates"
        )
    try:
        posting_date = date.fromisoformat(sale_date).isoformat()
        source_deadline = date.fromisoformat(ship_deadline).isoformat()
    except ValueError as error:
        raise ValueError(
            "E-Commerce source sale dates are not valid ISO dates"
        ) from error
    if record.get("source") != SOURCE_SYSTEM or record.get("kind") != "order":
        raise ValueError("E-Commerce source record is not a native pending sale")
    if record.get("party") != f"ecommercebench-marketplace:{store_type}":
        raise ValueError(
            "E-Commerce source sale counterparty does not match its native store"
        )
    context = {
        "native_action": "ship_orders",
        "native_actions": ["ship_orders", "withdraw"],
        "events": ["on_submit", "on_update_after_submit", "on_cancel", "on_trash"],
        "native_ids": {
            "shipment_id": shipment_id,
            "store_id": store_id,
            "product_id": product_id,
        },
        "source_item_code": f"ECOM-{product_id}",
        "source_quantity": quantity,
        "source_unit_price": format(unit_price, "f"),
        "source_gross_amount": format(native_gross, "f"),
        "source_net_amount": format(_decimal(facts.get("revenue_net"), "net sale amount") / ratio, "f"),
        "currency": currency,
        "document_fields": {"speed": "custom_source_shipping_speed"},
        "upstream_commit": UPSTREAM_COMMIT,
    }
    snapshot = _source_snapshot(record, facts, context)
    return {
        "facts": facts,
        "source_id": source_id,
        "store_id": store_id,
        "store_type": store_type,
        "shipment_id": shipment_id,
        "product_id": product_id,
        "quantity": quantity,
        "currency": currency,
        "unit_price": unit_price,
        "amount": total,
        "transaction_date": posting_date,
        "ship_deadline": source_deadline,
        "context": context,
        "snapshot": snapshot,
    }


def reconcile(
    source_records: list[dict[str, Any]],
    company_binding: dict[str, Any],
    as_of: str | datetime,
) -> dict[str, Any]:
    """Create/reuse source-backed draft Sales Orders for exact native pending sales."""
    company_id, company = company_binding.get("company_id"), company_binding.get("erp_company")
    if not isinstance(company_id, str) or not company_id or not isinstance(company, str) or not company:
        raise ValueError(
            "E-Commerce finance reconciliation requires the existing company ERP binding"
        )
    if not frappe.db.exists("Company", company):
        raise ValueError("Configured native ERP company does not exist")
    ensure_ecommerce_finance_contract()
    receipts: list[dict[str, Any]] = []
    unmapped: list[dict[str, str]] = []
    pending_outcomes: list[dict[str, Any]] = []
    for record in sorted(
        source_records, key=lambda row: (row.get("occurred_at", ""), row.get("id", ""))
    ):
        if record.get("source") != SOURCE_SYSTEM:
            raise ValueError(
                "E-Commerce finance operation received an unknown source provider"
            )
        try:
            if record.get("kind") == "bank_transaction":
                mapped_bank = _map_native_bank_movement(record, company_binding)
                transaction, created = _ensure_native_bank_transaction(mapped_bank)
                receipts.append({
                    "source_id": str(record["id"]),
                    "doctype": "Bank Transaction",
                    "name": transaction,
                    "created": created,
                })
                continue
            if record.get("kind") in {"catalog_product", "catalog_supplier"}:
                mapped_catalog = _map_catalog_record(record, company_binding)
                if mapped_catalog["kind"] == "catalog_product":
                    item_code, created = _ensure_item(
                        mapped_catalog["facts"], company_id
                    )
                    receipts.append({
                        "source_id": str(record["id"]), "doctype": "Item",
                        "name": item_code, "created": created,
                    })
                else:
                    supplier, created = _ensure_catalog_supplier(
                        mapped_catalog["facts"], company_id, MONEY_UNIT["currency"]
                    )
                    receipts.append({
                        "source_id": str(record["id"]), "doctype": "Supplier",
                        "name": supplier, "created": created,
                    })
                continue
            if record.get("kind") == "native_outcome":
                outcome_receipt = _process_native_outcome(record, company_binding)
                receipts.append(outcome_receipt)
                if outcome_receipt["status"] != "completed":
                    pending_outcomes.append({
                        "source_id": outcome_receipt["source_id"],
                        "pending_reasons": outcome_receipt["pending_reasons"],
                    })
                continue
            mapped = _map_pending_sale(record, company_binding)
        except ValueError as error:
            unmapped.append(
                {"source_id": str(record.get("id", "unknown")), "reason": str(error)}
            )
            continue
        item_code, item_created = _ensure_item(mapped["facts"], company_id)
        customer, customer_created = _ensure_marketplace(
            mapped["store_type"],
            company,
            company_id,
            mapped["currency"],
        )
        bank_ledger, bank_account = _ensure_marketplace_bank(
            company, mapped["currency"], mapped["store_type"]
        )
        context = {
            **mapped["context"],
            "source_item_code": item_code,
            "marketplace_customer": customer,
            "marketplace_bank_account": bank_ledger,
            "marketplace_bank_account_doc": bank_account,
        }
        snapshot = _source_snapshot(record, mapped["facts"], context)
        conversion = _conversion_rate(
            mapped["currency"],
            _company_currency(company),
            mapped["transaction_date"],
            "for_selling",
        )
        existing = get_source_document(
            "Sales Order",
            source_system=SOURCE_SYSTEM,
            source_id=mapped["source_id"],
            record_type="pending-sale",
            task_id=company_id,
            experiment_id="background",
            company_id=company_id,
        )
        created = False
        if existing:
            if json.loads(existing.custom_source_snapshot) != snapshot:
                raise RuntimeError(
                    "Existing E-Commerce Sales Order conflicts with immutable native sale facts"
                )
            order = existing
        else:
            values = {
                "company": company,
                "customer": customer,
                "transaction_date": mapped["transaction_date"],
                "currency": mapped["currency"],
                "conversion_rate": float(conversion),
                "custom_ecommerce_shipment_id": str(mapped["shipment_id"]),
                "custom_ecommerce_store_id": mapped["store_id"],
                "custom_ecommerce_product_id": mapped["product_id"],
                "custom_ecommerce_ship_deadline": mapped["ship_deadline"],
                "custom_ecommerce_date_semantics": DATE_MEANING,
                "delivery_date": mapped["ship_deadline"],
                "terms": DATE_MEANING,
                "items": [
                    {
                        "item_code": item_code,
                        "qty": mapped["quantity"],
                        "rate": float(mapped["unit_price"]),
                        "delivery_date": mapped["ship_deadline"],
                    }
                ],
            }
            order, created = insert_source_document(
                "Sales Order",
                values,
                source_system=SOURCE_SYSTEM,
                source_id=mapped["source_id"],
                record_type="pending-sale",
                task_id=company_id,
                experiment_id="background",
                company_id=company_id,
                snapshot=snapshot,
            )
        receipts.append(
            {
                "source_id": mapped["source_id"],
                "doctype": "Sales Order",
                "name": order.name,
                "created": created,
                "item_created": item_created,
                "customer_created": customer_created,
                "native_context": context,
            }
        )
    return {
        "as_of": _date(as_of), "receipts": receipts,
        "unmapped": unmapped, "pending_outcomes": pending_outcomes,
    }


__all__ = [
    "SOURCE_SYSTEM",
    "UPSTREAM_COMMIT",
    "ensure_ecommerce_finance_contract",
    "reconcile",
]
