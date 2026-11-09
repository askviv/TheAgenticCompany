"""Assign benchmark finance work and queue the Viv handoff."""

import hashlib
from html import escape

import frappe
from frappe.desk.form import assign_to


def delegate_finance_task(doctype, name, employee, recipient, description):
    # Keep the previous settlement identity so a retry cannot send a second email.
    key = name if doctype == "Bank Transaction" else f"{doctype}:{name}"
    identity = hashlib.sha256(key.encode()).hexdigest()
    message_id = f"benchmark-finance-{identity}@theagentcompany.local"
    if frappe.db.exists("Email Queue", {"message_id": message_id}):
        return
    if not frappe.db.get_value("User", employee, "enabled"):
        raise RuntimeError("The configured finance reviewer has no active ERPNext account")
    if not frappe.has_permission(doctype, ptype="read", doc=name, user=employee):
        raise RuntimeError("The finance reviewer cannot access the ERPNext record")
    assign_to.add({
        "doctype": doctype, "name": name, "assign_to": [employee],
        "description": "Delegated to Viv for action. Track the response in your mailbox.",
    })

    employee_name = frappe.db.get_value("User", employee, "full_name")
    instruction = (
        f"Hi Viv, I’ve been assigned a new task in ERPNext for {doctype} {name}. "
        f"Can you take care of it? {description}"
    )
    queue = frappe.sendmail(
        recipients=[recipient], subject=f"{employee_name} delegated {doctype} {name}",
        message=f"{escape(instruction)}<br><br>{escape(employee_name)}",
        reply_to=employee, reference_doctype=doctype, reference_name=name,
        message_id=message_id, add_unsubscribe_link=0,
    )
    if not queue:
        raise RuntimeError("ERPNext did not queue the Viv handoff")


def send_pending_finance_handoffs():
    queues = frappe.get_all("Email Queue", filters={
        "message_id": ["like", "benchmark-finance-%@theagentcompany.local"],
        "status": ["in", ["Not Sent", "Partially Sent"]],
    }, pluck="name", order_by="creation asc")
    for name in queues:
        frappe.get_doc("Email Queue", name).send()
        frappe.db.commit()
