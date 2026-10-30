"""Project explicitly mapped ERP evidence onto E-CommerceBench's native lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
from typing import Any
import uuid

from servers.simulation.ecommerce_background import (
    UPSTREAM_COMMIT,
    PROVIDER_ID,
    calendar_supply_conditions,
)
from servers.simulation.feedback_contracts import NativeActionReceipt
from servers.simulation.feedback_contracts import TranslatedAction
from servers.simulation.store import SQLiteStore, StoreConflict
from third_party.ecommercebench.upstream.tools.ecommerce_env import EcommerceEnv
from third_party.ecommercebench.upstream.tools.opponent.order_processor import (
    _process_order as process_native_supplier_order,
)


NATIVE_ECOMMERCE_SOURCE = "qwenlm-ecommercebench-native-economy"


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return value
    raise TypeError("E-Commerce feedback inputs must be contract models or mappings")


from .feedback_contracts import read_scalar as _field


def translate(
    mapping: Any,
    evidence: Any,
    *,
    shipment_evidence: list[Any] | None = None,
    invoice_evidence: list[Any] | None = None,
) -> dict[str, Any]:
    """Translate ERP delivery evidence into one whole native shipment.

    `native_context.native_ids` must bind `shipment_id`, `store_id`, and
    `product_id`; each submitted Delivery Note must map exact quantity and
    native speed. Upstream shipping is atomic per pending shipment, so multiple
    partial Delivery Notes are accepted together only when they total the exact
    source quantity and use one consistent speed. The speed is never defaulted
    because it changes native shipping cost, escrow, and return behavior.
    """
    route, source = _plain(mapping), _plain(evidence)

    def result(
        status: str,
        reason: str,
        coverage: list[str] | None = None,
        action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "action": action,
            "reason": reason,
            "coverage": coverage or [],
        }

    if route.get("company_id") != "company" or source.get("company_id") != "company":
        return result("unmapped", "evidence and mapping are not scoped to the company")
    if (
        route.get("benchmark") != "ecommercebench"
        or route.get("upstream_commit") != UPSTREAM_COMMIT
    ):
        return result(
            "unmapped", "mapping does not target the pinned E-CommerceBench provider"
        )
    linked_native_record = (
        route.get("erp_doctype") == "Sales Order"
        and source.get("erp_doctype") in {"Delivery Note", "Payment Entry"}
    )
    if route.get("erp_doctype") == "Item" and source.get("erp_doctype") == "Purchase Order":
        return _translate_supplier_purchase(route, source, result)
    if not linked_native_record:
        return result(
            "unmapped",
            "evidence does not identify the mapped ERP source or a supported native action",
        )
    context = route.get("native_context")
    if not isinstance(context, dict):
        return result(
            "inconclusive", "mapping has no explicit E-CommerceBench native context"
        )
    if context.get("native_action") != "ship_orders":
        return result(
            "unmapped",
            "ERP evidence has no equivalent supported E-CommerceBench action",
        )
    if source.get("event") not in context.get("events", []):
        return result(
            "unmapped", "company evidence event is not enabled by this mapping"
        )
    if source.get("erp_doctype") == "Payment Entry":
        return _translate_marketplace_withdrawal(
            route, source, context, invoice_evidence or [], result
        )
    if source.get("event") not in {"on_submit", "on_update_after_submit"}:
        return result("unmapped", "ERP delivery evidence is not an active submitted Delivery Note")
    native_ids, document_fields = (
        context.get("native_ids"),
        context.get("document_fields"),
    )
    if not isinstance(native_ids, dict) or not isinstance(document_fields, dict):
        return result(
            "inconclusive",
            "shipment mapping requires explicit native IDs and ERP fields",
        )
    required_ids = {"shipment_id", "store_id", "product_id"}
    required_fields = {"speed"}
    if (
        set(native_ids) != required_ids
        or set(document_fields) != required_fields
        or set(native_ids).intersection(document_fields)
    ):
        return result(
            "inconclusive",
            "shipment mapping must bind exact shipment, store, product, quantity, and speed",
        )
    if isinstance(native_ids["shipment_id"], bool) or not isinstance(
        native_ids["shipment_id"], int
    ):
        return result("inconclusive", "native shipment ID must be an integer")
    if any(
        not isinstance(native_ids[key], str) or not native_ids[key]
        for key in ("store_id", "product_id")
    ):
        return result(
            "inconclusive", "native store and product IDs must be non-empty strings"
        )
    source_item_code = context.get("source_item_code")
    source_quantity = context.get("source_quantity")
    if (
        not isinstance(source_item_code, str)
        or not source_item_code
        or isinstance(source_quantity, bool)
        or not isinstance(source_quantity, int)
        or source_quantity <= 0
    ):
        return result(
            "inconclusive",
            "Delivery Note mapping lacks exact source item and quantity facts",
        )
    candidates = shipment_evidence or [evidence]
    documents_by_name: dict[str, tuple[tuple[str, str, str], dict[str, Any]]] = {}
    for candidate in candidates:
        candidate_row = _plain(candidate)
        if (
            candidate_row.get("company_id") != "company"
            or candidate_row.get("erp_doctype") != "Delivery Note"
        ):
            continue
        candidate_document = candidate_row.get("document")
        if not isinstance(candidate_document, dict):
            continue
        name = str(candidate_row.get("erp_name", ""))
        if not name:
            continue
        modified = str(candidate_document.get("modified", ""))
        occurred_at = str(candidate_row.get("occurred_at", ""))
        event_id = str(candidate_row.get("evidence_id", ""))
        sort_key = (modified, occurred_at, event_id)
        prior = documents_by_name.get(name)
        if prior is not None and sort_key <= prior[0]:
            continue
        if candidate_row.get("event") not in {"on_submit", "on_update_after_submit"} or candidate_document.get("docstatus") != 1:
            documents_by_name[name] = (sort_key, {
                "evidence_id": event_id,
                "name": name,
                "quantity": Decimal(0),
                "speed": None,
                "company_time": candidate_row.get("company_time"),
                "active": False,
            })
            continue
        lines = candidate_document.get("items")
        if not isinstance(lines, list):
            documents_by_name[name] = (sort_key, {
                "evidence_id": event_id, "name": name, "quantity": Decimal(0),
                "speed": None, "company_time": candidate_row.get("company_time"),
                "active": False,
            })
            continue
        matching_lines = [
            line for line in lines
            if isinstance(line, dict)
            and line.get("against_sales_order") == route.get("erp_name")
            and line.get("item_code") == source_item_code
        ]
        if not matching_lines:
            documents_by_name[name] = (sort_key, {
                "evidence_id": event_id, "name": name, "quantity": Decimal(0),
                "speed": None, "company_time": candidate_row.get("company_time"),
                "active": False,
            })
            continue
        try:
            quantity = sum(Decimal(str(line["qty"])) for line in matching_lines)
        except (KeyError, InvalidOperation, ValueError):
            return result("inconclusive", "Delivery Note source item quantity is missing or invalid")
        if not quantity.is_finite() or quantity <= 0:
            return result("inconclusive", "Delivery Note source item quantity is invalid")
        speed_path = document_fields["speed"]
        try:
            speed = _field(candidate_document, speed_path)
        except (KeyError, ValueError) as error:
            return result("inconclusive", f"source ERP shipping speed is missing or invalid: {error.args[0]}")
        documents_by_name[name] = (sort_key, {
            "evidence_id": event_id,
            "name": name,
            "quantity": quantity,
            "speed": speed,
            "company_time": candidate_row.get("company_time"),
            "active": True,
        })
    selected = [item[1] for item in documents_by_name.values() if item[1]["active"]]
    if not selected:
        return result("unmapped", "No submitted Delivery Note item links the mapped source Sales Order and Item")
    if len(selected) != len({item["name"] for item in selected}):
        return result("inconclusive", "Delivery Note evidence does not identify unique native documents")
    delivered = sum((item["quantity"] for item in selected), Decimal(0))
    if not delivered.is_finite() or delivered != delivered.to_integral_value():
        return result("inconclusive", "Native E-CommerceBench shipment requires whole delivered units")
    if delivered < source_quantity:
        return result(
            "inconclusive",
            "Partial Delivery Note quantities are retained until they total the whole native shipment",
        )
    if delivered > source_quantity:
        return result("inconclusive", "ERP Delivery Note quantity exceeds the exact native shipment")
    speeds = {item["speed"] for item in selected}
    if len(speeds) != 1:
        return result("inconclusive", "Partial Delivery Notes specify inconsistent native shipping speeds")
    latest = max(
        selected,
        key=lambda item: next(row[0] for row in documents_by_name.values() if row[1]["name"] == item["name"]),
    )
    if latest["evidence_id"] != source.get("evidence_id"):
        return result("inconclusive", "Whole shipment action belongs to the last submitted Delivery Note")
    raw_quantity = int(delivered)
    try:
        quantity = (
            Decimal(str(raw_quantity))
            if not isinstance(raw_quantity, bool)
            else Decimal("NaN")
        )
    except (InvalidOperation, ValueError):
        quantity = Decimal("NaN")
    if not quantity.is_finite() or quantity <= 0:
        return result("inconclusive", "mapped shipment quantity must be positive")
    if quantity != quantity.to_integral_value():
        return result(
            "inconclusive", "native E-CommerceBench shipments require whole units"
        )
    quantity = int(quantity)
    speed = next(iter(speeds))
    if speed not in {"fast", "standard", "slow"}:
        return result(
            "inconclusive",
            "ERP evidence does not map to an exact native shipping speed",
        )
    mapping_id, evidence_id = (
        str(route.get("mapping_id", "")),
        str(source.get("evidence_id", "")),
    )
    if not mapping_id or not evidence_id:
        return result("inconclusive", "mapping and evidence require stable IDs")
    action_id = hashlib.sha256(f"{mapping_id}:{evidence_id}".encode()).hexdigest()
    delivery_evidence = sorted(selected, key=lambda item: (item["company_time"] or "", item["evidence_id"]))
    action = {
        "action_id": action_id,
        "mapping_id": mapping_id,
        "evidence_id": evidence_id,
        "benchmark": "ecommercebench",
        "native_action": "ship_orders",
        "supporting_evidence": [
            {"evidence_id": item["evidence_id"], "doctype": "Delivery Note", "name": item["name"]}
            for item in delivery_evidence
        ],
        "arguments": {
            **native_ids,
            "quantity": int(quantity),
            "speed": speed,
            "delivery_note_names": [item["name"] for item in delivery_evidence],
            "delivery_note_evidence_ids": [item["evidence_id"] for item in delivery_evidence],
        },
    }
    return result(
        "supported",
        "translated through the exact mapped E-CommerceBench shipment",
        [f"{PROVIDER_ID}:{UPSTREAM_COMMIT}", "EcommerceEnv.ship_orders"],
        action,
    )


def _translate_marketplace_withdrawal(route, source, context, invoice_evidence, result):
    if "withdraw" not in context.get("native_actions", []):
        return result("unmapped", "ERP source mapping has no native marketplace withdrawal action")
    if source.get("event") != "on_submit":
        return result("unmapped", "Marketplace withdrawal requires a submitted Payment Entry")
    payment = source.get("document")
    if not isinstance(payment, dict) or payment.get("docstatus") != 1:
        return result("inconclusive", "Payment Entry is not in submitted native state")
    expected_customer = context.get("marketplace_customer")
    expected_bank = context.get("marketplace_bank_account")
    expected_currency = context.get("currency")
    if not all(isinstance(value, str) and value for value in (expected_customer, expected_bank, expected_currency)):
        return result("inconclusive", "Marketplace withdrawal mapping lacks its native customer, bank, or currency")
    if (payment.get("payment_type") != "Receive" or payment.get("party_type") != "Customer"
            or payment.get("party") != expected_customer or payment.get("paid_to") != expected_bank):
        return result("unmapped", "Payment Entry is not a receipt from this marketplace into its mapped bank account")
    if (payment.get("paid_from_account_currency") != expected_currency
            or payment.get("paid_to_account_currency") != expected_currency):
        return result("inconclusive", "Payment Entry account currency does not match the native marketplace currency")
    references = payment.get("references")
    if not isinstance(references, list) or len(references) != 1:
        return result("inconclusive", "Marketplace withdrawal requires one exact Sales Invoice reference")
    reference = references[0]
    if not isinstance(reference, dict) or reference.get("reference_doctype") != "Sales Invoice":
        return result("unmapped", "Payment Entry does not reference a mapped Sales Invoice")
    invoice_name = reference.get("reference_name")
    invoice_rows = [
        _plain(row) for row in invoice_evidence
        if _plain(row).get("erp_doctype") == "Sales Invoice"
        and _plain(row).get("erp_name") == invoice_name
        and _plain(row).get("event") in {"on_submit", "on_update_after_submit"}
    ]
    invoice = max(invoice_rows, key=lambda row: (str(row.get("document", {}).get("modified", "")),
                                                  str(row.get("occurred_at", "")),
                                                  str(row.get("evidence_id", ""))), default=None)
    if invoice is None:
        return result("inconclusive", "Referenced Sales Invoice has no captured submitted company evidence")
    invoice_document = invoice.get("document")
    if not isinstance(invoice_document, dict) or invoice_document.get("docstatus") != 1:
        return result("inconclusive", "Referenced Sales Invoice is not in submitted native state")
    item_code = context.get("source_item_code")
    invoice_lines = invoice_document.get("items")
    if not isinstance(invoice_lines, list) or not isinstance(item_code, str):
        return result("inconclusive", "Referenced Sales Invoice lacks mapped source item lines")
    linked_lines = [
        row for row in invoice_lines
        if isinstance(row, dict)
        and row.get("sales_order") == route.get("erp_name")
        and row.get("item_code") == item_code
    ]
    if not linked_lines or len(linked_lines) != len(invoice_lines):
        return result("unmapped", "Sales Invoice does not contain only the exact mapped marketplace order item")
    try:
        amount = Decimal(str(payment["paid_amount"]))
        received = Decimal(str(payment["received_amount"]))
        allocated = Decimal(str(reference["allocated_amount"]))
    except (KeyError, InvalidOperation, ValueError):
        return result("inconclusive", "Payment Entry amount or allocation is missing or invalid")
    if (not amount.is_finite() or amount <= 0 or not received.is_finite()
            or not allocated.is_finite() or amount != received or amount != allocated
            or amount != amount.quantize(Decimal("0.01"))):
        return result("inconclusive", "Payment Entry amount is not an exact single-currency invoice receipt")
    mapping_id, evidence_id = str(route.get("mapping_id", "")), str(source.get("evidence_id", ""))
    if not mapping_id or not evidence_id:
        return result("inconclusive", "mapping and payment evidence require stable IDs")
    action_id = hashlib.sha256(f"{mapping_id}:{evidence_id}".encode()).hexdigest()
    action = {
        "action_id": action_id,
        "mapping_id": mapping_id,
        "evidence_id": evidence_id,
        "benchmark": "ecommercebench",
        "native_action": "withdraw",
        "supporting_evidence": [{
            "evidence_id": invoice.get("evidence_id"),
            "doctype": "Sales Invoice",
            "name": invoice_name,
        }],
        "arguments": {
            "company_id": route.get("company_id"),
            "store_id": context.get("native_ids", {}).get("store_id"),
            "amount": format(amount, "f"),
            "currency": expected_currency,
            "payment_entry": source.get("erp_name"),
            "sales_invoice": invoice_name,
        },
    }
    return result("supported", "translated through the exact native marketplace receipt", [
        f"{PROVIDER_ID}:{UPSTREAM_COMMIT}", "EcommerceEnv.withdraw",
    ], action)


def _translate_supplier_purchase(route, source, result):
    """Translate one submitted ERP Purchase Order through the upstream order processor."""
    if source.get("event") != "on_submit":
        return result("unmapped", "native supplier purchasing requires a submitted Purchase Order")
    document = source.get("document")
    context = route.get("native_context")
    if not isinstance(document, dict) or document.get("docstatus") != 1:
        return result("inconclusive", "Purchase Order is not in submitted native state")
    if not isinstance(context, dict):
        return result("inconclusive", "source Item mapping has no native supplier-order context")
    if context.get("native_action") != "process_structured_order":
        return result("unmapped", "source Item mapping has no native supplier-order action")
    native_ids = context.get("native_ids")
    if not isinstance(native_ids, dict):
        return result("inconclusive", "source Item mapping lacks exact native product identifiers")
    sku_id = native_ids.get("product_id")
    store_id = native_ids.get("store_id")
    source_item_code = context.get("source_item_code")
    if (not isinstance(sku_id, str) or not sku_id or not isinstance(store_id, str)
            or not store_id or source_item_code != route.get("erp_name")):
        return result("inconclusive", "source Item mapping does not identify its exact native SKU")
    supplier_id, supplier_name = document.get("supplier"), document.get("supplier_name")
    if (not isinstance(supplier_id, str) or not supplier_id
            or not isinstance(supplier_name, str) or not supplier_name):
        return result("inconclusive", "Purchase Order has no exact native supplier identity")
    lines = document.get("items")
    if not isinstance(lines, list) or not lines:
        return result("inconclusive", "Purchase Order has no native product lines")
    mapped_lines = []
    item_codes = []
    for line in lines:
        item_code = line.get("item_code") if isinstance(line, dict) else None
        if not isinstance(item_code, str) or not item_code.startswith("ECOM-"):
            return result("inconclusive", "Purchase Order contains a line without a pinned E-Commerce source Item")
        item_codes.append(item_code)
        try:
            quantity = Decimal(str(line["qty"]))
            agreed_price = Decimal(str(line["rate"]))
        except (KeyError, InvalidOperation, ValueError):
            return result("inconclusive", "Purchase Order line quantity or price is missing")
        if (not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value()
                or not agreed_price.is_finite() or agreed_price <= 0):
            return result("inconclusive", "Purchase Order terms do not map to native positive whole-unit purchase facts")
        mapped_lines.append({
            "sku_id": item_code.removeprefix("ECOM-"),
            "quantity": int(quantity),
            "unit_price": format(agreed_price, "f"),
        })
    if source_item_code not in item_codes:
        return result("unmapped", "Purchase Order does not reference this source Item")
    # The canonical first source-Item mapping owns one atomic action for the PO.
    # Other mapped line references remain visible but cannot submit duplicate orders.
    if source_item_code != min(item_codes):
        return result("unmapped", "another source Item mapping owns this Purchase Order action")
    if native_ids.get("product_id") != source_item_code.removeprefix("ECOM-"):
        return result("inconclusive", "source Item mapping does not match its upstream product ID")
    address = document.get("address_display")
    if not isinstance(address, str) or not address.strip():
        return result("inconclusive", "Purchase Order has no native shipping address")
    mapping_id, evidence_id = str(route.get("mapping_id", "")), str(source.get("evidence_id", ""))
    if not mapping_id or not evidence_id:
        return result("inconclusive", "source Item mapping and Purchase Order evidence require stable IDs")
    action_id = hashlib.sha256(f"{mapping_id}:{evidence_id}".encode()).hexdigest()
    action = {
        "action_id": action_id,
        "mapping_id": mapping_id,
        "evidence_id": evidence_id,
        "benchmark": "ecommercebench",
        "native_action": "process_structured_order",
        "supporting_evidence": [],
        "arguments": {
            "company_id": route.get("company_id"),
            "store_id": store_id,
            "store_type": context.get("store_type"),
            "purchase_order": source.get("erp_name"),
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "items": mapped_lines,
            "shipping_address": address,
        },
    }
    return result("supported", "translated through the pinned native supplier order processor", [
        f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
        "tools.opponent.order_processor.process_structured_order",
    ], action)


def apply_native(environment: Any, action: Any) -> dict[str, Any]:
    """Apply one translated shipment or marketplace withdrawal natively."""
    translated = TranslatedAction.model_validate(_plain(action)).model_dump(mode="json")
    if translated.get("benchmark") != "ecommercebench":
        return {
            "status": "unmapped",
            "reason": "action does not target the pinned E-CommerceBench provider",
        }
    args = translated.get("arguments")
    if not isinstance(args, dict):
        return {
            "status": "inconclusive",
            "reason": "translated shipment arguments are missing",
        }
    if translated.get("native_action") == "withdraw":
        if args.get("company_id") != "company" or not isinstance(args.get("store_id"), str):
            return {"status": "inconclusive", "reason": "marketplace withdrawal lacks the exact company store identity"}
        try:
            amount = Decimal(str(args.get("amount")))
        except (InvalidOperation, ValueError):
            amount = Decimal("NaN")
        if (not amount.is_finite() or amount <= 0
                or amount != amount.quantize(Decimal("0.01"))
                or args.get("currency") != "GBP"):
            return {"status": "inconclusive", "reason": "marketplace withdrawal amount or currency is invalid"}
        if args["store_id"] not in environment.stores:
            return {"status": "inconclusive", "reason": "marketplace withdrawal store is absent from native state"}
        balance_before = Decimal(str(environment.platform_wallet))
        if balance_before < amount:
            return {"status": "inconclusive", "reason": "native marketplace wallet does not contain the ERP receipt amount"}
        outcome = environment.withdraw(float(amount))
        if not outcome.get("success"):
            return {"status": "inconclusive", "reason": "native marketplace withdrawal was not accepted"}
        withdrawn = Decimal(str(outcome.get("withdrawn")))
        if withdrawn != amount:
            raise RuntimeError("Native marketplace withdrawal amount differs from the exact ERP receipt")
        return {
            "status": "accepted",
            "native_action": "withdraw",
            "action_id": translated["action_id"],
            "evidence": {
                "store_id": args["store_id"],
                "payment_entry": args.get("payment_entry"),
                "sales_invoice": args.get("sales_invoice"),
                "currency": args["currency"],
                "amount": format(withdrawn.quantize(Decimal("0.01")), ".2f"),
                "platform_wallet_before": format(balance_before, "f"),
                "platform_wallet_after": format(Decimal(str(environment.platform_wallet)), "f"),
                "bank_balance_after": format(Decimal(str(environment.bank_balance)), "f"),
            },
        }
    if translated.get("native_action") == "process_structured_order":
        if (args.get("company_id") != "company"
                or not isinstance(args.get("store_id"), str)
                or args.get("store_id") not in environment.stores):
            return {"status": "inconclusive", "reason": "supplier order lacks the exact company store identity"}
        supplier_name = args.get("supplier_name")
        supplier_id = args.get("supplier_id")
        order_lines = args.get("items")
        address = args.get("shipping_address")
        if (not isinstance(supplier_name, str) or not supplier_name
                or not isinstance(supplier_id, str) or not supplier_id
                or not isinstance(address, str) or not address.strip()
                or not isinstance(order_lines, list) or not order_lines):
            return {"status": "inconclusive", "reason": "supplier order has incomplete native identity or terms"}
        supplier_facts = environment.supplier_info.get(supplier_name)
        if not isinstance(supplier_facts, dict) or supplier_facts.get("supplier_id") != supplier_id:
            return {"status": "inconclusive", "reason": "supplier is not in the pinned native supplier roster"}
        if not isinstance(args.get("store_type"), str):
            return {"status": "inconclusive", "reason": "supplier order has no exact company store type"}
        processor_lines = []
        for line in order_lines:
            sku_id = line.get("sku_id") if isinstance(line, dict) else None
            quantity = line.get("quantity") if isinstance(line, dict) else None
            try:
                price = Decimal(str(line["unit_price"]))
            except (KeyError, InvalidOperation, ValueError, TypeError):
                return {"status": "inconclusive", "reason": "supplier order has invalid native line price"}
            if (not isinstance(sku_id, str) or not sku_id
                    or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
                    or not price.is_finite() or price <= 0):
                return {"status": "inconclusive", "reason": "supplier order has incomplete native line identity or terms"}
            product = environment.products.get(sku_id)
            if not isinstance(product, dict):
                return {"status": "inconclusive", "reason": "SKU is absent from the pinned native product catalog"}
            if product.get("store_type") != args["store_type"]:
                return {"status": "inconclusive", "reason": "SKU does not belong to the company store type"}
            processor_lines.append({
                "sku_id": sku_id,
                "product_name": product.get("title", sku_id)[:50],
                "quantity": quantity,
                "unit_price": float(price),
            })
        before = pickle.dumps(environment, protocol=pickle.HIGHEST_PROTOCOL)
        result = process_native_supplier_order(
            environment, supplier_name, processor_lines, address,
        )
        if not result.get("success"):
            restored = pickle.loads(before)
            environment.__dict__.clear()
            environment.__dict__.update(restored.__dict__)
            return {"status": "inconclusive", "reason": "native supplier order was not accepted",
                    "native_result": {"error": result.get("error")}}
        details = result.get("order_details") or []
        if len(details) != len(processor_lines):
            raise RuntimeError("Native supplier processor did not return every submitted line")
        arrivals = {
            row.get("order_id"): row
            for row in environment.pending_deliveries
        }
        native_orders = []
        for detail in details:
            delivery = arrivals.get(detail.get("order_id"))
            if not delivery or delivery.get("product_id") != detail.get("sku_id"):
                raise RuntimeError("Native supplier processor did not persist exact pending delivery facts")
            native_orders.append({
                "native_order_id": detail.get("order_id"),
                "sku_id": detail.get("sku_id"),
                "quantity_ordered": int(detail["quantity"]),
                "quantity_expected": int(delivery["quantity"]),
                "agreed_unit_price": detail.get("unit_price"),
                "native_eta_range_days": detail.get("eta_days"),
                "native_arrival_time": delivery["arrival_time"].isoformat(),
            })
        return {
            "status": "accepted", "native_action": "process_structured_order",
            "action_id": translated["action_id"],
            "evidence": {
                "purchase_order": args.get("purchase_order"),
                "supplier_name": supplier_name,
                "native_orders": native_orders,
                "native_total_charged": format(Decimal(str(result["total_charged"])), ".2f"),
                "bank_balance_after": format(Decimal(str(environment.bank_balance)), ".2f"),
                "shipping_address": address,
            },
        }
    if translated.get("native_action") != "ship_orders":
        return {
            "status": "unmapped",
            "reason": "action has no supported native E-CommerceBench operation",
        }
    shipment_id = args.get("shipment_id")
    quantity = args.get("quantity")
    speed = args.get("speed")
    if (
        isinstance(shipment_id, bool)
        or not isinstance(shipment_id, int)
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity <= 0
        or speed not in {"fast", "standard", "slow"}
    ):
        return {
            "status": "inconclusive",
            "reason": "translated shipment arguments are invalid",
        }
    rows = environment.get_pending_shipments().get("pending_shipments", [])
    matches = [row for row in rows if row.get("shipment_id") == shipment_id]
    if len(matches) != 1:
        return {
            "status": "inconclusive",
            "reason": "mapped native shipment is not uniquely pending",
        }
    pending = matches[0]
    for key in ("store_id", "product_id", "quantity"):
        if pending.get(key) != args.get(key):
            return {
                "status": "inconclusive",
                "reason": f"mapped native shipment {key} does not match pending upstream state",
            }
    result = environment.ship_orders(shipment_ids=[shipment_id], speed=speed)
    shipped = result.get("shipments", [])
    if (
        not result.get("success")
        or result.get("shipped_count") != 1
        or len(shipped) != 1
        or shipped[0].get("shipment_id") != shipment_id
        or shipped[0].get("product_id") != pending["product_id"]
        or shipped[0].get("quantity") != pending["quantity"]
    ):
        return {
            "status": "inconclusive",
            "reason": "native E-CommerceBench did not confirm the exact mapped shipment",
            "native_result": result,
        }
    return {
        "status": "accepted",
        "native_action": "ship_orders",
        "action_id": translated["action_id"],
        "evidence": {
            "shipment_id": shipment_id,
            "store_id": pending["store_id"],
            "product_id": pending["product_id"],
            "quantity": pending["quantity"],
            "speed": speed,
            "shipping_cost": result["total_shipping_cost"],
            "escrow": result["total_revenue_into_escrow"],
            "bank_balance_after": format(Decimal(str(environment.bank_balance)), "f"),
        },
    }


def initialize_native_store(
    *, seed: int, start_date: str, store_type: str, store_name: str
) -> tuple[Any, dict[str, Any]]:
    """Create a real pinned E-CommerceBench world and open its mapped store."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError(
            "E-CommerceBench initialization requires a non-negative integer seed"
        )
    if not all(
        isinstance(value, str) and value.strip()
        for value in (start_date, store_type, store_name)
    ):
        raise ValueError(
            "E-CommerceBench initialization requires explicit date, store type, and store name"
        )
    # Keep the upstream environment's own defaults (including its native
    # horizon); this adapter only supplies the configured identity and seed.
    environment = EcommerceEnv(seed=seed, start_date=start_date)
    opened = environment.open_store(store_type, store_name)
    if not opened.get("success") or not opened.get("store_id"):
        raise ValueError(
            "pinned E-CommerceBench could not open the explicitly mapped company store"
        )
    return environment, opened


class EcommerceNativeActionCheckpoint:
    """Atomically persist the trusted upstream environment and action receipt.

    The environment checkpoint is written to a private content file first; its
    pointer and the action receipt then commit in one existing SQLiteStore
    transaction. An interrupted pre-commit leaves the previous checkpoint live.
    """

    def __init__(self, store: SQLiteStore, checkpoint_directory: str | Path):
        self.store = store
        self.directory = Path(checkpoint_directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS ecommerce_native_world_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), checkpoint_path TEXT NOT NULL,
                upstream_commit TEXT NOT NULL, store_id TEXT NOT NULL, identity_json TEXT NOT NULL,
                company_cursor TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS ecommerce_native_action_receipts (
                action_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS ecommerce_native_advance_receipts (
                operation_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS ecommerce_native_source_observations (
                source_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS ecommerce_native_source_receipts (
                source_id TEXT PRIMARY KEY, doctype TEXT NOT NULL, document_name TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL)""")

    @staticmethod
    def _identity(
        profile: dict[str, Any], seed: int, started_at: datetime
    ) -> dict[str, Any]:
        company = profile.get("company", {})
        ecommerce = company.get("background", {}).get("ecommerce", {})
        company_id, company_name = company.get("id"), company.get("name")
        store_type = ecommerce.get("store_type")
        money_unit = ecommerce.get("money_unit")
        if (
            company_id != "company"
            or not isinstance(company_name, str)
            or not company_name.strip()
        ):
            raise ValueError(
                "E-Commerce native bootstrap requires the configured company identity"
            )
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError(
                "E-Commerce native bootstrap requires a non-negative integer seed"
            )
        if not isinstance(store_type, str) or not store_type:
            raise ValueError(
                "E-Commerce native bootstrap requires an explicit upstream store type"
            )
        if not isinstance(money_unit, dict):
            raise ValueError(
                "E-Commerce native bootstrap requires the approved native money-unit mapping"
            )
        currency = money_unit.get("currency")
        raw_units_per_currency = money_unit.get("native_units_per_currency_unit")
        try:
            units_per_currency = Decimal(str(raw_units_per_currency))
        except (InvalidOperation, ValueError):
            units_per_currency = Decimal("NaN")
        if (
            currency != "GBP"
            or not units_per_currency.is_finite()
            or units_per_currency != Decimal("1")
        ):
            raise ValueError(
                "E-Commerce native money-unit mapping must match the approved 1:1 GBP unit"
            )
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError(
                "E-Commerce native bootstrap requires a timezone-aware company start time"
            )
        return {
            "company_id": company_id,
            "company_name": company_name,
            "seed": seed,
            "started_at": started_at.astimezone(timezone.utc).isoformat(),
            "store_type": store_type,
            "money_unit": {
                "currency": currency,
                "native_units_per_currency_unit": format(units_per_currency, "f"),
            },
            "upstream_commit": UPSTREAM_COMMIT,
        }

    @staticmethod
    def _state_ref(path: str) -> str:
        return "ecommerce-native:" + hashlib.sha256(path.encode()).hexdigest()

    def source_item_context(
        self, source_snapshot: dict[str, Any], erp_document: dict[str, Any]
    ) -> dict[str, Any]:
        """Derive adapter context from immutable Item provenance and actual checkpoint state."""
        if (source_snapshot.get("source") != NATIVE_ECOMMERCE_SOURCE
                or source_snapshot.get("upstream_commit") != UPSTREAM_COMMIT):
            raise ValueError("E-Commerce source Item snapshot has an unsupported provider identity")
        product_id = source_snapshot.get("product_id")
        product_facts = source_snapshot.get("product")
        if (not isinstance(product_id, str) or not product_id
                or not isinstance(product_facts, dict)
                or erp_document.get("doctype") != "Item"
                or erp_document.get("item_code") != f"ECOM-{product_id}"
                or erp_document.get("custom_source_system") != NATIVE_ECOMMERCE_SOURCE
                or erp_document.get("custom_source_id") != product_id):
            raise ValueError("E-Commerce source Item document does not match immutable upstream identity")
        with self.store.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
        if state is None or state["upstream_commit"] != UPSTREAM_COMMIT:
            raise RuntimeError("E-Commerce source Item cannot be mapped before native bootstrap")
        identity = json.loads(state["identity_json"])
        checkpoint_bytes = Path(state["checkpoint_path"]).read_bytes()
        environment = pickle.loads(checkpoint_bytes)
        product = environment.products.get(product_id)
        projected_product = (
            {key: product.get(key) for key in (
                "product_id", "title", "category", "store_type", "brand", "size", "reference_price",
            )}
            if isinstance(product, dict) else None
        )
        if (not isinstance(product, dict)
                or product.get("store_type") != identity["store_type"]
                or projected_product != product_facts):
            raise ValueError("E-Commerce source Item facts do not match the pinned native catalog")
        return {
            "native_action": "process_structured_order",
            "native_actions": ["process_structured_order"],
            "native_ids": {"store_id": state["store_id"], "product_id": product_id},
            "source_item_code": f"ECOM-{product_id}",
            "store_type": identity["store_type"],
            "upstream_commit": UPSTREAM_COMMIT,
        }

    def bootstrap_from_profile(
        self, profile: dict[str, Any], seed: int, started_at: datetime | str
    ) -> dict[str, Any]:
        """Create the actual upstream retail world exactly once from company config."""
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        identity = self._identity(profile, seed, started_at)
        encoded_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if prior is not None:
                if (
                    prior["upstream_commit"] != UPSTREAM_COMMIT
                    or prior["identity_json"] != encoded_identity
                ):
                    raise StoreConflict(
                        "E-Commerce native world bootstrap identity changed"
                    )
                return {
                    "status": "already_initialized",
                    "store_id": prior["store_id"],
                    "native_state_ref": self._state_ref(prior["checkpoint_path"]),
                }
            environment, opened = initialize_native_store(
                seed=seed,
                start_date=identity["started_at"][:10],
                store_type=identity["store_type"],
                store_name=identity["company_name"],
            )
            path = self._write_checkpoint(environment)
            connection.execute(
                """INSERT INTO ecommerce_native_world_state
                (singleton,checkpoint_path,upstream_commit,store_id,identity_json,company_cursor,updated_at)
                VALUES(1,?,?,?,?,?,?)""",
                (
                    str(path),
                    UPSTREAM_COMMIT,
                    opened["store_id"],
                    encoded_identity,
                    identity["started_at"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return {
                "status": "initialized",
                "store_id": opened["store_id"],
                "store_type": identity["store_type"],
                "native_state_ref": self._state_ref(str(path)),
            }

    def initialize(
        self, environment: Any, *, store_id: str, identity: dict[str, Any]
    ) -> None:
        """Persist an already initialized pinned environment, primarily for fixtures."""
        if not isinstance(environment, EcommerceEnv):
            raise TypeError(
                "native checkpoint requires the pinned EcommerceEnv instance"
            )
        store = environment.stores.get(store_id)
        if store is None or not store.is_open:
            raise ValueError(
                "native checkpoint requires the explicitly mapped open company store"
            )
        encoded_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if prior is not None:
                if (
                    prior["identity_json"] == encoded_identity
                    and prior["store_id"] == store_id
                ):
                    return
                raise StoreConflict(
                    "E-Commerce native state is already initialized with different identity"
                )
            path = self._write_checkpoint(environment)
            connection.execute(
                """INSERT INTO ecommerce_native_world_state
                (singleton,checkpoint_path,upstream_commit,store_id,identity_json,company_cursor,updated_at)
                VALUES(1,?,?,?,?,?,?)""",
                (
                    str(path),
                    UPSTREAM_COMMIT,
                    store_id,
                    encoded_identity,
                    identity["started_at"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def capture_evaluation_snapshot(self) -> dict[str, Any]:
        """Capture immutable provider bytes and identity while excluding clock writers."""
        with self.store.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if state is None or state["upstream_commit"] != UPSTREAM_COMMIT:
                raise RuntimeError("pinned E-CommerceBench native world is not initialized")
            checkpoint_candidate = Path(state["checkpoint_path"])
            if checkpoint_candidate.is_symlink():
                raise RuntimeError("E-Commerce native checkpoint may not be a symlink")
            checkpoint_path = checkpoint_candidate.resolve(strict=True)
            if not checkpoint_path.is_relative_to(self.directory.resolve()):
                raise RuntimeError("E-Commerce native checkpoint is outside the configured private store")
            checkpoint_bytes = checkpoint_path.read_bytes()
            identity_json = state["identity_json"]
            identity = json.loads(identity_json)
            checkpoint_hash = hashlib.sha256(checkpoint_bytes).hexdigest()
            identity_fingerprint = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
            company_time = state["company_cursor"]
            state_ref = self._state_ref(str(checkpoint_path))
            store_id = state["store_id"]
        initialization_request_sha256 = self._evaluation_initialization_request_sha256()
        capture_id = hashlib.sha256(
            f"{UPSTREAM_COMMIT}\0{identity_fingerprint}\0{initialization_request_sha256}\0"
            f"{checkpoint_hash}\0{company_time}".encode("utf-8")
        ).hexdigest()
        return {
            "capture_id": capture_id,
            "upstream_commit": UPSTREAM_COMMIT,
            "native_state_ref": state_ref,
            "native_state_hash": checkpoint_hash,
            "native_company_time": company_time,
            "identity_fingerprint": identity_fingerprint,
            "initialization_request_sha256": initialization_request_sha256,
            "store_id": store_id,
            "store_type": identity["store_type"],
            "money_unit": identity["money_unit"],
            "checkpoint_path": str(checkpoint_path),
        }

    def evaluation_source_identity(self) -> dict[str, Any]:
        """Read only the pinned bootstrap identity needed before evaluation reservation."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT identity_json,upstream_commit FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
        if row is None or row["upstream_commit"] != UPSTREAM_COMMIT:
            raise RuntimeError("pinned E-CommerceBench native world is not initialized")
        identity = json.loads(row["identity_json"])
        fingerprint = hashlib.sha256(row["identity_json"].encode("utf-8")).hexdigest()
        return {
            "company_id": identity["company_id"],
            "provider_id": "ecommercebench",
            "upstream_commit": row["upstream_commit"],
            "seed": identity["seed"],
            "started_at": identity["started_at"],
            "identity_fingerprint": fingerprint,
            "initialization_request_sha256": self._evaluation_initialization_request_sha256(),
            "money_metric_units": {"profitability": identity["money_unit"]["currency"]},
        }

    def _evaluation_initialization_request_sha256(self) -> str:
        request = self.store.get_background_initialization_request("ecommercebench", "company")
        if not isinstance(request, dict):
            raise RuntimeError("E-CommerceBench background initialization provenance is missing")
        return hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def evaluate_captured_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Read provider analysis from captured bytes without advancing live state."""
        if not isinstance(snapshot, dict) or snapshot.get("upstream_commit") != UPSTREAM_COMMIT:
            raise ValueError("E-Commerce evaluation snapshot has an unsupported provider revision")
        candidate = Path(str(snapshot.get("checkpoint_path", "")))
        if candidate.is_symlink():
            raise ValueError("E-Commerce evaluation snapshot checkpoint may not be a symlink")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("E-Commerce evaluation snapshot is outside the configured private store")
        if self._state_ref(str(path)) != snapshot.get("native_state_ref"):
            raise ValueError("E-Commerce evaluation snapshot reference does not match its captured checkpoint")
        checkpoint_bytes = path.read_bytes()
        checkpoint_hash = hashlib.sha256(checkpoint_bytes).hexdigest()
        if checkpoint_hash != snapshot.get("native_state_hash"):
            raise StoreConflict("E-Commerce captured provider checkpoint changed after snapshot")
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT identity_json,store_id,upstream_commit FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
        if row is None or row["upstream_commit"] != UPSTREAM_COMMIT:
            raise RuntimeError("pinned E-CommerceBench native world is no longer initialized")
        identity = json.loads(row["identity_json"])
        identity_fingerprint = hashlib.sha256(row["identity_json"].encode("utf-8")).hexdigest()
        if (identity_fingerprint != snapshot.get("identity_fingerprint")
                or row["store_id"] != snapshot.get("store_id")
                or identity.get("store_type") != snapshot.get("store_type")
                or identity.get("money_unit") != snapshot.get("money_unit")):
            raise StoreConflict("E-Commerce evaluation snapshot belongs to a different company-world identity")
        if self._evaluation_initialization_request_sha256() != snapshot.get("initialization_request_sha256"):
            raise StoreConflict("E-Commerce evaluation snapshot belongs to a different initialization request")
        environment = pickle.loads(checkpoint_bytes)
        if row["store_id"] not in getattr(environment, "stores", {}):
            raise RuntimeError("E-Commerce captured checkpoint does not contain its initialized store")
        report = environment.get_analysis_report()
        if not isinstance(report, dict):
            raise TypeError("Pinned E-Commerce provider analysis report is not an object")
        return {
            "upstream_commit": UPSTREAM_COMMIT,
            "native_state_ref": snapshot["native_state_ref"],
            "native_state_hash": checkpoint_hash,
            "native_company_time": snapshot["native_company_time"],
            "identity_fingerprint": identity_fingerprint,
            "initialization_request_sha256": snapshot["initialization_request_sha256"],
            "scope": "shared_company",
            "native_status": "finished" if environment.is_done else "incomplete",
            "money_unit": snapshot["money_unit"],
            "report": report,
        }

    def verify_evaluation_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Verify exact captured evidence after evaluation without comparing live cursor."""
        if not isinstance(snapshot, dict) or snapshot.get("upstream_commit") != UPSTREAM_COMMIT:
            raise ValueError("E-Commerce evaluation snapshot has an unsupported provider revision")
        candidate = Path(str(snapshot.get("checkpoint_path", "")))
        if candidate.is_symlink():
            raise ValueError("E-Commerce evaluation snapshot checkpoint may not be a symlink")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("E-Commerce evaluation snapshot is outside the configured private store")
        if self._state_ref(str(path)) != snapshot.get("native_state_ref"):
            raise StoreConflict("E-Commerce evaluation snapshot reference changed")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != snapshot.get("native_state_hash"):
            raise StoreConflict("E-Commerce captured provider checkpoint changed after evaluation")
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT identity_json,store_id,upstream_commit FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
        if row is None or row["upstream_commit"] != UPSTREAM_COMMIT:
            raise RuntimeError("pinned E-CommerceBench native world is no longer initialized")
        identity_fingerprint = hashlib.sha256(row["identity_json"].encode("utf-8")).hexdigest()
        identity = json.loads(row["identity_json"])
        if (identity_fingerprint != snapshot.get("identity_fingerprint")
                or row["store_id"] != snapshot.get("store_id")
                or identity.get("store_type") != snapshot.get("store_type")
                or identity.get("money_unit") != snapshot.get("money_unit")
                or self._evaluation_initialization_request_sha256()
                != snapshot.get("initialization_request_sha256")):
            raise StoreConflict("E-Commerce evaluation snapshot belongs to a different company-world identity")
        return True

    def translate(self, mapping: Any, evidence: Any) -> dict[str, Any]:
        current = _plain(evidence)
        route = _plain(mapping)
        if route.get("erp_doctype") == "Item" and not route.get("native_context"):
            with self.store.transaction() as connection:
                stored_mapping = connection.execute(
                    "SELECT baseline_json FROM company_record_mappings WHERE mapping_id=?",
                    (route.get("mapping_id"),),
                ).fetchone()
            if stored_mapping is None:
                raise RuntimeError("E-Commerce Item feedback mapping has no original source baseline")
            baseline = json.loads(stored_mapping["baseline_json"])
            raw_snapshot = baseline.get("custom_source_snapshot")
            if isinstance(raw_snapshot, str):
                snapshot = json.loads(raw_snapshot)
            elif isinstance(raw_snapshot, dict):
                snapshot = raw_snapshot
            else:
                raise ValueError("E-Commerce source Item baseline has no native snapshot")
            # Older immutable snapshots carried product_id in a flattened field
            # alongside the product facts; reconstruct that same source shape
            # without changing the stored baseline or mapping.
            product_facts = snapshot.get("product")
            product_id = snapshot.get("product_id")
            if product_facts is None and isinstance(product_id, str):
                product_facts = {
                    key: snapshot.get(key) for key in (
                        "product_id", "title", "category", "store_type", "brand", "size", "reference_price",
                    ) if key in snapshot
                }
            source_snapshot = {
                "source": snapshot.get("source"),
                "upstream_commit": snapshot.get("upstream_commit"),
                "product_id": product_id,
                "product": product_facts,
            }
            route["native_context"] = self.source_item_context(source_snapshot, baseline)
        candidates = {str(current.get("evidence_id", "")): current}
        invoices = {}
        with self.store._connection(None) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_evidence'"
            ).fetchone()
            if exists:
                for row in connection.execute("SELECT payload_json FROM company_evidence"):
                    payload = json.loads(row["payload_json"])
                    evidence_id = str(payload.get("evidence_id", ""))
                    if evidence_id:
                        candidates[evidence_id] = payload
                        if payload.get("erp_doctype") == "Sales Invoice":
                            invoices[evidence_id] = payload
        return translate(
            route,
            current,
            shipment_evidence=list(candidates.values()),
            invoice_evidence=list(invoices.values()),
        )

    def advance_to(
        self, company_time: datetime | str, *, connection=None
    ) -> dict[str, Any]:
        """Advance the persisted native environment through actual daily triggers."""
        if isinstance(company_time, str):
            company_time = datetime.fromisoformat(company_time.replace("Z", "+00:00"))
        if company_time.tzinfo is None or company_time.utcoffset() is None:
            raise ValueError(
                "E-Commerce native advance requires a timezone-aware company time"
            )
        target = company_time.astimezone(timezone.utc)
        target_text = target.isoformat()
        request = {
            "company_id": "company",
            "until": target_text,
            "upstream_commit": UPSTREAM_COMMIT,
        }
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        operation_id = "clock:" + hashlib.sha256(request_json.encode()).hexdigest()
        transaction = (
            self.store.transaction()
            if connection is None
            else self.store._connection(connection)
        )
        with transaction as connection:
            prior = connection.execute(
                "SELECT request_json,result_json FROM ecommerce_native_advance_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_json"] != request_json:
                    raise StoreConflict(
                        "E-Commerce native clock operation identity was reused"
                    )
                return json.loads(prior["result_json"])
            state = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if state is None or state["upstream_commit"] != UPSTREAM_COMMIT:
                raise RuntimeError(
                    "pinned E-CommerceBench native world is not initialized"
                )
            previous_cursor = datetime.fromisoformat(state["company_cursor"])
            if target < previous_cursor:
                raise ValueError("E-Commerce native company clock cannot run backwards")
            identity = json.loads(state["identity_json"])
            with open(state["checkpoint_path"], "rb") as checkpoint:
                environment = pickle.load(checkpoint)
            target_native = target.replace(second=0, microsecond=0, tzinfo=None)
            native_events: list[dict[str, Any]] = []
            money_unit = identity["money_unit"]
            units_per_currency = Decimal(money_unit["native_units_per_currency_unit"])
            acknowledged_source_ids = {
                row["source_id"]
                for row in connection.execute(
                    "SELECT source_id FROM ecommerce_native_source_receipts"
                )
            }
            source_records_by_id: dict[str, dict[str, Any]] = {}
            # Advance results stay available until their exact ERP receipt is
            # acknowledged, so a restart before import can replay source facts.
            for stored in connection.execute(
                "SELECT result_json FROM ecommerce_native_advance_receipts ORDER BY committed_at"
            ):
                for source_record in json.loads(stored["result_json"]).get("source_records", []):
                    source_id = str(source_record.get("id", ""))
                    if source_id and source_id not in acknowledged_source_ids:
                        source_records_by_id.setdefault(source_id, source_record)
            unmapped_by_id: dict[str, dict[str, Any]] = {}
            native_outcomes: list[dict[str, Any]] = []
            unmapped_outcomes: list[dict[str, Any]] = []
            batch_to_shipment: dict[int, dict[str, Any]] = {}
            action_rows = connection.execute(
                "SELECT request_json,result_json FROM ecommerce_native_action_receipts"
            ).fetchall()
            for action_row in action_rows:
                request_action = json.loads(action_row["request_json"])
                action_result = json.loads(action_row["result_json"])
                evidence = action_result.get("outcome", {}).get("evidence", {})
                batch_id = evidence.get("batch_id")
                arguments = request_action.get("arguments", {})
                action_kind = request_action.get("native_action")
                if action_result.get("accepted") and action_kind in {
                    "ship_orders", "withdraw", "process_structured_order",
                }:
                    outcome = action_result.get("outcome", {})
                    occurred_at = outcome.get("company_time") or request_action.get("company_time")
                    if action_kind == "ship_orders":
                        direction, native_amount = "withdrawal", evidence.get("shipping_cost")
                        movement_type = "shipment"
                        reference = f"shipment:{evidence.get('shipment_id')}"
                    elif action_kind == "withdraw":
                        direction, native_amount = "deposit", evidence.get("amount")
                        movement_type = "marketplace_settlement"
                        reference = str(evidence.get("payment_entry") or "")
                    else:
                        direction, native_amount = "withdrawal", evidence.get("native_total_charged")
                        movement_type = "supplier_purchase"
                        reference = str(evidence.get("purchase_order") or "")
                    try:
                        event_time = datetime.fromisoformat(
                            str(occurred_at).replace("Z", "+00:00")
                        )
                        amount_native = Decimal(str(native_amount))
                    except (TypeError, ValueError, InvalidOperation):
                        event_time = None
                        amount_native = Decimal("NaN")
                    if (event_time is None or event_time.tzinfo is None
                            or event_time.utcoffset() is None or not amount_native.is_finite()
                            or amount_native <= 0):
                        unmapped_outcomes.append({
                            "event_type": "bank_movement", "source_action_id": action_result.get("action_id"),
                            "mapping_status": "inconclusive",
                            "reason": "Pinned E-Commerce bank movement lacks an exact amount or aware company-time receipt",
                        })
                    else:
                        action_id = str(action_result.get("action_id", ""))
                        source_id = f"bank:{state['store_id']}:{action_id}"
                        if source_id not in acknowledged_source_ids:
                            amount = amount_native / units_per_currency
                            bank_fact = {
                                "store_id": state["store_id"],
                                "store_type": identity["store_type"],
                                "action_id": action_id,
                                "movement_type": movement_type,
                                "direction": direction,
                                "amount": format(amount, "f"),
                                "amount_native_units": format(amount_native, "f"),
                                "currency": money_unit["currency"],
                                "occurred_at": event_time.astimezone(timezone.utc).isoformat(),
                                "reference": reference,
                                "bank_balance_after": outcome.get("evidence", {}).get("bank_balance_after"),
                            }
                            source_records_by_id[source_id] = {
                                "id": source_id,
                                "source": NATIVE_ECOMMERCE_SOURCE,
                                "kind": "bank_transaction",
                                "source_ref": action_id,
                                "occurred_at": bank_fact["occurred_at"],
                                "upstream_commit": UPSTREAM_COMMIT,
                                "party": f"ecommercebench-marketplace:{identity['store_type']}",
                                "currency": money_unit["currency"],
                                "amount": bank_fact["amount"],
                                "native_units_per_currency_unit": money_unit[
                                    "native_units_per_currency_unit"
                                ],
                                "native_record": bank_fact,
                            }
                if action_result.get("accepted") and isinstance(batch_id, int):
                    shipment = {
                        "source_id": f"{arguments.get('store_id')}:shipment:{arguments.get('shipment_id')}",
                        "store_id": arguments.get("store_id"),
                        "shipment_id": arguments.get("shipment_id"),
                        "product_id": arguments.get("product_id"),
                        "quantity": arguments.get("quantity"),
                        "delivery_note_names": list(arguments.get("delivery_note_names", [])),
                    }
                    previous = batch_to_shipment.get(batch_id)
                    if previous is not None and previous != shipment:
                        raise StoreConflict(
                            "E-Commerce settlement batch is linked to multiple native shipments"
                        )
                    batch_to_shipment[batch_id] = shipment

            opening_time = environment.start_time.replace(tzinfo=timezone.utc)
            opening_balance_id = f"bank:{state['store_id']}:opening_balance"
            if opening_balance_id not in acknowledged_source_ids:
                opening_amount = Decimal(str(environment.initial_balance))
                source_records_by_id[opening_balance_id] = {
                    "id": opening_balance_id,
                    "source": NATIVE_ECOMMERCE_SOURCE,
                    "kind": "bank_transaction",
                    "source_ref": "native-opening-balance",
                    "occurred_at": opening_time.isoformat(),
                    "upstream_commit": UPSTREAM_COMMIT,
                    "party": f"ecommercebench-marketplace:{identity['store_type']}",
                    "currency": money_unit["currency"],
                    "amount": format(opening_amount / units_per_currency, "f"),
                    "native_units_per_currency_unit": money_unit[
                        "native_units_per_currency_unit"
                    ],
                    "native_record": {
                        "store_id": state["store_id"],
                        "store_type": identity["store_type"],
                        "movement_type": "opening_balance",
                        "direction": "deposit",
                        "amount": format(opening_amount / units_per_currency, "f"),
                        "amount_native_units": format(opening_amount, "f"),
                        "currency": money_unit["currency"],
                        "occurred_at": opening_time.isoformat(),
                        "reference": "native-opening-balance",
                        "bank_balance_after": format(opening_amount, "f"),
                    },
                }
            setup_amount = Decimal(str(environment._ledger.get("setup", 0)))
            setup_id = f"bank:{state['store_id']}:initial_store_setup"
            if setup_amount > 0 and setup_id not in acknowledged_source_ids:
                source_records_by_id[setup_id] = {
                    "id": setup_id,
                    "source": NATIVE_ECOMMERCE_SOURCE,
                    "kind": "bank_transaction",
                    "source_ref": "native-store-setup",
                    "occurred_at": opening_time.isoformat(),
                    "upstream_commit": UPSTREAM_COMMIT,
                    "party": f"ecommercebench-marketplace:{identity['store_type']}",
                    "currency": money_unit["currency"],
                    "amount": format(setup_amount / units_per_currency, "f"),
                    "native_units_per_currency_unit": money_unit[
                        "native_units_per_currency_unit"
                    ],
                    "native_record": {
                        "store_id": state["store_id"],
                        "store_type": identity["store_type"],
                        "movement_type": "initial_store_setup",
                        "direction": "withdrawal",
                        "amount": format(setup_amount / units_per_currency, "f"),
                        "amount_native_units": format(setup_amount, "f"),
                        "currency": money_unit["currency"],
                        "occurred_at": opening_time.isoformat(),
                        "reference": "native-store-setup",
                        "bank_balance_after": format(
                            Decimal(str(environment.initial_balance)) - setup_amount, "f"
                        ),
                    },
                }

            def capture_pending(observed_at: datetime) -> None:
                observed_text = observed_at.astimezone(timezone.utc).isoformat()
                rows = environment.get_pending_shipments().get("pending_shipments", [])
                raw_pending = {
                    int(item["shipment_id"]): item
                    for item in environment.pending_shipments
                }
                for row in rows:
                    source_id = f"{state['store_id']}:shipment:{row['shipment_id']}"
                    if source_id in acknowledged_source_ids:
                        continue
                    raw = raw_pending.get(int(row["shipment_id"]))
                    product = environment.products.get(str(row["product_id"]))
                    if (
                        raw is None
                        or raw.get("store_id") != state["store_id"]
                        or not isinstance(product, dict)
                        or product.get("store_type") != identity["store_type"]
                    ):
                        unmapped_by_id[source_id] = {
                            "source_id": source_id,
                            "source_system": NATIVE_ECOMMERCE_SOURCE,
                            "upstream_commit": UPSTREAM_COMMIT,
                            "native_record_type": "pending_shipment",
                            "native_record": {
                                key: row[key]
                                for key in (
                                    "shipment_id",
                                    "store_id",
                                    "product_id",
                                    "quantity",
                                    "sale_date",
                                    "ship_deadline",
                                )
                                if key in row
                            },
                            "mapping_status": "unmapped",
                            "reason": "Pinned E-CommerceBench shipment is missing its exact company store or product facts",
                        }
                        continue
                    try:
                        quantity = Decimal(str(raw["quantity"]))
                        native_gross = Decimal(str(raw["revenue_gross"]))
                        unit_price = Decimal(str(raw["unit_price"]))
                        native_net = Decimal(str(raw["revenue_net"]))
                        commission = Decimal(str(raw["commission"]))
                    except (KeyError, InvalidOperation, ValueError):
                        quantity = native_gross = unit_price = native_net = (
                            commission
                        ) = Decimal("NaN")
                    if (
                        not quantity.is_finite()
                        or quantity <= 0
                        or quantity != quantity.to_integral_value()
                        or not native_gross.is_finite()
                        or native_gross < 0
                        or not unit_price.is_finite()
                        or unit_price < 0
                        or not native_net.is_finite()
                        or native_net < 0
                        or not commission.is_finite()
                        or commission < 0
                    ):
                        unmapped_by_id[source_id] = {
                            "source_id": source_id,
                            "source_system": NATIVE_ECOMMERCE_SOURCE,
                            "upstream_commit": UPSTREAM_COMMIT,
                            "native_record_type": "pending_shipment",
                            "native_record": dict(row),
                            "mapping_status": "unmapped",
                            "reason": "Pinned E-CommerceBench shipment has invalid native quantity or money facts",
                        }
                        continue
                    observation = connection.execute(
                        "SELECT observed_at FROM ecommerce_native_source_observations WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                    if observation is None:
                        stable_observed_at = observed_text
                        connection.execute(
                            "INSERT INTO ecommerce_native_source_observations(source_id,observed_at) VALUES(?,?)",
                            (source_id, stable_observed_at),
                        )
                    else:
                        stable_observed_at = observation["observed_at"]
                    native_record = {
                        "shipment_id": int(row["shipment_id"]),
                        "store_id": str(row["store_id"]),
                        "store_type": identity["store_type"],
                        "product_id": str(row["product_id"]),
                        "product": {
                            key: product.get(key)
                            for key in (
                                "product_id",
                                "title",
                                "category",
                                "store_type",
                                "brand",
                                "size",
                                "reference_price",
                            )
                        },
                        "quantity": int(quantity),
                        "unit_price": format(unit_price, "f"),
                        "revenue_gross": format(native_gross, "f"),
                        "revenue_net": format(native_net, "f"),
                        "commission": format(commission, "f"),
                        "sale_date": str(row["sale_date"]),
                        "ship_deadline": str(row["ship_deadline"]),
                    }
                    source_record = {
                        "id": source_id,
                        "source": NATIVE_ECOMMERCE_SOURCE,
                        "kind": "order",
                        "source_ref": source_id,
                        "occurred_at": stable_observed_at,
                        "upstream_commit": UPSTREAM_COMMIT,
                        "party": f"ecommercebench-marketplace:{identity['store_type']}",
                        "currency": money_unit["currency"],
                        "amount": format(native_gross / units_per_currency, "f"),
                        "observed_at": stable_observed_at,
                        "native_units_per_currency_unit": money_unit[
                            "native_units_per_currency_unit"
                        ],
                        "native_record": native_record,
                    }
                    prior_record = source_records_by_id.get(source_id)
                    if prior_record is not None and prior_record != source_record:
                        raise StoreConflict(
                            "E-Commerce shipment ID changed its native source facts during one clock advance"
                        )
                    source_records_by_id[source_id] = source_record

            def capture_company_catalog(observed_at: datetime) -> None:
                """Expose the pinned source catalog and supplier roster as native master data."""
                # Master data has no transaction time; use the source world's
                # own configured start anchor rather than inventing one per poll.
                observed_text = identity["started_at"]
                for product_id, product in sorted(environment.products.items()):
                    if product.get("store_type") != identity["store_type"]:
                        continue
                    source_id = f"catalog:{state['store_id']}:product:{product_id}"
                    if source_id in acknowledged_source_ids:
                        continue
                    source_record = {
                        "id": source_id,
                        "source": NATIVE_ECOMMERCE_SOURCE,
                        "kind": "catalog_product",
                        "source_ref": source_id,
                        "occurred_at": observed_text,
                        "upstream_commit": UPSTREAM_COMMIT,
                        "party": f"ecommercebench-marketplace:{identity['store_type']}",
                        # Catalog rows are non-monetary source master data.
                        # Do not fabricate a zero-valued financial fact.
                        "observed_at": observed_text,
                        "native_record": {
                            "store_id": state["store_id"],
                            "store_type": identity["store_type"],
                            "product_id": product_id,
                            "product": {
                                key: product.get(key)
                                for key in (
                                    "product_id", "title", "category", "store_type",
                                    "brand", "size", "reference_price",
                                )
                            },
                        },
                    }
                    source_records_by_id[source_id] = source_record
                catalog_categories = {
                    product.get("category") for product in environment.products.values()
                    if product.get("store_type") == identity["store_type"]
                    and isinstance(product.get("category"), str)
                }
                for supplier in sorted(
                    environment.suppliers_data,
                    key=lambda row: (str(row.get("supplier_id", "")), str(row.get("supplier_name", ""))),
                ):
                    supplier_id = str(supplier.get("supplier_id", ""))
                    supplier_name = supplier.get("supplier_name")
                    if not supplier_id or not isinstance(supplier_name, str) or not supplier_name:
                        continue
                    source_id = f"catalog:{state['store_id']}:supplier:{supplier_id}"
                    if source_id in acknowledged_source_ids:
                        continue
                    served_categories = set(str(supplier.get("categories_served", "")).split("|"))
                    if not served_categories.intersection(catalog_categories):
                        continue
                    source_record = {
                        "id": source_id,
                        "source": NATIVE_ECOMMERCE_SOURCE,
                        "kind": "catalog_supplier",
                        "source_ref": source_id,
                        "occurred_at": observed_text,
                        "upstream_commit": UPSTREAM_COMMIT,
                        "party": f"ecommercebench-marketplace:{identity['store_type']}",
                        # Supplier roster rows are non-monetary source master data.
                        "observed_at": observed_text,
                        "native_record": {
                            "store_id": state["store_id"],
                            "store_type": identity["store_type"],
                            "supplier_id": supplier_id,
                            "supplier_name": supplier_name,
                        },
                    }
                    source_records_by_id[source_id] = source_record

            # Preserve existing source sales before advancing; a large company
            # clock jump may cross their upstream shipping deadline.
            capture_company_catalog(previous_cursor)
            capture_pending(previous_cursor)
            while target_native > environment.current_time and not environment.is_done:
                trigger_time = environment._next_daily_trigger_time()
                step_target = min(target_native, trigger_time)
                minutes = int(
                    (step_target - environment.current_time).total_seconds() // 60
                )
                if minutes <= 0:
                    break
                before_pending = {
                    int(row["shipment_id"]): dict(row)
                    for row in environment.pending_shipments
                }
                before_returns = {
                    int(row["batch_id"]): dict(row)
                    for row in environment.pending_returns
                }
                before_escrow_by_batch = {
                    int(row["batch_id"]): Decimal(str(row.get("amount", 0)))
                    for row in environment.escrow_batches
                }
                before_bank_balance = Decimal(str(environment.bank_balance))
                before_ledger = {
                    key: Decimal(str(value)) for key, value in environment._ledger.items()
                }
                step_result = environment.advance_minutes(minutes)
                after_bank_balance = Decimal(str(environment.bank_balance))
                bank_delta = after_bank_balance - before_bank_balance
                native_events.extend(step_result.get("events", []))
                daily_summaries = [
                    event.get("summary", {})
                    for event in step_result.get("events", [])
                    if event.get("type") == "daily_trigger"
                ]
                if daily_summaries:
                    step_time = step_target.replace(tzinfo=timezone.utc)
                    capture_pending(step_time)
                    if bank_delta:
                        source_id = f"bank:{state['store_id']}:daily:{step_time.date().isoformat()}"
                        if source_id not in acknowledged_source_ids:
                            amount = abs(bank_delta) / units_per_currency
                            ledger_deltas = {
                                key: format(Decimal(str(value)) - before_ledger.get(key, Decimal(0)), "f")
                                for key, value in environment._ledger.items()
                                if Decimal(str(value)) != before_ledger.get(key, Decimal(0))
                            }
                            bank_fact = {
                                "store_id": state["store_id"],
                                "store_type": identity["store_type"],
                                "movement_type": "daily_native_cash_activity",
                                "direction": "deposit" if bank_delta > 0 else "withdrawal",
                                "amount": format(amount, "f"),
                                "amount_native_units": format(abs(bank_delta), "f"),
                                "currency": money_unit["currency"],
                                "occurred_at": step_time.isoformat(),
                                "reference": f"native-day:{step_time.date().isoformat()}",
                                "bank_balance_after": format(after_bank_balance, "f"),
                                "ledger_deltas_native_units": ledger_deltas,
                                "daily_summaries": daily_summaries,
                            }
                            source_records_by_id[source_id] = {
                                "id": source_id,
                                "source": NATIVE_ECOMMERCE_SOURCE,
                                "kind": "bank_transaction",
                                "source_ref": bank_fact["reference"],
                                "occurred_at": step_time.isoformat(),
                                "upstream_commit": UPSTREAM_COMMIT,
                                "party": f"ecommercebench-marketplace:{identity['store_type']}",
                                "currency": money_unit["currency"],
                                "amount": bank_fact["amount"],
                                "native_units_per_currency_unit": money_unit[
                                    "native_units_per_currency_unit"
                                ],
                                "native_record": bank_fact,
                            }
                    after_pending_ids = {
                        int(row["shipment_id"]) for row in environment.pending_shipments
                    }
                    cancelled = [
                        row
                        for shipment_id, row in before_pending.items()
                        if shipment_id not in after_pending_ids
                    ]
                    for summary in daily_summaries:
                        outcome_time = step_target.replace(
                            tzinfo=timezone.utc
                        ).isoformat()
                        cancelled_count = int(summary.get("orders_cancelled", 0) or 0)
                        if cancelled_count != len(cancelled):
                            if cancelled_count:
                                unmapped_outcomes.append(
                                    {
                                        "event_type": "shipment_cancellation",
                                        "date": summary.get("date"),
                                        "count": cancelled_count,
                                        "mapping_status": "unmapped",
                                        "reason": "Pinned E-CommerceBench cancellation output did not identify each removed shipment",
                                    }
                                )
                        else:
                            for row in cancelled:
                                cancellation_id = (
                                    f"outcome:{state['store_id']}:shipment_cancelled:{row['shipment_id']}"
                                )
                                cancellation_facts = {
                                    key: value.isoformat()
                                    if hasattr(value, "isoformat")
                                    else value
                                    for key, value in row.items()
                                    if key in (
                                        "shipment_id", "store_id", "product_id", "quantity",
                                        "unit_price", "revenue_gross", "revenue_net",
                                        "sale_date", "deadline",
                                    )
                                }
                                native_outcomes.append(
                                    {
                                        "event_type": "shipment_cancelled",
                                        "source_id": f"{row['store_id']}:shipment:{row['shipment_id']}",
                                        "occurred_at": outcome_time,
                                        "source": NATIVE_ECOMMERCE_SOURCE,
                                        "upstream_commit": UPSTREAM_COMMIT,
                                        "mapping_status": "source_linked",
                                        "erp_reversal_status": "not_applied",
                                        "reason": "Native cancellation is recorded without changing the employee-owned ERP Sales Order",
                                        "native_record": cancellation_facts,
                                    }
                                )
                                source_records_by_id.setdefault(cancellation_id, {
                                    "id": cancellation_id,
                                    "source": NATIVE_ECOMMERCE_SOURCE,
                                    "kind": "native_outcome",
                                    "source_ref": f"{row['store_id']}:shipment:{row['shipment_id']}",
                                    "occurred_at": outcome_time,
                                    "observed_at": outcome_time,
                                    "upstream_commit": UPSTREAM_COMMIT,
                                    "party": f"ecommercebench-marketplace:{identity['store_type']}",
                                    "currency": money_unit["currency"],
                                    "native_units_per_currency_unit": money_unit[
                                        "native_units_per_currency_unit"
                                    ],
                                    "native_record": {
                                        "event_type": "shipment_cancelled",
                                        "store_type": identity["store_type"],
                                        "source_shipment_id": f"{row['store_id']}:shipment:{row['shipment_id']}",
                                        **cancellation_facts,
                                    },
                                })
                        after_returns = {
                            int(row["batch_id"]): dict(row)
                            for row in environment.pending_returns
                        }
                        removed_returns = [
                            row
                            for batch_id, row in before_returns.items()
                            if batch_id not in after_returns
                        ]
                        refund_count = int(summary.get("returns_processed", 0) or 0)
                        escrow_consumed_by_batch: dict[int, Decimal] = {}
                        for row in removed_returns:
                            shipment = batch_to_shipment.get(int(row["batch_id"]))
                            try:
                                refund_amount = Decimal(
                                    str(row["refund_per_unit"])
                                ) * int(row["quantity"])
                            except (KeyError, InvalidOperation, ValueError):
                                refund_amount = Decimal("NaN")
                            if not refund_amount.is_finite() or refund_amount < 0:
                                unmapped_outcomes.append(
                                    {
                                        "event_type": "refund",
                                        "date": summary.get("date"),
                                        "count": 1,
                                        "mapping_status": "unmapped",
                                        "reason": "Pinned E-CommerceBench return has invalid refund amount facts",
                                    }
                                )
                                continue
                            refund_facts = {
                                "event_type": "refund_processed",
                                "store_type": identity["store_type"],
                                "batch_id": row["batch_id"],
                                "store_id": row["store_id"],
                                "shipment_id": shipment.get("shipment_id") if shipment else None,
                                "product_id": row["product_id"],
                                "quantity": row["quantity"],
                                "refund_per_unit": format(
                                    Decimal(str(row["refund_per_unit"])), "f"
                                ),
                                "refund_amount_native_units": format(refund_amount, "f"),
                                "escrow_reclaimed_native_units": format(
                                    min(
                                        max(
                                            before_escrow_by_batch.get(int(row["batch_id"]), Decimal(0))
                                            - escrow_consumed_by_batch.get(int(row["batch_id"]), Decimal(0)),
                                            Decimal(0),
                                        ),
                                        refund_amount,
                                    ),
                                    "f",
                                ),
                                "commission_reversed_native_units": format(
                                    refund_amount - min(
                                        max(
                                            before_escrow_by_batch.get(int(row["batch_id"]), Decimal(0))
                                            - escrow_consumed_by_batch.get(int(row["batch_id"]), Decimal(0)),
                                            Decimal(0),
                                        ),
                                        refund_amount,
                                    ),
                                    "f",
                                ),
                                "source_delivery_note_names": list(
                                    shipment.get("delivery_note_names", [])
                                ) if shipment else [],
                                "arrival_date": row["arrival_date"].isoformat()
                                if hasattr(row["arrival_date"], "isoformat")
                                else str(row["arrival_date"]),
                            }
                            escrow_consumed_by_batch[int(row["batch_id"])] = (
                                escrow_consumed_by_batch.get(int(row["batch_id"]), Decimal(0))
                                + Decimal(refund_facts["escrow_reclaimed_native_units"])
                            )
                            native_outcomes.append(
                                {
                                    "event_type": "refund_processed",
                                    "source_id": shipment["source_id"]
                                    if shipment
                                    else None,
                                    "occurred_at": outcome_time,
                                    "source": NATIVE_ECOMMERCE_SOURCE,
                                    "upstream_commit": UPSTREAM_COMMIT,
                                    "mapping_status": "source_linked"
                                    if shipment
                                    else "unmapped",
                                    "erp_reversal_status": "not_applied",
                                    "reason": None
                                    if shipment
                                    else "Native return batch has no exact accepted shipment action receipt",
                                    "currency": money_unit["currency"],
                                    "amount": format(
                                        refund_amount / units_per_currency, "f"
                                    ),
                                    "native_units_per_currency_unit": money_unit[
                                        "native_units_per_currency_unit"
                                    ],
                                    "native_record": refund_facts,
                                }
                            )
                            if shipment:
                                refund_id = (
                                    f"outcome:{state['store_id']}:refund:{row['batch_id']}"
                                )
                                source_records_by_id.setdefault(refund_id, {
                                    "id": refund_id,
                                    "source": NATIVE_ECOMMERCE_SOURCE,
                                    "kind": "native_outcome",
                                    "source_ref": shipment["source_id"],
                                    "occurred_at": outcome_time,
                                    "observed_at": outcome_time,
                                    "upstream_commit": UPSTREAM_COMMIT,
                                    "party": f"ecommercebench-marketplace:{identity['store_type']}",
                                    "currency": money_unit["currency"],
                                    "amount": format(refund_amount / units_per_currency, "f"),
                                    "native_units_per_currency_unit": money_unit[
                                        "native_units_per_currency_unit"
                                    ],
                                    "native_record": refund_facts,
                                })
                        if refund_count != len(removed_returns) and refund_count:
                            unmapped_outcomes.append(
                                {
                                    "event_type": "refund",
                                    "date": summary.get("date"),
                                    "count": refund_count - len(removed_returns),
                                    "mapping_status": "unmapped",
                                    "reason": "Pinned E-CommerceBench refund summary has no matching pending-return facts",
                                }
                            )
            native_result = {"events": native_events}
            # Include pending sales from the final native cursor even when the
            # requested boundary did not land on a daily trigger.
            capture_pending(target)
            calendar_conditions = calendar_supply_conditions(
                int(identity["seed"]),
                identity["store_type"],
                previous_cursor,
                target,
            )
            pending_rows = environment.get_pending_shipments().get(
                "pending_shipments", []
            )
            # Upstream transaction dates are dates, so occurred_at identifies
            # when this pending sale was actually observed on the company clock;
            # the original native sale date remains in native_record.sale_date.
            daily_triggers = [
                event.get("summary", {})
                for event in native_result.get("events", [])
                if event.get("type") == "daily_trigger"
            ]
            path = self._write_checkpoint(environment)
            result = {
                "status": "finished" if environment.is_done else "active",
                "company_time": target_text,
                "native_time": environment.current_time.isoformat(),
                "store_id": state["store_id"],
                "store_type": identity["store_type"],
                "daily_triggers": [
                    {"date": item.get("date"), "day": item.get("day")}
                    for item in daily_triggers
                ],
                "active_calendar_events": [
                    row.get("event_name") for row in environment.active_events
                ],
                "calendar_conditions": calendar_conditions,
                "pending_native_shipments": len(pending_rows),
                "source_records": list(source_records_by_id.values()),
                "unmapped_native_transactions": list(unmapped_by_id.values()),
                "native_outcomes": native_outcomes,
                "unmapped_native_outcomes": unmapped_outcomes,
                "coverage": {
                    "native_provider": f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
                    "currency_available": True,
                    "erp_item_mapping_available": True,
                    "transactions_mapped_to_erp": 0,
                    "cancellation_shipment_linkage": "derived_from_native_pending_state_transition",
                    "refund_shipment_linkage": "linked_via_native_shipment_batch_receipt",
                },
                "native_state_ref": self._state_ref(str(path)),
            }
            connection.execute(
                """UPDATE ecommerce_native_world_state
                SET checkpoint_path=?,company_cursor=?,updated_at=? WHERE singleton=1""",
                (str(path), target_text, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                "INSERT INTO ecommerce_native_advance_receipts VALUES(?,?,?,?)",
                (
                    operation_id,
                    request_json,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return result

    def acknowledge_source_receipts(self, receipts: list[dict[str, Any]]) -> None:
        """Stop replaying exact E-Commerce source facts only after native ERP receipts exist."""
        with self.store.transaction() as connection:
            state = connection.execute(
                "SELECT store_id FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if state is None:
                raise RuntimeError("E-Commerce source facts cannot be acknowledged before bootstrap")
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    raise ValueError("E-Commerce native catalog receipt must be an object")
                source_id = receipt.get("source_id")
                if receipt.get("schema") == "native_outcome_v1":
                    if not isinstance(source_id, str) or not source_id.startswith(
                        f"outcome:{state['store_id']}:"
                    ):
                        raise ValueError("E-Commerce native outcome receipt has an unknown source ID")
                    if receipt.get("status") not in {"completed", "pending"}:
                        raise ValueError("E-Commerce native outcome receipt has an invalid status")
                    if receipt.get("status") == "pending":
                        # A pending outcome remains in the durable source queue.
                        continue
                    if receipt.get("pending_reasons") != []:
                        raise ValueError("Completed E-Commerce native outcome receipt cannot retain pending reasons")
                    documents = receipt.get("documents")
                    if not isinstance(documents, list) or not documents:
                        raise ValueError("Completed E-Commerce outcome receipt must identify native documents")
                    normalized_documents = []
                    for document in documents:
                        if (not isinstance(document, dict)
                                or not isinstance(document.get("doctype"), str)
                                or not isinstance(document.get("name"), str)
                                or not document["name"]
                                or document.get("docstatus") not in (None, 0, 1)):
                            raise ValueError("E-Commerce outcome receipt contains an invalid native document reference")
                        if document.get("doctype") != "Comment" and document.get("docstatus") != 1:
                            raise ValueError("Completed E-Commerce outcome documents must be submitted")
                        normalized_documents.append({
                            "doctype": document["doctype"],
                            "name": document["name"],
                            "docstatus": document.get("docstatus"),
                        })
                    doctype = "native_outcome_v1"
                    name = json.dumps(
                        normalized_documents, sort_keys=True, separators=(",", ":")
                    )
                else:
                    doctype, name = receipt.get("doctype"), receipt.get("name")
                if not isinstance(source_id, str) or not isinstance(name, str) or not name:
                    raise ValueError("E-Commerce native catalog receipt lacks exact source and ERP identity")
                if doctype == "native_outcome_v1":
                    expected_doctype = "native_outcome_v1"
                elif source_id.startswith(f"catalog:{state['store_id']}:product:"):
                    expected_doctype = "Item"
                elif source_id.startswith(f"catalog:{state['store_id']}:supplier:"):
                    expected_doctype = "Supplier"
                elif source_id.startswith(f"bank:{state['store_id']}:"):
                    expected_doctype = "Bank Transaction"
                elif source_id.startswith(f"{state['store_id']}:shipment:"):
                    expected_doctype = "Sales Order"
                else:
                    continue
                if doctype != expected_doctype:
                    raise StoreConflict("E-Commerce source receipt has a mismatched ERP document type")
                existing = connection.execute(
                    "SELECT doctype,document_name FROM ecommerce_native_source_receipts WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                if existing is not None:
                    if (existing["doctype"], existing["document_name"]) != (doctype, name):
                        raise StoreConflict("E-Commerce source ID was acknowledged against conflicting ERP records")
                    continue
                connection.execute(
                    "INSERT INTO ecommerce_native_source_receipts VALUES(?,?,?,?)",
                    (source_id, doctype, name, datetime.now(timezone.utc).isoformat()),
                )

    def apply(self, action: Any) -> dict[str, Any]:
        translated = _plain(action)
        action_id = str(translated.get("action_id", ""))
        if not action_id:
            raise ValueError("native action requires a stable action ID")
        request_json = json.dumps(translated, sort_keys=True, separators=(",", ":"))
        raw_company_time = translated.get("company_time")
        action_time = None
        if raw_company_time is not None:
            action_time = datetime.fromisoformat(
                str(raw_company_time).replace("Z", "+00:00")
            )
            if action_time.tzinfo is None or action_time.utcoffset() is None:
                raise ValueError("native action company time must be timezone-aware")
            action_time = action_time.astimezone(timezone.utc)
        native_action_time = (
            action_time.replace(second=0, microsecond=0)
            if action_time is not None
            else None
        )
        with self.store.transaction() as connection:
            prior = connection.execute(
                "SELECT request_json,result_json FROM ecommerce_native_action_receipts WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_json"] != request_json:
                    raise StoreConflict(
                        "E-Commerce native action ID was reused for different evidence"
                    )
                return json.loads(prior["result_json"])
            state = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
            if state is None or state["upstream_commit"] != UPSTREAM_COMMIT:
                raise RuntimeError(
                    "pinned E-CommerceBench native state is not initialized"
                )
            if translated.get("arguments", {}).get("store_id") != state["store_id"]:
                raise StoreConflict(
                    "E-Commerce native action targets a store outside the initialized company mapping"
                )
            current_cursor = datetime.fromisoformat(state["company_cursor"])
            cursor_native = current_cursor.astimezone(timezone.utc).replace(
                second=0, microsecond=0
            )
            if native_action_time is not None and native_action_time < cursor_native:
                receipt = NativeActionReceipt(
                    action_id=action_id,
                    accepted=False,
                    reconciled=False,
                    native_state_ref=self._state_ref(state["checkpoint_path"]),
                    outcome={
                        "status": "inconclusive",
                        "reason": "company evidence predates the confirmed native E-Commerce clock",
                        "company_time": action_time.isoformat(),
                        "native_action_time": native_action_time.isoformat(),
                    },
                ).model_dump(mode="json")
                connection.execute(
                    "INSERT INTO ecommerce_native_action_receipts VALUES(?,?,?,?)",
                    (
                        action_id,
                        request_json,
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                return receipt
            if action_time is not None and action_time > current_cursor:
                # Hold this writer transaction through clock advancement and the
                # action receipt so another process cannot advance past evidence
                # time between the two native checkpoint mutations.
                self.advance_to(action_time, connection=connection)
                state = connection.execute(
                    "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
                ).fetchone()
            with open(state["checkpoint_path"], "rb") as checkpoint:
                environment = pickle.load(checkpoint)
            prior_batch_ids = {
                int(row["batch_id"]) for row in environment.escrow_batches
            }
            outcome = apply_native(environment, translated)
            if action_time is not None:
                outcome["company_time"] = action_time.isoformat()
                outcome["native_action_time"] = native_action_time.isoformat()
            accepted = outcome["status"] == "accepted"
            if accepted:
                if translated.get("native_action") == "ship_orders":
                    new_batches = [
                        int(row["batch_id"])
                        for row in environment.escrow_batches
                        if int(row["batch_id"]) not in prior_batch_ids
                    ]
                    if len(new_batches) != 1:
                        raise RuntimeError(
                            "Native E-Commerce shipment did not create one identifiable escrow batch"
                        )
                    outcome["evidence"]["batch_id"] = new_batches[0]
                path = self._write_checkpoint(environment)
                connection.execute(
                    "UPDATE ecommerce_native_world_state SET checkpoint_path=?,updated_at=? WHERE singleton=1",
                    (str(path), datetime.now(timezone.utc).isoformat()),
                )
                state_ref = self._state_ref(str(path))
            else:
                state_ref = self._state_ref(state["checkpoint_path"])
            receipt = NativeActionReceipt(
                action_id=action_id,
                accepted=accepted,
                reconciled=accepted,
                native_state_ref=state_ref,
                outcome=outcome,
            ).model_dump(mode="json")
            encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO ecommerce_native_action_receipts VALUES(?,?,?,?)",
                (
                    action_id,
                    request_json,
                    encoded,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return receipt

    def reconcile(self, action: Any) -> dict[str, Any] | None:
        translated = _plain(action)
        action_id = str(translated.get("action_id", ""))
        request_json = json.dumps(translated, sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT request_json,result_json FROM ecommerce_native_action_receipts WHERE action_id=?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        if row["request_json"] != request_json:
            raise StoreConflict(
                "E-Commerce native action ID was reused for different evidence"
            )
        return NativeActionReceipt.model_validate_json(row["result_json"]).model_dump(
            mode="json"
        )

    def evaluate(self, mappings: list[Any], evidence: list[Any]) -> dict[str, Any]:
        """Report mapping coverage and observed native receipts without inferring impact."""
        translations = []
        for mapping in mappings:
            route = _plain(mapping)
            candidates = []
            for candidate in evidence:
                source = _plain(candidate)
                document = source.get("document") or {}
                items = document.get("items", []) if isinstance(document, dict) else []
                if (
                    route.get("erp_doctype") == "Sales Order"
                    and source.get("erp_doctype") == "Delivery Note"
                    and source.get("event") == "on_submit"
                    and any(
                        isinstance(row, dict)
                        and row.get("against_sales_order") == route.get("erp_name")
                        for row in items
                    )
                ):
                    candidates.append(candidate)
            # A source Sales Order's unrelated save/submit events do not hide a
            # submitted Delivery Note that links to its exact native order row.
            if not candidates:
                translations.append(
                    {
                        "mapping_id": route.get("mapping_id"),
                        "status": "inconclusive",
                        "reason": "no submitted Delivery Note linked to the mapped source Sales Order",
                    }
                )
                continue
            translations.extend(self.translate(route, candidate) for candidate in candidates)
        actions_by_id = {
            row["action"]["action_id"]: row["action"]
            for row in translations
            if row.get("action")
        }
        actions = list(actions_by_id.values())
        receipts = [self.reconcile(action) for action in actions]
        completed = [
            receipt
            for receipt in receipts
            if receipt and receipt["accepted"] and receipt["reconciled"]
        ]
        with self.store.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM ecommerce_native_world_state WHERE singleton=1"
            ).fetchone()
        if state is None:
            raise RuntimeError(
                "Native E-CommerceBench evaluation requires its mapped checkpoint"
            )
        checkpoint_bytes = Path(state["checkpoint_path"]).read_bytes()
        native_state_hash = hashlib.sha256(checkpoint_bytes).hexdigest()
        environment = pickle.loads(checkpoint_bytes)
        native_analysis = environment.get_analysis_report()
        return {
            "evaluation_label": "company-adapted evaluation",
            "upstream_commit": UPSTREAM_COMMIT,
            "native_state_hash": native_state_hash,
            "native_company_time": state["company_cursor"],
            "scope": "shared_company",
            "native_evaluator": "EcommerceEnv.get_analysis_report",
            "native_status": "finished" if environment.is_done else "incomplete",
            "native_analysis": native_analysis,
            "translations": translations,
            "native_actions_supported": len(actions),
            "native_actions_reconciled": len(completed),
            "native_shipping_cost": sum(
                float(item["outcome"].get("evidence", {}).get("shipping_cost", 0))
                for item in completed
            ),
            "native_escrow": sum(
                float(item["outcome"].get("evidence", {}).get("escrow", 0))
                for item in completed
            ),
            "coverage": [
                f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
                "EcommerceEnv.ship_orders",
                "EcommerceEnv.get_analysis_report",
            ],
        }

    def _write_checkpoint(self, environment: Any) -> Path:
        descriptor, temporary = tempfile.mkstemp(
            prefix="ecommerce-native-", suffix=".tmp", dir=self.directory
        )
        path = self.directory / f"ecommerce-native-{uuid.uuid4().hex}.pickle"
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                pickle.dump(environment, output, protocol=pickle.HIGHEST_PROTOCOL)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            return path
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


__all__ = [
    "NATIVE_ECOMMERCE_SOURCE",
    "translate",
    "apply_native",
    "initialize_native_store",
    "EcommerceNativeActionCheckpoint",
]
