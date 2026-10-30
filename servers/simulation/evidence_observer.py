"""Translate native webhook metadata into immutable, private company evidence."""

from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
from .company_feedback import identity
from .feedback_contracts import CompanyEvidence, NativeDocumentEvent

_TAU_DATASET_OWNER = "4472c9ce-758f-5485-98f1-be6379f9f42b"


def native_document_evidence(
    event: NativeDocumentEvent, *, company_time=None, erp_public_url: str
) -> CompanyEvidence:
    doc = event.document
    scoped_issue = (
        doc.get("doctype") == "Issue"
        and doc.get("custom_customer_service_order")
        and event.linked_company == "The Agent Company"
    )
    scoped_address = (
        doc.get("doctype") == "Address"
        and event.linked_company == "The Agent Company"
        and any(
            isinstance(link, dict)
            and link.get("link_doctype") == "Customer"
            and isinstance(link.get("link_name"), str)
            for link in doc.get("links", [])
        )
    )
    if doc.get("company") != "The Agent Company" and not scoped_issue and not scoped_address:
        raise ValueError("Native evidence is outside the bound ERP company")
    doctype, name = doc["doctype"], doc["name"]
    modified = datetime.fromisoformat(doc["modified"])
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=ZoneInfo(event.erp_timezone))
    experiment_id = doc.get("custom_experiment_id")
    if experiment_id in ("background", "company", _TAU_DATASET_OWNER):
        experiment_id = None
    return CompanyEvidence(
        evidence_id=identity(doctype, name, event.event, doc["modified"]),
        erp_doctype=doctype,
        erp_name=name,
        event=event.event,
        actor=doc["modified_by"],
        occurred_at=modified,
        company_time=company_time,
        document=doc,
        experiment_id=experiment_id,
        evidence_refs=[
            erp_public_url.rstrip("/")
            + "/app/"
            + doctype.lower().replace(" ", "-")
            + "/"
            + quote(name, safe="")
        ],
    )
