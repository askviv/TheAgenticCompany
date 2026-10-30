"""Capture a stable, read-only snapshot of ERP evidence relevant to an evaluation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Any, Iterable

import frappe


COMPANY = "The Agent Company"
FINANCIAL_DOCTYPES = frozenset({
    "Purchase Order", "Purchase Receipt", "Purchase Invoice",
    "Sales Order", "Delivery Note", "Sales Invoice",
    "Payment Entry", "Journal Entry",
})
RELATED_DOCTYPES = frozenset({
    "Issue", "Address", "Customer", "Supplier", "Mode of Payment",
    "Item", "Item Price", "Item Attribute", "Price List",
})
CATALOG_DOCTYPES = frozenset({"Item", "Item Price", "Item Attribute", "Price List"})
GL_DOCTYPE = "GL Entry"
ALLOWED_DOCTYPES = FINANCIAL_DOCTYPES | RELATED_DOCTYPES | {GL_DOCTYPE}
_SENSITIVE_FIELD_PARTS = ("password", "secret", "token", "api_key", "private_key", "credential", "auth")
_USER_METADATA_FIELDS = frozenset({"owner", "modified_by"})


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        total_microseconds = ((value.days * 86_400 + value.seconds) * 1_000_000) + value.microseconds
        return {"__type__": "timedelta", "microseconds": total_microseconds}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key) not in _USER_METADATA_FIELDS
                and not str(key).startswith("_")
                and not any(part in str(key).lower() for part in _SENSITIVE_FIELD_PARTS)}
    if isinstance(value, (list, tuple)):
        rows = list(value)
        if rows and all(isinstance(item, dict) and isinstance(item.get("idx"), int) for item in rows):
            rows.sort(key=lambda item: (item["idx"], str(item.get("name", ""))))
        # Child row order remains native idx order; other arrays keep source order.
        return [_json_value(item) for item in rows]
    if hasattr(value, "as_dict"):
        return _json_value(value.as_dict())
    raise TypeError(f"Unsupported ERP snapshot value type: {type(value).__name__}")


def _doc_dict(doctype: str, name: str, company: str) -> dict[str, Any]:
    """Load a fresh native document and retain its child rows in native order."""
    document = frappe.get_doc(doctype, name)
    values = document.as_dict()
    if not isinstance(values, dict):
        raise TypeError(f"Frappe returned a non-object {doctype} document")
    result = _json_value(values)
    if not isinstance(result, dict):
        raise TypeError(f"Frappe returned an invalid {doctype} document")
    result["doctype"] = doctype
    result["name"] = name
    for fieldname, value in list(result.items()):
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            result[fieldname] = [
                row for row in value
                if not isinstance(row.get("company"), str) or row.get("company") == company
            ]
    return result


def _document_company(record: dict[str, Any]) -> str | None:
    value = record.get("company")
    return value if isinstance(value, str) else None


def _ref(doctype: Any, name: Any) -> tuple[str, str] | None:
    if not isinstance(doctype, str) or doctype not in ALLOWED_DOCTYPES:
        return None
    if not isinstance(name, str) or not name:
        return None
    return doctype, name


def _links_from_document(doctype: str, record: dict[str, Any]) -> Iterable[tuple[str, str]]:
    metadata = frappe.get_meta(doctype)
    for field in metadata.fields:
        fieldtype = getattr(field, "fieldtype", None)
        fieldname = getattr(field, "fieldname", None)
        if not isinstance(fieldname, str) or not fieldname:
            continue
        value = record.get(fieldname)
        if fieldtype == "Link":
            candidate = _ref(getattr(field, "options", None), value)
            if candidate:
                yield candidate
        elif fieldtype == "Dynamic Link":
            target_field = getattr(field, "options", None)
            candidate = _ref(record.get(target_field), value)
            if candidate:
                yield candidate

    # ERPNext financial child rows use dynamic links for references to source
    # invoices/orders. Preserve only explicitly allowlisted target doctypes.
    for value in record.values():
        if isinstance(value, list):
            for child in value:
                if not isinstance(child, dict):
                    continue
                child_doctype = child.get("reference_doctype") or child.get("reference_type") or child.get("voucher_type")
                child_name = child.get("reference_name") or child.get("voucher_no")
                if child.get("link_doctype") and child.get("link_name"):
                    child_doctype, child_name = child["link_doctype"], child["link_name"]
                candidate = _ref(child_doctype, child_name)
                if candidate:
                    yield candidate


def _reverse_linked_documents(target_doctype: str, target_name: str, company: str) -> Iterable[tuple[str, str]]:
    """Find allowed direct/child references pointing to one relevant financial doc."""
    for candidate_doctype in sorted(ALLOWED_DOCTYPES - {GL_DOCTYPE}):
        if not frappe.db.exists("DocType", candidate_doctype):
            continue
        metadata = frappe.get_meta(candidate_doctype)
        company_filters = {"company": company} if metadata.has_field("company") else {}
        for field in metadata.fields:
            fieldtype = getattr(field, "fieldtype", None)
            fieldname = getattr(field, "fieldname", None)
            if not isinstance(fieldname, str) or not fieldname:
                continue
            filters = None
            if fieldtype == "Link" and getattr(field, "options", None) == target_doctype:
                filters = {fieldname: target_name, **company_filters}
            elif fieldtype == "Dynamic Link" and getattr(field, "options", None):
                target_field = field.options
                filters = {target_field: target_doctype, fieldname: target_name, **company_filters}
            if filters is not None:
                rows = frappe.get_all(
                    candidate_doctype, filters=filters, fields=["name"], order_by="name asc",
                )
                for row in rows:
                    name = row.name if hasattr(row, "name") else row["name"]
                    yield candidate_doctype, name

        for table_field in metadata.fields:
            if getattr(table_field, "fieldtype", None) != "Table":
                continue
            child_doctype = getattr(table_field, "options", None)
            if not isinstance(child_doctype, str) or not frappe.db.exists("DocType", child_doctype):
                continue
            child_meta = frappe.get_meta(child_doctype)
            for child_field in child_meta.fields:
                fieldtype = getattr(child_field, "fieldtype", None)
                fieldname = getattr(child_field, "fieldname", None)
                if not isinstance(fieldname, str) or not fieldname:
                    continue
                filters = None
                if fieldtype == "Link" and getattr(child_field, "options", None) == target_doctype:
                    filters = {fieldname: target_name, "parenttype": candidate_doctype,
                               "parentfield": table_field.fieldname}
                elif fieldtype == "Dynamic Link" and getattr(child_field, "options", None):
                    filters = {child_field.options: target_doctype, fieldname: target_name,
                               "parenttype": candidate_doctype, "parentfield": table_field.fieldname}
                if filters is not None:
                    rows = frappe.get_all(
                        child_doctype, filters=filters, fields=["parent"], order_by="parent asc",
                    )
                    for row in rows:
                        parent = row.parent if hasattr(row, "parent") else row["parent"]
                        yield candidate_doctype, parent


def _validate_refs(entity_refs: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    if isinstance(entity_refs, (str, bytes, dict)):
        raise TypeError("evaluation evidence refs must be a sequence of {doctype,name} objects")
    references = set()
    for row in entity_refs:
        if not isinstance(row, dict):
            raise TypeError("evaluation evidence ref must be an object")
        reference = _ref(row.get("doctype"), row.get("name"))
        if reference is None:
            raise ValueError("evaluation evidence ref has an unsupported doctype or missing name")
        references.add(reference)
    if not references:
        raise ValueError("evaluation evidence requires at least one native document reference")
    return sorted(references)


def _begin_read_snapshot() -> None:
    active = frappe.db.sql("SELECT @@in_transaction", as_list=True)
    if active and active[0] and int(active[0][0]):
        raise RuntimeError("ERP evidence capture requires a fresh database transaction")
    frappe.db.sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    frappe.db.sql("START TRANSACTION READ ONLY, WITH CONSISTENT SNAPSHOT")


def _is_connected_to_financial_root(
    key: tuple[str, str], edges: set[tuple[tuple[str, str], tuple[str, str]]],
    financial_keys: set[tuple[str, str]],
) -> bool:
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for left, right in edges:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    seen = set()
    stack = [key]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in financial_keys:
            return True
        stack.extend(adjacency.get(current, ()))
    return False


def capture_company_evaluation_evidence(
    entity_refs: list[dict[str, str]], company: str = COMPANY
) -> dict[str, Any]:
    """Read linked company records in one repeatable-read transaction.

    The caller owns transaction rollback. This function intentionally neither
    commits nor rolls back, so a caller can bracket its evaluation operation
    with a matching snapshot comparison and then release the same read view.
    """
    if not isinstance(company, str) or not company:
        raise ValueError("evaluation evidence requires an explicit company name")
    roots = _validate_refs(entity_refs)
    _begin_read_snapshot()

    pending = list(roots)
    records: dict[tuple[str, str], dict[str, Any]] = {}
    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    while pending:
        doctype, name = pending.pop(0)
        key = (doctype, name)
        if key in records:
            continue
        if not frappe.db.exists("DocType", doctype):
            raise LookupError(f"evaluation evidence DocType is not installed: {doctype}")
        if not frappe.db.exists(doctype, name):
            raise LookupError(f"evaluation evidence document disappeared: {doctype}/{name}")
        record = _doc_dict(doctype, name, company)
        if doctype in FINANCIAL_DOCTYPES and _document_company(record) != company:
            raise PermissionError(f"evaluation evidence document is outside the requested company: {doctype}/{name}")
        related_company = record.get("company") or record.get("custom_customer_service_company")
        if doctype in RELATED_DOCTYPES and related_company and related_company != company:
            raise PermissionError(f"evaluation evidence document is outside the requested company: {doctype}/{name}")
        if doctype == GL_DOCTYPE and record.get("company") != company:
            raise PermissionError(f"GL Entry is outside the requested company: {name}")
        records[key] = record
        for linked in _links_from_document(doctype, record):
            edges.add((key, linked))
            if linked not in records:
                pending.append(linked)
        if doctype in FINANCIAL_DOCTYPES:
            for linked in _reverse_linked_documents(doctype, name, company):
                edges.add((key, linked))
                if linked not in records:
                    pending.append(linked)

    financial_keys = {key for key in records if key[0] in FINANCIAL_DOCTYPES}
    if not financial_keys:
        raise ValueError("evaluation evidence roots do not resolve to a company financial document")
    for root in roots:
        if root[0] in RELATED_DOCTYPES - CATALOG_DOCTYPES and not _is_connected_to_financial_root(
            root, edges, financial_keys
        ):
            raise PermissionError(f"related evaluation document is not linked to an in-scope financial document: {root[0]}/{root[1]}")

    relevant_vouchers = sorted((doctype, name) for doctype, name in records
                               if doctype in FINANCIAL_DOCTYPES)
    if relevant_vouchers:
        gl_names = set()
        for voucher_type, voucher_name in relevant_vouchers:
            gl_rows = frappe.get_all(
                GL_DOCTYPE,
                filters={"company": company, "voucher_type": voucher_type,
                         "voucher_no": voucher_name},
                fields=["name"], order_by="name asc",
            )
            gl_names.update(row.name if hasattr(row, "name") else row["name"] for row in gl_rows)
        for name in sorted(gl_names):
            key = (GL_DOCTYPE, name)
            if key not in records:
                gl = _doc_dict(*key, company)
                if gl.get("company") != company:
                    raise PermissionError(f"GL Entry is outside the requested company: {key[1]}")
                records[key] = gl

    by_doctype: dict[str, list[dict[str, Any]]] = {}
    for (doctype, _name), record in sorted(records.items()):
        by_doctype.setdefault(doctype, []).append(record)
    return {
        "schema_version": 1,
        "company": company,
        "scope": {
            "mode": "linked_native_documents",
            "roots": [{"doctype": doctype, "name": name} for doctype, name in roots],
            "document_count": len(records),
        },
        "documents": by_doctype,
    }


def evaluation_evidence_version_manifest(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic document hashes suitable for before/after guards."""
    documents = evidence.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("evaluation evidence has no document map")
    versions = []
    for doctype, records in sorted(documents.items()):
        if not isinstance(records, list):
            raise ValueError("evaluation evidence document group is invalid")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("name"), str):
                raise ValueError("evaluation evidence document is invalid")
            canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            versions.append({
                "doctype": doctype,
                "name": record["name"],
                "modified": record.get("modified"),
                "docstatus": record.get("docstatus"),
                "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            })
    canonical_versions = json.dumps(versions, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "company": evidence.get("company"),
        "scope": evidence.get("scope"),
        "documents": versions,
        "sha256": hashlib.sha256(canonical_versions.encode("utf-8")).hexdigest(),
    }


__all__ = ["capture_company_evaluation_evidence", "evaluation_evidence_version_manifest"]
