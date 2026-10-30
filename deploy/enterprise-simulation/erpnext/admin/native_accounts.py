"""Native ERPNext accounts and party mappings for source-backed documents."""
from __future__ import annotations

from typing import Any

import frappe


def ensure_party_account(
    company: str,
    party_doctype: str,
    currency: str,
    *,
    preferred_name: str | None = None,
) -> dict[str, Any]:
    """Return a native receivable/payable leaf with the requested currency.

    Company-default accounts are reused for base-currency transactions. Foreign
    currency ledgers are created below the existing native receivable/payable
    group and start with no opening balance or journal entries.
    """
    roles = {
        "Customer": ("Receivable", "Accounts Receivable", "default_receivable_account", "Receivables"),
        "Supplier": ("Payable", "Accounts Payable", "default_payable_account", "Payables"),
    }
    try:
        account_type, group_name, default_field, plural = roles[party_doctype]
    except KeyError as exc:
        raise ValueError(f"Unsupported native party DocType: {party_doctype}") from exc
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    if not company_currency:
        raise RuntimeError(f"Native ERP company {company} has no default currency")

    if currency == company_currency:
        configured = frappe.db.get_value("Company", company, default_field)
        if configured:
            account = frappe.get_doc("Account", configured)
            if (account.company != company or account.account_type != account_type or account.is_group
                    or account.disabled or account.account_currency not in (None, "", currency)):
                raise RuntimeError(f"Company {company} has an invalid native {account_type} default account")
            return {"account": configured, "created": False, "currency": currency}

    if not frappe.db.exists("Currency", currency):
        raise RuntimeError(f"Native ERP has no Currency {currency}")
    parent_name = frappe.db.get_value(
        "Account", {"company": company, "account_name": group_name, "is_group": 1}, "name",
    )
    if not parent_name:
        raise RuntimeError(f"Company {company} has no native {group_name} parent group")
    parent = frappe.get_doc("Account", parent_name)
    expected_root = "Asset" if party_doctype == "Customer" else "Liability"
    if parent.root_type != expected_root or not parent.is_group:
        raise RuntimeError(f"Company {company} native {group_name} parent has an invalid account type")

    account_name = preferred_name or f"Source {currency} {plural}"
    existing_name = frappe.db.get_value(
        "Account", {"company": company, "account_name": account_name}, "name",
    ) if preferred_name else None
    if existing_name:
        account = frappe.get_doc("Account", existing_name)
        if (account.parent_account != parent_name or account.root_type != expected_root
                or account.account_type != account_type or account.account_currency != currency
                or account.is_group or account.disabled):
            raise RuntimeError(f"Existing native {account_name} conflicts with its currency account mapping")
        return {"account": existing_name, "created": False, "currency": currency}

    account = frappe.get_doc({
        "doctype": "Account", "account_name": account_name, "company": company,
        "parent_account": parent_name, "root_type": expected_root,
        "report_type": "Balance Sheet", "account_type": account_type,
        "account_currency": currency, "is_group": 0,
    })
    account.insert(ignore_permissions=True)
    return {"account": account.name, "created": True, "currency": currency}


def map_party_to_account(party: Any, account_name: str, *, company: str, newly_created: bool) -> None:
    """Map a new source party to its transaction currency's native ledger.

    Existing agent-configured party mappings are preserved. A conflicting
    existing mapping fails clearly instead of being replaced or broadened.
    """
    account = frappe.get_doc("Account", account_name)
    expected_type = "Receivable" if party.doctype == "Customer" else "Payable"
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    if (account.company != company or account.account_type != expected_type
            or account.is_group or account.disabled):
        raise RuntimeError(f"Native party account {account_name} is not an enabled company leaf")
    rows = [row for row in (party.accounts or []) if row.company == company]
    if len(rows) > 1:
        raise RuntimeError(f"Native {party.doctype} {party.name} has multiple account mappings for {company}")
    if rows:
        current = frappe.get_doc("Account", rows[0].account)
        if (current.company != company or current.account_type != expected_type
                or current.is_group or current.disabled):
            raise RuntimeError(f"Native {party.doctype} {party.name} has an invalid company account mapping")
        current_currency = current.account_currency or company_currency
        target_currency = account.account_currency or company_currency
        if current_currency == target_currency:
            return
        if not newly_created:
            raise RuntimeError(
                f"Existing native {party.doctype} {party.name} account mapping conflicts with "
                f"{account.account_currency} source transactions"
            )
        rows[0].account = account_name
    else:
        party.append("accounts", {"company": company, "account": account_name})
    party.save(ignore_permissions=True)


def map_party_to_currency(
    party: Any,
    currency: str,
    *,
    company: str,
    newly_created: bool,
    preferred_name: str | None = None,
) -> dict[str, Any]:
    """Preserve a valid existing mapping or assign the deterministic source ledger."""
    expected_type = "Receivable" if party.doctype == "Customer" else "Payable"
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    rows = [row for row in (party.accounts or []) if row.company == company]
    if len(rows) > 1:
        raise RuntimeError(f"Native {party.doctype} {party.name} has multiple account mappings for {company}")
    if rows:
        current = frappe.get_doc("Account", rows[0].account)
        if (current.company != company or current.account_type != expected_type
                or current.is_group or current.disabled):
            raise RuntimeError(f"Native {party.doctype} {party.name} has an invalid company account mapping")
        if (current.account_currency or company_currency) == currency:
            return {"account": current.name, "created": False, "currency": currency}
        if not newly_created:
            raise RuntimeError(
                f"Existing native {party.doctype} {party.name} account mapping conflicts with {currency} source transactions"
            )

    account = ensure_party_account(company, party.doctype, currency, preferred_name=preferred_name)
    map_party_to_account(party, account["account"], company=company, newly_created=newly_created)
    return account
