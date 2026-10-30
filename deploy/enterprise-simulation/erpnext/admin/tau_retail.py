"""Project an authoritative tau2 retail database slice into native ERPNext docs.

The document records belong to the existing company site.  This adapter never
resets that site: each experiment/task/source tuple gets a unique native ERP
document key and repeated imports verify, rather than overwrite, existing rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import frappe
from source_records import ensure_source_fields, get_source_document, source_fields, source_key
from native_accounts import ensure_party_account, map_party_to_account


COMPANY = "The Agent Company"
SOURCE = "tau2:retail"
PRICE_LIST = "τ Retail USD"
COMPANY_ID = "company"
_DATASET_TASK = "retail-data"
_DATASET_EXPERIMENT = "4472c9ce-758f-5485-98f1-be6379f9f42b"
_ENTITIES = ("users", "products", "items", "orders")
_ORDER_STATUSES = {"processed", "pending", "pending (item modified)", "delivered", "cancelled",
                   "exchange requested", "return requested"}
_PAYMENT_METHODS = {"gift_card", "paypal", "credit_card"}
_SERVICE_STATES = {
    "pending": "Pending",
    "pending (item modified)": "Pending (Item Modified)",
    "processed": "Processed",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "return requested": "Return Requested",
    "exchange requested": "Exchange Requested",
}
_SOURCE_STATUS_BY_SERVICE_STATE = {value: key for key, value in _SERVICE_STATES.items()}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Retail manifest {field} must be a non-empty string")
    return value


def _native_item_code(value: str, source_id: str) -> str:
    readable_id = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id).strip("-_")[:48] or "ITEM"
    return f"TAU-{readable_id}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:10].upper()}"


def _projection_key(experiment_id: str, task_id: str, entity: str, source_id: str) -> str:
    scoped_task, scoped_experiment = _source_scope(task_id, experiment_id, entity)
    return source_key(SOURCE, source_id, entity, scoped_task, scoped_experiment, COMPANY_ID)


def _source_scope(task_id: str, experiment_id: str, entity: str) -> tuple[str, str]:
    """Share source business records across task runs in the existing company."""
    if entity == "service-request":
        return task_id, experiment_id
    return _DATASET_TASK, _DATASET_EXPERIMENT


def _add_projection_fields() -> None:
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    specs = {
        "Customer": ("email_id",),
        "Item": ("item_name",),
        "Item Group": ("item_group_name",),
        "Item Price": ("price_list_rate",),
        "Item Attribute": ("attribute_name",),
        "Sales Order": ("terms",),
        "Delivery Note": ("terms",),
        "Sales Invoice": ("remarks",),
        "Payment Entry": ("remarks",),
        "Mode of Payment": ("type",),
        "Issue": ("subject",),
        "Address": ("address_line1",),
    }
    for doctype, after_fields in specs.items():
        ensure_source_fields(doctype, after_fields[0])
    create_custom_fields({
        "Sales Order": [
            {"fieldname": "custom_tau_projection_as_of", "label": "τ Projection Date",
             "fieldtype": "Date", "insert_after": "custom_experiment_id", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_usd_to_company_rate", "label": "τ USD to Company Currency Rate",
             "fieldtype": "Float", "insert_after": "custom_tau_projection_as_of", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_exchange_against_order", "label": "τ Exchange Against Order",
             "fieldtype": "Link", "options": "Sales Order", "insert_after": "custom_tau_usd_to_company_rate"},
            {"fieldname": "custom_customer_service_state", "label": "Customer Service State",
             "fieldtype": "Data", "insert_after": "status", "read_only": 1, "allow_on_submit": 1},
            {"fieldname": "custom_customer_service_external_order_id", "label": "External Order ID",
             "fieldtype": "Data", "insert_after": "custom_customer_service_state", "read_only": 1,
             "in_global_search": 1, "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "custom_customer_service_cancel_reason", "label": "Cancellation Reason",
             "fieldtype": "Select", "options": "\nno longer needed\nordered by mistake",
             "insert_after": "custom_customer_service_external_order_id"},
            {"fieldname": "custom_tau_order_payment_method", "label": "Selected Payment Method",
             "fieldtype": "Link", "options": "Mode of Payment",
             "insert_after": "custom_customer_service_cancel_reason"},
        ],
        "Sales Invoice": [
            {"fieldname": "custom_tau_projection_as_of", "label": "τ Projection Date",
             "fieldtype": "Date", "insert_after": "custom_experiment_id", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_usd_to_company_rate", "label": "τ USD to Company Currency Rate",
             "fieldtype": "Float", "insert_after": "custom_tau_projection_as_of", "read_only": 1, "hidden": 1},
        ],
        "Payment Entry": [
            {"fieldname": "custom_tau_payment_method_id", "label": "τ Payment Method ID",
             "fieldtype": "Data", "insert_after": "custom_source_snapshot", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_transaction_type", "label": "τ Transaction Type",
             "fieldtype": "Select", "options": "payment\nrefund", "insert_after": "custom_tau_payment_method_id",
             "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_amount_usd", "label": "τ Amount USD",
             "fieldtype": "Currency", "options": "USD", "insert_after": "custom_tau_transaction_type",
             "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_projection_as_of", "label": "τ Projection Date",
             "fieldtype": "Date", "insert_after": "custom_tau_amount_usd", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_usd_to_company_rate", "label": "τ USD to Company Currency Rate",
             "fieldtype": "Float", "insert_after": "custom_tau_projection_as_of", "read_only": 1, "hidden": 1},
            {"fieldname": "custom_tau_payment_reference_policy", "label": "τ Payment Reference Policy",
             "fieldtype": "Data", "insert_after": "custom_tau_usd_to_company_rate", "read_only": 1, "hidden": 1},
        ],
        "Mode of Payment": [
            {"fieldname": "custom_tau_customer_id", "label": "Retail Customer",
             "fieldtype": "Link", "options": "Customer", "insert_after": "custom_source_snapshot"},
            {"fieldname": "custom_tau_payment_source", "label": "Retail Payment Source",
             "fieldtype": "Select", "options": "gift_card\npaypal\ncredit_card",
             "insert_after": "custom_tau_customer_id"},
            {"fieldname": "custom_tau_payment_brand", "label": "Retail Card Brand",
             "fieldtype": "Data", "insert_after": "custom_tau_payment_source"},
            {"fieldname": "custom_tau_payment_last_four", "label": "Retail Card Last Four",
             "fieldtype": "Data", "insert_after": "custom_tau_payment_brand"},
            {"fieldname": "custom_tau_gift_card_balance_usd", "label": "Gift Card Balance (USD)",
             "fieldtype": "Currency", "options": "USD", "insert_after": "custom_tau_payment_last_four"},
        ],
        "Issue": [
            {"fieldname": "custom_customer_service_type", "label": "Customer Service Request Type",
             "fieldtype": "Select", "options": "Return\nExchange", "insert_after": "custom_source_snapshot"},
            {"fieldname": "custom_customer_service_order", "label": "Customer Order",
             "fieldtype": "Link", "options": "Sales Order", "insert_after": "custom_customer_service_type"},
            {"fieldname": "custom_customer_service_item_ids", "label": "Requested Item IDs",
             "fieldtype": "Long Text", "insert_after": "custom_customer_service_order"},
            {"fieldname": "custom_customer_service_payment_method", "label": "Refund or Exchange Payment Method",
             "fieldtype": "Link", "options": "Mode of Payment", "insert_after": "custom_customer_service_item_ids"},
            {"fieldname": "custom_customer_service_new_item_ids", "label": "Requested Replacement Item IDs",
             "fieldtype": "Long Text", "insert_after": "custom_customer_service_payment_method"},
            {"fieldname": "custom_customer_service_price_difference", "label": "Exchange Price Difference (USD)",
             "fieldtype": "Currency", "options": "USD", "insert_after": "custom_customer_service_new_item_ids"},
        ],
        "Sales Order Item": [
            {"fieldname": "custom_source_item_id", "label": "Source Item ID",
             "fieldtype": "Data", "insert_after": "item_code", "read_only": 1, "in_list_view": 1,
             "no_copy": 1},
            {"fieldname": "custom_source_item_index", "label": "Source Order Item Index",
             "fieldtype": "Int", "insert_after": "custom_source_item_id", "read_only": 1, "hidden": 1},
        ],
        "Delivery Note Item": [
            {"fieldname": "custom_source_item_id", "label": "Source Item ID",
             "fieldtype": "Data", "insert_after": "item_code", "read_only": 1, "in_list_view": 1,
             "no_copy": 1},
            {"fieldname": "custom_source_item_index", "label": "Source Order Item Index",
             "fieldtype": "Int", "insert_after": "custom_source_item_id", "read_only": 1, "hidden": 1},
        ],
        "Item Attribute": [
            {"fieldname": "custom_tau_source_attribute_name", "label": "τ Source Attribute Name",
             "fieldtype": "Data", "insert_after": "attribute_name", "read_only": 1, "hidden": 1},
        ],
    }, update=True)


def _ensure_price_list() -> None:
    if frappe.db.exists("Price List", PRICE_LIST):
        price_list = frappe.get_doc("Price List", PRICE_LIST)
        if price_list.currency != "USD" or not price_list.selling or not price_list.enabled:
            raise RuntimeError("Existing τ Retail USD Price List does not match its native currency contract")
        return
    frappe.get_doc({
        "doctype": "Price List", "price_list_name": PRICE_LIST, "currency": "USD",
        "selling": 1, "buying": 0, "enabled": 1,
    }).insert(ignore_permissions=True)


def _ensure_retail_item_group() -> str:
    name = "τ Retail"
    if frappe.db.exists("Item Group", name):
        group = frappe.get_doc("Item Group", name)
        if group.is_group or group.parent_item_group != "All Item Groups":
            raise RuntimeError("Existing τ Retail item group does not match its ERPNext contract")
        return group.name
    group = frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
                            "parent_item_group": "All Item Groups", "is_group": 0})
    group.insert(ignore_permissions=True)
    return group.name


def _attribute_abbreviation(value: str) -> str:
    # ERPNext requires an abbreviation for every Item Attribute Value. Keep it
    # deterministic and unique; the source option itself remains the actual
    # attribute value shown to users and read by the evaluator.
    letters = re.sub(r"[^A-Za-z0-9]", "", value).upper()[:6] or "VALUE"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:4].upper()
    return f"{letters[:5]}{suffix}"[:10]


def _native_attribute_name(source_attribute: str) -> str:
    return f"Retail {source_attribute[:1].upper()}{source_attribute[1:]}"


def _ensure_item_attribute(attribute_name: str, source_attribute_name: str, values: set[str]) -> None:
    if not attribute_name or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("τ product attribute names and values must be non-empty strings")
    attribute_source_id = f"retail-attribute:{attribute_name}"
    attribute = get_source_document(
        "Item Attribute", source_system=SOURCE, source_id=attribute_source_id,
        record_type="item-attribute", task_id="shared-catalog", experiment_id="shared-catalog",
        company_id=COMPANY_ID,
    )
    if attribute:
        if (attribute.disabled or attribute.numeric_values
                or attribute.custom_tau_source_attribute_name != source_attribute_name):
            raise RuntimeError(f"Existing Item Attribute {attribute_name} cannot represent retail option values")
    else:
        if frappe.db.exists("Item Attribute", attribute_name):
            raise RuntimeError(f"Unowned native Item Attribute {attribute_name} conflicts with τ retail metadata")
        attribute = frappe.get_doc({"doctype": "Item Attribute", "attribute_name": attribute_name,
                                    "custom_tau_source_attribute_name": source_attribute_name,
                                    "numeric_values": 0, "item_attribute_values": [],
                                    **source_fields(
                                        source_system=SOURCE, source_id=attribute_source_id,
                                        record_type="item-attribute", task_id="shared-catalog",
                                        experiment_id="shared-catalog", company_id=COMPANY_ID,
                                        snapshot={"attribute_name": attribute_name},
                                    )})
    existing = {row.attribute_value for row in attribute.item_attribute_values}
    for value in sorted(values - existing):
        attribute.append("item_attribute_values", {
            "attribute_value": value,
            "abbr": _attribute_abbreviation(f"{attribute_name}\0{value}"),
        })
    if not existing and values or values - existing:
        attribute.save(ignore_permissions=True) if attribute.name else attribute.insert(ignore_permissions=True)


def _ensure_customer_service_workflow() -> None:
    """Use Frappe's workflow actions to enforce the retail service lifecycle."""
    role = "Simulation Retail Support"
    locked_edit_role = "System Manager"
    if not frappe.db.exists("Role", role):
        frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
    if not frappe.db.exists("Role", locked_edit_role):
        raise RuntimeError("Native System Manager role is unavailable for locked Sales Order workflow states")
    for state in _SERVICE_STATES.values():
        if not frappe.db.exists("Workflow State", state):
            frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state,
                            "style": "Warning" if "Requested" in state else "Info"}).insert(ignore_permissions=True)
    action_names = ("Process Order", "Modify Order Items", "Cancel Pending Order", "Deliver Order",
                    "Request Return", "Request Exchange")
    for action in action_names:
        if not frappe.db.exists("Workflow Action Master", action):
            frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
                ignore_permissions=True,
            )
    workflow_name = "Customer Service Order Lifecycle"
    transitions = [
        ("Pending", "Process Order", "Processed"),
        ("Pending", "Modify Order Items", "Pending (Item Modified)"),
        ("Pending", "Cancel Pending Order", "Cancelled"),
        ("Processed", "Deliver Order", "Delivered"),
        ("Delivered", "Request Return", "Return Requested"),
        ("Delivered", "Request Exchange", "Exchange Requested"),
    ]
    expected_transitions = {(state, action, following) for state, action, following in transitions}
    if frappe.db.exists("Workflow", workflow_name):
        workflow = frappe.get_doc("Workflow", workflow_name)
        actual = {(row.state, row.action, row.next_state) for row in workflow.transitions}
        actual_states = {row.state: (row.allow_edit or None, str(row.doc_status)) for row in workflow.states}
        expected_states = {state: (role if state == "Pending" else locked_edit_role, "1")
                           for state in _SERVICE_STATES.values()}
        if (workflow.document_type != "Sales Order" or not workflow.is_active
                or workflow.workflow_state_field != "custom_customer_service_state"
                or actual != expected_transitions or actual_states != expected_states):
            raise RuntimeError("Existing customer-service order workflow conflicts with the retail lifecycle")
        return
    frappe.get_doc({
        "doctype": "Workflow",
        "workflow_name": workflow_name,
        "document_type": "Sales Order",
        "is_active": 1,
        "workflow_state_field": "custom_customer_service_state",
        "states": [{"state": state, "doc_status": "1",
                    "allow_edit": role if state == "Pending" else locked_edit_role}
                   for state in _SERVICE_STATES.values()],
        "transitions": [{"state": state, "action": action, "next_state": following,
                         "allowed": role} for state, action, following in transitions],
    }).insert(ignore_permissions=True)


def _ensure_retail_user_access(operator_users: list[str]) -> None:
    """Add the scoped support role to explicitly configured users only."""
    if not isinstance(operator_users, list) or not operator_users:
        raise RuntimeError("Retail task import requires explicit retail_operator_users configuration")
    if len(operator_users) != len(set(operator_users)):
        raise ValueError("retail_operator_users must not contain duplicate User names")
    known_roles = {"Accounts Manager": "Accounts User", "Purchase User": "Sales User"}
    for email in operator_users:
        if not isinstance(email, str) or not email or not frappe.db.exists("User", email):
            raise RuntimeError("A configured retail operator ERPNext User does not exist")
        user = frappe.get_doc("User", email)
        current = {row.role for row in user.roles}
        roles = ["Simulation Retail Support", "Support Team"]
        for current_role, standard_role in known_roles.items():
            if current_role in current:
                roles.append(standard_role)
        if email == "theagentcompany@example.com":
            roles.append("Sales User")
        for role in roles:
            if not frappe.db.exists("Role", role):
                raise RuntimeError(f"Required native ERPNext role is unavailable: {role}")
            if role not in current:
                user.append("roles", {"role": role})
        user.save(ignore_permissions=True)
        company_permissions = frappe.get_all(
            "User Permission", filters={"user": email, "allow": "Company"}, fields=["name", "for_value"],
        )
        other_companies = [row.for_value for row in company_permissions if row.for_value != COMPANY]
        if other_companies:
            raise RuntimeError("Configured retail operator has a conflicting ERPNext company restriction")
        if not company_permissions:
            frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Company",
                            "for_value": COMPANY, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
        else:
            for permission_row in company_permissions:
                permission = frappe.get_doc("User Permission", permission_row.name)
                if not permission.apply_to_all_doctypes:
                    permission.apply_to_all_doctypes = 1
                    permission.save(ignore_permissions=True)


def ensure_retail_bank_account() -> dict[str, Any]:
    """Ensure native Bank account configuration without posting any balance.

    Preserve a configured company default. If there is no default, create and
    select a dedicated leaf account beneath the chart's existing Bank Accounts
    group. This operation creates no opening balance or accounting entry.
    """
    company = frappe.get_doc("Company", COMPANY)
    parent_name = frappe.db.get_value(
        "Account", {"company": COMPANY, "account_name": "Bank Accounts", "is_group": 1}, "name",
    )
    if not parent_name:
        raise RuntimeError(f"Company {COMPANY} has no native Bank Accounts parent group")
    parent = frappe.get_doc("Account", parent_name)
    if parent.root_type != "Asset" or parent.account_type != "Bank" or parent.is_group != 1:
        raise RuntimeError("The existing Bank Accounts parent does not match ERPNext's native account contract")

    dedicated_name = "Retail Payment Clearing"
    existing_name = frappe.db.get_value(
        "Account", {"company": COMPANY, "account_name": dedicated_name}, "name",
    )
    created = False
    if existing_name:
        account = frappe.get_doc("Account", existing_name)
        if (account.parent_account != parent_name or account.root_type != "Asset"
                or account.account_type != "Bank" or account.is_group != 0 or account.disabled):
            raise RuntimeError("Existing Retail Payment Clearing account conflicts with the native Bank mapping")
    else:
        account = frappe.get_doc({
            "doctype": "Account",
            "account_name": dedicated_name,
            "company": COMPANY,
            "parent_account": parent_name,
            "root_type": "Asset",
            "report_type": "Balance Sheet",
            "account_type": "Bank",
            "is_group": 0,
            "account_currency": company.default_currency,
        })
        account.insert(ignore_permissions=True)
        created = True

    configured_default = company.default_bank_account
    if configured_default:
        default_account = frappe.get_doc("Account", configured_default)
        if (default_account.company != COMPANY or default_account.root_type != "Asset"
                or default_account.account_type != "Bank" or default_account.is_group):
            raise RuntimeError("Configured company default bank account is not a native Bank leaf for this company")
        selected_name = configured_default
    else:
        company.default_bank_account = account.name
        company.save(ignore_permissions=True)
        selected_name = account.name
    return {"account": account.name, "company_default_account": selected_name,
            "created": created, "default_preserved": bool(configured_default)}


def ensure_retail_usd_receivable_account() -> dict[str, Any]:
    """Create the native USD receivable ledger used by USD retail invoices."""
    return ensure_party_account(
        COMPANY, "Customer", "USD", preferred_name="Retail USD Receivables",
    )


def _ensure_customer_usd_receivable_mapping(customer: Any, account_name: str, newly_created: bool) -> None:
    map_party_to_account(customer, account_name, company=COMPANY, newly_created=newly_created)


def ensure_retail_contract(operator_users: list[str]) -> dict[str, Any]:
    """Install shared schema, workflow, scoped roles and native USD accounting."""
    _add_projection_fields()
    _ensure_customer_service_workflow()
    _ensure_retail_user_access(operator_users)
    return {"bank": ensure_retail_bank_account(),
            "receivable": ensure_retail_usd_receivable_account()}


def _get_or_insert(doctype: str, projection_key: str, values: dict[str, Any]):
    existing_name = frappe.db.get_value(doctype, {"custom_source_key": projection_key}, "name")
    if existing_name:
        existing = frappe.get_doc(doctype, existing_name)
        for fieldname in ("custom_source_key", "custom_source_id", "custom_task_id", "custom_experiment_id",
                          "custom_company_id", "custom_source_system"):
            if getattr(existing, fieldname, None) != values[fieldname]:
                raise RuntimeError(f"Existing native {doctype} has a conflicting imported-source identity")
        # Existing documents may have been changed by the agent; imports must
        # never restore a benchmark snapshot over agent work.
        return existing, False
    doc = frappe.get_doc({"doctype": doctype, **values})
    doc.insert(ignore_permissions=True)
    return doc, True


def _existing_projection(doctype: str, experiment_id: str, task_id: str, entity: str, source_id: str):
    scoped_task, scoped_experiment = _source_scope(task_id, experiment_id, entity)
    return get_source_document(
        doctype, source_system=SOURCE, source_id=source_id, record_type=entity,
        task_id=scoped_task, experiment_id=scoped_experiment, company_id=COMPANY_ID,
    )


def _base_fields(experiment_id: str, task_id: str, entity: str, source_id: str, record: dict[str, Any]):
    scoped_task, scoped_experiment = _source_scope(task_id, experiment_id, entity)
    return source_fields(
        source_system=SOURCE, source_id=source_id, record_type=entity, task_id=scoped_task,
        experiment_id=scoped_experiment, company_id=COMPANY_ID, snapshot=record,
    )


def _validate_manifest(
    manifest: dict[str, Any], experiment_id: str,
) -> tuple[str, dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    _required_string(experiment_id, "experiment_id")
    task_id = _required_string(manifest.get("task_id"), "task_id")
    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict) or upstream.get("project") != "sierra-research/tau2-bench" \
            or upstream.get("domain") != "retail" \
            or upstream.get("commit") != "7aad81c37a3a78b4ed3e4866769f851400c64415":
        raise ValueError("Retail manifest is not an upstream tau2 retail task")
    if manifest.get("schema_version") != 1:
        raise ValueError("Retail manifest schema version is unsupported")
    integrity = manifest.get("source_integrity")
    if (not isinstance(integrity, dict) or integrity.get("status") not in ("valid", "failed")
            or not isinstance(integrity.get("issues"), list)):
        raise ValueError("Retail manifest source-integrity report is invalid")
    if (integrity["status"] == "valid" and integrity["issues"]
            or any(not isinstance(issue, dict) for issue in integrity["issues"])):
        raise ValueError("Retail manifest marks source integrity valid but includes unresolved issues")
    task = manifest.get("task")
    if not isinstance(task, dict) or task.get("id") != task_id or task.get("initial_state") is not None:
        raise ValueError("Retail manifest task identity or authoritative initial_state is invalid")
    source = manifest.get("source_database")
    if not isinstance(source, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
        raise ValueError("Retail manifest is missing the pinned source database digest")
    original = manifest.get("original_db")
    mapping = manifest.get("id_mapping")
    if not isinstance(original, dict) or not isinstance(mapping, dict):
        raise ValueError("Retail manifest is missing source records or identifier mapping")
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for entity in _ENTITIES:
        entity_records = original.get(entity, {})
        entity_mapping = mapping.get(entity, {})
        if not isinstance(entity_records, dict) or not isinstance(entity_mapping, dict):
            raise ValueError(f"Retail manifest {entity} must be an object")
        for source_id, record in entity_records.items():
            _required_string(source_id, f"original_db.{entity} key")
            if not isinstance(record, dict):
                raise ValueError(f"Retail manifest original_db.{entity}[{source_id}] must be an object")
            if entity_mapping.get(source_id) != f"tau2:retail:{entity[:-1]}:{source_id}":
                raise ValueError(f"Retail manifest {entity} identifier mapping is inconsistent")
            record_id_field = {"users": "user_id", "products": "product_id", "items": "item_id",
                               "orders": "order_id"}[entity]
            if record.get(record_id_field) != source_id:
                raise ValueError(f"Retail manifest original_db.{entity} record ID does not match its key")
        records[entity] = entity_records
    expected_variant_ids: set[str] = set()
    for product_id, product in records["products"].items():
        variants = product.get("variants")
        if not isinstance(variants, dict):
            raise ValueError(f"τ retail product {product_id} has no variant map")
        expected_variant_ids.update(variants)
    item_mapping = mapping.get("items", {})
    if (not isinstance(item_mapping, dict)
            or set(item_mapping) != expected_variant_ids
            or any(item_mapping[item_id] != f"tau2:retail:item:{item_id}" for item_id in expected_variant_ids)):
        raise ValueError("Retail manifest variant identifier mapping is inconsistent")
    return task_id, records, integrity["issues"]


def _customer_values(record: dict[str, Any]) -> tuple[str, str]:
    name = record.get("name")
    if not isinstance(name, dict):
        raise ValueError("τ retail user has no native name fields")
    full_name = " ".join(part for part in (name.get("first_name"), name.get("last_name")) if part)
    if not full_name:
        raise ValueError("τ retail user has no usable name")
    return full_name, _required_string(record.get("user_id"), "user.user_id")


def _address_values(address: dict[str, Any], title: str, is_primary: bool, email: str | None,
                    customer_id: str, experiment_id: str, task_id: str, entity: str,
                    source_id: str) -> dict[str, Any]:
    if not isinstance(address, dict):
        raise ValueError(f"τ retail {entity} has no address object")
    country = _required_string(address.get("country"), f"{entity}.address.country")
    if country == "USA":
        country = "United States"
    values = {
        "address_title": title,
        "address_type": "Billing" if is_primary else "Shipping",
        "address_line1": _required_string(address.get("address1"), f"{entity}.address.address1"),
        "address_line2": address.get("address2") or "",
        "city": _required_string(address.get("city"), f"{entity}.address.city"),
        "state": address.get("state") or "",
        "pincode": _required_string(address.get("zip"), f"{entity}.address.zip"),
        "country": country,
        "is_primary_address": int(is_primary),
        "is_shipping_address": 1,
        "links": [{"link_doctype": "Customer", "link_name": customer_id}],
        **({"email_id": _required_string(email, f"{entity}.email")} if email else {}),
        **_base_fields(experiment_id, task_id, entity, source_id, address),
    }
    return values


def _product_item_map(records: dict[str, dict[str, dict[str, Any]]]) -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for product_id, product in records["products"].items():
        variants = product.get("variants")
        if not isinstance(variants, dict):
            raise ValueError(f"τ retail product {product_id} has no variant map")
        for variant_id, variant in variants.items():
            if not isinstance(variant, dict) or variant.get("item_id") != variant_id:
                raise ValueError(f"τ retail product {product_id} has an invalid variant")
            if variant_id in result:
                raise ValueError(f"τ retail variant {variant_id} is not unique across products")
            result[variant_id] = (product_id, product, variant)
    for variant_id, item in records["items"].items():
        if variant_id not in result:
            raise ValueError(f"τ retail item {variant_id} has no source product variant")
        if item.get("item_id") != variant_id:
            raise ValueError(f"τ retail item {variant_id} has a mismatched source identifier")
    return result


def _seed_service_request(order: dict[str, Any], source_id: str, user: dict[str, Any],
                          order_doc: Any, experiment_id: str, task_id: str) -> str | None:
    status = order.get("status")
    if status not in ("exchange requested", "return requested"):
        return None
    issue_type = "Exchange" if status == "exchange requested" else "Return"
    existing = _existing_projection("Issue", experiment_id, task_id, "service-request", source_id)
    if existing:
        return existing.name
    source_items = order.get("exchange_items") if issue_type == "Exchange" else order.get("return_items")
    if not isinstance(source_items, list):
        source_items = []
    method_id = order.get("exchange_payment_method_id") if issue_type == "Exchange" else order.get("return_payment_method_id")
    method_name = None
    if method_id:
        method_name, _account = _mode_of_payment(
            method_id, user.get("payment_methods", {}),
            _existing_projection("Customer", experiment_id, task_id, "user", user["user_id"]).name,
            experiment_id, task_id,
        )
    replacement_items = order.get("exchange_new_items", []) if issue_type == "Exchange" else []
    title = f"τ retail {issue_type.lower()} request for {source_id}"
    values = {
        "subject": title,
        "customer": frappe.db.get_value("Sales Order", order_doc.name, "customer"),
        "company": COMPANY,
        "raised_by": user.get("email") or "",
        "status": "Open",
        "custom_customer_service_type": issue_type,
        "custom_customer_service_order": order_doc.name,
        "custom_customer_service_company": order_doc.company,
        "custom_customer_service_item_ids": json.dumps(source_items, ensure_ascii=False, sort_keys=True),
        "custom_customer_service_payment_method": method_name,
        "custom_customer_service_new_item_ids": json.dumps(replacement_items, ensure_ascii=False, sort_keys=True),
        "custom_customer_service_price_difference": order.get("exchange_price_difference") or 0,
        **_base_fields(experiment_id, task_id, "service-request", source_id, order),
    }
    return frappe.get_doc({"doctype": "Issue", **values}).insert(ignore_permissions=True).name


def _mode_of_payment(
    method_id: str, payment_methods: dict[str, Any], customer_id: str,
    experiment_id: str, task_id: str,
) -> tuple[str, str]:
    method = payment_methods.get(method_id)
    if not isinstance(method, dict):
        raise ValueError(f"τ payment method {method_id} is absent from the source customer")
    source = method.get("source")
    if source not in _PAYMENT_METHODS:
        raise ValueError(f"τ payment method {method_id} has an unsupported source type")
    balance = method.get("balance")
    if source == "gift_card" and (
        isinstance(balance, bool) or not isinstance(balance, (int, float)) or balance < 0
    ):
        raise ValueError(f"τ gift-card payment method {method_id} has no valid native balance")
    company_accounts = frappe.db.get_value(
        "Company", COMPANY, ["default_cash_account", "default_bank_account"], as_dict=True,
    )
    account_field = "default_cash_account" if source == "gift_card" else "default_bank_account"
    account = company_accounts.get(account_field) if company_accounts else None
    if not account:
        raise RuntimeError(f"Company {COMPANY} has no native {source} clearing account configured")
    projection_key = _projection_key(experiment_id, task_id, "payment-method", method_id)
    mode = _existing_projection("Mode of Payment", experiment_id, task_id, "payment-method", method_id)
    if mode:
        if (mode.type != ("Cash" if source == "gift_card" else "Bank")
                or not any(row.company == COMPANY and row.default_account == account for row in mode.accounts)
                or mode.custom_tau_customer_id != customer_id):
            raise RuntimeError("Existing τ Mode of Payment does not match its native account mapping")
        return mode.name, account
    customer_source_id = frappe.db.get_value("Customer", customer_id, "custom_source_id") or customer_id
    mode_suffix = hashlib.sha256(projection_key.encode("utf-8")).hexdigest()[:8].upper()
    mode_name = f"{source.replace('_', ' ').title()} {method_id} [{customer_source_id}] [{mode_suffix}]"
    mode = frappe.get_doc({
        "doctype": "Mode of Payment",
        "mode_of_payment": mode_name,
        "type": "Cash" if source == "gift_card" else "Bank",
        "enabled": 1,
        "accounts": [{"company": COMPANY, "default_account": account}],
        "custom_tau_customer_id": customer_id,
        "custom_tau_payment_source": source,
        "custom_tau_payment_brand": method.get("brand") or "",
        "custom_tau_payment_last_four": method.get("last_four") or "",
        "custom_tau_gift_card_balance_usd": balance if source == "gift_card" else None,
        **_base_fields(experiment_id, task_id, "payment-method", method_id, method),
    })
    mode.insert(ignore_permissions=True)
    return mode.name, account


def _submit_delivery_notes(
    order_doc: Any, order: dict[str, Any], source_id: str, experiment_id: str, task_id: str,
    projection_as_of: str,
) -> list[Any]:
    # ERPNext 16 keeps the native SO document mappers on sales_order.py.
    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

    deliveries = order.get("fulfillments", [])
    if not isinstance(deliveries, list):
        raise ValueError(f"τ retail order {source_id} has invalid fulfillment history")
    if not deliveries:
        return []
    source_rows: dict[str, list[Any]] = {}
    source_rows_by_name: dict[str, Any] = {}
    for row in order_doc.items:
        item = frappe.get_doc("Item", row.item_code)
        item_source_id = item.custom_source_id if item.custom_source_system == SOURCE else None
        if not item_source_id:
            raise ValueError(f"Native Sales Order {order_doc.name} contains an untracked variant item")
        source_rows.setdefault(item_source_id, []).append(row)
        source_rows_by_name[row.name] = row
    delivered_indexes: set[int] = set()
    documents = []
    for index, fulfillment in enumerate(deliveries):
        fulfillment_id = f"{source_id}:fulfillment:{index}"
        existing = _existing_projection("Delivery Note", experiment_id, task_id, "fulfillment", fulfillment_id)
        if existing:
            documents.append(existing)
            continue
        item_ids = fulfillment.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError(f"τ retail order {source_id} has a fulfillment without item IDs")
        if any(item_id not in source_rows for item_id in item_ids):
            raise ValueError(f"τ retail order {source_id} fulfillment references an item outside its native order")
        selected_rows = []
        for item_id in item_ids:
            match = next((row for row in source_rows[item_id]
                          if row.custom_source_item_index not in delivered_indexes), None)
            if not match:
                raise ValueError(f"τ retail order {source_id} fulfillment exceeds ordered quantity for {item_id}")
            delivered_indexes.add(match.custom_source_item_index)
            selected_rows.append(match)
        doc = make_delivery_note(order_doc.name)
        selected = {row.name for row in selected_rows}
        kept = []
        for row in doc.items:
            source_row = source_rows_by_name.get(row.so_detail)
            if source_row and source_row.name in selected:
                variant = frappe.get_doc("Item", source_row.item_code)
                row.custom_source_item_id = variant.custom_source_id
                row.custom_source_item_index = source_row.custom_source_item_index
                row.qty = 1
                kept.append(row)
        if len(kept) != len(selected):
            raise RuntimeError(f"Native Delivery Note mapping lost an item from τ fulfillment {source_id}:{index}")
        doc.set("items", kept)
        doc.posting_date = projection_as_of
        tracking_ids = fulfillment.get("tracking_id", [])
        if len(tracking_ids) > 1:
            raise ValueError(f"τ retail fulfillment {fulfillment_id} has multiple tracking IDs; ERPNext LR No is singular")
        doc.lr_no = tracking_ids[0] if tracking_ids else ""
        doc.update({
            **_base_fields(experiment_id, task_id, "fulfillment", fulfillment_id, fulfillment),
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        documents.append(doc)
    return documents


def _create_sales_invoice(
    order_doc: Any, order: dict[str, Any], source_id: str, experiment_id: str, task_id: str,
    projection_as_of: str, exchange_rate: float,
) -> Any:
    from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

    existing = _existing_projection("Sales Invoice", experiment_id, task_id, "sales-invoice", source_id)
    if existing:
        return existing
    invoice = make_sales_invoice(order_doc.name)
    if not invoice.items:
        raise RuntimeError(f"Native Sales Invoice builder returned no items for τ order {source_id}")
    invoice.posting_date = projection_as_of
    invoice.due_date = projection_as_of
    invoice.currency = "USD"
    invoice.conversion_rate = exchange_rate
    invoice.custom_tau_projection_as_of = projection_as_of
    invoice.custom_tau_usd_to_company_rate = exchange_rate
    invoice.update({
        **_base_fields(experiment_id, task_id, "sales-invoice", source_id, order),
    })
    invoice.insert(ignore_permissions=True)
    invoice.submit()
    return invoice


def _create_credit_note(
    invoice: Any, order: dict[str, Any], source_id: str, event_id: str,
    experiment_id: str, task_id: str, projection_as_of: str, exchange_rate: float,
) -> Any:
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

    credit_note = _existing_projection("Sales Invoice", experiment_id, task_id, "credit-note", event_id)
    if credit_note:
        return credit_note
    credit_note = make_sales_return(invoice.name)
    if not credit_note.items:
        raise RuntimeError(f"Native Sales Return builder returned no items for τ order {source_id}")
    credit_note.posting_date = projection_as_of
    credit_note.due_date = projection_as_of
    credit_note.currency = "USD"
    credit_note.conversion_rate = exchange_rate
    credit_note.custom_tau_projection_as_of = projection_as_of
    credit_note.custom_tau_usd_to_company_rate = exchange_rate
    credit_note.update({
        **_base_fields(experiment_id, task_id, "credit-note", event_id, order),
    })
    credit_note.insert(ignore_permissions=True)
    credit_note.submit()
    return credit_note


def _submit_payment(
    reference_doctype: str, reference_name: str, event: dict[str, Any], event_id: str,
    user: dict[str, Any], order_id: str, experiment_id: str, task_id: str,
    projection_as_of: str, exchange_rate: float, customer_id: str,
) -> Any:
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    existing = _existing_projection("Payment Entry", experiment_id, task_id, "payment", event_id)
    if existing:
        return existing
    method_id = _required_string(event.get("payment_method_id"), f"payment {event_id}.payment_method_id")
    mode_name, account = _mode_of_payment(
        method_id, user.get("payment_methods", {}), customer_id, experiment_id, task_id,
    )
    payment = get_payment_entry(reference_doctype, reference_name)
    payment.mode_of_payment = mode_name
    if payment.payment_type == "Receive":
        payment.paid_to = account
    elif payment.payment_type == "Pay":
        payment.paid_from = account
    else:
        raise RuntimeError(f"Native Payment Entry builder returned unsupported type for τ event {event_id}")
    payment.posting_date = projection_as_of
    # ERPNext requires a bank reference and date for native Bank accounts.
    # τ does not supply processor references or transaction dates, so these
    # fields identify the imported source event and declared posting-date policy.
    payment.reference_no = f"τ import {event_id}"
    payment.reference_date = projection_as_of
    payment.custom_tau_payment_method_id = method_id
    payment.custom_tau_transaction_type = event.get("transaction_type")
    payment.custom_tau_amount_usd = event.get("amount")
    payment.custom_tau_projection_as_of = projection_as_of
    payment.custom_tau_usd_to_company_rate = exchange_rate
    payment.custom_tau_payment_reference_policy = "source_event_id_and_projection_as_of"
    payment.update({
        **_base_fields(experiment_id, task_id, "payment", event_id, event),
    })
    payment.insert(ignore_permissions=True)
    payment.submit()
    return payment


def _seed_native_order_history(
    order_doc: Any, order: dict[str, Any], source_id: str, user: dict[str, Any],
    experiment_id: str, task_id: str, projection_as_of: str, exchange_rate: float,
) -> dict[str, int]:
    from decimal import Decimal

    counts = {"delivery_notes": 0, "sales_invoices": 0, "credit_notes": 0, "payment_entries": 0}
    source_items = order.get("items", [])
    order_amount = sum((Decimal(str(item.get("price"))) for item in source_items), Decimal("0"))
    events = order.get("payment_history", [])
    if not isinstance(events, list):
        raise ValueError(f"τ retail order {source_id} has invalid payment history")
    invoice = None
    if order.get("status") in ("processed", "delivered", "cancelled",
                                "return requested", "exchange requested"):
        delivery_notes = _submit_delivery_notes(
            order_doc, order, source_id, experiment_id, task_id, projection_as_of,
        )
        counts["delivery_notes"] += len(delivery_notes)
        invoice = _create_sales_invoice(
            order_doc, order, source_id, experiment_id, task_id, projection_as_of, exchange_rate,
        )
        counts["sales_invoices"] += 1

    for index, event in enumerate(events):
        transaction_type = event.get("transaction_type")
        if transaction_type not in ("payment", "refund"):
            raise ValueError(f"τ retail order {source_id} has an unsupported payment event")
        try:
            amount = Decimal(str(event.get("amount")))
        except Exception as error:
            raise ValueError(f"τ retail order {source_id} has an invalid payment amount") from error
        if amount != order_amount:
            raise ValueError(f"τ retail order {source_id} payment amount does not exactly match native source total")
        reference_doctype = "Sales Order"
        reference_name = order_doc.name
        if transaction_type == "refund":
            if not invoice:
                raise ValueError(f"τ retail order {source_id} refunds an order without a native invoice")
            credit_event_id = f"{source_id}:credit-note:{index}"
            credit_note = _create_credit_note(
                invoice, order, source_id, credit_event_id, experiment_id, task_id,
                projection_as_of, exchange_rate,
            )
            counts["credit_notes"] += int(
                credit_note.custom_source_key == _projection_key(
                    experiment_id, task_id, "credit-note", credit_event_id,
                )
            )
            reference_doctype = "Sales Invoice"
            reference_name = credit_note.name
        elif invoice:
            reference_doctype = "Sales Invoice"
            reference_name = invoice.name
        event_id = f"{source_id}:payment:{index}"
        existing = _existing_projection("Payment Entry", experiment_id, task_id, "payment", event_id)
        if not existing:
            _submit_payment(
                reference_doctype, reference_name, event, event_id, user, source_id,
                experiment_id, task_id, projection_as_of, exchange_rate,
                _existing_projection("Customer", experiment_id, task_id, "user", user["user_id"]).name,
            )
            counts["payment_entries"] += 1
    return counts


def _apply_source_workflow_state(order_doc: Any, source_status: str) -> Any:
    """Reach imported lifecycle state only through native ERPNext transitions."""
    from frappe.model.workflow import apply_workflow

    actions = {
        "pending": (),
        "pending (item modified)": ("Modify Order Items",),
        "processed": ("Process Order",),
        "delivered": ("Process Order", "Deliver Order"),
        "cancelled": ("Cancel Pending Order",),
        "return requested": ("Process Order", "Deliver Order", "Request Return"),
        "exchange requested": ("Process Order", "Deliver Order", "Request Exchange"),
    }
    if source_status not in actions:
        raise ValueError(f"τ retail status {source_status} has no native workflow path")
    for action in actions[source_status]:
        order_doc = apply_workflow(order_doc, action)
    if source_status == "cancelled" and order_doc.status != "Closed":
        order_doc.update_status("Closed")
        order_doc.reload()
    return order_doc


def seed_retail_task(
    manifest: dict[str, Any], experiment_id: str, projection_as_of: str,
) -> dict[str, Any]:
    """Create native ERPNext documents for one authoritative τ task slice.

    Source snapshots preserve provenance only; the evaluator bridge reads
    current native documents and never treats these snapshots as outcomes.
    """
    task_id, records, source_gaps = _validate_manifest(manifest, experiment_id)
    if frappe.utils.getdate(projection_as_of).isoformat() != projection_as_of:
        raise ValueError("projection_as_of must be YYYY-MM-DD")
    created = {entity: 0 for entity in _ENTITIES}
    created["addresses"] = 0
    created["service_requests"] = 0
    created["payment_methods"] = 0
    created["item_prices"] = 0
    created.update({
        "delivery_notes": 0, "sales_invoices": 0, "credit_notes": 0, "payment_entries": 0,
    })
    projection_gaps: list[dict[str, str]] = []
    identities: dict[tuple[str, str], str] = {}
    address_ids: dict[tuple[str, str], str] = {}
    try:
        from erpnext.setup.utils import get_exchange_rate

        exchange_date = projection_as_of
        exchange_rate = get_exchange_rate("USD", "GBP", exchange_date, args="for_selling")
        if not exchange_rate:
            raise RuntimeError(f"Native ERPNext USD/GBP selling exchange rate unavailable for {exchange_date}")
        receivable_account = frappe.db.get_value(
            "Account", {"company": COMPANY, "account_name": "Retail USD Receivables"}, "name",
        )
        if not receivable_account:
            raise RuntimeError("Native retail USD receivable account setup has not been completed")
        receivable_doc = frappe.get_doc("Account", receivable_account)
        if receivable_doc.account_currency != "USD" or receivable_doc.account_type != "Receivable":
            raise RuntimeError("Native retail receivable account is not a USD Receivable leaf")
        _ensure_price_list()
        for source_id, user in records["users"].items():
            customer_name, exact_id = _customer_values(user)
            values = {
                "customer_name": customer_name,
                "customer_type": "Individual",
                "customer_group": "Individual",
                "territory": "United Kingdom",
                "default_currency": "USD",
                **({"email_id": user["email"]} if user.get("email") else {}),
                **_base_fields(experiment_id, task_id, "user", source_id, user),
            }
            doc, inserted = _get_or_insert("Customer", _projection_key(experiment_id, task_id, "user", source_id), values)
            _ensure_customer_usd_receivable_mapping(doc, receivable_account, inserted)
            identities[("user", source_id)] = doc.name
            created["users"] += int(inserted)
            for method_id in user.get("payment_methods", {}):
                method_exists = _existing_projection(
                    "Mode of Payment", experiment_id, task_id, "payment-method", method_id,
                ) is not None
                _mode_of_payment(method_id, user["payment_methods"], doc.name, experiment_id, task_id)
                identities[("payment_method", method_id)] = _existing_projection(
                    "Mode of Payment", experiment_id, task_id, "payment-method", method_id,
                ).name
                method_inserted = not method_exists
                created["payment_methods"] += int(method_inserted)
            address_values = _address_values(
                user.get("address"), customer_name, True, user.get("email"), doc.name,
                experiment_id, task_id, "user-address", source_id,
            )
            address_doc, address_inserted = _get_or_insert(
                "Address", _projection_key(experiment_id, task_id, "user-address", source_id), address_values,
            )
            address_ids[("user", source_id)] = address_doc.name
            if inserted and not getattr(doc, "customer_primary_address", None):
                frappe.db.set_value("Customer", doc.name, "customer_primary_address", address_doc.name)
            created["addresses"] += int(address_inserted)

        variant_map = _product_item_map(records)
        retail_item_group = _ensure_retail_item_group() if variant_map else None
        template_names: dict[str, str] = {}
        for product_id, product in records["products"].items():
            option_values: dict[str, set[str]] = {}
            option_keys = None
            for variant in product["variants"].values():
                options = variant.get("options")
                if not isinstance(options, dict):
                    raise ValueError(f"τ retail product {product_id} has invalid variant options")
                keys = set(options)
                if option_keys is not None and keys != option_keys:
                    raise ValueError(f"τ retail product {product_id} variants do not share a stable attribute set")
                option_keys = keys
                for attribute, value in options.items():
                    if not isinstance(attribute, str) or not isinstance(value, str):
                        raise ValueError(f"τ retail product {product_id} has non-string variant options")
                    option_values.setdefault(attribute, set()).add(value)
            for attribute, values_for_attribute in option_values.items():
                _ensure_item_attribute(_native_attribute_name(attribute), attribute, values_for_attribute)
            template_code = _native_item_code(
                _projection_key(experiment_id, task_id, "product-template", product_id), product_id,
            )
            template_values = {
                "item_code": template_code,
                "item_name": str(product.get("name") or product_id),
                "item_group": retail_item_group,
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_sales_item": 1,
                "has_variants": 1,
                "variant_based_on": "Item Attribute",
                "attributes": [{"attribute": _native_attribute_name(attribute)}
                               for attribute in sorted(option_keys or set())],
                **_base_fields(experiment_id, task_id, "product", product_id, product),
            }
            template, product_inserted = _get_or_insert(
                "Item", _projection_key(experiment_id, task_id, "product", product_id), template_values,
            )
            template_names[product_id] = template.name
            identities[("product", product_id)] = template.name
            created["products"] += int(product_inserted)
        variant_doc_names: dict[str, str] = {}
        for variant_id, (product_id, product, variant) in variant_map.items():
            source_record = records["items"].get(variant_id, variant)
            item_code = _native_item_code(_projection_key(experiment_id, task_id, "item", variant_id), variant_id)
            label = str(product.get("name") or product_id)
            options = variant.get("options")
            if isinstance(options, dict) and options:
                label += " (" + ", ".join(f"{key}: {value}" for key, value in sorted(options.items())) + ")"
            if not isinstance(variant.get("available"), bool):
                raise ValueError(f"τ retail variant {variant_id} has an invalid availability value")
            doc = _existing_projection("Item", experiment_id, task_id, "item", variant_id)
            inserted = doc is None
            if inserted:
                from erpnext.controllers.item_variant import create_variant

                doc = create_variant(template_names[product_id], {
                    _native_attribute_name(attribute): value for attribute, value in options.items()
                })
                doc.item_code = item_code
                doc.item_name = label
                doc.stock_uom = "Nos"
                doc.is_stock_item = 0
                doc.is_sales_item = 1
                doc.disabled = int(not variant["available"])
                doc.update(_base_fields(experiment_id, task_id, "item", variant_id, source_record))
                doc.insert(ignore_permissions=True)
            variant_doc_names[variant_id] = doc.name
            identities[("item", variant_id)] = doc.name
            created["items"] += int(inserted)
            price = variant.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
                raise ValueError(f"τ retail variant {variant_id} has an invalid native selling price")
            price_values = {
                "item_code": doc.name,
                "price_list": PRICE_LIST,
                "price_list_rate": price,
                "selling": 1,
                "buying": 0,
                **_base_fields(experiment_id, task_id, "item-price", variant_id, source_record),
            }
            price_doc, price_inserted = _get_or_insert(
                "Item Price", _projection_key(experiment_id, task_id, "item-price", variant_id), price_values,
            )
            identities[("item_price", variant_id)] = price_doc.name
            created["item_prices"] += int(price_inserted)
        for source_id, order in records["orders"].items():
            order_id = _required_string(order.get("order_id"), f"order {source_id}.order_id")
            source_status = _required_string(order.get("status"), f"order {order_id}.status")
            if source_status not in _ORDER_STATUSES:
                raise ValueError(f"τ retail order {order_id} has an unsupported source status")
            user_id = _required_string(order.get("user_id"), f"order {source_id}.user_id")
            user_record = records["users"].get(user_id)
            if not user_record:
                projection_gaps.append({"entity": "orders", "source_id": order_id, "field": "user_id",
                                        "reason": "native_customer_source_record_missing"})
                continue
            customer_id = identities.get(("user", user_id))
            if not customer_id:
                projection_gaps.append({"entity": "orders", "source_id": order_id, "field": "user_id",
                                        "reason": "native_customer_not_projected"})
                continue
            customer_address = address_ids.get(("user", user_id))
            if not customer_address:
                projection_gaps.append({"entity": "orders", "source_id": order_id, "field": "user_id",
                                        "reason": "native_customer_address_not_projected"})
                continue
            source_items = order.get("items")
            if not isinstance(source_items, list) or not source_items:
                raise ValueError(f"τ retail order {order_id} has no native ERPNext item rows")
            item_rows = []
            missing_variant = None
            for item_index, source_item in enumerate(source_items):
                variant_id = _required_string(source_item.get("item_id"), f"order {order_id} item_id")
                item_code = variant_doc_names.get(variant_id)
                if not item_code:
                    missing_variant = variant_id
                    break
                parent_product = variant_map[variant_id][0]
                if source_item.get("product_id") != parent_product:
                    missing_variant = variant_id
                    break
                item_rows.append({
                    "item_code": item_code,
                    "qty": 1,
                    "rate": source_item.get("price"),
                    "price_list_rate": source_item.get("price"),
                    "description": str(source_item.get("name") or ""),
                    "custom_source_item_id": variant_id,
                    "custom_source_item_index": item_index,
                })
            if missing_variant:
                projection_gaps.append({"entity": "orders", "source_id": order_id, "field": "items.item_id",
                                        "reason": f"native_variant_not_projected:{missing_variant}"})
                continue
            shipping_values = _address_values(
                order.get("address"), f"{order_id} shipping", False,
                user_record.get("email"), customer_id,
                experiment_id, task_id, "order-address", order_id,
            )
            shipping_address, shipping_inserted = _get_or_insert(
                "Address", _projection_key(experiment_id, task_id, "order-address", order_id), shipping_values,
            )
            address_ids[("order", order_id)] = shipping_address.name
            created["addresses"] += int(shipping_inserted)
            payment_history = order.get("payment_history", [])
            if not isinstance(payment_history, list):
                raise ValueError(f"τ retail order {order_id} has invalid payment history")
            selected_method = None
            if payment_history:
                selected_method_id = _required_string(
                    payment_history[-1].get("payment_method_id"),
                    f"order {order_id}.payment_history.payment_method_id",
                )
                selected_method, _account = _mode_of_payment(
                    selected_method_id, user_record.get("payment_methods", {}), customer_id,
                    experiment_id, task_id,
                )
            values = {
                "customer": customer_id,
                "company": COMPANY,
                "transaction_date": exchange_date,
                "delivery_date": exchange_date,
                "currency": "USD",
                "conversion_rate": exchange_rate,
                "selling_price_list": PRICE_LIST,
                "plc_conversion_rate": exchange_rate,
                "customer_address": customer_address,
                "shipping_address_name": shipping_address.name,
                "items": item_rows,
                "custom_tau_projection_as_of": exchange_date,
                "custom_tau_usd_to_company_rate": exchange_rate,
                "custom_customer_service_state": _SERVICE_STATES["pending"],
                "custom_customer_service_external_order_id": order_id,
                "custom_customer_service_cancel_reason": order.get("cancel_reason") or "",
                "custom_tau_order_payment_method": selected_method,
                **_base_fields(experiment_id, task_id, "order", order_id, order),
            }
            doc, inserted = _get_or_insert("Sales Order", _projection_key(experiment_id, task_id, "order", order_id), values)
            identities[("order", order_id)] = doc.name
            if inserted:
                doc.submit()
            created["orders"] += int(inserted)
            if inserted:
                history_counts = _seed_native_order_history(
                    doc, order, order_id, records["users"][user_id], experiment_id, task_id,
                    exchange_date, exchange_rate,
                )
                doc = _apply_source_workflow_state(doc, source_status)
                for entity, count in history_counts.items():
                    created[entity] += count
            service_request = _seed_service_request(
                order, order_id, records["users"][user_id], doc, experiment_id, task_id,
            )
            if service_request:
                identities[("service_request", order_id)] = service_request
                created["service_requests"] = created.get("service_requests", 0) + 1
    except Exception:
        frappe.db.rollback()
        raise
    from company_evidence import refresh_address_evidence_scope

    refresh_address_evidence_scope()
    native_documents: dict[str, dict[str, str]] = {
        "users": {}, "products": {}, "items": {}, "item_prices": {}, "orders": {}, "payment_methods": {},
        "addresses": {},
    }
    entity_aliases = {"user": "users", "product": "products", "item": "items", "order": "orders",
                      "item_price": "item_prices", "payment_method": "payment_methods",
                      "service_request": "service_requests"}
    for (entity, source_id), native_name in identities.items():
        target = entity_aliases.get(entity)
        if target:
            native_documents[target][source_id] = native_name
    for (entity, source_id), native_name in address_ids.items():
        native_documents["addresses"][f"{entity}:{source_id}"] = native_name
    return {"task_id": task_id, "experiment_id": experiment_id, "company": COMPANY,
            "created": created, "projection_as_of": exchange_date, "usd_to_gbp_rate": exchange_rate,
            "native_documents": native_documents,
            "source_gaps": source_gaps, "projection_gaps": projection_gaps}


def observe_retail_task(manifest: dict[str, Any], experiment_id: str) -> dict[str, Any]:
    """Read the current native ERP state into τ retail's partial DB contract."""
    task_id, records, source_gaps = _validate_manifest(manifest, experiment_id)
    observation: dict[str, dict[str, Any]] = {key: {} for key in ("products", "users", "orders")}
    unmapped: list[dict[str, str]] = []
    native_evidence: dict[str, list[dict[str, Any]]] = {}
    service_issue_evidence: dict[str, dict[str, Any]] = {}

    def gap(entity: str, source_id: str, field: str, reason: str) -> None:
        unmapped.append({"entity": entity, "source_id": source_id, "field": field, "reason": reason})

    def native(doctype: str, entity: str, source_id: str):
        return _existing_projection(doctype, experiment_id, task_id, entity, source_id)

    def linked(doctype: str, fields: list[str], **filters: Any):
        return frappe.get_all(doctype, filters=filters, fields=fields)

    def source_country(country: str) -> str:
        return "USA" if country == "United States" else country

    order_names: dict[str, Any] = {}
    for source_id, product in records["products"].items():
        product_doc = native("Item", "product", source_id)
        if not product_doc:
            gap("products", source_id, "product", "native_product_group_missing")
            continue
        variants = {}
        for variant_id, variant in product["variants"].items():
            item = native("Item", "item", variant_id)
            price = native("Item Price", "item-price", variant_id)
            if not item or not price:
                gap("products", source_id, f"variants.{variant_id}", "native_item_or_price_missing")
                continue
            options = {}
            for row in item.attributes:
                source_attribute_name = frappe.db.get_value(
                    "Item Attribute", row.attribute, "custom_tau_source_attribute_name",
                )
                if not source_attribute_name:
                    gap("products", source_id, f"variants.{variant_id}.options.{row.attribute}",
                        "native_item_attribute_source_name_missing")
                    continue
                options[source_attribute_name] = row.attribute_value
            variants[variant_id] = {"item_id": variant_id, "options": options,
                                    "available": not bool(item.disabled), "price": price.price_list_rate}
        observation["products"][source_id] = {"product_id": source_id,
                                               "name": product_doc.item_name,
                                               "variants": variants}

    for source_id, user in records["users"].items():
        customer = native("Customer", "user", source_id)
        address_name = getattr(customer, "customer_primary_address", None) if customer else None
        address = frappe.get_doc("Address", address_name) if address_name and frappe.db.exists("Address", address_name) else None
        if not customer or not address:
            gap("users", source_id, "identity_or_address", "native_customer_or_address_missing")
            continue
        first_last = str(customer.customer_name or "").split(" ", 1)
        observed_user = {
            "user_id": source_id,
            "name": {"first_name": first_last[0], "last_name": first_last[1] if len(first_last) > 1 else ""},
            "email": customer.email_id or "",
            "address": {"address1": address.address_line1, "address2": address.address_line2 or "",
                        "city": address.city, "country": source_country(address.country), "state": address.state or "",
                        "zip": address.pincode},
            "payment_methods": {}, "orders": [],
        }
        for mode_row in linked("Mode of Payment", ["name"], custom_tau_customer_id=customer.name):
            mode = frappe.get_doc("Mode of Payment", mode_row.name)
            if (mode.custom_source_system != SOURCE or mode.custom_task_id != _DATASET_TASK
                    or mode.custom_experiment_id != _DATASET_EXPERIMENT):
                continue
            method_id = mode.custom_source_id
            observed_user["payment_methods"][method_id] = {
                "id": method_id,
                "source": mode.custom_tau_payment_source,
                **({"brand": mode.custom_tau_payment_brand} if mode.custom_tau_payment_brand else {}),
                **({"last_four": mode.custom_tau_payment_last_four} if mode.custom_tau_payment_last_four else {}),
                **({"balance": mode.custom_tau_gift_card_balance_usd}
                   if mode.custom_tau_payment_source == "gift_card" else {}),
            }
        for source_order_id, source_order in records["orders"].items():
            if source_order.get("user_id") == source_id:
                order_doc = native("Sales Order", "order", source_order_id)
                if order_doc:
                    observed_user["orders"].append(source_order_id)
                    order_names[source_order_id] = order_doc
        observation["users"][source_id] = observed_user

    for source_id, source_order in records["orders"].items():
        order_doc = order_names.get(source_id) or native("Sales Order", "order", source_id)
        if not order_doc:
            gap("orders", source_id, "order", "native_sales_order_missing")
            continue
        native_evidence[source_id] = [{
            "doctype": "Sales Order", "name": order_doc.name,
            "docstatus": int(order_doc.docstatus),
            "modified": str(order_doc.modified) if order_doc.modified else "",
        }]
        source_customer_id = next((uid for uid, row in records["users"].items()
                                   if row.get("user_id") == source_order.get("user_id")), None)
        source_user = observation["users"].get(source_customer_id, {})
        items = []
        for row in sorted(order_doc.items, key=lambda line: line.custom_source_item_index or 0):
            variant_doc = frappe.get_doc("Item", row.item_code)
            variant_id = variant_doc.custom_source_id if variant_doc.custom_source_system == SOURCE else None
            product_id = None
            if variant_doc.variant_of:
                product = frappe.get_doc("Item", variant_doc.variant_of)
                if product.custom_source_system == SOURCE:
                    product_id = product.custom_source_id
            items.append({"item_id": variant_id, "product_id": product_id,
                "name": row.description, "price": row.rate})
            if not variant_id or not product_id:
                gap("orders", source_id, "items.item_id", "native_sales_order_item_source_id_missing")
        observed_fulfillments = []
        delivery_names = {
            row.parent for row in linked("Delivery Note Item", ["parent"], against_sales_order=order_doc.name)
        }
        for index, _ in enumerate(source_order.get("fulfillments", [])):
            delivery = native("Delivery Note", "fulfillment", f"{source_id}:fulfillment:{index}")
            if delivery:
                delivery_names.add(delivery.name)
        deliveries = [frappe.get_doc("Delivery Note", name) for name in delivery_names
                      if frappe.db.exists("Delivery Note", name)
                      and frappe.get_doc("Delivery Note", name).docstatus == 1]
        native_delivery_facts = []
        for delivery in sorted(deliveries, key=lambda doc: (doc.posting_date or "", doc.name)):
            try:
                tracking_ids = [delivery.lr_no] if delivery.lr_no else []
            except AttributeError:
                tracking_ids = []
                gap("orders", source_id, "fulfillments.tracking_id", "native_delivery_note_lr_no_unavailable")
            delivered_item_ids = []
            delivery_items = []
            for row in delivery.items:
                item_doc = frappe.get_doc("Item", row.item_code)
                if item_doc.custom_source_system != SOURCE:
                    gap("orders", source_id, "fulfillments.item_ids", "native_delivery_contains_untracked_item")
                    delivery_items.append({"item_id": None, "quantity": row.qty})
                    continue
                qty = int(row.qty)
                if qty != row.qty or qty < 0:
                    gap("orders", source_id, "fulfillments.item_ids", "native_delivery_quantity_is_not_integer")
                    delivery_items.append({"item_id": item_doc.custom_source_id, "quantity": row.qty})
                    continue
                delivered_item_ids.extend([item_doc.custom_source_id] * qty)
                delivery_items.append({"item_id": item_doc.custom_source_id, "quantity": row.qty})
            observed_fulfillments.append({
                "tracking_id": tracking_ids,
                "item_ids": delivered_item_ids,
            })
            native_delivery_facts.append({
                "doctype": "Delivery Note",
                "name": delivery.name,
                "docstatus": int(delivery.docstatus),
                "modified": str(delivery.modified) if delivery.modified else "",
                "posting_date": str(delivery.posting_date) if delivery.posting_date else None,
                "against_sales_order": order_doc.name,
                "items": delivery_items,
            })
        invoice_names = {row.parent for row in linked(
            "Sales Invoice Item", ["parent"], sales_order=order_doc.name,
        )}
        source_invoice_names = set(invoice_names)
        if invoice_names:
            invoice_names.update(row.name for row in frappe.get_all(
                "Sales Invoice", filters={"return_against": ["in", sorted(invoice_names)]}, fields=["name"],
            ))
        invoice_names.update(
            row.name for row in linked("Sales Invoice", ["name"], custom_source_key=_projection_key(
                experiment_id, task_id, "sales-invoice", source_id,
            ))
        )
        invoices = [frappe.get_doc("Sales Invoice", name) for name in invoice_names
                    if frappe.db.exists("Sales Invoice", name)
                    and frappe.get_doc("Sales Invoice", name).docstatus == 1]
        native_invoice_facts = []
        for invoice in sorted(invoices, key=lambda doc: (doc.posting_date or "", doc.name)):
            invoice_items = []
            for row in invoice.items:
                item_doc = frappe.get_doc("Item", row.item_code) if row.item_code else None
                item_source_id = (
                    item_doc.custom_source_id
                    if item_doc and item_doc.custom_source_system == SOURCE
                    else None
                )
                invoice_items.append({
                    "item_id": item_source_id,
                    "quantity": row.qty,
                    "rate": row.rate,
                    "amount": row.amount,
                })
            native_invoice_facts.append({
                "doctype": "Sales Invoice",
                "name": invoice.name,
                "docstatus": int(invoice.docstatus),
                "modified": str(invoice.modified) if invoice.modified else "",
                "is_return": bool(invoice.is_return),
                "return_against": invoice.return_against,
                "currency": invoice.currency,
                "posting_date": str(invoice.posting_date) if invoice.posting_date else None,
                "grand_total": invoice.grand_total,
                "items": invoice_items,
                "linked_to_original_order_invoice": (
                    not bool(invoice.is_return)
                    or invoice.return_against in source_invoice_names
                ),
            })
        reference_names = {order_doc.name, *(invoice.name for invoice in invoices)}
        payment_names = set()
        for reference_name in reference_names:
            reference_doctype = "Sales Order" if reference_name == order_doc.name else "Sales Invoice"
            payment_names.update(row.parent for row in linked(
                "Payment Entry Reference", ["parent"], reference_doctype=reference_doctype,
                reference_name=reference_name,
            ))
        payments = [frappe.get_doc("Payment Entry", name) for name in payment_names
                    if frappe.db.exists("Payment Entry", name)
                    and frappe.get_doc("Payment Entry", name).docstatus == 1]
        payment_history = []
        native_payment_facts = []
        payments.sort(key=lambda payment: (
            (0, int(payment.custom_source_id.rsplit(":", 1)[1]))
            if payment.custom_source_id and payment.custom_source_id.rsplit(":", 1)[-1].isdigit()
            else (1, str(payment.creation or payment.name))
        ))
        for payment in payments:
            method = frappe.get_doc("Mode of Payment", payment.mode_of_payment) if payment.mode_of_payment else None
            method_id = method.custom_source_id if method and method.custom_source_system == SOURCE else None
            event_type = "payment" if payment.payment_type == "Receive" else (
                "refund" if payment.payment_type == "Pay" else None
            )
            reference_names = {order_doc.name, *(inv.name for inv in invoices)}
            payment_allocations = []
            for reference in payment.references:
                if reference.reference_name not in reference_names:
                    continue
                allocated = payment.get_allocated_amount_in_transaction_currency(
                    reference.allocated_amount,
                    reference.reference_doctype,
                    reference.reference_name,
                )
                reference_currency = frappe.db.get_value(
                    reference.reference_doctype, reference.reference_name, "currency",
                )
                payment_allocations.append({
                    "reference_doctype": reference.reference_doctype,
                    "reference_name": reference.reference_name,
                    "amount_in_reference_currency": allocated,
                    "currency": reference_currency,
                })
            reference_amounts = [
                row["amount_in_reference_currency"] for row in payment_allocations
            ]
            if not reference_amounts:
                gap("orders", source_id, "payment_history.amount", "native_payment_has_no_order_reference_allocation")
                continue
            payment_history.append({"transaction_type": event_type,
                                    "amount": abs(sum(reference_amounts)),
                                    "payment_method_id": method_id})
            native_payment_facts.append({
                "doctype": "Payment Entry",
                "name": payment.name,
                "docstatus": int(payment.docstatus),
                "modified": str(payment.modified) if payment.modified else "",
                "payment_type": payment.payment_type,
                "transaction_type": event_type,
                "posting_date": str(payment.posting_date) if payment.posting_date else None,
                "payment_method_id": method_id,
                "references": payment_allocations,
            })
            native_evidence[source_id].append({
                "doctype": "Payment Entry", "name": payment.name,
                "docstatus": int(payment.docstatus),
                "modified": str(payment.modified) if payment.modified else "",
            })
            if not event_type or not method_id:
                gap("orders", source_id, "payment_history", "native_payment_entry_missing_source_event_fields")
        replacement_sales_orders = []
        replacement_order_rows = linked(
            "Sales Order", ["name"], company=COMPANY,
            custom_tau_exchange_against_order=order_doc.name,
        )
        for replacement_row in sorted(replacement_order_rows, key=lambda row: row.name):
            replacement = frappe.get_doc("Sales Order", replacement_row.name)
            replacement_items = []
            for row in replacement.items:
                item_doc = frappe.get_doc("Item", row.item_code) if row.item_code else None
                item_id = (
                    item_doc.custom_source_id
                    if item_doc and item_doc.custom_source_system == SOURCE
                    else None
                )
                replacement_items.append({
                    "item_id": item_id, "quantity": row.qty,
                    "rate": row.rate, "amount": row.amount,
                })
            replacement_deliveries = []
            replacement_delivery_names = {
                row.parent for row in linked(
                    "Delivery Note Item", ["parent"], against_sales_order=replacement.name,
                )
            }
            for delivery_name in sorted(replacement_delivery_names):
                delivery = frappe.get_doc("Delivery Note", delivery_name)
                replacement_deliveries.append({
                    "doctype": "Delivery Note", "name": delivery.name,
                    "docstatus": int(delivery.docstatus),
                    "modified": str(delivery.modified) if delivery.modified else "",
                    "posting_date": str(delivery.posting_date) if delivery.posting_date else None,
                    "items": [
                        {"item_code": row.item_code, "quantity": row.qty}
                        for row in delivery.items
                    ],
                })
            replacement_invoice_names = {
                row.parent for row in linked(
                    "Sales Invoice Item", ["parent"], sales_order=replacement.name,
                )
            }
            if replacement_invoice_names:
                replacement_invoice_names.update(row.name for row in frappe.get_all(
                    "Sales Invoice",
                    filters={"return_against": ["in", sorted(replacement_invoice_names)]},
                    fields=["name"],
                ))
            replacement_invoices = []
            for invoice_name in sorted(replacement_invoice_names):
                invoice = frappe.get_doc("Sales Invoice", invoice_name)
                replacement_invoices.append({
                    "doctype": "Sales Invoice", "name": invoice.name,
                    "docstatus": int(invoice.docstatus),
                    "modified": str(invoice.modified) if invoice.modified else "",
                    "is_return": bool(invoice.is_return),
                    "return_against": invoice.return_against,
                    "currency": invoice.currency,
                    "posting_date": str(invoice.posting_date) if invoice.posting_date else None,
                    "grand_total": invoice.grand_total,
                    "items": [{
                        "item_code": row.item_code, "quantity": row.qty,
                        "rate": row.rate, "amount": row.amount,
                    } for row in invoice.items],
                })
            replacement_payment_names = set()
            for reference_name in {replacement.name, *replacement_invoice_names}:
                reference_doctype = "Sales Order" if reference_name == replacement.name else "Sales Invoice"
                replacement_payment_names.update(row.parent for row in linked(
                    "Payment Entry Reference", ["parent"],
                    reference_doctype=reference_doctype, reference_name=reference_name,
                ))
            replacement_payments = []
            for payment_name in sorted(replacement_payment_names):
                payment = frappe.get_doc("Payment Entry", payment_name)
                replacement_payments.append({
                    "doctype": "Payment Entry", "name": payment.name,
                    "docstatus": int(payment.docstatus),
                    "modified": str(payment.modified) if payment.modified else "",
                    "payment_type": payment.payment_type,
                    "posting_date": str(payment.posting_date) if payment.posting_date else None,
                    "mode_of_payment": payment.mode_of_payment,
                    "references": [{
                        "reference_doctype": ref.reference_doctype,
                        "reference_name": ref.reference_name,
                        "allocated_amount": ref.allocated_amount,
                    } for ref in payment.references
                      if ref.reference_name in {replacement.name, *replacement_invoice_names}],
                })
            replacement_sales_orders.append({
                "doctype": "Sales Order", "name": replacement.name,
                "docstatus": int(replacement.docstatus),
                "modified": str(replacement.modified) if replacement.modified else "",
                "status": replacement.status,
                "transaction_date": str(replacement.transaction_date) if replacement.transaction_date else None,
                "delivery_date": str(replacement.delivery_date) if replacement.delivery_date else None,
                "items": replacement_items,
                "delivery_notes": replacement_deliveries,
                "sales_invoices": replacement_invoices,
                "payment_entries": replacement_payments,
            })
        service_state = order_doc.custom_customer_service_state
        if order_doc.docstatus == 2:
            status = "cancelled"
        else:
            status = _SOURCE_STATUS_BY_SERVICE_STATE.get(service_state)
            if not status:
                gap("orders", source_id, "status", "native_customer_service_workflow_state_missing_or_unknown")
        service_issue_rows = linked("Issue", ["name"], custom_customer_service_order=order_doc.name)
        service_issues = [frappe.get_doc("Issue", row.name) for row in service_issue_rows]
        if len(service_issues) > 1:
            gap("orders", source_id, "service_request", "shared_record_state_ambiguous")
        service_issue = service_issues[-1] if len(service_issues) == 1 else None
        if service_issue:
            issue_method_id = None
            if service_issue.custom_customer_service_payment_method:
                linked_method = frappe.get_doc(
                    "Mode of Payment", service_issue.custom_customer_service_payment_method
                )
                if linked_method.custom_source_system == SOURCE:
                    issue_method_id = linked_method.custom_source_id
            try:
                issue_item_ids = json.loads(service_issue.custom_customer_service_item_ids or "[]")
                issue_new_item_ids = json.loads(service_issue.custom_customer_service_new_item_ids or "[]")
            except (TypeError, ValueError):
                issue_item_ids, issue_new_item_ids = None, None
            service_issue_evidence[source_id] = {
                "name": service_issue.name,
                "status": service_issue.status,
                "docstatus": int(service_issue.docstatus or 0),
                "modified": str(service_issue.modified) if service_issue.modified else "",
                "native_ref": {
                    "doctype": "Issue", "name": service_issue.name,
                    "docstatus": int(service_issue.docstatus or 0),
                    "modified": str(service_issue.modified) if service_issue.modified else "",
                },
                "custom_customer_service_type": service_issue.custom_customer_service_type,
                "custom_customer_service_order": service_issue.custom_customer_service_order,
                "custom_customer_service_item_ids": issue_item_ids,
                "custom_customer_service_new_item_ids": issue_new_item_ids,
                "custom_customer_service_payment_method": service_issue.custom_customer_service_payment_method,
                "payment_method_id": issue_method_id,
                "custom_customer_service_price_difference": service_issue.custom_customer_service_price_difference,
            }
            # The τ RetailTools return/exchange calls stop at a customer-service
            # request. Keep actual ERP settlement documents as separate business
            # evidence; they are not an upstream τ action or completion state.
            service_issue_evidence[source_id]["native_settlement"] = {
                "coverage": "native_settlement_not_defined",
                "issue_ref": service_issue_evidence[source_id]["native_ref"],
                "delivery_notes": native_delivery_facts,
                "sales_invoices": native_invoice_facts,
                "payment_entries": native_payment_facts,
                "replacement_sales_orders": replacement_sales_orders,
            }
        if service_issue and service_issue.status == "Open":
            if service_state not in (_SERVICE_STATES["delivered"], _SERVICE_STATES["return requested"],
                                     _SERVICE_STATES["exchange requested"]):
                gap("orders", source_id, "service_request", "native_return_or_exchange_issue_is_not_in_a_delivered_state")
            elif service_issue.custom_customer_service_type == "Return":
                status = "return requested"
            elif service_issue.custom_customer_service_type == "Exchange":
                status = "exchange requested"
            else:
                gap("orders", source_id, "service_request.type", "native_service_issue_type_missing")
        extra_order_fields: dict[str, Any] = {}
        if status == "cancelled" and order_doc.custom_customer_service_cancel_reason:
            extra_order_fields["cancel_reason"] = order_doc.custom_customer_service_cancel_reason
        if service_issue:
            try:
                request_items = json.loads(service_issue.custom_customer_service_item_ids or "[]")
                replacement_items = json.loads(service_issue.custom_customer_service_new_item_ids or "[]")
            except (TypeError, ValueError):
                request_items, replacement_items = [], []
                gap("orders", source_id, "service_request.items", "native_service_issue_item_ids_invalid")
            payment_method_id = None
            if service_issue.custom_customer_service_payment_method:
                method = frappe.get_doc("Mode of Payment", service_issue.custom_customer_service_payment_method)
                if method.custom_source_system == SOURCE:
                    payment_method_id = method.custom_source_id
            if service_issue.custom_customer_service_type == "Return":
                extra_order_fields.update({"return_items": sorted(request_items),
                                           "return_payment_method_id": payment_method_id})
            elif service_issue.custom_customer_service_type == "Exchange":
                extra_order_fields.update({"exchange_items": sorted(request_items),
                                           "exchange_new_items": sorted(replacement_items),
                                           "exchange_payment_method_id": payment_method_id,
                                           "exchange_price_difference": service_issue.custom_customer_service_price_difference})
        address_name = order_doc.shipping_address_name or order_doc.customer_address
        address = frappe.get_doc("Address", address_name) if (
            address_name and frappe.db.exists("Address", address_name)
        ) else None
        if not address:
            gap("orders", source_id, "address", "native_sales_order_shipping_address_missing")
        if status is not None:
          observation["orders"][source_id] = {
            "order_id": source_id, "user_id": order_doc.customer and source_customer_id,
            "address": ({"address1": address.address_line1, "address2": address.address_line2 or "",
                         "city": address.city, "country": source_country(address.country), "state": address.state or "",
                         "zip": address.pincode} if address else {}),
            "items": items, "status": status,
            "fulfillments": observed_fulfillments,
            "payment_history": payment_history,
            **extra_order_fields,
          }
        if not source_user:
            gap("orders", source_id, "user_id", "native_customer_not_in_task_slice")
    return {"task_id": task_id, "experiment_id": experiment_id, "observation": observation,
            "unmapped": unmapped, "source_gaps": source_gaps,
            "native_evidence": native_evidence,
            "service_issue_evidence": service_issue_evidence}
