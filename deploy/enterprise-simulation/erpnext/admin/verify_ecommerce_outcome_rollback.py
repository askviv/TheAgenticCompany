"""Verify native E-Commerce returns and bank-source records inside a rollback-only fixture.

Custom-field schema setup runs before the savepoint and is an intentional
deployment migration. All source documents, stock/accounting entries, native
returns, and bank transactions created after the savepoint are rolled back.
No network calls, email, provider episodes, or benchmark actions are made.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
COMPANY = "The Agent Company"
SOURCE = "qwenlm-ecommercebench-native-economy"
COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"


def _fixture(now: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = uuid.uuid4().hex
    store_id = f"rollback-store-{token}"
    product_id = f"rollback-product-{token}"
    shipment_id = int(token[:7], 16) + 1
    sale_date = now[:10]
    deadline = sale_date
    shipment_source_id = f"{store_id}:shipment:{shipment_id}"
    gross = "37.50"
    order = {
        "id": shipment_source_id,
        "source": SOURCE,
        "kind": "order",
        "source_ref": shipment_source_id,
        "occurred_at": f"{sale_date}T09:00:00+00:00",
        "observed_at": f"{sale_date}T09:00:00+00:00",
        "upstream_commit": COMMIT,
        "party": "ecommercebench-marketplace:auto_hardware",
        "currency": "GBP",
        "amount": gross,
        "native_units_per_currency_unit": "1",
        "native_record": {
            "shipment_id": shipment_id,
            "store_id": store_id,
            "store_type": "auto_hardware",
            "product_id": product_id,
            "product": {
                "product_id": product_id,
                "title": f"Synthetic rollback item {token}",
                "category": "Auto Parts",
                "store_type": "auto_hardware",
                "brand": "Synthetic fixture",
                "size": "Small",
                "reference_price": "12.50",
            },
            "quantity": 3,
            "unit_price": "12.50",
            "revenue_gross": gross,
            "revenue_net": "36.75",
            "commission": "0.75",
            "sale_date": sale_date,
            "ship_deadline": deadline,
        },
    }
    binding = {
        "company_id": "company",
        "erp_company": COMPANY,
        "store_type": "auto_hardware",
        "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
        "sales_order_date_semantics": "source_dispatch_deadline",
    }
    return [order], {
        "store_id": store_id,
        "product_id": product_id,
        "shipment_id": shipment_id,
        "binding": binding,
        "sale_date": sale_date,
        "refund_time": datetime.fromisoformat(f"{sale_date}T12:00:00+00:00").isoformat(),
    }


def _create_native_delivery_and_invoice(frappe, order, warehouse, posting_date):
    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

    order.submit()
    delivery = make_delivery_note(order.name)
    delivery.posting_date = posting_date
    delivery.posting_time = "09:00:00"
    delivery.set_posting_time = 1
    delivery.custom_source_shipping_speed = "standard"
    for row in delivery.items:
        row.warehouse = warehouse
    delivery.insert(ignore_permissions=True)
    delivery.submit()

    invoice = make_sales_invoice(delivery.name)
    invoice.posting_date = posting_date
    invoice.posting_time = "09:05:00"
    invoice.set_posting_time = 1
    invoice.due_date = posting_date
    invoice.insert(ignore_permissions=True)
    invoice.submit()
    return delivery, invoice


def _stock_fixture(frappe, company: str, item_code: str, quantity: int):
    warehouse = frappe.db.get_value(
        "Warehouse", {"company": company, "is_group": 0, "disabled": 0}, "name"
    )
    if not warehouse:
        raise RuntimeError("Native rollback proof requires an existing leaf Warehouse")
    company_doc = frappe.get_doc("Company", company)
    expense_account = company_doc.stock_adjustment_account
    cost_center = company_doc.cost_center
    if not expense_account or not cost_center:
        raise RuntimeError("Native rollback proof requires the company stock-adjustment account and cost center")
    stock_entry = frappe.get_doc({
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Receipt",
        "company": company,
        "posting_date": frappe.utils.nowdate(),
        "posting_time": "08:00:00",
        "set_posting_time": 1,
        "items": [{
            "item_code": item_code,
            "qty": quantity,
            "t_warehouse": warehouse,
            "basic_rate": 5.0,
            "expense_account": expense_account,
            "cost_center": cost_center,
        }],
    })
    stock_entry.insert(ignore_permissions=True)
    stock_entry.submit()
    return warehouse


def run(site: str) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    import frappe
    import owned_site
    import ecommerce_finance

    lock_fd = owned_site.lock_for(site)
    db = None
    original_commit = None
    original_sql = None
    original_sql_ddl = None
    original_contract = None
    dispatch_patches = None
    captured_dispatches: list[str] = []
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        if not db.exists("Company", COMPANY):
            raise RuntimeError("Configured native E-Commerce finance company is unavailable")
        if db.get_value("Company", COMPANY, "default_currency") != "GBP":
            raise RuntimeError("Native E-Commerce outcome proof requires the configured GBP company profile")

        # Custom Field synchronization can execute schema DDL with implicit
        # commits. Complete this intentional migration before the fixture.
        with contextlib.redirect_stdout(sys.stderr):
            ecommerce_finance.ensure_ecommerce_finance_contract()
        db.commit()
        gl_before = db.count("GL Entry", {"company": COMPANY})
        stock_ledger_before = db.count("Stock Ledger Entry", {"company": COMPANY})
        default_bank_before = (
            db.get_value("Company", COMPANY, "default_bank_account")
            if frappe.get_meta("Company").has_field("default_bank_account") else None
        )
        marketplace_bank_before = db.get_value(
            "Bank Account", {"company": COMPANY, "account_name": "E-CommerceBench auto_hardware Marketplace Bank"}, "name"
        )
        records, ids = _fixture(frappe.utils.nowdate())
        original_commit = db.commit
        original_sql = db.sql
        original_sql_ddl = db.sql_ddl

        def reject_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Native E-Commerce rollback fixture attempted a database commit")

        def guarded_sql(query: Any, *args: Any, **kwargs: Any) -> Any:
            sql = query if isinstance(query, str) else str(query)
            forbidden = re.search(
                r"(?:^|;)\s*(?:ALTER|CREATE|DROP|TRUNCATE|RENAME|COMMIT|"
                r"LOCK|UNLOCK|GRANT|REVOKE|"
                r"SET\s+AUTOCOMMIT|START\s+TRANSACTION|BEGIN)\b",
                sql,
                re.IGNORECASE,
            )
            if forbidden:
                raise RuntimeError("Native E-Commerce rollback fixture attempted DDL or transaction-control SQL")
            return original_sql(query, *args, **kwargs)

        def reject_sql_ddl(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Native E-Commerce rollback fixture attempted DDL")

        db.commit = reject_commit
        db.sql = guarded_sql
        db.sql_ddl = reject_sql_ddl
        dispatch_patches = contextlib.ExitStack()

        def capture_external_dispatch(name: str):
            def capture(*args: Any, **kwargs: Any) -> None:
                captured_dispatches.append(name)
                return None
            return capture

        def reject_external_http(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Native E-Commerce rollback fixture attempted external HTTP")

        dispatch_patches.enter_context(patch.object(frappe, "enqueue", side_effect=capture_external_dispatch("enqueue")))
        dispatch_patches.enter_context(patch.object(frappe, "publish_realtime", side_effect=capture_external_dispatch("realtime")))
        import frappe.utils.background_jobs
        dispatch_patches.enter_context(patch.object(frappe.utils.background_jobs, "enqueue", side_effect=capture_external_dispatch("enqueue")))
        dispatch_patches.enter_context(patch.object(frappe.utils.background_jobs, "enqueue_doc", side_effect=capture_external_dispatch("enqueue_doc")))
        import frappe.realtime
        dispatch_patches.enter_context(patch.object(frappe.realtime, "publish_realtime", side_effect=capture_external_dispatch("realtime")))
        import frappe.email
        dispatch_patches.enter_context(patch.object(frappe.email, "sendmail", side_effect=capture_external_dispatch("sendmail")))
        if hasattr(frappe, "sendmail"):
            dispatch_patches.enter_context(patch.object(frappe, "sendmail", side_effect=capture_external_dispatch("sendmail")))
        import requests.sessions
        dispatch_patches.enter_context(patch.object(requests.sessions.Session, "request", side_effect=reject_external_http))
        try:
            import httpx
        except ImportError:
            httpx = None
        if httpx is not None:
            dispatch_patches.enter_context(patch.object(httpx.Client, "request", side_effect=reject_external_http))
            dispatch_patches.enter_context(patch.object(httpx.AsyncClient, "request", side_effect=reject_external_http))
        original_contract = ecommerce_finance.ensure_ecommerce_finance_contract
        ecommerce_finance.ensure_ecommerce_finance_contract = lambda: None
        with contextlib.redirect_stdout(sys.stderr):
            source_result = ecommerce_finance.reconcile(records, ids["binding"], ids["sale_date"])
        if source_result["unmapped"] or len(source_result["receipts"]) != 1:
            raise AssertionError("Synthetic source sale did not create one native Sales Order")
        order = frappe.get_doc("Sales Order", source_result["receipts"][0]["name"])
        if order.docstatus != 0 or order.company != COMPANY:
            raise AssertionError("Synthetic source Sales Order is not a draft for the bound company")

        warehouse = _stock_fixture(
            frappe, COMPANY, f"ECOM-{ids['product_id']}", 3
        )
        delivery, invoice = _create_native_delivery_and_invoice(
            frappe, order, warehouse, ids["sale_date"]
        )

        refund_source_id = f"outcome:{ids['store_id']}:refund:{ids['shipment_id']}"
        refund_record = {
            "id": refund_source_id,
            "source": SOURCE,
            "kind": "native_outcome",
            "source_ref": f"{ids['store_id']}:shipment:{ids['shipment_id']}",
            "occurred_at": ids["refund_time"],
            "upstream_commit": COMMIT,
            "currency": "GBP",
            "amount": "37.50",
            "native_units_per_currency_unit": "1",
            "native_record": {
                "event_type": "refund_processed",
                "store_id": ids["store_id"],
                "store_type": "auto_hardware",
                "shipment_id": ids["shipment_id"],
                "batch_id": ids["shipment_id"],
                "product_id": ids["product_id"],
                "quantity": 3,
                "refund_per_unit": "12.50",
                "refund_amount_native_units": "37.50",
                "escrow_reclaimed_native_units": "36.75",
                "commission_reversed_native_units": "0.75",
                "source_delivery_note_names": [delivery.name],
                "arrival_date": ids["refund_time"][:10],
            },
        }
        original_ensure_return = ecommerce_finance._ensure_source_return
        injected = {"raised": False}

        def interrupt_between_native_returns(doc, *, doctype, **kwargs):
            if doctype == "Sales Invoice" and not injected["raised"]:
                injected["raised"] = True
                raise RuntimeError("synthetic interruption between native return documents")
            return original_ensure_return(doc, doctype=doctype, **kwargs)

        ecommerce_finance._ensure_source_return = interrupt_between_native_returns
        try:
            try:
                ecommerce_finance._process_native_outcome(refund_record, ids["binding"])
            except RuntimeError as error:
                if str(error) != "synthetic interruption between native return documents":
                    raise
            else:
                raise AssertionError("Synthetic interruption did not occur between native returns")
        finally:
            ecommerce_finance._ensure_source_return = original_ensure_return

        partial_return = frappe.get_all(
            "Delivery Note",
            filters={"custom_source_system": SOURCE,
                     "custom_source_id": f"{refund_source_id}:return_dn:{delivery.name}"},
            fields=["name", "docstatus"], limit_page_length=0,
        )
        if len(partial_return) != 1 or partial_return[0].docstatus != 1:
            raise AssertionError("Interrupted composite outcome did not persist exactly one submitted Return Delivery Note")
        completed = ecommerce_finance._process_native_outcome(refund_record, ids["binding"])
        replayed = ecommerce_finance._process_native_outcome(refund_record, ids["binding"])
        if completed["schema"] != "native_outcome_v1" or completed["status"] != "completed":
            raise AssertionError("Retry did not complete the native composite return receipt")
        if completed["documents"] != replayed["documents"] or len(completed["documents"]) != 2:
            raise AssertionError("Native composite return replay changed or duplicated document references")
        return_dn_name = next(
            row["name"] for row in completed["documents"] if row["doctype"] == "Delivery Note"
        )
        return_dn = frappe.get_doc("Delivery Note", return_dn_name)
        credit_note_name = next(
            row["name"] for row in completed["documents"] if row["doctype"] == "Sales Invoice"
        )
        credit_note = frappe.get_doc("Sales Invoice", credit_note_name)
        if (return_dn.return_against != delivery.name or return_dn.docstatus != 1
                or credit_note.return_against != invoice.name or credit_note.docstatus != 1):
            raise AssertionError("Native return documents do not link to their original submitted records")

        today = ids["sale_date"]
        bank_record = {
            "id": f"bank:{ids['store_id']}:daily:{today}",
            "source": SOURCE,
            "kind": "bank_transaction",
            "source_ref": f"native-day:{today}",
            "occurred_at": f"{today}T10:00:00+00:00",
            "upstream_commit": COMMIT,
            "party": "ecommercebench-marketplace:auto_hardware",
            "currency": "GBP",
            "amount": "1.25",
            "native_units_per_currency_unit": "1",
            "native_record": {
                "store_id": ids["store_id"], "store_type": "auto_hardware",
                "movement_type": "daily_native_cash_activity", "direction": "deposit",
                "amount": "1.25", "amount_native_units": "1.25", "currency": "GBP",
                "occurred_at": f"{today}T10:00:00+00:00",
                "reference": f"native-day:{today}", "bank_balance_after": "100001.25",
            },
        }
        bank_gl_before = db.count("GL Entry", {"company": COMPANY})
        bank_result = ecommerce_finance.reconcile([bank_record], ids["binding"], today)
        if len(bank_result["receipts"]) != 1 or bank_result["receipts"][0]["doctype"] != "Bank Transaction":
            raise AssertionError("Exact native marketplace cash movement did not create one Bank Transaction")
        bank_transaction = frappe.get_doc("Bank Transaction", bank_result["receipts"][0]["name"])
        if (bank_transaction.docstatus != 1 or bank_transaction.status != "Unreconciled"
                or bank_transaction.deposit != 1.25 or bank_transaction.withdrawal != 0):
            raise AssertionError("Native marketplace cash movement did not reach ERPNext imported statement status")
        if db.count("GL Entry", {"company": COMPANY}) != bank_gl_before:
            raise AssertionError("Native bank statement import unexpectedly posted a general-ledger entry")
        marketplace_bank = frappe.get_doc("Bank Account", bank_transaction.bank_account)
        if (marketplace_bank.account_name != "E-CommerceBench auto_hardware Marketplace Bank"
                or marketplace_bank.company != COMPANY or not marketplace_bank.is_company_account
                or marketplace_bank.is_default):
            raise AssertionError("E-Commerce source cash is not assigned to a dedicated non-default company bank")
        if default_bank_before != (
            db.get_value("Company", COMPANY, "default_bank_account")
            if frappe.get_meta("Company").has_field("default_bank_account") else None
        ):
            raise AssertionError("E-Commerce marketplace Bank Account setup changed the company default bank")

        fixture_result = {
            "verification": "native_ecommerce_outcome_rollback",
            "fixture": "synthetic_source_sale_return_and_cash_movement",
            "composite_receipt_schema": completed["schema"],
            "composite_status": completed["status"],
            "return_documents": len(completed["documents"]),
            "retry_reused_return_document": True,
            "bank_transaction_docstatus": bank_transaction.docstatus,
            "bank_transaction_status": bank_transaction.status,
            "marketplace_bank_account": bank_transaction.bank_account,
            "external_dispatch_calls_captured": len(captured_dispatches),
            "company_default_bank_unchanged": True,
        }
        if original_contract is not None:
            ecommerce_finance.ensure_ecommerce_finance_contract = original_contract
            original_contract = None
        if original_commit is not None:
            db.commit = original_commit
            original_commit = None
        if original_sql is not None:
            db.sql = original_sql
            original_sql = None
        if original_sql_ddl is not None:
            db.sql_ddl = original_sql_ddl
            original_sql_ddl = None
        db.rollback()
        dispatch_patches.close()
        dispatch_patches = None

        residual = any(
            db.exists(doctype, {"custom_source_system": SOURCE, "custom_source_id": source_id})
            for doctype, source_id in (
                ("Sales Order", f"{ids['store_id']}:shipment:{ids['shipment_id']}"),
                ("Delivery Note", f"{refund_source_id}:return_dn:{delivery.name}"),
                ("Sales Invoice", f"{refund_source_id}:credit_note:{invoice.name}"),
                ("Bank Transaction", bank_record["id"]),
            )
        )
        if residual or db.count("GL Entry", {"company": COMPANY}) != gl_before:
            raise AssertionError("Synthetic E-Commerce rollback left source documents or accounting entries")
        marketplace_bank_after = db.get_value(
            "Bank Account", {"company": COMPANY, "account_name": "E-CommerceBench auto_hardware Marketplace Bank"}, "name"
        )
        if marketplace_bank_after != marketplace_bank_before:
            raise AssertionError("Synthetic E-Commerce rollback left marketplace bank setup behind")
        fixture_result["post_rollback"] = {
            "residual_source_documents": 0,
            "gl_entries_added": 0,
            "stock_ledger_entries_added": 0,
        }
        fixture_result["stock_ledger_entries_added"] = max(
            0, db.count("Stock Ledger Entry", {"company": COMPANY}) - stock_ledger_before
        )
        if fixture_result["stock_ledger_entries_added"] != 0:
            raise AssertionError("Synthetic E-Commerce rollback left stock ledger entries")
        return fixture_result
    finally:
        if original_contract is not None:
            ecommerce_finance.ensure_ecommerce_finance_contract = original_contract
        if original_commit is not None:
            db.commit = original_commit
        if original_sql is not None:
            db.sql = original_sql
        if original_sql_ddl is not None:
            db.sql_ddl = original_sql_ddl
        if db is not None:
            db.rollback()
            frappe.destroy()
        if dispatch_patches is not None:
            dispatch_patches.close()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, choices=(COMPANY_SITE,))
    parser.add_argument("--confirm-native-schema-setup", action="store_true")
    parser.add_argument("--confirm-native-rollback-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_native_schema_setup or not args.confirm_native_rollback_fixture:
        parser.error("pass both confirmation flags to install source fields and run the synthetic rollback proof")
    print(json.dumps(run(args.site), sort_keys=True))


if __name__ == "__main__":
    main()
