#!/usr/bin/env python3
"""Import fresh MidPoint resources, seed employee projections and OwnCloud policy."""

from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from bootstrap_workplace import _profile


PROFILE_PATH = Path("/etc/company-workplace/deployment-profile.json")
IDENTITIES_PATH = Path("/etc/company-workplace/company-identities.json")
SECRETS_DIR = Path("/etc/company-workplace/secrets")
COMPOSE_DIR = Path("/etc/company-workplace/compose")
STATE_DIR = Path("/etc/company-workplace/state")
MAIL_ROOT = Path("/data/theagentcompany/mailu")
ENTERPRISE_ROOT = Path("/data/theagentcompany/enterprise")
MIDPOINT_URL = "http://127.0.0.1:18121/midpoint"
COMMON = "http://midpoint.evolveum.com/xml/ns/public/common/common-3"
EA = "https://theagentcompany.com/xml/ns/midpoint/employee-accounts"
RESOURCE_OIDS = {
    "rocketchat": "57a19684-cfe8-4f21-b7b4-0fb02b5ec001",
    "mail": "57a19684-cfe8-4f21-b7b4-0fb02b5ec002",
    "owncloud": "57a19684-cfe8-4f21-b7b4-0fb02b5ec003",
}
ROCKETCHAT_BASE = "http://127.0.0.1:3000"
SEED_ADMIN_PASSWORD = "theagentcompany"


def _private_json(path: Path) -> dict[str, Any]:
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"Private configuration {path} must be owner-only")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Private configuration {path.name} must contain a JSON object")
    return value


def _private_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _mailu_token(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "API_TOKEN" and value.strip():
            return value.strip()
    raise RuntimeError("Fresh Mailu configuration has no API token")


class MidPointRest:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/") + "/"
        self.credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")

    def request(self, method: str, path: str, *, payload: bytes | None = None,
                content_type: str = "application/json", accepted: tuple[int, ...] = (200, 201, 202, 204, 303)):
        request = Request(
            urljoin(self.base_url, path.lstrip("/")), data=payload, method=method,
            headers={"Authorization": f"Basic {self.credentials}", "Accept": "application/json",
                     "Content-Type": content_type},
        )
        try:
            with urlopen(request) as response:
                body = response.read()
                if response.status not in accepted:
                    raise RuntimeError(f"midPoint {method} {path.split('?')[0]} returned HTTP {response.status}")
                return response.status, response.headers.get("Location"), body
        except HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"midPoint {method} {path.split('?')[0]} returned HTTP {status}") from error
        except URLError as error:
            raise RuntimeError(f"midPoint {method} {path.split('?')[0]} could not be reached") from error

    def json(self, method: str, path: str, payload: dict[str, Any] | None = None,
             *, accepted: tuple[int, ...] = (200, 201, 202, 204, 303)) -> tuple[int, str | None, Any]:
        encoded = None if payload is None else json.dumps(payload).encode("utf-8")
        status, location, body = self.request(method, path, payload=encoded, accepted=accepted)
        return status, location, json.loads(body) if body else None

    def task(self, location: str) -> dict[str, Any]:
        task_url = urljoin(self.base_url, location)
        if not task_url.startswith(self.base_url):
            raise RuntimeError("midPoint returned a task URL outside its private API")
        while True:
            status, _, payload = self.json("GET", task_url, accepted=(200,))
            task = payload.get("task") if isinstance(payload, dict) else None
            if not isinstance(task, dict):
                raise RuntimeError("midPoint task readback has an unexpected shape")
            state = task.get("executionState")
            if state == "closed":
                result = task.get("resultStatus")
                if result != "success":
                    raise RuntimeError(f"midPoint task completed with result {result or 'unknown'}")
                return task
            if state not in {"runnable", "waiting", "suspended", "running", None}:
                raise RuntimeError(f"midPoint task returned unexpected execution state {state}")
            time.sleep(2)

    def import_resource(self, oid: str) -> None:
        status, location, _ = self.request(
            "POST", f"ws/rest/resources/{oid}/import/AccountObjectClass", payload=b"",
            content_type="application/json",
        )
        if status != 303 or not location:
            raise RuntimeError(f"midPoint resource import {oid} did not return its task location")
        self.task(location)


class RocketChatAuthenticationError(RuntimeError):
    pass


class RocketChatRest:
    def __init__(self, base_url: str, user_id: str, token: str):
        self.base_url = base_url.rstrip("/") + "/api/v1/"
        self.user_id = user_id
        self.token = token

    def request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            urljoin(self.base_url, endpoint), data=body, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "X-Auth-Token": self.token, "X-User-Id": self.user_id},
        )
        try:
            with urlopen(request) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                if endpoint.startswith("users.info?") and error.code == 400:
                    problem = json.loads(error.read())
                    message = problem.get("error", "") if isinstance(problem, dict) else ""
                    if (isinstance(problem, dict) and problem.get("errorType") in
                            {"error-user-not-found", "error-user-does-not-exist"}) or \
                        (isinstance(message, str) and message.casefold().rstrip(".") == "user not found"):
                        return None
                status = error.code
            finally:
                error.close()
            raise RuntimeError(f"Rocket.Chat {method} {endpoint.split('?')[0]} returned HTTP {status}") from error
        except URLError as error:
            raise RuntimeError(f"Rocket.Chat {method} {endpoint.split('?')[0]} could not be reached") from error
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError(f"Rocket.Chat {method} {endpoint.split('?')[0]} returned an unsuccessful response")
        return result

    def user_info(self, field: str, value: str) -> dict[str, Any] | None:
        if field not in {"userId", "username"}:
            raise ValueError("Unsupported Rocket.Chat user lookup field")
        result = self.request("GET", "users.info?" + urlencode({field: value}))
        if result is None:
            return None
        user = result.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("_id"), str):
            raise RuntimeError("Rocket.Chat users.info response did not include an immutable user ID")
        return user

    def update_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "users.update", {"userId": user_id, "data": data})
        user = result.get("user") if isinstance(result, dict) else None
        if not isinstance(user, dict) or user.get("_id") != user_id:
            raise RuntimeError("Rocket.Chat users.update readback did not match its immutable user ID")
        return user


def _rocket_authentication(base: str, username: str, password: str) -> tuple[str, str]:
    endpoint = base.rstrip("/") + "/api/v1/login"
    payload = json.dumps({"user": username, "password": password}).encode("utf-8")
    request = Request(endpoint, data=payload, method="POST",
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urlopen(request) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 401:
            raise RocketChatAuthenticationError("Rocket.Chat login credentials were rejected") from error
        raise RuntimeError(f"Rocket.Chat login returned HTTP {status}") from error
    except URLError as error:
        raise RuntimeError("Rocket.Chat login could not be completed") from error
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or result.get("status") != "success":
        raise RuntimeError("Rocket.Chat login did not return a successful result")
    user_id, token = data.get("userId"), data.get("authToken")
    if not isinstance(user_id, str) or not user_id or not isinstance(token, str) or not token:
        raise RuntimeError("Rocket.Chat login response lacks its immutable user ID or token")
    return user_id, token


def _wait_midpoint() -> None:
    url = MIDPOINT_URL.rstrip("/") + "/actuator/health"
    while True:
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request) as response:
                if response.status == 200:
                    body = json.loads(response.read())
                    if body.get("status") == "UP":
                        return
                    raise RuntimeError(f"midPoint health endpoint returned {body.get('status')!r}")
        except HTTPError as error:
            if error.code != 503:
                raise
            error.close()
        except URLError as error:
            reason = error.reason
            if not isinstance(reason, OSError) or reason.errno != errno.ECONNREFUSED:
                raise RuntimeError("midPoint health request failed") from error
        time.sleep(2)


def _wait_http(url: str, service: str) -> None:
    while True:
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"})) as response:
                if 200 <= response.status < 300:
                    return
        except HTTPError as error:
            if error.code != 503:
                raise RuntimeError(f"{service} readiness endpoint returned HTTP {error.code}") from error
            error.close()
        except URLError as error:
            reason = error.reason
            if not isinstance(reason, OSError) or reason.errno != errno.ECONNREFUSED:
                raise RuntimeError(f"{service} readiness request failed") from error
        time.sleep(2)


def _rocket_admin(base: str, password: str) -> tuple[str, str, RocketChatRest]:
    username = "theagentcompany"
    try:
        user_id, token = _rocket_authentication(base, username, password)
    except RocketChatAuthenticationError:
        if password == SEED_ADMIN_PASSWORD:
            raise
        seed_user_id, seed_token = _rocket_authentication(base, username, SEED_ADMIN_PASSWORD)
        client = RocketChatRest(base, seed_user_id, seed_token)
        admin = client.user_info("userId", seed_user_id)
        if not isinstance(admin, dict) or admin.get("username") != username:
            raise RuntimeError("Rocket.Chat seed administrator identity does not match its configured account")
        client.update_user(seed_user_id, {"password": password})
        user_id, token = _rocket_authentication(base, username, password)
        if user_id != seed_user_id:
            raise RuntimeError("Rocket.Chat administrator ID changed after password reconciliation")
    return user_id, token, RocketChatRest(base, user_id, token)


def _write_connector_secrets(profile: dict[str, Any], root: Path,
                             rocket_user_id: str, rocket_token: str) -> Path:
    username = profile["company"]["background"]["owncloud"]["service_owner"]["owncloud_uid"]
    payload = {
        "rocketchat": {"auth_token": rocket_token, "user_id": rocket_user_id},
        "mailu": {"api_token": _mailu_token(MAIL_ROOT / "mailu.env")},
        "owncloud": {"username": username, "app_password": profile["sandbox_password"]},
    }
    path = root / "midpoint-connector-secrets.json"
    _private_write(path, payload)
    return path


def _rocket_email(user: dict[str, Any]) -> str | None:
    emails = user.get("emails")
    if not isinstance(emails, list):
        return None
    addresses = [entry.get("address") for entry in emails if isinstance(entry, dict)
                 and isinstance(entry.get("address"), str)]
    return addresses[0] if len(addresses) == 1 else None


def _provision_chat_accounts(client: RocketChatRest, base: str, identities: dict[str, Any],
                             profile: dict[str, Any]) -> dict[str, dict[str, str]]:
    credentials: dict[str, dict[str, str]] = {}
    password = profile["sandbox_password"]
    employees = identities["employees"]
    if len(employees) != 17:
        raise RuntimeError("Fresh Rocket.Chat provisioning requires the approved 17-person roster")
    for employee in employees:
        employee_id = employee["id"]
        account = employee.get("accounts", {}).get("rocketchat")
        if not isinstance(account, dict):
            raise RuntimeError(f"Employee {employee_id} has no configured Rocket.Chat identity")
        username, expected_id = account.get("username"), account.get("id")
        email = employee.get("email")
        if not all(isinstance(value, str) and value for value in (username, expected_id, email)):
            raise RuntimeError(f"Employee {employee_id} has incomplete Rocket.Chat identity data")
        user = client.user_info("userId", expected_id)
        if (not isinstance(user, dict) or user.get("username") != username
                or (_rocket_email(user) or "").casefold() != email.casefold()):
            raise RuntimeError(f"Employee {employee_id} Rocket.Chat user differs from the supplied seed identity")
        client.update_user(expected_id, {"password": password})
        actual_id, _ = _rocket_authentication(base, username, password)
        if actual_id != expected_id:
            raise RuntimeError(f"Employee {employee_id} Rocket.Chat login resolved to a different immutable ID")
        credentials[employee_id] = {"username": username, "password": password}

    viv = profile["viv"]
    username = viv.get("chat_username", "viv")
    email = viv["email"]
    if not isinstance(username, str) or not username or not isinstance(email, str) or not email:
        raise RuntimeError("Viv's Rocket.Chat username and email must be non-empty strings")
    user = client.user_info("username", username)
    if user is None:
        result = client.request("POST", "users.create", {
            "username": username, "name": "Viv", "email": email, "password": password,
            "roles": ["user"], "sendWelcomeEmail": False,
        })
        user = result.get("user") if isinstance(result, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("_id"), str):
            raise RuntimeError("Rocket.Chat users.create did not return Viv's immutable user ID")
        user_id = user["_id"]
    else:
        user_id = user["_id"]
        client.update_user(user_id, {"username": username, "name": "Viv", "email": email,
                                     "password": password})
    verified = client.user_info("userId", user_id)
    if (not verified or verified.get("username") != username
            or (_rocket_email(verified) or "").casefold() != email.casefold()):
        raise RuntimeError("Rocket.Chat Viv account does not match its configured identity")
    login_id, _ = _rocket_authentication(base, username, password)
    if login_id != user_id:
        raise RuntimeError("Rocket.Chat Viv login resolved to a different immutable ID")
    identities["company"].setdefault("background", {})["rocketchat"] = {
        "assistant_username": username, "assistant_email": email, "assistant_user_id": user_id,
    }
    return credentials


def _build_and_install_connectors(source: Path, midpoint_config: Path) -> None:
    connector_dir = source / "deploy/single-host/midpoint/connectors"
    subprocess.run(["bash", str(connector_dir / "build.sh")], cwd=connector_dir, check=True)
    volume = json.loads(midpoint_config.read_text(encoding="utf-8"))["volumes"]["midpoint-home"]
    volume_name = volume.get("name")
    if not volume_name:
        raise RuntimeError("Resolved MidPoint configuration has no midpoint-home volume name")
    mountpoint = Path(subprocess.run(
        ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume_name],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    connector_destination = mountpoint / "connid-connectors"
    connector_destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    connector_destination.chown(1000, 1000)
    built = sorted((connector_dir / "build").glob("connector-*-0.2.0-tac.1.jar"))
    expected = {"connector-rocketchat-0.2.0-tac.1.jar", "connector-mailu-0.2.0-tac.1.jar",
                "connector-owncloud-0.2.0-tac.1.jar"}
    if {path.name for path in built} != expected:
        raise RuntimeError("Pinned native connector build did not produce all three expected bundles")
    for artifact in built:
        destination = connector_destination / artifact.name
        shutil.copyfile(artifact, destination)
        destination.chmod(0o644)
        destination.chown(1000, 1000)
    subprocess.run(["docker", "compose", "--file", str(midpoint_config), "restart", "midpoint"], check=True)


def _import_resources(api: MidPointRest, source: Path, secrets_path: Path, admin_password_path: Path) -> None:
    importer = source / "deploy/single-host/midpoint/connectors/configure_resources.py"
    env = os.environ.copy()
    env.update({
        "ROCKETCHAT_BASE_ADDRESS": "http://rocketchat:3000/api/v1",
        "MAILU_BASE_ADDRESS": "http://mailu-api/api/v1",
        "OWNCLOUD_BASE_ADDRESS": "http://owncloud/ocs/v1.php/cloud",
    })
    subprocess.run([
        sys.executable, str(importer), "--midpoint", MIDPOINT_URL,
        "--secrets", str(secrets_path), "--admin-username", "administrator",
        "--admin-password-file", str(admin_password_path), "--with-owncloud", "--replace-existing",
    ], check=True, env=env)


def _get_shadows(api: MidPointRest, resource_oid: str) -> list[dict[str, Any]]:
    query = f'resourceRef matches (oid = "{resource_oid}") and objectClass = "ri:User"'
    _, _, payload = api.json("POST", "ws/rest/shadows/search", {"query": {"filter": {"text": query}}},
                             accepted=(200,))
    shadows = (((payload or {}).get("object") or {}).get("object"))
    if not isinstance(shadows, list) or any(not isinstance(shadow, dict) for shadow in shadows):
        raise RuntimeError("midPoint shadow import readback has an unexpected shape")
    return shadows


def _one_shadow(shadows: list[dict[str, Any]], attribute: str, expected: str, employee_id: str,
                service: str) -> dict[str, Any]:
    matches = [shadow for shadow in shadows
               if isinstance(shadow.get("attributes"), dict)
               and shadow["attributes"].get(attribute) == expected]
    if len(matches) != 1:
        raise RuntimeError(f"Employee {employee_id} has {len(matches)} {service} shadows matching its immutable ID")
    return matches[0]


def _owner_oid(api: MidPointRest, shadow_oid: str) -> str | None:
    status, _, body = api.request("GET", f"ws/rest/shadows/{shadow_oid}/owner", accepted=(200, 404))
    if status == 404 or not body:
        return None
    payload = json.loads(body)
    if payload is None:
        return None
    owner = payload.get("user", payload.get("object", {}))
    if not isinstance(owner, dict):
        raise RuntimeError("midPoint shadow owner readback has an unexpected shape")
    return owner.get("oid")


def _current_user(api: MidPointRest, oid: str) -> dict[str, Any]:
    _, _, payload = api.json("GET", f"ws/rest/users/{oid}", accepted=(200,))
    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict) or user.get("oid") != oid:
        raise RuntimeError("midPoint employee readback has an unexpected shape")
    return user


def _add_link(api: MidPointRest, user_oid: str, shadow: dict[str, Any], employee_id: str) -> None:
    shadow_oid = shadow.get("oid")
    if not isinstance(shadow_oid, str) or not shadow_oid:
        raise RuntimeError(f"Employee {employee_id} has a shadow without an OID")
    owner_oid = _owner_oid(api, shadow_oid)
    current = _current_user(api, user_oid)
    references = current.get("linkRef", [])
    references = [references] if isinstance(references, dict) else references
    if not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references):
        raise RuntimeError("midPoint user link readback has an unexpected shape")
    existing = [ref for ref in references if ref.get("oid") == shadow_oid]
    if len(existing) > 1:
        raise RuntimeError(f"Employee {employee_id} has a duplicate {shadow.get('resourceRef')} native link")
    if owner_oid not in (None, user_oid):
        raise RuntimeError(f"Employee {employee_id} shadow is owned by a different central identity")
    if existing:
        if owner_oid != user_oid:
            raise RuntimeError(f"Employee {employee_id} native link owner is not confirmed")
        return
    if owner_oid is not None:
        raise RuntimeError(f"Employee {employee_id} shadow owner conflicts with its missing link")
    api.json("PATCH", f"ws/rest/users/{user_oid}", {
        "objectModification": {"itemDelta": {
            "modificationType": "add", "path": "linkRef",
            "value": [{"oid": shadow_oid, "type": "ShadowType"}],
        }},
    }, accepted=(200, 204))
    current = _current_user(api, user_oid)
    references = current.get("linkRef", [])
    references = [references] if isinstance(references, dict) else references
    if not isinstance(references, list) or sum(
        1 for ref in references if isinstance(ref, dict) and ref.get("oid") == shadow_oid
    ) != 1:
        raise RuntimeError(f"Employee {employee_id} native link readback failed")


def _create_user_xml(employee: dict[str, Any], sandbox_password: str) -> bytes:
    name = employee["displayName"].split()
    ET.register_namespace("", COMMON)
    ET.register_namespace("ea", EA)
    root = ET.Element(f"{{{COMMON}}}user")
    for key, value in (
        ("name", employee["id"]), ("fullName", employee["displayName"]),
        ("givenName", name[0]), ("familyName", " ".join(name[1:])),
        ("emailAddress", employee["email"]),
    ):
        ET.SubElement(root, f"{{{COMMON}}}{key}").text = value
    credentials = ET.SubElement(root, f"{{{COMMON}}}credentials")
    password = ET.SubElement(credentials, f"{{{COMMON}}}password")
    value = ET.SubElement(password, f"{{{COMMON}}}value")
    ET.SubElement(value, f"{{{COMMON}}}clearValue").text = sandbox_password
    extension = ET.SubElement(root, f"{{{COMMON}}}extension")
    accounts = employee["accounts"]
    for key, text in (
        ("rocketchatUsername", accounts["rocketchat"]["username"]),
        ("planeUid", accounts["plane"]["id"]),
        ("planeEmail", accounts["plane"]["email"]),
    ):
        ET.SubElement(extension, f"{{{EA}}}{key}").text = text
    assignment = ET.SubElement(root, f"{{{COMMON}}}assignment")
    construction = ET.SubElement(assignment, f"{{{COMMON}}}construction")
    ET.SubElement(construction, f"{{{COMMON}}}resourceRef", {
        "oid": RESOURCE_OIDS["owncloud"], "type": "ResourceType",
    })
    ET.SubElement(construction, f"{{{COMMON}}}kind").text = "account"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _ensure_employee(api: MidPointRest, employee: dict[str, Any], password: str) -> dict[str, Any]:
    _, _, payload = api.json("POST", "ws/rest/users/search", {
        "query": {"filter": {"text": f'name = "{employee["id"]}"'}},
    }, accepted=(200,))
    matches = ((payload or {}).get("object") or {}).get("object")
    if not isinstance(matches, list):
        raise RuntimeError("midPoint employee lookup returned an unexpected shape")
    if len(matches) > 1:
        raise RuntimeError(f"Employee {employee['id']} already has duplicate central identities")
    if matches:
        current = matches[0]
        expected = (employee["displayName"], employee["email"])
        actual = (current.get("fullName"), current.get("emailAddress"))
        if actual != expected:
            raise RuntimeError(f"Existing MidPoint identity {employee['id']} differs from fresh company source")
        user_oid = current.get("oid")
        if not isinstance(user_oid, str) or not user_oid:
            raise RuntimeError(f"Existing MidPoint identity {employee['id']} has no OID")
    else:
        status, location, body = api.request(
            "POST", "ws/rest/users", payload=_create_user_xml(employee, password),
            content_type="application/xml", accepted=(201, 202, 303),
        )
        if status in (202, 303):
            if not location:
                raise RuntimeError(f"MidPoint creation for {employee['id']} returned no task location")
            task = api.task(location)
            result_ref = task.get("affectedObjects")
            del result_ref
            _, _, lookup = api.json("POST", "ws/rest/users/search", {
                "query": {"filter": {"text": f'name = "{employee["id"]}"'}},
            }, accepted=(200,))
            matches = ((lookup or {}).get("object") or {}).get("object")
            if not isinstance(matches, list) or len(matches) != 1:
                raise RuntimeError(f"MidPoint task did not create exactly one employee {employee['id']}")
            current = matches[0]
            user_oid = current.get("oid")
        else:
            if not location:
                raise RuntimeError(f"MidPoint creation for {employee['id']} returned no object location")
            user_oid = urlparse(location).path.rstrip("/").split("/")[-1]
            current = _current_user(api, user_oid)
        if not isinstance(user_oid, str) or not user_oid:
            raise RuntimeError(f"MidPoint creation for {employee['id']} returned no OID")
    current = _current_user(api, user_oid)
    if current.get("name") != employee["id"] or current.get("fullName") != employee["displayName"]:
        raise RuntimeError(f"MidPoint employee readback differs for {employee['id']}")
    return current


def _seed_account_shadows(api: MidPointRest, identities: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    shadows_by_resource = {
        "rocketchat": _get_shadows(api, RESOURCE_OIDS["rocketchat"]),
        "mail": _get_shadows(api, RESOURCE_OIDS["mail"]),
    }
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for employee in identities["employees"]:
        employee_id = employee["id"]
        accounts = employee["accounts"]
        rocket = _one_shadow(shadows_by_resource["rocketchat"], "_id",
                             accounts["rocketchat"]["id"], employee_id, "Rocket.Chat")
        mail = _one_shadow(shadows_by_resource["mail"], "email",
                           accounts["mail"]["address"], employee_id, "Mailu")
        result[employee_id] = {"rocketchat": rocket, "mail": mail}
    return result


def _owncloud_policy(source: Path, profile_path: Path, midpoint_password: str,
                     identities_path: Path) -> dict[str, Any]:
    source_root = str(source)
    sys.path.insert(0, source_root)
    from servers.company.owncloud import (
        OwnCloudClient,
        OwnCloudIdentity,
        OwnCloudShareLedger,
        _department_group_policy,
        ensure_company_staff_memberships,
        reconcile_access_policy,
        resolve_midpoint_owncloud_identities,
        resource_reference_from_webdav,
    )
    from servers.simulation.store import SQLiteStore

    company = _private_json(identities_path)
    settings = company["company"]["background"]["owncloud"]
    selected = settings.get("selected_access_policy")
    policy = settings.get("access_policy_candidates", {}).get(selected)
    if not isinstance(policy, dict) or policy.get("mode") != selected or policy.get("status") != "selected":
        raise RuntimeError("Fresh company OwnCloud policy is missing its selected approved mapping")
    admin_uid = settings["service_owner"]["owncloud_uid"]
    owncloud = OwnCloudClient("http://127.0.0.1:8092", admin_uid, _profile(profile_path)["sandbox_password"])
    employee_records = company.get("employees")
    if not isinstance(employee_records, list) or len(employee_records) != 17:
        raise RuntimeError("Fresh company profile must contain its 17 employee records")
    credentials = {"username": "administrator", "password": midpoint_password}
    linked = resolve_midpoint_owncloud_identities(MIDPOINT_URL, credentials, employee_records)
    expected_ids = {employee["id"] for employee in employee_records}
    if set(linked) != expected_ids:
        raise RuntimeError("MidPoint OwnCloud links do not cover the fresh employee roster")
    references = {}
    owner = OwnCloudIdentity(None, None, None, admin_uid)
    expanded = _department_group_policy(policy, settings["company_staff_group_id"])
    reference_ids = {share["file_reference_id"] for group in expanded["role_groups"]
                     for share in group["shares"]}
    for reference_id in sorted(reference_ids):
        source_entry = settings["candidate_resources"].get(reference_id)
        if not isinstance(source_entry, dict) or not isinstance(source_entry.get("path"), str):
            raise RuntimeError(f"Approved OwnCloud policy refers to unknown native file {reference_id}")
        reference, _ = resource_reference_from_webdav(
            owncloud, reference_id=reference_id, owner=owner, path=source_entry["path"],
        )
        references[reference_id] = reference
    staff = ensure_company_staff_memberships(
        owncloud, settings["company_staff_group_id"], linked,
    )
    store_path = ENTERPRISE_ROOT / "state" / "state.sqlite"
    store = SQLiteStore(store_path)
    outcomes = reconcile_access_policy(
        {admin_uid: owncloud}, policy=policy, references=references, identities=linked,
        staff_group_id=settings["company_staff_group_id"], group_admin_client=owncloud,
        ledger=OwnCloudShareLedger(store), company_id=company["company"]["id"],
    )
    company["company"]["background"]["owncloud"]["bootstrap_access_policy"] = {
        "mode": selected, "staff_group": staff, "reconciled_share_count": len(outcomes),
    }
    _private_write(identities_path, company)
    return {"employees_linked": len(linked), "staff_group": staff, "shares_reconciled": len(outcomes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/agentic-world/source"))
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--identities", type=Path, default=IDENTITIES_PATH)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("must run as root")
    source = args.source.resolve(strict=True)
    profile = _profile(args.profile)
    if not all((STATE_DIR / name).is_file() for name in ("rocketchat-seed-restored", "plane-seed-restored")):
        raise RuntimeError("Fresh Rocket.Chat and Plane seed restores must finish before identity provisioning")
    company = _private_json(args.identities)
    employees = company.get("employees")
    if not isinstance(employees, list) or len(employees) != 17:
        raise RuntimeError("The source company identity map must contain the approved 17-person roster")
    midpoint_password_path = SECRETS_DIR / "midpoint-admin"
    if midpoint_password_path.stat().st_mode & 0o077:
        raise PermissionError("MidPoint administrator password file must be owner-only")
    midpoint_password = midpoint_password_path.read_text(encoding="utf-8").strip()
    _wait_midpoint()
    _wait_http("http://127.0.0.1:3000/api/info", "Rocket.Chat")
    _wait_http("http://127.0.0.1:8092/status.php", "ownCloud")
    rocket_user_id, rocket_token, rocket_api = _rocket_admin(
        ROCKETCHAT_BASE, profile["sandbox_password"],
    )
    chat_credentials = _provision_chat_accounts(
        rocket_api, ROCKETCHAT_BASE, company, profile,
    )
    _private_write(ENTERPRISE_ROOT / "secrets" / "employee-chat-credentials.json", chat_credentials)
    _private_write(args.identities, company)
    midpoint_config = COMPOSE_DIR / "tac-midpoint.json"
    _build_and_install_connectors(source, midpoint_config)
    _wait_midpoint()
    connector_secrets = _write_connector_secrets(
        profile | {"company": company["company"]}, SECRETS_DIR, rocket_user_id, rocket_token,
    )
    api = MidPointRest(MIDPOINT_URL, "administrator", midpoint_password)
    _import_resources(api, source, connector_secrets, midpoint_password_path)
    for resource_oid in RESOURCE_OIDS.values():
        api.import_resource(resource_oid)
    existing = _seed_account_shadows(api, company)
    created_users: dict[str, dict[str, Any]] = {}
    for employee in employees:
        current = _ensure_employee(api, employee, profile["sandbox_password"])
        user_oid = current["oid"]
        created_users[employee["id"]] = current
        _add_link(api, user_oid, existing[employee["id"]]["rocketchat"], employee["id"])
        _add_link(api, user_oid, existing[employee["id"]]["mail"], employee["id"])
    for employee in employees:
        user = _current_user(api, created_users[employee["id"]]["oid"])
        refs = user.get("linkRef", [])
        refs = [refs] if isinstance(refs, dict) else refs
        owned = [ref for ref in refs if isinstance(ref, dict)
                 and ref.get("type") == "ShadowType"]
        resource_by_shadow = {}
        for ref in owned:
            shadow_oid = ref.get("oid")
            if not isinstance(shadow_oid, str) or not shadow_oid:
                continue
            _, _, payload = api.json("GET", f"ws/rest/shadows/{shadow_oid}", accepted=(200,))
            shadow = payload.get("shadow") if isinstance(payload, dict) else None
            resource = shadow.get("resourceRef") if isinstance(shadow, dict) else None
            if isinstance(resource, dict) and isinstance(resource.get("oid"), str):
                resource_by_shadow[shadow_oid] = resource["oid"]
        linked_resource_oids = set(resource_by_shadow.values())
        if not set(RESOURCE_OIDS.values()).issubset(linked_resource_oids):
            raise RuntimeError(f"Employee {employee['id']} is missing one or more native resource links")
        owncloud_shadow_oids = [shadow_oid for shadow_oid, resource_oid in resource_by_shadow.items()
                                if resource_oid == RESOURCE_OIDS["owncloud"]
                                and _owner_oid(api, shadow_oid) == user["oid"]]
        if len(owncloud_shadow_oids) != 1:
            raise RuntimeError(f"Employee {employee['id']} has {len(owncloud_shadow_oids)} linked OwnCloud projections")
        owncloud_shadow = owncloud_shadow_oids[0]
        employee["accounts"]["owncloud"]["midpoint_shadow_oid"] = owncloud_shadow
    _private_write(args.identities, company)
    policy_result = _owncloud_policy(source, args.profile, midpoint_password, args.identities)
    print(json.dumps({"employees_provisioned": len(employees), "resources_imported": len(RESOURCE_OIDS),
                      "owncloud_policy": policy_result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
