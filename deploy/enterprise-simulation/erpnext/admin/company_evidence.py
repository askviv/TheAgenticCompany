"""Install native signed Webhooks and read existing source-linked company records."""

from __future__ import annotations

import json
from typing import Any

import frappe


COMPANY_NAME = "The Agent Company"
COMPANY_ID = "company"
DOCTYPES = (
    "Purchase Order",
    "Sales Order",
    "Purchase Receipt",
    "Delivery Note",
    "Purchase Invoice",
    "Sales Invoice",
    "Payment Entry",
    "Journal Entry",
)
DOC_EVENTS = (
    "on_update",
    "on_submit",
    "on_cancel",
    "on_update_after_submit",
    "on_trash",
)
ISSUE_EVENTS = ("on_update", "on_trash")
ADDRESS_EVENTS = ("on_update",)
SOURCE_FIELDS = (
    "custom_source_system",
    "custom_source_id",
    "custom_source_key",
    "custom_task_id",
    "custom_experiment_id",
    "custom_company_id",
    "custom_source_snapshot",
)


def _payload_template(event: str, erp_timezone: str, *, linked_company: bool = False) -> str:
    # Frappe's native JSON webhook renderer converts document dates to strings
    # before this template executes. Its `json` filter is frappe.as_json.
    template = (
        '{"event":'
        + frappe.as_json(event)
        + ',"document":{{ doc|json }}'
        + ',"erp_timezone":'
        + frappe.as_json(erp_timezone)
    )
    if linked_company:
        template += ',"linked_company":{{ doc.custom_customer_service_company|json }}'
    return template + "}"


def _address_customer_condition() -> str:
    """Scope Address hooks to Customer links already bound to this company."""
    customer_names = sorted(row["name"] for row in frappe.get_all(
        "Customer",
        filters={
            "custom_company_id": COMPANY_ID,
            "custom_source_system": ["is", "set"],
            "custom_source_id": ["is", "set"],
        },
        fields=["name"],
    ))
    return (
        "any(link.link_doctype == 'Customer' and link.link_name in "
        + frappe.as_json(customer_names)
        + " for link in doc.links)"
    ) if customer_names else "False"


def refresh_address_evidence_scope() -> dict[str, Any]:
    """Refresh the signed Address hook allowlist after new company customers arrive."""
    name = _webhook_name("Address", "on_update")
    if not frappe.db.exists("Webhook", name):
        return {"status": "not_installed"}
    webhook = frappe.get_doc("Webhook", name)
    if webhook.webhook_doctype != "Address" or webhook.webhook_docevent != "on_update":
        raise RuntimeError("Native Address evidence webhook has a conflicting event identity")
    condition = _address_customer_condition()
    if webhook.condition != condition:
        webhook.condition = condition
        webhook.save(ignore_permissions=True)
        return {"status": "updated"}
    return {"status": "current"}


def _webhook_name(doctype: str, event: str) -> str:
    return f"TAC Company Evidence - {doctype} - {event}"


def _ensure_issue_company_field() -> bool:
    """Ensure the one required schema field; return whether this call installed it."""
    meta = frappe.get_meta("Issue")
    field = meta.get_field("custom_customer_service_company")
    installed = field is None
    if installed:
        if not meta.has_field("custom_customer_service_order"):
            raise RuntimeError("Native Issue service-order link is unavailable")
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

        create_custom_fields({"Issue": [{
        "fieldname": "custom_customer_service_company",
        "label": "Customer Service Company",
        "fieldtype": "Link",
        "options": "Company",
        "fetch_from": "custom_customer_service_order.company",
        "insert_after": "custom_customer_service_order",
        "read_only": 1,
        "no_copy": 1,
        }]}, update=True)
    field = frappe.get_meta("Issue").get_field("custom_customer_service_company")
    if not field or (field.fieldtype, field.options, field.fetch_from) != (
        "Link", "Company", "custom_customer_service_order.company"
    ):
        raise RuntimeError("Native Issue linked-company evidence field is unavailable or conflicting")
    return installed


def ensure_evidence_webhooks(endpoint: str, secret: str) -> dict[str, Any]:
    """Create/update the full native evidence observer with Frappe HMAC signing.

    The secret is stored only in Frappe's native Password field. No secret is
    placed in the URL, webhook headers, return value, or output logs.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("Company evidence webhook endpoint is required")
    endpoint = endpoint.strip()
    if not isinstance(secret, str) or not secret:
        raise ValueError("Company evidence webhook signing secret is required")
    erp_timezone = frappe.db.get_single_value("System Settings", "time_zone")
    if not isinstance(erp_timezone, str) or not erp_timezone.strip():
        raise RuntimeError("Native ERPNext system timezone is unavailable")
    if not frappe.db.exists("Company", COMPANY_NAME):
        raise RuntimeError("Native company is unavailable for the evidence observer")

    _ensure_issue_company_field()

    installed, created, updated = [], 0, 0
    for doctype in DOCTYPES:
        if not frappe.db.exists("DocType", doctype) or not frappe.get_meta(
            doctype
        ).has_field("company"):
            raise RuntimeError(f"Native ERPNext {doctype} company field is unavailable")
        for event in DOC_EVENTS:
            name = _webhook_name(doctype, event)
            values = {
                "doctype": "Webhook",
                "name": name,
                "webhook_doctype": doctype,
                "webhook_docevent": event,
                "enabled": 1,
                "condition": f"doc.company == {json.dumps(COMPANY_NAME)}",
                "request_url": endpoint,
                "request_method": "POST",
                "request_structure": "JSON",
                "webhook_json": _payload_template(event, erp_timezone),
                "enable_security": 1,
                "webhook_secret": secret,
                "timeout": 10,
                "background_jobs_queue": "short",
                "webhook_headers": [],
                "webhook_data": [],
            }
            if frappe.db.exists("Webhook", name):
                doc = frappe.get_doc("Webhook", name)
                if doc.webhook_doctype != doctype or doc.webhook_docevent != event:
                    raise RuntimeError(
                        f"Native evidence webhook {name} has a conflicting event identity"
                    )
                changed = False
                for field, value in values.items():
                    if field in {
                        "doctype",
                        "name",
                        "webhook_doctype",
                        "webhook_docevent",
                        "webhook_secret",
                    }:
                        continue
                    if getattr(doc, field, None) != value:
                        setattr(doc, field, value)
                        changed = True
                # Frappe's Password field is encrypted. Compare only in memory;
                # never return or log its value.
                try:
                    current_secret = doc.get_password(
                        "webhook_secret", raise_exception=False
                    )
                except TypeError:
                    current_secret = doc.get_password("webhook_secret", False)
                if current_secret != secret:
                    doc.webhook_secret = secret
                    changed = True
                if changed:
                    doc.save(ignore_permissions=True)
                    updated += 1
            else:
                doc = frappe.get_doc(values)
                doc.insert(ignore_permissions=True)
                created += 1
            installed.append(name)
    issue_meta = frappe.get_meta("Issue")
    if not issue_meta.has_field("custom_customer_service_order"):
        raise RuntimeError("Native Issue service-order link is unavailable")
    for event in ISSUE_EVENTS:
        name = _webhook_name("Issue", event)
        values = {
            "doctype": "Webhook",
            "name": name,
            "webhook_doctype": "Issue",
            "webhook_docevent": event,
            "enabled": 1,
            "condition": f"doc.custom_customer_service_company == {json.dumps(COMPANY_NAME)}",
            "request_url": endpoint,
            "request_method": "POST",
            "request_structure": "JSON",
            "webhook_json": _payload_template(event, erp_timezone, linked_company=True),
            "enable_security": 1,
            "webhook_secret": secret,
            "timeout": 10,
            "background_jobs_queue": "short",
            "webhook_headers": [],
            "webhook_data": [],
        }
        if frappe.db.exists("Webhook", name):
            doc = frappe.get_doc("Webhook", name)
            if doc.webhook_doctype != "Issue" or doc.webhook_docevent != event:
                raise RuntimeError(f"Native evidence webhook {name} has a conflicting event identity")
            changed = False
            for field, value in values.items():
                if field in {"doctype", "name", "webhook_doctype", "webhook_docevent", "webhook_secret"}:
                    continue
                if getattr(doc, field, None) != value:
                    setattr(doc, field, value)
                    changed = True
            try:
                current_secret = doc.get_password("webhook_secret", raise_exception=False)
            except TypeError:
                current_secret = doc.get_password("webhook_secret", False)
            if current_secret != secret:
                doc.webhook_secret = secret
                changed = True
            if changed:
                doc.save(ignore_permissions=True)
                updated += 1
        else:
            frappe.get_doc(values).insert(ignore_permissions=True)
            created += 1
        installed.append(name)
    address_condition = _address_customer_condition()
    for event in ADDRESS_EVENTS:
        name = _webhook_name("Address", event)
        values = {
            "doctype": "Webhook",
            "name": name,
            "webhook_doctype": "Address",
            "webhook_docevent": event,
            "enabled": 1,
            "condition": address_condition,
            "request_url": endpoint,
            "request_method": "POST",
            "request_structure": "JSON",
            "webhook_json": _payload_template(event, erp_timezone)
            .removesuffix("}")
            + ',"linked_company":'
            + frappe.as_json(COMPANY_NAME)
            + "}",
            "enable_security": 1,
            "webhook_secret": secret,
            "timeout": 10,
            "background_jobs_queue": "short",
            "webhook_headers": [],
            "webhook_data": [],
        }
        if frappe.db.exists("Webhook", name):
            doc = frappe.get_doc("Webhook", name)
            if doc.webhook_doctype != "Address" or doc.webhook_docevent != event:
                raise RuntimeError(f"Native evidence webhook {name} has a conflicting event identity")
            changed = False
            for field, value in values.items():
                if field in {"doctype", "name", "webhook_doctype", "webhook_docevent", "webhook_secret"}:
                    continue
                if getattr(doc, field, None) != value:
                    setattr(doc, field, value)
                    changed = True
            try:
                current_secret = doc.get_password("webhook_secret", raise_exception=False)
            except TypeError:
                current_secret = doc.get_password("webhook_secret", False)
            if current_secret != secret:
                doc.webhook_secret = secret
                changed = True
            if changed:
                doc.save(ignore_permissions=True)
                updated += 1
        else:
            frappe.get_doc(values).insert(ignore_permissions=True)
            created += 1
        installed.append(name)
    return {
        "company": COMPANY_NAME,
        "endpoint": endpoint,
        "webhooks": len(installed),
        "created": created,
        "updated": updated,
        "doctype_count": len(DOCTYPES),
        "issue_webhooks": len(ISSUE_EVENTS),
        "event_count": len(DOC_EVENTS),
    }


def collect_mapped_records() -> list[dict[str, Any]]:
    """Read only existing native company documents with intact source identity."""
    if not frappe.db.exists("Company", COMPANY_NAME):
        raise RuntimeError(
            "Native company is unavailable for source-linked record collection"
        )
    records: list[dict[str, Any]] = []
    for doctype in DOCTYPES:
        meta = frappe.get_meta(doctype)
        if not meta.has_field("company") or not all(
            meta.has_field(field) for field in SOURCE_FIELDS
        ):
            continue
        rows = frappe.get_all(
            doctype,
            filters={
                "company": COMPANY_NAME,
                "custom_company_id": COMPANY_ID,
                "custom_source_system": ["is", "set"],
                "custom_source_id": ["is", "set"],
            },
            fields=["name", *SOURCE_FIELDS],
            order_by="name asc",
        )
        for row in rows:
            document = frappe.get_doc(doctype, row["name"])
            if (
                document.company != COMPANY_NAME
                or document.custom_company_id != COMPANY_ID
                or any(
                    not document.get(field)
                    for field in SOURCE_FIELDS
                    if field != "custom_source_snapshot"
                )
            ):
                raise RuntimeError(
                    f"Source-linked native {doctype} identity changed during collection"
                )
            raw_snapshot = document.custom_source_snapshot
            if not raw_snapshot:
                raise RuntimeError(
                    f"Source-linked native {doctype} has no immutable source snapshot"
                )
            try:
                snapshot = (
                    json.loads(raw_snapshot)
                    if isinstance(raw_snapshot, str)
                    else dict(raw_snapshot)
                )
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"Source-linked native {doctype} has an invalid source snapshot"
                ) from error
            records.append(
                {
                    "company_id": COMPANY_ID,
                    "doctype": doctype,
                    "name": document.name,
                    "source_system": document.custom_source_system,
                    "source_id": document.custom_source_id,
                    "source_key": document.custom_source_key,
                    "task_id": document.custom_task_id,
                    "experiment_id": document.custom_experiment_id,
                    "source_snapshot": snapshot,
                    "document": document.as_dict(convert_dates_to_str=True),
                }
            )
    # Address has no company field. It is admitted only when linked to a
    # source-mapped Customer in this company; the webhook condition applies the
    # same boundary before any address content leaves ERPNext.
    customer_rows = frappe.get_all(
        "Customer",
        filters={"custom_company_id": COMPANY_ID,
                 "custom_source_system": ["is", "set"], "custom_source_id": ["is", "set"]},
        fields=["name", *SOURCE_FIELDS],
        order_by="name asc",
    )
    for customer_row in customer_rows:
        customer = frappe.get_doc("Customer", customer_row["name"])
        address_name = customer.customer_primary_address
        if not address_name or not frappe.db.exists("Address", address_name):
            continue
        address = frappe.get_doc("Address", address_name)
        address_links = getattr(address, "links", []) or []
        if not any(row.link_doctype == "Customer" and row.link_name == customer.name for row in address_links):
            continue
        if (address.custom_company_id != COMPANY_ID
                or address.custom_source_system != customer.custom_source_system
                or address.custom_source_id != customer.custom_source_id):
            continue
        records.append({
            "doctype": "Address",
            "name": address.name,
            "source_system": address.custom_source_system,
            "source_id": address.custom_source_id,
            "source_key": address.custom_source_key,
            "task_id": address.custom_task_id,
            "experiment_id": address.custom_experiment_id,
            "company_id": address.custom_company_id,
            "source_snapshot": address.custom_source_snapshot,
            "document": address.as_dict(convert_dates_to_str=True),
        })
    # Item is global in ERPNext and has no company field. Admit source Items
    # only through an uncancelled PO in this company, then require the existing
    # company/source identity on that exact Item.
    purchase_orders = frappe.get_all(
        "Purchase Order",
        filters={"company": COMPANY_NAME, "docstatus": ["in", [0, 1]]},
        fields=["name"],
        order_by="name asc",
    )
    order_names = [row["name"] for row in purchase_orders]
    item_codes = set()
    if order_names:
        item_rows = frappe.get_all(
            "Purchase Order Item",
            filters={"parent": ["in", order_names]},
            fields=["item_code"],
            order_by="item_code asc",
        )
        item_codes = {row["item_code"] for row in item_rows if row.get("item_code")}
    if item_codes:
        item_rows = frappe.get_all(
            "Item",
            filters={
                "name": ["in", sorted(item_codes)],
                "custom_company_id": COMPANY_ID,
                "custom_source_system": ["is", "set"],
                "custom_source_id": ["is", "set"],
            },
            fields=["name", *SOURCE_FIELDS],
            order_by="name asc",
        )
        for item_row in item_rows:
            item = frappe.get_doc("Item", item_row["name"])
            if (
                item.custom_company_id != COMPANY_ID
                or not item.custom_source_system
                or not item.custom_source_id
                or not item.custom_source_key
                or not item.custom_task_id
                or not item.custom_experiment_id
                or not item.custom_source_snapshot
            ):
                raise RuntimeError("Company-linked Purchase Order Item has incomplete source identity")
            try:
                snapshot = (
                    json.loads(item.custom_source_snapshot)
                    if isinstance(item.custom_source_snapshot, str)
                    else dict(item.custom_source_snapshot)
                )
            except (TypeError, ValueError) as error:
                raise RuntimeError("Company-linked Item has an invalid source snapshot") from error
            records.append({
                "company_id": COMPANY_ID,
                "doctype": "Item",
                "name": item.name,
                "source_system": item.custom_source_system,
                "source_id": item.custom_source_id,
                "source_key": item.custom_source_key,
                "task_id": item.custom_task_id,
                "experiment_id": item.custom_experiment_id,
                "source_snapshot": snapshot,
                "document": item.as_dict(convert_dates_to_str=True),
            })
    return records


def collect_company_issue_events() -> dict[str, Any]:
    """Recover existing τ service requests only through their actual linked Sales Order."""
    timezone = frappe.db.get_single_value("System Settings", "time_zone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise RuntimeError("Native ERPNext system timezone is unavailable")
    if not frappe.get_meta("Issue").has_field("custom_customer_service_order"):
        raise RuntimeError("Native Issue service-order link is unavailable")
    rows = frappe.get_all(
        "Issue",
        filters={
            "custom_customer_service_order": ["is", "set"],
            "custom_customer_service_type": ["in", ["Return", "Exchange"]],
        },
        fields=["name", "custom_customer_service_order", "custom_customer_service_type"],
        order_by="name asc",
    )
    events = []
    for row in rows:
        linked_company = frappe.db.get_value(
            "Sales Order", row["custom_customer_service_order"], "company"
        )
        if linked_company != COMPANY_NAME:
            continue
        document = frappe.get_doc("Issue", row["name"])
        events.append({
            "event": "on_update",
            "erp_timezone": timezone,
            "linked_company": linked_company,
            "document": document.as_dict(convert_dates_to_str=True),
        })
    return {"issue_events": events}


def verify_evidence_webhook_render(
    webhook_name: str, doctype: str, document_name: Any
) -> dict[str, Any]:
    """Read-only verifier for native body, company condition, signature, and state.

    It invokes Frappe's own webhook render/signing helpers and never performs an
    HTTP request. Callers may wrap installer/fixture setup in a database
    savepoint and roll that setup back after this verification.
    """
    if doctype not in DOCTYPES + ("Issue", "Address"):
        raise ValueError(
            "Evidence webhook verifier received an unsupported native DocType"
        )
    webhook = frappe.get_doc("Webhook", webhook_name)
    document = (
        document_name
        if hasattr(document_name, "as_dict")
        else frappe.get_doc(doctype, document_name)
    )
    document_name = document.name
    is_issue = doctype == "Issue"
    is_address = doctype == "Address"
    linked_company = (
        getattr(document, "custom_customer_service_company", None)
        if is_issue
        else COMPANY_NAME if is_address else getattr(document, "company", None)
    )
    address_has_company_customer = is_address and any(
        row.link_doctype == "Customer"
        and row.link_name in {
            customer["name"] for customer in frappe.get_all(
                "Customer",
                filters={"custom_company_id": COMPANY_ID,
                         "custom_source_system": ["is", "set"],
                         "custom_source_id": ["is", "set"]},
                fields=["name"],
            )
        }
        for row in (getattr(document, "links", None) or [])
    )
    if (
        not webhook.enabled
        or not webhook.enable_security
        or webhook.webhook_doctype != doctype
        or not webhook.webhook_docevent
        or webhook.condition != (
            f"doc.custom_customer_service_company == {json.dumps(COMPANY_NAME)}"
            if is_issue
            else _address_customer_condition() if is_address
            else f"doc.company == {json.dumps(COMPANY_NAME)}"
        )
    ):
        raise RuntimeError(
            "Native evidence webhook is disabled or has a conflicting identity"
        )
    if linked_company != COMPANY_NAME or (is_address and not address_has_company_customer):
        raise ValueError(
            "Evidence webhook verifier requires a native document from the company"
        )
    if webhook.webhook_docevent == "on_submit" and int(document.docstatus) != 1:
        raise RuntimeError(
            "Native on-submit evidence verifier requires a submitted document"
        )
    if webhook.webhook_docevent == "on_cancel" and int(document.docstatus) != 2:
        raise RuntimeError(
            "Native on-cancel evidence verifier requires a cancelled document"
        )
    if (
        webhook.webhook_docevent == "on_update_after_submit"
        and int(document.docstatus) != 1
    ):
        raise RuntimeError(
            "Native after-submit evidence verifier requires a submitted document"
        )
    from frappe.integrations.doctype.webhook.webhook import (
        get_webhook_data,
        get_webhook_headers,
    )

    payload = get_webhook_data(document, webhook)
    if (
        payload.get("event") != webhook.webhook_docevent
        or payload.get("erp_timezone")
        != frappe.db.get_single_value("System Settings", "time_zone")
        or not isinstance(payload.get("document"), dict)
        or payload["document"].get("doctype") != doctype
        or payload["document"].get("name") != document_name
        or (
            payload.get("linked_company")
            if is_issue or is_address
            else payload["document"].get("company")
        ) != COMPANY_NAME
    ):
        raise RuntimeError(
            "Native evidence webhook rendered an incomplete event envelope"
        )
    headers = get_webhook_headers(document, webhook, data=payload)
    signature = headers.get("X-Frappe-Webhook-Signature")
    if not signature:
        raise RuntimeError("Native Frappe webhook did not produce its HMAC signature")
    return {
        "event": payload["event"],
        "doctype": doctype,
        "name": document_name,
        "docstatus": int(document.docstatus),
        "company": COMPANY_NAME,
        "signed": True,
        "transmitted": False,
    }


def verify_evidence_webhook_rollback(
    endpoint: str,
    secret: str,
    doctype: str,
) -> dict[str, Any]:
    """Install temporarily and verify a synthetic posted doc without HTTP.

    Webhook fixture writes are rolled back to the caller's savepoint. The
    company document is read only; Frappe's own renderer/signing helpers are
    called directly, so no network request or external side effect occurs.
    """
    if doctype not in DOCTYPES + ("Issue",):
        raise ValueError(
            "Evidence rollback verifier received an unsupported native DocType"
        )
    # Custom Field installation executes schema DDL, which commits the current
    # MariaDB transaction. Perform that intentional schema setup first so the
    # following savepoint can reliably roll back webhook fixture mutations.
    issue_field_installed = _ensure_issue_company_field()
    savepoint = "company_evidence_webhook_verification"
    frappe.db.savepoint(savepoint)
    try:
        ensure_evidence_webhooks(endpoint, secret)
        issue = doctype == "Issue"
        event = "on_update" if issue else "on_submit"
        document = frappe.get_doc({
            "doctype": doctype,
            "name": "TAC-COMPANY-EVIDENCE-ROLLBACK-FIXTURE",
            "company": COMPANY_NAME,
            "custom_customer_service_company": COMPANY_NAME if issue else None,
            "custom_customer_service_order": "TAC-SYNTHETIC-SO" if issue else None,
            "docstatus": 0 if issue else 1,
        })
        # This unsaved native Frappe Document exercises the same `as_dict`,
        # JSON renderer, condition, and signing functions without creating or
        # submitting a business transaction.
        result = verify_evidence_webhook_render(
            _webhook_name(doctype, event),
            doctype,
            document,
        )
    except BaseException as original:
        try:
            frappe.db.rollback(save_point=savepoint)
        except BaseException as cleanup:
            original.add_note(
                f"Evidence webhook verification rollback failed: {type(cleanup).__name__}"
            )
        raise
    frappe.db.rollback(save_point=savepoint)
    return {
        "installed_webhooks": len(DOCTYPES) * len(DOC_EVENTS) + len(ISSUE_EVENTS) + len(ADDRESS_EVENTS),
        **result,
        "fixture": "unsaved_native_issue_state" if doctype == "Issue" else "unsaved_native_posted_state",
        "schema_field_installed": issue_field_installed,
        "schema_field_persisted": issue_field_installed,
        "rolled_back": True,
    }


__all__ = [
    "COMPANY_NAME",
    "COMPANY_ID",
    "DOCTYPES",
    "DOC_EVENTS",
    "ISSUE_EVENTS",
    "ensure_evidence_webhooks",
    "collect_mapped_records",
    "collect_company_issue_events",
    "refresh_address_evidence_scope",
    "verify_evidence_webhook_render",
    "verify_evidence_webhook_rollback",
]


def collect_evidence_deliveries(cursor: dict[str, str] | None = None) -> dict[str, Any]:
    """Read one bounded page of native post-commit delivery evidence."""
    cursor = cursor or {"creation": "1900-01-01 00:00:00", "name": ""}
    rows = frappe.db.sql(
        "SELECT name,creation,data FROM `tabWebhook Request Log` "
        "WHERE webhook LIKE %s AND (creation > %s OR (creation = %s AND name > %s)) "
        "ORDER BY creation,name LIMIT 100",
        (
            "TAC Company Evidence - %",
            cursor["creation"],
            cursor["creation"],
            cursor["name"],
        ),
        as_dict=True,
    )
    events = []
    for row in rows:
        if row.get("data"):
            payload = json.loads(row["data"])
            event_company = (
                payload.get("linked_company")
                if payload.get("document", {}).get("doctype") in {"Issue", "Address"}
                else payload.get("document", {}).get("company")
            )
            if event_company != COMPANY_NAME:
                raise ValueError(
                    "Owned native evidence request log crossed its company boundary"
                )
            events.append(payload)
    next_cursor = (
        {"creation": str(rows[-1]["creation"]), "name": rows[-1]["name"]}
        if rows
        else cursor
    )
    return {"events": events, "cursor": next_cursor}
