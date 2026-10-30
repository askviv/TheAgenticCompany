"""Native ERPNext notifications for company-owned disruption Issues."""
from __future__ import annotations

from datetime import datetime
from email import policy
from email.parser import BytesParser
import json
from typing import Any

import frappe


NOTIFICATION_NAME = "Company Supplier Disruption"
ISSUE_OWNERSHIP_FIELD = "custom_company_issue_key"
ISSUE_EMPLOYEE_FIELD = "custom_responsible_employee"
ISSUE_EVENT_FIELD = "custom_source_event_id"
ISSUE_OUTCOME_FIELD = "custom_requested_outcome"
ISSUE_MARKER_FIELD = "custom_issue_notification_marker"
ISSUE_ORDERS_FIELD = "custom_affected_orders_json"
ISSUE_QUEUE_FIELD = "custom_issue_email_queue"
ISSUE_MESSAGE_ID_FIELD = "custom_issue_email_message_id"

BUSINESS_DESCRIPTION = (
    "The linked company orders were accepted under disrupted logistics conditions. "
    "Review their recorded delivery commitments and the operational and financial follow-up."
)

_REQUIRED_BRIDGE_FIELDS = (
    ISSUE_OWNERSHIP_FIELD,
    ISSUE_EMPLOYEE_FIELD,
    ISSUE_EVENT_FIELD,
    ISSUE_OUTCOME_FIELD,
    ISSUE_MARKER_FIELD,
    ISSUE_ORDERS_FIELD,
)


def _notification_values() -> dict[str, Any]:
    return {
        "enabled": 1,
        "is_standard": 0,
        "channel": "Email",
        "event": "Custom",
        "document_type": "Issue",
        "subject": "{{ doc.subject }}",
        "message": (
            "{{ doc.subject }}\n\n" + BUSINESS_DESCRIPTION
            + "\n\n{{ doc.custom_requested_outcome }}"
        ),
        "message_type": "Plain Text",
        "condition_type": "Python",
        # Frappe's safe_eval intentionally omits Python builtins such as bool().
        "condition": f"doc.{ISSUE_OWNERSHIP_FIELD}",
        "send_system_notification": 1,
        "notification_type": "Alert",
        "send_to_all_assignees": 0,
        "recipients": [{"receiver_by_document_field": ISSUE_EMPLOYEE_FIELD}],
    }


def ensure_notification_contract() -> dict[str, Any]:
    """Install Issue receipt fields and the native one-user Email+Desk rule."""
    meta = frappe.get_meta("Issue")
    missing = [field for field in _REQUIRED_BRIDGE_FIELDS if not meta.has_field(field)]
    if missing:
        raise RuntimeError(
            "Company Issue bridge fields must be installed before the notification contract: "
            + ", ".join(missing)
        )

    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if not meta.has_field(ISSUE_QUEUE_FIELD) or not meta.has_field(ISSUE_MESSAGE_ID_FIELD):
        if not meta.has_field(ISSUE_ORDERS_FIELD):
            raise RuntimeError("Issue affected-order field is unavailable for notification receipt fields")
        create_custom_fields({"Issue": [
            {"fieldname": ISSUE_QUEUE_FIELD, "label": "Issue Notification Email Queue",
             "fieldtype": "Link", "options": "Email Queue", "insert_after": ISSUE_ORDERS_FIELD,
             "read_only": 1, "hidden": 1, "no_copy": 1},
            {"fieldname": ISSUE_MESSAGE_ID_FIELD, "label": "Issue Notification Message ID",
             "fieldtype": "Data", "insert_after": ISSUE_QUEUE_FIELD,
             "read_only": 1, "hidden": 1, "no_copy": 1},
        ]})

    if not frappe.db.exists("Notification Type", "Alert"):
        raise RuntimeError("Native ERPNext Notification Type Alert is unavailable")
    values = _notification_values()
    if frappe.db.exists("Notification", NOTIFICATION_NAME):
        notification = frappe.get_doc("Notification", NOTIFICATION_NAME)
        for field, expected in values.items():
            if field == "recipients":
                actual = [{
                    "receiver_by_document_field": row.receiver_by_document_field,
                    "receiver_by_role": row.receiver_by_role,
                    "cc": row.cc,
                    "bcc": row.bcc,
                    "condition": row.condition,
                } for row in (notification.recipients or [])]
                expected = [{**expected[0], "receiver_by_role": None, "cc": None,
                             "bcc": None, "condition": None}]
                if actual != expected:
                    raise RuntimeError("Existing company supplier notification has conflicting recipients")
            elif getattr(notification, field, None) != expected:
                raise RuntimeError(f"Existing company supplier notification conflicts at {field}")
    else:
        notification = frappe.get_doc({"doctype": "Notification", "name": NOTIFICATION_NAME, **values})
        notification.insert(ignore_permissions=True)

    for field in (ISSUE_QUEUE_FIELD, ISSUE_MESSAGE_ID_FIELD):
        if not frappe.get_meta("Issue").has_field(field):
            raise RuntimeError(f"Native Issue notification receipt field {field} was not installed")
    return {"notification": NOTIFICATION_NAME, "receipt_fields": [ISSUE_QUEUE_FIELD, ISSUE_MESSAGE_ID_FIELD]}


def _issue_by_key(issue_key: str):
    if not isinstance(issue_key, str) or not issue_key:
        raise ValueError("company-owned Issue key is required")
    issue_names = frappe.get_all(
        "Issue", filters={ISSUE_OWNERSHIP_FIELD: issue_key}, fields=["name"], limit=2,
    )
    if len(issue_names) != 1:
        raise ValueError("company-owned Issue key must resolve to exactly one native Issue")
    issue = frappe.get_doc("Issue", issue_names[0].name)
    return issue


def _email_queue(name: str):
    if not frappe.db.exists("Email Queue", name):
        raise ValueError("Owned native Email Queue is unavailable")
    return frappe.get_doc("Email Queue", name)


def email_queue_message_ids(queue: Any) -> tuple[str, str]:
    """Return the RFC Message-ID header and ERPNext's raw queue identity."""
    raw_message = getattr(queue, "message", None)
    if isinstance(raw_message, str):
        raw_bytes = raw_message.encode("utf-8")
    elif isinstance(raw_message, bytes):
        raw_bytes = raw_message
    else:
        raise RuntimeError(f"Native Email Queue {queue.name} has no serialized MIME message")
    mime_message = BytesParser(policy=policy.default).parsebytes(raw_bytes, headersonly=True)
    header_value = mime_message.get("Message-ID")
    if not isinstance(header_value, str):
        raise RuntimeError(f"Native Email Queue {queue.name} MIME message has no Message-ID header")
    message_id = header_value.strip()
    if not (message_id.startswith("<") and message_id.endswith(">") and len(message_id) > 2):
        raise RuntimeError(f"Native Email Queue {queue.name} MIME Message-ID is not RFC bracketed")
    queue_message_id = str(getattr(queue, "message_id", "") or "").strip()
    if queue_message_id not in (message_id, message_id[1:-1]):
        raise RuntimeError(
            f"Native Email Queue {queue.name} stored ID disagrees with its MIME Message-ID header"
        )
    return message_id, queue_message_id


def _validate_issue_receipt(issue: Any, queue: Any) -> None:
    message_id, _queue_message_id = email_queue_message_ids(queue)
    if (getattr(issue, "doctype", None) != "Issue" or not getattr(issue, "name", None)
            or not getattr(issue, ISSUE_OWNERSHIP_FIELD, None)):
        raise ValueError("Native Issue is missing its company ownership key")
    if (queue.reference_doctype != "Issue" or queue.reference_name != issue.name
            or queue.name != getattr(issue, ISSUE_QUEUE_FIELD, None)
            or message_id != getattr(issue, ISSUE_MESSAGE_ID_FIELD, None)):
        raise ValueError("Email Queue does not match the company-owned Issue receipt")
    user = frappe.db.get_value("User", issue.get(ISSUE_EMPLOYEE_FIELD), ["email", "enabled"], as_dict=True)
    if (not user or not user.enabled or len(queue.recipients or []) != 1
            or [address.casefold() for address in queue.to] != [user.email.casefold()]):
        raise ValueError("Owned Email Queue recipient changed from its responsible employee")


def _receipt(issue: Any, queue: Any) -> dict[str, Any]:
    message_id, queue_message_id = email_queue_message_ids(queue)
    recipients = list(queue.recipients or [])
    recipient_emails = queue.to
    if len(recipient_emails) != 1:
        raise RuntimeError(f"Owned Email Queue {queue.name} does not have exactly one To recipient")
    if not queue.communication or not frappe.db.exists("Communication", queue.communication):
        raise RuntimeError(f"Native Email Queue {queue.name} has no linked Communication receipt")
    communication = frappe.get_doc("Communication", queue.communication)
    if not communication.subject:
        raise RuntimeError(f"Native Communication {communication.name} has no subject")
    affected_orders = json.loads(getattr(issue, ISSUE_ORDERS_FIELD))
    if not isinstance(affected_orders, list) or any(
        not isinstance(order, dict) or not isinstance(order.get("doctype"), str)
        or not isinstance(order.get("name"), str) for order in affected_orders
    ):
        raise ValueError("Owned Issue affected-order JSON must contain native doctype/name links")
    return {
        "issue_key": getattr(issue, ISSUE_OWNERSHIP_FIELD),
        "issue_name": issue.name,
        "email_queue": queue.name,
        "message_id": message_id,
        "email_queue_message_id": queue_message_id,
        "subject": communication.subject,
        "recipient_email": recipient_emails[0],
        "status": queue.status,
        "business_description": BUSINESS_DESCRIPTION,
        "requested_outcome": getattr(issue, ISSUE_OUTCOME_FIELD),
        "affected_orders": affected_orders,
        "recipient_count": len(recipients),
        "sent_recipient_count": sum(row.status == "Sent" for row in recipients),
    }


def _new_error_log_names(before: set[str], since: datetime) -> list[str]:
    return [row.name for row in frappe.get_all(
        "Error Log",
        filters={"title": "Failed to send Notification", "creation": [">=", since]},
        fields=["name", "creation"], order_by="creation desc",
    ) if row.name not in before]


def queue_issue_notification(issue: Any) -> dict[str, Any]:
    """Call native Notification.send and capture its exact new Email Queue.

    This function does not commit. The Issue, native Communication, Email Queue,
    and receipt fields remain part of the caller's transaction. Notification's
    System Notification is enqueued after commit by Frappe itself.
    """
    _validate_issue_receipt_fields(issue)
    if getattr(issue, ISSUE_QUEUE_FIELD, None):
        queue = _email_queue(issue.get(ISSUE_QUEUE_FIELD))
        _validate_issue_receipt(issue, queue)
        return _receipt(issue, queue)

    notification = frappe.get_doc("Notification", NOTIFICATION_NAME)
    if not notification.enabled or notification.event != "Custom" or notification.document_type != "Issue":
        raise RuntimeError("Native company supplier Notification is not enabled for Issue Custom events")
    recipient, cc, bcc = notification.get_list_of_recipients(issue, {})
    employee = issue.get(ISSUE_EMPLOYEE_FIELD)
    user = frappe.db.get_value("User", employee, ["email", "enabled"], as_dict=True)
    if not user or not user.enabled or recipient != [user.email] or cc or bcc:
        raise RuntimeError("Native company Issue Notification does not resolve exactly its responsible employee")

    queues_before = {
        row.name for row in frappe.get_all(
            "Email Queue", filters={"reference_doctype": "Issue", "reference_name": issue.name},
            fields=["name"],
        )
    }
    errors_before = {row.name for row in frappe.get_all("Error Log", fields=["name"])}
    started_at = frappe.utils.now_datetime()
    notification.send(issue)
    queue_names = [row.name for row in frappe.get_all(
        "Email Queue", filters={"reference_doctype": "Issue", "reference_name": issue.name},
        fields=["name"],
    ) if row.name not in queues_before]
    if len(queue_names) != 1:
        error_refs = _new_error_log_names(errors_before, started_at)
        error_ref = error_refs[0] if error_refs else "no matching Error Log was recorded"
        raise RuntimeError(
            f"Native Notification created {len(queue_names)} Email Queues for Issue {issue.name}; "
            f"expected exactly one; see Error Log {error_ref}"
        )

    queue = _email_queue(queue_names[0])
    _validate_issue_receipt_fields(issue)
    if (queue.reference_doctype != "Issue" or queue.reference_name != issue.name
            or not email_queue_message_ids(queue)[0] or queue.to != recipient):
        raise RuntimeError("Native Notification Email Queue failed exact Issue, recipient, or Message-ID validation")
    issue.set(ISSUE_QUEUE_FIELD, queue.name)
    issue.set(ISSUE_MESSAGE_ID_FIELD, email_queue_message_ids(queue)[0])
    issue.save(ignore_permissions=True)
    _validate_issue_receipt(issue, queue)
    return _receipt(issue, queue)


def _validate_issue_receipt_fields(issue: Any) -> None:
    if getattr(issue, "doctype", None) != "Issue" or not getattr(issue, "name", None):
        raise ValueError("A persisted native Issue is required")
    if not getattr(issue, ISSUE_OWNERSHIP_FIELD, None):
        raise ValueError("Native Issue is missing its company ownership key")
    if not getattr(issue, ISSUE_EMPLOYEE_FIELD, None):
        raise ValueError("Native Issue is missing its responsible employee")
    if not getattr(issue, ISSUE_EVENT_FIELD, None):
        raise ValueError("Native Issue is missing its source event identity")
    if not getattr(issue, ISSUE_MARKER_FIELD, None):
        raise ValueError("Native Issue is missing its owned notification marker")


def send_owned_notification(queue_name: str, issue_key: str) -> dict[str, Any]:
    """Send only the Email Queue receipt stored on the matching owned Issue."""
    issue = _issue_by_key(issue_key)
    queue = _email_queue(queue_name)
    _validate_issue_receipt(issue, queue)
    if queue.status == "Sent":
        return _receipt(issue, queue)
    if queue.status not in ("Not Sent", "Partially Sent"):
        raise RuntimeError(f"Owned Email Queue {queue.name} cannot send from native status {queue.status}")

    queue.send()
    queue = _email_queue(queue_name)
    _validate_issue_receipt(issue, queue)
    receipt = _receipt(issue, queue)
    if queue.status != "Sent" or receipt["sent_recipient_count"] != receipt["recipient_count"]:
        raise RuntimeError(
            f"Owned Email Queue {queue.name} did not reach native Sent status "
            f"(current status: {queue.status})"
        )
    return receipt


def list_owned_notification_receipts() -> list[dict[str, Any]]:
    """Read receipts linked from owned Issues only; never scan arbitrary mail."""
    issues = frappe.get_all(
        "Issue",
        filters={ISSUE_OWNERSHIP_FIELD: ["is", "set"], ISSUE_QUEUE_FIELD: ["is", "set"]},
        fields=["name", ISSUE_OWNERSHIP_FIELD, ISSUE_EVENT_FIELD, ISSUE_QUEUE_FIELD,
                ISSUE_MESSAGE_ID_FIELD],
        order_by="creation asc, name asc",
    )
    receipts = []
    for row in issues:
        issue = frappe.get_doc("Issue", row.name)
        queue = _email_queue(row.get(ISSUE_QUEUE_FIELD))
        _validate_issue_receipt(issue, queue)
        receipts.append(_receipt(issue, queue))
    return receipts


__all__ = [
    "email_queue_message_ids",
    "ensure_notification_contract",
    "queue_issue_notification",
    "send_owned_notification",
    "list_owned_notification_receipts",
]
