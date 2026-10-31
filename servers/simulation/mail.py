"""Authenticated Mailu gateway for run-scoped enterprise simulation messages.

Mailu and smtp4dev are existing services. This adapter reuses the company's
native transport/receipt methods; it does not create an SMTP server, persist
outbox state, or treat an email sender header as approval authority.
"""

from __future__ import annotations

import email
import email.policy
from email.header import decode_header, make_header
import json
from pathlib import Path
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any, Callable
from urllib.parse import quote, urljoin

import requests
from authres import AuthenticationResultsHeader, AuthResError
from imapclient import IMAPClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from servers.company.scenarios import (
    RealMailTransport,
    _roundcube_message_url,
    _ssl_context,
)


SPECIAL_VIV_ID = "viv"
SPECIAL_VIV_EMAIL = "agent@agent.example.test"


class _ActorRef(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    actor_id: str = Field(min_length=1)
    email: str = Field(min_length=3)


class _ActorRecord(_ActorRef):
    kind: str | None = None


class _GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actors: list[_ActorRecord]
    mailu_smtp_host: str = "mail.company.example.test"
    mailu_smtp_port: int = Field(default=2587, gt=0, le=65535)
    mailu_imap_host: str = "mail.company.example.test"
    mailu_imap_port: int = Field(default=2993, gt=0, le=65535)
    webmail_url: str = "https://mail.company.example.test/webmail/"
    delivery_aliases: dict[str, str] = Field(default_factory=dict)
    tls_ca_file: str | None = None
    smtp_timeout_seconds: float = Field(default=20, gt=0)
    confirm_timeout_seconds: float = Field(default=30, gt=0)
    smtp4dev_smtp_host: str = "127.0.0.1"
    smtp4dev_smtp_port: int = Field(default=19125, gt=0, le=65535)
    smtp4dev_api_base_url: str = "http://127.0.0.1:19157"
    smtp4dev_mailbox_name: str = "Default"

    @model_validator(mode="after")
    def validate_actor_records(self) -> _GatewayConfig:
        actor_ids = [actor.actor_id for actor in self.actors]
        actor_emails = [actor.email.casefold() for actor in self.actors]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("configured actor IDs must be unique")
        if len(actor_emails) != len(set(actor_emails)):
            raise ValueError("configured actor email addresses must be unique")
        for actor in self.actors:
            is_viv_id = actor.actor_id == SPECIAL_VIV_ID
            is_viv_email = actor.email.casefold() == SPECIAL_VIV_EMAIL
            if is_viv_id != is_viv_email:
                raise ValueError("the configured Viv actor must use its fixed actor ID and address")
        from email.headerregistry import Address
        for address, actor_id in self.delivery_aliases.items():
            Address(addr_spec=address)
            if actor_id not in actor_ids or actor_id == SPECIAL_VIV_ID:
                raise ValueError("delivery alias must resolve to a configured company mailbox actor")
        return self


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class _SendContext(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject: str = Field(min_length=1)
    message_id: str = Field(min_length=3)
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=0, strict=True)
    in_reply_to: str | None = None
    automated: bool = False


class _ReceiptContext(_SendContext):
    pass


class _PollContext(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    after_uid: int = Field(ge=0)
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=0, strict=True)
    known_message_ids: set[str]


class _WaitContext(_PollContext):
    timeout: float = Field(default=30, gt=0)


class _VivPollContext(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    run_id: str = Field(min_length=1)
    generation: int = Field(ge=0, strict=True)
    known_ids: set[str]


class _ActivityContext(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    timeout: float = Field(default=30, gt=0)


class _IssueNotificationContext(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message_id: str = Field(pattern=r"^<[^<>\s]+>$")


class MailDeliveryUnconfirmed(RuntimeError):
    """SMTP accepted a message but no matching mailbox receipt was found."""


class MailReceiptMismatch(RuntimeError):
    """A message ID exists, but its run correlation or subject does not match."""


class MailReplySourceUnavailable(RuntimeError):
    """An employee reply has no unique exact source message in its inbox."""


class SMTP4DevAPIError(RuntimeError):
    """smtp4dev API request or response failed without exposing its body."""

    def __init__(self, operation: str, status: int | None = None):
        self.operation = operation
        self.status = status
        detail = f"smtp4dev {operation} failed"
        if status is not None:
            detail += f" (HTTP {status})"
        super().__init__(detail)


class EmployeeBrowserUnavailable(RuntimeError):
    """An employee-authored message cannot use the managed native browser."""


def _single_header(headers: dict[str, list[Any]], name: str) -> str:
    """Return one decoded header; duplicate correlation fields are ambiguous."""
    values = headers.get(name, headers.get(name.casefold(), []))
    if len(values) != 1:
        return ""
    return str(make_header(decode_header(str(values[0])))).strip()


def _mailu_authenticated_actor(auth_results: list[Any], mailu_host: str,
                              actor_by_email: dict[str, str]) -> str | None:
    """Trust one Rspamd result from our configured authserv-id with its SASL user."""
    if len(auth_results) != 1:
        return None
    try:
        parsed = AuthenticationResultsHeader.parse(
            "Authentication-Results: " + str(auth_results[0])
        )
    except (AuthResError, SyntaxError, ValueError):
        return None
    if parsed.authserv_id.casefold() != mailu_host.casefold():
        return None
    auth_methods = [result for result in parsed.results if result.method == "auth"]
    if len(auth_methods) != 1 or auth_methods[0].result != "pass":
        return None
    usernames = [prop.value for prop in auth_methods[0].properties
                 if prop.type == "smtp" and prop.name == "auth"]
    if len(usernames) != 1:
        return None
    return actor_by_email.get(usernames[0].casefold())


def _flag_text(flag: Any) -> str:
    return flag.decode("ascii") if isinstance(flag, bytes) else str(flag)


def _referenced_message_ids(
    message: email.message.Message | dict[str, list[Any]],
) -> set[str]:
    if isinstance(message, email.message.Message):
        values = [*message.get_all("In-Reply-To", []), *message.get_all("References", [])]
    else:
        values = [
            *message.get("in-reply-to", []),
            *message.get("references", []),
        ]
    return {
        message_id
        for value in values
        for message_id in re.findall(r"<[^<>\s]+>", str(value))
    }


def _decoded_header(values: list[Any]) -> str:
    if not values:
        return ""
    return ", ".join(
        str(make_header(decode_header(str(value)))).strip()
        for value in values
    )


class MailuGateway:
    """Route run-scoped mail through the existing Mailu and smtp4dev services.

    ``config['actors']`` is a list of trusted actor records containing
    ``actor_id`` and ``email`` (and optionally ``kind``). ``credentials`` maps
    email addresses to private ``username``/``password`` records. Credentials
    stay in memory and are never included in receipts or errors.
    """

    def __init__(self, config: dict[str, Any], credentials: dict[str, dict[str, str]],
                 *, employee_desktops: Any | None = None, webmail_sender: Any | None = None):
        self.config = _GatewayConfig.model_validate(config)
        self._actors = {actor.actor_id: actor for actor in self.config.actors}
        self._credentials = {
            address.casefold(): _Credentials.model_validate(value)
            for address, value in credentials.items()
        }
        self._transport = RealMailTransport()
        self.employee_desktops = employee_desktops
        self._webmail_sender = webmail_sender

    def set_employee_desktops(self, manager: Any | None) -> None:
        """Attach the coordinator-owned employee browser runtime after engine setup."""
        self.employee_desktops = manager
        self._webmail_sender = None

    def _actor(self, value: dict[str, Any], role: str) -> _ActorRecord:
        actor_ref = _ActorRef.model_validate(value)
        actor = self._actors.get(actor_ref.actor_id)
        if actor is None or actor.email.casefold() != actor_ref.email.casefold():
            raise PermissionError(f"{role} actor is not in the configured actor directory")
        return actor

    def _credentials_for(self, actor: _ActorRecord) -> _Credentials:
        credentials = self._credentials.get(actor.email.casefold())
        if credentials is None:
            raise KeyError(f"credentials are unavailable for configured actor {actor.actor_id}")
        return credentials

    def _is_viv(self, actor: _ActorRecord) -> bool:
        return actor.actor_id == SPECIAL_VIV_ID and actor.email.casefold() == SPECIAL_VIV_EMAIL

    def _employee_id(self, actor: _ActorRecord) -> str | None:
        if self._is_viv(actor):
            return None
        if self.employee_desktops is None:
            if actor.kind == "employee":
                raise EmployeeBrowserUnavailable(
                    "employee browser runtime is not configured for this native send"
                )
            return None
        profile_path = getattr(self.employee_desktops, "company_profile_path", None)
        if not isinstance(profile_path, (str, Path)):
            raise EmployeeBrowserUnavailable("employee browser runtime has no company identity profile")
        try:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EmployeeBrowserUnavailable("company identity profile is unavailable for native send") from error
        employees = profile.get("employees") if isinstance(profile, dict) else None
        if not isinstance(employees, list):
            raise EmployeeBrowserUnavailable("company identity profile has no employee directory")
        matches = [item for item in employees if isinstance(item, dict)
                   and isinstance(item.get("email"), str)
                   and item["email"].casefold() == actor.email.casefold()]
        if not matches:
            if actor.kind == "employee":
                raise EmployeeBrowserUnavailable(
                    "employee actor email does not map to one company profile identity"
                )
            return None
        employee_ids = [item.get("id") for item in matches]
        if len(matches) != 1 or not isinstance(employee_ids[0], str) or not employee_ids[0]:
            raise EmployeeBrowserUnavailable("company profile email does not resolve to one employee identity")
        return employee_ids[0]

    def _employee_webmail(self):
        if self.employee_desktops is None:
            raise EmployeeBrowserUnavailable("employee browser runtime is not configured for this native send")
        if self._webmail_sender is None:
            from .employee_webmail import EmployeeWebmailSender

            self._webmail_sender = EmployeeWebmailSender(
                self.employee_desktops, self.config.webmail_url,
            )
        return self._webmail_sender

    def open_employee_inbox(self, employee_id: str, session: dict[str, Any] | None = None) -> None:
        """Open one profile-matched employee inbox in an already requested desktop."""
        if self.employee_desktops is None:
            raise EmployeeBrowserUnavailable("employee browser runtime is not configured")
        profile_path = getattr(self.employee_desktops, "company_profile_path", None)
        if not isinstance(profile_path, (str, Path)):
            raise EmployeeBrowserUnavailable("employee browser runtime has no company identity profile")
        try:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EmployeeBrowserUnavailable("company identity profile is unavailable for native inbox") from error
        employees = profile.get("employees") if isinstance(profile, dict) else None
        if not isinstance(employees, list):
            raise EmployeeBrowserUnavailable("company identity profile has no employee directory")
        matches = [item for item in employees if isinstance(item, dict) and item.get("id") == employee_id]
        if len(matches) != 1 or not isinstance(matches[0].get("email"), str):
            raise EmployeeBrowserUnavailable("employee ID does not resolve to one configured mailbox")
        email_address = matches[0]["email"]
        actors = [actor for actor in self._actors.values()
                  if actor.email.casefold() == email_address.casefold()]
        if len(actors) != 1 or self._is_viv(actors[0]):
            raise EmployeeBrowserUnavailable("employee profile mailbox does not resolve to one mail actor")
        credentials = self._credentials_for(actors[0])
        self._employee_webmail().open_inbox(
            employee_id,
            email_address,
            credentials.username,
            credentials.password,
            session=session,
        )

    def refresh_employee_inbox_if_running(self, actor: dict[str, Any]) -> bool:
        """Refresh a mapped employee's inbox only when its desktop is already running."""
        sender_actor = self._actor(actor, "mailbox")
        if sender_actor.kind != "employee":
            return False
        employee_id = self._employee_id(sender_actor)
        if employee_id is None:
            return False
        if self.employee_desktops is None:
            raise EmployeeBrowserUnavailable("employee browser runtime is not configured")
        session = self.employee_desktops.existing_session(employee_id)
        if session is None:
            return False
        credentials = self._credentials_for(sender_actor)
        self._employee_webmail().refresh_inbox(
            employee_id,
            sender_actor.email,
            credentials.username,
            credentials.password,
            session=session,
        )
        return True

    def _reply_source(self, actor: _ActorRecord, message_id: str) -> dict[str, str]:
        """Resolve one exact employee INBOX message without changing its flags."""
        if self._is_viv(actor):
            raise MailReceiptMismatch("a Viv capture message cannot be an employee inbox reply source")
        context = _IssueNotificationContext(message_id=message_id)
        credentials = self._credentials_for(actor)
        matches: list[dict[str, str]] = []
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.login(credentials.username, credentials.password)
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = self._uidvalidity(selected)
            for uid_value in client.search(["HEADER", "Message-ID", context.message_id]):
                uid = int(uid_value)
                fetched = client.fetch(
                    [uid], [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]
                ).get(uid)
                if not isinstance(fetched, dict):
                    raise MailReceiptMismatch("Mailu reply-source search returned incomplete metadata")
                raw = fetched.get(
                    b"BODY[HEADER.FIELDS (MESSAGE-ID)]",
                    fetched.get(b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]", b""),
                )
                parsed = email.message_from_bytes(raw or b"", policy=email.policy.default)
                actual_id = _single_header(
                    {"message-id": parsed.get_all("Message-ID", [])}, "message-id"
                )
                if actual_id != context.message_id:
                    continue
                # The exact Message-ID was found in this employee's authenticated INBOX.
                # To/Cc may contain aliases, and Bcc is intentionally absent from headers.
                matches.append({
                    "message_id": context.message_id,
                    "mailbox_uid": str(uid),
                    "uidvalidity": uidvalidity,
                    "webmail_url": _roundcube_message_url(self.config.webmail_url, str(uid)),
                })
        if len(matches) > 1:
            raise MailReceiptMismatch("Mailu employee inbox has duplicate exact reply-source messages")
        return matches[0] if matches else {}

    def open_employee_message_for_response(
        self, actor: dict[str, Any], message_id: str,
    ) -> bool:
        """Show the exact received message when a mapped employee handles it.

        This is called at the committed response decision boundary, never for
        generic IMAP arrival. Exact Message-ID lookup is read-only; navigating
        to its native Roundcube view is the employee's actual read action.
        """
        sender_actor = self._actor(actor, "mailbox")
        if self._is_viv(sender_actor):
            return False
        employee_id = self._employee_id(sender_actor)
        if employee_id is None:
            return False
        if self.employee_desktops is None:
            raise EmployeeBrowserUnavailable("employee browser runtime is not configured")
        source = self._reply_source(sender_actor, message_id)
        if not source:
            raise MailReplySourceUnavailable(
                "employee response has no exact source message in the configured inbox"
            )
        credentials = self._credentials_for(sender_actor)
        self._employee_webmail().open_message(
            employee_id,
            sender_actor.email,
            credentials.username,
            credentials.password,
            source["webmail_url"],
        )
        return True

    def verify_reply_source(self, actor: dict[str, Any], source: dict[str, str]) -> None:
        """Revalidate an exact source UID before the browser opens its Reply UI."""
        current = self._reply_source(self._actor(actor, "sender"), source["message_id"])
        if not current or any(current.get(key) != source.get(key)
                              for key in ("mailbox_uid", "uidvalidity", "webmail_url")):
            raise MailReceiptMismatch("Mailu reply-source UID changed before the browser opened it")

    def _smtp_send(
        self,
        sender: _ActorRecord,
        recipient: _ActorRecord,
        message: EmailMessage,
        on_submission_started: Callable[[], None],
        *, delivery_address: str | None = None,
    ) -> None:
        destination = delivery_address or recipient.email
        if self._is_viv(sender):
            with smtplib.SMTP(
                self.config.smtp4dev_smtp_host,
                self.config.smtp4dev_smtp_port,
                timeout=self.config.smtp_timeout_seconds,
            ) as client:
                client.ehlo()
                on_submission_started()
                refused = client.send_message(
                    message, from_addr=sender.email, to_addrs=[destination]
                )
        else:
            sender_credentials = self._credentials_for(sender)
            with smtplib.SMTP(
                self.config.mailu_smtp_host,
                self.config.mailu_smtp_port,
                timeout=self.config.smtp_timeout_seconds,
            ) as client:
                client.ehlo()
                client.starttls(context=_ssl_context(self.config.tls_ca_file))
                client.ehlo()
                client.login(sender_credentials.username, sender_credentials.password)
                on_submission_started()
                refused = client.send_message(
                    message, from_addr=sender.email, to_addrs=[destination]
                )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

    def send(
        self,
        sender: dict[str, Any],
        recipient: dict[str, Any],
        subject: str,
        body: str,
        message_id: str,
        run_id: str,
        generation: int,
        *,
        in_reply_to: str | None = None,
        reply_to_actor_id: str | None = None,
        automated: bool = False,
        delivery_address: str | None = None,
        on_submission_started: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Submit once, then require a native, correlated delivery receipt.

        The callback marks the external submission boundary immediately before
        SMTP's send command. If submission outcome is uncertain, callers must
        reconcile with ``find_receipt`` before retrying; this method never retries.
        """
        sender_actor = self._actor(sender, "sender")
        recipient_actor = self._actor(recipient, "recipient")
        reply_to_actor = None
        if reply_to_actor_id is not None:
            reply_to_actor = self._actors.get(reply_to_actor_id)
            if reply_to_actor is None:
                raise PermissionError("reply-to actor is not in the configured actor directory")
        context = _SendContext(
            subject=subject,
            message_id=message_id,
            run_id=run_id,
            generation=generation,
            in_reply_to=in_reply_to,
            automated=automated,
        )
        sender_credentials = None
        if not self._is_viv(sender_actor):
            sender_credentials = self._credentials_for(sender_actor)
        if not self._is_viv(recipient_actor):
            self._credentials_for(recipient_actor)
        if not isinstance(body, str):
            raise TypeError("body must be text")
        if delivery_address is not None and self.config.delivery_aliases.get(delivery_address) != recipient_actor.actor_id:
            raise PermissionError("delivery address is not a configured alias of the recipient mailbox")

        message = EmailMessage()
        message["From"] = sender_actor.email
        message["To"] = delivery_address or recipient_actor.email
        if reply_to_actor is not None:
            message["Reply-To"] = reply_to_actor.email
        message["Subject"] = context.subject
        message["Message-ID"] = context.message_id
        message["X-TAC-Run-ID"] = context.run_id
        message["X-TAC-Generation"] = str(context.generation)
        if context.automated:
            message["Auto-Submitted"] = "auto-replied"
        if context.in_reply_to:
            message["In-Reply-To"] = context.in_reply_to
            message["References"] = context.in_reply_to
        message.set_content(body)

        employee_id = self._employee_id(sender_actor)
        if employee_id is not None:
            webmail = self._employee_webmail()
            if sender_credentials is None:
                raise EmployeeBrowserUnavailable("employee webmail credentials are unavailable")
            reply_source = None
            source_verifier = None
            if context.in_reply_to is not None:
                reply_source = self._reply_source(sender_actor, context.in_reply_to)
                if not reply_source:
                    raise MailReplySourceUnavailable(
                        "employee reply has no exact source message in the configured inbox"
                    )
                source_verifier = lambda: self.verify_reply_source(
                    {"actor_id": sender_actor.actor_id, "email": sender_actor.email},
                    reply_source,
                )
            webmail.send(
                employee_id,
                sender_actor.email,
                sender_credentials.username,
                sender_credentials.password,
                delivery_address or recipient_actor.email,
                context.subject,
                body,
                context.message_id,
                context.run_id,
                context.generation,
                in_reply_to=context.in_reply_to,
                reply_message_url=(reply_source["webmail_url"] if reply_source else None),
                verify_reply_source=source_verifier,
                reply_to=reply_to_actor.email if reply_to_actor is not None else None,
                automated=context.automated,
                on_submission_started=on_submission_started,
            )
        elif delivery_address is None:
            self._smtp_send(sender_actor, recipient_actor, message, on_submission_started)
        else:
            self._smtp_send(sender_actor, recipient_actor, message, on_submission_started,
                            delivery_address=delivery_address)
        receipt = self.find_receipt(
            recipient_actor,
            context.message_id,
            context.subject,
            context.run_id,
            context.generation,
        )
        if receipt is None:
            raise MailDeliveryUnconfirmed(
                "SMTP submission completed but no matching recipient receipt was confirmed"
            )
        return {
            **receipt,
            "sender_actor_id": sender_actor.actor_id,
            "recipient_actor_id": recipient_actor.actor_id,
        }

    def find_receipt(
        self,
        recipient: dict[str, Any],
        message_id: str,
        subject: str,
        run_id: str,
        generation: int,
    ) -> dict[str, Any] | None:
        """Reconcile an earlier submission using the recipient's native provider."""
        recipient_actor = self._actor(recipient, "recipient")
        context = _ReceiptContext(
            subject=subject,
            message_id=message_id,
            run_id=run_id,
            generation=generation,
        )
        if self._is_viv(recipient_actor):
            fixture = {
                "subject": context.subject,
                "smtp4dev_base_url": self.config.smtp4dev_api_base_url,
                "smtp4dev_mailbox_name": self.config.smtp4dev_mailbox_name,
                "employee_mailbox": {"tls_ca_file": self.config.tls_ca_file},
            }
            receipt = self._transport.confirm_smtp4dev(fixture, context.message_id)
            if receipt is None:
                return None
            headers = self._smtp4dev_headers(receipt["mailbox_uid"])
            self._verify_headers(headers, context)
        else:
            credentials = self._credentials_for(recipient_actor)
            fixture = {
                "confirm_timeout_seconds": self.config.confirm_timeout_seconds,
                "employee_mailbox": {
                    "imap_host": self.config.mailu_imap_host,
                    "imap_port": self.config.mailu_imap_port,
                    "webmail_url": self.config.webmail_url,
                    "tls_ca_file": self.config.tls_ca_file,
                },
            }
            receipt = self._transport.confirm_message(
                fixture,
                credentials.model_dump(),
                context.message_id,
            )
            if receipt is None:
                return None
            headers = self._mailu_headers(receipt["mailbox_uid"], credentials)
            self._verify_headers(headers, context)
            if headers["uidvalidity"] != str(receipt["uidvalidity"]):
                raise MailReceiptMismatch("Mailu UIDVALIDITY changed during receipt confirmation")

        return {
            **receipt,
            "message_id": context.message_id,
            "subject": context.subject,
            "run_id": context.run_id,
            "generation": context.generation,
            "recipient_actor_id": recipient_actor.actor_id,
        }

    def inspect_issue_notification(
        self,
        recipient: dict[str, Any],
        exact_native_message_id: str,
    ) -> dict[str, Any] | None:
        """Inspect a tracked notification's native IMAP receipt without reading it.

        Search only the supplied Message-ID across selectable folders. The
        receipt time comes from IMAP INTERNALDATE; FLAGS supplies the current
        Seen state. No body, marking, or mailbox-wide unread scan is performed.
        """
        recipient_actor = self._actor(recipient, "recipient")
        if self._is_viv(recipient_actor):
            raise ValueError("Viv's smtp4dev inbox is not a Mailu IMAP mailbox")
        context = _IssueNotificationContext(message_id=exact_native_message_id)
        credentials = self._credentials_for(recipient_actor)
        selected_matches: list[dict[str, Any]] = []
        seen_any = False
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            # IMAPClient otherwise strips the server timezone and converts
            # INTERNALDATE to local naive time. Retain the authoritative instant.
            client.normalise_times = False
            client.login(credentials.username, credentials.password)
            selectable_folders = self._selectable_folders(client)
            for folder in selectable_folders:
                selection = client.select_folder(folder, readonly=True)
                uidvalidity = self._uidvalidity(selection)
                uids = client.search(["HEADER", "Message-ID", context.message_id])
                for uid_value in sorted(uids, key=int):
                    uid = int(uid_value)
                    fetched = client.fetch(
                        [uid],
                        [
                            b"FLAGS",
                            b"INTERNALDATE",
                            b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO)]",
                        ],
                    )
                    record = fetched.get(uid)
                    if not isinstance(record, dict):
                        raise MailReceiptMismatch("Mailu exact-message search returned no fetch metadata")
                    raw_headers = record.get(
                        b"BODY[HEADER.FIELDS (MESSAGE-ID TO)]",
                        record.get(b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO)]"),
                    )
                    parsed = email.message_from_bytes(raw_headers or b"", policy=email.policy.default)
                    actual_message_id = _single_header(
                        {"message-id": parsed.get_all("Message-ID", [])}, "message-id"
                    )
                    if actual_message_id != context.message_id:
                        raise MailReceiptMismatch("Mailu exact-message search returned a different Message-ID")
                    recipients = {
                        address.casefold()
                        for _display, address in getaddresses(parsed.get_all("To", []))
                        if address
                    }
                    if recipient_actor.email.casefold() not in recipients:
                        raise MailReceiptMismatch("Mailu exact-message recipient does not match the configured actor")
                    internal_date = record.get(b"INTERNALDATE")
                    if not isinstance(internal_date, datetime) or internal_date.tzinfo is None:
                        raise MailReceiptMismatch("Mailu exact-message receipt has no authoritative INTERNALDATE")
                    flags = record.get(b"FLAGS")
                    if not isinstance(flags, (list, tuple, set, frozenset)):
                        raise MailReceiptMismatch("Mailu exact-message receipt has invalid FLAGS metadata")
                    seen = any(_flag_text(flag).casefold() == r"\seen" for flag in flags)
                    seen_any = seen_any or seen
                    received_at = internal_date.astimezone(timezone.utc).isoformat()
                    selected_matches.append({
                        "message_id": context.message_id,
                        "received_at": received_at,
                        "seen": seen,
                        "mailbox_uid": str(uid),
                        "uidvalidity": uidvalidity,
                        "folder": folder,
                        "reference": f"{folder} UID {uid} UIDVALIDITY {uidvalidity}",
                    })
        if not selected_matches:
            return None
        selected_matches.sort(key=lambda item: (
            item["received_at"], item["folder"].casefold(), int(item["mailbox_uid"]),
        ))
        earliest = selected_matches[0]
        return {**earliest, "seen": seen_any}

    @staticmethod
    def _selectable_folders(client: IMAPClient) -> list[str]:
        folders: set[str] = set()
        for flags, _delimiter, mailbox in client.list_folders():
            normalized_flags = {
                _flag_text(flag).casefold() for flag in (flags or [])
            }
            if r"\noselect" in normalized_flags:
                continue
            folder = mailbox.decode("utf-8") if isinstance(mailbox, bytes) else str(mailbox)
            if folder:
                folders.add(folder)
        if not folders:
            raise RuntimeError("Mailu returned no selectable folders for the configured actor")
        return sorted(folders, key=lambda value: (value.casefold(), value))

    @staticmethod
    def _uidvalidity(selection: dict[Any, Any]) -> str:
        value = selection.get(b"UIDVALIDITY", selection.get("UIDVALIDITY"))
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
            raise MailReceiptMismatch("Mailu folder selection has no valid UIDVALIDITY")
        return str(value)

    def _mailu_headers(
        self, mailbox_uid: str, credentials: _Credentials
    ) -> dict[str, str]:
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.login(credentials.username, credentials.password)
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY", "unknown"))
            if isinstance(uidvalidity, bytes):
                uidvalidity = uidvalidity.decode("ascii")
            fetched = client.fetch(
                [int(mailbox_uid)],
                [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT X-TAC-RUN-ID X-TAC-GENERATION)]"],
            )
            raw = fetched[int(mailbox_uid)].get(
                b"BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT X-TAC-RUN-ID X-TAC-GENERATION)]", b""
            )
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        header_values = {
            name: parsed.get_all(name, [])
            for name in ("Message-ID", "Subject", "X-TAC-Run-ID", "X-TAC-Generation")
        }
        return {
            "message_id": _single_header(header_values, "Message-ID"),
            "subject": _single_header(header_values, "Subject"),
            "run_id": _single_header(header_values, "X-TAC-Run-ID"),
            "generation": _single_header(header_values, "X-TAC-Generation"),
            "uidvalidity": str(uidvalidity),
        }

    def _smtp4dev_headers(self, message_uuid: str) -> dict[str, str]:
        detail_url = urljoin(
            self.config.smtp4dev_api_base_url.rstrip("/") + "/",
            f"api/Messages/{message_uuid}",
        )
        response = requests.get(detail_url, timeout=self.config.smtp_timeout_seconds)
        response.raise_for_status()
        headers: dict[str, list[str]] = {}
        for item in response.json().get("headers", []):
            name = str(item.get("name", "")).casefold()
            headers.setdefault(name, []).append(str(item.get("value", "")).strip())
        return {
            "message_id": _single_header(headers, "message-id"),
            "subject": _single_header(headers, "subject"),
            "run_id": _single_header(headers, "x-tac-run-id"),
            "generation": _single_header(headers, "x-tac-generation"),
        }

    @staticmethod
    def _verify_headers(headers: dict[str, str], expected: _ReceiptContext) -> None:
        checks = {
            "message_id": expected.message_id,
            "subject": expected.subject,
            "run_id": expected.run_id,
            "generation": str(expected.generation),
        }
        if any(headers.get(key) != value for key, value in checks.items()):
            raise MailReceiptMismatch(
                "provider returned a message with mismatched correlation headers"
            )

    def poll_registered_replies(
        self,
        mailbox_actor: dict[str, Any],
        root_message_ids: set[str],
        after_uid: int,
        expected_sender_actor_ids: set[str],
        *,
        expected_uidvalidity: str | None = None,
        require_authenticated_submission: bool = False,
    ) -> list[dict[str, Any]]:
        """Read only delivered replies to registered message roots in one Mailu INBOX.

        IMAP header searches narrow the candidate set before any body is fetched.
        Every candidate must reference one of the exact registered Message-IDs,
        be addressed to this configured mailbox, and have a configured sender
        address explicitly allowed by the registered case. When authenticated
        submission is required, one RFC-parsed Authentication-Results header
        from the configured Rspamd authserv-id must map its passed SMTP user to
        that same actor. Messages are fetched with BODY.PEEK and the mailbox
        remains read-only.
        """
        mailbox = self._actor(mailbox_actor, "mailbox")
        if self._is_viv(mailbox):
            raise ValueError("Viv's smtp4dev inbox is not a Mailu IMAP mailbox")
        roots = {_IssueNotificationContext(message_id=value).message_id for value in root_message_ids}
        if not roots:
            return []
        if isinstance(after_uid, bool) or not isinstance(after_uid, int) or after_uid < 0:
            raise ValueError("after_uid must be a nonnegative integer")
        allowed_senders = [self._actors[actor_id] for actor_id in expected_sender_actor_ids
                           if actor_id in self._actors]
        if len(allowed_senders) != len(expected_sender_actor_ids) or not allowed_senders:
            raise PermissionError("registered reply sender is not in the configured actor directory")
        credentials = self._credentials_for(mailbox)
        matches: dict[int, str] = {}
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.login(credentials.username, credentials.password)
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = self._uidvalidity(selected)
            if expected_uidvalidity is not None and expected_uidvalidity != uidvalidity:
                # A replaced Mailu folder restarts its UID namespace. Restart
                # this exact-thread search from the beginning; durable Message-ID
                # receipts still provide deduplication across the reset.
                after_uid = 0
            for root_id in roots:
                for header_name in ("In-Reply-To", "References"):
                    for uid_value in client.search(["HEADER", header_name, root_id]):
                        uid = int(uid_value)
                        if uid > after_uid:
                            matches[uid] = uidvalidity
            if not matches:
                return []
            ordered_uids = sorted(matches)
            auth_header_fields = (
                b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO "
                b"REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]"
            )
            headers_by_uid = client.fetch(
                ordered_uids,
                [auth_header_fields],
            )
            accepted: list[dict[str, Any]] = []
            for uid in ordered_uids:
                record = headers_by_uid.get(uid)
                if not isinstance(record, dict):
                    raise MailReceiptMismatch("Mailu registered reply search returned incomplete metadata")
                raw_headers = record.get(
                    b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]",
                    record.get(b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]"),
                )
                parsed = email.message_from_bytes(raw_headers or b"", policy=email.policy.default)
                if not (_referenced_message_ids(parsed) & roots):
                    continue
                message_id = _single_header({"message-id": parsed.get_all("Message-ID", [])}, "message-id")
                if not message_id:
                    raise MailReceiptMismatch("Mailu registered reply has no unique Message-ID")
                recipient_addresses = {address.casefold() for _, address in getaddresses(parsed.get_all("To", [])) if address}
                if mailbox.email.casefold() not in recipient_addresses:
                    continue
                sender_addresses = [address.casefold() for _, address in getaddresses(parsed.get_all("From", [])) if address]
                if len(sender_addresses) != 1:
                    continue
                sender = next((actor for actor in allowed_senders if actor.email.casefold() in sender_addresses), None)
                if sender is None:
                    continue
                authenticated_actor = _mailu_authenticated_actor(
                    parsed.get_all("Authentication-Results", []),
                    self.config.mailu_smtp_host,
                    self._authenticated_actor_names(allowed_senders),
                )
                if require_authenticated_submission and authenticated_actor != sender.actor_id:
                    continue
                auto_submitted = str(parsed.get("Auto-Submitted", "no")).strip().casefold()
                if auto_submitted not in {"", "no"}:
                    continue
                fetched = client.fetch([uid], [b"BODY.PEEK[]", b"INTERNALDATE"])
                full_record = fetched.get(uid)
                if not isinstance(full_record, dict):
                    raise MailReceiptMismatch("Mailu registered reply body could not be fetched")
                raw = full_record.get(b"BODY[]", full_record.get(b"BODY.PEEK[]", b""))
                if not raw:
                    raise MailReceiptMismatch("Mailu registered reply has an empty body")
                full_message = email.message_from_bytes(raw, policy=email.policy.default)
                if str(full_message.get("Message-ID", "")).strip() != message_id:
                    raise MailReceiptMismatch("Mailu registered reply changed during fetch")
                full_sender_addresses = [address.casefold() for _, address in getaddresses(full_message.get_all("From", [])) if address]
                full_recipient_addresses = {address.casefold() for _, address in getaddresses(full_message.get_all("To", [])) if address}
                if (
                    not (_referenced_message_ids(full_message) & roots)
                    or full_sender_addresses != sender_addresses
                    or mailbox.email.casefold() not in full_recipient_addresses
                ):
                    raise MailReceiptMismatch("Mailu registered reply headers changed during fetch")
                full_authenticated_actor = _mailu_authenticated_actor(
                    full_message.get_all("Authentication-Results", []),
                    self.config.mailu_smtp_host,
                    self._authenticated_actor_names(allowed_senders),
                )
                if (
                    full_authenticated_actor != authenticated_actor
                    or full_message.get_all("Authentication-Results", [])
                    != parsed.get_all("Authentication-Results", [])
                ):
                    raise MailReceiptMismatch("Mailu registered reply authentication headers changed during fetch")
                body_part = full_message.get_body(preferencelist=("plain",))
                if body_part is None:
                    raise MailReceiptMismatch("Mailu registered reply has no text/plain body")
                received_at = full_record.get(b"INTERNALDATE")
                if not isinstance(received_at, datetime) or received_at.tzinfo is None:
                    raise MailReceiptMismatch("Mailu registered reply has no authoritative INTERNALDATE")
                accepted.append({
                    "mailbox_uid": str(uid),
                    "uidvalidity": matches[uid],
                    "reference": f"INBOX UID {uid} UIDVALIDITY {matches[uid]}",
                    "webmail_url": _roundcube_message_url(self.config.webmail_url, str(uid)),
                    "message_id": message_id,
                    "subject": str(full_message.get("Subject", "")),
                    "sender_actor_id": sender.actor_id,
                    "recipient_actor_id": mailbox.actor_id,
                    "authenticated_sender_actor_id": authenticated_actor,
                    "in_reply_to": str(full_message.get("In-Reply-To", "")),
                    "references": str(full_message.get("References", "")),
                    "received_at": received_at.astimezone(timezone.utc).isoformat(),
                    "body": body_part.get_content(),
                })
            return accepted

    def _authenticated_actor_names(self, actors: list[_ActorRecord]) -> dict[str, str]:
        identities = {}
        for actor in actors:
            identities[actor.email.casefold()] = actor.actor_id
            credentials = self._credentials.get(actor.email.casefold())
            if credentials is not None:
                identities[credentials.username.casefold()] = actor.actor_id
        return identities

    def poll(
        self,
        actor: dict[str, Any],
        after_uid: int,
        run_id: str,
        generation: int,
        known_message_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Read this actor's matching run/generation messages without marking read.

        The `From` header is returned as untrusted message metadata only. It is
        never used to infer identity, approval, payment authority, or permission.
        Message bodies are returned to the caller but not logged or persisted here.
        """
        recipient_actor = self._actor(actor, "mailbox")
        if self._is_viv(recipient_actor):
            raise ValueError("Viv's smtp4dev inbox is not a Mailu IMAP mailbox")
        credentials = self._credentials_for(recipient_actor)
        context = _PollContext(
            after_uid=after_uid,
            run_id=run_id,
            generation=generation,
            known_message_ids=known_message_ids,
        )
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.login(credentials.username, credentials.password)
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY", "unknown"))
            if isinstance(uidvalidity, bytes):
                uidvalidity = uidvalidity.decode("ascii")
            return self._poll_selected(client, uidvalidity, context)

    def wait_for_mail(
        self,
        actor: dict[str, Any],
        after_uid: int,
        run_id: str,
        generation: int,
        known_message_ids: set[str],
        timeout: float = 30,
    ) -> list[dict[str, Any]]:
        """Wait for matching mail using IMAP IDLE events and a bounded timeout."""
        recipient_actor = self._actor(actor, "mailbox")
        if self._is_viv(recipient_actor):
            raise ValueError("Viv's smtp4dev inbox is not a Mailu IMAP mailbox")
        credentials = self._credentials_for(recipient_actor)
        context = _WaitContext(
            after_uid=after_uid,
            run_id=run_id,
            generation=generation,
            known_message_ids=known_message_ids,
            timeout=timeout,
        )
        deadline = time.monotonic() + context.timeout
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=self.config.smtp_timeout_seconds,
        ) as client:
            client.login(credentials.username, credentials.password)
            selected = client.select_folder("INBOX", readonly=True)
            uidvalidity = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY", "unknown"))
            if isinstance(uidvalidity, bytes):
                uidvalidity = uidvalidity.decode("ascii")
            while True:
                messages = self._poll_selected(client, uidvalidity, context)
                if messages:
                    return messages
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                client.idle()
                try:
                    client.idle_check(timeout=remaining)
                except BaseException as error:
                    try:
                        client.idle_done()
                    except BaseException as cleanup_error:
                        error.add_note(
                            f"IMAP IDLE cleanup also failed: {type(cleanup_error).__name__}"
                        )
                    raise
                else:
                    client.idle_done()

    def poll_viv(
        self,
        run_id: str,
        generation: int,
        known_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Read only Viv mailbox messages correlated to this run generation.

        smtp4dev is a shared capture inbox. Its paginated message summaries are
        hydrated by the native detail and RFC822 download APIs, then filtered
        by exact TAC run headers or exact Message-ID references. Message bodies
        are returned to the caller and are never logged or cached here.
        """
        context = _VivPollContext(run_id=run_id, generation=generation, known_ids=known_ids)
        summaries = self._smtp4dev_message_summaries()
        found: list[dict[str, Any]] = []
        base_url = self.config.smtp4dev_api_base_url.rstrip("/") + "/"
        for summary in summaries:
            provider_id = summary.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                raise SMTP4DevAPIError("read message summary") from ValueError("summary id missing")
            detail = self._smtp4dev_json(f"api/Messages/{quote(provider_id, safe='')}", "read message detail")
            if detail.get("id") != provider_id:
                raise MailReceiptMismatch("smtp4dev message ID changed between list and detail")
            detail_headers = self._smtp4dev_detail_headers(detail)
            detail_message_id = _single_header(detail_headers, "message-id")
            if not detail_message_id:
                raise SMTP4DevAPIError("read message detail") from ValueError("Message-ID header missing or ambiguous")
            if provider_id in context.known_ids or detail_message_id in context.known_ids:
                continue
            if not self._viv_message_matches(detail_headers, context):
                continue

            mime_response = self._smtp4dev_get(
                f"api/Messages/{quote(provider_id, safe='')}/download", "read message MIME"
            )
            try:
                raw_mime = mime_response.content
                if not isinstance(raw_mime, bytes) or not raw_mime:
                    raise ValueError("empty MIME response")
                parsed = email.message_from_bytes(raw_mime, policy=email.policy.default)
            except Exception as error:
                raise SMTP4DevAPIError("parse message MIME", mime_response.status_code) from error

            mime_message_id = _single_header(
                {"message-id": parsed.get_all("Message-ID", [])}, "message-id"
            )
            if mime_message_id != detail_message_id:
                raise MailReceiptMismatch("smtp4dev MIME Message-ID does not match message detail")
            if not self._viv_message_matches(parsed, context):
                raise MailReceiptMismatch("smtp4dev MIME correlation does not match message detail")
            body_part = parsed.get_body(preferencelist=("plain", "html"))
            if body_part is None:
                raise SMTP4DevAPIError("read message body") from ValueError("plain-text or HTML MIME body missing")
            body = body_part.get_content()
            if not isinstance(body, str):
                raise SMTP4DevAPIError("decode message body") from TypeError("MIME body is not text")
            if not body:
                raise SMTP4DevAPIError("read message body") from ValueError("MIME body is empty")

            mailbox = quote(self.config.smtp4dev_mailbox_name, safe="")
            message_path = quote(provider_id, safe="")
            webmail_url = (
                self.config.smtp4dev_api_base_url.rstrip("/")
                + f"/#/messages/mailbox/{mailbox}/folder/INBOX/message/{message_path}"
            )
            found.append({
                "message_id": mime_message_id,
                "subject": _decoded_header(parsed.get_all("Subject", [])),
                "body": body,
                "from": _decoded_header(parsed.get_all("From", [])),
                "to": _decoded_header(parsed.get_all("To", [])),
                "in_reply_to": _decoded_header(parsed.get_all("In-Reply-To", [])),
                "references": _decoded_header(parsed.get_all("References", [])),
                "provider_id": provider_id,
                "webmail_url": webmail_url,
                "run_id": context.run_id,
                "generation": context.generation,
            })
        return found

    def wait_for_activity(self, actor: dict[str, Any], timeout: float = 30) -> bool:
        """Return whether the actor's Mailu INBOX signals activity through IMAP IDLE."""
        mailbox_actor = self._actor(actor, "mailbox")
        if self._is_viv(mailbox_actor):
            raise ValueError("Viv's smtp4dev inbox does not provide Mailu IMAP IDLE")
        credentials = self._credentials_for(mailbox_actor)
        context = _ActivityContext(timeout=timeout)
        with IMAPClient(
            self.config.mailu_imap_host,
            port=self.config.mailu_imap_port,
            ssl=True,
            ssl_context=_ssl_context(self.config.tls_ca_file),
            timeout=max(self.config.smtp_timeout_seconds, context.timeout),
        ) as client:
            client.login(credentials.username, credentials.password)
            client.select_folder("INBOX", readonly=True)
            client.idle()
            try:
                activity = client.idle_check(timeout=context.timeout)
            except BaseException as error:
                try:
                    client.idle_done()
                except BaseException as cleanup_error:
                    error.add_note(f"IMAP IDLE cleanup also failed: {type(cleanup_error).__name__}")
                raise
            client.idle_done()
        return bool(activity)

    def _smtp4dev_get(self, path: str, operation: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        url = urljoin(self.config.smtp4dev_api_base_url.rstrip("/") + "/", path)
        try:
            response = requests.get(url, params=params, timeout=self.config.smtp_timeout_seconds)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            status = error.response.status_code if isinstance(error, requests.HTTPError) and error.response else None
            raise SMTP4DevAPIError(operation, status) from error

    def _smtp4dev_json(self, path: str, operation: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._smtp4dev_get(path, operation, params=params)
        try:
            result = response.json()
        except (ValueError, requests.RequestException) as error:
            raise SMTP4DevAPIError(operation, response.status_code) from error
        if not isinstance(result, dict):
            raise SMTP4DevAPIError(operation, response.status_code) from TypeError("JSON response must be an object")
        return result

    def _smtp4dev_message_summaries(self) -> list[dict[str, Any]]:
        path = "api/Messages"
        params = {
            "mailboxName": self.config.smtp4dev_mailbox_name,
            "folderName": "INBOX",
            "page": 1,
            "pageSize": 100,
        }
        first = self._smtp4dev_json(path, "list messages", params=params)
        results = first.get("results")
        page_count = first.get("pageCount")
        if not isinstance(results, list) or isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 0:
            raise SMTP4DevAPIError("list messages", 200) from ValueError("invalid paged message response")
        summaries = list(results)
        for page in range(2, page_count + 1):
            next_params = {**params, "page": page}
            result = self._smtp4dev_json(path, "list messages", params=next_params)
            page_results = result.get("results")
            if not isinstance(page_results, list):
                raise SMTP4DevAPIError("list messages", 200) from ValueError("invalid paged message response")
            summaries.extend(page_results)
        if any(not isinstance(summary, dict) for summary in summaries):
            raise SMTP4DevAPIError("list messages", 200) from ValueError("invalid message summary")
        return summaries

    @staticmethod
    def _smtp4dev_detail_headers(detail: dict[str, Any]) -> dict[str, list[Any]]:
        headers = detail.get("headers")
        if not isinstance(headers, list):
            raise SMTP4DevAPIError("read message detail", 200) from ValueError("headers missing from detail")
        result: dict[str, list[Any]] = {}
        for item in headers:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise SMTP4DevAPIError("read message detail", 200) from ValueError("invalid header entry")
            result.setdefault(item["name"].casefold(), []).append(item.get("value", ""))
        return result

    @staticmethod
    def _viv_message_matches(headers: email.message.Message | dict[str, list[Any]], context: _VivPollContext) -> bool:
        runs = headers.get_all("X-TAC-Run-ID", []) if isinstance(headers, email.message.Message) else headers.get("x-tac-run-id", [])
        generations = headers.get_all("X-TAC-Generation", []) if isinstance(headers, email.message.Message) else headers.get("x-tac-generation", [])
        if runs or generations:
            return (
                len(runs) == 1 and len(generations) == 1
                and str(runs[0]).strip() == context.run_id
                and str(generations[0]).strip() == str(context.generation)
            )
        return bool(_referenced_message_ids(headers) & context.known_ids)

    def _poll_selected(
        self,
        client: IMAPClient,
        uidvalidity: Any,
        context: _PollContext,
    ) -> list[dict[str, Any]]:
        uids = client.search(["UID", f"{context.after_uid + 1}:*"])
        if not uids:
            return []
        fetched = client.fetch(uids, [b"BODY.PEEK[]"])
        messages: list[dict[str, Any]] = []
        for uid in sorted(uids):
            raw = fetched[uid].get(b"BODY[]", b"")
            parsed = email.message_from_bytes(raw, policy=email.policy.default)
            run_headers = parsed.get_all("X-TAC-Run-ID", [])
            generation_headers = parsed.get_all("X-TAC-Generation", [])
            if run_headers or generation_headers:
                is_current_run = (
                    len(run_headers) == 1
                    and len(generation_headers) == 1
                    and str(run_headers[0]) == context.run_id
                    and str(generation_headers[0]) == str(context.generation)
                )
            else:
                is_current_run = bool(
                    _referenced_message_ids(parsed) & context.known_message_ids
                )
            if not is_current_run:
                continue
            body_part = parsed.get_body(preferencelist=("plain",))
            if body_part is None:
                raise RuntimeError("matching simulation email has no text/plain body")
            messages.append(
                {
                    "mailbox_uid": str(uid),
                    "uidvalidity": str(uidvalidity),
                    "reference": f"INBOX UID {uid}",
                    "webmail_url": _roundcube_message_url(self.config.webmail_url, str(uid)),
                    "message_id": str(parsed.get("Message-ID", "")),
                    "subject": str(parsed.get("Subject", "")),
                    "from_header": str(parsed.get("From", "")),
                    "to_header": str(parsed.get("To", "")),
                    "in_reply_to": str(parsed.get("In-Reply-To", "")),
                    "references": str(parsed.get("References", "")),
                    "auto_submitted": str(parsed.get("Auto-Submitted", "no")),
                    "body": body_part.get_content(),
                    "run_id": context.run_id,
                    "generation": context.generation,
                }
            )
        return messages
