#!/usr/bin/env python3
"""Provision the existing Rocket.Chat operator mailbox and verify its SMTP test."""

from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import imaplib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path("/data/theagentcompany/mailu")
MAIL_DOMAIN = "example.com"
OPERATOR_ID = "theagentcompany"
OPERATOR_NAME = "TheAgentCompany operator"
ROCKET_URL = "http://127.0.0.1:3000/api/v1/"
SMTP_HOST = "mail.company.example.test"
FROM_ADDRESS = "postmaster@example.com"
STATE_PATH = ROOT / "rocketchat-smtp-test-state.json"


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def run(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode})")
    return result.stdout


def docker_env(container: str) -> dict[str, str]:
    output = run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container])
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


class MailuAPI:
    def __init__(self):
        self.base = "http://127.0.0.1:8112/api/v1"
        self.token = self._mailu_token()

    @staticmethod
    def _mailu_token() -> str:
        for line in (ROOT / "mailu.env").read_text(encoding="utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip()
        raise RuntimeError("Mailu API token is not configured")

    def request(self, method: str, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"Mailu API returned HTTP {status}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("Mailu API request could not be completed") from error

    def exists(self, address: str) -> bool:
        try:
            self.request("GET", "/user/" + quote(address, safe=""))
            return True
        except RuntimeError as error:
            if str(error) == "Mailu API returned HTTP 404":
                return False
            raise

    def create(self, address: str, name: str, password: str) -> bool:
        try:
            self.request("POST", "/user", {
                "email": address, "raw_password": password, "displayed_name": name,
                "enabled": True, "enable_imap": True, "enable_pop": False,
                "allow_spoofing": False, "global_admin": False,
            })
            return True
        except RuntimeError as error:
            if "HTTP 409" in str(error) and self.exists(address):
                return False
            raise


def ensure_operator_mailbox() -> tuple[str, str, bool]:
    credentials_path = ROOT / "service-credentials.json"
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    accounts = credentials.setdefault("accounts", {})
    email_address = f"{OPERATOR_ID}@{MAIL_DOMAIN}"
    entry = accounts.get(OPERATOR_ID)
    if entry:
        mail = entry.get("mail") or {}
        if mail.get("address") != email_address or not mail.get("password"):
            raise RuntimeError("Operator mailbox credential mapping is inconsistent")
        if mail.get("status") == "ready":
            return email_address, mail["password"], False
        password = mail["password"]
    else:
        if MailuAPI().exists(email_address):
            raise RuntimeError("Operator mailbox already exists without a managed password; leaving it unchanged")
        password = secrets.token_urlsafe(36)
        accounts[OPERATOR_ID] = {"mail": {"address": email_address, "password": password, "status": "pending"}}
        write_private(credentials_path, credentials)

    created = MailuAPI().create(email_address, OPERATOR_NAME, password)
    if not created:
        accounts.pop(OPERATOR_ID, None)
        write_private(credentials_path, credentials)
        raise RuntimeError("Operator mailbox appeared during creation; credentials were not assumed")
    accounts[OPERATOR_ID]["mail"]["status"] = "ready"
    write_private(credentials_path, credentials)
    return email_address, password, True


class RocketAPI:
    def __init__(self, username: str, password: str):
        self.password_digest = hashlib.sha256(password.encode()).hexdigest()
        login = self.request("POST", "login", {"user": username, "password": password})
        data = login.get("data") or {}
        if login.get("status") != "success" or not data.get("userId") or not data.get("authToken"):
            raise RuntimeError("Rocket.Chat login failed")
        self.user_id = data["userId"]
        self.headers = {"X-User-Id": self.user_id, "X-Auth-Token": data["authToken"]}

    def request(self, method: str, endpoint: str, payload: dict | None = None,
                with_password_2fa: bool = False) -> dict:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if hasattr(self, "headers"):
            headers.update(self.headers)
        if with_password_2fa:
            headers.update({"x-2fa-code": self.password_digest, "x-2fa-method": "password"})
        request = Request(ROCKET_URL + endpoint, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                body = json.loads(error.read())
            except (json.JSONDecodeError, OSError):
                body = {}
            code = body.get("errorType") or "unsuccessful_response"
            raise RuntimeError(f"Rocket.Chat API returned HTTP {error.code} ({code})") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("Rocket.Chat API request could not be completed") from error
        if result.get("success") is False or result.get("status") == "error":
            code = result.get("errorType") or "unsuccessful_response"
            raise RuntimeError(f"Rocket.Chat API failed ({code})")
        return result

    def current_user_email(self) -> str:
        result = self.request("GET", "users.info?userId=" + quote(self.user_id, safe=""))
        addresses = [item.get("address", "") for item in (result.get("user") or {}).get("emails", [])]
        return next((address for address in addresses if address), "")

    def send_native_test(self) -> None:
        self.request("POST", "settings/SMTP_Test_Button", {"execute": True}, with_password_2fa=True)


def read_mailbox(address: str, password: str, minimum_uid: int = 0) -> list[dict[str, str]]:
    import ssl

    messages = []
    with imaplib.IMAP4_SSL(SMTP_HOST, 2993, ssl_context=ssl.create_default_context()) as client:
        client.login(address, password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Operator INBOX could not be selected")
        status, data = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("Operator mailbox search failed")
        for uid in data[0].split():
            if int(uid) <= minimum_uid:
                continue
            status, fetched = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID FROM TO)])")
            if status != "OK":
                raise RuntimeError("Operator mailbox message fetch failed")
            raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
            header = email.message_from_bytes(raw, policy=email.policy.default)
            messages.append({
                "uid": uid.decode("ascii"),
                "subject": str(header.get("Subject", "")),
                "message_id": str(header.get("Message-ID", "")),
                "from": str(header.get("From", "")),
                "to": str(header.get("To", "")),
            })
    return messages


def save_test_state(state: dict) -> None:
    write_private(STATE_PATH, state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="create/reconcile mailbox and invoke native test mail")
    args = parser.parse_args()
    if not args.send:
        parser.error("--send is required")

    address, password, created = ensure_operator_mailbox()
    admin_env = docker_env("rocketchat")
    if admin_env.get("ADMIN_EMAIL", "").casefold() != address.casefold():
        raise RuntimeError("Rocket.Chat operator email does not match the operator mailbox")
    api = RocketAPI(admin_env["ADMIN_USERNAME"], admin_env["ADMIN_PASS"])
    if api.current_user_email().casefold() != address.casefold():
        raise RuntimeError("Logged-in Rocket.Chat operator email does not match the mailbox")

    state = None
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if state and state.get("status") == "delivered":
        message = next((item for item in read_mailbox(address, password)
                        if item["uid"] == state.get("uid")
                        and item["message_id"] == state.get("message_id")
                        and item["subject"] == "SMTP Test Email"
                        and FROM_ADDRESS in item["from"].casefold()
                        and address.casefold() in item["to"].casefold()), None)
        if not message:
            raise RuntimeError("Previously verified Rocket.Chat SMTP message is no longer in the operator INBOX")
        result = message
        sent_this_run = False
    else:
        if state is None:
            before = read_mailbox(address, password)
            baseline = max((int(item["uid"]) for item in before), default=0)
            state = {"status": "pending", "baseline_uid": baseline}
            save_test_state(state)
            api.send_native_test()
            state["action_invoked"] = True
            save_test_state(state)
            sent_this_run = True
        else:
            sent_this_run = False
        received = []
        for _ in range(40):
            received = read_mailbox(address, password, int(state["baseline_uid"]))
            match = next((item for item in received
                          if item["subject"] == "SMTP Test Email"
                          if "postmaster@example.com" in item["from"].casefold()
                          and address.casefold() in item["to"].casefold()), None)
            if match:
                result = match
                break
        else:
            raise RuntimeError("Rocket.Chat SMTP test was not found in the operator INBOX; delivery remains uncertain")
        state.update({"status": "delivered", "uid": result["uid"], "message_id": result["message_id"]})
        save_test_state(state)

    employee_credentials = json.loads((ROOT / "employee-credentials.json").read_text(encoding="utf-8"))
    employee_count = len(employee_credentials.get("employees", {}))
    service_accounts = json.loads((ROOT / "service-credentials.json").read_text(encoding="utf-8"))["accounts"]
    print(json.dumps({
        "status": "delivered", "application": "Rocket.Chat native SMTP test",
        "operator_mailbox_created": created, "operator_address": address,
        "employee_mailboxes": employee_count,
        "service_mailboxes": len(service_accounts),
        "total_mailboxes": employee_count + len(service_accounts),
        "inbox_uid": result["uid"], "message_id": result["message_id"],
        "subject": result["subject"], "smtp_test_action_invoked": bool(state.get("action_invoked")),
        "sent_by_this_run": sent_this_run,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error) if type(error) is RuntimeError else "operation failed"
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "detail": detail}), file=sys.stderr)
        raise SystemExit(1)
