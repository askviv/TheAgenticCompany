from __future__ import annotations

import importlib.util
from pathlib import Path

from servers.company.owncloud import OwnCloudIdentity


ROOT = Path(__file__).parents[3]
ENTRYPOINT = ROOT / "deploy/single-host/owncloud/reconcile-access-policy.py"
SPEC = importlib.util.spec_from_file_location("owncloud_policy_entrypoint", ENTRYPOINT)
ENTRYPOINT_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ENTRYPOINT_MODULE)


class NativeMetadataClient:
    def __init__(self):
        self.calls = []

    def groups(self):
        self.calls.append(("groups",))
        return {"admin", "company-staff"}

    def _ocs(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        assert path == "cloud/users"
        return {"users": ["employee-a", "employee-b", "theagentcompany"]}

    def user_groups(self, uid):
        self.calls.append(("user_groups", uid))
        return {"employee-a": {"company-staff"}, "employee-b": set(),
                "theagentcompany": {"admin"}}[uid]

    def list_shares(self):
        self.calls.append(("shares",))
        return [{"id": "851", "share_type": "3", "file_source": "851",
                 "uid_owner": "theagentcompany", "permissions": 1,
                 "token": "private-public-link-token", "url": "https://secret.example"}]


def test_entrypoint_resolves_the_selected_access_policy_from_company_profile():
    profile, settings, policy = ENTRYPOINT_MODULE._read_profile(
        ROOT / "deploy/single-host/company-identities.json"
    )

    assert profile["company"]["id"] == "company"
    assert settings["selected_access_policy"] == "department_based"
    assert policy["mode"] == "department_based"
    assert policy["status"] == "selected"
    assert len(policy["department_groups"]) == 6


def test_private_native_backup_captures_all_current_groups_and_all_users_memberships():
    client = NativeMetadataClient()
    identities = {
        "employee-a": OwnCloudIdentity("employee-a", "oid-a", "shadow-a", "employee-a"),
    }

    backup = ENTRYPOINT_MODULE._metadata_snapshot(
        client, identities, {"company-staff", "department-finance"}
    )

    assert backup["existing_groups"] == ["admin", "company-staff"]
    assert backup["target_group_ids_before"] == ["company-staff"]
    assert backup["group_memberships"] == {
        "employee-a": ["company-staff"],
        "employee-b": [],
        "theagentcompany": ["admin"],
    }
    assert backup["shares"] == [{"id": "851", "share_type": "3", "file_source": "851",
                                  "uid_owner": "theagentcompany", "permissions": 1}]
    assert all("token" not in share and "url" not in share for share in backup["shares"])
    assert ("user_groups", "employee-b") in client.calls
