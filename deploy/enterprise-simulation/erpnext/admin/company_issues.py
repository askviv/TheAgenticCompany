"""Idempotent native Issues for source-linked CoffeeBench logistics impacts."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Any

import frappe
from integration_contracts import ExternalSupplyCondition
from pydantic import AwareDatetime, TypeAdapter

from source_records import ensure_source_fields, get_source_document, insert_source_document


COFFEE_SOURCE_SYSTEM = "coffeebench"
COFFEE_PROVIDER_ID = "sakanaai-coffeebench-native-economy"
COFFEE_UPSTREAM_COMMIT = "9e2f3950b5812c8753de81ede9c68babc024b5c8"
ECOMMERCE_PROVIDER_ID = "qwenlm-ecommercebench-calendar"
ECOMMERCE_UPSTREAM_COMMIT = "b84678071c424fc94713b0a9e1ae86c519bd74c7"
COMPANY_ISSUE_RECORD_TYPE = "supplier-disruption-issue"


class CompanyIssueConflict(RuntimeError):
    """A stable issue identity was reused with different source facts."""


def ensure_company_issue_contract() -> None:
    """Install the source and notification-correlation fields on native Issue."""
    ensure_source_fields("Issue", "subject")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields({"Issue": [
        {"fieldname": "custom_company_issue_key", "label": "Company Issue Key", "fieldtype": "Data",
         "insert_after": "custom_source_snapshot", "read_only": 1, "hidden": 1,
         "no_copy": 1, "unique": 1},
        {"fieldname": "custom_responsible_employee", "label": "Responsible Employee", "fieldtype": "Link",
         "options": "User", "insert_after": "custom_company_issue_key", "read_only": 1},
        {"fieldname": "custom_responsible_role", "label": "Responsible Role", "fieldtype": "Data",
         "insert_after": "custom_responsible_employee", "read_only": 1},
        {"fieldname": "custom_source_event_id", "label": "Source Event ID", "fieldtype": "Data",
         "insert_after": "custom_responsible_role", "read_only": 1},
        {"fieldname": "custom_requested_outcome", "label": "Requested Outcome", "fieldtype": "Small Text",
         "insert_after": "custom_source_event_id", "read_only": 1},
        {"fieldname": "custom_issue_notification_marker", "label": "Issue Notification Marker", "fieldtype": "Data",
         "insert_after": "custom_requested_outcome", "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_affected_orders_json", "label": "Affected Native Orders", "fieldtype": "Long Text",
         "insert_after": "custom_issue_notification_marker", "read_only": 1, "hidden": 1,
         "no_copy": 1, "permlevel": 1},
    ]}, update=True)
    for fieldname in (
        "custom_company_issue_key", "custom_responsible_employee", "custom_responsible_role",
        "custom_source_event_id", "custom_requested_outcome", "custom_issue_notification_marker",
        "custom_affected_orders_json",
    ):
        if not frappe.get_meta("Issue").has_field(fieldname):
            raise RuntimeError(f"Native Issue.{fieldname} was not installed")


def _condition(value: dict[str, Any]) -> dict[str, Any]:
    condition = ExternalSupplyCondition.model_validate(value)
    if (condition.source != ECOMMERCE_PROVIDER_ID
            or condition.upstream_commit != ECOMMERCE_UPSTREAM_COMMIT):
        raise ValueError("Only pinned global E-Commerce logistics conditions can create company Issues")
    if condition.effective_until != condition.occurred_at + timedelta(days=condition.duration_days):
        raise ValueError("E-Commerce source condition end does not match its source duration")
    return condition.model_dump(mode="json")


def _same_condition(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _condition(left) == _condition(right)


def _order_ref(doctype: str, document: Any, snapshot: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    native = snapshot.get("native_record")
    if (snapshot.get("kind") != "order" or snapshot.get("native_record_type") != "deal"
            or snapshot.get("source") != COFFEE_PROVIDER_ID
            or snapshot.get("upstream_commit") != COFFEE_UPSTREAM_COMMIT
            or not isinstance(native, dict)):
        raise ValueError("CoffeeBench source-linked ERP order has an invalid native source snapshot")
    if str(native.get("deal_id", "")) != str(document.custom_source_id):
        raise CompanyIssueConflict("Native ERP order source identity does not match its CoffeeBench snapshot")
    condition_ids = snapshot.get("active_source_condition_ids")
    if not isinstance(condition_ids, list) or condition["event_id"] not in condition_ids:
        raise ValueError("CoffeeBench order is not linked to the supplied active source condition")
    linked_conditions = snapshot.get("active_source_conditions")
    if not isinstance(linked_conditions, list):
        raise ValueError("CoffeeBench order is missing its source condition provenance")
    linked = [item for item in linked_conditions if item.get("event_id") == condition["event_id"]]
    if len(linked) != 1 or not _same_condition(linked[0], condition):
        raise CompanyIssueConflict("CoffeeBench order source condition provenance does not match")
    accepted_at = TypeAdapter(AwareDatetime).validate_python(snapshot.get("occurred_at"))
    source_condition = ExternalSupplyCondition.model_validate(condition)
    if not (source_condition.occurred_at <= accepted_at < source_condition.effective_until):
        raise CompanyIssueConflict("CoffeeBench order acceptance time is outside the source condition interval")

    commitments = []
    for line in document.get("items") or []:
        commitments.append({key: line.get(key) for key in (
            "item_code", "qty", "rate", "schedule_date", "delivery_date",
        ) if line.get(key) is not None})
    return {
        "doctype": doctype,
        "name": document.name,
        "source_system": COFFEE_SOURCE_SYSTEM,
        "source_id": str(document.custom_source_id),
        "accepted_at": snapshot["occurred_at"],
        "accepted_at_virtual_minute": native.get("accepted_at_virtual_minute"),
        "native_delivery_at_virtual_minute": native.get("delivery_at_virtual_minute"),
        "original_delivery_commitment": {
            "header_delivery_date": document.get("delivery_date"),
            "items": commitments,
        },
        "source_snapshot": snapshot,
    }


def find_affected_company_orders(company_binding: dict[str, Any], source_condition: dict[str, Any]) -> list[dict[str, Any]]:
    """Find existing ERP orders whose immutable Coffee fact names this condition."""
    condition = _condition(source_condition)
    company_id = company_binding.get("company_id")
    erp_company = company_binding.get("erp_company")
    if not isinstance(company_id, str) or not company_id or not isinstance(erp_company, str) or not erp_company:
        raise ValueError("Bound company and native ERP company are required")
    if not frappe.db.exists("Company", erp_company):
        raise ValueError("Configured native ERP company does not exist")

    affected: list[dict[str, Any]] = []
    for doctype in ("Purchase Order", "Sales Order"):
        rows = frappe.get_all(
            doctype,
            filters={"company": erp_company, "custom_source_system": COFFEE_SOURCE_SYSTEM,
                     "custom_company_id": company_id},
            fields=["name", "custom_source_id", "custom_source_snapshot"],
            order_by="name asc",
        )
        for row in rows:
            document = frappe.get_doc(doctype, row["name"])
            raw_snapshot = document.get("custom_source_snapshot")
            if not raw_snapshot:
                raise CompanyIssueConflict(f"Source-linked native {doctype} is missing its immutable snapshot")
            try:
                snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else dict(raw_snapshot)
            except (TypeError, ValueError) as exc:
                raise CompanyIssueConflict(f"Source-linked native {doctype} has an invalid immutable snapshot") from exc
            active_ids = snapshot.get("active_source_condition_ids")
            if active_ids is None:
                continue
            if not isinstance(active_ids, list):
                raise CompanyIssueConflict(f"CoffeeBench source order {document.name} has invalid condition-link facts")
            if condition["event_id"] not in active_ids:
                continue
            if str(document.get("custom_source_id")) != str(row["custom_source_id"]):
                raise CompanyIssueConflict("Native ERP order source ID changed during condition lookup")
            affected.append(_order_ref(doctype, document, snapshot, condition))
    return affected


def _notification_key(company_id: str, event_id: str, employee_email: str) -> str:
    identity = "\0".join((company_id, event_id, employee_email.strip().casefold()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _order_identity(order: dict[str, Any]) -> tuple[str, str, str]:
    return str(order["doctype"]), str(order["name"]), str(order["source_id"])


def _format_order_link(order: dict[str, Any]) -> str:
    from frappe.utils import get_link_to_form

    commitments = order.get("original_delivery_commitment", {})
    dates = [line.get("schedule_date") or line.get("delivery_date")
             for line in commitments.get("items", [])]
    dates = [str(value) for value in dates if value]
    commitment = ", ".join(dates) if dates else "no native delivery date on the order"
    return (f"- {get_link_to_form(order['doctype'], order['name'])} "
            f"(CoffeeBench deal {order['source_id']}; accepted {order['accepted_at']}; "
            f"original ERP delivery commitment: {commitment}).")


def _issue_description(condition: dict[str, Any], requested_outcome: str,
                       affected_orders: list[dict[str, Any]]) -> str:
    links = "\n".join(_format_order_link(order) for order in affected_orders)
    if not links:
        links = "No matching CoffeeBench order source facts are currently present in the bound ERP company."
    return (
        f"Source logistics condition: {condition['event_name']}\n"
        f"Source event: {condition['event_id']} ({condition['source']}, commit {condition['upstream_commit']})\n"
        f"Effective interval: [{condition['occurred_at']}, {condition['effective_until']})\n"
        f"Native E-Commerce delivery-delay multiplier: {condition['delivery_delay_multiplier']}\n\n"
        f"Affected native order references:\n{links}\n\n"
        f"Requested outcome: {requested_outcome}\n\n"
        "This Issue records source-linked impact. Existing order commitments and source facts are unchanged."
    )


def _ensure_employee_doc_access(doctype: str, document: Any, employee: str, *, write: bool) -> None:
    has_read = frappe.has_permission(doctype, "read", doc=document, user=employee)
    has_write = frappe.has_permission(doctype, "write", doc=document, user=employee)
    if has_read and (has_write or not write):
        return
    has_share = frappe.has_permission(doctype, "share", doc=document, user=employee)
    frappe.share.add(
        doctype, document.name, employee, read=1, write=int(write or has_write),
        share=int(bool(has_share)), notify=0,
    )


def _comment_on_new_orders(issue: Any, orders: list[dict[str, Any]]) -> None:
    if not orders:
        return
    from frappe.utils import escape_html, get_link_to_form

    rows = []
    for order in orders:
        commitments = order.get("original_delivery_commitment", {})
        dates = [line.get("schedule_date") or line.get("delivery_date")
                 for line in commitments.get("items", [])]
        dates = [escape_html(str(value)) for value in dates if value]
        date_text = ", ".join(dates) if dates else "no delivery date recorded"
        rows.append(
            f"<li>{get_link_to_form(order['doctype'], order['name'])} "
            f"(recorded delivery commitment: {date_text})</li>"
        )
    issue.add_comment(
        "Info",
        "<p>Additional company orders linked to this logistics disruption:</p><ul>"
        + "".join(rows) + "</ul>",
    )


def upsert_company_issue(
    company_binding: dict[str, Any],
    source_condition: dict[str, Any],
    *,
    responsible_employee: str,
    responsible_role: str,
    requested_outcome: str,
) -> dict[str, Any]:
    """Create/reuse one Issue per condition and employee, append order refs only."""
    condition = _condition(source_condition)
    company_id = company_binding.get("company_id")
    erp_company = company_binding.get("erp_company")
    employee = responsible_employee.strip() if isinstance(responsible_employee, str) else ""
    role = responsible_role.strip() if isinstance(responsible_role, str) else ""
    outcome = requested_outcome.strip() if isinstance(requested_outcome, str) else ""
    if not isinstance(company_id, str) or not company_id or not isinstance(erp_company, str) or not erp_company:
        raise ValueError("Bound company and native ERP company are required")
    if not employee or not role or not outcome:
        raise ValueError("Responsible employee, role, and requested outcome are required")
    if not frappe.db.exists("Company", erp_company):
        raise ValueError("Configured native ERP company does not exist")
    if not frappe.db.exists("User", employee) or not frappe.db.get_value("User", employee, "enabled"):
        raise ValueError("Configured responsible ERP employee is unavailable")

    orders = find_affected_company_orders(company_binding, condition)
    orders_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("Affected native order reference must be an object")
        key = _order_identity(order)
        if key in orders_by_identity and orders_by_identity[key] != order:
            raise CompanyIssueConflict("Affected native order identity was reused with different facts")
        orders_by_identity[key] = order
    orders = list(orders_by_identity.values())
    issue_key = _notification_key(company_id, condition["event_id"], employee)
    source_id = issue_key
    task_id = company_id
    experiment_id = "background"
    source_snapshot = {
        "company_id": company_id,
        "erp_company": erp_company,
        "source_condition": condition,
        "responsible_employee": employee,
        "responsible_role": role,
        "requested_outcome": outcome,
    }
    description = _issue_description(condition, outcome, orders)
    existing_name = frappe.db.get_value("Issue", {"custom_company_issue_key": issue_key}, "name")
    if existing_name:
        issue = frappe.get_doc("Issue", existing_name)
        expected = {
            "custom_company_issue_key": issue_key,
            "custom_source_event_id": condition["event_id"],
            "custom_responsible_employee": employee,
            "custom_responsible_role": role,
            "custom_requested_outcome": outcome,
            "custom_issue_notification_marker": issue_key,
        }
        if any(issue.get(field) != value for field, value in expected.items()):
            raise CompanyIssueConflict("Existing native Issue notification identity does not match its source")
        source_doc = get_source_document(
            "Issue", source_system=ECOMMERCE_PROVIDER_ID, source_id=source_id,
            record_type=COMPANY_ISSUE_RECORD_TYPE, task_id=task_id,
            experiment_id=experiment_id, company_id=company_id,
        )
        if source_doc is None or json.loads(source_doc.custom_source_snapshot) != source_snapshot:
            raise CompanyIssueConflict("Existing native Issue source snapshot changed")
        prior_json = issue.get("custom_affected_orders_json") or "[]"
        try:
            prior_orders = json.loads(prior_json)
        except (TypeError, ValueError) as exc:
            raise CompanyIssueConflict("Existing native Issue affected-order list is invalid") from exc
        if not isinstance(prior_orders, list):
            raise CompanyIssueConflict("Existing native Issue affected-order list is invalid")
        prior_by_identity = {_order_identity(order): order for order in prior_orders}
        if len(prior_by_identity) != len(prior_orders):
            raise CompanyIssueConflict("Existing native Issue contains duplicate affected-order identities")
        new_orders = []
        for key, order in orders_by_identity.items():
            prior = prior_by_identity.get(key)
            if prior is not None:
                same_identity = all(prior.get(field) == order.get(field)
                                    for field in ("doctype", "name", "source_system", "source_id"))
                if not same_identity or prior.get("source_snapshot") != order.get("source_snapshot"):
                    raise CompanyIssueConflict("Affected native order source facts changed for an existing Issue")
                # Preserve the original commitment snapshot even if a user has
                # since edited the live ERP order.
                continue
            if prior is None:
                prior_by_identity[key] = order
                new_orders.append(order)
        if new_orders:
            issue.custom_affected_orders_json = frappe.as_json(list(prior_by_identity.values()))
            issue.save(ignore_permissions=True)
            _comment_on_new_orders(issue, new_orders)
        for order in orders:
            order_doc = frappe.get_doc(order["doctype"], order["name"])
            _ensure_employee_doc_access(order["doctype"], order_doc, employee, write=False)
        _ensure_employee_doc_access("Issue", issue, employee, write=True)
        return {"doctype": "Issue", "name": issue.name, "created": False,
                "notification_key": issue_key, "responsible_employee": employee,
                "source_event_id": condition["event_id"], "new_affected_orders": new_orders}

    values = {
        "subject": f"Logistics disruption: {condition['event_name']}",
        "description": description,
        "status": "Open",
        "custom_company_issue_key": issue_key,
        "custom_responsible_employee": employee,
        "custom_responsible_role": role,
        "custom_source_event_id": condition["event_id"],
        "custom_requested_outcome": outcome,
        "custom_issue_notification_marker": issue_key,
        "custom_affected_orders_json": frappe.as_json(orders),
    }
    issue, created = insert_source_document(
        "Issue", values, source_system=ECOMMERCE_PROVIDER_ID, source_id=source_id,
        record_type=COMPANY_ISSUE_RECORD_TYPE, task_id=task_id, experiment_id=experiment_id,
        company_id=company_id, snapshot=source_snapshot,
    )
    if (issue.get("custom_company_issue_key") != issue_key
            or issue.get("custom_issue_notification_marker") != issue_key):
        raise CompanyIssueConflict("Native Issue identity does not match its idempotent source key")
    for order in orders:
        order_doc = frappe.get_doc(order["doctype"], order["name"])
        _ensure_employee_doc_access(order["doctype"], order_doc, employee, write=False)
    _ensure_employee_doc_access("Issue", issue, employee, write=True)
    return {"doctype": "Issue", "name": issue.name, "created": created,
            "notification_key": issue_key, "responsible_employee": employee,
            "source_event_id": condition["event_id"], "new_affected_orders": orders}


def reconcile_disruption_issues(
    events: list[dict[str, Any]],
    company_binding: dict[str, Any],
    *,
    responsible_employee: str,
    responsible_role: str,
    requested_outcome: str,
) -> list[dict[str, Any]]:
    """Create/reuse Issues for global disruptions evidenced by existing Coffee orders."""
    if not isinstance(events, list):
        raise ValueError("Company source events must be a list")
    receipts = []
    seen: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Company source event must be an object")
        if event.get("event_type") != "SUPPLIER_SUPPLY_DISRUPTED":
            continue
        if event.get("source") != ECOMMERCE_PROVIDER_ID:
            raise ValueError("Supplier disruption event has an unexpected source identity")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Supplier disruption event payload must be an object")
        if payload.get("affected_categories") != "all":
            continue
        occurred_at = event.get("simulated_time")
        duration = payload.get("upstream_calendar_duration_days")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError("Supplier disruption duration is invalid")
        start = TypeAdapter(AwareDatetime).validate_python(occurred_at)
        condition = _condition({
            "event_id": event.get("event_id"),
            "source": event.get("source"),
            "upstream_commit": event.get("upstream_commit"),
            "occurred_at": start.isoformat(),
            "effective_until": (start + timedelta(days=duration)).isoformat(),
            "duration_days": duration,
            "event_name": payload.get("upstream_event_name"),
            "store_type": payload.get("store_type"),
            "affected_categories": payload.get("affected_categories"),
            "delivery_delay_multiplier": payload.get("delivery_delay_multiplier"),
        })
        prior = seen.get(condition["event_id"])
        if prior is not None:
            if prior != condition:
                raise CompanyIssueConflict("Company source event ID was reused with different disruption facts")
            continue
        seen[condition["event_id"]] = condition
        if not find_affected_company_orders(company_binding, condition):
            continue
        receipts.append(upsert_company_issue(
            company_binding, condition, responsible_employee=responsible_employee,
            responsible_role=responsible_role, requested_outcome=requested_outcome,
        ))
    return receipts


__all__ = [
    "CompanyIssueConflict", "ensure_company_issue_contract", "find_affected_company_orders",
    "reconcile_disruption_issues", "upsert_company_issue",
]
