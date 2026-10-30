from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


SEED_PATH = Path(__file__).parents[1] / "deploy/enterprise-simulation/erpnext/admin/seed_a1.py"
IDENTITIES_PATH = Path(__file__).parents[1] / "deploy/single-host/company-identities.json"


def load_seed(monkeypatch):
    frappe = types.ModuleType("frappe")
    frappe._dict = dict
    frappe.get_meta = Mock()
    frappe.defaults = SimpleNamespace(get_global_default=Mock(return_value="The Agent Company"))

    erpnext_package = types.ModuleType("erpnext")
    erpnext_package.__path__ = []
    setup_package = types.ModuleType("erpnext.setup")
    setup_package.__path__ = []
    wizard_package = types.ModuleType("erpnext.setup.setup_wizard")
    wizard_package.__path__ = []
    setup_wizard = types.ModuleType("erpnext.setup.setup_wizard.setup_wizard")
    setup_wizard.setup_complete = Mock()
    frappe_desk = types.ModuleType("frappe.desk")
    frappe_desk.__path__ = []
    frappe_page = types.ModuleType("frappe.desk.page")
    frappe_page.__path__ = []
    frappe_setup_wizard = types.ModuleType("frappe.desk.page.setup_wizard")
    frappe_setup_wizard.__path__ = []
    frappe_fixtures = types.ModuleType("frappe.desk.page.setup_wizard.install_fixtures")
    frappe_fixtures.update_genders = Mock()
    buying_package = types.ModuleType("erpnext.buying")
    buying_package.__path__ = []
    doctype_package = types.ModuleType("erpnext.buying.doctype")
    doctype_package.__path__ = []
    order_package = types.ModuleType("erpnext.buying.doctype.purchase_order")
    order_package.__path__ = []
    purchase_order = types.ModuleType("erpnext.buying.doctype.purchase_order.purchase_order")
    purchase_order.make_purchase_invoice = Mock()
    purchase_order.make_purchase_receipt = Mock()
    modules = {
        "frappe": frappe,
        "frappe.desk": frappe_desk,
        "frappe.desk.page": frappe_page,
        "frappe.desk.page.setup_wizard": frappe_setup_wizard,
        "frappe.desk.page.setup_wizard.install_fixtures": frappe_fixtures,
        "erpnext": erpnext_package,
        "erpnext.setup": setup_package,
        "erpnext.setup.setup_wizard": wizard_package,
        "erpnext.setup.setup_wizard.setup_wizard": setup_wizard,
        "erpnext.buying": buying_package,
        "erpnext.buying.doctype": doctype_package,
        "erpnext.buying.doctype.purchase_order": order_package,
        "erpnext.buying.doctype.purchase_order.purchase_order": purchase_order,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("tac_test_seed_a1", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, frappe, frappe_fixtures


def test_setup_company_uses_one_canonical_company_via_native_wizard(monkeypatch):
    seed, frappe, _ = load_seed(monkeypatch)
    companies = []
    frappe.get_all = Mock(side_effect=lambda doctype, **kwargs: list(companies))
    native_setup_complete = Mock(side_effect=lambda args: companies.append(args["company_name"]))
    seed.setup_complete = native_setup_complete

    seed._setup_company()

    native_setup_complete.assert_called_once()
    args = native_setup_complete.call_args.args[0]
    assert args["company_name"] == "The Agent Company"
    assert args["company_abbr"] == "TAC"
    assert companies == ["The Agent Company"]
    frappe.defaults.get_global_default.assert_called_once_with("company")


def test_setup_company_fails_on_preexisting_noncanonical_company(monkeypatch):
    seed, frappe, _ = load_seed(monkeypatch)
    frappe.get_all = Mock(return_value=["theagentcompany"])
    native_setup_complete = Mock()
    seed.setup_complete = native_setup_complete

    try:
        seed._setup_company()
    except RuntimeError as error:
        assert str(error) == "Fresh TAC ERP site unexpectedly contains a different Company"
    else:
        raise AssertionError("A preexisting differently named Company must not cause a second company")
    native_setup_complete.assert_not_called()


class FakeDB:
    def __init__(self):
        self.users = {}
        self.employees = {}
        self.roles = {"Employee", "Purchase User", "Stock User", "Delivery User", "Accounts Manager"}

    def exists(self, doctype, name):
        if doctype == "Role":
            return name in self.roles
        if doctype == "User":
            return name in self.users
        return False

    def get_value(self, doctype, filters, fields, as_dict=False):
        if doctype != "Employee":
            raise AssertionError(f"Unexpected lookup: {doctype}")
        row = self.employees.get(filters["user_id"])
        return None if row is None else SimpleNamespace(name=row.name, company=row.company)


class FakeDoc:
    def __init__(self, db, values):
        self._db = db
        self.__dict__.update(values)
        self.roles = [SimpleNamespace(**role) for role in values.get("roles", [])]
        self.flags = SimpleNamespace()

    def append(self, field, values):
        getattr(self, field).append(SimpleNamespace(**values))

    def insert(self, ignore_permissions=False):
        if self.doctype == "User":
            self._db.users[self.email] = self
        elif self.doctype == "Employee":
            self.name = f"HR-EMP-{len(self._db.employees) + 1:05d}"
            self._db.employees[self.user_id] = self

    def save(self, ignore_permissions=False):
        self._db.users[self.email] = self


def test_canonical_employee_seed_creates_linked_users_and_profiles_without_admin_roles(monkeypatch):
    seed, frappe, frappe_fixtures = load_seed(monkeypatch)
    db = FakeDB()
    frappe.db = db

    def get_doc(*args):
        if len(args) == 2 and args[0] == "User":
            return db.users[args[1]]
        return FakeDoc(db, args[0])

    frappe.get_doc = get_doc
    seed.EMPLOYEE_IDENTITIES = IDENTITIES_PATH

    seed._ensure_company_employees()
    frappe_fixtures.update_genders.assert_called_once_with()

    identities = json.loads(IDENTITIES_PATH.read_text(encoding="utf-8"))["employees"]
    assert len(db.users) == len(identities) == 17
    assert len(db.employees) == len(identities)
    for identity in identities:
        user = db.users[identity["email"]]
        employee = db.employees[identity["email"]]
        roles = {row.role for row in user.roles}
        assert "Employee" in roles
        assert "Administrator" not in roles and "System Manager" not in roles
        assert employee.company == "The Agent Company"
        assert employee.user_id == identity["email"]
        assert employee.company_email == identity["email"]

    assert {row.role for row in db.users["sarahjohnson@example.com"].roles} == {
        "Employee", "Purchase User"
    }
    assert {row.role for row in db.users["alexturner@example.com"].roles} == {
        "Employee", "Stock User"
    }
    assert {row.role for row in db.users["chenxinyi@example.com"].roles} == {
        "Employee", "Delivery User"
    }
    assert {row.role for row in db.users["liming@example.com"].roles} == {
        "Employee", "Accounts Manager"
    }

    original_passwords = {email: user.new_password for email, user in db.users.items()}
    seed._ensure_company_employees()
    assert len(db.users) == len(db.employees) == 17
    assert {email: user.new_password for email, user in db.users.items()} == original_passwords
