#!/usr/bin/env python3
"""Back up and configure Rocket.Chat 5.3 and ownCloud SMTP for private Mailu.

Run on the Mini with ``python3 configure-company-smtp.py --apply``. The script
never prints credentials or provider response bodies. Backups are written with
mode 0600 outside the repository under /data/theagentcompany/mailu.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path("/data/theagentcompany/mailu")
BACKUP_ROOT = ROOT / "app-smtp-backups"
ROCKET_URL = "http://127.0.0.1:3000/api/v1/"
SMTP_HOST = "mail.company.example.test"
SMTP_PORT = 2587
MAIL_DOMAIN = "example.com"
FROM_ADDRESS = "postmaster@example.com"
ROCKET_SETTINGS = (
    "SMTP_Protocol", "SMTP_Host", "SMTP_Port", "SMTP_IgnoreTLS",
    "SMTP_Username", "SMTP_Password", "From_Email",
)
OWNCLOUD_SETTINGS = (
    "mail_smtpmode", "mail_smtphost", "mail_smtpport", "mail_smtpsecure",
    "mail_smtpauth", "mail_smtpauthtype", "mail_smtpname", "mail_smtppassword",
    "mail_from_address", "mail_domain",
)


def private_json(path: Path, value: dict) -> None:
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


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({args[0]}), exit {result.returncode}")
    return result.stdout


def mail_credentials() -> tuple[str, str]:
    data = json.loads((ROOT / "service-credentials.json").read_text(encoding="utf-8"))
    account = data["accounts"]["postmaster"]["mail"]
    address = account["address"]
    if address != FROM_ADDRESS or not account.get("password"):
        raise RuntimeError("Configured postmaster mailbox credentials are unavailable or inconsistent")
    return address, account["password"]


def rocketchat_admin() -> tuple[str, str]:
    env_text = run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", "rocketchat"])
    env = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
    username, password = env.get("ADMIN_USERNAME"), env.get("ADMIN_PASS")
    if not username or not password:
        raise RuntimeError("Rocket.Chat administrator login is unavailable")
    return username, password


class RocketAPI:
    def __init__(self, username: str, password: str):
        self.admin_password_digest = hashlib.sha256(password.encode()).hexdigest()
        login = self.request("POST", "login", {"user": username, "password": password})
        data = login.get("data") or {}
        if login.get("status") != "success" or not data.get("userId") or not data.get("authToken"):
            raise RuntimeError("Rocket.Chat administrator login failed")
        self.headers = {"X-User-Id": data["userId"], "X-Auth-Token": data["authToken"]}

    def request(self, method: str, endpoint: str, payload: dict | None = None,
                extra_headers: dict[str, str] | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if hasattr(self, "headers"):
            headers.update(self.headers)
        if extra_headers:
            headers.update(extra_headers)
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
            raise RuntimeError(f"Rocket.Chat API {method} {endpoint.split('?')[0]} returned HTTP {error.code} ({code})") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Rocket.Chat API {method} {endpoint.split('?')[0]} could not be completed") from error
        if result.get("success") is False or result.get("status") == "error":
            code = result.get("errorType") or "unsuccessful_response"
            raise RuntimeError(f"Rocket.Chat API {method} {endpoint.split('?')[0]} failed ({code})")
        return result

    def settings(self) -> dict[str, object]:
        found = {}
        offset = 0
        while True:
            result = self.request("GET", f"settings?count=100&offset={offset}")
            items = result.get("settings")
            if not isinstance(items, list):
                raise RuntimeError("Rocket.Chat settings response has an unexpected shape")
            found.update({item["_id"]: item.get("value") for item in items
                          if isinstance(item, dict) and item.get("_id") in ROCKET_SETTINGS})
            offset += len(items)
            if offset >= int(result.get("total") or 0) or not items:
                return found

    def set(self, key: str, value: object) -> None:
        self.request("POST", f"settings/{quote(key, safe='')}", {"value": value}, {
            "x-2fa-code": self.admin_password_digest,
            "x-2fa-method": "password",
        })


def owncloud_settings() -> dict[str, object]:
    php = r'''require '/var/www/html/lib/base.php';
$keys = json_decode(stream_get_contents(STDIN), true);
$out = [];
foreach ($keys as $key) { $value = \OC::$server->getConfig()->getSystemValue($key, null);
if ($value !== null) { $out[$key] = $value; } }
echo json_encode((object)$out);'''
    output = run(["docker", "exec", "-i", "--user", "www-data", "owncloud", "php", "-r", php],
                 input_text=json.dumps(list(OWNCLOUD_SETTINGS)))
    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("ownCloud configuration could not be read") from error
    if not isinstance(result, dict):
        raise RuntimeError("ownCloud configuration response has an unexpected shape")
    return result


def set_owncloud(values: dict[str, object]) -> None:
    for key, value in values.items():
        if key == "mail_smtppassword":
            continue
        kind = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
        run(["docker", "exec", "--user", "www-data", "owncloud", "php", "occ",
             "config:system:set", key, "--value=" + str(value).lower() if isinstance(value, bool)
             else "--value=" + str(value), "--type=" + kind])
    php = r'''require '/var/www/html/lib/base.php';
$values = json_decode(stream_get_contents(STDIN), true);
foreach ($values as $key => $value) { \OC::$server->getConfig()->setSystemValue($key, $value); }
echo 'ok';'''
    output = run(["docker", "exec", "-i", "--user", "www-data", "owncloud", "php", "-r", php],
                 input_text=json.dumps({"mail_smtppassword": values["mail_smtppassword"]}))
    if output.strip() != "ok":
        raise RuntimeError("ownCloud did not confirm the SMTP secret update")


def make_backup(rocket: dict[str, object], owncloud: dict[str, object]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_ROOT.chmod(0o700)
    backup = BACKUP_ROOT / f"before-{timestamp}"
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    private_json(backup / "rocketchat-smtp.json", rocket)
    private_json(backup / "owncloud-smtp.json", owncloud)
    return backup


def apply() -> dict[str, object]:
    global CURRENT_PHASE
    CURRENT_PHASE = "read-credentials"
    address, password = mail_credentials()
    CURRENT_PHASE = "connect-rocketchat"
    admin_user, admin_password = rocketchat_admin()
    rocket = RocketAPI(admin_user, admin_password)
    CURRENT_PHASE = "read-rocketchat-settings"
    old_rocket = rocket.settings()
    missing = set(ROCKET_SETTINGS) - set(old_rocket)
    if missing:
        raise RuntimeError("Rocket.Chat SMTP settings are unavailable: " + ", ".join(sorted(missing)))
    CURRENT_PHASE = "read-owncloud-settings"
    old_owncloud = owncloud_settings()
    CURRENT_PHASE = "backup-existing-settings"
    backup = make_backup(old_rocket, old_owncloud)

    CURRENT_PHASE = "update-rocketchat-settings"
    new_rocket = {
        "SMTP_Protocol": "smtp", "SMTP_Host": SMTP_HOST, "SMTP_Port": "2587",
        "SMTP_IgnoreTLS": False, "SMTP_Username": address,
        "SMTP_Password": password, "From_Email": address,
    }
    for key, value in new_rocket.items():
        rocket.set(key, value)
    CURRENT_PHASE = "verify-rocketchat-settings"
    current_rocket = rocket.settings()
    for key, expected in new_rocket.items():
        if current_rocket.get(key) != expected:
            raise RuntimeError(f"Rocket.Chat SMTP verification failed for {key}")

    new_owncloud = {
        "mail_smtpmode": "smtp", "mail_smtphost": SMTP_HOST, "mail_smtpport": SMTP_PORT,
        "mail_smtpsecure": "tls", "mail_smtpauth": True, "mail_smtpauthtype": "LOGIN",
        "mail_smtpname": address, "mail_smtppassword": password,
        "mail_from_address": "postmaster", "mail_domain": MAIL_DOMAIN,
    }
    CURRENT_PHASE = "update-owncloud-settings"
    set_owncloud(new_owncloud)
    CURRENT_PHASE = "verify-owncloud-settings"
    current_owncloud = owncloud_settings()
    for key, expected in new_owncloud.items():
        if current_owncloud.get(key) != expected:
            raise RuntimeError(f"ownCloud SMTP verification failed for {key}")

    return {"backup_directory": str(backup), "rocketchat": "configured and read back",
            "owncloud": "configured and read back", "smtp_host": SMTP_HOST,
            "smtp_port": SMTP_PORT, "tls": "STARTTLS with certificate verification",
            "sender": address}


def restore(backup: Path) -> dict[str, object]:
    """Restore only the SMTP keys saved before this integration."""
    old_rocket=json.loads((backup/'rocketchat-smtp.json').read_text())
    old_owncloud=json.loads((backup/'owncloud-smtp.json').read_text())
    if set(old_rocket)-set(ROCKET_SETTINGS) or set(old_owncloud)-set(OWNCLOUD_SETTINGS):
        raise ValueError('Backup contains settings outside the SMTP restore scope')
    username,password=rocketchat_admin()
    rocket=RocketAPI(username,password)
    for key,value in old_rocket.items(): rocket.set(key,value)
    php=r'''require '/var/www/html/lib/base.php';
$data=json_decode(stream_get_contents(STDIN),true);
$config=\OC::$server->getConfig();
foreach ($data['keys'] as $key) {
 if (array_key_exists($key,$data['values'])) { $config->setSystemValue($key,$data['values'][$key]); }
 else { $config->deleteSystemValue($key); }
}
echo 'ok';'''
    result=run(['docker','exec','-i','--user','www-data','owncloud','php','-r',php],
        input_text=json.dumps({'keys':list(OWNCLOUD_SETTINGS),'values':old_owncloud}))
    if result.strip()!='ok': raise RuntimeError('ownCloud did not confirm SMTP restoration')
    if rocket.settings()!=old_rocket or owncloud_settings()!=old_owncloud:
        raise RuntimeError('Restored SMTP settings did not match the private backup')
    return {'status':'restored','scope':'SMTP settings only','backup_directory':str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="back up current settings and configure both apps")
    action.add_argument("--restore",type=Path,help="restore SMTP settings from a private pre-change backup directory")
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.restore) if args.restore else apply(), indent=2))
    except Exception as error:
        # Do not echo exception text from subprocesses or providers; it may contain secrets.
        message = str(error) if type(error) is RuntimeError else "operation failed"
        print(json.dumps({"status": "failed", "phase": globals().get("CURRENT_PHASE", "startup"),
                          "error_type": type(error).__name__, "detail": message}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
