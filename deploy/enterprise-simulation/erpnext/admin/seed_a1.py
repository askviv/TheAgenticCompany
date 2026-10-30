"""Seed a reproducible fictional company and ERP-backed A1 procurement case."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import frappe
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from erpnext.setup.setup_wizard.setup_wizard import setup_complete


COMPANY = "The Agent Company"
COMPANY_ABBR = "TAC"
SUPPLIER = "Northstar Components Ltd"
CUSTOMER = "Northstar Industrial Systems Ltd"
ITEM = "COMP-001"
SHIPPING_SERVICE_ITEM = "EXP-SHIP-SVC"
WAREHOUSE = "Stores - TAC"
RUN_DATE = "2026-10-01"
SHIPPING_MARKER_FIELD = "custom_tac_approval_marker"
EMPLOYEE_IDENTITIES = Path("/opt/company-identities.json")
SANDBOX_EMPLOYEE_PASSWORD = "theagentcompany"


def ensure_shipping_marker_field() -> str:
    """Create/verify the unique native idempotency field on Purchase Order."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if not frappe.get_meta("Purchase Order").has_field("terms"):
        raise RuntimeError("Native Purchase Order terms field is unavailable")
    create_custom_fields({"Purchase Order": [{
        "fieldname": SHIPPING_MARKER_FIELD,
        "label": "Simulation Approval Marker",
        "fieldtype": "Data",
        "insert_after": "terms",
        "unique": 1,
        "hidden": 1,
        "no_copy": 1,
        "set_only_once": 1,
    }]}, update=True)
    custom_field = frappe.db.get_value(
        "Custom Field", {"dt": "Purchase Order", "fieldname": SHIPPING_MARKER_FIELD},
        ["name", "unique"], as_dict=True,
    )
    if not custom_field or not custom_field.unique:
        raise RuntimeError("Native Purchase Order approval marker is not unique")
    return SHIPPING_MARKER_FIELD


def ensure_utc_timezone() -> str:
    """Keep Frappe ledger wall times in UTC for the simulation clock contract."""
    settings = frappe.get_single("System Settings")
    updates = {"time_zone": "UTC"}
    # A new Frappe site has an uninitialized singleton. Provide its required
    # locale fields only when absent so the native Document save lifecycle can
    # validate the timezone update without overwriting an existing locale.
    for fieldname, default in (
        ("language", "en"),
        ("date_format", "yyyy-mm-dd"),
        ("time_format", "HH:mm:ss"),
        ("number_format", "#,###.##"),
    ):
        if not getattr(settings, fieldname):
            updates[fieldname] = default
    if any(getattr(settings, fieldname) != value for fieldname, value in updates.items()):
        for fieldname, value in updates.items():
            setattr(settings, fieldname, value)
        settings.save()
    frappe.clear_cache(doctype="System Settings")
    settings = frappe.get_single("System Settings")
    from frappe.utils.data import get_system_timezone

    timezone = get_system_timezone()
    if settings.time_zone != "UTC" or timezone != "UTC":
        raise RuntimeError("Frappe System Settings did not retain the UTC timezone")
    return timezone


def _insert_if_missing(doc):
    if frappe.db.exists(doc.doctype, doc.name):
        return frappe.get_doc(doc.doctype, doc.name)
    doc.insert(ignore_permissions=True)
    return doc


def _write_private(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise FileExistsError("Private runtime credential file already exists")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
    path.chmod(0o600)


def _update_private(path: Path, values: dict[str, str]) -> None:
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    current.update(values)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(current, output, sort_keys=True)
        output.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def _setup_company() -> None:
    companies = frappe.get_all("Company", pluck="name")
    if companies == [COMPANY]:
        return
    if companies:
        raise RuntimeError("Fresh TAC ERP site unexpectedly contains a different Company")
    args = frappe._dict({
        "company_name": COMPANY,
        "company_abbr": COMPANY_ABBR,
        "currency": "GBP",
        "country": "United Kingdom",
        "chart_of_accounts": "Standard",
        "domain": "Manufacturing",
        "fy_start_date": "2026-01-01",
        "fy_end_date": "2026-12-31",
    })
    setup_complete(args)
    companies = frappe.get_all("Company", pluck="name")
    if companies != [COMPANY]:
        raise RuntimeError("ERPNext setup wizard did not create exactly the TAC company")
    if frappe.defaults.get_global_default("company") != COMPANY:
        raise RuntimeError("ERPNext did not retain TAC as the default company")


def _ensure_role(name: str, description: str) -> None:
    if not frappe.db.exists("Role", name):
        frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1,
                        "is_custom": 1, "disabled": 0, "description": description}).insert(ignore_permissions=True)


def _grant(
    role: str,
    doctypes: tuple[str, ...],
    *,
    write: tuple[str, ...] = (),
    create: tuple[str, ...] = (),
    submit: tuple[str, ...] = (),
) -> None:
    from frappe.permissions import add_permission, update_permission_property

    for doctype in doctypes:
        if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}):
            add_permission(doctype, role, 0, "read")
        if doctype in write:
            update_permission_property(doctype, role, 0, "write", 1)
        if doctype in create:
            update_permission_property(doctype, role, 0, "create", 1)
        if doctype in submit:
            update_permission_property(doctype, role, 0, "submit", 1)


def _ensure_workflow() -> None:
    workflow_name = "Simulation Purchase Invoice Review"
    if frappe.db.exists("Workflow", workflow_name):
        return
    for state, style in (("Draft", "Info"), ("Review Requested", "Warning"),
                         ("Approved for Posting", "Success")):
        if not frappe.db.exists("Workflow State", state):
            frappe.get_doc({
                "doctype": "Workflow State", "workflow_state_name": state, "style": style,
            }).insert(ignore_permissions=True)
    for action in ("Request Review", "Approve for Posting"):
        if not frappe.db.exists("Workflow Action Master", action):
            frappe.get_doc({
                "doctype": "Workflow Action Master", "workflow_action_name": action,
            }).insert(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Workflow",
        "workflow_name": workflow_name,
        "document_type": "Purchase Invoice",
        "is_active": 1,
        "workflow_state_field": "workflow_state",
        "states": [
            {"state": "Draft", "doc_status": "0", "allow_edit": "Simulation ERP Reviewer"},
            {"state": "Review Requested", "doc_status": "0", "allow_edit": "Simulation ERP Reviewer"},
            {"state": "Approved for Posting", "doc_status": "0", "allow_edit": "Accounts Manager"},
        ],
        "transitions": [
            {"state": "Draft", "action": "Request Review", "next_state": "Review Requested",
             "allowed": "Simulation ERP Reviewer"},
            {"state": "Review Requested", "action": "Approve for Posting", "next_state": "Approved for Posting",
             "allowed": "Accounts Manager"},
        ],
    }).insert(ignore_permissions=True)


def _ensure_users(site: str, credentials_dir: Path) -> None:
    from frappe.core.doctype.user.user import generate_keys

    _ensure_role("Simulation ERP Agent", "Read-only benchmark access to ERP business records")
    _ensure_role("Simulation ERP Reviewer", "Can request invoice review through the configured workflow")
    doctypes = (
        "Purchase Order", "Purchase Receipt", "Purchase Invoice", "Supplier", "Item", "Warehouse",
        "Bin", "Payment Entry", "Payment Entry Reference", "Customer", "Sales Order", "Sales Invoice",
    )
    _grant("Simulation ERP Agent", doctypes)
    _grant("Simulation ERP Reviewer", doctypes + ("Account",), write=("Purchase Invoice",))

    agent_email = "theagentcompany@example.com"
    if not frappe.db.exists("User", agent_email):
        agent = frappe.get_doc({
            "doctype": "User", "email": agent_email, "first_name": "The Agent Company",
            "username": "theagentcompany", "enabled": 1, "user_type": "System User",
            "send_welcome_email": 0, "new_password": "theagentcompany",
            "roles": [{"role": "Simulation ERP Agent"}],
        })
        agent.flags.no_welcome_mail = True
        agent.insert(ignore_permissions=True)

    _ensure_company_employees()

    adapter_email = "erpnext-adapter@example.com"
    if not frappe.db.exists("User", adapter_email):
        adapter = frappe.get_doc({
            "doctype": "User", "email": adapter_email, "first_name": "ERPNext Simulation Adapter",
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0,
            "roles": [{"role": "Simulation ERP Reviewer"}],
        })
        adapter.flags.no_welcome_mail = True
        adapter.insert(ignore_permissions=True)
        keys = generate_keys(adapter_email)
        _write_private(credentials_dir / f"{site}.json", {
            "site_name": site,
            "user": adapter_email,
            "api_key": keys["api_key"],
            "api_secret": keys["api_secret"],
        })

    poster_role = "Simulation ERP Receipt Poster"
    _ensure_role(poster_role, "Trusted engine-only permission to create and submit approved purchase receipts")
    poster_doctypes = (
        "Purchase Order", "Purchase Receipt", "Supplier", "Item", "Warehouse", "Company", "Account",
        "Cost Center",
    )
    _grant(poster_role, poster_doctypes, write=("Purchase Receipt",),
           create=("Purchase Receipt",), submit=("Purchase Receipt",))
    poster_email = "erpnext-receipt-poster@example.com"
    if not frappe.db.exists("User", poster_email):
        poster = frappe.get_doc({
            "doctype": "User", "email": poster_email, "first_name": "ERPNext Approved Receipt Writer",
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0,
            "roles": [{"role": poster_role}],
        })
        poster.flags.no_welcome_mail = True
        poster.insert(ignore_permissions=True)
        keys = generate_keys(poster_email)
        _update_private(credentials_dir / f"{site}.json", {
            "receipt_api_key": keys["api_key"],
            "receipt_api_secret": keys["api_secret"],
        })

    shipping_role = "Simulation ERP Shipping Poster"
    _ensure_role(shipping_role, "Trusted engine-only permission to commit an approved shipping service order")
    _grant(shipping_role, ("Purchase Order", "Supplier", "Item", "Company", "Account", "Cost Center"),
           write=("Purchase Order",), create=("Purchase Order",), submit=("Purchase Order",))
    shipping_email = "erpnext-shipping-poster@example.com"
    if not frappe.db.exists("User", shipping_email):
        poster = frappe.get_doc({
            "doctype": "User", "email": shipping_email, "first_name": "ERPNext Approved Shipping Writer",
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0,
            "roles": [{"role": shipping_role}],
        })
        poster.flags.no_welcome_mail = True
        poster.insert(ignore_permissions=True)
        keys = generate_keys(shipping_email)
        _update_private(credentials_dir / f"{site}.json", {
            "shipping_api_key": keys["api_key"],
            "shipping_api_secret": keys["api_secret"],
        })


def _ensure_company_employees() -> None:
    from frappe.desk.page.setup_wizard.install_fixtures import update_genders

    update_genders()
    if not EMPLOYEE_IDENTITIES.is_file():
        raise FileNotFoundError("Canonical company employee directory is unavailable to ERP seed")
    records = json.loads(EMPLOYEE_IDENTITIES.read_text(encoding="utf-8"))
    employees = records.get("employees") if isinstance(records, dict) else None
    if not isinstance(employees, list) or not employees:
        raise RuntimeError("Canonical company employee directory has no employee records")

    for identity in employees:
        name = identity["displayName"]
        email = identity["email"]
        first_name, _, last_name = name.partition(" ")
        user_roles = identity["erpRoles"]
        for role_name in user_roles:
            if not frappe.db.exists("Role", role_name):
                raise RuntimeError(f"Required ERPNext employee role is unavailable: {role_name}")

        user = frappe.get_doc("User", email) if frappe.db.exists("User", email) else None
        if user is None:
            user = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": first_name,
                "last_name": last_name or None,
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "new_password": SANDBOX_EMPLOYEE_PASSWORD,
                "roles": [{"role": role_name} for role_name in user_roles],
            })
            user.flags.no_welcome_mail = True
            user.insert(ignore_permissions=True)
        else:
            if not user.enabled or user.user_type != "System User":
                raise RuntimeError(f"Canonical employee ERP user is not enabled as a system user: {email}")
            current_roles = {row.role for row in user.roles}
            for role_name in set(user_roles) - current_roles:
                user.append("roles", {"role": role_name})
            user.first_name = first_name
            user.last_name = last_name or None
            user.save(ignore_permissions=True)

        existing = frappe.db.get_value("Employee", {"user_id": email}, ["name", "company"], as_dict=True)
        if existing:
            if existing.company != COMPANY:
                raise RuntimeError(f"Canonical employee is linked to a different company: {email}")
            continue

        first, _, rest = name.partition(" ")
        employee = frappe.get_doc({
            "doctype": "Employee",
            "first_name": first,
            "last_name": rest or None,
            "company": COMPANY,
            "status": "Active",
            "gender": "Other",
            "date_of_birth": "1990-01-01",
            "date_of_joining": "2026-01-01",
            "user_id": email,
            "company_email": email,
        })
        employee.insert(ignore_permissions=True)


def _ensure_outgoing_email() -> None:
    """Use the existing central Mailu postmaster SMTP identity without sending mail."""
    credentials_path = Path("/run/secrets/mailu-service-credentials.json")
    if not credentials_path.is_file():
        raise RuntimeError("Central Mailu service credentials are unavailable to the trusted site setup")
    records = json.loads(credentials_path.read_text())
    account = records["accounts"]["postmaster"]["mail"]
    password = account["password"]
    address = account["address"]
    existing = frappe.db.get_value("Email Account", {"email_id": address}, "name")
    values = {
        "email_id": address,
        "service": "",
        "enable_outgoing": 1,
        "enable_incoming": 0,
        "default_outgoing": 1,
        "smtp_server": "mail.company.example.test",
        "smtp_port": 2587,
        "use_tls": 1,
        "use_ssl": 0,
        "no_smtp_authentication": 0,
        "login_id_is_different": 0,
        "password": password,
    }
    if existing:
        account = frappe.get_doc("Email Account", existing)
        account.update(values)
        account.save(ignore_permissions=True)
    else:
        frappe.get_doc({"doctype": "Email Account", **values}).insert(ignore_permissions=True)


def seed(site: str, runtime_dir: str) -> dict[str, str]:
    frappe.init(site, sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        ensure_shipping_marker_field()
        ensure_utc_timezone()
        _setup_company()
        if not frappe.db.exists("Supplier Group", "Raw Material Suppliers"):
            frappe.get_doc({
                "doctype": "Supplier Group", "supplier_group_name": "Raw Material Suppliers",
                "parent_supplier_group": "All Supplier Groups", "is_group": 0,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", SUPPLIER):
            frappe.get_doc({
                "doctype": "Supplier", "supplier_name": SUPPLIER,
                "supplier_group": "Raw Material Suppliers", "supplier_type": "Company",
                "country": "United Kingdom",
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Warehouse", WAREHOUSE):
            frappe.get_doc({
                "doctype": "Warehouse", "warehouse_name": WAREHOUSE,
                "company": COMPANY, "is_group": 0,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Item", ITEM):
            frappe.get_doc({
                "doctype": "Item", "item_code": ITEM, "item_name": "Precision Drive Component",
                "item_group": "Raw Material", "stock_uom": "Nos", "is_stock_item": 1,
                "include_item_in_manufacturing": 1,
                "item_defaults": [{"company": COMPANY, "default_warehouse": WAREHOUSE}],
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Item Group", "Simulation Services"):
            frappe.get_doc({
                "doctype": "Item Group", "item_group_name": "Simulation Services",
                "parent_item_group": "All Item Groups", "is_group": 0,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Item", SHIPPING_SERVICE_ITEM):
            frappe.get_doc({
                "doctype": "Item", "item_code": SHIPPING_SERVICE_ITEM,
                "item_name": "Expedited Freight Service", "item_group": "Simulation Services",
                "stock_uom": "Nos", "is_stock_item": 0,
            }).insert(ignore_permissions=True)

        if not frappe.db.exists("Customer Group", "Commercial"):
            frappe.get_doc({
                "doctype": "Customer Group", "customer_group_name": "Commercial",
                "parent_customer_group": "All Customer Groups", "is_group": 0,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Territory", "United Kingdom"):
            frappe.get_doc({
                "doctype": "Territory", "territory_name": "United Kingdom",
                "parent_territory": "All Territories", "is_group": 0,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Customer", CUSTOMER):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": CUSTOMER, "customer_type": "Company",
                "customer_group": "Commercial", "territory": "United Kingdom",
            }).insert(ignore_permissions=True)

        _ensure_role("Simulation ERP Agent", "Read-only benchmark access to ERP business records")
        _ensure_role("Simulation ERP Reviewer", "Can request invoice review through the configured workflow")
        _grant("Simulation ERP Agent", ("Purchase Order", "Purchase Receipt", "Purchase Invoice", "Supplier",
                                          "Item", "Warehouse", "Bin", "Payment Entry", "Payment Entry Reference",
                                          "Customer", "Sales Order", "Sales Invoice"))
        _grant("Simulation ERP Reviewer", ("Purchase Order", "Purchase Receipt", "Purchase Invoice", "Supplier",
                                             "Item", "Warehouse", "Bin", "Payment Entry", "Payment Entry Reference",
                                             "Account", "Customer", "Sales Order", "Sales Invoice"),
               write=("Purchase Invoice",))
        _ensure_workflow()

        purchase_orders = frappe.get_all(
            "Purchase Order", filters={"supplier": SUPPLIER, "company": COMPANY,
                                         "transaction_date": RUN_DATE, "docstatus": 1}, fields=["name"])
        matching_orders = []
        for order in purchase_orders:
            candidate = frappe.get_doc("Purchase Order", order.name)
            matching_lines = [row for row in candidate.items
                              if row.item_code == ITEM and float(row.qty) == 10000 and float(row.rate) == 50]
            if len(matching_lines) == 1:
                matching_orders.append((candidate, matching_lines[0]))
        if len(matching_orders) > 1:
            raise RuntimeError("More than one submitted A1 purchase order exists on this run site")
        if matching_orders:
            po, po_line = matching_orders[0]
        else:
            po = frappe.get_doc({
                "doctype": "Purchase Order", "supplier": SUPPLIER, "company": COMPANY,
                "transaction_date": RUN_DATE, "schedule_date": "2026-10-06", "currency": "GBP",
                "items": [{"item_code": ITEM, "qty": 10000, "rate": 50, "schedule_date": "2026-10-06",
                           "warehouse": WAREHOUSE}],
            })
            po.insert(ignore_permissions=True)
            po.submit()
            po_line = po.items[0]

        receipt_rows = frappe.get_all(
            "Purchase Receipt Item",
            filters={"purchase_order": po.name, "purchase_order_item": po_line.name},
            fields=["parent", "received_qty", "qty"], order_by="creation asc")
        receipts = []
        for row in receipt_rows:
            candidate = frappe.get_doc("Purchase Receipt", row.parent)
            if candidate.docstatus == 1:
                receipts.append(candidate)
        if len(receipts) > 1:
            raise RuntimeError("A1 baseline contains multiple submitted receipts; exact run recovery is required")
        receipt = receipts[0] if receipts else None
        received_total = sum(float(row.received_qty) for receipt in receipts for row in receipt.items
                             if row.purchase_order_item == po_line.name)
        if received_total > 8000:
            raise RuntimeError("A1 submitted receipt quantity exceeds the seeded baseline")
        if received_total < 8000:
            receipt = make_purchase_receipt(po.name)
            receipt.posting_date = "2026-10-06"
            receipt.set_posting_time = 1
            receipt.items[0].received_qty = 8000 - received_total
            receipt.items[0].qty = 8000 - received_total
            receipt.insert(ignore_permissions=True)
            receipt.submit()

        existing_invoice = frappe.db.get_value(
            "Purchase Invoice", {"supplier": SUPPLIER, "bill_no": "SUP-NPC-A1-001"}, "name")
        if existing_invoice:
            invoice = frappe.get_doc("Purchase Invoice", existing_invoice)
        else:
            invoice = make_purchase_invoice(po.name)
            invoice.bill_no = "SUP-NPC-A1-001"
            invoice.bill_date = "2026-10-05"
            invoice.posting_date = "2026-10-06"
            invoice.set_posting_time = 1
            invoice.items[0].qty = 10000
            invoice.workflow_state = "Draft"
            invoice.insert(ignore_permissions=True)

        sales_orders = frappe.get_all(
            "Sales Order", filters={"customer": CUSTOMER, "company": COMPANY,
                                     "transaction_date": RUN_DATE, "docstatus": 1}, fields=["name"])
        matching_sales_orders = []
        for order in sales_orders:
            candidate = frappe.get_doc("Sales Order", order.name)
            if any(row.item_code == ITEM and float(row.qty) == 100 and float(row.rate) == 80
                   for row in candidate.items):
                matching_sales_orders.append(candidate)
        if len(matching_sales_orders) > 1:
            raise RuntimeError("More than one submitted E1 sales order exists on this run site")
        if matching_sales_orders:
            sales_order = matching_sales_orders[0]
        else:
            sales_order = frappe.get_doc({
                "doctype": "Sales Order", "customer": CUSTOMER, "company": COMPANY,
                "transaction_date": RUN_DATE, "delivery_date": "2026-10-07", "currency": "GBP",
                "selling_price_list": "Standard Selling",
                "items": [{"item_code": ITEM, "qty": 100, "rate": 80,
                           "delivery_date": "2026-10-07", "warehouse": WAREHOUSE}],
            })
            sales_order.insert(ignore_permissions=True)
            sales_order.submit()

        _ensure_outgoing_email()
        _ensure_users(site, Path(runtime_dir) / "api-credentials")
        frappe.db.commit()
        return {
            "site_name": site,
            "company_id": COMPANY,
            "supplier_id": SUPPLIER,
            "item_id": ITEM,
            "warehouse_id": WAREHOUSE,
            "purchase_order_id": po.name,
            "purchase_order_item_id": po.items[0].name,
            "purchase_receipt_id": receipt.name,
            "purchase_invoice_id": invoice.name,
            "customer_id": CUSTOMER,
            "sales_order_id": sales_order.name,
            "purchase_order_qty": "10000",
            "received_qty": "8000",
            "billed_qty": "10000",
            "expected_unreceived_billed_qty": "2000",
        }
    finally:
        frappe.destroy()
