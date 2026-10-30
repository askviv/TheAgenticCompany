"""Verify native company Webhook rendering/signing inside a rolled-back site transaction."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
VERIFY_ENDPOINT = "http://192.0.2.10:8131/company/evidence/native"


def run(site: str, doctype: str) -> dict[str, Any]:
    if site != COMPANY_SITE:
        raise ValueError(
            "Evidence observer verification is limited to the owned company site"
        )
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    import frappe
    import owned_site
    from company_evidence import (
        DOC_EVENTS,
        DOCTYPES,
        ISSUE_EVENTS,
        _webhook_name,
        verify_evidence_webhook_rollback,
    )

    lock_fd = owned_site.lock_for(site)
    initialized = False
    connected = False
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        initialized = True
        frappe.connect()
        connected = True
        frappe.set_user("Administrator")
        secret = secrets.token_urlsafe(32)
        names = [
            _webhook_name(doctype_name, event)
            for doctype_name in DOCTYPES
            for event in DOC_EVENTS
        ] + [
            _webhook_name("Issue", event) for event in ISSUE_EVENTS
        ]
        prior = {}
        for name in names:
            modified = frappe.db.get_value("Webhook", name, "modified")
            existing_secret = None
            if modified:
                existing_secret = frappe.get_doc("Webhook", name).get_password(
                    "webhook_secret",
                    raise_exception=False,
                )
            prior[name] = (modified, existing_secret)
        issue_field = frappe.db.get_value(
            "Custom Field",
            {"dt": "Issue", "fieldname": "custom_customer_service_company"},
            "name",
        )
        issue_field_modified = (
            frappe.db.get_value("Custom Field", issue_field, "modified")
            if issue_field else None
        )

        result = verify_evidence_webhook_rollback(VERIFY_ENDPOINT, secret, doctype)
        residual_changes = []
        for name, (modified, prior_secret) in prior.items():
            current_modified = frappe.db.get_value("Webhook", name, "modified")
            current_secret = None
            if current_modified:
                current_secret = frappe.get_doc("Webhook", name).get_password(
                    "webhook_secret",
                    raise_exception=False,
                )
            if current_modified != modified or current_secret != prior_secret:
                residual_changes.append(name)
        current_issue_field = frappe.db.get_value(
            "Custom Field",
            {"dt": "Issue", "fieldname": "custom_customer_service_company"},
            "name",
        )
        current_issue_field_modified = (
            frappe.db.get_value("Custom Field", current_issue_field, "modified")
            if current_issue_field else None
        )
        if result["schema_field_installed"]:
            if not current_issue_field:
                residual_changes.append("Issue.custom_customer_service_company")
            else:
                field = frappe.get_doc("Custom Field", current_issue_field)
                if (field.fieldtype, field.options, field.fetch_from) != (
                    "Link", "Company", "custom_customer_service_order.company"
                ):
                    residual_changes.append("Issue.custom_customer_service_company")
        elif (current_issue_field, current_issue_field_modified) != (issue_field, issue_field_modified):
            residual_changes.append("Issue.custom_customer_service_company")
        if residual_changes:
            raise AssertionError("Native webhook rollback left observer changes behind")
        return {
            "verification": "native_company_evidence_webhook_rollback",
            **result,
            "persisted_schema_migration": result["schema_field_persisted"],
            "post_rollback_webhook_changes": 0,
        }
    finally:
        if connected:
            frappe.db.rollback()
        if initialized:
            frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, choices=(COMPANY_SITE,))
    parser.add_argument(
        "--doctype",
        required=True,
        choices=(
            "Purchase Order",
            "Sales Order",
            "Purchase Receipt",
            "Delivery Note",
            "Purchase Invoice",
            "Sales Invoice",
            "Payment Entry",
            "Journal Entry",
            "Issue",
        ),
    )
    parser.add_argument("--confirm-native-rollback-fixture", action="store_true")
    args = parser.parse_args()
    if not args.confirm_native_rollback_fixture:
        parser.error(
            "pass --confirm-native-rollback-fixture to run the native no-network rollback proof"
        )
    print(json.dumps(run(args.site, args.doctype), sort_keys=True))


if __name__ == "__main__":
    main()
