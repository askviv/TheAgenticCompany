from __future__ import annotations

import importlib.util
import copy
from datetime import date, datetime
import json
from pathlib import Path
import sys
import types

import pytest


ADMIN = Path(__file__).parents[1] / "deploy/enterprise-simulation/erpnext/admin"


class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


class Doc:
    def __init__(self, db, values):
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_values", {})
        self.update(values)
        self._values["doctype"] = self._values.get("doctype")

    def __getattr__(self, key):
        return self._values.get(key)

    def __setattr__(self, key, value):
        self._values[key] = value

    def get(self, key, default=None):
        return self._values.get(key, default)

    def update(self, values):
        for key, value in values.items():
            if key == "items":
                value = [row if isinstance(row, Row) else Row(row) for row in value]
            self._values[key] = value
        return self

    def insert(self, ignore_permissions=False):
        self._values.setdefault("docstatus", 0)
        self._values.setdefault("name", f"{self.doctype}-{len(self._db.docs.get(self.doctype, {})) + 1}")
        self._db.docs.setdefault(self.doctype, {})[self.name] = self
        return self

    def submit(self):
        if self.doctype == "Sales Invoice" and self.is_return and self._db.fail_credit_submit:
            self._db.fail_credit_submit = False
            raise RuntimeError("synthetic credit-note submission interruption")
        self.submit_count = (self.submit_count or 0) + 1
        self.docstatus = 1
        if self.doctype == "Bank Transaction":
            self.status = "Unreconciled"
        return self

    def reload(self):
        return self


class Database:
    def __init__(self):
        self.docs = {}
        self.fail_credit_submit = False

    def exists(self, doctype, name):
        return name in self.docs.get(doctype, {})


def _load_subject(monkeypatch):
    db = Database()
    frappe = types.ModuleType("frappe")
    frappe.db = db
    frappe.get_precision = lambda doctype, fieldname, **kwargs: 3
    frappe.get_doc = lambda *args: (
        Doc(db, args[0]) if len(args) == 1 and isinstance(args[0], dict)
        else db.docs[args[0]][args[1]]
    )
    invoice_rows = [{
        "parent": "SI-1", "name": "SIROW-1", "qty": 2, "rate": 600,
        "stock_qty": 24, "conversion_factor": 12, "stock_uom": "Nos",
        "sales_order": "SO-1", "item_code": "ECOM-prod-1",
    }]
    frappe.get_all = lambda doctype, **kwargs: invoice_rows if doctype == "Sales Invoice Item" else []
    monkeypatch.setitem(sys.modules, "frappe", frappe)

    finance_sources = types.ModuleType("finance_sources")
    finance_sources._company_currency = lambda company: "GBP"
    finance_sources._conversion_rate = lambda *args: 1
    finance_sources._date = lambda value: str(value)
    monkeypatch.setitem(sys.modules, "finance_sources", finance_sources)
    native_accounts = types.ModuleType("native_accounts")
    native_accounts.map_party_to_currency = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "native_accounts", native_accounts)

    source_records = types.ModuleType("source_records")
    source_records.ensure_source_fields = lambda *args: None
    source_records.insert_source_document = lambda *args, **kwargs: None

    def source_fields(**kwargs):
        return {
            "custom_source_system": kwargs["source_system"],
            "custom_source_id": kwargs["source_id"],
            "custom_source_record_type": kwargs["record_type"],
            "custom_source_snapshot": json.dumps(kwargs["snapshot"], sort_keys=True),
        }

    def get_source_document(doctype, *, source_system, source_id, record_type, task_id,
                           experiment_id, company_id):
        if source_id == "store-1:shipment:7" and doctype == "Sales Order":
            return db.docs["Sales Order"]["SO-1"]
        return next((doc for doc in db.docs.get(doctype, {}).values()
                     if doc.custom_source_system == source_system
                     and doc.custom_source_id == source_id
                     and doc.custom_source_record_type == record_type), None)

    source_records.source_fields = source_fields
    source_records.get_source_document = get_source_document
    monkeypatch.setitem(sys.modules, "source_records", source_records)

    returned_qty = types.ModuleType("erpnext.controllers.sales_and_purchase_return")
    returned_qty.get_returned_qty_map_for_row = lambda *args: {"qty": 0}
    module_names = [
        "erpnext", "erpnext.controllers", "erpnext.stock", "erpnext.stock.doctype",
        "erpnext.stock.doctype.delivery_note", "erpnext.accounts", "erpnext.accounts.doctype",
        "erpnext.accounts.doctype.sales_invoice",
    ]
    for name in module_names:
        parent = types.ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(sys.modules, "erpnext.controllers.sales_and_purchase_return", returned_qty)

    dn_mapper = types.ModuleType("erpnext.stock.doctype.delivery_note.delivery_note")
    dn_mapper.make_sales_return = lambda name: Doc(db, {
        "doctype": "Delivery Note", "name": "DN-RETURN-1", "company": "The Agent Company",
        "docstatus": 0, "is_return": 1, "return_against": name,
        "items": [{"name": "DNRETROW-1", "dn_detail": "DNROW-1",
                   "item_code": "ECOM-prod-1", "qty": -2, "stock_qty": -24,
                   "conversion_factor": 12, "stock_uom": "Nos"}],
    })
    invoice_mapper = types.ModuleType("erpnext.accounts.doctype.sales_invoice.sales_invoice")
    invoice_mapper.make_sales_return = lambda name: Doc(db, {
        "doctype": "Sales Invoice", "name": "SI-RETURN-1", "company": "The Agent Company",
        "docstatus": 0, "is_return": 1, "return_against": name,
        "items": [{"name": "SIRETROW-1", "sales_invoice_item": "SIROW-1",
                   "item_code": "ECOM-prod-1", "qty": -2, "stock_qty": -24,
                   "conversion_factor": 12, "stock_uom": "Nos"}],
    })
    monkeypatch.setitem(sys.modules, "erpnext.stock.doctype.delivery_note.delivery_note", dn_mapper)
    monkeypatch.setitem(sys.modules, "erpnext.accounts.doctype.sales_invoice.sales_invoice", invoice_mapper)

    module_name = "ecommerce_finance_test_subject"
    spec = importlib.util.spec_from_file_location(module_name, ADMIN / "ecommerce_finance.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    db.docs["Sales Order"] = {
        "SO-1": Doc(db, {
            "doctype": "Sales Order", "name": "SO-1", "company": "The Agent Company",
            "custom_ecommerce_store_id": "store-1", "custom_ecommerce_shipment_id": "7",
            "custom_ecommerce_product_id": "prod-1",
            "items": [{"name": "SO-ROW-1", "item_code": "ECOM-prod-1", "conversion_factor": 12,
                       "qty": 2, "stock_qty": 24, "stock_uom": "Nos"}],
        })
    }
    db.docs["Item"] = {
        "ECOM-prod-1": Doc(db, {
            "doctype": "Item", "name": "ECOM-prod-1",
            "custom_source_system": "qwenlm-ecommercebench-native-economy",
            "custom_source_id": "prod-1", "is_stock_item": 1, "stock_uom": "Nos",
        })
    }
    db.docs["Delivery Note"] = {
        "DN-1": Doc(db, {
            "doctype": "Delivery Note", "name": "DN-1", "company": "The Agent Company",
            "docstatus": 1, "is_return": 0, "customer": "MARKET-1", "posting_date": "2026-10-31",
            "items": [{"name": "DNROW-1", "against_sales_order": "SO-1",
                       "item_code": "ECOM-prod-1", "qty": 2, "stock_qty": 24,
                       "conversion_factor": 12, "stock_uom": "Nos"}],
        })
    }
    db.docs["Sales Invoice"] = {
        "SI-1": Doc(db, {
            "doctype": "Sales Invoice", "name": "SI-1", "company": "The Agent Company",
            "docstatus": 1, "is_return": 0, "customer": "MARKET-1", "posting_date": "2026-10-31",
        })
    }
    return module, db, invoice_rows


def _refund_record():
    return {
        "id": "outcome:store-1:refund:4", "source": "qwenlm-ecommercebench-native-economy",
        "kind": "native_outcome", "source_ref": "store-1:shipment:7",
        "occurred_at": "2026-11-01T09:00:00+00:00", "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "currency": "GBP", "amount": "1200", "native_units_per_currency_unit": "1",
        "native_record": {
            "event_type": "refund_processed", "store_id": "store-1", "store_type": "auto_hardware",
            "batch_id": 4, "shipment_id": 7, "product_id": "prod-1", "quantity": 24,
            "refund_per_unit": "50", "refund_amount_native_units": "1200",
            "escrow_reclaimed_native_units": "1000", "commission_reversed_native_units": "200",
            "source_delivery_note_names": ["DN-1"],
        },
    }


def _cancellation_record():
    return {
        "id": "outcome:store-1:shipment_cancelled:7",
        "source": "qwenlm-ecommercebench-native-economy",
        "kind": "native_outcome", "source_ref": "store-1:shipment:7",
        "occurred_at": "2026-11-01T09:00:00+00:00",
        "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "native_record": {
            "event_type": "shipment_cancelled", "store_id": "store-1",
            "store_type": "auto_hardware", "source_shipment_id": "store-1:shipment:7",
            "shipment_id": 7, "product_id": "prod-1", "quantity": 2,
            "unit_price": 50, "sale_date": "2026-10-30", "deadline": "2026-10-31",
        },
    }


def _binding():
    return {
        "company_id": "company", "erp_company": "The Agent Company", "store_type": "auto_hardware",
        "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
    }


def test_bank_transaction_uses_native_import_status_and_retries_draft(monkeypatch):
    module, db, _ = _load_subject(monkeypatch)
    module._ensure_marketplace_bank = lambda *args: ("ACC-MARKET", "E-Commerce marketplace bank")

    def find_source(doctype, *, source_system, source_id, record_type, task_id,
                    experiment_id, company_id):
        return next((doc for doc in db.docs.get(doctype, {}).values()
                     if doc.custom_source_system == source_system
                     and doc.custom_source_id == source_id
                     and doc.custom_source_record_type == record_type), None)

    def insert_source(doctype, values, *, source_system, source_id, record_type,
                      task_id, experiment_id, company_id, snapshot):
        provenance = module.source_fields(
            source_system=source_system, source_id=source_id, record_type=record_type,
            task_id=task_id, experiment_id=experiment_id, company_id=company_id,
            snapshot=snapshot,
        )
        doc = module.frappe.get_doc({"doctype": doctype, **values, **provenance})
        doc.insert(ignore_permissions=True)
        return doc, True

    module.get_source_document = find_source
    module.insert_source_document = insert_source
    mapped = {
        "company_id": "company", "company": "The Agent Company",
        "store_type": "auto_hardware", "currency": "GBP", "source_id": "bank:store-1:daily:2026-10-30",
        "facts": {"movement_type": "daily_native_cash_activity", "amount_native_units": "1.25"},
        "movement_type": "daily_native_cash_activity", "occurred_at": datetime(2026, 10, 30, 10),
        "direction": "deposit", "amount": 1.25, "reference": "native-day:2026-10-30",
    }

    name, created = module._ensure_native_bank_transaction(mapped)
    transaction = db.docs["Bank Transaction"][name]
    assert created is True
    assert transaction.docstatus == 1
    assert transaction.status == "Unreconciled"
    assert transaction.submit_count == 1

    transaction.docstatus = 0
    transaction.status = ""
    retry_name, retried_created = module._ensure_native_bank_transaction(mapped)
    assert (retry_name, retried_created) == (name, False)
    assert transaction.docstatus == 1
    assert transaction.status == "Unreconciled"
    assert transaction.submit_count == 2

    replay_name, replay_created = module._ensure_native_bank_transaction(mapped)
    assert (replay_name, replay_created) == (name, False)
    assert transaction.submit_count == 2

    transaction.docstatus = 2
    transaction.status = "Cancelled"
    with pytest.raises(RuntimeError, match="cancelled or has an unsupported native status"):
        module._ensure_native_bank_transaction(mapped)


def test_refund_returns_pending_until_submitted_invoice_exists(monkeypatch):
    module, db, invoice_rows = _load_subject(monkeypatch)
    db.docs["Sales Invoice"].clear()
    invoice_rows.clear()

    result = module._process_native_outcome(_refund_record(), _binding())

    assert result["status"] == "pending"
    assert "submitted source Sales Invoices" in result["pending_reasons"][0]
    assert "DN-RETURN-1" not in db.docs.get("Delivery Note", {})


def test_refund_exact_stock_qty_survives_uom_conversion_and_unrepresentable_piece_stays_pending(monkeypatch):
    module, db, _ = _load_subject(monkeypatch)
    record = copy.deepcopy(_refund_record())
    facts = record["native_record"]
    facts.update({
        "quantity": 1,
        "refund_amount_native_units": "50",
        "escrow_reclaimed_native_units": "40",
        "commission_reversed_native_units": "10",
    })
    record["amount"] = "50"

    result = module._process_native_outcome(record, _binding())

    assert result["status"] == "pending"
    assert "cannot be represented exactly" in result["pending_reasons"][0]
    assert set(db.docs["Delivery Note"]) == {"DN-1"}
    assert set(db.docs["Sales Invoice"]) == {"SI-1"}


def test_native_cancellation_adds_one_source_comment_without_editing_sales_order(monkeypatch):
    module, db, _ = _load_subject(monkeypatch)
    order = db.docs["Sales Order"]["SO-1"]
    original_quantity = order.items[0].qty

    first = module._process_native_outcome(_cancellation_record(), _binding())
    second = module._process_native_outcome(_cancellation_record(), _binding())

    assert first["schema"] == "native_outcome_v1"
    assert first["status"] == second["status"] == "completed"
    assert first["documents"] == second["documents"]
    assert first["documents"][0]["doctype"] == "Comment"
    comment = db.docs["Comment"][first["documents"][0]["name"]]
    assert comment.reference_doctype == "Sales Order"
    assert comment.reference_name == "SO-1"
    assert "commitments were left unchanged" in comment.content
    assert order.items[0].qty == original_quantity
    assert len(db.docs["Comment"]) == 1


def test_refund_partial_native_write_retries_without_duplicate_half(monkeypatch):
    module, db, _ = _load_subject(monkeypatch)
    db.fail_credit_submit = True
    record = _refund_record()

    with pytest.raises(RuntimeError, match="synthetic credit-note submission interruption"):
        module._process_native_outcome(record, _binding())
    assert db.docs["Delivery Note"]["DN-RETURN-1"].docstatus == 1
    assert db.docs["Sales Invoice"]["SI-RETURN-1"].docstatus == 0

    result = module._process_native_outcome(record, _binding())
    assert result["schema"] == "native_outcome_v1"
    assert result["status"] == "completed"
    assert result["refund_accounting"] == {
        "gross_refund_native_units": "1200",
        "escrow_reclaimed_native_units": "1000",
        "commission_reversed_native_units": "200",
    }
    assert {(row["doctype"], row["name"], row["docstatus"]) for row in result["documents"]} == {
        ("Delivery Note", "DN-RETURN-1", 1), ("Sales Invoice", "SI-RETURN-1", 1),
    }
    returned_dn = db.docs["Delivery Note"]["DN-RETURN-1"]
    credit_note = db.docs["Sales Invoice"]["SI-RETURN-1"]
    assert returned_dn.return_against == "DN-1"
    assert returned_dn.posting_date == "2026-11-01"
    assert returned_dn.items[0].dn_detail == "DNROW-1" and returned_dn.items[0].qty == -2.0
    assert returned_dn.items[0].stock_qty == -24.0
    assert credit_note.return_against == "SI-1"
    assert credit_note.posting_date == "2026-11-01"
    assert credit_note.items[0].sales_invoice_item == "SIROW-1" and credit_note.items[0].qty == -2.0
    assert credit_note.items[0].stock_qty == -24.0
    assert len(db.docs["Delivery Note"]) == 2
    assert len(db.docs["Sales Invoice"]) == 2
    assert "Payment Entry" not in db.docs and "Bank Transaction" not in db.docs
    replay = module._process_native_outcome(record, _binding())
    assert replay["status"] == "completed"
    assert replay["documents"] == result["documents"]
    assert len(db.docs["Delivery Note"]) == 2
    assert len(db.docs["Sales Invoice"]) == 2
