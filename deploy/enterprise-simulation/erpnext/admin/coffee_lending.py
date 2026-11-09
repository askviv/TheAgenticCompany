"""Simulate underwriting for customer financing requests submitted in Lending."""

import json
from decimal import Decimal

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from finance_handoffs import delegate_finance_task


PRODUCTS = (
    ("COFFEE-PRIME", "Coffee Order Financing · Prime", 10000, 8),
    ("COFFEE-NEAR-PRIME", "Coffee Order Financing · Near prime", 5000, 14),
    ("COFFEE-SUBPRIME", "Coffee Order Financing · Subprime", 2500, 22),
)
ORDER_FIELD = "custom_coffee_sales_order"


def ensure_products(company):
    if not frappe.get_meta("Loan Application").has_field(ORDER_FIELD):
        create_custom_fields({"Loan Application": [{
            "fieldname": ORDER_FIELD,
            "label": "Coffee Sales Order",
            "fieldtype": "Link",
            "options": "Sales Order",
            "insert_after": "loan_product",
        }]})
    offset = "Coffee Order Financing"
    if not frappe.db.exists("Loan Demand Offset Order", offset):
        frappe.get_doc({
            "doctype": "Loan Demand Offset Order", "title": offset,
            "components": [{"demand_type": value} for value in ("EMI (Principal + Interest)", "Penalty", "Charges")],
        }).insert()
    for code, title, limit, rate in PRODUCTS:
        if frappe.db.exists("Loan Product", {"product_code": code, "company": company}):
            continue
        frappe.get_doc({
            "doctype": "Loan Product", "product_code": code, "product_name": title,
            "company": company, "maximum_loan_amount": limit, "rate_of_interest": rate,
            "is_term_loan": 1, "repayment_schedule_type": "Monthly as per repayment start date",
            "collection_offset_sequence_for_standard_asset": offset,
            "collection_offset_sequence_for_sub_standard_asset": offset,
        }).insert()


def decide_applications(context):
    company = context["company"]
    filters = {"company": company, "docstatus": 1, "status": "Open",
               "applicant_type": "Customer", ORDER_FIELD: ["is", "set"]}
    product_codes = [product[0] for product in PRODUCTS]
    for row in frappe.get_all("Loan Application", filters=filters, pluck="name"):
        application = frappe.get_doc("Loan Application", row, for_update=True)
        if application.docstatus != 1 or application.status != "Open":
            continue
        order = frappe.get_doc("Sales Order", application.get(ORDER_FIELD))
        if (order.company != company or order.customer != application.applicant
                or order.custom_source_system != "coffeebench"
                or order.custom_company_id != context["company_id"]):
            continue
        request = json.loads(order.custom_source_snapshot)
        if request["kind"] != "offer" or not request["native_record"].get("financing_requested"):
            continue
        product = frappe.get_doc("Loan Product", application.loan_product)
        buyer = request["native_record"]["buyer_id"]
        payments = [
            json.loads(snapshot)["native_record"]
            for snapshot in frappe.get_all("Issue", filters={
                "custom_source_system": "coffeebench",
                "custom_company_id": context["company_id"],
            }, pluck="custom_source_snapshot")
        ]
        history = [payment for payment in payments if payment.get("cash_from_party_id") == buyer]
        late = any(payment["paid_day"] > payment["due_day"] for payment in history)
        tier = 2 if late else 0 if history else 1
        requested_amount = (Decimal(request["amount"]) * Decimal(str(order.conversion_rate))).quantize(Decimal("0.01"))
        amount = Decimal(str(application.loan_amount))
        reason = None
        if product.company != company or product.disabled or product.product_code not in product_codes:
            reason = "The selected product is unavailable for coffee order financing."
        elif product_codes.index(product.product_code) < tier:
            reason = "The customer does not qualify for the selected product based on payment history."
        elif not (0 < amount <= min(requested_amount, Decimal(str(product.maximum_loan_amount)))):
            reason = "The amount exceeds the order financing request or product limit."
        elif application.repayment_frequency != "Monthly" or application.repayment_periods != 12:
            reason = "Coffee order financing uses 12 monthly payments."
        application.status = "Rejected" if reason else "Approved"
        application.save()
        message = (
            f"Lending declined application {application.name} for order {order.name}. {reason}"
            if reason else
            f"Lending approved application {application.name} for order {order.name}. Can you continue with the order?"
        )
        delegate_finance_task(
            "Loan Application", application.name, context["finance_reviewer"], context["assistant_email"], message,
        )
