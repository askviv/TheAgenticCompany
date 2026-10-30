"""Shared native Frappe identity and idempotency contract for imported source records.

Source snapshots are immutable provenance on native business documents. They are
not an outcome store: evaluators must read the native fields and linked documents.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe


IDENTITY_FIELDS = (
    "custom_source_system",
    "custom_source_id",
    "custom_source_key",
    "custom_task_id",
    "custom_experiment_id",
    "custom_company_id",
)


def source_key(
    source_system: str,
    source_id: str,
    record_type: str,
    task_id: str,
    experiment_id: str,
    company_id: str,
) -> str:
    parts = (source_system, source_id, record_type, task_id, experiment_id, company_id)
    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("Imported native source identity fields must be non-empty strings")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"SRC-{digest}"


def source_fields(
    *,
    source_system: str,
    source_id: str,
    record_type: str,
    task_id: str,
    experiment_id: str,
    company_id: str,
    snapshot: dict[str, Any],
) -> dict[str, str]:
    return {
        "custom_source_system": source_system,
        "custom_source_id": source_id,
        "custom_source_key": source_key(
            source_system, source_id, record_type, task_id, experiment_id, company_id,
        ),
        "custom_task_id": task_id,
        "custom_experiment_id": experiment_id,
        "custom_company_id": company_id,
        "custom_source_snapshot": json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
    }


def ensure_source_fields(doctype: str, insert_after: str) -> None:
    """Add the same hidden identity fields to any native ERPNext DocType."""
    if not frappe.get_meta(doctype).has_field(insert_after):
        raise RuntimeError(f"Native {doctype} source-field insertion anchor is unavailable")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields({doctype: [
        {"fieldname": "custom_source_system", "label": "Source System", "fieldtype": "Data",
         "insert_after": insert_after, "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_source_id", "label": "Source Record ID", "fieldtype": "Data",
         "insert_after": "custom_source_system", "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_source_key", "label": "Source Record Key", "fieldtype": "Data",
         "insert_after": "custom_source_id", "read_only": 1, "hidden": 1, "no_copy": 1, "unique": 1},
        {"fieldname": "custom_task_id", "label": "Source Task ID", "fieldtype": "Data",
         "insert_after": "custom_source_key", "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_experiment_id", "label": "Source Experiment ID", "fieldtype": "Data",
         "insert_after": "custom_task_id", "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_company_id", "label": "Source Company ID", "fieldtype": "Data",
         "insert_after": "custom_experiment_id", "read_only": 1, "hidden": 1, "no_copy": 1},
        {"fieldname": "custom_source_snapshot", "label": "Source Facts Snapshot", "fieldtype": "Long Text",
         "insert_after": "custom_company_id", "read_only": 1, "hidden": 1, "no_copy": 1},
    ]}, update=True)
    for fieldname in IDENTITY_FIELDS + ("custom_source_snapshot",):
        if not frappe.get_meta(doctype).has_field(fieldname):
            raise RuntimeError(f"Native {doctype}.{fieldname} source field was not installed")


def get_source_document(
    doctype: str,
    *,
    source_system: str,
    source_id: str,
    record_type: str,
    task_id: str,
    experiment_id: str,
    company_id: str,
):
    key = source_key(source_system, source_id, record_type, task_id, experiment_id, company_id)
    name = frappe.db.get_value(doctype, {"custom_source_key": key}, "name")
    if not name:
        return None
    doc = frappe.get_doc(doctype, name)
    expected = {
        "custom_source_system": source_system,
        "custom_source_id": source_id,
        "custom_source_key": key,
        "custom_task_id": task_id,
        "custom_experiment_id": experiment_id,
        "custom_company_id": company_id,
    }
    if any(getattr(doc, fieldname, None) != value for fieldname, value in expected.items()):
        raise RuntimeError(f"Existing native {doctype} has a conflicting imported-source identity")
    return doc


def insert_source_document(
    doctype: str,
    values: dict[str, Any],
    *,
    source_system: str,
    source_id: str,
    record_type: str,
    task_id: str,
    experiment_id: str,
    company_id: str,
    snapshot: dict[str, Any],
):
    """Insert once; return an existing document without restoring over agent edits."""
    existing = get_source_document(
        doctype, source_system=source_system, source_id=source_id, record_type=record_type,
        task_id=task_id, experiment_id=experiment_id, company_id=company_id,
    )
    if existing:
        return existing, False
    doc = frappe.get_doc({
        "doctype": doctype,
        **values,
        **source_fields(
            source_system=source_system, source_id=source_id, record_type=record_type,
            task_id=task_id, experiment_id=experiment_id, company_id=company_id,
            snapshot=snapshot,
        ),
    })
    doc.insert(ignore_permissions=True)
    return doc, True
