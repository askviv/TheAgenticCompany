"""Durable email transport scenarios for the local Viv integration environment.

Fixtures describe one employee mailbox and refer to credentials kept in a separate,
private JSON file. Audit rows deliberately contain identifiers, references, hashes,
and exception types only; message text and attachment bytes are never persisted.
Production fixtures use ``directory.mode = "midpoint"`` with an exact employee
name in ``employee_id`` to resolve authoritative mailbox and Rocket.Chat identifiers
before the durable run hash is created. Directory mode remains optional for isolated
transport tests.

Run with ``python -m servers.company.scenarios run --fixture ... --credentials ...``
or inspect persisted state with ``python -m servers.company.scenarios inspect``.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import fcntl
import json
import logging
import mimetypes
import os
import smtplib
import sqlite3
import ssl
import sys
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests


STEP_EMAIL_DELIVERED = "EMAIL_DELIVERED"
STEP_EMAIL_FORWARDED = "EMAIL_FORWARDED"
STEP_CHAT_POSTED = "CHAT_POSTED"
STEP_FAILED = "FAILED"

# Native midPoint resource OIDs for this deployment's linked service accounts.
MIDPOINT_RESOURCE_OIDS = {
    "rocketchat": "57a19684-cfe8-4f21-b7b4-0fb02b5ec001",
    "mailu": "57a19684-cfe8-4f21-b7b4-0fb02b5ec002",
    "owncloud": "57a19684-cfe8-4f21-b7b4-0fb02b5ec003",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_runs (
  id TEXT PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  fixture_version TEXT NOT NULL,
  environment_version TEXT NOT NULL,
  fixture_hash TEXT NOT NULL,
  actors TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  claim_owner TEXT
);
CREATE TABLE IF NOT EXISTS scenario_steps (
  run_id TEXT NOT NULL REFERENCES scenario_runs(id),
  step_key TEXT NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  external_id TEXT,
  submission_started_at TEXT,
  delivery_method TEXT,
  submission_started INTEGER NOT NULL DEFAULT 0,
  mailbox_uid TEXT,
  mailbox_uidvalidity TEXT,
  webmail_url TEXT,
  reference TEXT,
  content_hash TEXT,
  error_type TEXT,
  room_id TEXT,
  thread_id TEXT,
  browser_url TEXT,
  mention_account_id TEXT,
  file_references_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id, step_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS scenario_steps_external_id_uk
  ON scenario_steps(step_key, external_id) WHERE external_id IS NOT NULL;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture_hash(fixture: dict[str, Any], owncloud_files: list[tuple[Any, bytes]] | None = None) -> str:
    canonical = json.dumps(fixture, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    attachment_hashes = []
    for attachment in fixture.get("attachments", []):
        payload = Path(_safe_ref(attachment.get("path"), "attachment path")).read_bytes()
        attachment_hashes.append(_hash(payload))
    material = canonical + b"\0" + "\0".join(attachment_hashes).encode()
    if not owncloud_files:
        return _hash(material)
    references = json.dumps(
        [reference.public_dict() for reference, _payload in owncloud_files],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    source_hashes = [hashlib.sha256(payload).hexdigest() for _reference, payload in owncloud_files]
    return _hash(
        material + b"\0" + references + b"\0" + "\0".join(source_hashes).encode()
    )


def _message_id_for_run(seed: str, run_id: str, purpose: str) -> str:
    digest = _hash(f"{seed}:{run_id}:{purpose}".encode())[:32]
    return f"<{digest}@scenario.local>"


def _actors_snapshot(fixture: dict[str, Any]) -> dict[str, str]:
    mailbox = fixture["employee_mailbox"]
    actors = {"mailbox_address": mailbox["address"]}
    for field in ("employee_id", "mailbox_account_id"):
        value = fixture.get(field) if field == "employee_id" else mailbox.get(field)
        if value:
            actors[field] = _safe_ref(value, field)
    if fixture.get("midpoint_oid"):
        actors["midpoint_oid"] = _safe_ref(fixture["midpoint_oid"], "midpoint_oid")
    chat_id = mailbox.get("rocketchat_expected_user_id")
    if chat_id:
        actors["chat_account_id"] = _safe_ref(chat_id, "rocketchat_expected_user_id")
    return actors


def _roundcube_message_url(webmail_url: str, mailbox_uid: str) -> str:
    """Build Roundcube's native message-view request using the confirmed IMAP UID."""
    parts = urlsplit(webmail_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"_task": "mail", "_mbox": "INBOX", "_uid": str(mailbox_uid), "_action": "show"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _rocketchat_message_url(base_url: str, room: dict[str, Any], message_id: str) -> str | None:
    room_type = room.get("t")
    route = {"c": "channel", "p": "group", "d": "direct"}.get(room_type)
    identifier = room.get("name") if room_type in ("c", "p") else room.get("_id")
    if not route or not identifier:
        return None
    path = f"{route}/{quote(str(identifier), safe='')}"
    return urljoin(base_url.rstrip("/") + "/", path) + "?" + urlencode({"msg": message_id})


def _chat_text_with_mail_reference(message: str, mail_step: dict[str, Any]) -> str:
    reference = mail_step.get("webmail_url") or mail_step.get("reference")
    if not reference:
        reference = f"INBOX UIDVALIDITY={mail_step.get('mailbox_uidvalidity')} UID={mail_step.get('mailbox_uid')}"
    text = f"{message}\n\nEmployee inbox email: {reference}"
    file_references = mail_step.get("file_references", [])
    if file_references:
        rendered = "\n".join(
            f"{item['reference_id']}: {item['href']} (file ID {item['file_id']}; "
            f"ETag {item['etag']}; SHA-256 {item['sha256']})"
            for item in file_references
        )
        text += "\n\nOwnCloud source files:\n" + rendered
    return text


def _safe_ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Fixture must be a JSON object")
    for name in ("scenario_id", "fixture_version", "environment_version", "employee_mailbox"):
        if name not in data:
            raise ValueError(f"Fixture field {name} is required")
    for name in ("scenario_id", "fixture_version", "environment_version"):
        _safe_ref(data[name], f"Fixture field {name}")
    mailbox = data["employee_mailbox"]
    if not isinstance(mailbox, dict):
        raise ValueError("employee_mailbox must be an object")
    directory = data.get("directory")
    required = ["imap_host", "imap_port", "mail_credentials_ref"]
    if directory is not None:
        if not isinstance(directory, dict) or directory.get("mode") != "midpoint":
            raise ValueError("directory.mode must be midpoint when directory resolution is configured")
        _safe_ref(directory.get("base_url"), "directory.base_url")
        _safe_ref(directory.get("credentials_ref"), "directory.credentials_ref")
        _safe_ref(data.get("employee_id"), "employee_id central name")
    else:
        required.append("address")
    for name in required:
        if name not in mailbox:
            raise ValueError(f"employee_mailbox.{name} is required")
    if mailbox.get("webmail_url"):
        _safe_ref(mailbox["webmail_url"], "employee_mailbox.webmail_url")
    _safe_ref(data.get("message_id"), "message_id")
    _safe_ref(data.get("from_address"), "from_address")
    _safe_ref(data.get("subject"), "subject")
    if not isinstance(data.get("body"), str):
        raise ValueError("body must be a string")
    if not isinstance(data.get("attachments", []), list):
        raise ValueError("attachments must be a list")
    owncloud = data.get("owncloud")
    if owncloud is not None:
        if not isinstance(owncloud, dict):
            raise ValueError("owncloud must be an object")
        _safe_ref(owncloud.get("base_url"), "owncloud.base_url")
        references = owncloud.get("file_references", [])
        if not isinstance(references, list):
            raise ValueError("owncloud.file_references must be a list")
        seen_reference_ids = set()
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("OwnCloud file references must be objects")
            reference_id = _safe_ref(reference.get("reference_id"), "OwnCloud reference_id")
            if reference_id in seen_reference_ids:
                raise ValueError("OwnCloud reference_id must be unique within a scenario")
            seen_reference_ids.add(reference_id)
            _safe_ref(reference.get("owner_employee_id"), "OwnCloud owner_employee_id")
            _safe_ref(reference.get("path"), "OwnCloud file path")
            _safe_ref(reference.get("credentials_ref"), "OwnCloud credentials_ref")
        if references and directory is None:
            raise ValueError("OwnCloud source files require exact MidPoint employee identity resolution")
    if data.get("forward_to_viv") and not _safe_ref(data.get("viv_route_address"), "viv_route_address"):
        raise ValueError("viv_route_address is required when forwarding")
    if data.get("forward_to_viv"):
        _safe_ref(data.get("smtp4dev_base_url"), "smtp4dev_base_url")
        _safe_ref(mailbox.get("submission_smtp_host"), "employee_mailbox.submission_smtp_host")
        if "submission_smtp_port" not in mailbox:
            raise ValueError("employee_mailbox.submission_smtp_port is required when forwarding")
        _safe_ref(mailbox.get("submission_tls", "starttls"), "employee_mailbox.submission_tls")
        if data.get("smtp4dev_mailbox_name"):
            _safe_ref(data["smtp4dev_mailbox_name"], "smtp4dev_mailbox_name")
    if data.get("webmail_url"):
        _safe_ref(data["webmail_url"], "webmail_url")
    if data.get("post_chat"):
        _safe_ref(data.get("chat_message"), "chat_message")
        if "mention_account_id" in data:
            _safe_ref(data["mention_account_id"], "mention_account_id")
        for name in ("chat_credentials_ref", "rocketchat_base_url", "rocketchat_room_id"):
            _safe_ref(mailbox.get(name), f"employee_mailbox.{name}")
        if directory is None:
            _safe_ref(mailbox.get("rocketchat_expected_user_id"), "employee_mailbox.rocketchat_expected_user_id")
    return data


def _load_credentials(path: Path, fixture: dict[str, Any]) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise ValueError("Private credentials JSON file is unavailable")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise PermissionError("Private credentials JSON must not be accessible to group or other users")
    all_credentials = json.loads(path.read_text())
    mailbox = fixture["employee_mailbox"]
    refs = {"mail": _safe_ref(mailbox["mail_credentials_ref"], "mail_credentials_ref")}
    if fixture.get("directory"):
        refs["directory"] = _safe_ref(fixture["directory"]["credentials_ref"], "directory.credentials_ref")
    if fixture.get("post_chat"):
        refs["chat"] = _safe_ref(mailbox["chat_credentials_ref"], "chat_credentials_ref")
    resolved: dict[str, dict[str, str]] = {}
    for kind, ref in refs.items():
        item = all_credentials.get(ref)
        if not isinstance(item, dict):
            raise ValueError(f"Credential reference {kind} is unavailable")
        resolved[kind] = {key: _safe_ref(item.get(key), f"{kind} {key}") for key in ("username", "password")}
    for file_reference in (fixture.get("owncloud") or {}).get("file_references", []):
        ref = _safe_ref(file_reference["credentials_ref"], "OwnCloud credentials_ref")
        key = "owncloud:" + ref
        if key in resolved:
            continue
        value = all_credentials.get(ref)
        if not isinstance(value, dict):
            raise ValueError("OwnCloud file-owner credentials reference is unavailable")
        resolved[key] = {
            key: _safe_ref(value.get(key), f"OwnCloud {key}")
            for key in ("username", "password")
        }
    return resolved


def _resolve_midpoint_identity(fixture: dict[str, Any], credentials: dict[str, str]) -> dict[str, Any]:
    directory = fixture.get("directory")
    if not directory:
        return fixture
    base = directory["base_url"].rstrip("/") + "/"
    response = requests.get(urljoin(base, "ws/rest/users"),
                            auth=(credentials["username"], credentials["password"]),
                            headers={"Accept": "application/json"}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    entries = ((payload.get("object") or {}).get("object") if isinstance(payload, dict) else None)
    if not isinstance(entries, list):
        raise RuntimeError("MIDPOINT_USER_LIST_SHAPE_INVALID")
    requested_name = fixture["employee_id"]
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == requested_name]
    if len(matches) != 1:
        raise ValueError("MIDPOINT_EMPLOYEE_NAME_NOT_UNIQUE")
    employee = matches[0]
    oid = _safe_ref(employee.get("oid"), "midPoint user OID")
    resource_oids = dict(MIDPOINT_RESOURCE_OIDS)
    resource_oids.update(directory.get("resource_oids", {}))
    linked = employee.get("linkRef")
    if isinstance(linked, dict):
        linked = [linked]
    if not isinstance(linked, list):
        raise RuntimeError("MIDPOINT_USER_LINKS_SHAPE_INVALID")
    owncloud_references = (fixture.get("owncloud") or {}).get("file_references", [])
    shadows: dict[str, list[dict[str, Any]]] = {"mailu": [], "rocketchat": []}
    if owncloud_references:
        shadows["owncloud"] = []
    for link in linked:
        if not isinstance(link, dict) or not link.get("oid"):
            continue
        shadow_response = requests.get(urljoin(base, f"ws/rest/shadows/{link['oid']}"),
                                       auth=(credentials["username"], credentials["password"]),
                                       headers={"Accept": "application/json"}, timeout=15)
        shadow_response.raise_for_status()
        shadow_payload = shadow_response.json()
        shadow = shadow_payload.get("shadow") if isinstance(shadow_payload, dict) else None
        if not isinstance(shadow, dict):
            raise RuntimeError("MIDPOINT_SHADOW_SHAPE_INVALID")
        resource_ref = shadow.get("resourceRef")
        resource_oid = resource_ref.get("oid") if isinstance(resource_ref, dict) else None
        for service in shadows:
            if resource_oid == resource_oids[service]:
                shadows[service].append(shadow)

    if len(shadows["mailu"]) != 1:
        raise ValueError("MIDPOINT_MAILU_LINK_NOT_UNIQUE")
    mail_shadow = shadows["mailu"][0]
    mail_attrs = mail_shadow.get("attributes") or {}
    if not isinstance(mail_attrs, dict):
        raise RuntimeError("MIDPOINT_MAILU_ATTRIBUTES_SHAPE_INVALID")
    email_address = _safe_ref(mail_attrs.get("email"), "midPoint Mailu account email")
    # Current Mailu shadows use the mailbox address as their service UID; newer
    # projections may expose that same UID in the native `_id` attribute.
    mailbox_account_id = _safe_ref(mail_attrs.get("_id") or email_address,
                                   "midPoint Mailu account ID")
    if fixture.get("post_chat"):
        if len(shadows["rocketchat"]) != 1:
            raise ValueError("MIDPOINT_ROCKETCHAT_LINK_NOT_UNIQUE")
        chat_attrs = shadows["rocketchat"][0].get("attributes") or {}
        if not isinstance(chat_attrs, dict):
            raise RuntimeError("MIDPOINT_ROCKETCHAT_ATTRIBUTES_SHAPE_INVALID")
        fixture["employee_mailbox"]["rocketchat_expected_user_id"] = _safe_ref(
            chat_attrs.get("_id"), "midPoint Rocket.Chat account ID")
    fixture["employee_id"] = requested_name
    fixture["employee_name"] = requested_name
    fixture["midpoint_oid"] = oid
    fixture["employee_mailbox"]["address"] = email_address
    fixture["employee_mailbox"]["mailbox_account_id"] = mailbox_account_id
    if owncloud_references:
        owners_needed = {
            _safe_ref(reference.get("owner_employee_id"), "OwnCloud owner employee ID")
            for reference in owncloud_references
        }
        user_by_name = {
            item.get("name"): item for item in entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        owner_identities = {}
        for owner_employee_id in sorted(owners_needed):
            owner = user_by_name.get(owner_employee_id)
            if not isinstance(owner, dict):
                raise ValueError("MIDPOINT_OWNCLOUD_OWNER_NOT_FOUND")
            owner_oid = _safe_ref(owner.get("oid"), "midPoint OwnCloud owner OID")
            owner_links = owner.get("linkRef", [])
            if isinstance(owner_links, dict):
                owner_links = [owner_links]
            if not isinstance(owner_links, list):
                raise RuntimeError("MIDPOINT_OWNCLOUD_OWNER_LINKS_SHAPE_INVALID")
            if owner_employee_id == requested_name:
                candidates = shadows["owncloud"]
            else:
                candidates = []
                for owner_link in owner_links:
                    if not isinstance(owner_link, dict) or not owner_link.get("oid"):
                        continue
                    owner_shadow_response = requests.get(
                        urljoin(base, f"ws/rest/shadows/{owner_link['oid']}"),
                        auth=(credentials["username"], credentials["password"]),
                        headers={"Accept": "application/json"}, timeout=15,
                    )
                    owner_shadow_response.raise_for_status()
                    owner_shadow_payload = owner_shadow_response.json()
                    owner_shadow = owner_shadow_payload.get("shadow") if isinstance(owner_shadow_payload, dict) else None
                    if not isinstance(owner_shadow, dict):
                        raise RuntimeError("MIDPOINT_OWNCLOUD_OWNER_SHADOW_SHAPE_INVALID")
                    owner_resource_ref = owner_shadow.get("resourceRef")
                    if isinstance(owner_resource_ref, dict) and owner_resource_ref.get("oid") == resource_oids["owncloud"]:
                        candidates.append(owner_shadow)
            if len(candidates) != 1:
                raise ValueError("MIDPOINT_OWNCLOUD_OWNER_LINK_NOT_UNIQUE")
            owner_shadow = candidates[0]
            owner_shadow_oid = _safe_ref(owner_shadow.get("oid"), "midPoint OwnCloud shadow OID")
            owner_attrs = owner_shadow.get("attributes") or {}
            if not isinstance(owner_attrs, dict):
                raise RuntimeError("MIDPOINT_OWNCLOUD_OWNER_ATTRIBUTES_SHAPE_INVALID")
            owner_uid = _safe_ref(owner_attrs.get("_id") or owner_attrs.get("uid"), "midPoint OwnCloud UID")
            ownership_response = requests.get(
                urljoin(base, f"ws/rest/shadows/{owner_shadow_oid}/owner"),
                auth=(credentials["username"], credentials["password"]),
                headers={"Accept": "application/json"}, timeout=15,
            )
            ownership_response.raise_for_status()
            ownership_payload = ownership_response.json()
            shadow_owner = ownership_payload.get("user", ownership_payload.get("object", {}))
            if not isinstance(shadow_owner, dict) or shadow_owner.get("oid") != owner_oid:
                raise ValueError("MIDPOINT_OWNCLOUD_SHADOW_OWNER_MISMATCH")
            owner_identities[owner_employee_id] = {
                "owner_uid": owner_uid,
                "owner_midpoint_oid": owner_oid,
                "owner_shadow_oid": owner_shadow_oid,
            }
        for reference in owncloud_references:
            reference.update(owner_identities[reference["owner_employee_id"]])
    return fixture


def _fixture_message(
    fixture: dict[str, Any], recipient: str, sender: str, message_id: str,
    owncloud_files: list[tuple[Any, bytes]] | None = None,
) -> tuple[EmailMessage, str]:
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = fixture["subject"]
    message["Message-ID"] = message_id
    message.set_content(fixture["body"])
    digest_material = bytearray(message.as_bytes())
    for attachment in fixture.get("attachments", []):
        if not isinstance(attachment, dict):
            raise ValueError("Each attachment must specify filename and path")
        name = _safe_ref(attachment.get("filename"), "attachment filename")
        file_path = Path(_safe_ref(attachment.get("path"), "attachment path"))
        payload = file_path.read_bytes()
        maintype = attachment.get("maintype", "application")
        subtype = attachment.get("subtype", "octet-stream")
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=name)
        digest_material.extend(name.encode())
        digest_material.extend(payload)
    for reference, payload in owncloud_files or []:
        filename = Path(reference.path).name
        maintype, subtype = (mimetypes.guess_type(filename)[0] or "application/octet-stream").split("/", 1)
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        part = list(message.iter_attachments())[-1]
        part["Content-Location"] = reference.href
        part["X-TAC-OwnCloud-Reference-ID"] = reference.reference_id
        part["X-TAC-OwnCloud-FileID"] = reference.file_id
        part["X-TAC-OwnCloud-ETag"] = reference.etag
        part["X-TAC-OwnCloud-SHA256"] = reference.sha256
        digest_material.extend(reference.reference_id.encode())
        digest_material.extend(reference.file_id.encode())
        digest_material.extend(reference.etag.encode())
        digest_material.extend(reference.sha256.encode())
        digest_material.extend(payload)
    return message, _hash(bytes(digest_material))


def _resolve_owncloud_file_references(
    fixture: dict[str, Any],
    credentials: dict[str, dict[str, str]],
    *,
    session: Any = None,
) -> list[tuple[Any, bytes]]:
    """Fetch only configured files from their MidPoint-linked owner account."""
    owncloud = fixture.get("owncloud") or {}
    references = owncloud.get("file_references", [])
    if not references:
        return []
    from .owncloud import OwnCloudClient, OwnCloudIdentity, file_reference_from_webdav

    resolved = []
    for source in references:
        owner_uid = _safe_ref(source.get("owner_uid"), "resolved OwnCloud owner UID")
        owner_employee_id = _safe_ref(source.get("owner_employee_id"), "OwnCloud owner employee ID")
        auth = credentials.get("owncloud:" + _safe_ref(source.get("credentials_ref"), "OwnCloud credentials_ref"))
        if not isinstance(auth, dict):
            raise ValueError("OwnCloud file-owner credentials are unavailable")
        if auth.get("username") != owner_uid:
            raise ValueError("OwnCloud file credentials must authenticate as the exact linked owner UID")
        owner = OwnCloudIdentity(
            employee_id=owner_employee_id,
            midpoint_oid=_safe_ref(source.get("owner_midpoint_oid"), "MidPoint owner OID"),
            midpoint_shadow_oid=_safe_ref(source.get("owner_shadow_oid"), "MidPoint OwnCloud shadow OID"),
            owncloud_uid=owner_uid,
        )
        client = OwnCloudClient(
            owncloud["base_url"], auth["username"], auth["password"], session=session
        )
        resolved.append(file_reference_from_webdav(
            client,
            reference_id=_safe_ref(source.get("reference_id"), "OwnCloud reference ID"),
            owner=owner,
            path=_safe_ref(source.get("path"), "OwnCloud file path"),
        ))
    return resolved


def _ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    if not ca_file:
        return ssl.create_default_context()
    path = Path(ca_file)
    if not path.is_file():
        raise ValueError("Configured TLS CA file is unavailable")
    return ssl.create_default_context(cafile=str(path))


class RealMailTransport:
    """Mailu IMAP APPEND for fixture delivery and authenticated SMTP for forwarding."""

    def deliver_fixture(self, fixture: dict[str, Any], credentials: dict[str, str], message: EmailMessage,
                        digest: str, on_append_started: Any) -> None:
        from imapclient import IMAPClient

        box = fixture["employee_mailbox"]
        context = _ssl_context(box.get("tls_ca_file"))
        with IMAPClient(box["imap_host"], port=int(box["imap_port"]), ssl=True,
                        ssl_context=context, timeout=20) as client:
            client.login(credentials["username"], credentials["password"])
            client.select_folder("INBOX")
            on_append_started()
            client.append("INBOX", message.as_bytes())

    def confirm_message(self, fixture: dict[str, Any], credentials: dict[str, str], message_id: str) -> dict[str, str] | None:
        from imapclient import IMAPClient

        box = fixture["employee_mailbox"]
        context = _ssl_context(box.get("tls_ca_file"))
        timeout_seconds = float(fixture.get("confirm_timeout_seconds", 30))
        deadline = time.monotonic() + timeout_seconds
        with IMAPClient(box["imap_host"], port=int(box["imap_port"]), ssl=True,
                        ssl_context=context, timeout=20) as client:
            client.login(credentials["username"], credentials["password"])
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY", "unknown"))
            if isinstance(uidvalidity, bytes):
                uidvalidity = uidvalidity.decode("ascii")

            def find_message() -> dict[str, str] | None:
                for uid in client.search(["HEADER", "Message-ID", message_id]):
                    fetched = client.fetch([uid], [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
                    header_bytes = fetched[uid].get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]", b"")
                    parsed = email.message_from_bytes(header_bytes, policy=email.policy.default)
                    message_ids = parsed.get_all("Message-ID", [])
                    if len(message_ids) == 1 and str(message_ids[0]).strip() == message_id:
                        uid_text = str(uid)
                        webmail_root = box.get("webmail_url") or fixture.get("webmail_url") or ""
                        return {"mailbox_uid": uid_text,
                                "reference": f"INBOX UID {uid_text}",
                                "webmail_url": _roundcube_message_url(webmail_root, uid_text) if webmail_root else "",
                                "uidvalidity": str(uidvalidity)}
                return None

            while True:
                found = find_message()
                if found:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # IDLE gives the mailbox server a chance to notify us immediately.
                # Exit at most every two seconds to re-search, covering arrivals in
                # the small interval between SEARCH and entering IDLE.
                client.idle()
                try:
                    client.idle_check(timeout=min(remaining, 2.0))
                finally:
                    client.idle_done()

    def forward_message(self, fixture: dict[str, Any], credentials: dict[str, str], message: EmailMessage,
                        digest: str, on_send_started: Any) -> None:
        box = fixture["employee_mailbox"]
        context = _ssl_context(box.get("tls_ca_file"))
        mode = box.get("submission_tls", "starttls")
        if mode == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(box["submission_smtp_host"], int(box["submission_smtp_port"]),
                                                   timeout=20, context=context)
        elif mode == "starttls":
            client = smtplib.SMTP(box["submission_smtp_host"], int(box["submission_smtp_port"]), timeout=20)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        else:
            raise ValueError("submission_tls must be starttls or ssl")
        with client:
            client.login(credentials["username"], credentials["password"])
            on_send_started()
            client.send_message(message)

    def confirm_smtp4dev(self, fixture: dict[str, Any], message_id: str) -> dict[str, str] | None:
        """Use smtp4dev's documented message search and detail API, matching Message-ID exactly."""
        base = fixture["smtp4dev_base_url"].rstrip("/") + "/"
        verify: bool | str = fixture["employee_mailbox"].get("tls_ca_file") or True
        listing = requests.get(urljoin(base, "api/Messages"),
                               params={"searchTerms": fixture["subject"], "page": 1, "pageSize": 100},
                               timeout=15, verify=verify)
        listing.raise_for_status()
        first_page = listing.json()
        summaries = list(first_page.get("results") or [])
        page_count = int(first_page.get("pageCount") or 1)
        for page in range(2, page_count + 1):
            listing = requests.get(urljoin(base, "api/Messages"),
                                   params={"searchTerms": fixture["subject"], "page": page, "pageSize": 100},
                                   timeout=15, verify=verify)
            listing.raise_for_status()
            summaries.extend(listing.json().get("results") or [])
        for summary in summaries:
            message_uuid = summary.get("id")
            if not isinstance(message_uuid, str):
                continue
            detail_response = requests.get(urljoin(base, f"api/Messages/{message_uuid}"), timeout=15, verify=verify)
            detail_response.raise_for_status()
            detail = detail_response.json()
            if any(str(header.get("name", "")).casefold() == "message-id"
                   and str(header.get("value", "")).strip() == message_id
                   for header in detail.get("headers") or []):
                # The running UI's router exposes this explicit mailbox/folder/message route.
                mailbox = quote(fixture.get("smtp4dev_mailbox_name", "Default"), safe="")
                webmail_url = (fixture["smtp4dev_base_url"].rstrip("/")
                               + f"/#/messages/mailbox/{mailbox}/folder/INBOX/message/{quote(message_uuid, safe='')}")
                return {"mailbox_uid": message_uuid, "reference": f"smtp4dev message {message_uuid}",
                        "webmail_url": webmail_url}
        return None


class RealChatTransport:
    """Send through an employee's Rocket.Chat browser and verify its DDP receipt."""

    def __init__(self, session: requests.Session | None = None, browser_session_provider: Any = None):
        self.session = session or requests.Session()
        self.browser_session_provider = browser_session_provider

    def _browser_provider(self):
        if self.browser_session_provider is None:
            from .browser_chat import PlaywrightEmployeeBrowserSessionProvider

            self.browser_session_provider = PlaywrightEmployeeBrowserSessionProvider()
        return self.browser_session_provider

    @staticmethod
    def _message_from_frame(frame: Any) -> list[dict[str, Any]]:
        if isinstance(frame, bytes):
            try:
                frame = frame.decode("utf-8")
            except UnicodeDecodeError:
                return []
        if isinstance(frame, str):
            try:
                frame = json.loads(frame)
            except ValueError:
                return []
        found: list[dict[str, Any]] = []
        pending = [frame]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                found.append(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return found

    @staticmethod
    def _ddp_frame(frame: Any) -> dict[str, Any] | None:
        if isinstance(frame, bytes):
            try:
                frame = frame.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(frame, str):
            try:
                frame = json.loads(frame)
            except ValueError:
                return None
        return frame if isinstance(frame, dict) else None

    def _ddp_send_requests(self, frames: list[Any], room_id: str,
                           message_text: str) -> list[dict[str, str]]:
        requests_found = []
        for raw_frame in frames:
            frame = self._ddp_frame(raw_frame)
            if not frame or frame.get("msg") != "method" or frame.get("method") != "sendMessage":
                continue
            message_ids = [value.get("_id") for value in self._message_from_frame(frame.get("params"))
                           if value.get("rid") == room_id and value.get("msg") == message_text
                           and isinstance(value.get("_id"), str) and value.get("_id")]
            if len(message_ids) == 1 and isinstance(frame.get("id"), str) and frame["id"]:
                requests_found.append({"rpc_id": frame["id"], "message_id": message_ids[0]})
        # Identical matching send calls are ambiguous and cannot identify this attempt.
        return requests_found

    def _ddp_send_acknowledged(self, frames: list[Any], rpc_id: str,
                               message_id: str) -> bool:
        for raw_frame in frames:
            frame = self._ddp_frame(raw_frame)
            if (not frame or frame.get("msg") != "result" or frame.get("id") != rpc_id
                    or "error" in frame or "result" not in frame):
                continue
            result = frame["result"]
            acknowledged_id = result if isinstance(result, str) else None
            if isinstance(result, dict):
                acknowledged_id = result.get("_id") or result.get("messageId")
            if acknowledged_id is None or acknowledged_id == message_id:
                return True
        return False

    @staticmethod
    def _browser_room_url(base: str, room: dict[str, Any]) -> str:
        message_url = _rocketchat_message_url(base, room, "")
        if not message_url:
            raise RuntimeError("ROCKETCHAT_ROOM_BROWSER_ROUTE_UNAVAILABLE")
        parts = urlsplit(message_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _is_chat_websocket(socket: Any, base: str) -> bool:
        parsed, origin = urlsplit(socket.url), urlsplit(base)
        return parsed.netloc == origin.netloc and parsed.path.endswith("/websocket")

    def _active_chat_websocket(self, page: Any, sockets: list[Any],
                               socket_states: dict[int, dict[str, Any]], base: str) -> Any:
        while True:
            candidates = [socket for socket in sockets
                          if self._is_chat_websocket(socket, base) and not socket.is_closed()]
            if candidates:
                socket = candidates[-1]
            else:
                page.wait_for_event(
                    "websocket", predicate=lambda candidate: self._is_chat_websocket(candidate, base),
                    timeout=0,
                )
                continue

            state = socket_states[id(socket)]
            if state["connected"]:
                return socket
            try:
                # The listener installed on the page event buffers this frame even
                # if it arrives while React is making the composer visible.
                socket.wait_for_event("framereceived", timeout=0)
            except Exception as error:
                if socket.is_closed():
                    continue
                raise

    def _room(self, base: str, headers: dict[str, str], room_id: str) -> dict[str, Any]:
        response = self.session.get(urljoin(base, "api/v1/rooms.info"), headers=headers,
                                    params={"roomId": room_id}, timeout=15)
        response.raise_for_status()
        room = response.json().get("room") or {}
        if room.get("_id") != room_id:
            raise RuntimeError("ROCKETCHAT_ROOM_LOOKUP_MISMATCH")
        return room

    def _browser_auth(self, page: Any, base: str, credentials: dict[str, str], expected_user_id: str) -> dict[str, str]:
        page.goto(base, wait_until="domcontentloaded", timeout=0)
        auth = page.evaluate("""() => ({
          token: localStorage.getItem('Meteor.loginToken'),
          userId: localStorage.getItem('Meteor.userId')
        })""")
        if not isinstance(auth, dict) or not auth.get("token") or not auth.get("userId"):
            password = page.locator('input[type="password"]')
            username = page.locator('input[name="username"], input[name="email"], input[type="email"]')
            username.first.wait_for(state="visible", timeout=0)
            password.wait_for(state="visible", timeout=0)
            username.first.fill(credentials["username"], timeout=0)
            password.fill(credentials["password"], timeout=0)
            submit = page.locator('button[type="submit"]')
            submit.first.wait_for(state="visible", timeout=0)
            submit.first.click(timeout=0)
            page.wait_for_function(
                "() => !!localStorage.getItem('Meteor.loginToken') && !!localStorage.getItem('Meteor.userId')",
                timeout=0,
            )
            auth = page.evaluate("""() => ({
              token: localStorage.getItem('Meteor.loginToken'),
              userId: localStorage.getItem('Meteor.userId')
            })""")
        token, user_id = auth.get("token"), auth.get("userId")
        if not isinstance(token, str) or not token or not isinstance(user_id, str) or not user_id:
            raise RuntimeError("ROCKETCHAT_BROWSER_AUTHENTICATION_UNAVAILABLE")
        headers = {"X-Auth-Token": token, "X-User-Id": user_id}
        response = self.session.get(urljoin(base, "api/v1/me"), headers=headers, timeout=15)
        response.raise_for_status()
        if (response.json().get("_id") != expected_user_id or user_id != expected_user_id):
            raise RuntimeError("ROCKETCHAT_AUTHOR_MISMATCH")
        return headers

    @staticmethod
    def _verified_message(value: Any, *, message_id: str, room_id: str,
                          author_id: str, message_text: str) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("_id") != message_id:
            return None
        author = value.get("u") or {}
        if value.get("rid") != room_id:
            raise RuntimeError("ROCKETCHAT_MESSAGE_ROOM_MISMATCH")
        if not isinstance(author, dict) or author.get("_id") != author_id:
            raise RuntimeError("ROCKETCHAT_MESSAGE_AUTHOR_MISMATCH")
        if value.get("msg") != message_text:
            raise RuntimeError("ROCKETCHAT_MESSAGE_TEXT_MISMATCH")
        return value

    def _message_by_id(self, base: str, headers: dict[str, str], message_id: str,
                       room_id: str, author_id: str, message_text: str) -> dict[str, Any] | None:
        response = self.session.get(urljoin(base, "api/v1/chat.getMessage"), headers=headers,
                                    params={"msgId": message_id}, timeout=15)
        if response.status_code in {400, 404}:
            return None
        response.raise_for_status()
        message = response.json().get("message")
        return self._verified_message(message, message_id=message_id, room_id=room_id,
                                      author_id=author_id, message_text=message_text)

    def _receipt(self, fixture: dict[str, Any], room: dict[str, Any],
                 message: dict[str, Any]) -> dict[str, str | None]:
        room_id = fixture["employee_mailbox"]["rocketchat_room_id"]
        message_id = message["_id"]
        receipt = {"message_id": message_id, "room_id": room_id,
                   "thread_id": message.get("tmid"),
                   "browser_url": _rocketchat_message_url(
                       fixture["employee_mailbox"]["rocketchat_base_url"].rstrip("/") + "/",
                       room, message_id)}
        if fixture.get("mention_account_id"):
            receipt["mention_account_id"] = fixture["mention_account_id"]
        return receipt

    def _mention_text(self, fixture: dict[str, Any], headers: dict[str, str]) -> str | None:
        account_id = fixture.get("mention_account_id")
        if not account_id:
            return None
        base = fixture["employee_mailbox"]["rocketchat_base_url"].rstrip("/") + "/"
        response = self.session.get(urljoin(base, "api/v1/users.info"), headers=headers,
                                    params={"userId": account_id}, timeout=15)
        response.raise_for_status()
        user = response.json().get("user") or {}
        if user.get("_id") != account_id:
            raise RuntimeError("ROCKETCHAT_MENTION_ACCOUNT_MISMATCH")
        username = user.get("username")
        if not isinstance(username, str) or not username.strip():
            raise RuntimeError("ROCKETCHAT_MENTION_USERNAME_MISSING")
        return "@" + username

    def post(self, fixture: dict[str, Any], credentials: dict[str, str], attempt_id: str,
             *, on_submission_started: Any = None,
             on_message_id_captured: Any = None,
             on_effective_message_text: Any = None) -> dict[str, str | None]:
        _safe_ref(attempt_id, "chat attempt reference")
        box = fixture["employee_mailbox"]
        base = box["rocketchat_base_url"].rstrip("/") + "/"
        expected_author = box["rocketchat_expected_user_id"]
        with self._browser_provider().open_for_employee_email(box["address"]) as page:
            sockets = []
            socket_states: dict[int, dict[str, Any]] = {}

            def track_socket(socket: Any) -> None:
                sockets.append(socket)
                state: dict[str, Any] = {"connected": False, "frames": [], "sent_frames": []}
                socket_states[id(socket)] = state

                def track_frame(frame: Any) -> None:
                    state["frames"].append(frame)
                    if any(message.get("msg") == "connected"
                           for message in self._message_from_frame(frame)):
                        state["connected"] = True

                socket.on("framereceived", track_frame)
                socket.on("framesent", state["sent_frames"].append)

            page.on("websocket", track_socket)
            headers = self._browser_auth(page, base, credentials, expected_author)
            room_id = box["rocketchat_room_id"]
            room = self._room(base, headers, room_id)
            mention = self._mention_text(fixture, headers)
            message_text = (mention + " " if mention else "") + fixture["chat_message"]
            if on_effective_message_text is not None:
                on_effective_message_text(message_text)
            page.goto(self._browser_room_url(base, room), wait_until="domcontentloaded", timeout=0)
            composer = page.locator(
                'textarea[aria-label="Message"], textarea.rc-message-box__textarea, '
                'textarea[placeholder*="message" i], [contenteditable="true"][role="textbox"]'
            ).first
            composer.wait_for(state="visible", timeout=0)
            if not composer.is_visible():
                raise RuntimeError("ROCKETCHAT_MESSAGE_COMPOSER_UNAVAILABLE")
            socket = self._active_chat_websocket(page, sockets, socket_states, base)
            socket_state = socket_states[id(socket)]
            sent_frames = socket_state["sent_frames"]
            received_frames = socket_state["frames"]
            message_text = message_text.replace("\r\n", "\n").replace("\r", "\n")
            composer.fill("", timeout=0)
            lines = message_text.split("\n")
            for index, line in enumerate(lines):
                if line:
                    composer.press_sequentially(line, delay=150, timeout=0)
                if index < len(lines) - 1:
                    # Rocket.Chat documents Shift+Enter as a line break; plain
                    # Enter remains reserved for the final submit action.
                    composer.press("Shift+Enter", timeout=0)
            if on_submission_started is not None:
                on_submission_started()
            # Drop room-history events buffered during page load. Attribution below
            # still requires the ID observed in this click's outgoing DDP method.
            sent_frames.clear()
            received_frames.clear()
            composer.press("Enter", timeout=0)

            native_message = None
            while True:
                requests_found = self._ddp_send_requests(sent_frames, room_id, message_text)
                if len(requests_found) > 1:
                    raise RuntimeError("ROCKETCHAT_BROWSER_SEND_OUTCOME_UNCERTAIN")
                if requests_found:
                    send_request = requests_found[0]
                    for raw_frame in received_frames:
                        for candidate in self._message_from_frame(raw_frame):
                            author = candidate.get("u") or {}
                            if (candidate.get("_id") == send_request["message_id"]
                                    and candidate.get("rid") == room_id
                                    and isinstance(author, dict) and author.get("_id") == expected_author
                                    and candidate.get("msg") == message_text):
                                native_message = candidate
                                break
                        if native_message is not None:
                            break
                    acknowledged = self._ddp_send_acknowledged(
                        received_frames, send_request["rpc_id"], send_request["message_id"])
                else:
                    acknowledged = False
                if native_message is not None and acknowledged:
                    break
                try:
                    # Listener buffering plus Playwright's event wait avoids polling
                    # and leaves the acknowledged send pending until frames arrive.
                    socket.wait_for_event("framereceived", timeout=0)
                except Exception as error:
                    raise RuntimeError("ROCKETCHAT_BROWSER_SEND_OUTCOME_UNCERTAIN") from error
            native_id = native_message["_id"]
            if on_message_id_captured is not None:
                on_message_id_captured(native_id)
            confirmed = self._message_by_id(base, headers, native_id, room_id,
                                            expected_author, message_text)
            if confirmed is None:
                raise RuntimeError("ROCKETCHAT_MESSAGE_API_NOT_CONFIRMED")
            return self._receipt(fixture, room, confirmed)

    def confirm_message(self, fixture: dict[str, Any], credentials: dict[str, str],
                        message_id: str | None, message_text: str,
        submission_started_at: str | None) -> dict[str, str | None] | None:
        """Read-only recovery check; uncertain browser sends are never resent."""
        box = fixture["employee_mailbox"]
        base = box["rocketchat_base_url"].rstrip("/") + "/"
        author_id = box["rocketchat_expected_user_id"]
        with self._browser_provider().open_for_employee_email(box["address"]) as page:
            headers = self._browser_auth(page, base, credentials, author_id)
            room_id = box["rocketchat_room_id"]
            room = self._room(base, headers, room_id)
            if message_id:
                message = self._message_by_id(base, headers, message_id, room_id, author_id, message_text)
            else:
                # Text/time matches can identify an older identical message. Without
                # the browser-generated DDP ID persisted before failure, recovery is
                # deliberately inconclusive and the attempt is never resent.
                return None
            return self._receipt(fixture, room, message) if message else None

class ScenarioStore:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(_SCHEMA)
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(scenario_runs)")}
            if "actors" not in run_columns:
                connection.execute("ALTER TABLE scenario_runs ADD COLUMN actors TEXT NOT NULL DEFAULT '{}' ")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(scenario_steps)")}
            if "delivery_method" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN delivery_method TEXT")
            if "submission_started_at" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN submission_started_at TEXT")
            if "submission_started" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN submission_started INTEGER NOT NULL DEFAULT 0")
            if "mailbox_uidvalidity" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN mailbox_uidvalidity TEXT")
            if "webmail_url" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN webmail_url TEXT")
            if "room_id" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN room_id TEXT")
            if "thread_id" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN thread_id TEXT")
            if "browser_url" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN browser_url TEXT")
            if "file_references_json" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN file_references_json TEXT NOT NULL DEFAULT '[]'")
            if "mention_account_id" not in columns:
                connection.execute("ALTER TABLE scenario_steps ADD COLUMN mention_account_id TEXT")
            connection.commit()

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self):
        with closing(self.connection()) as connection:
            with connection:
                yield connection

    def get_or_create_run(self, fixture: dict[str, Any], fixture_hash: str,
                          requested_run_id: str | None = None, new_run: bool = False) -> str:
        actors_json = json.dumps(_actors_snapshot(fixture), sort_keys=True, separators=(",", ":"))
        if requested_run_id:
            with self.transaction() as connection:
                row = connection.execute("SELECT * FROM scenario_runs WHERE id=?", (requested_run_id,)).fetchone()
                if row is None:
                    raise ValueError("Requested scenario run ID was not found")
                expected = (fixture["scenario_id"], fixture["fixture_version"], fixture["environment_version"], fixture_hash)
                actual = (row["scenario_id"], row["fixture_version"], row["environment_version"], row["fixture_hash"])
                if actual != expected:
                    raise ValueError("Fixture content or version does not match the requested run")
                if row["actors"] == "{}":
                    connection.execute("UPDATE scenario_runs SET actors=? WHERE id=? AND actors='{}'", (actors_json, requested_run_id))
                return str(row["id"])
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scenario_runs WHERE scenario_id=? AND fixture_version=? AND environment_version=? ORDER BY created_at DESC LIMIT 1",
                (fixture["scenario_id"], fixture["fixture_version"], fixture["environment_version"]),
            ).fetchone()
            if row:
                if row["fixture_hash"] != fixture_hash:
                    raise ValueError("Fixture content changed without a fixture_version update")
                if not new_run:
                    if row["actors"] == "{}":
                        connection.execute("UPDATE scenario_runs SET actors=? WHERE id=? AND actors='{}'", (actors_json, row["id"]))
                    return str(row["id"])
        run_id = str(uuid.uuid4())
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO scenario_runs(id,scenario_id,fixture_version,environment_version,fixture_hash,actors,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, fixture["scenario_id"], fixture["fixture_version"], fixture["environment_version"], fixture_hash, actors_json, "RUNNING", now, now),
            )
        return run_id

    def claim(self, run_id: str, owner: str, fixture_hash: str) -> None:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE scenario_runs SET claim_owner=?,updated_at=? WHERE id=? AND fixture_hash=?",
                (owner, _now(), run_id, fixture_hash),
            ).rowcount
            if changed != 1:
                raise RuntimeError("SCENARIO_RUN_CLAIM_FAILED")

    def release(self, run_id: str, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE scenario_runs SET claim_owner=NULL,updated_at=? WHERE id=? AND claim_owner=?",
                               (_now(), run_id, owner))

    def step(self, run_id: str, step_key: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM scenario_steps WHERE run_id=? AND step_key=?", (run_id, step_key)).fetchone()
            return _scenario_step_dict(row) if row else None

    def record(self, run_id: str, step_key: str, step: str, status: str, *, external_id: str | None = None,
               delivery_method: str | None = None, submission_started: bool = False,
               submission_started_at: str | None = None,
               room_id: str | None = None, thread_id: str | None = None,
               browser_url: str | None = None,
               mention_account_id: str | None = None,
               mailbox_uid: str | None = None, mailbox_uidvalidity: str | None = None,
               webmail_url: str | None = None, reference: str | None = None,
               content_hash: str | None = None, error_type: str | None = None,
               file_references: list[dict[str, Any]] | None = None) -> None:
        file_references_json = json.dumps(file_references or [], sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO scenario_steps(run_id,step_key,step,status,external_id,submission_started_at,delivery_method,submission_started,mailbox_uid,mailbox_uidvalidity,webmail_url,reference,content_hash,error_type,room_id,thread_id,browser_url,mention_account_id,file_references_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,step_key) DO UPDATE SET step=excluded.step,status=excluded.status,external_id=excluded.external_id,submission_started_at=COALESCE(excluded.submission_started_at,scenario_steps.submission_started_at),delivery_method=COALESCE(excluded.delivery_method,scenario_steps.delivery_method),submission_started=MAX(excluded.submission_started,scenario_steps.submission_started),mailbox_uid=excluded.mailbox_uid,mailbox_uidvalidity=excluded.mailbox_uidvalidity,webmail_url=excluded.webmail_url,reference=excluded.reference,content_hash=excluded.content_hash,error_type=excluded.error_type,room_id=COALESCE(excluded.room_id,scenario_steps.room_id),thread_id=COALESCE(excluded.thread_id,scenario_steps.thread_id),browser_url=COALESCE(excluded.browser_url,scenario_steps.browser_url),mention_account_id=COALESCE(excluded.mention_account_id,scenario_steps.mention_account_id),file_references_json=excluded.file_references_json,updated_at=excluded.updated_at",
                (run_id, step_key, step, status, external_id, submission_started_at, delivery_method, int(submission_started), mailbox_uid, mailbox_uidvalidity, webmail_url, reference, content_hash, error_type, room_id, thread_id, browser_url, mention_account_id, file_references_json, _now()),
            )
            connection.execute("UPDATE scenario_runs SET updated_at=? WHERE id=?", (_now(), run_id))

    def mark_submission_started(self, run_id: str, step_key: str) -> None:
        started_at = _now()
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE scenario_steps SET submission_started=1,submission_started_at=COALESCE(submission_started_at,?),updated_at=? WHERE run_id=? AND step_key=?",
                (started_at, started_at, run_id, step_key),
            ).rowcount
            if changed != 1:
                raise RuntimeError("SCENARIO_STEP_SUBMISSION_MARK_FAILED")

    def finish(self, run_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE scenario_runs SET status=?,updated_at=? WHERE id=?", (status, _now(), run_id))

    def inspect(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            if run_id:
                runs = connection.execute("SELECT * FROM scenario_runs WHERE id=?", (run_id,)).fetchall()
            else:
                runs = connection.execute("SELECT * FROM scenario_runs ORDER BY created_at DESC").fetchall()
            return [{**dict(row), "steps": [_scenario_step_dict(s) for s in connection.execute(
                "SELECT * FROM scenario_steps WHERE run_id=? ORDER BY updated_at", (row["id"],)).fetchall()]} for row in runs]


def _scenario_step_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    encoded = value.pop("file_references_json", "[]")
    value["file_references"] = json.loads(encoded or "[]")
    return value


def _deliver_inbound(
    store: ScenarioStore, run_id: str, fixture: dict[str, Any],
    credentials: dict[str, str], mail: Any,
    owncloud_files: list[tuple[Any, bytes]] | None = None,
) -> None:
    original_id = _message_id_for_run(fixture["message_id"], run_id, "inbound")
    recipient = _safe_ref(fixture["employee_mailbox"]["address"], "employee mailbox address")
    owncloud_files = owncloud_files or []
    file_references = [reference.public_dict() for reference, _payload in owncloud_files]
    message, digest = _fixture_message(
        fixture, recipient, fixture["from_address"], original_id, owncloud_files
    )
    current = store.step(run_id, "mail-delivery")
    if current and current["status"] == STEP_EMAIL_DELIVERED:
        return
    needs_reconciliation = current and current["status"] in ("IN_PROGRESS", STEP_FAILED) and (
        bool(current["submission_started"]) or current["delivery_method"] is None
    )
    if needs_reconciliation:
        store.record(run_id, "mail-delivery", STEP_FAILED, STEP_FAILED, external_id=original_id,
                     delivery_method="IMAP_APPEND", submission_started=bool(current["submission_started"]),
                     content_hash=digest, error_type="ImapAppendOutcomeUncertain",
                     file_references=file_references)
        # APPEND is not idempotent. Reconcile an attempt that may have reached the
        # server before deciding whether this step is complete.
        try:
            found = mail.confirm_message(fixture, credentials, original_id)
        except Exception as error:
            store.record(run_id, "mail-delivery", STEP_FAILED, STEP_FAILED, external_id=original_id,
                         delivery_method="IMAP_APPEND", submission_started=bool(current["submission_started"]),
                         content_hash=digest, error_type=type(error).__name__,
                         file_references=file_references)
            raise RuntimeError("Inbound delivery needs IMAP reconciliation") from error
        if found:
            store.record(run_id, "mail-delivery", STEP_EMAIL_DELIVERED, STEP_EMAIL_DELIVERED,
                         external_id=original_id, delivery_method="IMAP_APPEND", submission_started=True,
                         mailbox_uid=found["mailbox_uid"],
                         mailbox_uidvalidity=found.get("uidvalidity"), webmail_url=found.get("webmail_url"),
                         reference=found["reference"], content_hash=digest,
                         file_references=file_references)
            return
        store.record(run_id, "mail-delivery", STEP_FAILED, STEP_FAILED, external_id=original_id,
                     delivery_method="IMAP_APPEND", submission_started=True,
                     content_hash=digest, error_type="ImapAppendMessageNotFound",
                     file_references=file_references)
        raise RuntimeError("Inbound APPEND outcome is uncertain; IMAP did not confirm the Message-ID")
    store.record(run_id, "mail-delivery", STEP_EMAIL_DELIVERED, "IN_PROGRESS", external_id=original_id,
                 delivery_method="IMAP_APPEND", content_hash=digest,
                 file_references=file_references)
    try:
        mail.deliver_fixture(fixture, credentials, message, digest,
                             lambda: store.mark_submission_started(run_id, "mail-delivery"))
        confirmed = mail.confirm_message(fixture, credentials, original_id)
    except Exception as error:
        store.record(run_id, "mail-delivery", STEP_FAILED, STEP_FAILED, external_id=original_id,
                     content_hash=digest, error_type=type(error).__name__,
                     file_references=file_references)
        raise RuntimeError("Inbound email delivery could not be confirmed") from error
    if not confirmed:
        store.record(run_id, "mail-delivery", STEP_FAILED, STEP_FAILED, external_id=original_id,
                     delivery_method="IMAP_APPEND", submission_started=True,
                     content_hash=digest, error_type="ImapMessageNotFound",
                     file_references=file_references)
        raise RuntimeError("Inbound APPEND outcome is uncertain; IMAP did not confirm the Message-ID")
    store.record(run_id, "mail-delivery", STEP_EMAIL_DELIVERED, STEP_EMAIL_DELIVERED,
                 external_id=original_id, delivery_method="IMAP_APPEND", submission_started=True,
                 mailbox_uid=confirmed["mailbox_uid"],
                 mailbox_uidvalidity=confirmed.get("uidvalidity"), webmail_url=confirmed.get("webmail_url"),
                 reference=confirmed["reference"], content_hash=digest,
                 file_references=file_references)


def _forward(
    store: ScenarioStore, run_id: str, fixture: dict[str, Any],
    credentials: dict[str, str], mail: Any,
    owncloud_files: list[tuple[Any, bytes]] | None = None,
) -> None:
    key = "mail-forward"
    forward_id = _message_id_for_run(fixture["message_id"], run_id, "forward")
    original_id = _message_id_for_run(fixture["message_id"], run_id, "inbound")
    owncloud_files = owncloud_files or []
    file_references = [reference.public_dict() for reference, _payload in owncloud_files]
    message, digest = _fixture_message(
        fixture, fixture["viv_route_address"], fixture["employee_mailbox"]["address"],
        forward_id, owncloud_files,
    )
    message["X-Forwarded-Message-ID"] = original_id
    current = store.step(run_id, key)
    if current and current["status"] == STEP_EMAIL_FORWARDED:
        return
    needs_reconciliation = current and current["status"] in ("IN_PROGRESS", STEP_FAILED) and (
        bool(current["submission_started"]) or current["delivery_method"] is None
    )
    if needs_reconciliation:
        # Reconcile smtp4dev by Message-ID before any retry; SMTP itself is not idempotent.
        try:
            found = _confirm_smtp4dev_until(mail, fixture, forward_id)
        except Exception as error:
            store.record(run_id, key, STEP_FAILED, STEP_FAILED, external_id=forward_id,
                         delivery_method="SMTP_SUBMISSION", submission_started=True,
                         reference="configured-viv-route",
                         content_hash=digest, error_type=type(error).__name__,
                         file_references=file_references)
            raise RuntimeError("Forwarding delivery needs smtp4dev reconciliation") from error
        if found:
            store.record(run_id, key, STEP_EMAIL_FORWARDED, STEP_EMAIL_FORWARDED, external_id=forward_id,
                         delivery_method="SMTP_SUBMISSION", submission_started=True,
                         mailbox_uid=found["mailbox_uid"], webmail_url=found.get("webmail_url"),
                         reference=found["reference"], content_hash=digest,
                         file_references=file_references)
            return
        store.record(run_id, key, STEP_FAILED, STEP_FAILED, external_id=forward_id,
                     delivery_method="SMTP_SUBMISSION", submission_started=True,
                     reference="configured-viv-route",
                     content_hash=digest, error_type="Smtp4devMessageNotFound",
                     file_references=file_references)
        raise RuntimeError("Forwarding SMTP outcome is uncertain; smtp4dev did not confirm the Message-ID")
    store.record(run_id, key, STEP_EMAIL_FORWARDED, "IN_PROGRESS", external_id=forward_id,
                 delivery_method="SMTP_SUBMISSION", reference="configured-viv-route", content_hash=digest,
                 file_references=file_references)
    try:
        mail.forward_message(fixture, credentials, message, digest,
                             lambda: store.mark_submission_started(run_id, key))
        confirmed = _confirm_smtp4dev_until(mail, fixture, forward_id)
    except Exception as error:
        store.record(run_id, key, STEP_FAILED, STEP_FAILED, external_id=forward_id,
                     delivery_method="SMTP_SUBMISSION", reference="configured-viv-route",
                     content_hash=digest, error_type=type(error).__name__,
                     file_references=file_references)
        raise RuntimeError("Forwarding SMTP outcome is uncertain; reconcile before retry") from error
    if not confirmed:
        store.record(run_id, key, STEP_FAILED, STEP_FAILED, external_id=forward_id,
                     delivery_method="SMTP_SUBMISSION", submission_started=True,
                     reference="configured-viv-route",
                     content_hash=digest, error_type="Smtp4devMessageNotFound",
                     file_references=file_references)
        raise RuntimeError("Forwarding SMTP outcome is uncertain; smtp4dev did not confirm the Message-ID")
    store.record(run_id, key, STEP_EMAIL_FORWARDED, STEP_EMAIL_FORWARDED, external_id=forward_id,
                 delivery_method="SMTP_SUBMISSION", submission_started=True,
                 mailbox_uid=confirmed["mailbox_uid"],
                 webmail_url=confirmed.get("webmail_url"),
                 reference=confirmed["reference"], content_hash=digest,
                 file_references=file_references)


def _confirm_smtp4dev_until(mail: Any, fixture: dict[str, Any], message_id: str) -> dict[str, str] | None:
    """Bounded API condition check for smtp4dev's captured message receipt."""
    deadline = time.monotonic() + float(fixture.get("confirm_timeout_seconds", 30))
    while True:
        found = mail.confirm_smtp4dev(fixture, message_id)
        if found:
            return found
        if time.monotonic() >= deadline:
            return None


@contextmanager
def _exclusive_execution_lock(database: Path):
    lock_path = Path(str(database) + ".scenario.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(lock_path.open("a+b")) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _error_type_chain(error: BaseException) -> list[str]:
    result = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        result.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return result


def run_scenario(fixture_path: str | Path, credentials_path: str | Path, database: str | Path,
                 *, run_id: str | None = None, new_run: bool = False,
                 mail_transport: Any = None, chat_transport: Any = None,
                 owncloud_session: Any = None) -> dict[str, Any]:
    fixture = _load_fixture(Path(fixture_path))
    credentials = _load_credentials(Path(credentials_path), fixture)
    fixture = _resolve_midpoint_identity(fixture, credentials.get("directory", {}))
    owncloud_files = _resolve_owncloud_file_references(
        fixture, credentials, session=owncloud_session
    )
    mail = mail_transport or RealMailTransport()
    chat = chat_transport or RealChatTransport() if fixture.get("post_chat") else None
    store = ScenarioStore(Path(database))
    fixture_hash = _fixture_hash(fixture, owncloud_files)
    with _exclusive_execution_lock(Path(database)):
        run_id = store.get_or_create_run(fixture, fixture_hash, run_id, new_run)
        owner = str(uuid.uuid4())
        store.claim(run_id, owner, fixture_hash)
        try:
            _deliver_inbound(
                store, run_id, fixture, credentials["mail"], mail, owncloud_files
            )
            if fixture.get("forward_to_viv"):
                _forward(store, run_id, fixture, credentials["mail"], mail, owncloud_files)
            if fixture.get("post_chat"):
                chat_attempt_id = "scenario-" + _hash((run_id + ":chat").encode())[:32]
                mail_step = store.step(run_id, "mail-delivery")
                chat_fixture = dict(fixture)
                chat_fixture["chat_message"] = _chat_text_with_mail_reference(fixture["chat_message"], mail_step or {})
                chat_hash = _hash(chat_fixture["chat_message"].encode())
                current = store.step(run_id, "chat-post")
                if not current or current["status"] != STEP_CHAT_POSTED:
                    file_references = [reference.public_dict() for reference, _payload in owncloud_files]
                    may_have_submitted = bool(current and current["submission_started"]) or bool(
                        current and current["delivery_method"] == "ROCKETCHAT_API")
                    if may_have_submitted:
                        store.record(run_id, "chat-post", STEP_FAILED, STEP_FAILED,
                                     external_id=current.get("external_id"),
                                     delivery_method=current.get("delivery_method") or "ROCKETCHAT_BROWSER",
                                     submission_started=bool(current["submission_started"]),
                                     submission_started_at=current.get("submission_started_at"),
                                     room_id=fixture["employee_mailbox"]["rocketchat_room_id"],
                                     mention_account_id=fixture.get("mention_account_id"),
                                     reference="employee-rocketchat", content_hash=chat_hash,
                                     error_type="ChatBrowserOutcomeUncertain",
                                     file_references=file_references)
                        try:
                            receipt = chat.confirm_message(
                                chat_fixture, credentials["chat"], current.get("external_id"),
                                chat_fixture["chat_message"], current.get("submission_started_at"),
                            )
                        except Exception as error:
                            latest = store.step(run_id, "chat-post") or current
                            store.record(run_id, "chat-post", STEP_FAILED, STEP_FAILED,
                                         external_id=latest.get("external_id"),
                                         delivery_method=latest.get("delivery_method"),
                                         submission_started=bool(latest["submission_started"]),
                                         submission_started_at=latest.get("submission_started_at"),
                                         content_hash=chat_hash, error_type=type(error).__name__,
                                         file_references=file_references)
                            raise RuntimeError("Rocket.Chat browser send needs read-only reconciliation") from error
                        if not receipt:
                            latest = store.step(run_id, "chat-post") or current
                            store.record(run_id, "chat-post", STEP_FAILED, STEP_FAILED,
                                         external_id=latest.get("external_id"),
                                         delivery_method=latest.get("delivery_method"),
                                         submission_started=bool(latest["submission_started"]),
                                         submission_started_at=latest.get("submission_started_at"),
                                         content_hash=chat_hash, error_type="ChatBrowserMessageNotFound",
                                         file_references=file_references)
                            raise RuntimeError("Rocket.Chat browser send outcome is uncertain; it was not resent")
                        store.record(run_id, "chat-post", STEP_CHAT_POSTED, STEP_CHAT_POSTED,
                                     external_id=receipt["message_id"], delivery_method="ROCKETCHAT_BROWSER",
                                     submission_started=True,
                                     submission_started_at=current.get("submission_started_at"),
                                     room_id=receipt["room_id"], thread_id=receipt.get("thread_id"),
                                     browser_url=receipt.get("browser_url"),
                                     mention_account_id=receipt.get("mention_account_id"),
                                     reference="employee-rocketchat", content_hash=chat_hash,
                                     file_references=file_references)
                    else:
                        store.record(run_id, "chat-post", STEP_CHAT_POSTED, "IN_PROGRESS",
                                     delivery_method="ROCKETCHAT_BROWSER",
                                     room_id=fixture["employee_mailbox"]["rocketchat_room_id"],
                                     mention_account_id=fixture.get("mention_account_id"),
                                     reference="employee-rocketchat", content_hash=chat_hash,
                                     file_references=file_references)

                        def mark_chat_submission_started() -> None:
                            store.mark_submission_started(run_id, "chat-post")

                        def record_native_message_id(message_id: str) -> None:
                            latest = store.step(run_id, "chat-post")
                            store.record(run_id, "chat-post", STEP_CHAT_POSTED, "IN_PROGRESS",
                                         external_id=message_id, delivery_method="ROCKETCHAT_BROWSER",
                                         submission_started=True,
                                         submission_started_at=(latest or {}).get("submission_started_at"),
                                         room_id=fixture["employee_mailbox"]["rocketchat_room_id"],
                                         mention_account_id=fixture.get("mention_account_id"),
                                         reference="employee-rocketchat", content_hash=chat_hash,
                                         file_references=file_references)

                        try:
                            receipt = chat.post(
                                chat_fixture, credentials["chat"], chat_attempt_id,
                                on_submission_started=mark_chat_submission_started,
                                on_message_id_captured=record_native_message_id,
                            )
                        except Exception as error:
                            latest = store.step(run_id, "chat-post") or {}
                            store.record(run_id, "chat-post", STEP_FAILED, STEP_FAILED,
                                         external_id=latest.get("external_id"),
                                         delivery_method="ROCKETCHAT_BROWSER",
                                         submission_started=bool(latest.get("submission_started")),
                                         submission_started_at=latest.get("submission_started_at"),
                                         room_id=fixture["employee_mailbox"]["rocketchat_room_id"],
                                         mention_account_id=fixture.get("mention_account_id"),
                                         reference="employee-rocketchat", content_hash=chat_hash,
                                         error_type=type(error).__name__,
                                         file_references=file_references)
                            raise RuntimeError("Rocket.Chat browser post was not confirmed") from error
                        store.record(run_id, "chat-post", STEP_CHAT_POSTED, STEP_CHAT_POSTED,
                                     external_id=receipt["message_id"], delivery_method="ROCKETCHAT_BROWSER",
                                     submission_started=True,
                                     submission_started_at=(store.step(run_id, "chat-post") or {}).get("submission_started_at"),
                                     room_id=receipt["room_id"], thread_id=receipt.get("thread_id"),
                                     browser_url=receipt.get("browser_url"),
                                     mention_account_id=receipt.get("mention_account_id"),
                                     reference="employee-rocketchat", content_hash=chat_hash,
                                     file_references=file_references)
            store.finish(run_id, "COMPLETED")
        except Exception as error:
            store.finish(run_id, "FAILED")
            chain = _error_type_chain(error)
            logging.error("Scenario failed run_id=%s exception_types=%s", run_id, ">".join(chain))
            result = store.inspect(run_id)[0]
            result["run_error_types"] = chain
            return result
        finally:
            store.release(run_id, owner)
        return store.inspect(run_id)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or inspect durable email transport scenarios")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run a scenario fixture")
    run_parser.add_argument("--fixture", required=True)
    run_parser.add_argument("--credentials", required=True, help="private JSON file referenced by fixture credential refs")
    run_parser.add_argument("--database", default=os.environ.get("EXPERIMENT_SCENARIOS_DB", "/data/scenarios.sqlite"))
    run_identity = run_parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id", help="resume this exact durable run")
    run_identity.add_argument("--new-run", action="store_true", help="start a fresh run even if a matching fixture run exists")
    inspect_parser = subparsers.add_parser("inspect", help="inspect durable scenario audit rows")
    inspect_parser.add_argument("--database", default=os.environ.get("EXPERIMENT_SCENARIOS_DB", "/data/scenarios.sqlite"))
    inspect_parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run_scenario(args.fixture, args.credentials, args.database,
                                  run_id=args.run_id, new_run=args.new_run)
        else:
            result = ScenarioStore(Path(args.database)).inspect(args.run_id)
        print(json.dumps(result, indent=2))
        return 0 if args.command == "inspect" or result.get("status") == "COMPLETED" else 1
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_types": _error_type_chain(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
