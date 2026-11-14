"""Trusted ZIP-pack boundary for native business documents on the existing site."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

import frappe

from source_records import ensure_source_fields, get_source_document, source_fields


# Uploads may describe business data, never site configuration or access control.
BUSINESS_DOCTYPES = frozenset({
    "Item", "Customer", "Supplier", "Contact", "Address", "Warehouse", "Account",
    "Loan Application", "Loan", "Loan Product", "Loan Disbursement", "Loan Repayment",
    "Bank Transaction", "Bank Account", "Timesheet", "HD Ticket", "CRM Lead",
    "Cost Center", "Project", "Task", "BOM", "Work Order", "Stock Entry",
    "Material Request", "Request for Quotation", "Supplier Quotation", "Quotation",
    "Sales Order", "Delivery Note", "Sales Invoice", "Purchase Order",
    "Purchase Receipt", "Purchase Invoice", "Payment Entry", "Journal Entry",
    "Payment Request", "Issue", "Opportunity", "Lead", "ToDo",
    "Salary Component", "Salary Structure", "Salary Structure Assignment",
    "Payroll Entry", "Salary Slip", "Additional Salary", "Expense Claim",
    "Leave Application", "Attendance", "Employee Checkin", "Employee Advance",
})
_SOURCE = "company-pack"


def _resolve(value: Any, records: dict[str, str], bindings: dict[str, str], anchor: datetime) -> Any:
    if isinstance(value, list):
        return [_resolve(item, records, bindings, anchor) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$ref"}:
        return records[value["$ref"]]
    if set(value) == {"$binding"}:
        return bindings[value["$binding"]]
    if set(value) == {"$date_offset"}:
        offset = value["$date_offset"]
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("Company pack date offset must be an integer")
        return (anchor + timedelta(days=offset)).date().isoformat()
    if any(str(key).startswith("$") for key in value):
        raise ValueError("Company pack contains an invalid reference")
    return {key: _resolve(item, records, bindings, anchor) for key, item in value.items()}


def _safe_fields(fields: dict[str, Any], doctype: str) -> None:
    meta = frappe.get_meta(doctype)
    for key, value in fields.items():
        field = meta.get_field(key)
        if not field or key.startswith("custom_"):
            raise ValueError(f"Company pack cannot write native field {doctype}.{key}")
        if field.fieldtype in {"Table", "Table MultiSelect"}:
            if not isinstance(value, list):
                raise ValueError("Company pack child table must contain rows")
            for row in value:
                if not isinstance(row, dict):
                    raise ValueError("Company pack child table row must be an object")
                _safe_fields(row, field.options)


def resolve_company_pack_directory(payload: dict[str, Any]) -> dict[str, str]:
    """Read native bindings using the mounted canonical identity directory."""
    from pathlib import Path

    company = payload["company_binding"]["erp_company"]
    if payload["company_binding"].get("company_id") != "company" or not frappe.db.exists("Company", company):
        raise ValueError("Company pack directory company binding is invalid")
    profile = json.loads(Path("/opt/company-identities.json").read_text())
    identities = {employee["id"]: employee for employee in profile["employees"]}
    department_names = {employee["department"] for employee in profile["employees"]}
    bindings = {"company": company}
    for employee_id in payload["employee_ids"]:
        identity = identities[employee_id]
        names = frappe.get_all("Employee", filters={"user_id": identity["email"], "company": company}, pluck="name", limit_page_length=2)
        if len(names) != 1:
            raise ValueError("Canonical native employee binding is unavailable or ambiguous")
        bindings["employee:" + employee_id] = names[0]
    for department_id, department_name in payload["departments"].items():
        if department_name not in department_names:
            raise ValueError("Canonical native department binding is unavailable")
        names = frappe.get_all("Department", filters={"department_name": department_name, "company": company}, pluck="name", limit_page_length=2)
        if len(names) != 1:
            raise ValueError("Canonical native department binding is unavailable or ambiguous")
        bindings["department:" + department_id] = names[0]
    return bindings


def import_company_pack(payload: dict[str, Any]) -> dict[str, Any]:
    """Insert/submit each baseline once; preserve all previously imported user work.

    Called only through the coordinator-owned, private UUID operation protocol.
    Schema installation commits before business writes; every business record is
    then committed atomically with its provenance and requested submission.
    """
    binding = payload["company_binding"]
    company = binding["erp_company"]
    if binding.get("company_id") != "company":
        raise ValueError("Company pack must target the existing company")
    if not frappe.db.exists("Company", company):
        raise ValueError("Company pack native company is unavailable")
    pack_id, version, digest = payload["pack_id"], payload["version"], payload["digest"]
    anchor = datetime.fromisoformat(payload["anchor_time"].replace("Z", "+00:00"))
    records = payload["records"]
    doctypes = {record["doctype"] for record in records}
    if not doctypes.issubset(BUSINESS_DOCTYPES):
        raise ValueError("Company pack contains a prohibited native document type")
    bindings = dict(payload["directory_bindings"])
    if bindings.pop("company", company) != company:
        raise ValueError("Company pack directory has a conflicting company binding")
    # These mappings originate in the canonical coordinator directory. Verify
    # their native tenant/User links here; uploaded User/Employee creation is forbidden.
    for token, name in bindings.items():
        if token.startswith("employee:"):
            employee = frappe.get_doc("Employee", name)
            if employee.company != company or not employee.user_id or not frappe.db.exists("User", employee.user_id):
                raise ValueError("Company pack employee binding is not in the existing company directory")
        elif token.startswith("department:"):
            if frappe.db.get_value("Department", name, "company") != company:
                raise ValueError("Company pack department binding is not in the existing company")
        else:
            raise ValueError("Company pack directory binding type is unsupported")
    bindings["company"] = company
    identity = {"source_system": _SOURCE, "task_id": pack_id,
                "experiment_id": str(version), "company_id": binding["company_id"]}
    for doctype in sorted(doctypes):
        meta = frappe.get_meta(doctype)
        anchor_field = next((field.fieldname for field in meta.fields
                             if field.fieldtype not in {"Section Break", "Column Break", "Tab Break"}), None)
        if not anchor_field:
            raise ValueError("Company pack native document has no source-field anchor")
        ensure_source_fields(doctype, anchor_field)
    frappe.db.commit()
    # A pack/version is immutable even when a retry omits its previously imported records.
    for doctype in sorted(BUSINESS_DOCTYPES):
        if not frappe.db.exists("DocType", doctype):
            continue
        if not frappe.get_meta(doctype).has_field("custom_source_key"):
            continue
        snapshots = frappe.get_all(doctype, filters={
            "custom_source_system": _SOURCE, "custom_task_id": pack_id,
            "custom_experiment_id": str(version), "custom_company_id": binding["company_id"],
        }, pluck="custom_source_snapshot")
        for snapshot in snapshots:
            facts = json.loads(snapshot)
            if facts.get("digest") != digest or facts.get("anchor_time") != payload["anchor_time"]:
                raise ValueError("Company pack version conflicts with its imported digest or anchor")
    names: dict[str, str] = {}
    created_count = reused_count = 0
    for record in records:
        doctype, record_id = record["doctype"], record["id"]
        if record_id in names or record["state"] not in {"draft", "submitted"}:
            raise ValueError("Company pack record identity or state is invalid")
        source = {**identity, "source_id": record_id, "record_type": doctype}
        try:
            existing = get_source_document(doctype, **source)
            if existing:
                # The initial insert and submit share a commit, so a reused draft
                # can only be intentionally retained/edited after that baseline.
                names[record_id] = existing.name
                reused_count += 1
            else:
                values = _resolve(record["fields"], names, bindings, anchor)
                _safe_fields(values, doctype)
                if "company" in values and values["company"] != company:
                    raise ValueError("Company pack document targets a different company")
                if frappe.get_meta(doctype).has_field("company"):
                    values["company"] = company
                snapshot = {"digest": digest, "anchor_time": payload["anchor_time"],
                            "record_id": record_id, "state": record["state"]}
                doc = frappe.get_doc({"doctype": doctype, **values,
                                      **source_fields(**source, snapshot=snapshot)})
                # Native permissions, mandatory fields, links and business hooks apply.
                doc.insert()
                if record["state"] == "submitted":
                    doc.submit()
                names[record_id] = doc.name
                created_count += 1
            frappe.db.commit()
        except Exception as error:
            try:
                frappe.db.rollback()
            except Exception as rollback_error:
                error.add_note(f"Company pack rollback failed: {type(rollback_error).__name__}")
            raise
    return {"records": names, "created_count": created_count, "reused_count": reused_count}
