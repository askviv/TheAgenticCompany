#!/usr/bin/env python3
"""Prepare private coordinator configuration and dependencies; do not start it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path("/data/theagentcompany/enterprise")
SOURCE = ROOT / "source"
MAIL_ROOT = Path("/data/theagentcompany/mailu")
MAIL_API = "http://127.0.0.1:8112/api/v1"
MAIL_DOMAIN = "example.com"
MAIL_HOST = "mail.company.example.test"
MAIL_SMTP_PORT = 2587
SELKIES_BASE_IMAGE = "ghcr.io/selkies-project/selkies/desktop:2.1.0-ubuntu26.04"
EMPLOYEE_DESKTOP_IMAGE = "tac-selkies-desktop:2.1.0-ubuntu26.04-socat1"
EMPLOYEE_DESKTOP_DOCKERFILE = Path("deploy/enterprise-simulation/coordinator/employee-desktop/Dockerfile")
EMPLOYEE_DESKTOP_DOCKERFILE_LABEL = "com.theagentcompany.employee-desktop.dockerfile-sha256"
ACTORS = [
    {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "agent"},
    {"actor_id": "requester", "email": "sarahjohnson@example.com", "kind": "employee"},
    {"actor_id": "warehouse", "email": "alexturner@example.com", "kind": "warehouse"},
    {"actor_id": "logistics", "email": "chenxinyi@example.com", "kind": "logistics"},
    {"actor_id": "manager", "email": "liming@example.com", "kind": "approver"},
    {"actor_id": "supplier", "email": "northwind-supplier@example.com", "kind": "supplier"},
    {"actor_id": "customer", "email": "cornerstone-customer@example.com", "kind": "customer"},
]
COUNTERPARTIES = {
    "northwind-supplier": ("northwind-supplier@example.com", "Northwind Components Supplier"),
    "cornerstone-customer": ("cornerstone-customer@example.com", "Cornerstone Customer"),
}
PRINCIPAL_ACTORS = {"controller": "simulation-controller", "evaluator": "private-evaluator", "agent": "viv"}
VIV_RELAY_ACCOUNT_KEY = "viv_reply_relay"
VIV_RELAY_ACCOUNT_EMAIL = "viv_reply_relay@example.com"
VIV_PUBLIC_EMAIL = "agent@agent.example.test"
PRESERVED_RUNTIME_SETTINGS = frozenset({
    "actor_workers", "background_model_id", "company_background_enabled",
    "company_issues_enabled", "erp_base_url", "erp_public_url", "issue_config_path",
    "employee_chat_credentials_file", "rocketchat_base_url",
    "retail_operator_users", "workers_enabled",
})


class MailuAPIError(RuntimeError):
    def __init__(self, operation: str, status: int | None = None):
        self.operation = operation
        self.status = status
        super().__init__(f"Mailu {operation} failed" + (f" (HTTP {status})" if status else ""))


class MailuClient:
    """Small stdlib client following deploy/single-host/mailu/provision.py's API contract."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None):
        request = Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
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
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
            raise MailuAPIError(f"{method} {path}", status) from error
        except URLError as error:
            raise MailuAPIError(f"{method} {path}") from error
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise MailuAPIError(f"{method} {path} JSON", status) from error

    def has_domain(self, name: str) -> bool:
        domains = self.request("GET", "/domain")
        if not isinstance(domains, list) or any(not isinstance(item, dict) for item in domains):
            raise MailuAPIError("list domains")
        return any(item.get("name") == name for item in domains)

    def user_exists(self, address: str) -> bool:
        try:
            self.request("GET", "/user/" + quote(address, safe=""))
        except MailuAPIError as error:
            if error.status == 404:
                return False
            raise
        return True

    def create_user(self, address: str, display_name: str, password: str) -> None:
        self.request("POST", "/user", {
            "email": address,
            "raw_password": password,
            "displayed_name": display_name,
            "enabled": True,
            "enable_imap": True,
            "enable_pop": False,
            "allow_spoofing": False,
            "global_admin": False,
        })


def ensure_recipient_alias(client: MailuClient, address: str, destinations: list[str], *, create_alias) -> None:
    """Provision one explicit native Mailu route without changing other aliases."""
    try:
        existing = client.request("GET", "/alias/" + quote(address, safe=""))
    except MailuAPIError as error:
        if error.status != 404:
            raise
        # Mailu 2024.06's API rejects external destinations; its native CLI
        # supports them and creates the same managed Alias model.
        create_alias(address, destinations)
        existing = client.request("GET", "/alias/" + quote(address, safe=""))
    if (not isinstance(existing, dict) or existing.get("email") != address
            or set(existing.get("destination", [])) != set(destinations)
            or existing.get("wildcard")):
        raise RuntimeError("Company support alias does not match its explicitly configured recipients")


def private_json(path: Path) -> dict:
    info = path.stat()
    if info.st_mode & 0o077:
        raise PermissionError(f"{path.name} must be mode 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def write_private_json(path: Path, value: dict) -> None:
    if path.parent.exists():
        if path.parent.stat().st_mode & 0o077:
            raise PermissionError(f"{path.parent.name} must be private to its owner")
    else:
        path.parent.mkdir(parents=True, mode=0o700)
        path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_private_secret(path: Path) -> None:
    if path.exists():
        info = path.stat()
        if info.st_mode & 0o077:
            raise PermissionError(f"{path.name} must be mode 0600")
        if len(path.read_text(encoding="utf-8").strip()) < 32:
            raise ValueError(f"{path.name} contains an invalid private secret")
        return
    if path.parent.exists() and path.parent.stat().st_mode & 0o077:
        raise PermissionError(f"{path.parent.name} must be private to its owner")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def env_value(path: Path, key: str) -> str:
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path.name} must be private (mode 0600)")
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#") and "=" in value:
            name, contents = value.split("=", 1)
            if name.strip() == key:
                result = contents.strip()
                if result:
                    return result
    raise ValueError(f"Required Mailu setting {key} is missing")


def verify_mailbox_password(address: str, password: str, *, ca_file: Path | None) -> bool:
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    try:
        with smtplib.SMTP(MAIL_HOST, MAIL_SMTP_PORT, timeout=15) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            client.login(address, password)
        return True
    except smtplib.SMTPAuthenticationError:
        return False
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError("Could not verify a pending Mailu mailbox credential") from error


def provision_counterparties(
    client: MailuClient,
    mail_root: Path,
    *,
    ca_file: Path | None,
    counterparties: dict[str, tuple[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    path = mail_root / "enterprise-counterparty-credentials.json"
    records = private_json(path) if path.exists() else {"schema_version": 1, "accounts": {}}
    accounts = records.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("Counterparty credential file has an invalid accounts mapping")
    credentials: dict[str, dict[str, str]] = {}

    for key, (address, display_name) in (counterparties or COUNTERPARTIES).items():
        entry = accounts.get(key)
        if entry is not None and not isinstance(entry, dict):
            raise ValueError(f"Managed credential record for {key} is invalid")
        mail = entry.get("mail", {}) if entry else {}
        if not isinstance(mail, dict):
            raise ValueError(f"Managed credential record for {key} is invalid")
        if mail and mail.get("address") != address:
            raise ValueError(f"Managed credential record for {key} has a different address")

        exists = client.user_exists(address)
        if exists and not mail.get("password"):
            raise RuntimeError(f"Mailu account {address} already exists without managed credentials; refusing to reset it")
        if exists and mail.get("status") == "pending":
            if not verify_mailbox_password(address, mail["password"], ca_file=ca_file):
                raise RuntimeError(f"Mailu account {address} exists but its pending managed credential did not authenticate")
            mail["status"] = "ready"
            write_private_json(path, records)
        elif exists and mail.get("status") != "ready":
            raise RuntimeError(f"Mailu account {address} has an unsupported managed credential state")
        elif not exists and mail:
            raise RuntimeError(f"Previously managed Mailu account {address} is missing; operator recovery is required")
        elif not exists:
            mail = {
                "address": address,
                "username": address,
                "password": "theagentcompany",
                "status": "pending",
            }
            accounts[key] = {"mail": mail}
            # Persist credentials before a remote account creation; an uncertain
            # response can be reconciled by authenticating this exact credential.
            write_private_json(path, records)
            try:
                client.create_user(address, display_name, mail["password"])
            except MailuAPIError as error:
                if error.status != 409 or not client.user_exists(address):
                    raise
                if not verify_mailbox_password(address, mail["password"], ca_file=ca_file):
                    raise RuntimeError(f"Mailu account {address} was concurrently created with different credentials") from error
            mail["status"] = "ready"
            write_private_json(path, records)
        credentials[address] = {"username": address, "password": mail["password"]}
    return credentials


def coffee_mail_directory(company_profile: dict) -> tuple[dict[str, tuple[str, str]], list[dict[str, str]]]:
    """Validate and convert explicit native Coffee IDs into managed Mailu actors."""
    background = company_profile.get("background", {})
    coffee = background.get("coffee", {}) if isinstance(background, dict) else {}
    records = coffee.get("counterparties") if isinstance(coffee, dict) else None
    sender_actor_id = coffee.get("mail_sender_actor_id") if isinstance(coffee, dict) else None
    allowed_senders = {
        actor["actor_id"] for actor in ACTORS
        if actor["kind"] in {"employee", "warehouse", "logistics", "approver"}
        or actor["actor_id"] == "viv"
    }
    if sender_actor_id not in allowed_senders:
        raise ValueError("CoffeeBench mail sender must name an existing company mailbox actor")
    native_ids = {"farmer_A", "farmer_B", "roaster_B", "retailer_A", "retailer_B"}
    if not isinstance(records, dict) or set(records) != native_ids:
        raise ValueError("CoffeeBench mail directory must explicitly map every external native business")
    accounts: dict[str, tuple[str, str]] = {}
    actors: list[dict[str, str]] = []
    for native_id in sorted(native_ids):
        record = records[native_id]
        if not isinstance(record, dict):
            raise ValueError("CoffeeBench mail directory entry must be an object")
        actor_id = record.get("actor_id")
        email = record.get("email")
        display_name = record.get("display_name")
        if (
            actor_id != f"coffee_{native_id}"
            or email != f"coffee-{native_id}@{MAIL_DOMAIN}"
            or not isinstance(display_name, str)
            or not display_name
            or record.get("kind") != "counterparty"
        ):
            raise ValueError("CoffeeBench mail identity does not match the managed simulator directory")
        accounts[f"coffee-{native_id}"] = (email, display_name)
        actors.append({"actor_id": actor_id, "email": email, "kind": "counterparty"})
    return accounts, actors


def managed_employee_actors(company_profile: dict) -> list[dict[str, str]]:
    """Map the existing profile's complete employee roster to Mailu identities."""
    employees = company_profile.get("employees")
    if not isinstance(employees, list):
        raise ValueError("Company profile must contain its managed employee roster")
    actors_by_email = {actor["email"].casefold(): actor for actor in ACTORS}
    known_ids = {actor["actor_id"] for actor in ACTORS}
    result = []
    seen_emails: set[str] = set()
    seen_ids: set[str] = set()
    for employee in employees:
        if not isinstance(employee, dict):
            raise ValueError("Company employee profile entry must be an object")
        employee_id, email = employee.get("id"), employee.get("email")
        if not isinstance(employee_id, str) or not employee_id or not isinstance(email, str) or "@" not in email:
            raise ValueError("Company employee profile entry lacks a stable ID or email")
        normalized_email = email.casefold()
        if normalized_email in seen_emails or employee_id in seen_ids:
            raise ValueError("Company employee profile contains duplicate identities")
        seen_emails.add(normalized_email)
        seen_ids.add(employee_id)
        existing = actors_by_email.get(normalized_email)
        if existing:
            continue
        actor_id = "employee-" + employee_id
        if actor_id in known_ids:
            raise ValueError("Company employee actor ID conflicts with an existing mailbox actor")
        known_ids.add(actor_id)
        result.append({"actor_id": actor_id, "email": email, "kind": "employee"})
    return result


def external_provider_mail_directory(company_profile: dict) -> tuple[dict[str, tuple[str, str]], list[dict[str, str]]]:
    """Load explicit native provider identities for the employee portal."""
    background = company_profile.get("background", {})
    records = background.get("external_providers") if isinstance(background, dict) else None
    provider_ids = {"tau2-airline", "tau2-telecom", "tau2-banking"}
    if not isinstance(records, dict) or set(records) != provider_ids:
        raise ValueError("Company profile must explicitly map all native external-provider mailboxes")
    accounts: dict[str, tuple[str, str]] = {}
    actors: list[dict[str, str]] = []
    for provider_id in sorted(provider_ids):
        record = records[provider_id]
        if not isinstance(record, dict):
            raise ValueError("External provider mail identity must be an object")
        actor_id, email, display_name = record.get("actor_id"), record.get("email"), record.get("display_name")
        expected = {
            "tau2-airline": "airline-support",
            "tau2-telecom": "telecom-support",
            "tau2-banking": "bank-support",
        }[provider_id]
        if (
            actor_id != expected
            or email != f"{expected}@{MAIL_DOMAIN}"
            or not isinstance(display_name, str) or not display_name
            or record.get("kind") != "counterparty"
        ):
            raise ValueError("External provider mailbox identity does not match its native provider mapping")
        accounts[expected] = (email, display_name)
        actors.append({"actor_id": actor_id, "email": email, "kind": "counterparty"})
    return accounts, actors


def existing_employee_credentials(mail_root: Path, actors: list[dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    records = private_json(mail_root / "employee-credentials.json")
    employees = records.get("employees")
    if not isinstance(employees, dict):
        raise ValueError("Employee credential file has an invalid employees mapping")
    result = {}
    expected = {actor["email"] for actor in (actors or ACTORS)
                if actor["kind"] in {"employee", "warehouse", "logistics", "approver"}}
    for address in expected:
        matches = []
        for employee in employees.values():
            mail = employee.get("mail", {}) if isinstance(employee, dict) else {}
            if isinstance(mail, dict) and mail.get("address") == address:
                matches.append(mail)
        if len(matches) > 1:
            raise RuntimeError("Existing managed employee mailbox credential identity is ambiguous")
        if len(matches) != 1 or not isinstance(matches[0].get("password"), str):
            continue
        result[address] = {"username": address, "password": matches[0]["password"]}
    return result


def viv_relay_credentials(mail_root: Path) -> dict[str, str]:
    records = private_json(mail_root / "service-credentials.json")
    account = records.get("accounts", {}).get(VIV_RELAY_ACCOUNT_KEY)
    mail = account.get("mail", {}) if isinstance(account, dict) else {}
    if (
        not isinstance(mail, dict)
        or mail.get("address") != VIV_RELAY_ACCOUNT_EMAIL
        or mail.get("status") != "ready"
        or not isinstance(mail.get("password"), str)
        or not mail["password"]
        or account.get("allow_spoofing") is not False
        or account.get("enable_imap") is not False
    ):
        raise RuntimeError("Managed non-spoofing Viv relay credentials are unavailable; provision Mailu first")
    return {"username": VIV_RELAY_ACCOUNT_EMAIL, "password": mail["password"]}


def principals(path: Path) -> dict[str, dict[str, str]]:
    if path.exists():
        current = private_json(path)
        result = {}
        configured_roles: set[str] = set()
        for token, record in current.items():
            role = record.get("role") if isinstance(record, dict) else None
            if role not in PRINCIPAL_ACTORS or not token or role in configured_roles:
                raise RuntimeError("Existing principals file contains unsupported identities; operator recovery is required")
            configured_roles.add(role)
            result[token] = {"actor_id": PRINCIPAL_ACTORS[role], "role": role}
    else:
        result = {}
    present = {record["role"] for record in result.values()}
    for role, actor_id in PRINCIPAL_ACTORS.items():
        if role not in present:
            result[secrets.token_urlsafe(32)] = {"actor_id": actor_id, "role": role}
    return result


def verify_source(source: Path) -> None:
    if not (source / "servers/simulation/runtime.py").is_file() or not (source / "servers/simulation/requirements.txt").is_file():
        raise FileNotFoundError("Enterprise source checkout lacks simulation runtime files")


def ensure_employee_desktop_image(source: Path) -> str:
    """Build the pinned local Selkies derivative once per Dockerfile revision.

    This only inspects/builds an image; it does not restart or replace running
    containers or touch any other Docker services.
    """
    dockerfile = source / EMPLOYEE_DESKTOP_DOCKERFILE
    dockerfile_digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    inspect_command = [
        "docker", "image", "inspect", EMPLOYEE_DESKTOP_IMAGE,
        "--format", "{{json .Config.Labels}}",
    ]

    def matching_revision() -> bool:
        result = subprocess.run(inspect_command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            return False
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Docker returned invalid employee desktop image labels") from error
        return (isinstance(labels, dict)
                and labels.get(EMPLOYEE_DESKTOP_DOCKERFILE_LABEL) == dockerfile_digest
                and labels.get("org.opencontainers.image.base.name") == SELKIES_BASE_IMAGE)

    if matching_revision():
        return EMPLOYEE_DESKTOP_IMAGE
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Docker daemon is unavailable; the employee desktop image is required") from error

    build_command = [
        "docker", "build", "--pull",
        "--build-arg", f"SELKIES_BASE_IMAGE={SELKIES_BASE_IMAGE}",
        "--build-arg", f"EMPLOYEE_DESKTOP_DOCKERFILE_SHA256={dockerfile_digest}",
        "--file", str(dockerfile),
        "--tag", EMPLOYEE_DESKTOP_IMAGE,
        str(dockerfile.parent),
    ]
    try:
        # Leave build progress/errors visible to the operator; the context has
        # only the Dockerfile and the build args contain no credentials.
        subprocess.run(build_command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Could not build the pinned Selkies employee desktop image") from error
    if not matching_revision():
        raise RuntimeError("Docker build completed without the expected employee desktop image labels")
    return EMPLOYEE_DESKTOP_IMAGE


def prepare_optional_employee_desktop_image(source: Path) -> str | None:
    """Report image provisioning failure while allowing core company setup through."""
    try:
        return ensure_employee_desktop_image(source)
    except Exception as error:
        print(
            "WARNING: employee desktop image provisioning failed "
            f"({type(error).__name__}: {error}); desktop sessions will be unavailable "
            "until coordinator bootstrap succeeds.",
            file=sys.stderr,
        )
        return None


def private_erp_compose(source: Path, destination: Path) -> Path:
    template = source / "deploy/enterprise-simulation/erpnext/compose.yml"
    if destination.exists():
        try:
            rendered = subprocess.run(
                ["docker", "compose", "--file", str(destination), "config", "--format", "json", "--no-interpolate"],
                check=True, capture_output=True, text=True,
            ).stdout
            compose = json.loads(rendered)
        except FileNotFoundError as exc:
            raise RuntimeError("Docker Compose is required to validate the existing ERP compose") from exc
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise RuntimeError("Existing ERP compose is unreadable or invalid; operator recovery is required") from exc
        services = compose.get("services") if isinstance(compose, dict) else None
        if not isinstance(services, dict) or not {"backend", "frontend", "db", "site-admin"}.issubset(services):
            raise RuntimeError("Existing ERP compose lacks required services; operator recovery is required")

        def mounts(service: dict) -> dict[str, tuple[str, str]]:
            mounts = service.get("volumes", [])
            result = {}
            if not isinstance(mounts, list):
                return result
            for mount in mounts:
                if isinstance(mount, str):
                    parts = mount.split(":", 2)
                    if len(parts) >= 2:
                        mount_type = "bind" if parts[0].startswith("/") or parts[0].startswith(".") else "volume"
                        result[parts[1]] = (mount_type, parts[0])
                elif isinstance(mount, dict) and mount.get("type") in {"bind", "volume", "tmpfs"}:
                    result[str(mount.get("target", ""))] = (
                        str(mount["type"]), str(mount.get("source", "")),
                    )
            return result

        expected = {
            "backend": {"/opt/erpnext_adapter.py": source / "servers/simulation/erpnext.py"},
            "site-admin": {
                "/opt/erpnext-admin": source / "deploy/enterprise-simulation/erpnext/admin",
                "/opt/erpnext-contracts/integration_contracts.py": source / "servers/simulation/integration_contracts.py",
                "/opt/company-identities.json": source / "deploy/single-host/company-identities.json",
                "/opt/erpnext_adapter.py": source / "servers/simulation/erpnext.py",
            },
        }
        for service_name, targets in expected.items():
            actual = mounts(services.get(service_name, {}))
            for target, expected_source in targets.items():
                if actual.get(target) != ("bind", str(expected_source)):
                    raise RuntimeError("Existing ERP compose source mounts differ from this checkout; explicit migration is required")
        required_data_mounts = {
            "backend": {"/home/frappe/frappe-bench/sites"},
            "frontend": {"/home/frappe/frappe-bench/sites"},
            "site-admin": {
                "/home/frappe/frappe-bench/sites", "/opt/erpnext-golden", "/opt/erpnext-runtime",
            },
            "db": {"/var/lib/mysql"},
        }
        for service_name, targets in required_data_mounts.items():
            actual = mounts(services.get(service_name, {}))
            if any(target not in actual or actual[target][0] not in {"bind", "volume"} for target in targets):
                raise RuntimeError("Existing ERP compose lacks required persistent mounts; operator recovery is required")
        return destination

    rendered = template.read_text(encoding="utf-8").replace(
        "/data/theagentcompany/source/", str(source) + "/"
    )
    if "/data/theagentcompany/source/" in rendered:
        raise RuntimeError("ERP compose still references the shared workplace source tree")
    destination.write_text(rendered, encoding="utf-8")
    destination.chmod(0o600)
    return destination


def preserve_runtime_settings(runtime_config: dict, previous_config: dict | None,
                              supplied_binding: dict | None = None) -> dict:
    """Preserve known operator-owned settings without carrying unknown keys forward."""
    result = dict(runtime_config)
    previous = previous_config or {}
    previous_binding = previous.get("company_binding")
    if supplied_binding is not None:
        if previous_binding is not None and supplied_binding != previous_binding:
            raise RuntimeError("Supplied company binding differs from the persisted company binding")
        result["company_binding"] = supplied_binding
    elif previous_binding is not None:
        result["company_binding"] = previous_binding
    if "actor_workers" in previous:
        actor_workers = previous["actor_workers"]
        if not isinstance(actor_workers, dict):
            raise ValueError("Persisted actor_workers configuration must be an object")
        result["actor_workers"] = actor_workers
    for key in PRESERVED_RUNTIME_SETTINGS - {"actor_workers"}:
        if key in previous:
            result[key] = previous[key]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--mail-root", type=Path, default=MAIL_ROOT)
    parser.add_argument("--mail-api", default=MAIL_API)
    parser.add_argument("--mailu-admin-container", default="theagentcompany-mailu-admin-1")
    parser.add_argument("--python", default="python3.12")
    parser.add_argument("--host", default="192.0.2.10")
    parser.add_argument("--port", type=int, default=8131)
    parser.add_argument("--provider-portal-public-url", default="http://192.0.2.10:8132")
    parser.add_argument("--tls-ca-file", type=Path, help="Optional CA trust bundle; defaults to the host trust store")
    parser.add_argument("--company-binding", type=Path, help="Private JSON binding for the existing company ERP site; preserved on later bootstrap runs")
    args = parser.parse_args()

    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("Run coordinator bootstrap as the host administrator")
    root = args.root.resolve()
    source = args.source.resolve(strict=True)
    if source == Path("/data/theagentcompany/source"):
        raise RuntimeError("Use the separate /data/theagentcompany/enterprise/source checkout")
    verify_source(source)
    mail_root = args.mail_root.resolve(strict=True)
    if args.tls_ca_file is not None and not args.tls_ca_file.is_file():
        raise FileNotFoundError("The configured Mailu TLS CA file is unavailable")
    python_info = subprocess.run([args.python, "--version"], check=True, capture_output=True, text=True)
    if "3.12" not in python_info.stdout + python_info.stderr:
        raise RuntimeError("Coordinator runtime requires the workplace's Python 3.12 environment")
    employee_desktop_image = prepare_optional_employee_desktop_image(source)
    if employee_desktop_image is not None:
        print(f"Employee desktop image ready: {employee_desktop_image}")

    mail_env = mail_root / "mailu.env"
    client = MailuClient(args.mail_api, env_value(mail_env, "API_TOKEN"))
    if not client.has_domain(MAIL_DOMAIN):
        raise RuntimeError(f"Mailu domain {MAIL_DOMAIN} is not configured; refusing to create or change domains")

    company_identity_profile = json.loads((source / "deploy/single-host/company-identities.json").read_text())
    company_profile = company_identity_profile["company"]
    employee_actors = managed_employee_actors(company_identity_profile)
    managed_actors = [*ACTORS, *employee_actors]
    employee_credentials = existing_employee_credentials(mail_root, managed_actors)
    expected_employee_credentials = {
        actor["email"] for actor in managed_actors
        if actor["kind"] in {"employee", "warehouse", "logistics", "approver"}
    }
    if expected_employee_credentials - employee_credentials.keys():
        print("Existing mailbox credentials are unavailable for some managed employees; only those accounts will be affected.")
    required_core_emails = {
        actor["email"] for actor in ACTORS
        if actor["kind"] in {"employee", "warehouse", "logistics", "approver"}
    }
    if required_core_emails - employee_credentials.keys():
        raise RuntimeError("Core company mailbox credentials are unavailable")
    managed_actors = [
        actor for actor in managed_actors
        if actor["kind"] not in {"employee", "warehouse", "logistics", "approver"}
        or actor["email"] in employee_credentials
    ]
    coffee_accounts, coffee_actors = coffee_mail_directory(company_profile)
    provider_accounts, provider_actors = external_provider_mail_directory(company_profile)
    actors = [*managed_actors, *coffee_actors, *provider_actors]
    if len({actor["actor_id"] for actor in actors}) != len(actors):
        raise ValueError("Configured Mailu actors must have unique IDs")
    counterparties = {**COUNTERPARTIES, **coffee_accounts, **provider_accounts}
    counterparty_credentials = provision_counterparties(
        client, mail_root, ca_file=args.tls_ca_file, counterparties=counterparties
    )
    all_credentials = {**employee_credentials, **counterparty_credentials}
    viv_actor = next(actor for actor in actors if actor["actor_id"] == "viv")
    if viv_actor["email"] != VIV_PUBLIC_EMAIL:
        raise ValueError("Viv actor does not match the configured company address")
    all_credentials[VIV_PUBLIC_EMAIL] = viv_relay_credentials(mail_root)
    for actor in actors:
        if actor["actor_id"] != "viv" and actor["email"] not in all_credentials:
            raise RuntimeError(f"No private Mailu credentials for configured actor {actor['actor_id']}")

    secrets_root = root / "secrets"
    config_root = root / "config"
    state_root = root / "state"
    venv = root / "venv"
    for directory in (root, secrets_root, config_root, state_root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    credentials_path = secrets_root / "mail-credentials.json"
    principals_path = secrets_root / "principals.json"
    agent_path = secrets_root / "agent-client.json"
    portal_session_key = secrets_root / "provider-portal-session-key"
    portal_integration_key = secrets_root / "provider-portal-integration-key"
    write_private_json(credentials_path, all_credentials)
    principal_data = principals(principals_path)
    write_private_json(principals_path, principal_data)
    agent_token = next(token for token, record in principal_data.items() if record["role"] == "agent")
    write_private_json(agent_path, {"base_url": f"http://{args.host}:{args.port}", "bearer_token": agent_token})
    write_private_secret(portal_session_key)
    write_private_secret(portal_integration_key)

    support_actor = next(actor for actor in actors if actor["actor_id"] == company_profile["supportActorId"])
    def create_alias(address, destinations):
        localpart, domain = address.rsplit("@", 1)
        subprocess.run(["docker", "exec", args.mailu_admin_container, "flask", "mailu", "alias",
                        localpart, domain, ",".join(destinations)],
                       check=True, capture_output=True, text=True, timeout=60)
    ensure_recipient_alias(client, company_profile["supportEmail"],
                           [support_actor["email"], company_profile["assistantEmail"]],
                           create_alias=create_alias)
    runtime_config = {
        "state_root": str(state_root),
        "erp_compose": str(private_erp_compose(source, config_root / "erpnext-compose.yml")),
        "erp_base_url": "http://192.0.2.10:8130",
        "erp_credentials_root": str(root / "erpnext/runtime/api-credentials"),
        "support_actor_id": company_profile["supportActorId"],
        "support_address": company_profile["supportEmail"],
        "mail": {
            "actors": actors,
            "delivery_aliases": {company_profile["supportEmail"]: company_profile["supportActorId"]},
            "mailu_smtp_host": MAIL_HOST,
            "mailu_smtp_port": MAIL_SMTP_PORT,
            "mailu_imap_host": MAIL_HOST,
            "mailu_imap_port": 2993,
            "webmail_url": "https://mail.company.example.test/webmail/",
            "tls_ca_file": str(args.tls_ca_file) if args.tls_ca_file else None,
            "smtp4dev_api_base_url": "http://127.0.0.1:19157",
            "smtp4dev_mailbox_name": "Default",
        },
        "mail_credentials_file": str(credentials_path),
        "employee_chat_credentials_file": str(secrets_root / "employee-chat-credentials.json"),
        "rocketchat_base_url": "https://chat.company.example.test",
        "principals_file": str(principals_path),
        "workers_enabled": True,
        "host": args.host,
        "port": args.port,
    }
    previous_config = config_root / "runtime.json"
    previous_runtime = private_json(previous_config) if previous_config.is_file() else None
    supplied_binding = private_json(args.company_binding) if args.company_binding is not None else None
    runtime_config = preserve_runtime_settings(runtime_config, previous_runtime, supplied_binding)
    write_private_json(previous_config, runtime_config)
    provider_portal_config = {
        "runtime_config_file": str(previous_config),
        "session_key_file": str(portal_session_key),
        "integration_key_file": str(portal_integration_key),
        "provider_worker_url": "http://127.0.0.1:8141",
        "provider_mail_actors": {
            provider_id: identity["actor_id"]
            for provider_id, identity in company_profile["background"]["external_providers"].items()
        },
        "internal_url": f"http://{args.host}:8132",
        "public_url": args.provider_portal_public_url,
    }
    write_private_json(config_root / "provider-portal.json", provider_portal_config)

    if not venv.exists():
        subprocess.run([args.python, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(venv / "bin/python"), "-m", "pip", "install", "--requirement",
                    str(source / "servers/simulation/requirements.txt")], check=True)

    print("Coordinator configuration and private dependencies prepared; service not started.")
    print(f"Source: {source} (dedicated enterprise source tree)")
    print(f"Runtime config: {config_root / 'runtime.json'} (mode 0600)")
    print(f"Agent client config: {agent_path} (agent principal only, mode 0600)")
    print("Provisioned only missing managed counterparty Mailu accounts; existing employee accounts were read-only.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Coordinator bootstrap failed ({type(error).__name__})", file=sys.stderr)
        raise
