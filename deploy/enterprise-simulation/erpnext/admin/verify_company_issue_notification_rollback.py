"""Opt-in native proof that source-mapped Coffee orders create one Issue and queue.

Uses a real pinned E-CommerceBench Cold Wave condition and synthetic native Coffee
Purchase Orders. The Issue, queue, shares, and fixture orders are created in a
commit-blocked transaction and verified absent after rollback. No email is sent.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
RUNTIME_ROOT = Path("/opt/erpnext-runtime")
PROFILE_PATH = Path("/opt/company-identities.json")
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
COMPANY = "The Agent Company"
ECOMMERCE_PROVIDER_ID = "qwenlm-ecommercebench-calendar"
ECOMMERCE_UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
COFFEE_PROVIDER_ID = "sakanaai-coffeebench-native-economy"
COFFEE_UPSTREAM_COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
COLD_WAVE_START = "2026-12-20T00:00:00+00:00"
COLD_WAVE_DURATION_DAYS = 5
COLD_WAVE_MULTIPLIER = "2.0"


def _source_condition(supplier_id: str, seed: int, supplier_actor_id: str,
                      store_type: str) -> tuple[dict[str, Any], dict[str, Any]]:
    start = datetime.fromisoformat(COLD_WAVE_START)
    event_date = start.date().isoformat()
    source_key = f"{ECOMMERCE_PROVIDER_ID}:{seed}:{supplier_id}:supply:{event_date}:Cold Wave"
    event_id = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]
    condition = {
        "event_id": event_id,
        "source": ECOMMERCE_PROVIDER_ID,
        "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "occurred_at": COLD_WAVE_START,
        "effective_until": (start + timedelta(days=COLD_WAVE_DURATION_DAYS)).isoformat(),
        "duration_days": COLD_WAVE_DURATION_DAYS,
        "event_name": "Cold Wave",
        "store_type": store_type,
        "affected_categories": "all",
        "delivery_delay_multiplier": COLD_WAVE_MULTIPLIER,
    }
    event = {
        "schema_version": 1,
        "company_id": "company",
        "event_id": event_id,
        "event_type": "SUPPLIER_SUPPLY_DISRUPTED",
        "simulated_time": COLD_WAVE_START,
        "source": ECOMMERCE_PROVIDER_ID,
        "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
        "entity_type": "supplier",
        "entity_id": supplier_id,
        "payload": {
            "upstream_event_name": "Cold Wave",
            "upstream_calendar_start_date": start.strftime("%m-%d"),
            "upstream_calendar_duration_days": COLD_WAVE_DURATION_DAYS,
            "affected_categories": "all",
            "delivery_delay_multiplier": COLD_WAVE_MULTIPLIER,
            "cost_increase": None,
            "upstream_source": "third_party/ecommercebench/upstream/data/events.csv",
            "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
            "store_type": store_type,
        },
        "visibility": [supplier_actor_id],
    }
    return condition, event


def _new_draft_purchase_order(frappe, source_id: str, supplier_id: str,
                              condition: dict[str, Any] | None) -> Any:
    from source_records import source_fields

    item = frappe.db.get_value("Item", {"disabled": 0}, "name")
    if not item or not frappe.db.exists("Supplier", supplier_id):
        raise RuntimeError("Synthetic source-mapping proof requires the bound supplier and an enabled item")
    item_doc = frappe.get_doc("Item", item)
    accepted_at = "2026-12-20T10:00:00+00:00"
    native_record = {
        "deal_id": source_id,
        "accepted_at_virtual_minute": 600,
        "delivery_at_virtual_minute": 21000,
        "item_id": "roasted_coffee_kg",
        "quantity": 1,
        "unit_price": "1.00",
        "total_price": "1.00",
    }
    coffee_snapshot = {
        "source": COFFEE_PROVIDER_ID,
        "upstream_commit": COFFEE_UPSTREAM_COMMIT,
        "kind": "order",
        "native_record_type": "deal",
        "native_record_id": source_id,
        "occurred_at": accepted_at,
        "active_source_condition_ids": [condition["event_id"]] if condition else [],
        "active_source_conditions": [condition] if condition else [],
        "native_record": native_record,
    }
    order = frappe.new_doc("Purchase Order")
    order.company = COMPANY
    order.supplier = supplier_id
    order.currency = frappe.db.get_value("Company", COMPANY, "default_currency")
    order.transaction_date = date(2026, 12, 20).isoformat()
    order.schedule_date = "2026-12-26"
    order.append("items", {
        "item_code": item,
        "qty": 1,
        "rate": 1,
        "uom": item_doc.stock_uom,
        "conversion_factor": 1,
        "schedule_date": "2026-12-26",
    })
    order.update(source_fields(
        source_system="coffeebench", source_id=source_id,
        record_type="order", task_id="company", experiment_id="background",
        company_id="company", snapshot=coffee_snapshot,
    ))
    order.insert(ignore_permissions=True)
    if order.docstatus != 0:
        raise AssertionError("Synthetic CoffeeBench Purchase Order must remain a draft")
    return order


def _share_perms(frappe, doctype: str, name: str, employee: str) -> dict[str, int] | None:
    value = frappe.db.get_value(
        "DocShare", {"share_doctype": doctype, "share_name": name, "user": employee},
        ["read", "write", "share"], as_dict=True,
    )
    if not value:
        return None
    return {field: int(value[field] or 0) for field in ("read", "write", "share")}


def run(site: str) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    (SITES_ROOT / site / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    import frappe
    import owned_site
    from company_issues import ensure_company_issue_contract, reconcile_disruption_issues
    from company_issue_notifications import ensure_notification_contract, queue_issue_notification
    from integration_contracts import resolve_company_issue_route

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        if not db.exists("Company", COMPANY):
            raise RuntimeError("Configured native company is unavailable")
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        route, employee_profile = resolve_company_issue_route(profile)
        employee = employee_profile["email"]
        user = frappe.db.get_value("User", employee, ["email", "enabled"], as_dict=True)
        if not user or not user.enabled or user.email != employee:
            raise RuntimeError("Configured Issue recipient is not an enabled native ERP User")
        manifest_path = RUNTIME_ROOT / f"{site}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        supplier_id = manifest["supplier_id"]
        if manifest.get("site_name") != site:
            raise RuntimeError("Company site runtime manifest does not match the requested native site")

        # Native schema setup may issue DDL. Keep it outside the rollback-only proof.
        with contextlib.redirect_stdout(sys.stderr):
            ensure_company_issue_contract()
            from finance_sources import ensure_finance_source_contract
            ensure_finance_source_contract()
            ensure_notification_contract()
        db.commit()

        fixture_id = f"issue-map-proof-{uuid.uuid4().hex}"
        background = profile["company"]["background"]
        ecommerce = background["ecommerce"]
        condition, event = _source_condition(
            supplier_id, int(background["seed"]), ecommerce["supplier_actor_id"],
            ecommerce["store_type"],
        )
        matching_source_id = f"{fixture_id}-matching-deal"
        unrelated_source_id = f"{fixture_id}-unrelated-deal"
        late_source_id = f"{fixture_id}-late-deal"
        event["event_id"] = condition["event_id"]
        event["entity_id"] = supplier_id
        before_issue_count = db.count("Issue", {"custom_company_issue_key": ["is", "set"]})
        before_queue_count = db.count("Email Queue", {"reference_doctype": "Issue"})
        before_gl_count = db.count("GL Entry", {"company": COMPANY})
        original_commit = db.commit

        def reject_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Synthetic source-to-Issue proof attempted a database commit")

        db.commit = reject_commit
        issue_name = matching_po_name = unrelated_po_name = late_po_name = queue_name = communication_name = None
        issue_key = None
        comment_names = []
        matching_source_snapshot = None
        matching_commitments = None
        try:
            matching_order = _new_draft_purchase_order(
                frappe, matching_source_id, supplier_id, condition,
            )
            matching_po_name = matching_order.name
            matching_source_snapshot = matching_order.custom_source_snapshot
            matching_commitments = frappe.as_json({
                "schedule_date": matching_order.schedule_date,
                "items": [{"qty": row.qty, "rate": row.rate, "schedule_date": row.schedule_date}
                          for row in matching_order.items],
            })
            unrelated_order = _new_draft_purchase_order(
                frappe, unrelated_source_id, supplier_id, None,
            )
            unrelated_po_name = unrelated_order.name

            company_binding = {"company_id": "company", "erp_company": COMPANY}
            first = reconcile_disruption_issues(
                [event, dict(event)], company_binding,
                responsible_employee=employee, responsible_role=route.responsible_role,
                requested_outcome=route.requested_outcome,
            )
            if len(first) != 1 or first[0]["created"] is not True:
                raise AssertionError("Source mapping did not create exactly one Issue from the supported condition")
            issue_name = first[0]["name"]
            issue_key = first[0]["notification_key"]
            issue = frappe.get_doc("Issue", issue_name)
            initial_description = issue.description

            late_order = _new_draft_purchase_order(frappe, late_source_id, supplier_id, condition)
            late_po_name = late_order.name
            repeated = reconcile_disruption_issues(
                [event], company_binding,
                responsible_employee=employee, responsible_role=route.responsible_role,
                requested_outcome=route.requested_outcome,
            )
            if (len(repeated) != 1 or repeated[0]["created"] is not False
                    or repeated[0]["name"] != issue_name
                    or [row["source_id"] for row in repeated[0]["new_affected_orders"]] != [late_source_id]):
                raise AssertionError("Late source-linked order was not appended idempotently to its existing Issue")
            replayed = reconcile_disruption_issues(
                [event], company_binding,
                responsible_employee=employee, responsible_role=route.responsible_role,
                requested_outcome=route.requested_outcome,
            )
            if len(replayed) != 1 or replayed[0]["new_affected_orders"]:
                raise AssertionError("Repeated source reconciliation duplicated the late order comment")
            issue.reload()
            affected = json.loads(issue.custom_affected_orders_json)
            if [(row["doctype"], row["name"], row["source_id"]) for row in affected] != [
                    ("Purchase Order", matching_po_name, matching_source_id),
                    ("Purchase Order", late_po_name, late_source_id)]:
                raise AssertionError("Issue mapping included an unrelated or missing native order")
            if issue.description != initial_description:
                raise AssertionError("Appending a late order overwrote the employee-authored Issue description")
            comments = frappe.get_all(
                "Comment", filters={"reference_doctype": "Issue", "reference_name": issue_name,
                                    "comment_type": "Info"}, fields=["name"],
            )
            comment_names = [row.name for row in comments]
            if len(comments) != 1 or late_po_name not in frappe.get_doc("Comment", comments[0].name).content:
                raise AssertionError("Late affected order was not added once as a native Issue Info comment")
            if issue.custom_source_event_id != condition["event_id"]:
                raise AssertionError("Mapped Issue lost the exact source-condition event identity")
            if issue.custom_responsible_employee != employee or issue.custom_responsible_role != route.responsible_role:
                raise AssertionError("Mapped Issue lost its authoritative employee route")

            receipt = queue_issue_notification(issue)
            repeated_receipt = queue_issue_notification(issue)
            queue_name = receipt["email_queue"]
            communication_name = frappe.db.get_value("Email Queue", queue_name, "communication")
            if repeated_receipt["email_queue"] != queue_name or db.count(
                    "Email Queue", {"reference_doctype": "Issue", "reference_name": issue.name}) != 1:
                raise AssertionError("Native Notification queueing was not idempotent for the mapped Issue")
            if receipt["recipient_email"] != employee or not receipt["message_id"]:
                raise AssertionError("Native notification receipt did not target the configured employee")
            if [(row["doctype"], row["name"], row["source_id"])
                    for row in receipt["affected_orders"]] != [
                    ("Purchase Order", matching_po_name, matching_source_id),
                    ("Purchase Order", late_po_name, late_source_id)]:
                raise AssertionError("Native Notification receipt did not retain the actual matching PO reference")

            if not frappe.has_permission("Issue", "read", doc=issue, user=employee):
                raise AssertionError("Configured Issue recipient cannot read the mapped native Issue")
            if not frappe.has_permission("Issue", "write", doc=issue, user=employee):
                raise AssertionError("Configured Issue recipient cannot update the mapped native Issue")
            matching_live = frappe.get_doc("Purchase Order", matching_po_name)
            if not frappe.has_permission("Purchase Order", "read", doc=matching_live, user=employee):
                raise AssertionError("Configured Issue recipient cannot read the linked native order")
            late_live = frappe.get_doc("Purchase Order", late_po_name)
            if not frappe.has_permission("Purchase Order", "read", doc=late_live, user=employee):
                raise AssertionError("Configured Issue recipient cannot read the late linked native order")
            issue_share = _share_perms(frappe, "Issue", issue_name, employee)
            po_share = _share_perms(frappe, "Purchase Order", matching_po_name, employee)
            late_po_share = _share_perms(frappe, "Purchase Order", late_po_name, employee)
            if issue_share and (issue_share["read"] != 1 or issue_share["write"] != 1 or issue_share["share"] != 0):
                raise AssertionError("Scoped Issue DocShare grants more or less than requested")
            if po_share and (po_share["read"] != 1 or po_share["write"] != 0 or po_share["share"] != 0):
                raise AssertionError("Scoped Purchase Order DocShare grants more than read access")
            if late_po_share and (late_po_share["read"] != 1 or late_po_share["write"] != 0
                                  or late_po_share["share"] != 0):
                raise AssertionError("Late Purchase Order DocShare grants more than read access")
            if issue_share is None and not frappe.has_permission("Issue", "write", doc=issue, user=employee):
                raise AssertionError("Issue access is neither shared nor granted by the native roles")
            if po_share is None and not frappe.has_permission("Purchase Order", "read", doc=matching_live, user=employee):
                raise AssertionError("Order access is neither shared nor granted by the native roles")
            if late_po_share is None and not frappe.has_permission("Purchase Order", "read", doc=late_live, user=employee):
                raise AssertionError("Late order access is neither shared nor granted by the native roles")

            matching_live.reload()
            after_commitments = frappe.as_json({
                "schedule_date": matching_live.schedule_date,
                "items": [{"qty": row.qty, "rate": row.rate, "schedule_date": row.schedule_date}
                          for row in matching_live.items],
            })
            if matching_live.custom_source_snapshot != matching_source_snapshot or after_commitments != matching_commitments:
                raise AssertionError("Issue reconciliation changed native order source facts or commitments")
            if db.count("GL Entry", {"company": COMPANY, "voucher_type": "Purchase Order",
                                      "voucher_no": matching_po_name}):
                raise AssertionError("Draft source-mapped Purchase Order created a ledger posting")
        finally:
            db.rollback()
            db.commit = original_commit

        residuals = {
            "purchase_orders": sum(bool(db.exists("Purchase Order", name)) for name in
                                    (matching_po_name, unrelated_po_name, late_po_name) if name),
            "issue": bool(db.exists("Issue", issue_name)) if issue_name else False,
            "comments": sum(bool(db.exists("Comment", name)) for name in comment_names),
            "email_queue": bool(db.exists("Email Queue", queue_name)) if queue_name else False,
            "communication": bool(db.exists("Communication", communication_name)) if communication_name else False,
            "doc_shares": sum(db.count("DocShare", {"share_name": name, "user": employee})
                              for name in (matching_po_name, issue_name) if name),
        }
        if any(residuals.values()):
            raise AssertionError("Source-mapped Issue rollback left one or more native fixture rows behind")
        if db.count("Issue", {"custom_company_issue_key": ["is", "set"]}) != before_issue_count:
            raise AssertionError("Rollback changed the number of company Issue records")
        if db.count("Email Queue", {"reference_doctype": "Issue"}) != before_queue_count:
            raise AssertionError("Rollback changed the number of Issue Email Queues")
        if db.count("GL Entry", {"company": COMPANY}) != before_gl_count:
            raise AssertionError("Rollback changed company ledger entries")

        return {
            "verification": "native_company_source_mapped_issue_rollback",
            "fixture": "pinned_cold_wave_with_synthetic_coffee_orders",
            "company": COMPANY,
            "commit_blocked_for_fixture": True,
            "mapping_created_one_issue": True,
            "unrelated_order_excluded": True,
            "late_order_comment_count": len(comment_names),
            "repeated_mapping_reused_issue": True,
            "native_queue_count": 1,
            "native_permission_checks": {"issue_read": True, "issue_write": True,
                                         "affected_order_read": True},
            "smtp_send_called": False,
            "employee_email_in_output": False,
            "post_rollback_residuals": residuals,
            "issue_email_queue_count_delta": 0,
            "gl_entry_count_delta": 0,
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
        parser.error("pass --confirm-native-rollback-fixture to run the source-mapped rollback proof")
    print(json.dumps(run(args.site), sort_keys=True))


if __name__ == "__main__":
    main()
