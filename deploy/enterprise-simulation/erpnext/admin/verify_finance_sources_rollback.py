"""Opt-in native ERPNext proof for source-backed draft orders and invoices.

Run only in the one-off site-admin container with both confirmation flags.
The helper installs any required custom-field schema before the fixture, then
blocks all commits while synthetic finance facts are imported and observed.
The fixture contains no upstream task, customer data, or persistent posting.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
COMPANY = "The Agent Company"
STARTED_AT = "2026-10-20T00:00:00+00:00"


def _facts() -> tuple[list[dict[str, Any]], dict[str, str]]:
    fixture_id = uuid.uuid4().hex
    supplier = f"Rollback Supplier {fixture_id}"
    buyer = f"Rollback Buyer {fixture_id}"
    so_id = f"rollback-so-{fixture_id}"
    offer_id = f"rollback-offer-{fixture_id}"
    po_id = f"rollback-po-{fixture_id}"
    si_id = f"rollback-si-{fixture_id}"
    pi_id = f"rollback-pi-{fixture_id}"
    buyer_email = f"coffee-fixture-buyer-{fixture_id}@example.test"
    supplier_email = f"coffee-fixture-supplier-{fixture_id}@example.test"
    item = {
        "item_id": "green_coffee_kg", "quantity": 2,
        "unit_price": "10.00", "total_price": "20.00",
    }
    return [
        {
            "id": f"event-offer-{fixture_id}", "source": "sakanaai-coffeebench-native-economy",
            "kind": "offer", "currency": "USD", "amount": "28.50",
            "occurred_at": "2026-10-29T11:00:00+00:00", "source_ref": offer_id,
            "native_record_type": "offer", "native_record_id": offer_id,
            "native_record": {
                "offer_id": offer_id, "listing_id": f"rollback-listing-{fixture_id}",
                "buyer_id": buyer, "seller_id": "roaster_A", "item_id": "green_coffee_kg",
                "quantity": 3, "unit_price": "9.50", "total_price": "28.50",
                "payment_terms_days": 30, "offer_status": "pending",
                "upstream_source": "coffeebench/marketplace.py:Offer",
            },
        },
        {
            "id": f"event-so-{fixture_id}", "source": "sakanaai-coffeebench-native-economy",
            "kind": "order", "currency": "USD", "occurred_at": "2026-10-29T12:00:00+00:00",
            "native_record_type": "deal", "native_record_id": so_id,
            "native_record": {"deal_id": so_id, "buyer_id": buyer, "seller_id": "roaster_A",
                              "delivery_at_virtual_minute": 12 * 1440, **item},
        },
        {
            "id": f"event-po-{fixture_id}", "source": "sakanaai-coffeebench-native-economy",
            "kind": "order", "currency": "USD", "occurred_at": "2026-10-29T12:01:00+00:00",
            "native_record_type": "deal", "native_record_id": po_id,
            "native_record": {"deal_id": po_id, "buyer_id": "roaster_A", "seller_id": supplier,
                              "delivery_at_virtual_minute": 12 * 1440, **item},
        },
        {
            "id": f"event-si-{fixture_id}", "source": "sakanaai-coffeebench-native-economy",
            "kind": "invoice", "currency": "USD", "occurred_at": "2026-10-30T12:00:00+00:00",
            "native_record_type": "invoice", "native_record_id": si_id,
            "native_record": {
                "invoice_id": si_id, "issuer_id": "roaster_A", "payer_id": buyer,
                "amount": "20.00", "issue_day": 10, "due_day": 12, "reference": so_id,
            },
        },
        {
            "id": f"event-pi-{fixture_id}", "source": "sakanaai-coffeebench-native-economy",
            "kind": "invoice", "currency": "USD", "occurred_at": "2026-10-30T12:01:00+00:00",
            "native_record_type": "invoice", "native_record_id": pi_id,
            "native_record": {
                "invoice_id": pi_id, "issuer_id": supplier, "payer_id": "roaster_A",
                "amount": "20.00", "issue_day": 10, "due_day": 12, "reference": po_id,
            },
        },
    ], {"supplier": supplier, "buyer": buyer, "sales_order": so_id, "offer": offer_id,
        "purchase_order": po_id, "sales_invoice": si_id, "purchase_invoice": pi_id,
        "item": "green_coffee_kg", "buyer_email": buyer_email, "supplier_email": supplier_email}


def run(site: str) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    (SITES_ROOT / site / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    import frappe
    import owned_site
    from finance_sources import ensure_finance_source_contract, reconcile

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        if not db.exists("Company", COMPANY):
            raise RuntimeError("Configured native finance company is unavailable")
        if db.get_value("Company", COMPANY, "default_currency") != "GBP":
            raise RuntimeError("Synthetic finance proof expects the existing GBP company profile")

        # Frappe's custom-field installer performs schema DDL. Establish that
        # native schema as setup before protecting the business-data transaction.
        custom_fields_before = db.count("Custom Field", {"dt": ["in", [
            "Item", "Customer", "Supplier", "Sales Order", "Sales Order Item", "Purchase Order",
            "Sales Invoice", "Purchase Invoice", "Issue",
        ]]})
        with contextlib.redirect_stdout(sys.stderr):
            ensure_finance_source_contract()
        db.commit()
        custom_fields_after = db.count("Custom Field", {"dt": ["in", [
            "Item", "Customer", "Supplier", "Sales Order", "Sales Order Item", "Purchase Order",
            "Sales Invoice", "Purchase Invoice", "Issue",
        ]]})

        records, ids = _facts()
        binding = {
            "company_id": "company", "erp_company": COMPANY,
            "company_business_ids": ["roaster_A"], "party_map": {}, "item_map": {},
            "started_at": STARTED_AT,
            "counterparty_mail_directory": {
                ids["buyer"]: {
                    "actor_id": "coffee_fixture_buyer", "email": ids["buyer_email"],
                    "display_name": "Synthetic Coffee Buyer", "kind": "counterparty",
                },
                ids["supplier"]: {
                    "actor_id": "coffee_fixture_supplier", "email": ids["supplier_email"],
                    "display_name": "Synthetic Coffee Supplier", "kind": "counterparty",
                },
            },
        }
        usd_accounts_before = {
            row.name for row in frappe.get_all(
                "Account", filters={"company": COMPANY, "account_currency": "USD",
                                    "account_type": ["in", ["Receivable", "Payable"]], "is_group": 0},
                fields=["name"],
            )
        }
        gl_before = db.count("GL Entry", {"company": COMPANY})
        original_commit = db.commit

        def reject_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Synthetic finance source proof attempted a database commit")

        db.commit = reject_commit
        try:
            with contextlib.redirect_stdout(sys.stderr):
                receipt = reconcile(records, binding, "2026-10-30")
                replay = reconcile(records, binding, "2026-10-30")
            receipt_identity = sorted(
                (row["source_id"], row["doctype"], row["name"])
                for row in receipt["receipts"]
            )
            replay_identity = sorted(
                (row["source_id"], row["doctype"], row["name"])
                for row in replay["receipts"]
            )
            if receipt_identity != replay_identity:
                raise AssertionError("Repeated source reconciliation created duplicate native documents")
            expected_types = {"Sales Order": 2, "Purchase Order": 1, "Sales Invoice": 1, "Purchase Invoice": 1}
            actual_types = {kind: sum(row["doctype"] == kind for row in receipt["receipts"])
                            for kind in expected_types}
            if actual_types != expected_types:
                raise AssertionError("Native finance fixture did not create one draft of each expected DocType")
            order_rows = [row for row in receipt["receipts"] if row["doctype"] == "Sales Order"]
            deal_row = next(row for row in order_rows if row["source_id"] == ids["sales_order"])
            offer_row = next(row for row in order_rows if row["source_id"] == ids["offer"])
            docs = {row["doctype"]: frappe.get_doc(row["doctype"], row["name"])
                    for row in receipt["receipts"] if row["doctype"] != "Sales Order"}
            docs["Sales Order"] = frappe.get_doc("Sales Order", deal_row["name"])
            docs["offer"] = frappe.get_doc("Sales Order", offer_row["name"])
            if any(doc.docstatus != 0 for doc in docs.values()):
                raise AssertionError("Source-backed finance documents must remain native drafts")
            if any(doc.currency != "USD" for doc in docs.values()):
                raise AssertionError("Finance source documents did not retain USD transaction currency")
            expected_delivery = (datetime.fromisoformat(STARTED_AT) + timedelta(minutes=12 * 1440)).date().isoformat()
            order_dates = {
                "Sales Order": str(docs["Sales Order"].delivery_date),
                "Sales Order Item": str(docs["Sales Order"].items[0].delivery_date),
                "Purchase Order Item": str(docs["Purchase Order"].items[0].schedule_date),
            }
            if any(value != expected_delivery for value in order_dates.values()):
                raise AssertionError(
                    f"Native order schedules did not preserve the source delivery minute: {order_dates}"
                )
            if any(value == str(docs["Sales Order"].transaction_date) for value in order_dates.values()):
                raise AssertionError("Native order schedule incorrectly equals its order date")
            offer_doc = docs["offer"]
            if (
                offer_doc.custom_native_offer_id != ids["offer"]
                or offer_doc.custom_native_payment_terms_days != 30
                or len(offer_doc.items) != 1
                or offer_doc.items[0].custom_native_coffee_item_id != "green_coffee_kg"
                or offer_doc.items[0].qty != 3
                or offer_doc.items[0].rate != 9.5
            ):
                raise AssertionError("Pending offer Sales Order did not preserve exact native terms")
            for email, party_doctype, party_name in (
                (ids["buyer_email"], "Customer", docs["Sales Invoice"].customer),
                (ids["supplier_email"], "Supplier", docs["Purchase Invoice"].supplier),
            ):
                contacts = frappe.get_all(
                    "Contact Email", filters={"email_id": email}, fields=["parent"]
                )
                if len(contacts) != 1:
                    raise AssertionError("Native counterparty email was not linked to one Contact")
                contact = frappe.get_doc("Contact", contacts[0]["parent"])
                if (party_doctype, party_name) not in {
                    (link.link_doctype, link.link_name) for link in contact.links
                }:
                    raise AssertionError("Native counterparty Contact is not linked to its business party")
            expected_due = (datetime.fromisoformat(STARTED_AT).date() + timedelta(days=12)).isoformat()
            observed_due = {kind: str(docs[kind].due_date) for kind in ("Sales Invoice", "Purchase Invoice")}
            if any(value != expected_due for value in observed_due.values()):
                raise AssertionError(
                    f"Invoice due dates did not preserve the absolute source due-day: "
                    f"expected {expected_due}, observed {observed_due}"
                )
            customer = frappe.get_doc("Customer", docs["Sales Invoice"].customer)
            customer_account = next(row.account for row in customer.accounts if row.company == COMPANY)
            if docs["Sales Invoice"].debit_to != customer_account:
                raise AssertionError("Sales Invoice does not use its Customer's native receivable mapping")
            receivable = frappe.get_doc("Account", customer_account)
            if receivable.account_currency != "USD" or receivable.account_type != "Receivable":
                raise AssertionError("Sales Invoice Customer is not mapped to a native USD receivable")
            payable = docs["Purchase Invoice"].credit_to
            payable_account = frappe.get_doc("Account", payable)
            if payable_account.account_currency != "USD" or payable_account.account_type != "Payable":
                raise AssertionError("Purchase Invoice does not use a native USD payable ledger")
            supplier = frappe.get_doc("Supplier", docs["Purchase Invoice"].supplier)
            supplier_account = next(row.account for row in supplier.accounts if row.company == COMPANY)
            if payable != supplier_account:
                raise AssertionError("Purchase Invoice does not use its Supplier's native payable mapping")
            if not receipt["receipts"] or len(receipt["receipts"]) != 5:
                raise AssertionError("Source receipts do not cover each expected native finance document")
        finally:
            db.rollback()
            db.commit = original_commit

        source_id_sets = (
            ("Sales Order", ids["sales_order"]), ("Sales Order", ids["offer"]), ("Purchase Order", ids["purchase_order"]),
            ("Sales Invoice", ids["sales_invoice"]), ("Purchase Invoice", ids["purchase_invoice"]),
            ("Customer", ids["buyer"]), ("Supplier", ids["supplier"]), ("Item", ids["item"]),
        )
        residual = [
            {"doctype": doctype, "count": int(bool(db.exists(doctype, {
                "custom_source_system": "coffeebench", "custom_source_id": source_id,
            })))}
            for doctype, source_id in source_id_sets
            if db.exists(doctype, {"custom_source_system": "coffeebench", "custom_source_id": source_id})
        ]
        usd_accounts_after = {
            row.name for row in frappe.get_all(
                "Account", filters={"company": COMPANY, "account_currency": "USD",
                                    "account_type": ["in", ["Receivable", "Payable"]], "is_group": 0},
                fields=["name"],
            )
        }
        leftover_contact_emails = frappe.get_all(
            "Contact Email",
            filters={"email_id": ["in", [ids["buyer_email"], ids["supplier_email"]]]},
            fields=["parent"],
        )
        if (residual or leftover_contact_emails or usd_accounts_after != usd_accounts_before
                or db.count("GL Entry", {"company": COMPANY}) != gl_before):
            raise AssertionError("Synthetic finance rollback left source rows, accounts, or ledger entries")

        return {
            "verification": "native_erpnext_finance_source_rollback",
            "fixture": "synthetic_pending_offer_and_sales_and_purchase_orders_and_invoices",
            "company": COMPANY,
            "commit_blocked_for_fixture": True,
            "draft_documents_observed": actual_types,
            "transaction_currency": "USD",
            "invoice_due_date": expected_due,
            "order_posting_date": str(docs["Sales Order"].transaction_date),
            "order_delivery_date": expected_delivery,
            "foreign_currency_accounts_created_inside_rollback": len(set(
                [docs["Sales Invoice"].debit_to, docs["Purchase Invoice"].credit_to]
            ) - usd_accounts_before),
            "schema_custom_fields_added_during_setup": max(0, custom_fields_after - custom_fields_before),
            "post_rollback": {"checked_source_rows": len(source_id_sets), "residual_source_rows": len(residual),
                              "residual_accounts": len(usd_accounts_after - usd_accounts_before), "gl_entries_added": 0},
            "exchange_rate_used": docs["Sales Order"].conversion_rate,
        }
    finally:
        if db is not None:
            db.rollback()
        frappe.destroy()
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
