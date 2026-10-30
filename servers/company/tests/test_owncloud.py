"""OwnCloud adapter contracts with native HTTP boundaries replaced."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from servers.company.owncloud import (
    OwnCloudClient,
    OwnCloudError,
    OwnCloudFileReference,
    OwnCloudIdentity,
    OwnCloudShareLedger,
    ensure_company_staff_memberships,
    reconcile_access_policy,
    resolve_midpoint_owncloud_identities,
)


class Response:
    def __init__(self, *, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("synthetic HTTP error")


class MidpointSession:
    def __init__(self, *, linked_user="central-1"):
        self.linked_user = linked_user

    def get(self, url, **kwargs):
        if url.endswith("/ws/rest/users"):
            return Response(body={"object": {"object": [{
                "oid": "central-1", "name": "sarah-johnson",
                "linkRef": [{"oid": "shadow-owncloud-1"}],
            }]}})
        if url.endswith("/ws/rest/shadows/shadow-owncloud-1"):
            return Response(body={"shadow": {
                "oid": "shadow-owncloud-1",
                "resourceRef": {"oid": "57a19684-cfe8-4f21-b7b4-0fb02b5ec003"},
                "attributes": {"_id": "sarah_johnson"},
            }})
        if url.endswith("/ws/rest/shadows/shadow-owncloud-1/owner"):
            return Response(body={"user": {"oid": self.linked_user}})
        raise AssertionError(url)

class WebDAVSession:
    def __init__(self, *, href="/remote.php/dav/files/sarah_johnson/Documents/statement%20final.pdf",
                 folder=False):
        self.href = href
        self.folder = folder
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PROPFIND":
            xml = f'''<?xml version="1.0"?>
              <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
                <d:response><d:href>{self.href}</d:href><d:propstat><d:prop>
                  <oc:fileid>82415</oc:fileid><d:getetag>"v7-55a"</d:getetag>
                  {'' if self.folder else '<d:getcontentlength>19</d:getcontentlength>'}
                  <d:getlastmodified>Fri, 30 Oct 2026 12:00:00 GMT</d:getlastmodified>
                  <d:resourcetype>{'<d:collection />' if self.folder else ''}</d:resourcetype>
                </d:prop></d:propstat></d:response>
              </d:multistatus>'''.encode()
            return Response(status=207, content=xml)
        raise AssertionError(method)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert not self.folder
        return Response(status=200, content=b"source statement v7")


class NativeOCSSession:
    """Captured OwnCloud 10.16 JSON response shapes, with no live requests."""
    share = {
        "id": 1, "share_type": 3, "file_source": 851, "item_source": 851,
        "item_type": "file", "uid_owner": "theagentcompany", "share_with": None,
        "permissions": 1, "name": None,
    }

    def request(self, method, url, **kwargs):
        if url.endswith("/cloud/groups"):
            data = {"groups": ["admin"]}
        elif url.endswith("/cloud/groups/admin"):
            data = {"users": ["theagentcompany"]}
        elif url.endswith("/cloud/users/sarah_johnson/groups"):
            data = {"groups": []}
        elif url.endswith("/apps/files_sharing/api/v1/shares"):
            data = [self.share]
        elif url.endswith("/apps/files_sharing/api/v1/shares/1"):
            data = [self.share]
        else:
            raise AssertionError((method, url))
        return Response(body={"ocs": {"meta": {"status": "ok", "statuscode": 100},
                                        "data": data}})


def test_native_ocs_json_group_and_share_shapes_are_parsed_without_empty_fallbacks():
    client = OwnCloudClient("https://owncloud.example.test", "theagentcompany", "private",
                            session=NativeOCSSession())
    assert client.groups() == {"admin"}
    assert client.group_members("admin") == {"theagentcompany"}
    assert client.user_groups("sarah_johnson") == set()
    rows = client.list_shares()
    assert len(rows) == 1
    assert rows[0]["file_source"] == 851
    assert client.get_share("1")["item_type"] == "file"


def test_unexpected_native_ocs_list_shape_fails_visibly():
    class InvalidSession:
        def request(self, *_args, **_kwargs):
            return Response(body={"ocs": {"meta": {"status": "ok", "statuscode": 100},
                                          "data": {"shares": []}}})

    client = OwnCloudClient("https://owncloud.example.test", "theagentcompany", "private",
                            session=InvalidSession())
    with pytest.raises(OwnCloudError, match="OWNCLOUD_SHARE_LIST_SHAPE_INVALID"):
        client.list_shares()


def test_midpoint_resolver_requires_exact_linked_shadow_uid_and_owner():
    profile_employee = {
        "id": "sarah-johnson",
        "accounts": {"owncloud": {
            "id": "sarah_johnson", "midpoint_shadow_oid": "shadow-owncloud-1",
        }},
    }
    identity = resolve_midpoint_owncloud_identities(
        "https://midpoint.example.test",
        {"username": "directory", "password": "private"},
        [profile_employee],
        session=MidpointSession(),
    )["sarah-johnson"]
    assert identity == OwnCloudIdentity(
        "sarah-johnson", "central-1", "shadow-owncloud-1", "sarah_johnson"
    )
    with pytest.raises(OwnCloudError, match="SHADOW_OWNER_MISMATCH"):
        resolve_midpoint_owncloud_identities(
            "https://midpoint.example.test",
            {"username": "directory", "password": "private"},
            [profile_employee],
            session=MidpointSession(linked_user="different-user"),
        )


def test_webdav_reference_uses_native_href_fileid_etag_and_download_hash():
    session = WebDAVSession()
    client = OwnCloudClient(
        "https://owncloud.example.test", "sarah_johnson", "private", session=session
    )
    identity = OwnCloudIdentity("sarah-johnson", "central-1", "shadow-1", "sarah_johnson")
    from servers.company.owncloud import file_reference_from_webdav

    reference, content = file_reference_from_webdav(
        client, reference_id="statement-2026", owner=identity,
        path="Documents/statement final.pdf",
    )
    assert content == b"source statement v7"
    assert reference.file_id == "82415"
    assert reference.etag == '"v7-55a"'
    assert reference.sha256 == hashlib.sha256(content).hexdigest()
    assert reference.href == (
        "https://owncloud.example.test/remote.php/dav/files/"
        "sarah_johnson/Documents/statement%20final.pdf"
    )
    assert session.calls[0][2]["headers"]["Depth"] == "0"
    assert session.calls[0][2]["allow_redirects"] is False
    assert session.calls[1][2]["headers"]["If-Match"] == '"v7-55a"'
    with pytest.raises(PermissionError, match="file-owner account"):
        OwnCloudClient("https://owncloud.example.test", "admin", "private", session=session).file_metadata(
            "sarah_johnson", "Documents/statement final.pdf"
        )


class MembershipClient:
    def __init__(self):
        self.available_groups = {"admin"}
        self.members = {"admin": {"theagentcompany"}}
        self.added = []

    def ensure_group(self, group_id):
        created = group_id not in self.available_groups
        self.available_groups.add(group_id)
        self.members.setdefault(group_id, set())
        return created

    def ensure_membership(self, uid, group_id):
        if uid in self.members.setdefault(group_id, set()):
            return False
        self.members[group_id].add(uid)
        self.added.append((uid, group_id))
        return True

    def group_members(self, group_id):
        return set(self.members[group_id])


def test_staff_group_adds_exact_midpoint_users_without_changing_existing_admin_membership():
    client = MembershipClient()
    identities = {
        name: OwnCloudIdentity(name, f"midpoint-{name}", f"shadow-{name}", uid)
        for name, uid in (("sarah-johnson", "sarah_johnson"), ("li-ming", "li_ming"))
    }
    first = ensure_company_staff_memberships(client, "company-staff", identities)
    second = ensure_company_staff_memberships(client, "company-staff", identities)
    assert first == {"group_id": "company-staff", "created": True,
                     "members_added": ["li-ming", "sarah-johnson"], "member_count": 2}
    assert second["created"] is False
    assert second["members_added"] == []
    assert client.members["admin"] == {"theagentcompany"}
    assert len(client.added) == 2


class PolicyClient(MembershipClient):
    def __init__(self, reference):
        super().__init__()
        self.reference = reference
        self.username = reference.owner_uid
        self.shares = []
        self.next_id = 1
        self.updates = []

    def resource_metadata(self, uid, path):
        assert (uid, path) == (self.reference.owner_uid, self.reference.path)
        return {"href": self.reference.href, "file_id": self.reference.file_id,
                "etag": self.reference.etag, "sha256": self.reference.sha256,
                "size": self.reference.size, "is_collection": self.reference.is_collection}, \
            None if self.reference.is_collection else b"x" * self.reference.size

    def list_shares(self):
        return list(self.shares)

    def create_group_share(self, path, group_id, permissions):
        share = {"id": str(self.next_id), "share_type": "1", "share_with": group_id,
                 "file_source": self.reference.file_id, "uid_owner": self.reference.owner_uid,
                 "item_type": "folder" if self.reference.is_collection else "file",
                 "permissions": permissions}
        self.next_id += 1
        self.shares.append(share)
        return {"id": share["id"]}

    def get_share(self, share_id):
        return next(row for row in self.shares if row["id"] == share_id)

    def update_share(self, share_id, **fields):
        row = self.get_share(share_id)
        row.update(fields)
        self.updates.append((share_id, fields))
        return row


def _reference():
    return OwnCloudFileReference(
        reference_id="policy-doc", owner_employee_id="sarah-johnson",
        owner_midpoint_oid="midpoint-sarah", owner_shadow_oid="shadow-sarah",
        owner_uid="sarah_johnson", path="Documents/source.pdf",
        href="https://owncloud.example.test/remote.php/dav/files/sarah_johnson/Documents/source.pdf",
        file_id="82415", etag='"v7"', sha256="a" * 64, size=1,
    )


class MemoryLedger:
    def __init__(self):
        self.records = {}

    def get(self, company_id, managed_key):
        return self.records.get((company_id, managed_key))

    def put(self, company_id, managed_key, record):
        current = self.get(company_id, managed_key)
        if current is not None and current != record:
            raise OwnCloudError("ledger identity conflict")
        self.records[(company_id, managed_key)] = dict(record)


class SqliteTestStore:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

    @contextmanager
    def transaction(self):
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


def test_all_staff_policy_creates_only_explicit_source_linked_native_group_share():
    reference = _reference()
    client = PolicyClient(reference)
    ledger = MemoryLedger()
    result = reconcile_access_policy(
        {reference.owner_uid: client},
        policy={"mode": "all_staff_shared", "group_id": "company-staff",
                "shares": [{"file_reference_id": reference.reference_id, "permissions": 1}]},
        references={reference.reference_id: reference}, identities={},
        staff_group_id="company-staff", ledger=ledger,
    )
    assert result[0]["status"] == "created"
    assert result[0]["permissions"] == 1
    assert client.shares[0]["share_with"] == "company-staff"
    assert client.shares[0]["file_source"] == "82415"


def test_role_policy_uses_explicit_employee_ids_and_updates_only_its_own_native_share():
    reference = _reference()
    client = PolicyClient(reference)
    identity = OwnCloudIdentity("sarah-johnson", "midpoint-sarah", "shadow-sarah", "sarah_johnson")
    ledger = MemoryLedger()
    policy = {"mode": "role_based", "role_groups": [{
        "group_id": "finance", "employee_ids": ["sarah-johnson"],
        "shares": [{"file_reference_id": reference.reference_id, "permissions": 1}],
    }]}
    first = reconcile_access_policy(
        {reference.owner_uid: client}, policy=policy,
        references={reference.reference_id: reference},
        identities={identity.employee_id: identity}, staff_group_id="company-staff",
        group_admin_client=client, ledger=ledger,
    )
    assert first[0]["status"] == "created"
    changed = {**policy, "role_groups": [{**policy["role_groups"][0],
                                            "shares": [{"file_reference_id": reference.reference_id,
                                                        "permissions": 3}]}]}
    updated = reconcile_access_policy(
        {reference.owner_uid: client}, policy=changed,
        references={reference.reference_id: reference},
        identities={identity.employee_id: identity}, staff_group_id="company-staff",
        group_admin_client=client, ledger=ledger,
    )
    assert updated[0]["status"] == "updated"
    assert client.shares[0]["permissions"] == 3
    assert client.shares[0]["share_with"] == "finance"


def test_changed_source_does_not_create_duplicate_for_existing_managed_share():
    reference = _reference()
    client = PolicyClient(reference)
    ledger = MemoryLedger()
    policy = {"mode": "all_staff_shared", "group_id": "company-staff",
              "shares": [{"file_reference_id": reference.reference_id, "permissions": 1}]}
    reconcile_access_policy(
        {reference.owner_uid: client}, policy=policy,
        references={reference.reference_id: reference}, identities={},
        staff_group_id="company-staff", ledger=ledger,
    )
    client.shares[0]["file_source"] = "replaced-file-id"
    with pytest.raises(OwnCloudError, match="OWNCLOUD_MANAGED_SHARE_SOURCE_CHANGED_PRESERVED"):
        reconcile_access_policy(
            {reference.owner_uid: client}, policy={**policy, "shares": [{
                "file_reference_id": reference.reference_id, "permissions": 3,
            }]},
            references={reference.reference_id: reference}, identities={},
            staff_group_id="company-staff", ledger=ledger,
        )
    assert len(client.shares) == 1
    assert client.updates == []


def test_policy_retry_ignores_normal_content_changes_but_rejects_replaced_native_resource():
    reference = _reference()

    class MutableResourcePolicyClient(PolicyClient):
        def __init__(self, ref):
            super().__init__(ref)
            self.native_metadata = {
                "href": ref.href, "file_id": ref.file_id, "etag": ref.etag,
                "sha256": ref.sha256, "size": ref.size,
                "is_collection": ref.is_collection,
            }

        def resource_metadata(self, uid, path):
            assert (uid, path) == (self.reference.owner_uid, self.reference.path)
            return dict(self.native_metadata), b"updated company-owned content"

    client = MutableResourcePolicyClient(reference)
    ledger = MemoryLedger()
    policy = {"mode": "all_staff_shared", "group_id": "company-staff",
              "shares": [{"file_reference_id": reference.reference_id, "permissions": 1}]}
    first = reconcile_access_policy(
        {reference.owner_uid: client}, policy=policy,
        references={reference.reference_id: reference}, identities={},
        staff_group_id="company-staff", ledger=ledger,
    )
    original_share = dict(client.shares[0])

    client.native_metadata.update({
        "etag": '"v8-after-finance-edit"',
        "sha256": "b" * 64,
        "size": 29,
    })
    retry = reconcile_access_policy(
        {reference.owner_uid: client}, policy=policy,
        references={reference.reference_id: reference}, identities={},
        staff_group_id="company-staff", ledger=ledger,
    )
    assert first[0]["status"] == "created"
    assert retry[0]["status"] == "unchanged"
    assert retry[0]["share_id"] == original_share["id"]
    assert client.shares == [original_share]

    client.native_metadata["file_id"] = "replacement-file-id"
    with pytest.raises(OwnCloudError, match="OWNCLOUD_FILE_REFERENCE_CHANGED"):
        reconcile_access_policy(
            {reference.owner_uid: client}, policy=policy,
            references={reference.reference_id: reference}, identities={},
            staff_group_id="company-staff", ledger=ledger,
        )
    assert client.shares == [original_share]
    assert client.updates == []


def test_sqlite_ledger_persists_and_rejects_retargeting_owned_share_identity():
    ledger = OwnCloudShareLedger(SqliteTestStore())
    record = {"group_id": "company-staff", "reference_id": "policy-doc",
              "owner_uid": "sarah_johnson", "file_id": "82415",
              "item_type": "file", "share_id": "31"}
    ledger.put("company", "stable-key", record)
    assert ledger.get("company", "stable-key") == {
        **record, "company_id": "company", "managed_key": "stable-key",
        "updated_at": ledger.get("company", "stable-key")["updated_at"],
    }
    with pytest.raises(OwnCloudError, match="SOURCE_CHANGED_PRESERVED"):
        ledger.put("company", "stable-key", {**record, "file_id": "replaced"})


def test_native_folder_snapshot_has_fileid_etag_but_no_fake_content_hash_or_get():
    session = WebDAVSession(
        href="/remote.php/dav/files/sarah_johnson/Documents/Financials/",
        folder=True,
    )
    client = OwnCloudClient("https://owncloud.example.test", "sarah_johnson", "private",
                            session=session)
    from servers.company.owncloud import resource_reference_from_webdav
    owner = OwnCloudIdentity("sarah-johnson", "central-1", "shadow-1", "sarah_johnson")
    reference, payload = resource_reference_from_webdav(
        client, reference_id="financials", owner=owner, path="Documents/Financials",
    )
    assert payload is None
    assert reference.file_id == "82415"
    assert reference.is_collection is True
    assert reference.sha256 is None
    assert reference.size is None
    assert [method for method, *_ in session.calls] == ["PROPFIND", "PROPFIND"]


def test_native_folder_reference_can_be_shared_without_fabricated_content_facts():
    reference = OwnCloudFileReference(
        reference_id="financials", owner_employee_id=None, owner_midpoint_oid=None,
        owner_shadow_oid=None, owner_uid="theagentcompany", path="Documents/Financials",
        href="https://owncloud.example.test/remote.php/dav/files/theagentcompany/Documents/Financials/",
        file_id="187", etag='"folder-v4"', sha256=None, size=None, is_collection=True,
    )
    client = PolicyClient(reference)
    result = reconcile_access_policy(
        {reference.owner_uid: client},
        policy={"mode": "all_staff_shared", "group_id": "company-staff",
                "shares": [{"file_reference_id": reference.reference_id, "permissions": 1}]},
        references={reference.reference_id: reference}, identities={},
        staff_group_id="company-staff", ledger=MemoryLedger(),
    )
    assert result[0]["status"] == "created"
    assert client.shares[0]["item_type"] == "folder"
    assert client.shares[0]["file_source"] == "187"
    assert "name" not in client.shares[0]


def test_no_policy_means_no_share_request_and_no_product_readiness_gate():
    assert reconcile_access_policy(
        {}, policy=None, references={}, identities={}, staff_group_id="company-staff"
    ) == []


def test_department_profile_reconciles_native_groups_and_distinct_folder_permissions_idempotently():
    profile = json.loads((Path(__file__).parents[3] / "deploy/single-host/company-identities.json").read_text())
    config = profile["company"]["background"]["owncloud"]
    identities = {
        employee["id"]: OwnCloudIdentity(
            employee["id"], f"midpoint-{employee['id']}", f"shadow-{employee['id']}",
            employee["accounts"]["owncloud"]["username"],
        ) for employee in profile["employees"]
    }
    references = {
        key: OwnCloudFileReference(
            reference_id=key, owner_employee_id=None, owner_midpoint_oid=None,
            owner_shadow_oid=None, owner_uid="theagentcompany", path=value["path"],
            href=f"https://owncloud.example.test/{value['path']}",
            file_id=value["file_id"], etag=value.get("etag", '"fixture-folder-version"'), sha256=value.get("sha256"),
            size=value.get("size"), is_collection=value.get("is_collection", False),
        ) for key, value in config["candidate_resources"].items() if "file_id" in value
    }

    class DepartmentClient(PolicyClient):
        def resource_metadata(self, uid, path):
            self.reference = next(ref for ref in references.values() if ref.path == path)
            return super().resource_metadata(uid, path)

    client = DepartmentClient(next(iter(references.values())))
    ledger = MemoryLedger()
    arguments = dict(
        policy=config["access_policy_candidates"]["department_based"],
        references=references, identities=identities, staff_group_id="company-staff",
        group_admin_client=client, ledger=ledger,
    )
    first = reconcile_access_policy({"theagentcompany": client}, **arguments)
    second = reconcile_access_policy({"theagentcompany": client}, **arguments)
    grants = {(row["share_with"], row["file_source"]): row["permissions"] for row in client.shares}
    assert len(first) == len(second) == len(grants) == 14
    assert all(row["status"] == "unchanged" for row in second)
    assert grants[("department-finance", "187")] == 15
    assert ("department-technical", "187") not in grants
    assert ("company-staff", "187") not in grants
    assert grants[("company-staff", "179")] == 1
    assert grants[("department-product-ux", "179")] == 15
    assert grants[("company-staff", "180")] == 1
    assert grants[("department-documentation", "180")] == 3
    assert client.members["department-finance"] == {identities["david-wong"].owncloud_uid}
    assert len(client.members["department-technical"]) == 10
    assert len(client.members["company-staff"]) == 17
    assert client.members["admin"] == {"theagentcompany"}
