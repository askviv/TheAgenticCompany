"""Native OwnCloud Classic group, share, and WebDAV integration.

MidPoint remains the source of employee identities.  This module only follows
the exact linked OwnCloud UID and uses OwnCloud's OCS and WebDAV APIs for native
group, share, and file metadata operations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests


OWNCLOUD_RESOURCE_OID = "57a19684-cfe8-4f21-b7b4-0fb02b5ec003"
_OCS = "ocs/v1.php"
_WEBDAV = "remote.php/dav/files"
_DAV = "DAV:"
_OC = "http://owncloud.org/ns"
_UID = re.compile(r"^[A-Za-z0-9._@-]+$")
PERMISSION_READ = 1
PERMISSION_UPDATE = 2
PERMISSION_CREATE = 4
PERMISSION_DELETE = 8
PERMISSION_SHARE = 16


class OwnCloudError(RuntimeError):
    """A native OCS/WebDAV operation failed with a safe status identifier."""


class ManagedShareLedger(Protocol):
    def get(self, company_id: str, managed_key: str) -> dict[str, Any] | None: ...
    def put(self, company_id: str, managed_key: str, record: dict[str, Any]) -> None: ...


class OwnCloudShareLedger:
    """Persist exact native group-share IDs in the coordinator's SQLite store."""

    def __init__(self, store: Any):
        self.store = store
        with store.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS owncloud_managed_shares (
                company_id TEXT NOT NULL,
                managed_key TEXT NOT NULL,
                group_id TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                owner_uid TEXT NOT NULL,
                file_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                share_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(company_id, managed_key)
            )""")

    def get(self, company_id: str, managed_key: str) -> dict[str, Any] | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM owncloud_managed_shares WHERE company_id=? AND managed_key=?",
                (company_id, managed_key),
            ).fetchone()
        return dict(row) if row else None

    def put(self, company_id: str, managed_key: str, record: dict[str, Any]) -> None:
        from datetime import datetime, timezone

        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM owncloud_managed_shares WHERE company_id=? AND managed_key=?",
                (company_id, managed_key),
            ).fetchone()
            identity = (record["group_id"], record["reference_id"], record["owner_uid"],
                        record["file_id"], record["item_type"])
            if current is not None:
                existing = (current["group_id"], current["reference_id"], current["owner_uid"],
                            current["file_id"], current["item_type"])
                if existing != identity:
                    raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_SOURCE_CHANGED_PRESERVED")
                if current["share_id"] != record["share_id"]:
                    raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_ID_CONFLICT")
            connection.execute(
                "INSERT INTO owncloud_managed_shares "
                "(company_id,managed_key,group_id,reference_id,owner_uid,file_id,item_type,share_id,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id,managed_key) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (company_id, managed_key, record["group_id"], record["reference_id"],
                 record["owner_uid"], record["file_id"], record["item_type"], record["share_id"],
                 datetime.now(timezone.utc).isoformat()),
            )


@dataclass(frozen=True)
class OwnCloudIdentity:
    employee_id: str | None
    midpoint_oid: str | None
    midpoint_shadow_oid: str | None
    owncloud_uid: str


@dataclass(frozen=True)
class OwnCloudFileReference:
    reference_id: str
    owner_employee_id: str | None
    owner_midpoint_oid: str | None
    owner_shadow_oid: str | None
    owner_uid: str
    path: str
    href: str
    file_id: str
    etag: str
    sha256: str | None
    size: int | None
    last_modified: str | None = None
    is_collection: bool = False

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_midpoint_owncloud_identities(
    midpoint_base_url: str,
    credentials: dict[str, str],
    employee_records: list[dict[str, Any]],
    *,
    session: requests.Session | Any = None,
    resource_oid: str = OWNCLOUD_RESOURCE_OID,
) -> dict[str, OwnCloudIdentity]:
    """Resolve exact central names through their linked native OwnCloud shadows.

    Profile UIDs and shadow OIDs are checked against MidPoint rather than used
    as independent identity sources. This function is read-only.
    """
    client = session or requests.Session()
    base = midpoint_base_url.rstrip("/") + "/"
    response = client.get(
        urljoin(base, "ws/rest/users"),
        auth=(credentials["username"], credentials["password"]),
        headers={"Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    users = ((payload.get("object") or {}).get("object")
             if isinstance(payload, dict) else None)
    if not isinstance(users, list):
        raise OwnCloudError("MIDPOINT_USER_LIST_SHAPE_INVALID")

    identities: dict[str, OwnCloudIdentity] = {}
    for record in employee_records:
        employee_id = _required_text(record.get("id"), "employee id")
        expected_account = record.get("accounts", {}).get("owncloud")
        if not isinstance(expected_account, dict):
            raise OwnCloudError("PROFILE_OWNCLOUD_IDENTITY_MISSING")
        expected_uid = _required_text(expected_account.get("id"), "profile ownCloud UID")
        expected_shadow_oid = _required_text(
            expected_account.get("midpoint_shadow_oid"), "profile ownCloud shadow OID"
        )
        matches = [user for user in users if isinstance(user, dict)
                   and user.get("name") == employee_id]
        if len(matches) != 1:
            raise OwnCloudError("MIDPOINT_EMPLOYEE_IDENTITY_NOT_UNIQUE")
        user = matches[0]
        midpoint_oid = _required_text(user.get("oid"), "midPoint user OID")
        links = user.get("linkRef", [])
        if isinstance(links, dict):
            links = [links]
        if not isinstance(links, list):
            raise OwnCloudError("MIDPOINT_USER_LINKS_SHAPE_INVALID")
        linked_oids = [link.get("oid") for link in links if isinstance(link, dict)]
        if linked_oids.count(expected_shadow_oid) != 1:
            raise OwnCloudError("MIDPOINT_OWNCLOUD_LINK_NOT_UNIQUE")
        shadow_response = client.get(
            urljoin(base, f"ws/rest/shadows/{quote(expected_shadow_oid, safe='')}"),
            auth=(credentials["username"], credentials["password"]),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        shadow_response.raise_for_status()
        shadow_payload = shadow_response.json()
        shadow = shadow_payload.get("shadow") if isinstance(shadow_payload, dict) else None
        if not isinstance(shadow, dict) or shadow.get("oid") != expected_shadow_oid:
            raise OwnCloudError("MIDPOINT_OWNCLOUD_SHADOW_SHAPE_INVALID")
        resource_ref = shadow.get("resourceRef")
        actual_resource_oid = resource_ref.get("oid") if isinstance(resource_ref, dict) else None
        attributes = shadow.get("attributes")
        if not isinstance(attributes, dict) or actual_resource_oid != resource_oid:
            raise OwnCloudError("MIDPOINT_OWNCLOUD_RESOURCE_MISMATCH")
        actual_uid = attributes.get("_id") or attributes.get("uid")
        if actual_uid != expected_uid or not isinstance(actual_uid, str) or not _UID.fullmatch(actual_uid):
            raise OwnCloudError("MIDPOINT_OWNCLOUD_UID_MISMATCH")
        owner_response = client.get(
            urljoin(base, f"ws/rest/shadows/{quote(expected_shadow_oid, safe='')}/owner"),
            auth=(credentials["username"], credentials["password"]),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        owner_response.raise_for_status()
        owner_payload = owner_response.json()
        owner = owner_payload.get("user", owner_payload.get("object", {}))
        if not isinstance(owner, dict) or owner.get("oid") != midpoint_oid:
            raise OwnCloudError("MIDPOINT_OWNCLOUD_SHADOW_OWNER_MISMATCH")
        identities[employee_id] = OwnCloudIdentity(
            employee_id=employee_id,
            midpoint_oid=midpoint_oid,
            midpoint_shadow_oid=expected_shadow_oid,
            owncloud_uid=actual_uid,
        )
    return identities


class OwnCloudClient:
    """Small API-native client for ownCloud Classic OCS and WebDAV."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: requests.Session | Any = None,
        timeout: float = 20,
        verify: bool | str = True,
    ):
        parts = urlsplit(base_url)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username:
            raise ValueError("OwnCloud base URL must be an absolute HTTP(S) URL without credentials")
        if not username or not password:
            raise ValueError("OwnCloud API credentials are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        self.timeout = timeout
        self.verify = verify

    def _url(self, relative: str) -> str:
        return urljoin(self.base_url, relative.lstrip("/"))

    def _ocs(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Any:
        response = self.session.request(
            method,
            self._url(f"{_OCS}/{path.lstrip('/')}"),
            params={"format": "json", **(params or {})},
            data=data,
            auth=(self.username, self.password),
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            timeout=self.timeout,
            verify=self.verify,
            allow_redirects=False,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise OwnCloudError(f"OWNCLOUD_OCS_HTTP_{response.status_code}")
        try:
            body = response.json()
            meta = body["ocs"]["meta"]
            status = int(meta["statuscode"])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise OwnCloudError("OWNCLOUD_OCS_RESPONSE_SHAPE_INVALID") from error
        if status in allow_status:
            return None
        if status != 100:
            raise OwnCloudError(f"OWNCLOUD_OCS_STATUS_{status}")
        if "data" not in body["ocs"]:
            raise OwnCloudError("OWNCLOUD_OCS_DATA_SHAPE_INVALID")
        return body["ocs"]["data"]

    def groups(self) -> set[str]:
        return _ocs_string_list(self._ocs("GET", "cloud/groups"), "groups")

    def group_members(self, group_id: str) -> set[str]:
        return _ocs_string_list(
            self._ocs("GET", f"cloud/groups/{quote(group_id, safe='')}"), "users"
        )

    def user_groups(self, uid: str) -> set[str]:
        return _ocs_string_list(
            self._ocs("GET", f"cloud/users/{quote(uid, safe='')}/groups"), "groups"
        )

    def ensure_group(self, group_id: str) -> bool:
        if group_id in self.groups():
            return False
        self._ocs("POST", "cloud/groups", data={"groupid": group_id})
        if group_id not in self.groups():
            raise OwnCloudError("OWNCLOUD_GROUP_CREATE_NOT_CONFIRMED")
        return True

    def ensure_membership(self, uid: str, group_id: str) -> bool:
        if group_id in self.user_groups(uid):
            return False
        self._ocs("POST", f"cloud/users/{quote(uid, safe='')}/groups", data={"groupid": group_id})
        if group_id not in self.user_groups(uid):
            raise OwnCloudError("OWNCLOUD_GROUP_MEMBERSHIP_NOT_CONFIRMED")
        return True

    def resource_metadata(self, uid: str, path: str) -> tuple[dict[str, Any], bytes | None]:
        if uid != self.username:
            raise PermissionError("WebDAV file snapshots must use the linked file-owner account")
        path = _relative_file_path(path)
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        url = self._url(f"{_WEBDAV}/{quote(uid, safe='')}/{encoded_path}")
        propfind_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            '<d:prop><oc:fileid/><d:getetag/><d:getcontentlength/>'
            '<d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>'
        )
        response = self.session.request(
            "PROPFIND", url, data=propfind_body,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            auth=(self.username, self.password), timeout=self.timeout,
            verify=self.verify, allow_redirects=False,
        )
        if response.status_code != 207:
            raise OwnCloudError(f"OWNCLOUD_WEBDAV_PROPFIND_HTTP_{response.status_code}")
        try:
            root = ET.fromstring(response.content)
            responses = root.findall(f"{{{_DAV}}}response")
            if len(responses) != 1:
                raise ValueError("response count")
            href = responses[0].findtext(f"{{{_DAV}}}href")
            prop = responses[0].find(f".//{{{_DAV}}}prop")
            file_id = prop.findtext(f"{{{_OC}}}fileid") if prop is not None else None
            etag = prop.findtext(f"{{{_DAV}}}getetag") if prop is not None else None
            size_text = prop.findtext(f"{{{_DAV}}}getcontentlength") if prop is not None else None
            modified = prop.findtext(f"{{{_DAV}}}getlastmodified") if prop is not None else None
            resource_type = prop.find(f"{{{_DAV}}}resourcetype") if prop is not None else None
        except (ET.ParseError, AttributeError, TypeError, ValueError) as error:
            raise OwnCloudError("OWNCLOUD_WEBDAV_PROPFIND_SHAPE_INVALID") from error
        if not href or not file_id or not etag or resource_type is None:
            raise OwnCloudError("OWNCLOUD_WEBDAV_RESOURCE_REFERENCE_INCOMPLETE")
        is_collection = resource_type.find(f"{{{_DAV}}}collection") is not None
        if not is_collection and not size_text:
            raise OwnCloudError("OWNCLOUD_WEBDAV_FILE_REFERENCE_INCOMPLETE")
        actual_url = urljoin(self.base_url, href)
        expected_parts = urlsplit(url)
        actual_parts = urlsplit(actual_url)
        if actual_parts.netloc.casefold() != urlsplit(self.base_url).netloc.casefold():
            raise OwnCloudError("OWNCLOUD_WEBDAV_HREF_HOST_MISMATCH")
        actual_path = unquote(actual_parts.path)
        expected_path = unquote(expected_parts.path)
        if is_collection:
            actual_path = actual_path.rstrip("/")
            expected_path = expected_path.rstrip("/")
        if actual_path != expected_path:
            raise OwnCloudError("OWNCLOUD_WEBDAV_HREF_PATH_MISMATCH")
        payload = None
        expected_size = None
        if not is_collection:
            downloaded = self.session.get(
                actual_url, auth=(self.username, self.password),
                headers={"If-Match": str(etag)}, timeout=self.timeout,
                verify=self.verify, allow_redirects=False,
            )
            if downloaded.status_code == 412:
                raise OwnCloudError("OWNCLOUD_WEBDAV_FILE_CHANGED_DURING_READ")
            if downloaded.status_code != 200:
                raise OwnCloudError(f"OWNCLOUD_WEBDAV_GET_HTTP_{downloaded.status_code}")
            payload = downloaded.content
            try:
                expected_size = int(size_text)
            except ValueError as error:
                raise OwnCloudError("OWNCLOUD_WEBDAV_FILE_SIZE_INVALID") from error
            if expected_size != len(payload):
                raise OwnCloudError("OWNCLOUD_WEBDAV_FILE_SIZE_MISMATCH")
        final_response = self.session.request(
            "PROPFIND", url, data=propfind_body,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            auth=(self.username, self.password), timeout=self.timeout,
            verify=self.verify, allow_redirects=False,
        )
        if final_response.status_code != 207:
            raise OwnCloudError(f"OWNCLOUD_WEBDAV_VERIFY_HTTP_{final_response.status_code}")
        try:
            final_root = ET.fromstring(final_response.content)
            final_prop = final_root.find(f".//{{{_DAV}}}prop")
            final_file_id = final_prop.findtext(f"{{{_OC}}}fileid") if final_prop is not None else None
            final_etag = final_prop.findtext(f"{{{_DAV}}}getetag") if final_prop is not None else None
            final_size = final_prop.findtext(f"{{{_DAV}}}getcontentlength") if final_prop is not None else None
            final_type = final_prop.find(f"{{{_DAV}}}resourcetype") if final_prop is not None else None
        except (ET.ParseError, AttributeError, TypeError, ValueError) as error:
            raise OwnCloudError("OWNCLOUD_WEBDAV_VERIFY_SHAPE_INVALID") from error
        final_is_collection = (final_type is not None
                               and final_type.find(f"{{{_DAV}}}collection") is not None)
        if (str(final_file_id) != str(file_id) or str(final_etag) != str(etag)
                or final_is_collection != is_collection
                or (not is_collection and str(final_size) != str(expected_size))):
            raise OwnCloudError("OWNCLOUD_WEBDAV_FILE_CHANGED_DURING_READ")
        metadata = {
            "href": actual_url,
            "file_id": str(file_id),
            "etag": str(etag),
            "size": expected_size,
            "last_modified": modified,
            "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
            "is_collection": is_collection,
        }
        return metadata, payload

    def file_metadata(self, uid: str, path: str) -> tuple[dict[str, Any], bytes]:
        metadata, payload = self.resource_metadata(uid, path)
        if metadata["is_collection"] or payload is None:
            raise OwnCloudError("OWNCLOUD_WEBDAV_REFERENCE_IS_DIRECTORY")
        return metadata, payload

    def list_shares(self) -> list[dict[str, Any]]:
        data = self._ocs("GET", "apps/files_sharing/api/v1/shares", params={"reshares": "true"})
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise OwnCloudError("OWNCLOUD_SHARE_LIST_SHAPE_INVALID")
        return data

    def create_group_share(
        self, path: str, group_id: str, permissions: int,
    ) -> dict[str, Any]:
        data = self._ocs(
            "POST", "apps/files_sharing/api/v1/shares",
            data={"path": _relative_file_path(path), "shareType": 1,
                  "shareWith": group_id, "permissions": permissions},
        )
        if not isinstance(data, dict):
            raise OwnCloudError("OWNCLOUD_SHARE_CREATE_RESPONSE_SHAPE_INVALID")
        return data

    def update_share(self, share_id: str, **fields: Any) -> None:
        if len(fields) != 1 or set(fields) != {"permissions"}:
            raise ValueError("OwnCloud group-share updates accept only permissions")
        self._ocs(
            "PUT", f"apps/files_sharing/api/v1/shares/{quote(share_id, safe='')}",
            data=fields,
        )

    def get_share(self, share_id: str) -> dict[str, Any]:
        data = self._ocs(
            "GET", f"apps/files_sharing/api/v1/shares/{quote(share_id, safe='')}"
        )
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise OwnCloudError("OWNCLOUD_SHARE_READBACK_SHAPE_INVALID")
        return data[0]


def ensure_company_staff_memberships(
    client: OwnCloudClient,
    group_id: str,
    identities: dict[str, OwnCloudIdentity],
) -> dict[str, Any]:
    """Add verified MidPoint-linked employees to the company group; never remove."""
    group_id = _group_id(group_id)
    if group_id == "admin":
        raise ValueError("company staff cannot use the OwnCloud administrator group")
    if not identities:
        raise ValueError("MidPoint-verified employee identities are required")
    created = client.ensure_group(group_id)
    added = []
    for employee_id, identity in sorted(identities.items()):
        if employee_id != identity.employee_id or not identity.midpoint_oid or not identity.midpoint_shadow_oid:
            raise ValueError("Company membership requires exact MidPoint-linked OwnCloud identity data")
        if client.ensure_membership(identity.owncloud_uid, group_id):
            added.append(employee_id)
    members = client.group_members(group_id)
    expected_uids = {identity.owncloud_uid for identity in identities.values()}
    if not expected_uids.issubset(members):
        raise OwnCloudError("OWNCLOUD_COMPANY_GROUP_READBACK_MISMATCH")
    return {"group_id": group_id, "created": created,
            "members_added": added, "member_count": len(members)}


def reconcile_access_policy(
    client_by_owner_uid: dict[str, OwnCloudClient],
    *,
    policy: dict[str, Any] | None,
    references: dict[str, OwnCloudFileReference],
    identities: dict[str, OwnCloudIdentity],
    staff_group_id: str,
    group_admin_client: OwnCloudClient | None = None,
    ledger: ManagedShareLedger | None = None,
    company_id: str = "company",
) -> list[dict[str, Any]]:
    """Apply explicit group-share policy entries using owned OCS shares only.

    ``None`` means no share-policy actions. It is not a runtime readiness gate.
    Existing unmanaged shares are reported and preserved. Managed share IDs
    live in the coordinator SQLite ledger because OwnCloud 10.16 does not keep
    a custom name on group shares.
    """
    if policy is None:
        return []
    if policy.get("mode") == "department_based":
        policy = _department_group_policy(policy, staff_group_id)
    mode = policy.get("mode")
    if mode == "all_staff_shared":
        group_id = _group_id(policy.get("group_id"))
        if group_id != staff_group_id:
            raise ValueError("all_staff_shared must target the configured company staff group")
        targets = [(group_id, share) for share in _share_specs(policy.get("shares"))]
    elif mode == "role_based":
        role_groups = policy.get("role_groups")
        if not isinstance(role_groups, list):
            raise ValueError("role_based policy requires explicit role_groups")
        if role_groups and group_admin_client is None:
            raise ValueError("role_based policy requires the configured OwnCloud group administrator client")
        targets = []
        known_employees = set(identities)
        for group in role_groups:
            if not isinstance(group, dict):
                raise ValueError("role_based group entries must be objects")
            group_id = _group_id(group.get("group_id"))
            employee_ids = group.get("employee_ids")
            if (not isinstance(employee_ids, list) or not employee_ids
                    or any(item not in known_employees for item in employee_ids)):
                raise ValueError("role_based groups must name known employee IDs explicitly")
            group_admin_client.ensure_group(group_id)
            for employee_id in employee_ids:
                group_admin_client.ensure_membership(identities[employee_id].owncloud_uid, group_id)
            targets.extend((group_id, share) for share in _share_specs(group.get("shares")))
    else:
        raise ValueError("OwnCloud access policy mode must be all_staff_shared or role_based")

    if targets and ledger is None:
        raise ValueError("OwnCloud group-share reconciliation requires its durable coordinator ledger")
    results: list[dict[str, Any]] = []
    policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    for group_id, share in targets:
        reference_id = share["file_reference_id"]
        reference = references.get(reference_id)
        if reference is None:
            raise ValueError(f"OwnCloud policy refers to unknown file reference {reference_id}")
        owner_client = client_by_owner_uid.get(reference.owner_uid)
        if owner_client is None:
            raise ValueError("OwnCloud file owner has no authenticated native API client")
        if owner_client.username != reference.owner_uid:
            raise PermissionError("OwnCloud policy metadata must use the exact file-owner account")
        current, _payload = owner_client.resource_metadata(reference.owner_uid, reference.path)
        pinned = reference.public_dict()
        # A resource's content is expected to change during normal company
        # work. Group-share identity follows the stable owner/path/file ID and
        # resource type; scenario attachment snapshots retain their separate
        # strict ETag/hash validation.
        for key in ("href", "file_id", "is_collection"):
            if current[key] != pinned[key]:
                raise OwnCloudError("OWNCLOUD_FILE_REFERENCE_CHANGED")
        shares = owner_client.list_shares()
        managed_key = hashlib.sha256(f"{group_id}:{reference_id}".encode()).hexdigest()[:20]
        target_rows = [item for item in shares
                       if str(item.get("share_type")) == "1"
                       and item.get("share_with") == group_id
                       and str(item.get("file_source", item.get("item_source", ""))) == reference.file_id]
        ledger_record = ledger.get(company_id, managed_key)
        item = None
        if ledger_record is not None:
            expected_type = "folder" if reference.is_collection else "file"
            if (ledger_record["group_id"] != group_id
                    or ledger_record["reference_id"] != reference_id
                    or ledger_record["owner_uid"] != reference.owner_uid
                    or ledger_record["file_id"] != reference.file_id
                    or ledger_record["item_type"] != expected_type):
                raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_SOURCE_CHANGED_PRESERVED")
            matching_ids = [row for row in shares if str(row.get("id")) == ledger_record["share_id"]]
            if len(matching_ids) != 1:
                raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_MISSING")
            item = matching_ids[0]
            _verify_share_identity(item, reference, group_id)
        unmanaged = [row for row in target_rows
                     if item is None or str(row.get("id")) != ledger_record["share_id"]]
        if item is None and unmanaged:
            results.append({"file_reference_id": reference_id, "group_id": group_id,
                            "status": "unmanaged_existing_preserved",
                            "share_ids": sorted(str(row.get("id")) for row in unmanaged)})
            continue
        desired_permissions = share["permissions"]
        if item is not None:
            share_id = str(item.get("id", ""))
            try:
                current_permissions = int(item.get("permissions", -1))
            except (TypeError, ValueError) as error:
                raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_IDENTITY_MISMATCH") from error
            updated = False
            if current_permissions != desired_permissions:
                owner_client.update_share(share_id, permissions=desired_permissions)
                updated = True
            readback = owner_client.get_share(share_id)
            _verify_share(readback, reference, group_id, desired_permissions)
            ledger.put(company_id, managed_key, {
                "group_id": group_id, "reference_id": reference_id,
                "owner_uid": reference.owner_uid, "file_id": reference.file_id,
                "item_type": "folder" if reference.is_collection else "file",
                "share_id": share_id,
            })
            results.append({"file_reference_id": reference_id, "group_id": group_id,
                            "share_id": share_id, "status": "updated" if updated else "unchanged",
                            "permissions": desired_permissions, "policy_version": policy_hash,
                            "unmanaged_share_ids_preserved": sorted(str(row.get("id")) for row in unmanaged)})
        else:
            data = owner_client.create_group_share(
                reference.path, group_id, desired_permissions
            )
            share_id = data.get("id")
            if share_id is None:
                raise OwnCloudError("OWNCLOUD_SHARE_CREATE_NOT_CONFIRMED")
            readback = owner_client.get_share(str(share_id))
            _verify_share(readback, reference, group_id, desired_permissions)
            ledger.put(company_id, managed_key, {
                "group_id": group_id, "reference_id": reference_id,
                "owner_uid": reference.owner_uid, "file_id": reference.file_id,
                "item_type": "folder" if reference.is_collection else "file",
                "share_id": str(share_id),
            })
            results.append({"file_reference_id": reference_id, "group_id": group_id,
                            "share_id": str(share_id), "status": "created",
                            "permissions": desired_permissions, "policy_version": policy_hash})
    return results


def _department_group_policy(policy: dict[str, Any], staff_group_id: str) -> dict[str, Any]:
    """Express department ownership through the existing native group-share path."""
    groups = {
        group["group_id"]: {**group, "shares": []}
        for group in policy["department_groups"]
    }
    staff = policy["company_staff"]
    if staff["group_id"] != staff_group_id or staff_group_id in groups:
        raise ValueError("Department policy must use the configured distinct company staff group")
    groups[staff_group_id] = {
        **staff, "shares": list(staff.get("general_document_shares", [])),
    }
    for assignment in policy["department_folder_shares"]:
        group_id = assignment["department_group_id"]
        if group_id not in groups or group_id == staff_group_id:
            raise ValueError("Department folder ownership must reference a configured department group")
        share = {"file_reference_id": assignment["file_reference_id"],
                 "permissions": assignment["permissions"]}
        groups[group_id]["shares"].append(share)
        for extra in assignment.get("additional_group_shares", []):
            if extra["group_id"] not in groups:
                raise ValueError("Additional folder access must reference a configured group")
            groups[extra["group_id"]]["shares"].append({
                "file_reference_id": assignment["file_reference_id"],
                "permissions": extra["permissions"],
            })
    for assignment in policy.get("general_document_maintenance_shares", []):
        if assignment["group_id"] not in groups:
            raise ValueError("Document access must reference a configured group")
        groups[assignment["group_id"]]["shares"].append({
            "file_reference_id": assignment["file_reference_id"],
            "permissions": assignment["permissions"],
        })
    return {"mode": "role_based", "role_groups": list(groups.values())}


def resource_reference_from_webdav(
    client: OwnCloudClient,
    *,
    reference_id: str,
    owner: OwnCloudIdentity,
    path: str,
) -> tuple[OwnCloudFileReference, bytes | None]:
    reference_id = _required_text(reference_id, "OwnCloud reference ID")
    metadata, content = client.resource_metadata(owner.owncloud_uid, path)
    return OwnCloudFileReference(
        reference_id=reference_id,
        owner_employee_id=owner.employee_id,
        owner_midpoint_oid=owner.midpoint_oid,
        owner_shadow_oid=owner.midpoint_shadow_oid,
        owner_uid=owner.owncloud_uid,
        path=_relative_file_path(path),
        **metadata,
    ), content


def file_reference_from_webdav(
    client: OwnCloudClient,
    *,
    reference_id: str,
    owner: OwnCloudIdentity,
    path: str,
) -> tuple[OwnCloudFileReference, bytes]:
    reference, content = resource_reference_from_webdav(
        client, reference_id=reference_id, owner=owner, path=path,
    )
    if reference.is_collection or content is None:
        raise OwnCloudError("OWNCLOUD_WEBDAV_REFERENCE_IS_DIRECTORY")
    return reference, content


def _share_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Explicit OwnCloud file share list is required")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("OwnCloud share entries must be objects")
        reference_id = _required_text(item.get("file_reference_id"), "file reference ID")
        permissions = item.get("permissions")
        if (isinstance(permissions, bool) or not isinstance(permissions, int)
                or permissions < 1 or permissions > 31 or not permissions & PERMISSION_READ):
            raise ValueError("OwnCloud share permissions must be an explicit native permission bitmask")
        result.append({"file_reference_id": reference_id, "permissions": permissions})
    return result


def _verify_share(
    share: dict[str, Any], reference: OwnCloudFileReference, group_id: str,
    permissions: int | None,
) -> None:
    _verify_share_identity(share, reference, group_id)
    try:
        actual_permissions = int(share.get("permissions", -1))
    except (TypeError, ValueError) as error:
        raise OwnCloudError("OWNCLOUD_SHARE_READBACK_MISMATCH") from error
    if permissions is not None and actual_permissions != permissions:
        raise OwnCloudError("OWNCLOUD_SHARE_READBACK_MISMATCH")


def _verify_share_identity(
    share: dict[str, Any], reference: OwnCloudFileReference, group_id: str,
) -> None:
    if (
        str(share.get("share_type")) != "1"
        or share.get("share_with") != group_id
        or str(share.get("file_source", share.get("item_source", ""))) != reference.file_id
        or share.get("uid_owner") != reference.owner_uid
        or share.get("item_type") != ("folder" if reference.is_collection else "file")
    ):
        raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_SOURCE_CHANGED_PRESERVED")
    if not share.get("id"):
        raise OwnCloudError("OWNCLOUD_MANAGED_SHARE_IDENTITY_MISMATCH")


def _ocs_string_list(data: Any, field: str) -> set[str]:
    if not isinstance(data, dict) or field not in data:
        raise OwnCloudError("OWNCLOUD_OCS_LIST_RESPONSE_SHAPE_INVALID")
    raw = data[field]
    if isinstance(raw, dict) and set(raw) == {"element"}:
        raw = raw["element"]
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise OwnCloudError("OWNCLOUD_OCS_LIST_RESPONSE_SHAPE_INVALID")
    return set(raw)


def _group_id(value: Any) -> str:
    value = _required_text(value, "OwnCloud group ID")
    if value == "admin" or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise ValueError("OwnCloud group ID is invalid")
    return value


def _relative_file_path(value: Any) -> str:
    value = _required_text(value, "OwnCloud file path").replace("\\", "/")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("OwnCloud file paths must be non-empty paths relative to an owner home")
    return "/".join(parts)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


__all__ = [
    "OWNCLOUD_RESOURCE_OID", "OwnCloudClient", "OwnCloudError", "OwnCloudIdentity",
    "OwnCloudFileReference", "OwnCloudShareLedger", "PERMISSION_READ", "PERMISSION_UPDATE",
    "PERMISSION_CREATE", "PERMISSION_DELETE", "PERMISSION_SHARE",
    "ensure_company_staff_memberships", "file_reference_from_webdav",
    "resource_reference_from_webdav",
    "reconcile_access_policy", "resolve_midpoint_owncloud_identities",
]
