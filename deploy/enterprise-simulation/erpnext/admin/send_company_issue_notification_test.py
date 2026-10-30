"""Opt-in native employee email integration proof; creates an auditable fixture.

This sends one labelled synthetic notification to the responsible employee from
the private company profile. Run only with --confirm-employee-test-email. It
does not use any experiment/source event, and the test Issue key is deliberately
outside the production company-issue key space. It leaves the exact fixture for
independent mailbox inspection and later scoped cleanup.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
COMPANY = "The Agent Company"
PROFILE_PATH = Path("/opt/company-identities.json")


def _route_employee() -> tuple[str, str, str]:
    from integration_contracts import resolve_company_issue_route

    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    route, employee = resolve_company_issue_route(profile)
    if route.actor_id != "logistics" or route.responsible_role != "Delivery User":
        raise RuntimeError("Private company profile does not resolve the expected logistics Delivery User")
    return employee["email"], route.responsible_role, route.requested_outcome


def _existing_fixture_refs(frappe) -> tuple[str, str]:
    supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
    item = frappe.db.get_value("Item", {"disabled": 0}, "name")
    if not supplier or not item:
        raise RuntimeError("Synthetic notification proof requires an existing enabled supplier and item")
    return supplier, item


def _new_draft_purchase_order(frappe, fixture_id: str) -> Any:
    from source_records import source_fields

    supplier, item = _existing_fixture_refs(frappe)
    item_doc = frappe.get_doc("Item", item)
    order = frappe.new_doc("Purchase Order")
    order.company = COMPANY
    order.supplier = supplier
    order.currency = frappe.db.get_value("Company", COMPANY, "default_currency")
    order.transaction_date = date.today().isoformat()
    order.schedule_date = date.today().isoformat()
    order.append("items", {
        "item_code": item,
        "qty": 1,
        "rate": 1,
        "uom": item_doc.stock_uom,
        "conversion_factor": 1,
        "schedule_date": date.today().isoformat(),
    })
    order.update(source_fields(
        source_system="notification-integration-proof", source_id=fixture_id,
        record_type="purchase-order", task_id="synthetic", experiment_id=fixture_id,
        company_id="company", snapshot={"fixture": "synthetic notification integration proof"},
    ))
    order.insert(ignore_permissions=True)
    if order.docstatus != 0:
        raise AssertionError("Synthetic Purchase Order must remain a draft")
    return order


def _write_private_metadata(path: Path, values: dict[str, Any], *, create: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else os.O_TRUNC | os.O_NOFOLLOW)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, 0o600)


def run(site: str, metadata_path: Path) -> dict[str, Any]:
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    (SITES_ROOT / site / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    import frappe
    import owned_site
    from company_issues import ensure_company_issue_contract
    from company_issue_notifications import (
        email_queue_message_ids,
        ensure_notification_contract,
        queue_issue_notification,
        send_owned_notification,
    )

    employee_email, responsible_role, requested_outcome = _route_employee()
    if metadata_path.exists():
        raise FileExistsError("Private notification-test metadata file already exists; choose a new path")

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        if not db.exists("Company", COMPANY):
            raise RuntimeError("Configured native company is unavailable")
        user = frappe.db.get_value("User", employee_email, ["email", "enabled"], as_dict=True)
        if not user or not user.enabled or user.email != employee_email:
            raise RuntimeError("The private company route does not resolve to an enabled ERP User email")

        # Schema setup is separate and durable; only native fixtures follow.
        with contextlib.redirect_stdout(sys.stderr):
            ensure_company_issue_contract()
            from finance_sources import ensure_finance_source_contract
            ensure_finance_source_contract()
            ensure_notification_contract()
        db.commit()

        fixture_id = f"notification-proof-{uuid.uuid4().hex}"
        purchase_order = _new_draft_purchase_order(frappe, fixture_id)
        issue = frappe.new_doc("Issue")
        issue.subject = "[Integration test] Synthetic supplier delivery disruption"
        issue.description = (
            "Integration test only. This synthetic Issue verifies native ERP email delivery. "
            "It does not represent a real customer, order, or source event."
        )
        issue.custom_company_issue_key = f"notification-proof:{fixture_id}"
        issue.custom_responsible_employee = employee_email
        issue.custom_responsible_role = responsible_role
        issue.custom_source_event_id = fixture_id
        issue.custom_requested_outcome = "Integration test only: no business action is requested."
        issue.custom_issue_notification_marker = f"{fixture_id}:notification"
        issue.custom_affected_orders_json = json.dumps([
            {"doctype": "Purchase Order", "name": purchase_order.name},
        ])
        from source_records import source_fields
        issue.update(source_fields(
            source_system="notification-integration-proof", source_id=fixture_id,
            record_type="issue", task_id="synthetic", experiment_id=fixture_id,
            company_id="company", snapshot={"fixture": "synthetic notification integration proof"},
        ))
        issue.insert(ignore_permissions=True)

        receipt = queue_issue_notification(issue)
        # Recheck the native Notification's resolved routing and the persisted
        # queue before the only SMTP send in this script.
        notification = frappe.get_doc("Notification", "Company Supplier Disruption")
        to, cc, bcc = notification.get_list_of_recipients(issue, {})
        queue = frappe.get_doc("Email Queue", receipt["email_queue"])
        if to != [employee_email] or cc or bcc:
            raise RuntimeError("Refusing SMTP: native Notification To/CC/BCC do not match the private route")
        if queue.to != [employee_email] or len(queue.recipients or []) != 1:
            raise RuntimeError("Refusing SMTP: native Email Queue does not target exactly the routed employee")
        if getattr(queue, "cc", None) or getattr(queue, "bcc", None):
            raise RuntimeError("Refusing SMTP: native Email Queue contains CC or BCC recipients")
        if receipt["recipient_email"] != employee_email:
            raise RuntimeError("Refusing SMTP: notification receipt recipient differs from the private route")
        if not receipt["message_id"] or not receipt["subject"]:
            raise RuntimeError("Refusing SMTP: native notification is missing its Message-ID or subject")

        notification_log_fields = {
            field.fieldname for field in frappe.get_meta("Notification Log").fields
        }
        if {"document_type", "document_name"} <= notification_log_fields:
            notification_log_filters = {"document_type": "Issue", "document_name": issue.name}
        elif {"reference_doctype", "reference_name"} <= notification_log_fields:
            notification_log_filters = {"reference_doctype": "Issue", "reference_name": issue.name}
        else:
            raise RuntimeError("Cannot resolve exact native Notification Log links for test fixture cleanup")

        # Persist cleanup identifiers before crossing SMTP so even a send or
        # later observation failure remains recoverable without broad searches.
        _write_private_metadata(metadata_path, {
            "verification": "native_company_issue_notification_employee_delivery",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "site": site,
            "company": COMPANY,
            "fixture_id": fixture_id,
            "issue_key": receipt["issue_key"],
            "issue_name": issue.name,
            "purchase_order_name": purchase_order.name,
            "email_queue": receipt["email_queue"],
            "email_queue_message_id": receipt["email_queue_message_id"],
            "communication": queue.communication,
            "notification_logs": [],
            "message_id": receipt["message_id"],
            "subject": receipt["subject"],
            "recipient_email": employee_email,
            "recipient_role": responsible_role,
            "native_status": queue.status,
            "sent_recipient_count": 0,
            "recipient_count": 1,
            "smtp_send_called": False,
            "body_or_credentials_included": False,
        }, create=True)

        db.commit()  # Persist only this labelled synthetic fixture and native queue.
        send_error_type = None
        send_error = None
        try:
            sent = send_owned_notification(receipt["email_queue"], receipt["issue_key"])
        except Exception as error:
            # The message may have reached SMTP before a later queue update
            # failed. Preserve the exact native state and cleanup identifiers.
            send_error = error
            send_error_type = type(error).__name__
            sent = None
        db.commit()

        queue = frappe.get_doc("Email Queue", receipt["email_queue"])
        issue = frappe.get_doc("Issue", receipt["issue_name"])
        communication_name = queue.communication
        notification_log_error = None
        try:
            notification_log_names = [row.name for row in frappe.get_all(
                "Notification Log", filters=notification_log_filters, fields=["name"],
            )]
        except Exception as error:
            notification_log_names = []
            notification_log_error = type(error).__name__
        mime_message_id, queue_message_id = email_queue_message_ids(queue)

        metadata = {
            "verification": "native_company_issue_notification_employee_delivery",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "site": site,
            "company": COMPANY,
            "fixture_id": fixture_id,
            "issue_key": receipt["issue_key"],
            "issue_name": issue.name,
            "purchase_order_name": purchase_order.name,
            "email_queue": queue.name,
            "communication": communication_name,
            "notification_logs": notification_log_names,
            "message_id": mime_message_id,
            "email_queue_message_id": queue_message_id,
            "subject": receipt["subject"],
            "recipient_email": employee_email,
            "recipient_role": responsible_role,
            "native_status": queue.status,
            "sent_recipient_count": sent["sent_recipient_count"] if sent else sum(
                row.status == "Sent" for row in (queue.recipients or [])
            ),
            "recipient_count": sent["recipient_count"] if sent else len(queue.recipients or []),
            "smtp_send_called": True,
            "send_error_type": send_error_type,
            "notification_log_lookup_error": notification_log_error,
            "body_or_credentials_included": False,
        }
        _write_private_metadata(metadata_path, metadata)
        if send_error_type:
            raise RuntimeError(
                f"Native notification send did not complete; private fixture metadata was written "
                f"(error type: {send_error_type})"
            ) from send_error
        if queue.status != "Sent" or sent["sent_recipient_count"] != 1:
            raise RuntimeError("Native Email Queue did not confirm Sent to its single routed recipient")
        return {
            "verification": metadata["verification"],
            "native_status": queue.status,
            "sent_recipient_count": sent["sent_recipient_count"],
            "fixture_retained_for_mailbox_inspection": True,
            "private_metadata_written": True,
            "smtp_send_called": True,
        }
    finally:
        if db is not None:
            db.rollback()
        frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def cleanup(site: str, metadata_path: Path) -> dict[str, Any]:
    """Delete only the exact labelled fixture after mailbox inspection is done."""
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    fixture_id = metadata.get("fixture_id")
    issue_name = metadata.get("issue_name")
    po_name = metadata.get("purchase_order_name")
    queue_name = metadata.get("email_queue")
    communication_name = metadata.get("communication")
    if (metadata.get("verification") != "native_company_issue_notification_employee_delivery"
            or metadata.get("site") != site
            or not isinstance(fixture_id, str) or not fixture_id.startswith("notification-proof-")
            or metadata.get("issue_key") != f"notification-proof:{fixture_id}"
            or not all(isinstance(value, str) and value for value in (
                issue_name, po_name, queue_name, communication_name,
                metadata.get("message_id"), metadata.get("recipient_email"),
            ))):
        raise ValueError("Private metadata does not identify one labelled notification test fixture")

    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    os.chdir(SITES_ROOT)
    import frappe
    import owned_site

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db

        # Verify every named record and relationship before deleting anything.
        issue = frappe.get_doc("Issue", issue_name)
        if (issue.custom_company_issue_key != metadata["issue_key"]
                or issue.custom_source_system != "notification-integration-proof"
                or issue.custom_source_id != fixture_id):
            raise ValueError("Named Issue does not match the labelled test key and source identity")
        purchase_order = frappe.get_doc("Purchase Order", po_name)
        if (purchase_order.custom_source_system != "notification-integration-proof"
                or purchase_order.custom_source_id != fixture_id or purchase_order.docstatus != 0):
            raise ValueError("Named Purchase Order is not the matching synthetic draft")
        queue = frappe.get_doc("Email Queue", queue_name)
        from company_issue_notifications import email_queue_message_ids
        mime_message_id, queue_message_id = email_queue_message_ids(queue)
        if (queue.reference_doctype != "Issue" or queue.reference_name != issue_name
                or queue_message_id != metadata.get("email_queue_message_id")
                or mime_message_id != metadata["message_id"]
                or queue.communication != communication_name
                or queue.to != [metadata["recipient_email"]]
                or len(queue.recipients or []) != 1 or queue.status != "Sent"):
            raise ValueError("Named Email Queue does not match the delivered single-recipient test")
        communication = frappe.get_doc("Communication", communication_name)
        if (communication.reference_doctype != "Issue" or communication.reference_name != issue_name
                or communication.subject != metadata["subject"]):
            raise ValueError("Named Communication does not match the synthetic Issue and subject")

        logs = []
        log_fields = {field.fieldname for field in frappe.get_meta("Notification Log").fields}
        if {"document_type", "document_name"} <= log_fields:
            filters = {"document_type": "Issue", "document_name": issue_name}
        elif {"reference_doctype", "reference_name"} <= log_fields:
            filters = {"reference_doctype": "Issue", "reference_name": issue_name}
        else:
            filters = None
        expected_logs = set(metadata.get("notification_logs", []))
        if filters:
            rows = frappe.get_all("Notification Log", filters=filters, fields=["name"])
            logs = [row.name for row in rows if row.name in expected_logs]
            if set(logs) != expected_logs:
                raise ValueError("An expected native Notification Log is not linked to the test Issue")
        elif expected_logs:
            raise ValueError("Cannot verify exact native Notification Log references")

        for name in logs:
            frappe.delete_doc("Notification Log", name, force=True, ignore_permissions=True)
        frappe.delete_doc("Email Queue", queue_name, force=True, ignore_permissions=True)
        frappe.delete_doc("Communication", communication_name, force=True, ignore_permissions=True)
        frappe.delete_doc("Issue", issue_name, force=True, ignore_permissions=True)
        frappe.delete_doc("Purchase Order", po_name, force=True, ignore_permissions=True)
        db.commit()

        residual = {
            "Issue": bool(db.exists("Issue", issue_name)),
            "Purchase Order": bool(db.exists("Purchase Order", po_name)),
            "Email Queue": bool(db.exists("Email Queue", queue_name)),
            "Communication": bool(db.exists("Communication", communication_name)),
            "Notification Log": sum(bool(db.exists("Notification Log", name)) for name in logs),
        }
        if any(residual.values()):
            raise RuntimeError("One or more exact notification test records remain after scoped cleanup")
        metadata_path.unlink()
        return {
            "verification": "native_company_issue_notification_fixture_cleanup",
            "deleted_exact_records": {
                "issues": 1, "purchase_orders": 1, "email_queues": 1,
                "communications": 1, "notification_logs": len(logs),
            },
            "residual_records": residual,
        }
    finally:
        if db is not None:
            db.rollback()
        frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def refresh_message_id(site: str, metadata_path: Path) -> dict[str, Any]:
    """Repair the already-sent probe receipt from its exact native MIME header."""
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    fixture_id = metadata.get("fixture_id")
    issue_name = metadata.get("issue_name")
    queue_name = metadata.get("email_queue")
    if (metadata.get("verification") != "native_company_issue_notification_employee_delivery"
            or metadata.get("site") != site
            or not isinstance(fixture_id, str) or not fixture_id.startswith("notification-proof-")
            or not all(isinstance(value, str) and value for value in (issue_name, queue_name))):
        raise ValueError("Private metadata does not identify the labelled notification test fixture")

    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, str(Path(__file__).parent))
    os.chdir(SITES_ROOT)
    import frappe
    import owned_site
    from company_issue_notifications import email_queue_message_ids

    lock_fd = owned_site.lock_for(site)
    db = None
    try:
        frappe.init(site, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        db = frappe.db
        issue = frappe.get_doc("Issue", issue_name)
        queue = frappe.get_doc("Email Queue", queue_name)
        if (issue.custom_company_issue_key != metadata["issue_key"]
                or issue.custom_source_system != "notification-integration-proof"
                or issue.custom_source_id != fixture_id
                or issue.custom_issue_email_queue != queue_name
                or queue.reference_doctype != "Issue" or queue.reference_name != issue_name):
            raise ValueError("Native Issue and Email Queue do not match the exact probe metadata")
        mime_message_id, queue_message_id = email_queue_message_ids(queue)
        if metadata.get("email_queue_message_id") not in (None, queue_message_id):
            raise ValueError("Native Email Queue identity changed since the private metadata was written")
        if metadata.get("message_id") not in (queue_message_id, mime_message_id):
            raise ValueError("Private metadata Message-ID disagrees with the exact native MIME message")
        if issue.custom_issue_email_message_id not in (queue_message_id, mime_message_id):
            raise ValueError("Native Issue notification receipt disagrees with its Email Queue MIME header")
        changed_issue = issue.custom_issue_email_message_id != mime_message_id
        if changed_issue:
            issue.custom_issue_email_message_id = mime_message_id
            issue.save(ignore_permissions=True)
            db.commit()
        metadata["message_id"] = mime_message_id
        metadata["email_queue_message_id"] = queue_message_id
        metadata["native_message_id_corrected_from_mime"] = True
        _write_private_metadata(metadata_path, metadata)
        return {
            "verification": "native_company_issue_notification_mime_message_id_repair",
            "message_id_is_rfc_bracketed": True,
            "issue_receipt_updated": changed_issue,
            "smtp_send_called": False,
            "private_metadata_updated": True,
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
    parser.add_argument("--metadata-path", required=True, type=Path,
                        help="New private file path for Message-ID and exact fixture cleanup references")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm-employee-test-email", action="store_true")
    action.add_argument("--confirm-cleanup-after-mailbox-check", action="store_true")
    action.add_argument("--confirm-refresh-mime-message-id", action="store_true")
    args = parser.parse_args()
    if args.confirm_employee_test_email:
        result = run(args.site, args.metadata_path)
    elif args.confirm_refresh_mime_message_id:
        result = refresh_message_id(args.site, args.metadata_path)
    else:
        result = cleanup(args.site, args.metadata_path)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
