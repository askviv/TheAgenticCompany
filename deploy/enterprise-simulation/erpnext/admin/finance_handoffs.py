"""Assign benchmark finance work and queue the Viv reconciliation request."""

import hashlib
from html import escape

import frappe
from frappe.desk.form import assign_to


def assign_finance_review(doctype, name, employee, description):
    if not frappe.db.get_value("User", employee, "enabled"):
        raise RuntimeError("The configured finance reviewer has no active ERPNext account")
    if not frappe.has_permission(doctype, ptype="read", doc=name, user=employee):
        raise RuntimeError("The finance reviewer cannot access the ERPNext record")
    assign_to.add({
        "doctype": doctype, "name": name, "assign_to": [employee],
        "description": description,
    })


def queue_reconciliation(transaction, recipient, instruction):
    if transaction.status != "Unreconciled":
        return
    identity = hashlib.sha256(transaction.name.encode()).hexdigest()
    message_id = f"benchmark-finance-{identity}@theagentcompany.local"
    if frappe.db.exists("Email Queue", {"message_id": message_id}):
        return
    queue = frappe.sendmail(
        recipients=[recipient], subject=f"Reconcile bank transaction {transaction.name}",
        message=escape(instruction), reference_doctype=transaction.doctype,
        reference_name=transaction.name, message_id=message_id, add_unsubscribe_link=0,
    )
    if not queue:
        raise RuntimeError("ERPNext did not queue the Viv reconciliation request")


def send_pending_reconciliation_requests():
    queues = frappe.get_all("Email Queue", filters={
        "reference_doctype": "Bank Transaction",
        "message_id": ["like", "benchmark-finance-%@theagentcompany.local"],
        "status": ["in", ["Not Sent", "Partially Sent"]],
    }, pluck="name", order_by="creation asc")
    for name in queues:
        frappe.get_doc("Email Queue", name).send()
        frappe.db.commit()
