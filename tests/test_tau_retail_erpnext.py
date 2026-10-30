from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from datetime import date
from types import SimpleNamespace


ADAPTER_PATH = Path(__file__).parents[1] / "deploy/enterprise-simulation/erpnext/admin/tau_retail.py"


class FakeDocument:
    def __init__(self, fake_frappe, values):
        object.__setattr__(self, "_frappe", fake_frappe)
        data = dict(values)
        for key in ("items", "attributes", "item_attribute_values", "roles", "states", "transitions",
                    "references", "accounts"):
            if isinstance(data.get(key), list):
                data[key] = [NativeRow(value) if isinstance(value, dict) else value for value in data[key]]
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name):
        return self._data.get(name)

    def __setattr__(self, name, value):
        self._data[name] = value

    def insert(self, ignore_permissions=False):
        doctype = self._data["doctype"]
        sequence = len(self._frappe.documents[doctype]) + 1
        self._data.setdefault(
            "name", self._data.get("item_code") or self._data.get("item_group_name")
            or self._data.get("address_title") or self._data.get("mode_of_payment")
            or self._data.get("price_list_name") or self._data.get("attribute_name")
            or self._data.get("workflow_name") or self._data.get("workflow_state_name")
            or self._data.get("workflow_action_name") or self._data.get("role_name")
            or self._data.get("account_name")
            or self._data.get("email") or f"{doctype}-{sequence:05d}",
        )
        if doctype == "Payment Entry":
            self._data["name"] = f"Payment Entry-{sequence:05d}"
        self._frappe.documents[doctype].append(self)
        for index, row in enumerate(self._data.get("items", [])):
            if isinstance(row, NativeRow) and not row.get("name"):
                row.name = f"{self.name}-ROW-{index}"
        child_doctypes = {
            "Delivery Note": ("Delivery Note Item", "items"),
            "Sales Invoice": ("Sales Invoice Item", "items"),
            "Payment Entry": ("Payment Entry Reference", "references"),
        }
        child_spec = child_doctypes.get(doctype)
        if child_spec and child_spec[0] in self._frappe.documents:
            child_doctype, child_field = child_spec
            for index, row in enumerate(self._data.get(child_field, [])):
                child_values = dict(row)
                child_values.update({"doctype": child_doctype, "parent": self.name,
                                     "name": f"{self.name}-ROW-{index}"})
                self._frappe.documents[child_doctype].append(FakeDocument(self._frappe, child_values))
        return self

    def submit(self):
        self._data["docstatus"] = 1

    def append(self, fieldname, values):
        self._data.setdefault(fieldname, []).append(NativeRow(values))

    def update(self, values):
        self._data.update(values)
        return self

    def set(self, fieldname, value):
        self._data[fieldname] = value

    def reload(self):
        return self

    def update_status(self, status):
        self._data["status"] = status

    def get_allocated_amount_in_transaction_currency(self, allocated_amount, reference_doctype, reference_name):
        return allocated_amount

    def save(self, ignore_permissions=False):
        return self.insert(ignore_permissions=ignore_permissions) if not self.name else self


class NativeRow(dict):
    __getattr__ = dict.get

    def __setattr__(self, name, value):
        self[name] = value


class FakeDB:
    def __init__(self, fake_frappe):
        self.frappe = fake_frappe
        self.commits = 0
        self.rollbacks = 0

    def get_value(self, doctype, filters, field, **kwargs):
        if doctype == "Company":
            values = {"default_cash_account": "Cash - TAC", "default_bank_account": "Bank - TAC",
                      "default_currency": "GBP", "default_receivable_account": "Debtors - TAC"}
            existing = next(iter(self.frappe.documents["Company"]), None)
            if existing:
                values = {key: getattr(existing, key, None) for key in values}
            if isinstance(field, list):
                return values if kwargs.get("as_dict") else [values[name] for name in field]
            return values.get(field)
        if isinstance(filters, str):
            filters = {"name": filters}
        for document in self.frappe.documents[doctype]:
            if all(getattr(document, key) == value for key, value in filters.items()):
                return getattr(document, field)
        return None

    def exists(self, doctype, name):
        if isinstance(name, dict):
            return any(all(getattr(document, key, None) == value for key, value in name.items())
                       for document in self.frappe.documents[doctype])
        return any(document.name == name for document in self.frappe.documents[doctype])

    def set_value(self, doctype, name, fieldname, value):
        for document in self.frappe.documents[doctype]:
            if document.name == name:
                setattr(document, fieldname, value)
                return
        raise AssertionError(f"Unexpected document update: {doctype}/{name}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def load_adapter(monkeypatch):
    # The imported Frappe helper is bound to a particular fake site. A fresh
    # adapter test must never query documents retained from a prior fake site.
    monkeypatch.delitem(sys.modules, "source_records", raising=False)
    monkeypatch.delitem(sys.modules, "native_accounts", raising=False)
    monkeypatch.delitem(sys.modules, "company_evidence", raising=False)
    frappe = types.ModuleType("frappe")
    frappe.documents = {doctype: [] for doctype in (
        "Customer", "Item", "Item Group", "Sales Order", "Address", "Price List", "Item Price",
        "Item Attribute", "Role", "Workflow State", "Workflow Action Master", "Workflow", "User",
        "User Permission", "Issue", "Mode of Payment", "Delivery Note", "Delivery Note Item", "Sales Invoice",
        "Sales Invoice Item", "Payment Entry", "Payment Entry Reference", "Account", "Company", "Currency",
        "Supplier", "Webhook",
    )}
    FakeDocument(frappe, {"doctype": "Currency", "name": "USD"}).insert()
    frappe.db = FakeDB(frappe)
    frappe.get_meta = lambda doctype: SimpleNamespace(has_field=lambda field: True)
    def get_doc(values, name=None):
        if isinstance(values, str):
            return next(document for document in frappe.documents[values] if document.name == name)
        return FakeDocument(frappe, values)

    frappe.get_doc = get_doc
    def get_all(doctype, filters=None, fields=None):
        filters = filters or {}
        return [SimpleNamespace(**{field: getattr(doc, field, None) for field in (fields or ["name"])})
                for doc in frappe.documents[doctype]
                if all(getattr(doc, key, None) == value for key, value in filters.items())]
    frappe.get_all = get_all
    frappe.new_doc = lambda doctype: FakeDocument(frappe, {"doctype": doctype})
    frappe.parse_json = lambda value: value
    frappe.has_permission = lambda *args, **kwargs: True
    frappe.utils = SimpleNamespace(getdate=date.fromisoformat)
    frappe.workflow_actions = []

    model = types.ModuleType("frappe.model")
    model.__path__ = []
    workflow_module = types.ModuleType("frappe.model.workflow")
    transitions = {
        ("Pending", "Process Order"): "Processed",
        ("Pending", "Modify Order Items"): "Pending (Item Modified)",
        ("Pending", "Cancel Pending Order"): "Cancelled",
        ("Processed", "Deliver Order"): "Delivered",
        ("Delivered", "Request Return"): "Return Requested",
        ("Delivered", "Request Exchange"): "Exchange Requested",
    }
    def apply_workflow(doc, action):
        current = doc.custom_customer_service_state
        if (current, action) not in transitions:
            raise AssertionError(f"Native workflow has no transition from {current!r} via {action!r}")
        doc.custom_customer_service_state = transitions[(current, action)]
        frappe.workflow_actions.append((current, action, doc.custom_customer_service_state))
        return doc
    workflow_module.apply_workflow = apply_workflow

    custom = types.ModuleType("frappe.custom")
    custom.__path__ = []
    custom_doctype = types.ModuleType("frappe.custom.doctype")
    custom_doctype.__path__ = []
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field")
    custom_field.__path__ = []
    custom_field_module = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field_module.create_custom_fields = lambda fields, update=False: None
    erpnext = types.ModuleType("erpnext")
    erpnext.__path__ = []
    erpnext_setup = types.ModuleType("erpnext.setup")
    erpnext_setup.__path__ = []
    erpnext_setup_utils = types.ModuleType("erpnext.setup.utils")
    erpnext_setup_utils.get_exchange_rate = lambda source, target, day, args=None: 0.78
    erpnext_controllers = types.ModuleType("erpnext.controllers")
    erpnext_controllers.__path__ = []
    erpnext_item_variant = types.ModuleType("erpnext.controllers.item_variant")
    def create_variant(template_name, options):
        template = next(doc for doc in frappe.documents["Item"] if doc.name == template_name)
        return FakeDocument(frappe, {
            "doctype": "Item", "variant_of": template_name, "item_group": template.item_group,
            "stock_uom": template.stock_uom, "is_stock_item": template.is_stock_item,
            "attributes": [{"attribute": key, "attribute_value": value} for key, value in options.items()],
        })
    erpnext_item_variant.create_variant = create_variant
    for role_name in ("Sales User", "Accounts User", "Support Team", "System Manager"):
        FakeDocument(frappe, {"doctype": "Role", "role_name": role_name, "name": role_name}).insert()
    FakeDocument(frappe, {"doctype": "Account", "name": "Retail USD Receivables - TAC",
                          "account_name": "Retail USD Receivables", "company": "The Agent Company",
                          "account_type": "Receivable", "account_currency": "USD",
                          "root_type": "Asset", "parent_account": "Accounts Receivable - TAC",
                          "is_group": 0}).insert()
    FakeDocument(frappe, {"doctype": "User", "email": "theagentcompany@example.com",
                          "roles": [{"role": "Simulation ERP Agent"}]}).insert()
    for module in (frappe, custom, custom_doctype, custom_field, custom_field_module, model, workflow_module):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    for module in (erpnext, erpnext_setup, erpnext_setup_utils, erpnext_controllers, erpnext_item_variant):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(ADAPTER_PATH.parent))

    spec = importlib.util.spec_from_file_location("test_tau_retail_native_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, frappe


def install_native_builder_fakes(monkeypatch, frappe):
    packages = (
        "erpnext.selling", "erpnext.selling.doctype", "erpnext.selling.doctype.sales_order",
        "erpnext.accounts", "erpnext.accounts.doctype", "erpnext.accounts.doctype.sales_invoice",
        "erpnext.accounts.doctype.payment_entry",
    )
    for name in packages:
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)

    sales_order = types.ModuleType("erpnext.selling.doctype.sales_order.sales_order")
    def make_delivery_note(order_name):
        order = frappe.get_doc("Sales Order", order_name)
        return FakeDocument(frappe, {"doctype": "Delivery Note", "items": [
            {"item_code": row.item_code, "qty": row.qty, "so_detail": row.name,
             "against_sales_order": order.name}
            for row in order.items
        ]})
    def make_sales_invoice(order_name):
        order = frappe.get_doc("Sales Order", order_name)
        return FakeDocument(frappe, {"doctype": "Sales Invoice", "currency": order.currency,
            "conversion_rate": order.conversion_rate, "items": [
                {"item_code": row.item_code, "qty": row.qty, "rate": row.rate,
                 "sales_order": order.name, "so_detail": row.name}
                for row in order.items
            ]})
    sales_order.make_delivery_note = make_delivery_note
    sales_order.make_sales_invoice = make_sales_invoice

    sales_invoice = types.ModuleType("erpnext.accounts.doctype.sales_invoice.sales_invoice")
    def make_sales_return(invoice_name):
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        return FakeDocument(frappe, {"doctype": "Sales Invoice", "is_return": 1,
            "return_against": invoice.name, "currency": invoice.currency,
            "conversion_rate": invoice.conversion_rate, "items": [
                {"item_code": row.item_code, "qty": -row.qty, "rate": row.rate,
                 "sales_order": row.sales_order, "so_detail": row.so_detail}
                for row in invoice.items
            ]})
    sales_invoice.make_sales_return = make_sales_return

    payment_entry = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
    def get_payment_entry(reference_doctype, reference_name):
        reference = frappe.get_doc(reference_doctype, reference_name)
        payment_type = "Pay" if getattr(reference, "is_return", 0) else "Receive"
        return FakeDocument(frappe, {
            "doctype": "Payment Entry",
            "payment_type": payment_type,
            "references": [{"reference_doctype": reference_doctype,
                            "reference_name": reference_name,
                            "allocated_amount": -59.5 if payment_type == "Pay" else 59.5}],
        })
    payment_entry.get_payment_entry = get_payment_entry

    for module in (sales_order, sales_invoice, payment_entry):
        monkeypatch.setitem(sys.modules, module.__name__, module)


def manifest():
    user = {"user_id": "user-1", "name": {"first_name": "Rae", "last_name": "Example"},
            "email": "rae@example.invalid", "orders": ["#W1"], "address": {
                "address1": "1 Example Street", "address2": "", "city": "Denver", "country": "USA",
                "state": "CO", "zip": "80279",
            }}
    product = {"product_id": "product-1", "name": "Trail Shoes", "variants": {
        "variant-1": {"item_id": "variant-1", "options": {"size": "8"}, "available": True, "price": 59.5},
    }}
    order = {"order_id": "#W1", "user_id": "user-1", "status": "pending", "payment_history": [],
             "fulfillments": [], "address": {"address1": "2 Example Street", "address2": "", "city": "Denver",
             "country": "USA", "state": "CO", "zip": "80279"}, "items": [{"item_id": "variant-1",
             "product_id": "product-1", "name": "Trail Shoes", "price": 59.5}]}
    records = {"users": {"user-1": user}, "products": {"product-1": product},
               "orders": {"#W1": order}}
    mappings = {entity: {key: f"tau2:retail:{singular}:{key}" for key in values}
                for entity, singular in (("users", "user"), ("products", "product"),
                                         ("orders", "order"))
                for values in [records[entity]]}
    mappings["items"] = {"variant-1": "tau2:retail:item:variant-1"}
    return {
        "schema_version": 1,
        "source_integrity": {"status": "valid", "issues": []},
        "upstream": {"project": "sierra-research/tau2-bench", "commit": "7aad81c37a3a78b4ed3e4866769f851400c64415", "domain": "retail"},
        "task_id": "retail-task-1",
        "task": {"id": "retail-task-1", "initial_state": None, "evaluation_criteria": {}},
        "source_database": {"path": "servers/simulation/tau2_actor_runtime/data/tau2/domains/retail/db.json",
                            "sha256": "a" * 64},
        "original_db": records,
        "id_mapping": mappings,
    }


def test_import_projects_native_customer_variant_and_submitted_sales_order(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    data = manifest()

    result = adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")

    assert frappe.db.commits == 0
    assert result["created"]["users"] == 1
    assert result["created"]["products"] == 1
    assert result["created"]["items"] == 1
    assert result["created"]["orders"] == 1
    assert result["created"]["item_prices"] == 1
    customer = frappe.documents["Customer"][0]
    variant = next(doc for doc in frappe.documents["Item"] if doc.custom_source_id == "variant-1")
    template = next(doc for doc in frappe.documents["Item"] if doc.custom_source_id == "product-1")
    order = frappe.documents["Sales Order"][0]
    user_address = next(doc for doc in frappe.documents["Address"] if doc.custom_source_id == "user-1")
    assert customer.email_id == "rae@example.invalid"
    assert customer.custom_source_id == "user-1"
    assert variant.custom_source_id == "variant-1"
    assert template.has_variants == 1
    assert template.attributes[0].attribute == "Retail Size"
    assert variant.variant_of == template.name
    assert variant.attributes[0].attribute_value == "8"
    assert order.custom_source_id == "#W1"
    assert order.currency == "USD"
    assert order.conversion_rate == 0.78
    assert order.docstatus == 1
    assert user_address.country == "United States"
    assert user_address.links[0]["link_name"] == customer.name
    assert order.items[0]["item_code"] == variant.name
    assert '"status":"pending"' in order.custom_source_snapshot
    assert frappe.documents["Workflow"] == []
    adapter._ensure_customer_service_workflow()
    workflow = next(doc for doc in frappe.documents["Workflow"]
                    if doc.workflow_name == "Customer Service Order Lifecycle")
    transition_pairs = {(row.state, row.action, row.next_state) for row in workflow.transitions}
    assert ("Pending", "Modify Order Items", "Pending (Item Modified)") in transition_pairs
    assert ("Pending (Item Modified)", "Cancel Pending Order", "Cancelled") not in transition_pairs
    assert ("Delivered", "Request Return", "Return Requested") in transition_pairs
    workflow_states = {row.state: row.allow_edit for row in workflow.states}
    assert workflow_states["Pending"] == "Simulation Retail Support"
    assert workflow_states["Pending (Item Modified)"] == "System Manager"

    # Reimport cannot duplicate records or overwrite changes made after import.
    customer.customer_name = "Agent edited customer"
    replay = adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    assert replay["created"]["users"] == 0
    assert replay["created"]["orders"] == 0
    assert customer.customer_name == "Agent edited customer"
    assert len(frappe.documents["Sales Order"]) == 1
    assert order.docstatus == 1

    # Observation reads the editable native Item Variant Attribute and Workflow
    # field; the immutable source snapshot cannot override either value.
    observation = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    product = observation["observation"]["products"]["product-1"]
    assert product["variants"]["variant-1"]["options"] == {"size": "8"}
    shipping_address = next(doc for doc in frappe.documents["Address"]
                            if doc.name == order.shipping_address_name)
    shipping_address.address_line1 = "3 Agent Edited Street"
    observation = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observation["observation"]["orders"]["#W1"]["address"]["address1"] == "3 Agent Edited Street"
    variant.attributes[0].attribute_value = "10"
    observation = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observation["observation"]["products"]["product-1"]["variants"]["variant-1"]["options"] == {"size": "10"}
    order.custom_customer_service_state = "Delivered"
    observation = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observation["observation"]["orders"]["#W1"]["status"] == "delivered"
    assert observation["unmapped"] == []

    first_native_names = {"customer": customer.name, "template": template.name,
                          "item": variant.name, "order": order.name}
    second_experiment = adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000002", "2026-10-30")
    assert second_experiment["created"]["items"] == 0
    assert second_experiment["created"]["users"] == 0
    assert second_experiment["created"]["orders"] == 0
    assert second_experiment["native_documents"]["users"]["user-1"] == first_native_names["customer"]
    assert second_experiment["native_documents"]["products"]["product-1"] == first_native_names["template"]
    assert second_experiment["native_documents"]["items"]["variant-1"] == first_native_names["item"]
    assert second_experiment["native_documents"]["orders"]["#W1"] == first_native_names["order"]
    same_source_variants = [doc for doc in frappe.documents["Item"] if doc.custom_source_id == "variant-1"]
    assert len(same_source_variants) == 1
    assert same_source_variants[0].custom_experiment_id == adapter._DATASET_EXPERIMENT
    assert customer.customer_name == "Agent edited customer"
    assert variant.attributes[0].attribute_value == "10"
    assert order.custom_customer_service_state == "Delivered"
    assert shipping_address.address_line1 == "3 Agent Edited Street"


def test_duplicate_order_lines_remain_distinct_and_observed(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    data = manifest()
    data["task"]["initial_state"] = None
    data["original_db"]["orders"]["#W1"]["items"].append(
        dict(data["original_db"]["orders"]["#W1"]["items"][0])
    )
    data["original_db"]["orders"]["#W1"]["status"] = "pending (item modified)"
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    order = frappe.documents["Sales Order"][0]
    assert len(order.items) == 2
    assert [row.custom_source_item_index for row in order.items] == [0, 1]
    assert {row.item_code for row in order.items} <= {item.name for item in frappe.documents["Item"]}, (
        [row.item_code for row in order.items], [item.name for item in frappe.documents["Item"]],
    )
    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert [row["item_id"] for row in observed["observation"]["orders"]["#W1"]["items"]] == [
        "variant-1", "variant-1",
    ]
    assert observed["observation"]["orders"]["#W1"]["status"] == "pending (item modified)"
    assert not observed["unmapped"]


def test_native_delivery_invoice_and_payment_builders_drive_observed_outcome(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    install_native_builder_fakes(monkeypatch, frappe)
    data = manifest()
    method_id = "paypal-1"
    data["original_db"]["users"]["user-1"]["payment_methods"] = {
        method_id: {"id": method_id, "source": "paypal"},
    }
    order = data["original_db"]["orders"]["#W1"]
    order["status"] = "delivered"
    order["fulfillments"] = [{"tracking_id": ["TRACK-1"], "item_ids": ["variant-1"]}]
    order["payment_history"] = [{"transaction_type": "payment", "amount": 59.5,
                                 "payment_method_id": method_id}]

    receipt = adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    assert receipt["created"]["delivery_notes"] == 1
    assert receipt["created"]["sales_invoices"] == 1
    assert receipt["created"]["payment_entries"] == 1
    assert all(doc.docstatus == 1 for doctype in ("Delivery Note", "Sales Invoice", "Payment Entry")
               for doc in frappe.documents[doctype])
    native_payment = frappe.documents["Payment Entry"][0]
    assert native_payment.reference_no == "τ import #W1:payment:0"
    assert native_payment.reference_date == "2026-10-30"
    assert native_payment.custom_tau_payment_reference_policy == "source_event_id_and_projection_as_of"
    native_order = frappe.documents["Sales Order"][0]
    original_mode = next(doc for doc in frappe.documents["Mode of Payment"]
                         if doc.custom_source_id == method_id)
    assert native_order.custom_tau_order_payment_method == original_mode.name

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    native_order = observed["observation"]["orders"]["#W1"]
    assert native_order["status"] == "delivered"
    assert native_order["fulfillments"] == [{"tracking_id": ["TRACK-1"], "item_ids": ["variant-1"]}]
    assert native_order["payment_history"] == [{"transaction_type": "payment", "amount": 59.5,
                                                 "payment_method_id": method_id}]
    assert observed["unmapped"] == []


def test_draft_delivery_invoice_and_payment_do_not_change_tau_history(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    install_native_builder_fakes(monkeypatch, frappe)
    data = manifest()
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    order = frappe.documents["Sales Order"][0]
    item = order.items[0]
    frappe.get_doc({
        "doctype": "Delivery Note", "docstatus": 0, "lr_no": "DRAFT-TRACK",
        "items": [{"item_code": item.item_code, "qty": 1,
                   "against_sales_order": order.name}],
    }).insert()
    frappe.get_doc({
        "doctype": "Sales Invoice", "docstatus": 0,
        "items": [{"item_code": item.item_code, "qty": 1, "rate": 59.5,
                   "sales_order": order.name}],
    }).insert()
    frappe.get_doc({
        "doctype": "Payment Entry", "docstatus": 0, "payment_type": "Receive",
        "mode_of_payment": "PayPal", "references": [
            {"reference_doctype": "Sales Order", "reference_name": order.name,
             "allocated_amount": 59.5},
        ],
    }).insert()

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    native_order = observed["observation"]["orders"]["#W1"]
    assert native_order["status"] == "pending"
    assert native_order["fulfillments"] == []
    assert native_order["payment_history"] == []
    assert not observed["unmapped"]


def test_pending_method_selection_does_not_rewrite_posted_payment_history(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    install_native_builder_fakes(monkeypatch, frappe)
    data = manifest()
    first_method, next_method = "card-1", "card-2"
    data["original_db"]["users"]["user-1"]["payment_methods"] = {
        first_method: {"id": first_method, "source": "credit_card", "brand": "visa", "last_four": "1111"},
        next_method: {"id": next_method, "source": "credit_card", "brand": "mastercard", "last_four": "2222"},
    }
    data["original_db"]["orders"]["#W1"]["payment_history"] = [
        {"transaction_type": "payment", "amount": 59.5, "payment_method_id": first_method},
    ]
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    order = frappe.documents["Sales Order"][0]
    selected = next(doc for doc in frappe.documents["Mode of Payment"] if doc.custom_source_id == next_method)
    order.custom_tau_order_payment_method = selected.name

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observed["observation"]["orders"]["#W1"]["payment_history"] == [
        {"transaction_type": "payment", "amount": 59.5, "payment_method_id": first_method},
    ]
    assert not observed["unmapped"]


def test_native_credit_note_and_refund_payment_are_observed_from_posted_documents(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    install_native_builder_fakes(monkeypatch, frappe)
    data = manifest()
    method_id = "paypal-1"
    data["original_db"]["users"]["user-1"]["payment_methods"] = {
        method_id: {"id": method_id, "source": "paypal"},
    }
    order = data["original_db"]["orders"]["#W1"]
    order["status"] = "processed"
    order["payment_history"] = [
        {"transaction_type": "payment", "amount": 59.5, "payment_method_id": method_id},
        {"transaction_type": "refund", "amount": 59.5, "payment_method_id": method_id},
    ]

    receipt = adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    assert receipt["created"]["sales_invoices"] == 1
    assert receipt["created"]["credit_notes"] == 1
    assert receipt["created"]["payment_entries"] == 2
    sales_invoices = frappe.documents["Sales Invoice"]
    assert len(sales_invoices) == 2
    assert sum(bool(doc.is_return) for doc in sales_invoices) == 1
    assert all(doc.docstatus == 1 for doc in sales_invoices)
    assert sum(doc.payment_type == "Pay" for doc in frappe.documents["Payment Entry"]) == 1
    order_doc = frappe.documents["Sales Order"][0]
    order_doc.custom_customer_service_state = "Delivered"
    issue = frappe.get_doc({
        "doctype": "Issue", "subject": "Synthetic return request", "status": "Open",
        "custom_customer_service_type": "Return",
        "custom_customer_service_order": order_doc.name,
        "custom_customer_service_item_ids": '["variant-1"]',
        "custom_customer_service_new_item_ids": "[]",
    }).insert()

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observed["observation"]["orders"]["#W1"]["payment_history"] == [
        {"transaction_type": "payment", "amount": 59.5, "payment_method_id": method_id},
        {"transaction_type": "refund", "amount": 59.5, "payment_method_id": method_id},
    ]
    assert observed["unmapped"] == []
    settlement = observed["service_issue_evidence"]["#W1"]["native_settlement"]
    assert settlement["coverage"] == "native_settlement_not_defined"
    assert {row["name"] for row in settlement["sales_invoices"]} == {
        invoice.name for invoice in sales_invoices
    }
    assert sum(row["is_return"] for row in settlement["sales_invoices"]) == 1
    assert len(settlement["payment_entries"]) == 2
    assert all(row["docstatus"] == 1 for row in settlement["payment_entries"])
    assert issue.name == settlement["issue_ref"]["name"]


def test_native_return_issue_and_payment_method_drive_observed_request(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    data = manifest()
    method_id = "gift_card-1"
    data["original_db"]["users"]["user-1"]["payment_methods"] = {
        method_id: {"id": method_id, "source": "gift_card", "balance": 12.5},
    }
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    order = frappe.documents["Sales Order"][0]
    mode = next(doc for doc in frappe.documents["Mode of Payment"]
                if doc.custom_source_id == method_id)
    mode.custom_tau_gift_card_balance_usd = 9.0
    balance_observation = adapter.observe_retail_task(
        data, "00000000-0000-4000-8000-000000000001",
    )
    assert balance_observation["observation"]["users"]["user-1"]["payment_methods"][method_id]["balance"] == 9.0
    mode.custom_tau_payment_source = "paypal"
    mode.custom_tau_payment_brand = "Updated in ERPNext"
    order.custom_customer_service_state = "Delivered"
    issue = frappe.get_doc({
        "doctype": "Issue", "subject": "Return request", "status": "Open",
        "custom_customer_service_type": "Return",
        "custom_customer_service_order": order.name,
        "custom_customer_service_item_ids": '["variant-1"]',
        "custom_customer_service_payment_method": mode.name,
        "custom_customer_service_new_item_ids": "[]",
    }).insert()

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    native_order = observed["observation"]["orders"]["#W1"]
    assert native_order["status"] == "return requested"
    assert native_order["return_items"] == ["variant-1"]
    assert native_order["return_payment_method_id"] == method_id
    observed_method = observed["observation"]["users"]["user-1"]["payment_methods"][method_id]
    assert observed_method == {
        "id": method_id, "source": "paypal", "brand": "Updated in ERPNext",
    }
    assert observed["unmapped"] == []
    assert issue.custom_customer_service_order == order.name
    issue_evidence = observed["service_issue_evidence"]["#W1"]
    assert issue_evidence["name"] == issue.name
    assert issue_evidence["status"] == "Open"
    assert issue_evidence["custom_customer_service_item_ids"] == ["variant-1"]
    assert issue_evidence["payment_method_id"] == method_id
    assert issue_evidence["native_ref"]["doctype"] == "Issue"
    assert issue_evidence["native_settlement"]["coverage"] == "native_settlement_not_defined"
    assert issue_evidence["native_settlement"]["sales_invoices"] == []
    assert issue_evidence["native_settlement"]["payment_entries"] == []


def test_native_exchange_settlement_observes_actual_replacement_documents(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    data = manifest()
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    original = frappe.documents["Sales Order"][0]
    original.custom_customer_service_state = "Delivered"
    variant = next(item for item in frappe.documents["Item"] if item.custom_source_id == "variant-1")
    replacement = frappe.get_doc({
        "doctype": "Sales Order", "name": "SO-REPLACEMENT", "company": "The Agent Company",
        "docstatus": 1, "status": "To Deliver and Bill",
        "custom_tau_exchange_against_order": original.name,
        "transaction_date": "2026-10-30", "delivery_date": "2026-11-03",
        "items": [{"item_code": variant.name, "qty": 1, "rate": 59.5, "amount": 59.5}],
    }).insert()
    frappe.get_doc({
        "doctype": "Delivery Note", "name": "DN-REPLACEMENT", "docstatus": 1,
        "posting_date": "2026-11-02", "items": [{
            "item_code": variant.name, "qty": 1, "against_sales_order": replacement.name,
        }],
    }).insert()
    frappe.get_doc({
        "doctype": "Sales Invoice", "name": "SINV-REPLACEMENT", "docstatus": 1,
        "currency": "USD", "posting_date": "2026-10-30", "grand_total": 59.5,
        "is_return": 0, "return_against": None,
        "items": [{"item_code": variant.name, "qty": 1, "rate": 59.5, "amount": 59.5,
                   "sales_order": replacement.name}],
    }).insert()
    replacement_payment = frappe.get_doc({
        "doctype": "Payment Entry", "payment_type": "Receive", "posting_date": "2026-10-30",
        "mode_of_payment": "Bank", "references": [{
            "reference_doctype": "Sales Invoice", "reference_name": "SINV-REPLACEMENT",
            "allocated_amount": 59.5,
        }],
    }).insert()
    replacement_payment.docstatus = 1
    issue = frappe.get_doc({
        "doctype": "Issue", "subject": "Synthetic exchange request", "status": "Open",
        "custom_customer_service_type": "Exchange", "custom_customer_service_order": original.name,
        "custom_customer_service_item_ids": '["variant-1"]',
        "custom_customer_service_new_item_ids": '["variant-1"]',
    }).insert()

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")

    settlement = observed["service_issue_evidence"]["#W1"]["native_settlement"]
    assert issue.name == settlement["issue_ref"]["name"]
    assert len(settlement["replacement_sales_orders"]) == 1
    replacement_outcome = settlement["replacement_sales_orders"][0]
    assert replacement_outcome["name"] == replacement.name
    assert replacement_outcome["delivery_notes"][0]["name"] == "DN-REPLACEMENT"
    assert replacement_outcome["sales_invoices"][0]["name"] == "SINV-REPLACEMENT"
    assert replacement_outcome["payment_entries"][0]["name"] == replacement_payment.name


def test_multiple_run_owned_service_requests_on_shared_order_are_reported_ambiguous(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    data = manifest()
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    order = frappe.documents["Sales Order"][0]
    order.custom_customer_service_state = "Delivered"
    for title, kind in (("Return from experiment 1", "Return"),
                        ("Exchange from experiment 2", "Exchange")):
        frappe.get_doc({
            "doctype": "Issue", "subject": title, "status": "Open",
            "custom_customer_service_type": kind,
            "custom_customer_service_order": order.name,
            "custom_customer_service_item_ids": '["variant-1"]',
            "custom_customer_service_new_item_ids": "[]",
        }).insert()

    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert {
        "entity": "orders", "source_id": "#W1", "field": "service_request",
        "reason": "shared_record_state_ambiguous",
    } in observed["unmapped"]


def test_import_rejects_task_state_changes_and_noncanonical_mapping(monkeypatch):
    adapter, _ = load_adapter(monkeypatch)
    changed = manifest()
    changed["source_integrity"] = {"status": "failed", "issues": [{"entity": "orders",
                               "reason": "source_record_missing", "source_ids": ["#MISSING"]}]}
    result = adapter.seed_retail_task(changed, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    assert result["source_gaps"][0]["source_ids"] == ["#MISSING"]

    changed = manifest()
    changed["task"]["initial_state"] = {"orders": {"#W1": {"status": "delivered"}}}
    try:
        adapter.seed_retail_task(changed, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    except ValueError as error:
        assert "authoritative initial_state" in str(error)
    else:
        raise AssertionError("The importer must not accept mutated task initial_state")

    changed = manifest()
    changed["id_mapping"]["orders"]["#W1"] = "tau2:retail:order:some-other-order"
    try:
        adapter.seed_retail_task(changed, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    except ValueError as error:
        assert "identifier mapping is inconsistent" in str(error)
    else:
        raise AssertionError("The importer must reject source ID remapping")


def test_native_variant_attribute_mapping_preserves_source_option_case(monkeypatch):
    adapter, _frappe = load_adapter(monkeypatch)
    data = manifest()
    data["original_db"]["products"]["product-1"]["variants"]["variant-1"]["options"] = {
        "RAM": "16GB",
    }
    adapter.seed_retail_task(data, "00000000-0000-4000-8000-000000000001", "2026-10-30")
    observed = adapter.observe_retail_task(data, "00000000-0000-4000-8000-000000000001")
    assert observed["observation"]["products"]["product-1"]["variants"]["variant-1"]["options"] == {
        "RAM": "16GB",
    }


def test_native_bank_setup_creates_leaf_without_balance_and_sets_only_missing_default(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    company = FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company",
                                    "default_currency": "GBP", "default_bank_account": None})
    company.insert()
    parent = FakeDocument(frappe, {"doctype": "Account", "name": "Bank Accounts - TAC",
                                   "account_name": "Bank Accounts", "company": "The Agent Company",
                                   "root_type": "Asset", "account_type": "Bank", "is_group": 1})
    parent.insert()

    result = adapter.ensure_retail_bank_account()

    account = next(row for row in frappe.documents["Account"]
                   if row.account_name == "Retail Payment Clearing")
    assert result == {"account": account.name, "company_default_account": account.name,
                      "created": True, "default_preserved": False}
    assert account.parent_account == parent.name
    assert account.account_type == "Bank"
    assert account.account_currency == "GBP"
    assert account.is_group == 0
    assert "opening_balance" not in account._data
    assert company.default_bank_account == account.name


def test_native_bank_setup_preserves_existing_company_default(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    company = FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company",
                                    "default_currency": "GBP", "default_bank_account": "Existing Bank - TAC"})
    company.insert()
    parent = FakeDocument(frappe, {"doctype": "Account", "name": "Bank Accounts - TAC",
                                   "account_name": "Bank Accounts", "company": "The Agent Company",
                                   "root_type": "Asset", "account_type": "Bank", "is_group": 1})
    parent.insert()
    existing = FakeDocument(frappe, {"doctype": "Account", "name": "Existing Bank - TAC",
                                     "account_name": "Existing Bank", "company": "The Agent Company",
                                     "root_type": "Asset", "account_type": "Bank", "is_group": 0})
    existing.insert()

    result = adapter.ensure_retail_bank_account()

    assert result["company_default_account"] == existing.name
    assert result["default_preserved"] is True
    assert company.default_bank_account == existing.name
    assert result["account"] != existing.name


def test_native_usd_receivable_account_is_created_under_existing_receivables_group(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    frappe.documents["Account"].clear()
    parent = FakeDocument(frappe, {"doctype": "Account", "name": "Accounts Receivable - TAC",
                                   "account_name": "Accounts Receivable", "company": "The Agent Company",
                                   "root_type": "Asset", "account_type": "", "is_group": 1})
    parent.insert()

    result = adapter.ensure_retail_usd_receivable_account()

    account = next(row for row in frappe.documents["Account"]
                   if row.account_name == "Retail USD Receivables")
    assert result == {"account": account.name, "created": True, "currency": "USD"}
    assert account.parent_account == parent.name
    assert account.account_type == "Receivable"
    assert account.account_currency == "USD"
    assert account.is_group == 0
    assert "opening_balance" not in account._data


def test_customer_usd_receivable_mapping_preserves_existing_usd_choice(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    usd_account = FakeDocument(frappe, {"doctype": "Account", "name": "USD Receivables - TAC",
                                        "account_currency": "USD", "company": "The Agent Company",
                                        "account_type": "Receivable", "is_group": 0, "disabled": 0})
    usd_account.insert()
    FakeDocument(frappe, {"doctype": "Account", "name": "Other USD Account - TAC",
                          "account_currency": "USD", "company": "The Agent Company",
                          "account_type": "Receivable", "is_group": 0, "disabled": 0}).insert()
    customer = FakeDocument(frappe, {"doctype": "Customer", "name": "Customer-1",
                                     "accounts": [{"company": "The Agent Company",
                                                   "account": usd_account.name}]})
    customer.insert()

    adapter._ensure_customer_usd_receivable_mapping(customer, "Other USD Account - TAC", newly_created=False)

    assert customer.accounts[0].account == usd_account.name


def test_new_customer_gets_usd_account_mapping_without_changing_company_default(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    gbp = FakeDocument(frappe, {"doctype": "Account", "name": "Debtors - TAC",
                                "account_currency": "GBP", "account_type": "Receivable"})
    gbp.company = "The Agent Company"
    gbp.is_group = 0
    gbp.disabled = 0
    gbp.insert()
    FakeDocument(frappe, {"doctype": "Account", "name": "Retail USD Receivables - TAC",
                          "account_currency": "USD", "company": "The Agent Company",
                          "account_type": "Receivable", "is_group": 0, "disabled": 0}).insert()
    customer = FakeDocument(frappe, {"doctype": "Customer", "name": "Customer-1", "accounts": [
        {"company": "The Agent Company", "account": gbp.name},
    ]})
    customer.insert()

    adapter._ensure_customer_usd_receivable_mapping(customer, "Retail USD Receivables - TAC", newly_created=True)

    assert customer.accounts[0].account == "Retail USD Receivables - TAC"
    assert frappe.db.get_value("Company", "The Agent Company", "default_receivable_account") == "Debtors - TAC"


def test_native_currency_helper_maps_new_supplier_to_dedicated_usd_payables(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    from native_accounts import map_party_to_currency

    parent = FakeDocument(frappe, {"doctype": "Account", "name": "Accounts Payable - TAC",
                                   "account_name": "Accounts Payable", "company": "The Agent Company",
                                   "root_type": "Liability", "is_group": 1})
    parent.insert()
    base_account = FakeDocument(frappe, {"doctype": "Account", "name": "Creditors - TAC",
                                        "account_name": "Creditors", "company": "The Agent Company",
                                        "root_type": "Liability", "account_type": "Payable",
                                        "account_currency": "GBP", "is_group": 0, "disabled": 0})
    base_account.insert()
    supplier = FakeDocument(frappe, {"doctype": "Supplier", "name": "Source Supplier",
                                     "accounts": [{"company": "The Agent Company",
                                                   "account": base_account.name}]})
    supplier.insert()

    result = map_party_to_currency(supplier, "USD", company="The Agent Company", newly_created=True)

    account = next(row for row in frappe.documents["Account"] if row.name == result["account"])
    assert result["currency"] == "USD"
    assert account.account_name == "Source USD Payables"
    assert account.account_type == "Payable"
    assert account.account_currency == "USD"
    assert supplier.accounts[0].account == account.name


def test_native_currency_helper_preserves_existing_valid_same_currency_ledger(monkeypatch):
    adapter, frappe = load_adapter(monkeypatch)
    from native_accounts import map_party_to_currency

    existing = FakeDocument(frappe, {"doctype": "Account", "name": "Agent USD Receivables - TAC",
                                    "account_name": "Agent USD Receivables", "company": "The Agent Company",
                                    "root_type": "Asset", "account_type": "Receivable",
                                    "account_currency": "USD", "is_group": 0, "disabled": 0})
    existing.insert()
    customer = FakeDocument(frappe, {"doctype": "Customer", "name": "Configured Customer",
                                     "accounts": [{"company": "The Agent Company", "account": existing.name}]})
    customer.insert()

    result = map_party_to_currency(customer, "USD", company="The Agent Company", newly_created=False)

    assert result["account"] == existing.name
    assert customer.accounts[0].account == existing.name
    assert not any(doc.account_name == "Source USD Receivables" for doc in frappe.documents["Account"])
