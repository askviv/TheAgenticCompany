import importlib.util
from pathlib import Path
import sys
import types
from datetime import timedelta


ADMIN = Path(__file__).resolve().parents[1]


class FakeDocument:
    def __init__(self, values):
        self.values = values

    def as_dict(self):
        return self.values


class FakeDatabase:
    def __init__(self):
        self.docs = {
            ("Purchase Invoice", "PI-1"): {
                "doctype": "Purchase Invoice", "name": "PI-1", "company": "The Agent Company",
                "supplier": "SUP-1", "modified": "2026-10-30 10:00:00", "docstatus": 1,
                "items": [
                    {"doctype": "Purchase Invoice Item", "name": "PI-ROW-2", "idx": 2,
                     "item_code": "ITEM-2", "qty": "2.000", "amount": "20.00"},
                    {"doctype": "Purchase Invoice Item", "name": "PI-ROW-1", "idx": 1,
                     "item_code": "ITEM-1", "qty": "1.000", "amount": "10.00"},
                ],
                "references": [{"doctype": "Purchase Invoice Reference", "idx": 1,
                                "reference_doctype": "Purchase Order", "reference_name": "PO-1"}],
            },
            ("Purchase Order", "PO-1"): {
                "doctype": "Purchase Order", "name": "PO-1", "company": "The Agent Company",
                "supplier": "SUP-1", "modified": "2026-10-30 09:00:00", "docstatus": 1,
                "items": [{"doctype": "Purchase Order Item", "idx": 1,
                           "item_code": "ITEM-1", "qty": "1.000"}],
            },
            ("Supplier", "SUP-1"): {
                "doctype": "Supplier", "name": "SUP-1", "supplier_name": "Supplier One",
                "supplier_primary_address": "ADDR-1", "accounts": [
                    {"doctype": "Party Account", "idx": 1, "company": "The Agent Company",
                     "account": "2100 - AP - TAC"},
                    {"doctype": "Party Account", "idx": 2, "company": "Other Co",
                     "account": "2100 - AP - OTHER"},
                ],
            },
            ("Address", "ADDR-1"): {
                "doctype": "Address", "name": "ADDR-1", "address_line1": "1 Business Way",
                "links": [{"doctype": "Dynamic Link", "idx": 1,
                           "link_doctype": "Supplier", "link_name": "SUP-1"}],
            },
            ("Payment Entry", "PE-1"): {
                "doctype": "Payment Entry", "name": "PE-1", "company": "The Agent Company",
                "party_type": "Supplier", "party": "SUP-1", "mode_of_payment": "BANK-1",
                "modified": "2026-10-30 11:00:00", "docstatus": 1,
                "references": [{"doctype": "Payment Entry Reference", "idx": 1,
                                "reference_doctype": "Purchase Invoice", "reference_name": "PI-1"}],
            },
            ("Mode of Payment", "BANK-1"): {
                "doctype": "Mode of Payment", "name": "BANK-1", "type": "Bank",
            },
            ("GL Entry", "GL-PI"): {
                "doctype": "GL Entry", "name": "GL-PI", "company": "The Agent Company",
                "voucher_type": "Purchase Invoice", "voucher_no": "PI-1", "debit": "30.00",
                "modified": "2026-10-30 10:00:01",
            },
            ("GL Entry", "GL-PE"): {
                "doctype": "GL Entry", "name": "GL-PE", "company": "The Agent Company",
                "voucher_type": "Payment Entry", "voucher_no": "PE-1", "credit": "30.00",
                "modified": "2026-10-30 11:00:01",
            },
            ("GL Entry", "GL-WRONG-PAIR"): {
                "doctype": "GL Entry", "name": "GL-WRONG-PAIR", "company": "The Agent Company",
                "voucher_type": "Purchase Order", "voucher_no": "PI-1", "debit": "999.00",
            },
            ("GL Entry", "GL-OTHER-CO"): {
                "doctype": "GL Entry", "name": "GL-OTHER-CO", "company": "Other Co",
                "voucher_type": "Purchase Invoice", "voucher_no": "PI-1", "debit": "999.00",
            },
            ("Purchase Invoice", "PI-UNRELATED"): {
                "doctype": "Purchase Invoice", "name": "PI-UNRELATED", "company": "The Agent Company",
                "supplier": "SUP-1", "modified": "2026-10-30 13:00:00", "docstatus": 1,
            },
            ("Customer", "C-OTHER"): {
                "doctype": "Customer", "name": "C-OTHER", "customer_name": "Unrelated",
            },
        }
        self.allowed = {doctype for doctype, _ in self.docs} | {
            "Purchase Invoice", "Purchase Order", "Supplier", "Address", "Payment Entry",
            "Mode of Payment", "GL Entry", "Customer",
            "Purchase Invoice Item", "Purchase Invoice Reference", "Purchase Order Item",
            "Payment Entry Reference", "Party Account",
        }
        self.active = False
        self.sql_calls = []
        self.commit_calls = 0
        self.reads = []

    def sql(self, query, as_list=False):
        self.sql_calls.append(query)
        if query == "SELECT @@in_transaction":
            return [(int(self.active),)]
        if query.startswith("START TRANSACTION"):
            self.active = True
            return []
        return []

    def exists(self, doctype, name):
        if doctype == "DocType":
            return name in self.allowed
        return (doctype, name) in self.docs

    def rollback(self):
        self.active = False


def _load_module(monkeypatch):
    database = FakeDatabase()
    frappe = types.ModuleType("frappe")
    frappe.db = database
    frappe.get_doc = lambda doctype, name: (
        database.reads.append((doctype, name))
        or FakeDocument(database.docs[(doctype, name)])
    )
    fields = {
        "Purchase Invoice": [
            ("company", "Link", "Company"), ("supplier", "Link", "Supplier"),
            ("items", "Table", "Purchase Invoice Item"), ("references", "Table", "Purchase Invoice Reference"),
        ],
        "Purchase Order": [("company", "Link", "Company"), ("supplier", "Link", "Supplier"),
                           ("items", "Table", "Purchase Order Item")],
        "Purchase Receipt": [("company", "Link", "Company")],
        "Sales Order": [("company", "Link", "Company")],
        "Delivery Note": [("company", "Link", "Company")],
        "Sales Invoice": [("company", "Link", "Company")],
        "Payment Entry": [("company", "Link", "Company"), ("party_type", "Data", None),
                          ("party", "Dynamic Link", "party_type"),
                          ("mode_of_payment", "Link", "Mode of Payment"),
                          ("references", "Table", "Payment Entry Reference")],
        "Journal Entry": [("company", "Link", "Company")],
        "Supplier": [("supplier_primary_address", "Link", "Address"), ("accounts", "Table", "Party Account")],
        "Customer": [],
        "Address": [],
        "Mode of Payment": [],
        "GL Entry": [("company", "Link", "Company"), ("voucher_type", "Data", None),
                     ("voucher_no", "Dynamic Link", "voucher_type")],
        "Purchase Invoice Item": [("purchase_order", "Link", "Purchase Order")],
        "Purchase Invoice Reference": [("reference_doctype", "Data", None),
                                       ("reference_name", "Dynamic Link", "reference_doctype")],
        "Purchase Order Item": [],
        "Payment Entry Reference": [("reference_doctype", "Data", None),
                                    ("reference_name", "Dynamic Link", "reference_doctype")],
        "Party Account": [],
    }
    def get_meta(doctype):
        values = fields.get(doctype, [])
        return types.SimpleNamespace(
            fields=[types.SimpleNamespace(fieldname=name, fieldtype=fieldtype, options=options)
                    for name, fieldtype, options in values],
            has_field=lambda fieldname: any(row[0] == fieldname for row in values),
        )
    frappe.get_meta = get_meta

    def get_all(doctype, filters, fields, order_by):
        if doctype in {"Purchase Invoice Item", "Purchase Invoice Reference", "Purchase Order Item",
                       "Payment Entry Reference"}:
            rows = []
            for (parent_doctype, parent_name), document in database.docs.items():
                for parentfield, child_rows in document.items():
                    if not isinstance(child_rows, list):
                        continue
                    for child in child_rows:
                        if child.get("doctype") != doctype:
                            continue
                        row = {**child, "parent": parent_name, "parenttype": parent_doctype,
                               "parentfield": parentfield}
                        if all(row.get(field) == value for field, value in filters.items()):
                            rows.append(types.SimpleNamespace(**{field: row.get(field) for field in fields}))
            return rows
        rows = [record for (record_doctype, _), record in database.docs.items()
                if record_doctype == doctype]
        rows = [row for row in rows if all(row.get(field) == value for field, value in filters.items())]
        rows.sort(key=lambda row: row["name"])
        return [types.SimpleNamespace(name=row["name"]) for row in rows]

    frappe.get_all = get_all
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    spec = importlib.util.spec_from_file_location(
        "evaluation_evidence_test_subject", ADMIN / "evaluation_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, database


def test_capture_is_linked_company_scoped_sorted_and_read_only(monkeypatch):
    module, database = _load_module(monkeypatch)
    refs = [{"doctype": "Purchase Invoice", "name": "PI-1"},
            {"doctype": "Payment Entry", "name": "PE-1"}]
    snapshot = module.capture_company_evaluation_evidence(refs)

    assert snapshot["schema_version"] == 1
    assert snapshot["scope"]["mode"] == "linked_native_documents"
    assert [row["name"] for row in snapshot["documents"]["Purchase Invoice"]] == ["PI-1"]
    assert [row["name"] for row in snapshot["documents"]["Purchase Order"]] == ["PO-1"]
    assert [row["name"] for row in snapshot["documents"]["GL Entry"]] == ["GL-PE", "GL-PI"]
    supplier_accounts = snapshot["documents"]["Supplier"][0]["accounts"]
    assert len(supplier_accounts) == 1 and supplier_accounts[0]["company"] == "The Agent Company"
    assert [row["idx"] for row in snapshot["documents"]["Purchase Invoice"][0]["items"]] == [1, 2]
    assert "PI-UNRELATED" not in repr(snapshot)
    assert "GL-WRONG-PAIR" not in repr(snapshot)
    assert "GL-OTHER-CO" not in repr(snapshot)
    assert "C-OTHER" not in repr(snapshot)
    assert database.active is True
    assert database.commit_calls == 0
    assert "REPEATABLE READ" in database.sql_calls[1]
    assert "READ ONLY, WITH CONSISTENT SNAPSHOT" in database.sql_calls[2]

    database.rollback()
    chained = module.capture_company_evaluation_evidence([
        {"doctype": "Purchase Order", "name": "PO-1"},
    ])
    assert {row["name"] for row in chained["documents"]["Purchase Invoice"]} == {"PI-1"}
    assert {row["name"] for row in chained["documents"]["Payment Entry"]} == {"PE-1"}
    database.rollback()
    database.reads.clear()
    second = module.capture_company_evaluation_evidence(refs)
    assert second == snapshot
    assert database.reads.count(("Purchase Invoice", "PI-1")) == 1


def test_related_party_root_must_be_connected_to_company_financial_record(monkeypatch):
    module, _database = _load_module(monkeypatch)
    try:
        module.capture_company_evaluation_evidence([
            {"doctype": "Purchase Invoice", "name": "PI-1"},
            {"doctype": "Customer", "name": "C-OTHER"},
        ])
    except PermissionError as exc:
        assert "not linked to an in-scope financial document" in str(exc)
    else:
        raise AssertionError("unrelated customer record widened evaluation evidence scope")


def test_capture_refuses_to_start_inside_an_existing_transaction(monkeypatch):
    module, database = _load_module(monkeypatch)
    database.active = True
    try:
        module.capture_company_evaluation_evidence([
            {"doctype": "Purchase Invoice", "name": "PI-1"},
        ])
    except RuntimeError as exc:
        assert "fresh database transaction" in str(exc)
    else:
        raise AssertionError("evidence capture reused an unrelated transaction")


def test_version_manifest_is_stable_and_detects_native_document_changes(monkeypatch):
    module, database = _load_module(monkeypatch)
    snapshot = module.capture_company_evaluation_evidence([
        {"doctype": "Purchase Invoice", "name": "PI-1"},
    ])
    first = module.evaluation_evidence_version_manifest(snapshot)
    assert module.evaluation_evidence_version_manifest(snapshot) == first

    changed = dict(snapshot)
    changed["documents"] = {doctype: [dict(record) for record in records]
                            for doctype, records in snapshot["documents"].items()}
    changed["documents"]["Purchase Invoice"][0]["docstatus"] = 0
    second = module.evaluation_evidence_version_manifest(changed)
    assert first["sha256"] != second["sha256"]
    database.rollback()


def test_native_duration_values_are_encoded_without_float_rounding(monkeypatch):
    module, _database = _load_module(monkeypatch)
    assert module._json_value(timedelta(days=-1, seconds=86399, microseconds=7)) == {
        "__type__": "timedelta", "microseconds": -999_993,
    }
