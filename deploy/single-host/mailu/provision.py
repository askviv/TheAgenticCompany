#!/usr/bin/env python3
"""Idempotently provision Mailu mailboxes from the company identity map.

This script only creates missing domains/users. It never changes or deletes an
existing Mailu account. Generated passwords are stored in mode-0600 files under
the persistent Mailu data directory and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import smtplib
import ssl
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path("/data/theagentcompany/mailu")
DEFAULT_API = "http://127.0.0.1:8112/api/v1"
DEFAULT_IDENTITIES = ROOT / "company-identities.json"
MAIL_DOMAIN = "example.com"
VIV_RELAY_KEY = "viv_reply_relay"
VIV_RELAY_EMAIL = f"{VIV_RELAY_KEY}@{MAIL_DOMAIN}"
SMTP_SUBMISSION_HOST = "mail.company.example.test"
SMTP_SUBMISSION_PORT = 2587


class MailuAPIError(RuntimeError):
    """A safe, response-body-free Mailu API failure."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def read_env_value(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, value = stripped.split("=", 1)
            if name.strip() == key:
                return value.strip()
    raise RuntimeError(f"Required Mailu setting {key} is missing")


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
    with path.open() as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise RuntimeError(f"Credential file {path.name} must contain a JSON object")
    return value


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


class MailuClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
        except HTTPError as error:
            status = error.code
            error.close()
            raise MailuAPIError(f"Mailu API {method} {path} returned HTTP {status}", status=status) from error
        except URLError as error:
            raise MailuAPIError(f"Mailu API {method} {path} could not be reached") from error

        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise MailuAPIError(f"Mailu API {method} {path} returned invalid JSON") from error

    def domains(self) -> list[dict]:
        response = self.request("GET", "/domain")
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise MailuAPIError("Mailu API returned an unexpected domain-list shape")
        return response

    def ensure_domain(self, name: str) -> bool:
        if any(domain.get("name") == name for domain in self.domains()):
            return False
        try:
            self.request("POST", "/domain", {"name": name})
        except MailuAPIError as error:
            if error.status != 409:
                raise
            if any(domain.get("name") == name for domain in self.domains()):
                return False
            raise
        return True

    def user_exists(self, email: str) -> bool:
        return self.get_user(email) is not None

    def get_user(self, email: str) -> dict | None:
        try:
            response = self.request("GET", "/user/" + quote(email, safe=""))
        except MailuAPIError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(response, dict):
            raise MailuAPIError("Mailu API returned an unexpected user-record shape")
        return response

    def create_user(
        self, email: str, name: str, password: str, *, global_admin: bool = False,
        enable_imap: bool = True,
    ) -> bool:
        payload = {
            "email": email,
            "raw_password": password,
            "displayed_name": name,
            "enabled": True,
            "enable_imap": enable_imap,
            "enable_pop": False,
            "allow_spoofing": False,
            "global_admin": global_admin,
        }
        try:
            self.request("POST", "/user", payload)
        except MailuAPIError as error:
            if error.status != 409 or not self.user_exists(email):
                raise
            return False
        return True


def ensure_account(
    client: MailuClient,
    records: dict,
    record_key: str,
    email: str,
    name: str,
    *,
    global_admin: bool = False,
) -> str:
    entry = records.setdefault(record_key, {"mail": {"address": email}})
    mail = entry.setdefault("mail", {"address": email})
    if mail.get("address") != email:
        raise RuntimeError(f"Credential mapping for {record_key} does not match the identity file")

    if client.user_exists(email):
        if mail.get("password"):
            mail["status"] = "ready"
            return "preserved"
        # An existing account without a saved credential belongs to another
        # provisioning path. Never reset its password to make this script pass.
        records.pop(record_key, None)
        return "existing_without_managed_credentials"

    password = mail.get("password") or secrets.token_urlsafe(36)
    mail.update({"address": email, "password": password, "status": "pending"})
    # Persist before the network mutation so an uncertain POST can safely retry.
    return "pending"


def authenticate_viv_relay(address: str, password: str) -> None:
    """Prove the saved relay credential with SMTP AUTH; send no message."""
    with smtplib.SMTP(SMTP_SUBMISSION_HOST, SMTP_SUBMISSION_PORT, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(address, password)


def ensure_viv_relay_account(client: MailuClient, service_accounts: dict) -> str:
    record = service_accounts.get(VIV_RELAY_KEY)
    if record is None:
        record = {"mail": {"address": VIV_RELAY_EMAIL}}
        service_accounts[VIV_RELAY_KEY] = record
    if not isinstance(record, dict) or not isinstance(record.get("mail"), dict):
        raise RuntimeError("Viv relay credential mapping is invalid; operator recovery is required")
    mail = record["mail"]
    if mail.get("address") != VIV_RELAY_EMAIL:
        raise RuntimeError("Viv relay credential identity does not match the configured account")
    record["allow_spoofing"] = False
    record["enable_imap"] = False

    native_user = client.get_user(VIV_RELAY_EMAIL)
    if native_user is None:
        # Keep an uncertain create attempt's exact credential for retry; only
        # generate when no credential was ever durably stored.
        if not mail.get("password"):
            mail["password"] = secrets.token_urlsafe(36)
        mail["status"] = "pending"
        return "pending"

    if not isinstance(mail.get("password"), str) or not mail["password"]:
        raise RuntimeError("The existing Viv relay account has no managed credential; operator recovery is required")
    mail["status"] = "pending"
    if native_user.get("allow_spoofing") is not False or native_user.get("global_admin") is not False:
        raise RuntimeError("Viv relay Mailu account must remain non-spoofing and non-administrative")
    if native_user.get("enabled") is not True or native_user.get("enable_imap") is not False:
        raise RuntimeError("Viv relay Mailu account must remain enabled for SMTP only")
    try:
        authenticate_viv_relay(VIV_RELAY_EMAIL, mail["password"])
    except (OSError, smtplib.SMTPException) as error:
        # A pending POST may have created the native account with a different
        # credential (or the API response may have been lost). Never mark that
        # saved password ready based on account existence alone.
        raise RuntimeError(
            "Saved Viv relay credential did not authenticate; operator recovery is required"
        ) from error
    mail["status"] = "ready"
    return "preserved"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--identities", type=Path, default=DEFAULT_IDENTITIES)
    parser.add_argument("--api", default=DEFAULT_API)
    args = parser.parse_args()
    root = args.root

    token = read_env_value(root / "mailu.env", "API_TOKEN")
    client = MailuClient(args.api, token)
    with args.identities.open() as file:
        identities = json.load(file)
    employees = identities.get("employees")
    if not isinstance(employees, list):
        raise RuntimeError("Identity file must contain an employees array")

    employee_records = read_json(root / "employee-credentials.json", {"schema_version": 1, "employees": {}})
    employees_by_id = employee_records.setdefault("employees", {})
    if not isinstance(employees_by_id, dict):
        raise RuntimeError("Employee credential file has an invalid employees mapping")
    service_records = read_json(root / "service-credentials.json", {"schema_version": 1, "accounts": {}})
    service_accounts = service_records.setdefault("accounts", {})
    if not isinstance(service_accounts, dict):
        raise RuntimeError("Service credential file has an invalid accounts mapping")

    def verify_viv_relay() -> str:
        try:
            return ensure_viv_relay_account(client, service_accounts)
        except Exception:
            # Persist the pending state before surfacing auth or identity
            # uncertainty so a later run cannot trust stale ready metadata.
            write_private_json(root / "service-credentials.json", service_records)
            raise

    created_domain = client.ensure_domain(MAIL_DOMAIN)
    counts = {"created": 0, "preserved": 0, "existing_without_managed_credentials": 0}
    seen_ids = set()
    for employee in employees:
        employee_id = employee.get("id")
        email = employee.get("accounts", {}).get("mail", {}).get("address")
        name = employee.get("displayName")
        if not all(isinstance(value, str) and value for value in (employee_id, email, name)):
            raise RuntimeError("Each employee needs a stable ID, display name, and mail address")
        if employee_id in seen_ids:
            raise RuntimeError("Identity file contains duplicate employee IDs")
        seen_ids.add(employee_id)
        if email.rsplit("@", 1)[-1].lower() != MAIL_DOMAIN:
            raise RuntimeError(f"Identity {employee_id} is not in the configured mail domain")

        outcome = ensure_account(client, employees_by_id, employee_id, email, name)
        if outcome == "pending":
            write_private_json(root / "employee-credentials.json", employee_records)
            record = employees_by_id[employee_id]
            if client.create_user(email, name, record["mail"]["password"]):
                counts["created"] += 1
                outcome = "created"
            else:
                # A competing run inserted this account after our first GET.
                # We cannot claim that our generated password is authoritative.
                employees_by_id.pop(employee_id, None)
                counts["existing_without_managed_credentials"] += 1
                write_private_json(root / "employee-credentials.json", employee_records)
                continue
            record["mail"]["status"] = "ready"
        elif outcome in counts:
            counts[outcome] += 1
        write_private_json(root / "employee-credentials.json", employee_records)

    # Mailu requires a real user or alias at its configured postmaster address.
    service_key = "postmaster"
    postmaster_email = f"{service_key}@{MAIL_DOMAIN}"
    outcome = ensure_account(
        client, service_accounts, service_key, postmaster_email, "Mailu Postmaster"
    )
    if outcome == "pending":
        write_private_json(root / "service-credentials.json", service_records)
        record = service_accounts[service_key]
        if not client.create_user(postmaster_email, "Mailu Postmaster", record["mail"]["password"]):
            service_accounts.pop(service_key, None)
            write_private_json(root / "service-credentials.json", service_records)
            raise RuntimeError("Postmaster was created concurrently; rerun to preserve its account")
        record["mail"]["status"] = "ready"
        outcome = "created"
    write_private_json(root / "service-credentials.json", service_records)
    write_private_json(root / "employee-credentials.json", employee_records)

    viv_relay_outcome = verify_viv_relay()
    if viv_relay_outcome == "pending":
        write_private_json(root / "service-credentials.json", service_records)
        relay_mail = service_accounts[VIV_RELAY_KEY]["mail"]
        if not client.create_user(
            VIV_RELAY_EMAIL, "Viv Reply Relay", relay_mail["password"],
            enable_imap=False,
        ):
            write_private_json(root / "service-credentials.json", service_records)
            # A concurrent or uncertain create is safe only if the exact
            # durable credential authenticates against the resulting account.
            viv_relay_outcome = verify_viv_relay()
            if viv_relay_outcome == "pending":
                raise RuntimeError("Viv relay account creation remains uncertain; saved credential is pending")
        else:
            relay_mail["status"] = "ready"
            viv_relay_outcome = "created"
    verify_viv_relay()
    write_private_json(root / "service-credentials.json", service_records)

    print(
        "Mailu provisioning complete: "
        f"domain_created={str(created_domain).lower()} "
        f"employee_records={len(employees_by_id)} "
        f"employee_accounts_created={counts['created']} "
        f"employees_preserved={counts['preserved']} "
        f"existing_without_managed_credentials={counts['existing_without_managed_credentials']} "
        f"postmaster={outcome} "
        f"viv_relay={viv_relay_outcome}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Mailu provisioning failed ({type(error).__name__})", file=sys.stderr)
        raise
