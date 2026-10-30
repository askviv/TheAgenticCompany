from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = (Path(__file__).parents[1] /
               "deploy/enterprise-simulation/erpnext/admin/company_issue_notifications.py")
BRIDGE_FIELDS = {
    "custom_company_issue_key", "custom_responsible_employee", "custom_source_event_id",
    "custom_requested_outcome", "custom_issue_notification_marker", "custom_affected_orders_json",
}


class FakeDoc:
    def __init__(self, frappe, values):
        object.__setattr__(self, "_frappe", frappe)
        data = dict(values)
        for key, value in data.items():
            if key == "recipients" and isinstance(value, list):
                data[key] = [FakeRow(row) for row in value]
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name):
        return self._data.get(name)

    def __setattr__(self, name, value):
        self._data[name] = value

    def get(self, name, default=None):
        return self._data.get(name, default)

    def set(self, name, value):
        self._data[name] = value

    def append(self, fieldname, values):
        self._data.setdefault(fieldname, []).append(FakeRow(values))

    def insert(self, ignore_permissions=False):
        self._data.setdefault("name", self._data.get("subject") or self._data.get("name"))
        self._frappe.docs[self.doctype].append(self)
        return self

    def save(self, ignore_permissions=False):
        return self

    @property
    def to(self):
        return [row.recipient for row in self.recipients]

    def send(self):
        self.send_count = (getattr(self, "send_count", None) or 0) + 1
        if self._frappe.notification_send_mode == "error":
            self._frappe.docs["Error Log"].append(FakeDoc(self._frappe, {
                "doctype": "Error Log", "name": "ERROR-0001", "title": "Failed to send Notification",
                "creation": self._frappe.now,
            }))
            return
        for row in self.recipients:
            row.status = "Sent"
        self.status = "Sent"


class FakeRow(dict):
    __getattr__ = dict.get

    def __setattr__(self, name, value):
        self[name] = value


class FakeFrappe:
    def __init__(self):
        self.now = datetime(2026, 10, 30, tzinfo=timezone.utc)
        self.notification_send_mode = "queue"
        self.docs = {name: [] for name in (
            "Issue", "Notification", "Notification Type", "Email Queue", "Error Log", "User", "Communication",
        )}
        self.meta_fields = {"Issue": set(BRIDGE_FIELDS)}
        self.db = FakeDB(self)
        self.utils = types.SimpleNamespace(now_datetime=lambda: self.now)

    def get_meta(self, doctype):
        return types.SimpleNamespace(has_field=lambda field: field in self.meta_fields.setdefault(doctype, set()))

    def db_doc(self, doctype, name):
        return next(row for row in self.docs[doctype] if row.name == name)

    def get_doc(self, values, name=None):
        if isinstance(values, str):
            if values == "Notification":
                return self.db_doc(values, name)
            return self.db_doc(values, name)
        if values["doctype"] == "Notification":
            doc = FakeDoc(self, values)

            def get_recipients(issue, context):
                assert context == {}
                return [issue.custom_responsible_employee], [], []

            def send(issue):
                doc.send_count = (doc.send_count or 0) + 1
                if self.notification_send_mode == "error":
                    self.docs["Error Log"].append(FakeDoc(self, {
                        "doctype": "Error Log", "name": "ERROR-0001",
                        "title": "Failed to send Notification", "creation": self.now,
                    }))
                    return
                user = self.db.get_value("User", issue.custom_responsible_employee,
                                         ["email", "enabled"], as_dict=True)
                queue = FakeDoc(self, {
                    "doctype": "Email Queue", "name": "EMAIL-0001", "reference_doctype": "Issue",
                    "reference_name": issue.name, "message_id": "msg-0001@example.test",
                    "message": "Message-ID: <msg-0001@example.test>\r\nSubject: test\r\n\r\nbody",
                    "communication": "COMM-0001", "status": "Not Sent", "recipients": [
                        {"recipient": user.email, "status": "Not Sent"},
                    ],
                })
                self.docs["Communication"].append(FakeDoc(self, {
                    "doctype": "Communication", "name": "COMM-0001", "subject": issue.subject,
                    "reference_doctype": "Issue", "reference_name": issue.name,
                }))
                self.docs["Email Queue"].append(queue)

            doc.get_list_of_recipients = get_recipients
            object.__setattr__(doc, "send", send)
            return doc
        return FakeDoc(self, values)

    def get_all(self, doctype, filters=None, fields=None, limit=None, order_by=None):
        filters = filters or {}
        found = []
        for doc in self.docs[doctype]:
            matches = True
            for field, wanted in filters.items():
                actual = getattr(doc, field, None)
                if isinstance(wanted, list):
                    operator, value = wanted
                    if operator == "is" and value == "set":
                        matches = matches and actual not in (None, "")
                    elif operator == ">=":
                        matches = matches and actual >= value
                else:
                    matches = matches and actual == wanted
            if matches:
                found.append(FakeRow({
                    field: getattr(doc, field, None) for field in (fields or ["name"])
                }))
        if limit:
            found = found[:limit]
        return found


class FakeDB:
    def __init__(self, frappe):
        self.frappe = frappe

    def exists(self, doctype, name):
        if isinstance(name, dict):
            return any(all(getattr(doc, key, None) == value for key, value in name.items())
                       for doc in self.frappe.docs[doctype])
        return any(doc.name == name for doc in self.frappe.docs[doctype])

    def get_value(self, doctype, filters, fields, as_dict=False):
        if doctype == "User":
            if isinstance(filters, dict):
                name = filters.get("name")
            else:
                name = filters
            doc = next((row for row in self.frappe.docs["User"] if row.name == name), None)
            if not doc:
                return None
            if as_dict:
                return types.SimpleNamespace(**{field: getattr(doc, field, None) for field in fields})
            if isinstance(fields, list):
                return [getattr(doc, field, None) for field in fields]
            return getattr(doc, fields, None)
        return None


def load_module(monkeypatch):
    frappe = FakeFrappe()
    frappe.docs["Notification Type"].append(FakeDoc(frappe, {
        "doctype": "Notification Type", "name": "Alert",
    }))
    frappe.docs["User"].append(FakeDoc(frappe, {
        "doctype": "User", "name": "employee@example.test", "email": "employee@example.test", "enabled": 1,
    }))
    module_frappe = types.ModuleType("frappe")
    module_frappe.db = frappe.db
    module_frappe.get_meta = frappe.get_meta
    module_frappe.get_doc = frappe.get_doc
    module_frappe.get_all = frappe.get_all
    module_frappe.utils = frappe.utils
    module_frappe.docs = frappe.docs
    module_frappe.notification_send_mode = frappe.notification_send_mode
    monkeypatch.setitem(sys.modules, "frappe", module_frappe)

    package_names = (
        "frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field",
    )
    for name in package_names:
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    def create_custom_fields(items):
        for doctype, fields in items.items():
            for field in fields:
                frappe.meta_fields.setdefault(doctype, set()).add(field["fieldname"])
    custom_field.create_custom_fields = create_custom_fields
    monkeypatch.setitem(sys.modules, "frappe.custom.doctype.custom_field.custom_field", custom_field)

    monkeypatch.delitem(sys.modules, "company_issue_notifications", raising=False)
    spec = importlib.util.spec_from_file_location("company_issue_notifications", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "company_issue_notifications", module)
    spec.loader.exec_module(module)
    return module, frappe


def owned_issue(frappe):
    issue = FakeDoc(frappe, {
        "doctype": "Issue", "name": "ISS-2026-00001", "subject": "Supplier disruption needs attention",
        "description": "A source disruption affects Purchase Order PO-00001.",
        "custom_company_issue_key": "company:ecommerce:event-1",
        "custom_responsible_employee": "employee@example.test",
        "custom_source_event_id": "event-1", "custom_requested_outcome": "Please review the affected order.",
        "custom_issue_notification_marker": "event-1:notification",
        "custom_affected_orders_json": "[{\"doctype\":\"Purchase Order\",\"name\":\"PO-00001\"}]",
    })
    frappe.docs["Issue"].append(issue)
    return issue


def test_native_notification_queues_exact_message_and_stores_receipt(monkeypatch):
    module, frappe = load_module(monkeypatch)
    module.ensure_notification_contract()
    issue = owned_issue(frappe)

    receipt = module.queue_issue_notification(issue)
    repeated = module.queue_issue_notification(issue)

    assert receipt == repeated
    assert receipt == {
        "issue_key": issue.custom_company_issue_key,
        "issue_name": issue.name,
        "email_queue": "EMAIL-0001",
        "message_id": "<msg-0001@example.test>",
        "email_queue_message_id": "msg-0001@example.test",
        "subject": issue.subject,
        "recipient_email": "employee@example.test",
        "status": "Not Sent",
        "business_description": module.BUSINESS_DESCRIPTION,
        "requested_outcome": issue.custom_requested_outcome,
        "affected_orders": [{"doctype": "Purchase Order", "name": "PO-00001"}],
        "recipient_count": 1,
        "sent_recipient_count": 0,
    }
    assert issue.custom_issue_email_queue == "EMAIL-0001"
    assert issue.custom_issue_email_message_id == "<msg-0001@example.test>"
    assert frappe.db_doc("Notification", module.NOTIFICATION_NAME).send_count == 1
    notification = frappe.db_doc("Notification", module.NOTIFICATION_NAME)
    assert notification.message_type == "Plain Text"
    assert module.BUSINESS_DESCRIPTION in notification.message
    assert "custom_source_snapshot" not in notification.message
    assert issue.description not in notification.message


def test_native_notification_failure_reports_error_log_reference(monkeypatch):
    module, frappe = load_module(monkeypatch)
    module.ensure_notification_contract()
    issue = owned_issue(frappe)
    frappe.notification_send_mode = "error"

    try:
        module.queue_issue_notification(issue)
    except RuntimeError as exc:
        assert "Email Queues" in str(exc)
        assert "ERROR-0001" in str(exc)
    else:
        raise AssertionError("Native Notification without an Email Queue must not be reported as success")


def test_owned_sender_sends_only_matching_queue_and_reports_native_status(monkeypatch):
    module, frappe = load_module(monkeypatch)
    module.ensure_notification_contract()
    issue = owned_issue(frappe)
    receipt = module.queue_issue_notification(issue)
    queue = frappe.db_doc("Email Queue", receipt["email_queue"])

    try:
        module.send_owned_notification(queue.name, "another:company:issue")
    except ValueError as exc:
        assert "exactly one native Issue" in str(exc)
    else:
        raise AssertionError("A queue must not be sent against another company Issue key")

    sent = module.send_owned_notification(queue.name, issue.custom_company_issue_key)
    repeated = module.send_owned_notification(queue.name, issue.custom_company_issue_key)

    assert sent["status"] == "Sent"
    assert sent["sent_recipient_count"] == 1
    assert repeated == sent


def test_native_mime_message_id_is_canonical_and_must_match_queue_identity(monkeypatch):
    module, frappe = load_module(monkeypatch)
    module.ensure_notification_contract()
    issue = owned_issue(frappe)
    receipt = module.queue_issue_notification(issue)
    queue = frappe.db_doc("Email Queue", receipt["email_queue"])

    assert module.email_queue_message_ids(queue) == (
        "<msg-0001@example.test>", "msg-0001@example.test",
    )
    queue.message = "Message-ID: <different@example.test>\r\n\r\nbody"
    try:
        module.email_queue_message_ids(queue)
    except RuntimeError as exc:
        assert "disagrees" in str(exc)
    else:
        raise AssertionError("A MIME Message-ID mismatch must not be reported as the queue receipt")


def test_receipt_listing_reads_owned_issue_queues_only(monkeypatch):
    module, frappe = load_module(monkeypatch)
    module.ensure_notification_contract()
    issue = owned_issue(frappe)
    owned = module.queue_issue_notification(issue)
    unrelated = FakeDoc(frappe, {
        "doctype": "Email Queue", "name": "OTHER-EMAIL", "reference_doctype": "User",
        "reference_name": "employee@example.test", "message_id": "<other@example.test>",
        "status": "Not Sent", "recipients": [],
    })
    frappe.docs["Email Queue"].append(unrelated)

    receipts = module.list_owned_notification_receipts()

    assert [row["email_queue"] for row in receipts] == [owned["email_queue"]]
