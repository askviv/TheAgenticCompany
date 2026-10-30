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
SERVICE_ACCOUNT_EMAIL = "viv_reply_relay@example.com"
PERSISTENCE_CHECKS = 5


def load_relay_credentials(path: Path = ROOT / "service-credentials.json") -> tuple[str, str]:
    info = path.stat()
    if info.st_mode & 0o077:
        raise PermissionError("Mailu service credential file must be mode 0600")
    records = json.loads(path.read_text(encoding="utf-8"))
    account = records.get("accounts", {}).get(SERVICE_ACCOUNT_KEY, {})
    mail = account.get("mail", {}) if isinstance(account, dict) else {}
    if (
        mail.get("address") != SERVICE_ACCOUNT_EMAIL
        or mail.get("status") != "ready"
        or not isinstance(mail.get("password"), str)
        or not mail["password"]
        or account.get("allow_spoofing") is not False
        or account.get("enable_imap") is not False
    ):
        raise RuntimeError("Managed non-spoofing Viv relay credentials are unavailable")
    return SERVICE_ACCOUNT_EMAIL, mail["password"]


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
    opener=urlopen,
) -> int:
    login, password = load_relay_credentials(credentials_path)
    _, settings = _request_json(base_url, "GET", None, opener)
    if not isinstance(settings, dict):
        raise RuntimeError("smtp4dev Server returned an unexpected settings object")
    settings.update(
        relaySmtpServer="mail.company.example.test",
        relaySmtpPort=2587,
        relayTlsMode="StartTls",
        relayLogin=login,
        relayPassword=password,
        relaySenderAddress="",
    )
    status, _ = _request_json(base_url, "POST", settings, opener)
    expected = {
        "relaySmtpServer": "mail.company.example.test",
        "relaySmtpPort": 2587,
        "relayTlsMode": "StartTls",
        "relayLogin": SERVICE_ACCOUNT_EMAIL,
        "relayPassword": password,
        "relaySenderAddress": "",
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
        if verified.get("relayAutomaticRelayExpression") != settings.get("relayAutomaticRelayExpression"):
            mismatches.append("relayAutomaticRelayExpression")
        if verified.get("relayAutomaticEmails") != settings.get("relayAutomaticEmails"):
            mismatches.append("relayAutomaticEmails")
        if not mismatches:
            return status
    fields = ", ".join(sorted(set(mismatches)))
    raise RuntimeError(f"smtp4dev relay settings did not persist; mismatched fields: {fields}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19157")
    args = parser.parse_args()
    try:
        status = configure_reply_relay(args.base_url)
    except Exception as error:
        print(f"smtp4dev relay configuration failed ({type(error).__name__})", file=sys.stderr)
        raise
    print(f"Authenticated smtp4dev reply relay configured; HTTP {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
