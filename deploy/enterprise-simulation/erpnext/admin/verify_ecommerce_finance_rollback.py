"""Opt-in native proof of source-backed E-Commerce sales-order import.

The fixture uses synthetic pending-sale facts and blocks database commits during
import, verification, and cleanup. It never advances a benchmark/provider.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
COMPANY = "The Agent Company"
SOURCE = "qwenlm-ecommercebench-native-economy"
COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"


def _fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixture = uuid.uuid4().hex
    store_id = f"rollback-store-{fixture}"
    product_id = f"rollback-product-{fixture}"
    shipment_id = int(fixture[:7], 16) + 1
    source_id = f"{store_id}:shipment:{shipment_id}"
    facts = {
        "shipment_id": shipment_id,
        "store_id": store_id,
        "store_type": "auto_hardware",
        "product_id": product_id,
        "product": {
            "product_id": product_id,
            "title": f"Rollback fixture item {fixture}",
            "category": "Auto Parts",
            "store_type": "auto_hardware",
            "brand": "Synthetic Fixture",
            "size": "Small",
            "reference_price": "12.50",
        },
        "quantity": 3,
        "unit_price": "12.50",
        "revenue_gross": "37.50",
        "revenue_net": "36.75",
        "commission": "0.75",
        "sale_date": "2026-10-29",
        "ship_deadline": "2026-10-31",
    }
    record = {
        "id": source_id,
        "source": SOURCE,
        "kind": "order",
        "source_ref": source_id,
        "occurred_at": "2026-10-30T12:00:00+00:00",
        "observed_at": "2026-10-30T12:00:00+00:00",
        "upstream_commit": COMMIT,
        "party": "ecommercebench-marketplace:auto_hardware",
        "currency": "GBP",
        "amount": "37.50",
        "native_units_per_currency_unit": "1",
        "native_record": facts,
    }
    binding = {
        "company_id": "company",
        "erp_company": COMPANY,
        "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
        "sales_order_date_semantics": "source_dispatch_deadline",
    }
    return [record], {
        "source_id": source_id,
        "store_id": store_id,
        "product_id": product_id,
        "shipment_id": shipment_id,
        "binding": binding,
    }


def run(site: str) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    import frappe
    import owned_site
    from ecommerce_finance import ensure_ecommerce_finance_contract, reconcile

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        if not db.exists("Company", COMPANY):
            raise RuntimeError(
                "Configured native E-Commerce finance company is unavailable"
            )
        if db.get_value("Company", COMPANY, "default_currency") != "GBP":
            raise RuntimeError(
                "Synthetic E-Commerce finance proof expects the existing GBP company profile"
            )

        custom_fields_before = db.count(
            "Custom Field",
            {"dt": ["in", ["Item", "Customer", "Sales Order", "Delivery Note"]]},
        )
        with contextlib.redirect_stdout(sys.stderr):
            ensure_ecommerce_finance_contract()
        db.commit()
        custom_fields_after = db.count(
            "Custom Field",
            {"dt": ["in", ["Item", "Customer", "Sales Order", "Delivery Note"]]},
        )

        records, ids = _fixture()
        marketplace_before = db.get_value(
            "Customer",
            {
                "custom_source_system": SOURCE,
                "custom_source_id": "marketplace:auto_hardware",
            },
            "name",
        )
        gl_before = db.count("GL Entry", {"company": COMPANY})
        original_commit = db.commit

        def reject_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "Synthetic E-Commerce source proof attempted a database commit"
            )

        db.commit = reject_commit
        try:
            with contextlib.redirect_stdout(sys.stderr):
                first = reconcile(records, ids["binding"], "2026-10-30")
                replay_records = [
                    {
                        **records[0],
                        "occurred_at": "2026-10-31T09:00:00+00:00",
                        "observed_at": "2026-10-31T09:00:00+00:00",
                    }
                ]
                replay = reconcile(replay_records, ids["binding"], "2026-10-31")
            if (
                first["unmapped"]
                or replay["unmapped"]
                or len(first["receipts"]) != 1
                or len(replay["receipts"]) != 1
            ):
                raise AssertionError(
                    "Native E-Commerce source fixture did not produce one mapped Sales Order"
                )
            receipt, replay_receipt = first["receipts"][0], replay["receipts"][0]
            if receipt["created"] is not True or replay_receipt["created"] is not False:
                raise AssertionError(
                    "Native E-Commerce Sales Order import is not idempotent"
                )
            if (
                receipt["name"] != replay_receipt["name"]
                or receipt["doctype"] != "Sales Order"
            ):
                raise AssertionError(
                    "E-Commerce source receipt identity changed on replay"
                )
            order = frappe.get_doc("Sales Order", receipt["name"])
            if (
                order.docstatus != 0
                or order.company != COMPANY
                or order.currency != "GBP"
                or str(order.transaction_date) != "2026-10-29"
                or len(order.items) != 1
            ):
                raise AssertionError(
                    "Native source Sales Order did not preserve company, draft, currency, or date"
                )
            item = order.items[0]
            if (
                item.qty != 3
                or item.rate != 12.5
                or item.item_code != f"ECOM-{ids['product_id']}"
                or order.custom_ecommerce_shipment_id != str(ids["shipment_id"])
                or order.custom_ecommerce_store_id != ids["store_id"]
                or str(order.custom_ecommerce_ship_deadline) != "2026-10-31"
                or str(order.delivery_date) != "2026-10-31"
                or str(item.delivery_date) != "2026-10-31"
                or order.custom_ecommerce_date_semantics
                != "Dispatch deadline, not arrival promise"
                or "Dispatch deadline, not arrival promise" not in (order.terms or "")
            ):
                raise AssertionError(
                    "Native source Sales Order lost exact shipment/product/quantity/price/deadline facts"
                )
            snapshot = json.loads(order.custom_source_snapshot)
            context = snapshot["feedback_context"]
            if snapshot["date_semantics"] != {
                "policy": "source_dispatch_deadline",
                "source_field": "native_record.ship_deadline",
                "erp_fields": [
                    "Sales Order.delivery_date",
                    "Sales Order Item.delivery_date",
                ],
                "meaning": "Dispatch deadline, not arrival promise",
            }:
                raise AssertionError(
                    "Native Sales Order snapshot did not retain the approved date semantics"
                )
            if (
                context["native_ids"]
                != {
                    "shipment_id": ids["shipment_id"],
                    "store_id": order.custom_ecommerce_store_id,
                    "product_id": ids["product_id"],
                }
                or context["source_item_code"] != item.item_code
                or context["source_quantity"] != 3
            ):
                raise AssertionError(
                    "Native source Sales Order did not retain exact feedback mapping context"
                )
            customer = frappe.get_doc("Customer", order.customer)
            if (
                customer.custom_source_system != SOURCE
                or "Marketplace" not in customer.customer_name
            ):
                raise AssertionError(
                    "Native source order counterparty is not the source Marketplace Customer"
                )
            speed_field = frappe.get_meta("Delivery Note").get_field(
                "custom_source_shipping_speed"
            )
            if (
                speed_field.fieldtype != "Select"
                or speed_field.options != "\nfast\nstandard\nslow"
                or speed_field.hidden
                or speed_field.read_only
                or speed_field.reqd
            ):
                raise AssertionError(
                    "Native Delivery Note shipping speed is not a user-editable, unset Select"
                )
            if db.count("GL Entry", {"company": COMPANY}) != gl_before:
                raise AssertionError(
                    "Draft E-Commerce source import posted accounting ledger entries"
                )
        finally:
            db.rollback()
            db.commit = original_commit

        residual = any(
            db.exists(
                doctype, {"custom_source_system": SOURCE, "custom_source_id": source_id}
            )
            for doctype, source_id in (
                ("Sales Order", ids["source_id"]),
                ("Item", ids["product_id"]),
            )
        )
        marketplace_after = db.get_value(
            "Customer",
            {
                "custom_source_system": SOURCE,
                "custom_source_id": "marketplace:auto_hardware",
            },
            "name",
        )
        if marketplace_after != marketplace_before:
            residual = True
        if residual or db.count("GL Entry", {"company": COMPANY}) != gl_before:
            raise AssertionError(
                "Synthetic E-Commerce source rollback left source rows or ledger entries"
            )
        return {
            "verification": "native_ecommerce_finance_rollback",
            "fixture": "synthetic_pending_sale",
            "company": COMPANY,
            "draft_sales_order": receipt["name"],
            "transaction_currency": "GBP",
            "source_transaction_date": "2026-10-29",
            "source_shipping_deadline": "2026-10-31",
            "replay_created": replay_receipt["created"],
            "sales_order_delivery_date_semantics": "dispatch_deadline_not_arrival_promise",
            "schema_custom_fields_added_during_setup": max(
                0, custom_fields_after - custom_fields_before
            ),
            "post_rollback": {"residual_source_rows": 0, "gl_entries_added": 0},
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
        parser.error(
            "pass both confirmation flags to install E-Commerce source fields and run the synthetic rollback proof"
        )
    print(json.dumps(run(args.site), sort_keys=True))


if __name__ == "__main__":
    main()
