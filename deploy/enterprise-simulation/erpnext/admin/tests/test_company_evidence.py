import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys
import types



ADMIN = Path(__file__).resolve().parents[1]


class FakeDoc:
    def __init__(self, values, database):
        self.__dict__.update(values)
        self.database = database

    def insert(self, ignore_permissions=False):
        self.database.docs[self.name] = self

    def save(self, ignore_permissions=False):
        self.database.docs[self.name] = self

    def get_password(self, field, raise_exception=False):
        return getattr(self, field, None)

    def get(self, field):
        return getattr(self, field, None)

    def as_dict(self, convert_dates_to_str=False):
        return {key: value for key, value in self.__dict__.items() if key != "database"}


class FakeDatabase:
    def __init__(self):
        self.docs = {}
        self.single_values = {("System Settings", "time_zone"): "UTC"}
        self.savepoints = {}
        self.custom_fields = {"Issue": {"custom_customer_service_order": {
            "fieldtype": "Link", "options": "Sales Order", "fetch_from": None,
        }}}
        self.sales_order_companies = {}
        self.rows = {"Customer": [{
            "name": "CUST-1", "custom_company_id": "company",
            "custom_source_system": "tau2-retail", "custom_source_id": "user-1",
        }]}

    def exists(self, doctype, name):
        if doctype == "Company":
            return name == "The Agent Company"
        if doctype == "DocType":
            return name in {
                "Purchase Order",
                "Sales Order",
                "Purchase Receipt",
                "Delivery Note",
                "Purchase Invoice",
                "Sales Invoice",
                "Payment Entry",
                "Journal Entry",
                "Issue",
                "Address",
            }
        if doctype == "Webhook":
            return name in self.docs
        return False

    def get_single_value(self, doctype, field):
        return self.single_values.get((doctype, field))

    def get_value(self, doctype, name, field):
        if doctype == "Sales Order" and field == "company":
            return self.sales_order_companies.get(name)
        return None

    def savepoint(self, name):
        self.savepoints[name] = (dict(self.docs), {doctype: dict(fields) for doctype, fields in self.custom_fields.items()})

    def rollback(self, save_point):
        self.docs, self.custom_fields = self.savepoints[save_point]


def _load_module(monkeypatch):
    database = FakeDatabase()
    frappe = types.ModuleType("frappe")
    frappe.db = database
    frappe.as_json = lambda value: json.dumps(
        value, default=lambda item: item.isoformat()
    )
    def get_meta(doctype):
        def has_field(field):
            return field == "company" or field in database.custom_fields.get(doctype, {}) or (
                doctype != "Issue" and field.startswith("custom_")
            )

        def get_field(field):
            value = database.custom_fields.get(doctype, {}).get(field)
            return types.SimpleNamespace(**value) if value else None

        return types.SimpleNamespace(has_field=has_field, get_field=get_field)

    frappe.get_meta = get_meta

    def get_doc(*args):
        if len(args) == 1 and isinstance(args[0], dict):
            return FakeDoc(args[0], database)
        doctype, name = args
        if doctype == "Webhook":
            return database.docs[name]
        return database.docs[f"{doctype}:{name}"]

    def get_all(doctype, filters=None, fields=None, order_by=None):
        filters = filters or {}
        rows = database.rows.get(doctype, [])
        if "company" in filters:
            rows = [row for row in rows if row.get("company") == filters["company"]]
        if "custom_company_id" in filters:
            rows = [row for row in rows if row.get("custom_company_id") == filters["custom_company_id"]]
        if "custom_source_system" in filters:
            rows = [row for row in rows if row.get("custom_source_system")]
        if "custom_customer_service_order" in filters:
            rows = [row for row in rows if row.get("custom_customer_service_order")]
        if "custom_customer_service_type" in filters:
            allowed = filters["custom_customer_service_type"][1]
            rows = [row for row in rows if row.get("custom_customer_service_type") in allowed]
        if "parent" in filters:
            rows = [row for row in rows if row.get("parent") in filters["parent"][1]]
        if "name" in filters:
            rows = [row for row in rows if row.get("name") in filters["name"][1]]
        return rows

    frappe.get_doc = get_doc
    frappe.get_all = get_all

    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")

    def create_custom_fields(specs, update=False):
        for doctype, fields in specs.items():
            database.custom_fields.setdefault(doctype, {}).update(
                {field["fieldname"]: field for field in fields}
            )

    custom_field.create_custom_fields = create_custom_fields
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        parent = types.ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(sys.modules, "frappe.custom.doctype.custom_field.custom_field", custom_field)
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    module_name = "company_evidence_test_subject"
    spec = importlib.util.spec_from_file_location(
        module_name, ADMIN / "company_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, database, frappe


def test_installs_company_scoped_native_hmac_webhooks_and_rotates_secret(monkeypatch):
    module, database, _ = _load_module(monkeypatch)
    result = module.ensure_evidence_webhooks(
        "https://company.example/evidence", "private-test-secret"
    )

    assert result["webhooks"] == 43
    assert result["created"] == 43
    assert "private-test-secret" not in repr(result)
    assert len(database.docs) == 43
    webhook = database.docs["TAC Company Evidence - Purchase Order - on_submit"]
    assert webhook.webhook_doctype == "Purchase Order"
    assert webhook.webhook_docevent == "on_submit"
    assert webhook.condition == 'doc.company == "The Agent Company"'
    assert webhook.enable_security == 1
    assert webhook.request_url == "https://company.example/evidence"
    assert webhook.request_method == "POST"
    assert webhook.request_structure == "JSON"
    assert webhook.background_jobs_queue == "short"
    assert webhook.webhook_secret == "private-test-secret"
    assert '"event":"on_submit"' in webhook.webhook_json
    assert '"document":{{ doc|json }}' in webhook.webhook_json
    assert '"erp_timezone":"UTC"' in webhook.webhook_json

    second = module.ensure_evidence_webhooks(
        "https://company.example/evidence", "private-test-secret"
    )
    assert second["created"] == 0 and second["updated"] == 0
    rotated = module.ensure_evidence_webhooks(
        "https://company.example/evidence", "rotated-secret"
    )
    assert rotated["updated"] == 43
    assert all(doc.webhook_secret == "rotated-secret" for doc in database.docs.values())
    assert "rotated-secret" not in repr(rotated)
    issue = database.docs["TAC Company Evidence - Issue - on_update"]
    assert issue.condition == 'doc.custom_customer_service_company == "The Agent Company"'
    assert '"linked_company":{{ doc.custom_customer_service_company|json }}' in issue.webhook_json
    address = database.docs["TAC Company Evidence - Address - on_update"]
    assert address.condition == "any(link.link_doctype == 'Customer' and link.link_name in [\"CUST-1\"] for link in doc.links)"
    assert '"linked_company":"The Agent Company"' in address.webhook_json
    assert "TAC Company Evidence - Issue - on_submit" not in database.docs
    assert database.custom_fields["Issue"]["custom_customer_service_company"] == {
        "fieldname": "custom_customer_service_company",
        "label": "Customer Service Company",
        "fieldtype": "Link",
        "options": "Company",
        "fetch_from": "custom_customer_service_order.company",
        "insert_after": "custom_customer_service_order",
        "read_only": 1,
        "no_copy": 1,
    }


def test_payload_template_renders_full_native_document_and_canonical_json(monkeypatch):
    from jinja2 import Environment

    module, _, frappe = _load_module(monkeypatch)
    template = module._payload_template("on_submit", "UTC")
    renderer = Environment()
    renderer.filters["json"] = frappe.as_json
    rendered = renderer.from_string(template).render(
        doc={
            "doctype": "Purchase Invoice",
            "name": "PINV-1",
            "company": module.COMPANY_NAME,
            "docstatus": 1,
            "items": [{"qty": 2.0}],
        },
    )
    payload = json.loads(rendered)
    assert payload == {
        "event": "on_submit",
        "erp_timezone": "UTC",
        "document": {
            "doctype": "Purchase Invoice",
            "name": "PINV-1",
            "company": module.COMPANY_NAME,
            "docstatus": 1,
            "items": [{"qty": 2.0}],
        },
    }
    issue_template = module._payload_template("on_update", "UTC", linked_company=True)
    renderer.filters["json"] = frappe.as_json
    issue_payload = json.loads(renderer.from_string(issue_template).render(
        doc={
            "doctype": "Issue", "name": "ISSUE-1",
            "custom_customer_service_company": module.COMPANY_NAME,
        }
    ))
    assert issue_payload["linked_company"] == module.COMPANY_NAME
    assert issue_payload["event"] == "on_update"


def test_collect_baseline_returns_only_company_source_linked_records(monkeypatch):
    module, database, frappe = _load_module(monkeypatch)
    database.rows = {
        "Purchase Order": [
            {
                "name": "PO-1",
                "company": module.COMPANY_NAME,
                "custom_company_id": module.COMPANY_ID,
                "custom_source_system": "coffeebench",
                "custom_source_id": "deal-1",
            }
        ],
    }
    database.docs["Purchase Order:PO-1"] = FakeDoc(
        {
            "doctype": "Purchase Order",
            "name": "PO-1",
            "company": module.COMPANY_NAME,
            "custom_company_id": module.COMPANY_ID,
            "custom_source_system": "coffeebench",
            "custom_source_id": "deal-1",
            "custom_source_key": "source-key",
            "custom_task_id": "company",
            "custom_experiment_id": "background",
            "custom_source_snapshot": '{"native_record_id":"deal-1"}',
            "docstatus": 1,
        },
        database,
    )

    rows = module.collect_mapped_records()
    assert len(rows) == 1
    assert rows[0]["doctype"] == "Purchase Order"
    assert rows[0]["source_snapshot"] == {"native_record_id": "deal-1"}
    assert rows[0]["document"]["docstatus"] == 1
    assert "document" in rows[0] and rows[0]["source_id"] == "deal-1"


def test_collect_source_item_only_when_referenced_by_target_company_po(monkeypatch):
    module, database, _frappe = _load_module(monkeypatch)
    database.rows = {
        "Purchase Order": [{"name": "PO-1", "company": module.COMPANY_NAME, "docstatus": 1}],
        "Purchase Order Item": [{"parent": "PO-1", "item_code": "ITEM-COMPANY"}],
        "Item": [{
            "name": "ITEM-COMPANY", "custom_company_id": module.COMPANY_ID,
            "custom_source_system": "ecommercebench", "custom_source_id": "widget-1",
        }],
        "Customer": [],
    }
    database.docs["Item:ITEM-COMPANY"] = FakeDoc({
        "doctype": "Item", "name": "ITEM-COMPANY",
        "custom_company_id": module.COMPANY_ID,
        "custom_source_system": "ecommercebench", "custom_source_id": "widget-1",
        "custom_source_key": "source-key", "custom_task_id": "company",
        "custom_experiment_id": "background",
        "custom_source_snapshot": '{"native_record_id":"widget-1"}',
    }, database)

    rows = module.collect_mapped_records()

    assert len(rows) == 1
    assert rows[0]["doctype"] == "Item"
    assert rows[0]["name"] == "ITEM-COMPANY"
    assert rows[0]["source_snapshot"] == {"native_record_id": "widget-1"}


def test_native_verifier_uses_signed_renderer_for_posted_doc_without_sending(
    monkeypatch,
):
    module, database, frappe = _load_module(monkeypatch)
    module.ensure_evidence_webhooks(
        "https://company.example/evidence", "private-test-secret"
    )
    webhook = database.docs["TAC Company Evidence - Purchase Invoice - on_submit"]
    document = FakeDoc(
        {
            "doctype": "Purchase Invoice",
            "name": "PINV-1",
            "company": module.COMPANY_NAME,
            "docstatus": 1,
        },
        database,
    )
    database.docs["Purchase Invoice:PINV-1"] = document

    def data_renderer(doc, hook):
        return {
            "event": hook.webhook_docevent,
            "erp_timezone": "UTC",
            "document": doc.as_dict(convert_dates_to_str=True),
        }

    def signed_headers(doc, hook, data=None):
        signature = base64.b64encode(
            hmac.new(
                hook.webhook_secret.encode(),
                frappe.as_json(data).encode(),
                hashlib.sha256,
            ).digest()
        )
        return {"X-Frappe-Webhook-Signature": signature}

    frappe_webhook = types.ModuleType("frappe.integrations.doctype.webhook.webhook")
    frappe_webhook.get_webhook_data = data_renderer
    frappe_webhook.get_webhook_headers = signed_headers
    for name in (
        "frappe.integrations",
        "frappe.integrations.doctype",
        "frappe.integrations.doctype.webhook",
    ):
        parent = types.ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(
        sys.modules, "frappe.integrations.doctype.webhook.webhook", frappe_webhook
    )
    frappe.integrations = sys.modules["frappe.integrations"]
    frappe.integrations.doctype = sys.modules["frappe.integrations.doctype"]
    frappe.integrations.doctype.webhook = sys.modules[
        "frappe.integrations.doctype.webhook"
    ]
    frappe.integrations.doctype.webhook.webhook = frappe_webhook

    result = module.verify_evidence_webhook_render(
        webhook.name, "Purchase Invoice", "PINV-1"
    )
    assert result == {
        "event": "on_submit",
        "doctype": "Purchase Invoice",
        "name": "PINV-1",
        "docstatus": 1,
        "company": module.COMPANY_NAME,
        "signed": True,
        "transmitted": False,
    }


def test_rollback_verifier_installs_renders_signs_and_rolls_back_without_network(
    monkeypatch,
):
    module, database, frappe = _load_module(monkeypatch)

    def data_renderer(doc, hook):
        return {
            "event": hook.webhook_docevent,
            "erp_timezone": "UTC",
            "document": doc.as_dict(convert_dates_to_str=True),
        }

    def signed_headers(doc, hook, data=None):
        signature = base64.b64encode(
            hmac.new(
                hook.webhook_secret.encode(),
                frappe.as_json(data).encode(),
                hashlib.sha256,
            ).digest()
        )
        return {"X-Frappe-Webhook-Signature": signature}

    frappe_webhook = types.ModuleType("frappe.integrations.doctype.webhook.webhook")
    frappe_webhook.get_webhook_data = data_renderer
    frappe_webhook.get_webhook_headers = signed_headers
    for name in (
        "frappe.integrations",
        "frappe.integrations.doctype",
        "frappe.integrations.doctype.webhook",
    ):
        parent = types.ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(
        sys.modules, "frappe.integrations.doctype.webhook.webhook", frappe_webhook
    )
    frappe.integrations = sys.modules["frappe.integrations"]
    frappe.integrations.doctype = sys.modules["frappe.integrations.doctype"]
    frappe.integrations.doctype.webhook = sys.modules[
        "frappe.integrations.doctype.webhook"
    ]
    frappe.integrations.doctype.webhook.webhook = frappe_webhook

    result = module.verify_evidence_webhook_rollback(
        "https://company.example/evidence",
        "private-test-secret",
        "Sales Invoice",
    )
    assert result == {
        "installed_webhooks": 43,
        "event": "on_submit",
        "doctype": "Sales Invoice",
        "name": "TAC-COMPANY-EVIDENCE-ROLLBACK-FIXTURE",
        "docstatus": 1,
        "company": module.COMPANY_NAME,
        "signed": True,
        "transmitted": False,
        "fixture": "unsaved_native_posted_state",
        "schema_field_installed": True,
        "schema_field_persisted": True,
        "rolled_back": True,
    }
    assert list(database.docs) == []
    assert "custom_customer_service_company" in database.custom_fields["Issue"]


def test_existing_service_issue_recovery_uses_actual_linked_sales_order_company(monkeypatch):
    module, database, _ = _load_module(monkeypatch)
    database.sales_order_companies = {
        "SO-COMPANY": module.COMPANY_NAME,
        "SO-OTHER": "Other Company",
    }
    database.rows = {"Issue": [
        {"name": "ISSUE-COMPANY", "custom_customer_service_order": "SO-COMPANY", "custom_customer_service_type": "Return"},
        {"name": "ISSUE-OTHER", "custom_customer_service_order": "SO-OTHER", "custom_customer_service_type": "Exchange"},
    ]}
    database.docs["Issue:ISSUE-COMPANY"] = FakeDoc({
        "doctype": "Issue", "name": "ISSUE-COMPANY", "modified": "2026-10-30T10:00:00",
        "modified_by": "Administrator", "custom_customer_service_order": "SO-COMPANY",
    }, database)
    database.docs["Issue:ISSUE-OTHER"] = FakeDoc({
        "doctype": "Issue", "name": "ISSUE-OTHER", "modified": "2026-10-30T10:00:00",
        "modified_by": "Administrator", "custom_customer_service_order": "SO-OTHER",
    }, database)

    recovered = module.collect_company_issue_events()
    assert len(recovered["issue_events"]) == 1
    assert recovered["issue_events"][0]["event"] == "on_update"
    assert recovered["issue_events"][0]["linked_company"] == module.COMPANY_NAME
    assert recovered["issue_events"][0]["document"]["name"] == "ISSUE-COMPANY"


def test_issue_webhook_native_render_and_rollback_use_linked_company_scope(monkeypatch):
    module, database, frappe = _load_module(monkeypatch)

    def data_renderer(doc, hook):
        data = {
            "event": hook.webhook_docevent,
            "erp_timezone": "UTC",
            "document": doc.as_dict(convert_dates_to_str=True),
        }
        if hook.webhook_doctype == "Issue":
            data["linked_company"] = doc.custom_customer_service_company
        return data

    def signed_headers(doc, hook, data=None):
        signature = base64.b64encode(
            hmac.new(hook.webhook_secret.encode(), frappe.as_json(data).encode(), hashlib.sha256).digest()
        )
        return {"X-Frappe-Webhook-Signature": signature}

    frappe_webhook = types.ModuleType("frappe.integrations.doctype.webhook.webhook")
    frappe_webhook.get_webhook_data = data_renderer
    frappe_webhook.get_webhook_headers = signed_headers
    for name in (
        "frappe.integrations",
        "frappe.integrations.doctype",
        "frappe.integrations.doctype.webhook",
    ):
        parent = types.ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(sys.modules, "frappe.integrations.doctype.webhook.webhook", frappe_webhook)
    frappe.integrations = sys.modules["frappe.integrations"]
    frappe.integrations.doctype = sys.modules["frappe.integrations.doctype"]
    frappe.integrations.doctype.webhook = sys.modules["frappe.integrations.doctype.webhook"]
    frappe.integrations.doctype.webhook.webhook = frappe_webhook

    result = module.verify_evidence_webhook_rollback(
        "https://company.example/evidence", "private-test-secret", "Issue"
    )
    assert result == {
        "installed_webhooks": 43,
        "event": "on_update",
        "doctype": "Issue",
        "name": "TAC-COMPANY-EVIDENCE-ROLLBACK-FIXTURE",
        "docstatus": 0,
        "company": module.COMPANY_NAME,
        "signed": True,
        "transmitted": False,
        "fixture": "unsaved_native_issue_state",
        "schema_field_installed": True,
        "schema_field_persisted": True,
        "rolled_back": True,
    }
    assert list(database.docs) == []
    assert "custom_customer_service_company" in database.custom_fields["Issue"]
