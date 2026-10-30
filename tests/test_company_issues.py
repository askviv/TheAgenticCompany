from __future__ import annotations

import importlib.util
import html
import json
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


ADMIN = Path(__file__).parents[1] / "deploy/enterprise-simulation/erpnext/admin"
CONTRACTS = Path(__file__).parents[1] / "servers/simulation"
MODULE = ADMIN / "company_issues.py"


class FakeDocument:
    def __init__(self, frappe_module, values):
        self._frappe = frappe_module
        self._data = dict(values)

    def get(self, field, default=None):
        return self._data.get(field, default)

    def __getattr__(self, name):
        return self._data.get(name)

    def __setattr__(self, name, value):
        if name in {"_frappe", "_data"}:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def insert(self, ignore_permissions=False):
        doctype = self._data["doctype"]
        self._data.setdefault("name", f"{doctype}-{len(self._frappe.documents[doctype]) + 1}")
        self._frappe.documents[doctype].append(self)
        return self

    def save(self, ignore_permissions=False):
        return self

    def add_comment(self, comment_type, text):
        self._frappe.comments.append({
            "reference_doctype": self.doctype, "reference_name": self.name,
            "comment_type": comment_type, "content": text,
        })


class FakeDB:
    def __init__(self, frappe_module):
        self.frappe = frappe_module

    def _matches(self, document, filters):
        return all(document.get(key) == value for key, value in filters.items())

    def exists(self, doctype, filters):
        if isinstance(filters, str):
            filters = {"name": filters}
        return any(self._matches(document, filters) for document in self.frappe.documents[doctype])

    def get_value(self, doctype, filters, field, **_kwargs):
        if isinstance(filters, str):
            filters = {"name": filters}
        for document in self.frappe.documents[doctype]:
            if self._matches(document, filters):
                return document.get(field)
        return None


class FakeShare:
    def __init__(self, frappe_module):
        self.frappe = frappe_module
        self.calls = []

    def add(self, doctype, name, user, *, read, write, share, notify):
        self.calls.append((doctype, name, user, read, write, share, notify))
        key = (doctype, name, user)
        prior = self.frappe.shares.get(key, {})
        self.frappe.shares[key] = {
            "read": max(int(read), prior.get("read", 0)),
            "write": max(int(write), prior.get("write", 0)),
            "share": max(int(share), prior.get("share", 0)),
        }


def _load_adapter(monkeypatch):
    monkeypatch.syspath_prepend(str(ADMIN))
    monkeypatch.syspath_prepend(str(CONTRACTS))
    monkeypatch.delitem(sys.modules, "source_records", raising=False)
    monkeypatch.delitem(sys.modules, "company_issues", raising=False)
    frappe = types.ModuleType("frappe")
    frappe.documents = {name: [] for name in ("Company", "User", "Issue", "Purchase Order", "Sales Order")}
    frappe.comments = []
    frappe.db = FakeDB(frappe)
    def json_default(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    frappe.as_json = lambda value: json.dumps(value, default=json_default)
    frappe.shares = {("Issue", "UNRELATED", "other@example.test"): {"read": 1, "write": 0, "share": 0}}
    frappe.share = FakeShare(frappe)

    def has_permission(doctype, ptype, *, doc, user):
        grants = frappe.shares.get((doctype, doc.name, user), {})
        return bool(grants.get(ptype, 0))

    frappe.has_permission = has_permission

    def get_doc(doctype_or_values, name=None):
        if isinstance(doctype_or_values, dict):
            return FakeDocument(frappe, doctype_or_values)
        return next(row for row in frappe.documents[doctype_or_values] if row.name == name)

    def get_all(doctype, *, filters, fields, **_kwargs):
        rows = []
        for document in frappe.documents[doctype]:
            if frappe.db._matches(document, filters):
                rows.append({field: document.get(field) for field in fields})
        return rows

    frappe.get_doc = get_doc
    frappe.get_all = get_all
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    utils = types.ModuleType("frappe.utils")
    utils.get_link_to_form = lambda doctype, name: f"<a href='/app/{doctype.lower().replace(' ', '-')}/{name}'>{name}</a>"
    utils.escape_html = html.escape
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    spec = importlib.util.spec_from_file_location("company_issues", MODULE)
    adapter = importlib.util.module_from_spec(spec)
    sys.modules["company_issues"] = adapter
    spec.loader.exec_module(adapter)
    return adapter, frappe


def _condition():
    return {
        "event_id": "source-event-1", "source": "qwenlm-ecommercebench-calendar",
        "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "occurred_at": "2026-12-20T00:00:00Z", "effective_until": "2026-12-22T00:00:00Z",
        "duration_days": 2, "event_name": "Cold Wave", "store_type": "auto_hardware",
        "affected_categories": "all", "delivery_delay_multiplier": "2.0",
    }


def _source_order(name, deal_id, condition, *, source_condition_ids=None, company_id="company"):
    source_condition_ids = source_condition_ids if source_condition_ids is not None else [condition["event_id"]]
    snapshot = {
        "source": "sakanaai-coffeebench-native-economy",
        "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8", "kind": "order",
        "native_record_type": "deal", "native_record_id": deal_id,
        "occurred_at": "2026-12-20T10:00:00+00:00",
        "active_source_condition_ids": source_condition_ids,
        "active_source_conditions": [condition] if condition["event_id"] in source_condition_ids else [],
        "native_record": {
            "deal_id": deal_id, "accepted_at_virtual_minute": 600,
            "delivery_at_virtual_minute": 3780, "item_id": "roasted_coffee_kg",
            "quantity": 4, "unit_price": "11.25", "total_price": "45.00",
        },
    }
    return FakeDocument(None, {
        "doctype": "Purchase Order", "name": name, "company": "The Agent Company",
        "custom_source_system": "coffeebench", "custom_source_id": deal_id,
        "custom_company_id": company_id,
        "custom_source_snapshot": json.dumps(snapshot, sort_keys=True),
        "items": [{"item_code": "roasted_coffee_kg", "qty": 4, "rate": Decimal("11.25"),
                   "schedule_date": date(2026, 12, 22)}],
        "delivery_date": None,
        "status": "Draft",
    })


def test_issue_links_exact_source_condition_orders_and_appends_without_rewriting_commitments(monkeypatch):
    adapter, frappe = _load_adapter(monkeypatch)
    frappe.documents["Company"].append(FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company"}))
    frappe.documents["User"].append(FakeDocument(frappe, {"doctype": "User", "name": "chen@example.test",
                                                             "enabled": 1}))
    condition = _condition()
    first_order = _source_order("PO-CB-1", "deal-1", condition)
    unrelated_order = _source_order("PO-CB-UNRELATED", "deal-other", condition,
                                    source_condition_ids=["different-condition"])
    other_company_order = _source_order("PO-OTHER", "deal-other-company", condition, company_id="other")
    frappe.documents["Purchase Order"].extend((first_order, unrelated_order, other_company_order))
    binding = {"company_id": "company", "erp_company": "The Agent Company"}

    affected = adapter.find_affected_company_orders(binding, condition)
    assert [(row["doctype"], row["name"], row["source_id"]) for row in affected] == [
        ("Purchase Order", "PO-CB-1", "deal-1"),
    ]
    assert affected[0]["original_delivery_commitment"]["items"][0] == {
        "item_code": "roasted_coffee_kg", "qty": 4, "rate": Decimal("11.25"),
        "schedule_date": date(2026, 12, 22),
    }

    first = adapter.upsert_company_issue(
        binding, condition, responsible_employee="chen@example.test", responsible_role="Delivery User",
        requested_outcome="Review affected source-linked commitments.",
    )
    assert first["created"] is True
    issue = next(row for row in frappe.documents["Issue"] if row.name == first["name"])
    original_snapshot = issue.custom_source_snapshot
    original_commitment = first_order.items[0].get("schedule_date")
    assert issue.custom_source_event_id == condition["event_id"]
    assert issue.custom_responsible_employee == "chen@example.test"
    assert issue.custom_responsible_role == "Delivery User"
    assert issue.custom_issue_notification_marker == first["notification_key"]
    assert frappe.shares[("Issue", issue.name, "chen@example.test")] == {"read": 1, "write": 1, "share": 0}
    assert frappe.shares[("Purchase Order", "PO-CB-1", "chen@example.test")] == {
        "read": 1, "write": 0, "share": 0,
    }
    assert frappe.shares[("Issue", "UNRELATED", "other@example.test")] == {"read": 1, "write": 0, "share": 0}
    assert len(frappe.share.calls) == 2
    assert "2026-12-22" in issue.description
    assert "CoffeeBench deal deal-1" in issue.description
    assert issue.custom_affected_orders_json
    assert json.loads(issue.custom_affected_orders_json)[0]["native_delivery_at_virtual_minute"] == 3780

    second_order = _source_order("PO-CB-2", "deal-2", condition)
    frappe.documents["Purchase Order"].append(second_order)
    issue.status = "Closed"  # User-owned Issue state must survive appending a later order.
    issue.description = "Employee-authored handling notes."
    first_order.items[0]["schedule_date"] = date(2026, 12, 23)  # Live native commitment was edited after the source snapshot.
    second = adapter.upsert_company_issue(
        binding, condition, responsible_employee="chen@example.test", responsible_role="Delivery User",
        requested_outcome="Review affected source-linked commitments.",
    )
    assert second["created"] is False
    assert second["name"] == first["name"]
    assert issue.status == "Closed"
    assert issue.description == "Employee-authored handling notes."
    assert len(frappe.comments) == 1
    assert frappe.comments[0]["comment_type"] == "Info"
    assert "PO-CB-2" in frappe.comments[0]["content"]
    assert "PO-CB-1" not in frappe.comments[0]["content"]
    assert frappe.shares[("Purchase Order", "PO-CB-2", "chen@example.test")] == {
        "read": 1, "write": 0, "share": 0,
    }
    assert issue.custom_source_snapshot == original_snapshot
    appended = json.loads(issue.custom_affected_orders_json)
    assert [(row["doctype"], row["name"], row["source_id"]) for row in appended] == [
        ("Purchase Order", "PO-CB-1", "deal-1"), ("Purchase Order", "PO-CB-2", "deal-2"),
    ]
    assert issue.description == "Employee-authored handling notes."
    assert first_order.items[0].get("schedule_date") == date(2026, 12, 23)
    assert appended[0]["original_delivery_commitment"]["items"][0]["schedule_date"] == "2026-12-22"

    repeated = adapter.upsert_company_issue(
        binding, condition, responsible_employee="chen@example.test", responsible_role="Delivery User",
        requested_outcome="Review affected source-linked commitments.",
    )
    assert repeated["created"] is False
    assert len(frappe.share.calls) == 3  # new PO only; existing Issue and PO shares are reused
    assert len(frappe.comments) == 1
    assert json.loads(issue.custom_affected_orders_json) == appended
    assert issue.description == "Employee-authored handling notes."


def test_existing_issue_rejects_changed_immutable_coffee_source_snapshot(monkeypatch):
    adapter, frappe = _load_adapter(monkeypatch)
    frappe.documents["Company"].append(FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company"}))
    frappe.documents["User"].append(FakeDocument(frappe, {"doctype": "User", "name": "chen@example.test",
                                                             "enabled": 1}))
    condition = _condition()
    order = _source_order("PO-CB-1", "deal-1", condition)
    frappe.documents["Purchase Order"].append(order)
    binding = {"company_id": "company", "erp_company": "The Agent Company"}
    adapter.upsert_company_issue(
        binding, condition, responsible_employee="chen@example.test", responsible_role="Delivery User",
        requested_outcome="Review source-linked commitments.",
    )
    changed_snapshot = json.loads(order.custom_source_snapshot)
    changed_snapshot["native_record"]["quantity"] = 5
    order.custom_source_snapshot = json.dumps(changed_snapshot, sort_keys=True)
    try:
        adapter.upsert_company_issue(
            binding, condition, responsible_employee="chen@example.test", responsible_role="Delivery User",
            requested_outcome="Review source-linked commitments.",
        )
    except adapter.CompanyIssueConflict:
        pass
    else:
        raise AssertionError("A changed immutable source snapshot must conflict with the existing Issue record")


def test_company_issue_key_deduplicates_recipient_email_case(monkeypatch):
    adapter, _ = _load_adapter(monkeypatch)
    assert adapter._notification_key("company", "event", " Chen@example.test ") == adapter._notification_key(
        "company", "event", "chen@EXAMPLE.test",
    )


def test_affected_order_json_uses_restricted_native_field_permission_level(monkeypatch):
    adapter, frappe = _load_adapter(monkeypatch)
    custom_fields = []
    field_names = set()

    class Meta:
        def has_field(self, fieldname):
            return fieldname in field_names

    meta = Meta()
    frappe.get_meta = lambda _doctype: meta

    def create_custom_fields(definitions, update=False):
        for doctype, fields in definitions.items():
            assert doctype == "Issue"
            custom_fields.extend(fields)
            field_names.update(field["fieldname"] for field in fields)

    custom_module = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_module.create_custom_fields = create_custom_fields
    for name in (
        "frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field",
    ):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "frappe.custom.doctype.custom_field.custom_field", custom_module)
    monkeypatch.setattr(adapter, "ensure_source_fields", lambda *_args: None)
    adapter.ensure_company_issue_contract()
    affected_orders_field = next(field for field in custom_fields
                                 if field["fieldname"] == "custom_affected_orders_json")
    assert affected_orders_field["hidden"] == 1
    assert affected_orders_field["permlevel"] == 1
    assert adapter._notification_key("company", "event", "chen@example.test") != adapter._notification_key(
        "other-company", "event", "chen@example.test",
    )


def test_legacy_coffee_order_without_condition_facts_does_not_block_supported_matches(monkeypatch):
    adapter, frappe = _load_adapter(monkeypatch)
    frappe.documents["Company"].append(FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company"}))
    condition = _condition()
    legacy = _source_order("PO-LEGACY", "legacy-deal", condition)
    snapshot = json.loads(legacy.custom_source_snapshot)
    snapshot.pop("active_source_condition_ids")
    snapshot.pop("active_source_conditions")
    legacy.custom_source_snapshot = json.dumps(snapshot)
    current = _source_order("PO-CURRENT", "current-deal", condition)
    frappe.documents["Purchase Order"].extend((legacy, current))
    assert [row["name"] for row in adapter.find_affected_company_orders(
        {"company_id": "company", "erp_company": "The Agent Company"}, condition,
    )] == ["PO-CURRENT"]


def test_reconcile_maps_only_global_native_source_events_to_issues(monkeypatch):
    adapter, frappe = _load_adapter(monkeypatch)
    frappe.documents["Company"].append(FakeDocument(frappe, {"doctype": "Company", "name": "The Agent Company"}))
    frappe.documents["User"].append(FakeDocument(frappe, {"doctype": "User", "name": "delivery@example.test",
                                                             "enabled": 1}))
    condition = _condition()
    frappe.documents["Purchase Order"].append(_source_order("PO-CB-1", "deal-1", condition))
    event = {
        "event_type": "SUPPLIER_SUPPLY_DISRUPTED", "event_id": condition["event_id"],
        "source": condition["source"], "upstream_commit": condition["upstream_commit"],
        "simulated_time": condition["occurred_at"], "payload": {
            "upstream_calendar_duration_days": condition["duration_days"],
            "upstream_event_name": condition["event_name"], "store_type": condition["store_type"],
            "affected_categories": "all", "delivery_delay_multiplier": condition["delivery_delay_multiplier"],
        },
    }
    scoped = {**event, "event_id": "scoped-only", "payload": {**event["payload"],
                                                                    "affected_categories": ["auto_hardware"]}}
    receipts = adapter.reconcile_disruption_issues(
        [event, dict(event), scoped], {"company_id": "company", "erp_company": "The Agent Company"},
        responsible_employee="delivery@example.test", responsible_role="Delivery User",
        requested_outcome="Review source-linked commitments.",
    )
    assert len(receipts) == 1
    assert receipts[0]["source_event_id"] == condition["event_id"]
    assert receipts[0]["created"] is True
    assert len(frappe.documents["Issue"]) == 1
