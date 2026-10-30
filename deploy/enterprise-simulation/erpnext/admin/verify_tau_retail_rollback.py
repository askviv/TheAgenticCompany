"""Opt-in native ERPNext integration proof for τ retail projection rollback.

Run only inside the one-off site-admin container with
``--confirm-native-rollback-fixture``. The script requires the retail native
contract to be installed, blocks every database commit during the synthetic
projection, verifies native reads, rolls the transaction back, then checks
that no fixture rows or shared catalog changes remain.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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


def _read_manifest(fixture_id: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, str]]:
    prefix = f"rollback-fixture-{fixture_id}-"
    user_id = prefix + "user"
    product_id = prefix + "product"
    item_id = prefix + "item"
    method_id = prefix + "paypal"
    cancelled_id = f"#ROLLBACK-{fixture_id}-CANCELLED"
    delivered_id = f"#ROLLBACK-{fixture_id}-DELIVERED"
    address = {
        "address1": "1 Contract Road", "address2": "", "city": "London",
        "country": "United Kingdom", "state": "", "zip": "EC1A 1AA",
    }
    cancelled = {
        "order_id": cancelled_id, "user_id": user_id, "status": "cancelled",
        "cancel_reason": "ordered by mistake",
        "payment_history": [
            {"transaction_type": "payment", "amount": 25.0, "payment_method_id": method_id},
            {"transaction_type": "refund", "amount": 25.0, "payment_method_id": method_id},
        ],
        "fulfillments": [], "address": dict(address),
        "items": [{"item_id": item_id, "product_id": product_id,
                    "name": "Synthetic Contract Product", "price": 25.0,
                    "options": {"size": "M"}}],
    }
    delivered = {
        "order_id": delivered_id, "user_id": user_id, "status": "delivered",
        "payment_history": [
            {"transaction_type": "payment", "amount": 25.0, "payment_method_id": method_id},
        ],
        "fulfillments": [{"tracking_id": [f"ROLLBACK-TRACK-{fixture_id}"], "item_ids": [item_id]}],
        "address": dict(address),
        "items": [{"item_id": item_id, "product_id": product_id,
                    "name": "Synthetic Contract Product", "price": 25.0,
                    "options": {"size": "M"}}],
    }
    user = {
        "user_id": user_id, "name": {"first_name": "Rollback", "last_name": "Fixture"},
        "email": "rollback-fixture@example.invalid", "address": dict(address),
        "payment_methods": {method_id: {"id": method_id, "source": "paypal"}},
        "orders": [cancelled_id, delivered_id],
    }
    product = {
        "product_id": product_id, "name": "Synthetic Contract Product",
        "variants": {item_id: {"item_id": item_id, "options": {"size": "M"},
                                "available": True, "price": 25.0}},
    }
    records = {
        "users": {user_id: user}, "products": {product_id: product},
        "items": {item_id: {"item_id": item_id}},
        "orders": {cancelled_id: cancelled, delivered_id: delivered},
    }
    singulars = {"users": "user", "products": "product", "items": "item", "orders": "order"}
    mapping = {
        entity: {source_id: f"tau2:retail:{singulars[entity]}:{source_id}" for source_id in rows}
        for entity, rows in records.items()
    }
    task_id = f"native-contract-rollback-{fixture_id}"
    manifest = {
        "schema_version": 1,
        "source_integrity": {"status": "valid", "issues": []},
        "upstream": {
            "project": "sierra-research/tau2-bench",
            "commit": "7aad81c37a3a78b4ed3e4866769f851400c64415",
            "domain": "retail",
        },
        "task_id": task_id,
        "task": {"id": task_id, "initial_state": None, "evaluation_criteria": {}},
        "source_database": {
            "path": "synthetic-native-rollback-fixture",
            "sha256": hashlib.sha256(fixture_id.encode("ascii")).hexdigest(),
        },
        "original_db": records,
        "id_mapping": mapping,
    }
    return manifest, records, {"user": user_id, "product": product_id, "item": item_id,
                               "payment_method": method_id, "cancelled_order": cancelled_id,
                               "delivered_order": delivered_id}


def _require_native_contract(frappe: Any) -> None:
    if frappe.db.get_value("Company", COMPANY, "default_bank_account") != "Retail Payment Clearing - TAC":
        raise RuntimeError("Native retail Bank account setup is incomplete")
    for name, currency, account_type in (
        ("Retail Payment Clearing - TAC", "GBP", "Bank"),
        ("Retail USD Receivables - TAC", "USD", "Receivable"),
    ):
        account = frappe.get_doc("Account", name)
        if (account.company != COMPANY or account.account_currency != currency
                or account.account_type != account_type or account.is_group):
            raise RuntimeError(f"Native retail accounting account {name} is not configured")
    custom_field = frappe.db.get_value(
        "Custom Field", {"dt": "Sales Order", "fieldname": "custom_customer_service_state"},
        ["read_only", "allow_on_submit"], as_dict=True,
    )
    if not custom_field or not custom_field.read_only or not custom_field.allow_on_submit:
        raise RuntimeError("Native customer-service workflow state is not configured for transitions")
    workflow = frappe.get_doc("Workflow", "Customer Service Order Lifecycle")
    if not workflow.is_active or workflow.workflow_state_field != "custom_customer_service_state":
        raise RuntimeError("Native customer-service workflow is inactive or uses another field")


def run(site: str) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    (SITES_ROOT / site / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    import frappe
    import owned_site
    from tau_retail import _projection_key, observe_retail_task, seed_retail_task

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        _require_native_contract(frappe)
        fixture_id = uuid.uuid4().hex
        experiment_id = str(uuid.uuid4())
        manifest, records, ids = _read_manifest(fixture_id)
        auxiliary_before = {
            ("Price List", "τ Retail USD"): bool(db.exists("Price List", "τ Retail USD")),
            ("Item Group", "τ Retail"): bool(db.exists("Item Group", "τ Retail")),
        }
        attribute_name = "Retail Size"
        attribute_before = None
        if db.exists("Item Attribute", attribute_name):
            attribute_before = [
                (row.attribute_value, row.abbr)
                for row in frappe.get_doc("Item Attribute", attribute_name).item_attribute_values
            ]
        bank_entries_before = db.count("GL Entry", {"account": "Retail Payment Clearing - TAC"})
        receivable_entries_before = db.count("GL Entry", {"account": "Retail USD Receivables - TAC"})
        original_commit = db.commit

        def reject_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Synthetic retail rollback verification attempted a database commit")

        db.commit = reject_commit
        try:
            with contextlib.redirect_stdout(sys.stderr):
                receipt = seed_retail_task(manifest, experiment_id, "2026-10-30")
                observation = observe_retail_task(manifest, experiment_id)
            cancelled = observation["observation"]["orders"][ids["cancelled_order"]]
            delivered = observation["observation"]["orders"][ids["delivered_order"]]
            expected_counts = {"delivery_notes": 1, "sales_invoices": 2,
                               "credit_notes": 1, "payment_entries": 3}
            if any(receipt["created"].get(key) != value for key, value in expected_counts.items()):
                raise AssertionError("Native synthetic lifecycle document counts did not match")
            if (cancelled["status"] != "cancelled"
                    or cancelled["payment_history"] != records["orders"][ids["cancelled_order"]]["payment_history"]):
                raise AssertionError("Native cancellation and refund observation did not match the fixture")
            if (delivered["status"] != "delivered"
                    or delivered["fulfillments"] != records["orders"][ids["delivered_order"]]["fulfillments"]
                    or delivered["payment_history"] != records["orders"][ids["delivered_order"]]["payment_history"]):
                raise AssertionError("Native delivery and payment observation did not match the fixture")
            if observation["unmapped"] or receipt["projection_gaps"] or receipt["source_gaps"]:
                raise AssertionError("Native synthetic lifecycle emitted unmapped or missing source records")
        finally:
            db.rollback()
            db.commit = original_commit

        source_keys = [
            ("Customer", "user", ids["user"]),
            ("Item", "product", ids["product"]), ("Item", "item", ids["item"]),
            ("Item Price", "item-price", ids["item"]),
            ("Address", "user-address", ids["user"]),
            ("Address", "order-address", ids["cancelled_order"]),
            ("Address", "order-address", ids["delivered_order"]),
            ("Mode of Payment", "payment-method", ids["payment_method"]),
            ("Sales Order", "order", ids["cancelled_order"]),
            ("Sales Order", "order", ids["delivered_order"]),
            ("Delivery Note", "fulfillment", f"{ids['delivered_order']}:fulfillment:0"),
            ("Sales Invoice", "sales-invoice", ids["cancelled_order"]),
            ("Sales Invoice", "sales-invoice", ids["delivered_order"]),
            ("Sales Invoice", "credit-note", f"{ids['cancelled_order']}:credit-note:1"),
            ("Payment Entry", "payment", f"{ids['cancelled_order']}:payment:0"),
            ("Payment Entry", "payment", f"{ids['cancelled_order']}:payment:1"),
            ("Payment Entry", "payment", f"{ids['delivered_order']}:payment:0"),
        ]
        residual = [
            {"doctype": doctype, "record_type": record_type, "count": db.count(
                doctype, {"custom_source_key": _projection_key(experiment_id, manifest["task_id"],
                                                                record_type, source_id)},
            )}
            for doctype, record_type, source_id in source_keys
            if db.count(doctype, {"custom_source_key": _projection_key(
                experiment_id, manifest["task_id"], record_type, source_id,
            )})
        ]
        auxiliary_after = {
            ("Price List", "τ Retail USD"): bool(db.exists("Price List", "τ Retail USD")),
            ("Item Group", "τ Retail"): bool(db.exists("Item Group", "τ Retail")),
        }
        attribute_after = None
        if db.exists("Item Attribute", attribute_name):
            attribute_after = [
                (row.attribute_value, row.abbr)
                for row in frappe.get_doc("Item Attribute", attribute_name).item_attribute_values
            ]
        auxiliary_changes = [
            {"doctype": doctype, "name": name}
            for (doctype, name), existed in auxiliary_before.items()
            if bool(db.exists(doctype, name)) != existed
        ]
        if (attribute_after != attribute_before or auxiliary_changes or residual
                or db.count("GL Entry", {"account": "Retail Payment Clearing - TAC"}) != bank_entries_before
                or db.count("GL Entry", {"account": "Retail USD Receivables - TAC"}) != receivable_entries_before):
            raise AssertionError("Synthetic rollback left native ERPNext rows or shared setup changes")
        return {
            "verification": "native_erpnext_retail_rollback",
            "fixture": "synthetic_cancelled_and_delivered_orders",
            "site": site,
            "company": COMPANY,
            "commit_blocked": True,
            "created_inside_rollback": receipt["created"],
            "observer": {
                "cancelled_status": cancelled["status"],
                "delivered_status": delivered["status"],
                "delivered_fulfillments": len(delivered["fulfillments"]),
                "payment_events": len(cancelled["payment_history"]) + len(delivered["payment_history"]),
                "unmapped": len(observation["unmapped"]),
            },
            "post_rollback": {
                "source_key_checks": len(source_keys), "residual_source_records": len(residual),
                "shared_catalog_changes": len(auxiliary_changes) + int(attribute_after != attribute_before),
                "bank_gl_entries_added": 0, "receivable_gl_entries_added": 0,
            },
            "usd_to_gbp_rate": receipt["usd_to_gbp_rate"],
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
    parser.add_argument("--confirm-native-rollback-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_native_rollback_fixture:
        parser.error("pass --confirm-native-rollback-fixture to run the synthetic rollback proof")
    print(json.dumps(run(args.site), sort_keys=True))


if __name__ == "__main__":
    main()
