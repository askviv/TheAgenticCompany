#!/usr/bin/env python3
"""Provision fresh Mailu and coordinator state from the private workplace profile."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from bootstrap_workplace import _profile


ROOT = Path("/data/theagentcompany")
MAIL_ROOT = ROOT / "mailu"
ENTERPRISE_ROOT = ROOT / "enterprise"
COMPOSE_ROOT = Path("/etc/company-workplace/compose")
TLS_ROOT = Path("/etc/company-clock-tls")
PROFILE_PATH = Path("/etc/company-workplace/deployment-profile.json")


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> str:
    child_env = os.environ.copy() if env is None else env.copy()
    child_env.pop("TAC_SANDBOX_PASSWORD", None)
    result = subprocess.run(command, check=True, env=child_env, text=True,
                            capture_output=capture)
    return result.stdout if capture else ""


def _native_binding(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("site_name"), str):
            return value
    raise RuntimeError("Native ERP company setup did not return its binding")


def _check_sandbox_credentials(path: Path, collection: str, password: str) -> None:
    if not path.exists():
        return
    records = json.loads(path.read_text(encoding="utf-8"))
    values = records.get(collection)
    if not isinstance(values, dict):
        raise RuntimeError(f"Managed credential file {path.name} has an invalid {collection} mapping")
    for entry in values.values():
        mail = entry.get("mail") if isinstance(entry, dict) else None
        current = mail.get("password") if isinstance(mail, dict) else None
        if current is not None and current != password:
            raise RuntimeError("Existing managed mailbox credentials differ from sandbox_password")


def _mail_env(source: Path, profile: dict, mail_domain: str) -> str:
    template = (source / "deploy/single-host/mailu/.env.example").read_text(encoding="utf-8")
    replacements = {
        "REPLACE_WITH_RANDOM_16_BYTE_SECRET": secrets.token_hex(32),
        "172.22.0.0/24": "172.22.0.0/24",
        "DOMAIN=example.com": f"DOMAIN={mail_domain}",
        "HOSTNAMES=mail.company.example.test": f"HOSTNAMES={profile['domains']['mail']}",
        "API_TOKEN=REPLACE_WITH_PRIVATE_API_TOKEN": f"API_TOKEN={secrets.token_urlsafe(48)}",
        "SITENAME=TheAgentCompany Mail": "SITENAME=TheAgentCompany Mail",
    }
    for old, new in replacements.items():
        template = template.replace(old, new)
    return template


def _mail_ports(profile: dict) -> dict[str, int]:
    defaults = {"smtp": 2525, "submission": 2587, "imaps": 2993}
    configured = profile.get("mail_ports", defaults)
    if not isinstance(configured, dict):
        raise ValueError("deployment profile mail_ports must be an object")
    result = {key: configured.get(key, value) for key, value in defaults.items()}
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535
           for value in result.values()):
        raise ValueError("deployment profile mail_ports values must be valid TCP ports")
    if len(set(result.values())) != len(result):
        raise ValueError("deployment profile mail_ports must use distinct TCP ports")
    return result


def _prepare_fresh_paths(source: Path, profile: dict) -> bool:
    marker = ROOT / "fresh-bootstrap.json"
    owned = (MAIL_ROOT, ENTERPRISE_ROOT / "erpnext")
    profile_identity = {
        "domains": profile["domains"],
        "public_ip": profile["public_ip"],
        "mail_ports": profile["mail_ports"],
        "viv": {key: profile["viv"][key] for key in
                ("email", "smtp_host", "smtp_port", "api_base_url", "mailbox_name")},
    }
    if marker.exists():
        record = json.loads(marker.read_text(encoding="utf-8"))
        if (record.get("schema_version") != 1 or record.get("source") != str(source)
                or record.get("profile") != profile_identity):
            raise RuntimeError("Existing fresh-workplace ownership marker does not match this source/profile")
    elif any(path.exists() and any(path.iterdir()) for path in owned):
        raise RuntimeError("Mailu or ERPNext data exists without this bootstrap's ownership marker")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    MAIL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ENTERPRISE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not marker.exists():
        _private_write(marker, json.dumps({"schema_version": 1, "source": str(source),
                                           "profile": profile_identity}, sort_keys=True) + "\n")
        return True
    return False


def _preflight_compose(path: Path, *, fresh: bool) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    project = config.get("name")
    services = config.get("services")
    if not isinstance(project, str) or not isinstance(services, dict):
        raise RuntimeError(f"Resolved Compose project is invalid: {path.name}")
    volumes = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
    known_volumes = set(volumes)
    for logical, definition in config.get("volumes", {}).items():
        definition = definition if isinstance(definition, dict) else {}
        name = definition.get("name", f"{project}_{logical}")
        if name not in known_volumes:
            continue
        if fresh:
            raise RuntimeError(f"Fresh bootstrap found pre-existing data volume for {project}/{logical}")
        labels = subprocess.run(["docker", "volume", "inspect", "--format", "{{json .Labels}}", name],
                                check=True, capture_output=True, text=True).stdout
        volume_labels = json.loads(labels) or {}
        if volume_labels.get("com.docker.compose.project") != project or \
                volume_labels.get("com.docker.compose.volume") != logical:
            raise RuntimeError(f"Refusing to reuse unowned Compose volume {name}")
    containers = subprocess.run(["docker", "compose", "--file", str(path), "ps", "--all", "--quiet"],
                                check=True, capture_output=True, text=True).stdout.splitlines()
    if containers and fresh:
        raise RuntimeError(f"Fresh bootstrap found pre-existing containers for {project}")
    if containers:
        inspected = subprocess.run(["docker", "inspect", *containers], check=True,
                                    capture_output=True, text=True).stdout
        for container in json.loads(inspected):
            labels = container.get("Config", {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.project") != project or service not in services:
                raise RuntimeError(f"Refusing to reuse an unowned container in {project}")


def _mailu_compose(source: Path, profile: dict, mail_domain: str, ports: dict[str, int]) -> Path:
    compose_dir = COMPOSE_ROOT
    compose_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    mail_env = MAIL_ROOT / "mailu.env"
    if mail_env.exists():
        if mail_env.stat().st_mode & 0o077:
            raise PermissionError("Existing Mailu environment file must be private")
        content = mail_env.read_text(encoding="utf-8")
        if f"DOMAIN={mail_domain}" not in content or f"HOSTNAMES={profile['domains']['mail']}" not in content:
            raise RuntimeError("Existing Mailu configuration does not match the supplied fresh profile")
    else:
        _private_write(mail_env, _mail_env(source, profile, mail_domain))

    tls_cert = TLS_ROOT / "fullchain.pem"
    tls_key = TLS_ROOT / "server.key"
    if not tls_cert.is_file() or not tls_key.is_file():
        raise FileNotFoundError("Workplace TLS must be prepared before starting Mailu")
    tls_data = MAIL_ROOT / "internal-https-tls"
    tls_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(tls_cert, tls_data / "fullchain.pem")
    shutil.copyfile(tls_key, tls_data / "privkey.pem")
    (tls_data / "fullchain.pem").chmod(0o644)
    (tls_data / "privkey.pem").chmod(0o600)

    yaml_path = compose_dir / "mailu.yml"
    compose = (source / "deploy/single-host/mailu/docker-compose.yml").read_text(encoding="utf-8")
    compose = compose.replace("192.0.2.10:2525:25", f"0.0.0.0:{ports['smtp']}:25")
    compose = compose.replace("192.0.2.10:2587:587", f"0.0.0.0:{ports['submission']}:587")
    compose = compose.replace("192.0.2.10:2993:993", f"0.0.0.0:{ports['imaps']}:993")
    # Mailu loads native Postfix/Rspamd overrides from its persistent data
    # directory. Preserve the repository's complete configuration, then render
    # only the two exact Viv mappings from the private deployment profile.
    overrides_source = source / "deploy/single-host/mailu/overrides"
    overrides_target = MAIL_ROOT / "overrides"
    shutil.copytree(overrides_source, overrides_target, dirs_exist_ok=True)
    viv_email = profile["viv"]["email"]
    smtp_host = str(profile["viv"]["smtp_host"])
    smtp_port = int(profile["viv"]["smtp_port"])
    if any(character in smtp_host for character in "\r\n[]"):
        raise ValueError("deployment profile viv.smtp_host is invalid for a Postfix transport map")
    if "@" not in viv_email or any(character.isspace() for character in viv_email):
        raise ValueError("deployment profile viv.email is invalid for a Postfix lookup map")
    postfix_dir = overrides_target / "postfix"
    _private_write(postfix_dir / "recipient-transport.map",
                   f"{viv_email} smtp:[{smtp_host}]:{smtp_port}\n")
    _private_write(postfix_dir / "viv-sender-login.map",
                   f"{viv_email} viv_reply_relay@{mail_domain}\n")
    _private_write(yaml_path, compose)
    json_path = compose_dir / "mailu.json"
    rendered = _run(["docker", "compose", "--file", str(yaml_path), "config", "--format", "json"], capture=True)
    _private_write(json_path, rendered)
    return json_path


def _prepare_erp(source: Path, profile: dict, sandbox_password: str, identities_path: Path) -> Path:
    _run(["bash", str(source / "deploy/enterprise-simulation/erpnext/business-apps/build.sh")])
    erp_root = ENTERPRISE_ROOT / "erpnext"
    secrets_root = erp_root / "secrets"
    secrets_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in (
        ("db-root-password", secrets.token_urlsafe(48)),
        ("site-admin-password", sandbox_password),
    ):
        path = secrets_root / name
        if path.exists():
            if path.stat().st_mode & 0o077 or not path.read_text(encoding="utf-8").strip():
                raise PermissionError(f"Existing ERPNext secret {name} is invalid")
            if name == "site-admin-password" and path.read_text(encoding="utf-8").strip() != value:
                raise RuntimeError("Existing ERPNext site-admin credential differs from sandbox_password")
        else:
            _private_write(path, value + "\n")
    config_root = ENTERPRISE_ROOT / "config"
    config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    yaml_path = config_root / "erpnext-compose.yml"
    compose = (source / "deploy/enterprise-simulation/erpnext/compose.yml").read_text(encoding="utf-8")
    compose = compose.replace("192.0.2.10", "127.0.0.1")
    compose = compose.replace("mail.company.example.test", profile["domains"]["mail"])
    compose = compose.replace(f"{profile['domains']['mail']}:127.0.0.1",
                              f"{profile['domains']['mail']}:host-gateway")
    compose = compose.replace("/data/theagentcompany/source/", str(source) + "/")
    compose = compose.replace(
        str(source / "deploy/single-host/company-identities.json"), str(identities_path)
    )
    _private_write(yaml_path, compose)
    json_path = COMPOSE_ROOT / "erpnext.json"
    rendered = json.loads(_run(["docker", "compose", "--file", str(yaml_path), "--profile", "admin", "config", "--format", "json"], capture=True))
    rendered['services']['site-admin']['environment']['TAC_ERP_PUBLIC_URL'] = f"https://{profile['domains']['erp']}"
    _private_write(json_path, json.dumps(rendered, indent=2) + "\n")
    return json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/agentic-world/source"))
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("must run as root")
    if args.profile.stat().st_mode & 0o077:
        raise PermissionError("deployment profile must be mode 0600")
    source = args.source.resolve(strict=True)
    profile = _profile(args.profile)
    try:
        public_ip = str(ipaddress.ip_address(profile["public_ip"]))
    except (KeyError, ValueError) as error:
        raise ValueError("deployment profile public_ip must be a valid IP address") from error
    viv = profile.get("viv")
    if not isinstance(viv, dict) or not all(viv.get(key) for key in
                                               ("email", "smtp_host", "smtp_port", "api_base_url", "mailbox_name")):
        raise ValueError("deployment profile viv object is incomplete")
    sandbox_password = profile.get("sandbox_password")
    if not isinstance(sandbox_password, str) or not sandbox_password:
        raise ValueError("deployment profile must include sandbox_password")
    source_identities = source / "deploy/single-host/company-identities.json"
    identities = json.loads(source_identities.read_text(encoding="utf-8"))
    identities["company"]["assistantEmail"] = viv["email"]
    identities_path = Path("/etc/company-workplace/company-identities.json")
    mail_domain = identities["company"]["supportEmail"].rsplit("@", 1)[1].lower()
    profile["public_ip"] = public_ip
    mail_ports = _mail_ports(profile)
    profile["mail_ports"] = mail_ports
    fresh = _prepare_fresh_paths(source, profile)
    sandbox_password_file = Path("/etc/company-workplace/secrets/sandbox-password")
    if sandbox_password_file.exists():
        if (stat.S_IMODE(sandbox_password_file.stat().st_mode) != 0o600
                or sandbox_password_file.read_text(encoding="utf-8").rstrip("\r\n") != sandbox_password):
            raise RuntimeError("Existing sandbox password file differs from the supplied fresh profile")
    else:
        _private_write(sandbox_password_file, sandbox_password + "\n")
    _private_write(identities_path, json.dumps(identities, sort_keys=True, indent=2) + "\n")
    _private_write(MAIL_ROOT / "company-identities.json", json.dumps(identities, sort_keys=True, indent=2) + "\n")
    mail_compose = _mailu_compose(source, profile, mail_domain, mail_ports)
    _preflight_compose(mail_compose, fresh=fresh)
    _run(["docker", "compose", "--file", str(mail_compose), "up", "-d", "--wait"])

    _check_sandbox_credentials(MAIL_ROOT / "employee-credentials.json", "employees", sandbox_password)
    _check_sandbox_credentials(MAIL_ROOT / "enterprise-counterparty-credentials.json", "accounts", sandbox_password)
    provision = source / "deploy/single-host/mailu/provision.py"
    _run([sys.executable, str(provision), "--root", str(MAIL_ROOT), "--identities",
          str(identities_path), "--api", "http://127.0.0.1:8112/api/v1", "--mail-domain", mail_domain,
          "--smtp-host", profile["domains"]["mail"], "--smtp-port", "12587",
          "--mailbox-password-file", str(sandbox_password_file)])

    erp_compose = _prepare_erp(source, profile, sandbox_password, identities_path)
    _preflight_compose(erp_compose, fresh=fresh)
    _run(["docker", "compose", "--file", str(erp_compose), "up", "-d", "--wait"])
    site_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(ENTERPRISE_ROOT / "company-baseline")))
    setup_env = os.environ.copy()
    setup_env.update({"TAC_MAIL_HOST": profile["domains"]["mail"],
                      "TAC_MAIL_SMTP_PORT": "12587"})
    setup = _run(["docker", "compose", "--profile", "admin", "--file", str(erp_compose),
                  "run", "--rm", "-e", "TAC_MAIL_HOST", "-e", "TAC_MAIL_SMTP_PORT",
                  "site-admin", "company-only", site_id],
                 env=setup_env, capture=True)
    binding_path = ENTERPRISE_ROOT / "config" / "company-binding.json"
    binding = _native_binding(setup)
    _private_write(binding_path, json.dumps(binding, sort_keys=True) + "\n")
    # The public hostname is configurable; Frappe's native site name is the
    # actual site returned by setup. Serve that one shared company explicitly.
    erp_config = json.loads(erp_compose.read_text())
    erp_config['services']['frontend']['environment']['FRAPPE_SITE_NAME_HEADER'] = binding['site_name']
    _private_write(erp_compose, json.dumps(erp_config, indent=2) + '\n')
    _run(['docker', 'compose', '--file', str(erp_compose), 'up', '-d', '--no-deps', 'frontend'])

    coordinator = source / "deploy/enterprise-simulation/coordinator/bootstrap.py"
    _run([sys.executable, str(coordinator), "--root", str(ENTERPRISE_ROOT), "--source", str(ENTERPRISE_ROOT / "source"),
          "--employee-cua-model", profile["codex"].get("employee_model", "gpt-6.1-terra"),
          "--mail-root", str(MAIL_ROOT), "--mail-api", "http://127.0.0.1:8112/api/v1",
          "--mail-domain", mail_domain, "--mail-host", profile["domains"]["mail"],
          "--mail-smtp-port", "12587",
          "--mail-imap-port", "12993",
          "--mail-webmail-url", f"https://{profile['domains']['mail']}/webmail/",
          "--erp-base-url", f"https://{profile['domains']['erp']}",
          "--chat-url", f"https://{profile['domains']['chat']}",
          "--viv-api-url", viv["api_base_url"], "--viv-smtp-host", viv["smtp_host"],
          "--viv-smtp-port", str(int(viv["smtp_port"])), "--viv-mailbox-name", viv["mailbox_name"],
          "--viv-email", viv["email"], "--company-profile", str(identities_path),
          "--host", "0.0.0.0", "--agent-base-url", "http://127.0.0.1:8131",
          "--provider-portal-public-url", f"https://{profile['domains']['experiments']}",
          "--tls-ca-file", str(TLS_ROOT / "root-ca.crt"), "--company-binding", str(binding_path),
          "--sandbox-password-file", str(sandbox_password_file)])
    print("Fresh Mailu, ERPNext company baseline, and coordinator configuration prepared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
