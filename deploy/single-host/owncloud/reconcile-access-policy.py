#!/usr/bin/env python3
"""Apply the selected company OwnCloud policy through native OCS/WebDAV APIs.

Run on the deployment host as the deployment operator. Passwords are read from the
existing Docker secrets/environment in memory and are never printed or passed
on a process command line. A private native-state backup is written before
any membership or share change.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import requests


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from servers.company.owncloud import (  # noqa: E402
    OwnCloudClient,
    OwnCloudIdentity,
    OwnCloudShareLedger,
    ensure_company_staff_memberships,
    reconcile_access_policy,
    resolve_midpoint_owncloud_identities,
    resource_reference_from_webdav,
    _department_group_policy,
)
from servers.simulation.store import SQLiteStore  # noqa: E402


def _docker_secret(container: str, *command: str) -> str:
    completed = subprocess.run(
        ["sudo", "-n", "docker", "exec", container, *command],
        check=True, capture_output=True, text=True,
    )
    value = completed.stdout.rstrip("\r\n")
    if not value:
        raise RuntimeError("Configured private container credential is empty")
    return value


def _midpoint_credentials(path: Path) -> dict[str, str]:
    if path.stat().st_mode & 0o077:
        raise PermissionError("MidPoint service credentials must be private to their owner")
    payload = json.loads(path.read_text(encoding="utf-8"))
    account = payload.get("accounts", {}).get("theagentcompany", {}).get("mail")
    if not isinstance(account, dict) or not isinstance(account.get("password"), str) or not account["password"]:
        raise RuntimeError("The existing private company MidPoint service credential is unavailable")
    return {"username": "theagentcompany", "password": account["password"]}


def _private_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_profile(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    company = profile["company"]
    settings = company["background"]["owncloud"]
    selected = settings.get("selected_access_policy")
    if not isinstance(selected, str):
        raise ValueError("Company OwnCloud selected_access_policy must name a configured candidate")
    policy = settings["access_policy_candidates"].get(selected)
    if not isinstance(policy, dict) or policy.get("mode") != selected or policy.get("status") != "selected":
        raise ValueError("Selected company OwnCloud policy is not an approved configured candidate")
    return profile, settings, policy


def _metadata_snapshot(client: OwnCloudClient, identities: dict[str, Any], target_group_ids: set[str]) -> dict[str, Any]:
    existing_groups = client.groups()
    user_data = client._ocs("GET", "cloud/users")
    if not isinstance(user_data, dict) or not isinstance(user_data.get("users"), list):
        raise RuntimeError("Native OwnCloud user inventory has an unsupported response shape")
    user_ids = user_data["users"]
    if any(not isinstance(uid, str) or not uid for uid in user_ids) or len(set(user_ids)) != len(user_ids):
        raise RuntimeError("Native OwnCloud user inventory contains invalid or duplicate identities")
    share_fields = (
        "id", "share_type", "share_with", "file_source", "item_source", "item_type",
        "permissions", "path", "uid_owner", "stime", "expiration",
    )
    shares = client.list_shares()
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "employee_uids": {key: value.owncloud_uid for key, value in sorted(identities.items())},
        "existing_groups": sorted(existing_groups),
        "target_group_ids_before": sorted(target_group_ids & existing_groups),
        "group_memberships": {
            uid: sorted(client.user_groups(uid)) for uid in sorted(user_ids)
        },
        "shares": [
            {key: value for key, value in share.items() if key in share_fields}
            for share in shares
        ],
    }


def _verify_applied_policy(
    client: OwnCloudClient,
    policy: dict[str, Any],
    identities: dict[str, Any],
    references: dict[str, Any],
    staff_group_id: str,
    before: dict[str, Any],
    owner_uid: str,
) -> dict[str, Any]:
    expected_by_group = {
        group["group_id"]: set(group["employee_ids"])
        for group in policy["department_groups"]
    }
    expected_by_group[staff_group_id] = set(policy["company_staff"]["employee_ids"])
    for group_id, expected_employees in expected_by_group.items():
        expected_uids = {identities[employee_id].owncloud_uid for employee_id in expected_employees}
        actual_uids = client.group_members(group_id)
        if actual_uids != expected_uids:
            raise RuntimeError("Native OwnCloud department membership readback differs from the approved employee map")
    employee_uids = {identity.owncloud_uid for identity in identities.values()}
    for employee_id, identity in identities.items():
        actual_groups = client.user_groups(identity.owncloud_uid)
        expected_groups = {staff_group_id} | {
            group_id for group_id, employees in expected_by_group.items()
            if group_id != staff_group_id and employee_id in next(
                group["employee_ids"] for group in policy["department_groups"]
                if group["group_id"] == group_id
            )
        }
        if not expected_groups.issubset(actual_groups) or actual_groups & set(expected_by_group) != expected_groups:
            raise RuntimeError("Native OwnCloud cross-department membership readback does not match the approved map")

    shares = client.list_shares()
    expected = []
    expanded_policy = _department_group_policy(policy, staff_group_id)
    for group in expanded_policy["role_groups"]:
        group_id, entries = group["group_id"], group["shares"]
        for share in entries:
            reference = references[share["file_reference_id"]]
            rows = [row for row in shares if str(row.get("share_type")) == "1"
                    and row.get("share_with") == group_id
                    and str(row.get("file_source", row.get("item_source", ""))) == reference.file_id]
            matching = [row for row in rows if int(row.get("permissions", -1)) == share["permissions"]]
            if len(rows) != 1 or len(matching) != 1:
                raise RuntimeError("Native OwnCloud share readback differs from the approved scope")
            expected.append((group_id, reference.file_id, str(matching[0].get("id")), share["permissions"]))

    share_fields = ("id", "share_type", "share_with", "file_source", "item_source", "item_type",
                    "permissions", "path", "uid_owner", "stime", "expiration")
    public_link = lambda row: (str(row.get("share_type")) == "3"
                               and str(row.get("file_source", row.get("item_source", ""))) == "851"
                               and row.get("uid_owner") == owner_uid)
    before_public = [row for row in before["shares"] if public_link(row)]
    after_public = [{key: value for key, value in row.items() if key in share_fields}
                    for row in shares if public_link(row)]
    if len(before_public) != 1 or after_public != before_public:
        raise RuntimeError("The pre-existing public share did not remain unchanged")
    if not employee_uids.issubset(set(before["group_memberships"])):
        raise RuntimeError("Pre-change membership snapshot is missing a configured employee")
    return {"verified_group_count": len(expected_by_group), "verified_member_count": sum(map(len, expected_by_group.values())),
            "verified_share_count": len(expected), "public_share_id": before_public[0]["id"],
            "public_share_file_id": "851"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=ROOT / "deploy/single-host/company-identities.json")
    parser.add_argument("--midpoint-url", default="http://127.0.0.1:18121/midpoint/")
    parser.add_argument("--owncloud-url", default="http://127.0.0.1:8092")
    parser.add_argument("--midpoint-credentials", type=Path,
                        default=Path("/data/theagentcompany/mailu/service-credentials.json"))
    parser.add_argument("--ledger-db", type=Path, default=Path("/data/theagentcompany/enterprise/state/state.sqlite"))
    parser.add_argument("--backup", type=Path, required=True, help="New private file to store native group/share metadata before changes")
    parser.add_argument("--apply", action="store_true", required=True, help="Required acknowledgment to apply the selected profile policy")
    args = parser.parse_args()

    profile, settings, policy = _read_profile(args.profile)
    company_id = profile["company"]["id"]
    service_owner = settings["service_owner"]
    owner_uid = service_owner["owncloud_uid"]
    owncloud_password = _docker_secret("owncloud", "printenv", "OWNCLOUD_ADMIN_PASSWORD")
    midpoint = _midpoint_credentials(args.midpoint_credentials)
    employee_records = profile.get("employees")
    if not isinstance(employee_records, list) or not employee_records:
        raise ValueError("Company OwnCloud policy requires configured employee identities")
    identities = resolve_midpoint_owncloud_identities(
        args.midpoint_url, midpoint, employee_records,
    )
    owner = OwnCloudIdentity(None, None, None, owner_uid)
    owncloud_session = requests.Session()
    owncloud_session.headers["Host"] = "the-agent-company.com:8092"
    owner_client = OwnCloudClient(args.owncloud_url, owner_uid, owncloud_password, session=owncloud_session)
    client_by_owner = {owner_uid: owner_client}

    references = {}
    for reference_id, source in settings["candidate_resources"].items():
        reference, _content = resource_reference_from_webdav(
            owner_client, reference_id=reference_id, owner=owner, path=source["path"],
        )
        # File contents and ETags change through ordinary employee work. The
        # access policy is bound to the native resource identity, not its seed
        # contents; strict content snapshots belong to attachment workflows.
        for field in ("file_id", "is_collection"):
            expected = source.get(field)
            if expected is not None and getattr(reference, field) != expected:
                raise RuntimeError(f"Configured OwnCloud resource reference changed: {reference_id}:{field}")
        references[reference_id] = reference

    group_ids = {settings["company_staff_group_id"]}
    group_ids.update(group["group_id"] for group in policy["department_groups"])
    snapshot = _metadata_snapshot(owner_client, identities, group_ids)
    original_public = [row for row in snapshot["shares"]
                       if str(row.get("share_type")) == "3"
                       and str(row.get("file_source", row.get("item_source", ""))) == "851"
                       and row.get("uid_owner") == owner_uid]
    if len(original_public) != 1:
        raise RuntimeError("The expected pre-existing public file share was not uniquely identified")
    _private_write(args.backup, snapshot)

    store = SQLiteStore(args.ledger_db)
    staff = ensure_company_staff_memberships(
        owner_client, settings["company_staff_group_id"], identities,
    )
    results = reconcile_access_policy(
        client_by_owner, policy=policy, references=references, identities=identities,
        staff_group_id=settings["company_staff_group_id"], group_admin_client=owner_client,
        ledger=OwnCloudShareLedger(store), company_id=company_id,
    )
    readback = _verify_applied_policy(
        owner_client, policy, identities, references, settings["company_staff_group_id"], snapshot, owner_uid,
    )
    print(json.dumps({
        "company_id": company_id,
        "policy": settings["selected_access_policy"],
        "backup": args.backup.name,
        "staff_group": staff,
        "department_groups": len(policy["department_groups"]),
        "share_count": len(results),
        "readback": readback,
        "share_results": results,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
