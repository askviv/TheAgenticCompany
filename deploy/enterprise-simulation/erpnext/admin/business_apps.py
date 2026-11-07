"""Native business app projections from the canonical company directory."""
from collections import defaultdict
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import frappe


def configure_passkeys():
    public_url = urlsplit(os.environ["TAC_ERP_PUBLIC_URL"])
    settings = frappe.get_single("Passkey Settings")
    settings.update(json.loads(Path(__file__).with_name("passkey-settings.json").read_text()))
    settings.passkey_rp_id = public_url.hostname
    settings.passkey_origins = urlunsplit((public_url.scheme, public_url.netloc, "", "", ""))
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache()


def configure_directory():
    from seed_a1 import EMPLOYEE_IDENTITIES, _ensure_company_employees

    required = {"hrms", "lending", "helpdesk", "crm"}
    if not required.issubset(set(frappe.get_installed_apps())):
        raise RuntimeError("Company business apps must be installed before directory provisioning")
    _ensure_company_employees()
    employees = json.loads(EMPLOYEE_IDENTITIES.read_text())["employees"]
    teams = defaultdict(list)
    for employee in employees:
        email = employee["email"]
        user = frappe.get_doc("User", email)
        app_roles = {role for roles in employee["frappeAppRoles"].values() for role in roles}
        user.add_roles(*sorted(app_roles))
        if "Agent" in employee["frappeAppRoles"].get("helpdesk", []):
            name = frappe.db.get_value("HD Agent", {"user": email}, "name")
            agent = frappe.get_doc("HD Agent", name) if name else frappe.new_doc("HD Agent")
            agent.update({"user": email, "agent_name": employee["displayName"], "is_active": 1})
            agent.save(ignore_permissions=True)
            teams[employee["department"]].append({"user": email})
    for department, members in teams.items():
        name = frappe.db.get_value("HD Team", {"team_name": department}, "name")
        team = frappe.get_doc("HD Team", name) if name else frappe.new_doc("HD Team")
        team.team_name = department
        team.set("users", members)
        team.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache()
    return {"employees": len(employees), "helpdesk_teams": len(teams), "identity_source": "company-identities.json"}
