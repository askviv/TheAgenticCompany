"""Assign benchmark finance work and queue the Viv handoff."""

import hashlib
import traceback
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import frappe
from frappe.desk.form import assign_to


def simulation_message_footer():
    try:
        rate = int(Path("/sys/kernel/virtual_time_rate").read_text(encoding="ascii"))
    except FileNotFoundError:
        rate = 1
    if rate < 1:
        raise ValueError("Simulation clock rate must be positive")
    simulated_time = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return (
        f"This is a simulated environment. The clock runs at {rate}× real time. "
        f"The current simulated time is {simulated_time}."
    )


def delegate_finance_task(doctype, name, employee, recipient, description, *, source_event_id=None):
    task = f"{doctype}:{name}"
    if source_event_id is not None:
        task += f":{source_event_id}"
    identity = hashlib.sha256(task.encode()).hexdigest()
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
    instruction = f"Hi Viv, {description}"
    footer = simulation_message_footer()
    queue = frappe.sendmail(
        recipients=[recipient], subject=f"{employee_name} delegated {doctype} {name}",
        message=f"{escape(instruction)}<br><br>{escape(employee_name)}<br><br><small>{escape(footer)}</small>",
        sender=employee, reply_to=employee, reference_doctype=doctype, reference_name=name,
        message_id=message_id, add_unsubscribe_link=0,
    )
    if not queue:
        raise RuntimeError("ERPNext did not queue the Viv handoff")


def send_pending_finance_handoffs():
    queues = frappe.get_all("Email Queue", filters={
        "message_id": ["like", "benchmark-finance-%@theagentcompany.local"],
        "reference_doctype": ["in", ["Sales Order", "Sales Invoice", "Purchase Invoice", "Loan Application"]],
        "status": ["in", ["Not Sent", "Partially Sent", "Error"]],
    }, pluck="name", order_by="creation asc")
    return send_finance_queues(queues)


def send_finance_queues(queues):
    errors = []
    for name in queues:
        try:
            queue = frappe.get_doc("Email Queue", name)
            if queue.status != "Error":
                queue.send()
                queue.reload()
            if queue.status != "Sent":
                raise RuntimeError("Finance email was not sent")
        except Exception as error:
            causes = []
            cause = error.__cause__ or error.__context__
            while cause is not None:
                causes.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            errors.append({
                "issue_key": name,
                "error_type": type(error).__name__,
                "causes": causes,
                "stack": [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)],
            })
        finally:
            # Preserve Frappe's delivery status and retry counter on failure.
            frappe.db.commit()
    return errors
