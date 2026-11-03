#!/usr/bin/env python3
"""Configure smtp4dev's native authenticated Mailu reply relay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path("/data/theagentcompany/mailu")
SERVICE_ACCOUNT_KEY = "viv_reply_relay"
SERVICE_ACCOUNT_LOCALPART = "viv_reply_relay"
PERSISTENCE_CHECKS = 5


def load_relay_credentials(path: Path = ROOT / "service-credentials.json",
                          mail_domain: str = "example.com") -> tuple[str, str]:
    info = path.stat()
    if info.st_mode & 0o077:
        raise PermissionError("Mailu service credential file must be mode 0600")
    records = json.loads(path.read_text(encoding="utf-8"))
    account = records.get("accounts", {}).get(SERVICE_ACCOUNT_KEY, {})
    mail = account.get("mail", {}) if isinstance(account, dict) else {}
    email = f"{SERVICE_ACCOUNT_LOCALPART}@{mail_domain}"
    if (
        mail.get("address") != email
        or mail.get("status") != "ready"
        or not isinstance(mail.get("password"), str)
        or not mail["password"]
        or account.get("allow_spoofing") is not False
        or account.get("enable_imap") is not False
    ):
        raise RuntimeError("Managed non-spoofing Viv relay credentials are unavailable")
    return email, mail["password"]


def _request_json(base_url: str, method: str, body: dict | None, opener):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + "/api/Server",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with opener(request, timeout=15) as response:
            status = response.status
            payload = response.read()
    except HTTPError as error:
        raise RuntimeError(f"smtp4dev Server {method} failed (HTTP {error.code})") from error
    except URLError as error:
        raise RuntimeError(f"smtp4dev Server {method} could not be reached") from error
    if not payload:
        return status, None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("smtp4dev Server returned invalid JSON") from error
    return status, value


def configure_reply_relay(
    base_url: str = "http://127.0.0.1:19157",
    credentials_path: Path = ROOT / "service-credentials.json",
    *,
    mail_domain: str = "example.com",
    relay_host: str = "mail.company.example.test",
    relay_port: int = 2587,
    viv_email: str | None = None,
    opener=urlopen,
) -> int:
    login, password = load_relay_credentials(credentials_path, mail_domain)
    _, settings = _request_json(base_url, "GET", None, opener)
    if not isinstance(settings, dict):
        raise RuntimeError("smtp4dev Server returned an unexpected settings object")
    condition = f"recipient.toLowerCase().endsWith({json.dumps('@' + mail_domain.lower())})"
    if viv_email is not None:
        condition = f"recipient.toLowerCase() !== {json.dumps(viv_email.lower())} && {condition}"
    expression = condition + " ? recipient : null"
    settings.update(
        relaySmtpServer=relay_host,
        relaySmtpPort=relay_port,
        relayTlsMode="StartTls",
        relayLogin=login,
        relayPassword=password,
        relaySenderAddress="",
        relayAutomaticRelayExpression=expression,
    )
    status, _ = _request_json(base_url, "POST", settings, opener)
    expected = {
        "relaySmtpServer": relay_host,
        "relaySmtpPort": relay_port,
        "relayTlsMode": "StartTls",
        "relayLogin": login,
        "relayPassword": password,
        "relaySenderAddress": "",
        "relayAutomaticRelayExpression": expression,
    }
    mismatches = ["settings"]
    for _ in range(PERSISTENCE_CHECKS):
        _, verified = _request_json(base_url, "GET", None, opener)
        if not isinstance(verified, dict):
            mismatches = ["settings"]
            continue
        mismatches = [
            key for key, value in expected.items()
            if key != "relayPassword" and verified.get(key) != value
        ]
        # smtp4dev deployments may either omit/redact the password or return
        # it. If returned, it must match the protected Mailu credential.
        if verified.get("relayPassword") not in (None, "") and verified.get("relayPassword") != password:
            mismatches.append("relayPassword")
        if verified.get("relayAutomaticRelayExpression") != expression:
            mismatches.append("relayAutomaticRelayExpression")
        if verified.get("relayAutomaticEmails") != settings.get("relayAutomaticEmails"):
            mismatches.append("relayAutomaticEmails")
        if not mismatches:
            return status
    fields = ", ".join(sorted(set(mismatches)))
    raise RuntimeError(f"smtp4dev relay settings did not persist; mismatched fields: {fields}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--credentials", type=Path, default=ROOT / "service-credentials.json")
    args = parser.parse_args()
    viv_email = None
    try:
        if args.profile is None:
            base_url = args.base_url or "http://127.0.0.1:19157"
            mail_domain, relay_host, relay_port = "example.com", "mail.company.example.test", 2587
        else:
            if args.profile.stat().st_mode & 0o077:
                raise PermissionError("deployment profile must be mode 0600")
            profile = json.loads(args.profile.read_text(encoding="utf-8"))
            viv = profile.get("viv")
            domains = profile.get("domains")
            if not isinstance(viv, dict) or not isinstance(viv.get("api_base_url"), str):
                raise ValueError("deployment profile does not contain viv.api_base_url")
            viv_email = viv['email']
            if not isinstance(domains, dict) or not isinstance(domains.get("mail"), str):
                raise ValueError("deployment profile does not contain domains.mail")
            identities = json.loads((ROOT / "company-identities.json").read_text(encoding="utf-8"))
            support_email = identities.get("company", {}).get("supportEmail")
            if not isinstance(support_email, str) or "@" not in support_email:
                raise RuntimeError("Managed company mail domain is unavailable")
            mail_domain = support_email.rsplit("@", 1)[1]
            relay_host = domains["mail"]
            ports = profile.get("mail_ports", {})
            relay_port = int(ports.get("submission", 2587))
            base_url = args.base_url or viv["api_base_url"]
        status = configure_reply_relay(base_url, args.credentials, mail_domain=mail_domain,
                                       relay_host=relay_host, relay_port=relay_port, viv_email=viv_email)
    except Exception as error:
        print(f"smtp4dev relay configuration failed ({type(error).__name__})", file=sys.stderr)
        raise
    print(f"Authenticated smtp4dev reply relay configured; HTTP {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
