#!/usr/bin/env python3
"""Link explicitly inventoried existing accounts using midPoint's native model API.

This does not assign resources, create target accounts, or change credentials.
Run discovery again after a company reset; stale/ambiguous IDs fail explicitly.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import stat
from urllib.error import HTTPError
from urllib.request import Request, urlopen

RESOURCES = {
    "rocketchat": ("57a19684-cfe8-4f21-b7b4-0fb02b5ec001", "_id", "id"),
    "mail": ("57a19684-cfe8-4f21-b7b4-0fb02b5ec002", "email", "address"),
    "owncloud": ("57a19684-cfe8-4f21-b7b4-0fb02b5ec003", "_id", "id"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--midpoint", default="http://127.0.0.1:18120/midpoint")
    parser.add_argument("--identities", type=Path, default=Path(__file__).parents[1] / "company-identities.json")
    parser.add_argument("--password-file", type=Path,
                        default=Path.home() / ".config/theagentcompany/midpoint-4.11-poc-admin-password")
    parser.add_argument("--service-credentials", type=Path,
                        help="Read the existing private company service account from Mailu's managed credential JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True,
                        help="New private file for pre-change resource, user-link, and shadow-reference metadata")
    args = parser.parse_args()
    if args.output.exists() or args.backup.exists():
        raise FileExistsError("Adoption output and backup paths must be new files")
    if args.service_credentials:
        if stat.S_IMODE(args.service_credentials.stat().st_mode) & 0o077:
            raise PermissionError("MidPoint service credential file must be private to its owner")
        credential_data = json.loads(args.service_credentials.read_text(encoding="utf-8"))
        service_account = credential_data.get("accounts", {}).get("theagentcompany", {}).get("mail")
        if not isinstance(service_account, dict) or not service_account.get("password"):
            raise ValueError("Existing company MidPoint service credential is unavailable")
        username, password = "theagentcompany", service_account["password"]
    else:
        if stat.S_IMODE(args.password_file.stat().st_mode) & 0o077:
            raise PermissionError("MidPoint password file must be private to its owner")
        username, password = "theagentcompany", args.password_file.read_text(encoding="utf-8").strip()
    authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def call(path, value=None, method=None, xml=False):
        data = value.encode() if xml else json.dumps(value).encode() if value is not None else None
        request = Request(args.midpoint.rstrip("/") + "/ws/rest/" + path, data=data, method=method,
                          headers={"Accept": "application/json", "Authorization": authorization,
                                   "Content-Type": "application/xml" if xml else "application/json"})
        try:
            with urlopen(request, timeout=60) as response:
                if response.status not in (200, 201, 204):
                    raise RuntimeError(f"midPoint returned non-success outcome HTTP {response.status}")
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise RuntimeError(f"midPoint {method or 'GET'} {path.split('?')[0]} returned HTTP {error.code}") from error

    users = call("users")["object"]["object"]
    if not isinstance(users, list):
        raise RuntimeError("MIDPOINT_USER_LIST_SHAPE_INVALID")
    shadows = {}
    for service, (resource, _, _) in RESOURCES.items():
        query_text = f'resourceRef matches (oid = "{resource}") and objectClass = "ri:User"'
        shadows[service] = call("shadows/search", {"query": {"filter": {"text": query_text}}})["object"]["object"]
        if not isinstance(shadows[service], list):
            raise RuntimeError(f"MIDPOINT_{service.upper()}_SHADOW_LIST_SHAPE_INVALID")

    profile = json.loads(args.identities.read_text(encoding="utf-8"))
    employees = profile.get("employees")
    if not isinstance(employees, list) or not employees:
        raise ValueError("A configured company employee roster is required")
    outcomes = []
    prechange_users = []
    prechange_shadows = []
    for employee in employees:
        matches = [user for user in users if user.get("name") == employee["id"]]
        if len(matches) != 1:
            raise RuntimeError(f"Employee {employee['id']} has no unique central record")
        user = matches[0]
        links = []
        for service, (_, attribute, key) in RESOURCES.items():
            expected = employee["accounts"][service][key]
            matches = [shadow for shadow in shadows[service] if shadow.get("attributes", {}).get(attribute) == expected]
            if len(matches) != 1:
                raise RuntimeError(f"Employee {employee['id']} has no unique {service} immutable ID match")
            shadow = matches[0]
            owner = call(f"shadows/{shadow['oid']}/owner")
            owner_user = {}
            if owner:
                owner_user = owner.get("user", owner.get("object", {}))
                if owner_user.get("oid") != user["oid"]:
                    raise RuntimeError(f"Employee {employee['id']} {service} account belongs to another central record")
            links.append({"service": service, "oid": shadow["oid"], "type": "ShadowType"})
            prechange_shadows.append({"service": service, "account_id": expected,
                                      "shadow_oid": shadow["oid"],
                                      "owner_user_oid": owner_user.get("oid") if owner else None})
        references = user.get("linkRef", [])
        references = [references] if isinstance(references, dict) else references
        if not isinstance(references, list) or any(not isinstance(item, dict) or not isinstance(item.get("oid"), str) for item in references):
            raise RuntimeError("MIDPOINT_USER_LINKS_SHAPE_INVALID")
        existing = {link["oid"] for link in references}
        missing = [link for link in links if link["oid"] not in existing]
        prechange_users.append({"employee_id": employee["id"], "user_oid": user["oid"],
                                "link_refs": references})
        outcomes.append({"employee_id": employee["id"], "midpoint_oid": user["oid"],
                         "links_added": len(missing), "link_refs": links})

    resource_records = []
    for service, (resource_oid, _, _) in RESOURCES.items():
        resource = call(f"resources/{resource_oid}")["resource"]
        resource_records.append({"service": service, "oid": resource_oid,
                                 "name": resource.get("name"), "version": resource.get("version")})
    backup = {"schema_version": 1, "captured_before_adoption": True,
              "resources": resource_records, "users": prechange_users,
              "shadows": prechange_shadows,
              "profile_shadow_oids": {
                  employee["id"]: {
                      service: employee.get("accounts", {}).get(service, {}).get("midpoint_shadow_oid")
                      for service in RESOURCES
                  }
                  for employee in employees
              }}
    _write_private_new(args.backup, backup)

    for employee, outcome in zip(employees, outcomes, strict=True):
        existing = {
            item["oid"] for item in next(row for row in prechange_users if row["employee_id"] == employee["id"])["link_refs"]
        }
        missing = [{key: link[key] for key in ("oid", "type")} for link in outcome["link_refs"]
                   if link["oid"] not in existing]
        if missing:
            call(f"users/{outcome['midpoint_oid']}", {"objectModification": {"itemDelta": {
                "modificationType": "add", "path": "linkRef", "value": missing}}}, method="PATCH")
    for employee, outcome in zip(employees, outcomes, strict=True):
        current = call(f"users/{outcome['midpoint_oid']}")["user"]
        references = current.get("linkRef", [])
        references = [references] if isinstance(references, dict) else references
        if not isinstance(references, list):
            raise RuntimeError("MIDPOINT_USER_LINK_READBACK_SHAPE_INVALID")
        actual = {link.get("oid") for link in references if isinstance(link, dict)}
        if not all(link["oid"] in actual for link in outcome["link_refs"]):
            raise RuntimeError(f"Employee {employee['id']} native link readback failed")
        employee["accounts"]["owncloud"]["midpoint_shadow_oid"] = next(
            link["oid"] for link in outcome["link_refs"] if link["service"] == "owncloud"
        )

    for outcome in outcomes:
        for link in outcome["link_refs"]:
            owner = call(f"shadows/{link['oid']}/owner")
            if not owner:
                raise RuntimeError("MIDPOINT_NATIVE_SHADOW_OWNER_READBACK_FAILED")
            owner_user = owner.get("user", owner.get("object", {}))
            if owner_user.get("oid") != outcome["midpoint_oid"]:
                raise RuntimeError("MIDPOINT_NATIVE_SHADOW_OWNER_READBACK_MISMATCH")

    _write_profile(args.identities, profile)
    _write_private_new(args.output, {"schema_version": 1, "employees": outcomes})
    print(f"Verified {len(outcomes)} employees; added {sum(row['links_added'] for row in outcomes)} native links")


def _write_private_new(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _write_profile(path: Path, profile: dict) -> None:
    current_stat = path.stat()
    mode = stat.S_IMODE(current_stat.st_mode)
    temporary = path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(profile, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, current_stat.st_uid, current_stat.st_gid)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
