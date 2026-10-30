"""Exercise the real Frappe REST API and record a safe A1 state fingerprint."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path


def _adapter_type():
    spec = importlib.util.spec_from_file_location("erpnext_adapter", "/opt/erpnext_adapter.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("ERPNext adapter source is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ERPNextAdapter


def probe(site: str, request_review: bool = True) -> dict[str, object]:
    runtime = Path("/opt/erpnext-runtime")
    manifest = json.loads((runtime / f"{site}.json").read_text(encoding="utf-8"))
    credentials_path = runtime / "api-credentials" / f"{site}.json"
    if os.name == "posix" and credentials_path.stat().st_mode & 0o077:
        raise PermissionError("ERPNext API credentials must be private")
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    if credentials.get("site_name") != site or manifest.get("site_name") != site:
        raise RuntimeError("ERPNext runtime metadata does not match its owned site")

    adapter = _adapter_type()(
        "http://frontend:8080",
        credentials["api_key"],
        credentials["api_secret"],
        runtime / f"{site}.idempotency.sqlite",
        site,
    )
    po_id = manifest["purchase_order_id"]
    receipt_id = manifest["purchase_receipt_id"]
    invoice_id = manifest["purchase_invoice_id"]
    po = adapter.get_purchase_order(po_id)
    receipt = adapter.get_purchase_receipt(receipt_id)
    invoice = adapter.get_purchase_invoice(invoice_id)
    supplier = adapter.get_supplier(manifest["supplier_id"])
    customer = adapter.get_customer(manifest["customer_id"])
    sales_order = adapter.get_sales_order(manifest["sales_order_id"])
    sales_refunds = adapter.read_sales_invoice_refunds(manifest["sales_order_id"])
    assessment = adapter.assess_invoice(invoice_id)
    payment_status = adapter.read_payment_status(invoice_id)
    stock_balance = adapter.get_stock_balance(manifest["item_id"], manifest["warehouse_id"])

    snapshot = {
        "site_name": site,
        "purchase_order": {
            "name": po.get("name"), "docstatus": int(po["docstatus"]),
            "supplier": po.get("supplier"), "company": po.get("company"),
            "items": [{"name": row.get("name"), "item_code": row.get("item_code"),
                       "qty": str(Decimal(str(row["qty"]))),
                       "received_qty": str(Decimal(str(row.get("received_qty") or 0)))}
                      for row in po.get("items", [])],
        },
        "purchase_receipt": {
            "name": receipt.get("name"), "docstatus": int(receipt["docstatus"]),
            "supplier": receipt.get("supplier"),
            "items": [{"purchase_order": row.get("purchase_order"),
                       "purchase_order_item": row.get("purchase_order_item"),
                       "received_qty": str(Decimal(str(row.get("received_qty") or 0))),
                       "qty": str(Decimal(str(row.get("qty") or 0)))}
                      for row in receipt.get("items", [])],
        },
        "purchase_invoice": {
            "name": invoice.get("name"), "docstatus": int(invoice["docstatus"]),
            "workflow_state": invoice.get("workflow_state"),
            "bill_no": invoice.get("bill_no"), "grand_total": str(Decimal(str(invoice["grand_total"]))),
            "items": [{"purchase_order": row.get("purchase_order"), "po_detail": row.get("po_detail"),
                       "qty": str(Decimal(str(row["qty"])))} for row in invoice.get("items", [])],
        },
        "supplier": {"name": supplier.get("name"), "supplier_name": supplier.get("supplier_name")},
        "customer": {"name": customer.get("name"), "customer_name": customer.get("customer_name")},
        "sales_order": {"name": sales_order.get("name"), "customer": sales_order.get("customer"),
                        "docstatus": int(sales_order["docstatus"]),
                        "items": [{"item_code": row.get("item_code"), "qty": str(Decimal(str(row["qty"]))),
                                   "rate": str(Decimal(str(row["rate"]))) }
                                   for row in sales_order.get("items", [])]},
        "sales_refunds": sales_refunds,
        "assessment": assessment,
        "stock_balance": str(stock_balance),
        "payment_status": payment_status,
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

    reviewed = invoice
    if request_review:
        reviewed = adapter.request_invoice_review(invoice_id, f"{site}:a1-review")
    assessment = adapter.assess_invoice(invoice_id)
    passed = (
        po.get("docstatus") == 1
        and receipt.get("docstatus") == 1
        and invoice.get("docstatus") == 0
        and Decimal(assessment["lines"][0]["unreceived_billed_qty"]) == Decimal("2000")
        and Decimal(stock_balance) == Decimal("8000")
        and reviewed.get("docstatus") == 0
        and (not request_review or reviewed.get("workflow_state") == "Review Requested")
        and payment_status["payments"] == []
        and customer.get("name") == manifest["customer_id"]
        and sales_order.get("name") == manifest["sales_order_id"]
        and int(sales_order.get("docstatus", 0)) == 1
        and sales_refunds == []
    )
    result = {
        "passed": passed,
        "site_name": site,
        "fingerprint_before_review": fingerprint,
        "purchase_order_id": po_id,
        "purchase_receipt_id": receipt_id,
        "purchase_invoice_id": invoice_id,
        "supplier_id": manifest["supplier_id"],
        "customer_id": manifest["customer_id"],
        "sales_order_id": manifest["sales_order_id"],
        "ordered_qty": "10000",
        "received_qty": "8000",
        "billed_qty": "10000",
        "unreceived_billed_qty": str(assessment["lines"][0]["unreceived_billed_qty"]),
        "stock_balance": str(stock_balance),
        "invoice_docstatus": int(reviewed["docstatus"]),
        "invoice_workflow_state": reviewed.get("workflow_state"),
        "payment_entry_count": len(payment_status["payments"]),
        "sales_refund_count": len(sales_refunds),
    }
    (runtime / f"{site}.probe.json").write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    (runtime / f"{site}.probe.json").chmod(0o600)
    return result
