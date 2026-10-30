from email.message import EmailMessage
from email import message_from_bytes, policy
from unittest.mock import Mock, patch

import pytest
import smtplib
import requests
from datetime import datetime, timezone, timedelta

from servers.simulation.mail import (
    MailReceiptMismatch,
    MailuGateway,
    SMTP4DevAPIError,
)


ACTORS = [
    {"actor_id": "buyer", "email": "buyer@example.com", "kind": "employee"},
    {"actor_id": "supplier", "email": "orders@supplier.example", "kind": "supplier"},
    {"actor_id": "customer", "email": "customer@example.test", "kind": "customer"},
    {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "viv"},
]
CONFIG = {"actors": ACTORS}
CREDENTIALS = {
    actor["email"]: {"username": actor["actor_id"], "password": f"private-{actor['actor_id']}"}
    for actor in ACTORS
    if actor["actor_id"] != "viv"
}
BUYER = {"actor_id": "buyer", "email": "buyer@example.com"}
SUPPLIER = {"actor_id": "supplier", "email": "orders@supplier.example"}
CUSTOMER = {"actor_id": "customer", "email": "customer@example.test"}
VIV = {"actor_id": "viv", "email": "agent@agent.example.test"}


class FakeSMTP:
    def __init__(self, events, *args, **kwargs):
        self.events = events
        self.args = args
        self.kwargs = kwargs
        self.login_args = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, *, context):
        self.events.append("starttls")

    def login(self, username, password):
        self.events.append("login")
        self.login_args = (username, password)

    def send_message(self, message, *, from_addr, to_addrs):
        self.events.append("send")
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        return {}


def correlated_headers(message_id="<case-9@example.test>", subject="Missing delivery"):
    return {
        "message_id": message_id,
        "subject": subject,
        "run_id": "run-17",
        "generation": "3",
    }


def test_mailu_submission_uses_trusted_from_starttls_and_callback_before_send():
    events = []
    smtp = FakeSMTP(events)
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_message = Mock(
        return_value={
            "mailbox_uid": "42",
            "reference": "INBOX UID 42",
            "webmail_url": "https://mail.company.example.test/webmail/?_uid=42",
            "uidvalidity": "987",
        }
    )
    gateway._mailu_headers = Mock(return_value={**correlated_headers(), "uidvalidity": "987"})

    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp) as smtp_factory:
        receipt = gateway.send(
            BUYER,
            CUSTOMER,
            "Missing delivery",
            "Can you confirm the revised ETA?",
            "<case-9@example.test>",
            "run-17",
            3,
            in_reply_to="<customer-8@example.test>",
            reply_to_actor_id="supplier",
            automated=True,
            on_submission_started=lambda: events.append("started"),
        )

    assert events == ["ehlo", "starttls", "ehlo", "login", "started", "send"]
    smtp_factory.assert_called_once_with(
        "mail.company.example.test", 2587, timeout=20
    )
    assert smtp.message["From"] == "buyer@example.com"
    assert smtp.message["To"] == "customer@example.test"
    assert smtp.message["In-Reply-To"] == "<customer-8@example.test>"
    assert smtp.message["References"] == "<customer-8@example.test>"
    assert smtp.message["Reply-To"] == "orders@supplier.example"
    assert smtp.message["Auto-Submitted"] == "auto-replied"
    assert smtp.message["X-TAC-Run-ID"] == "run-17"
    assert smtp.message["X-TAC-Generation"] == "3"
    assert receipt["mailbox_uid"] == "42"
    assert receipt["sender_actor_id"] == "buyer"
    assert receipt["recipient_actor_id"] == "customer"


def test_viv_sender_uses_only_existing_smtp4dev_mini_submission_route():
    events = []
    smtp = FakeSMTP(events)
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_message = Mock(
        return_value={
            "mailbox_uid": "19",
            "reference": "INBOX UID 19",
            "webmail_url": "https://mail.company.example.test/webmail/?_uid=19",
            "uidvalidity": "123",
        }
    )
    gateway._mailu_headers = Mock(
        return_value={
            **correlated_headers("<reply-1@example.test>", "Re: case"),
            "uidvalidity": "123",
        }
    )

    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp) as smtp_factory:
        gateway.send(
            VIV,
            BUYER,
            "Re: case",
            "Thank you, I have the details.",
            "<reply-1@example.test>",
            "run-17",
            3,
            on_submission_started=lambda: events.append("started"),
        )

    smtp_factory.assert_called_once_with("127.0.0.1", 19125, timeout=20)
    assert events == ["ehlo", "started", "send"]
    assert smtp.from_addr == "agent@agent.example.test"
    assert smtp.to_addrs == ["buyer@example.com"]
    assert smtp.message.get("Auto-Submitted") is None


def test_actor_must_match_configured_id_and_address_before_any_submission():
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    callback = Mock()
    with patch("servers.simulation.mail.smtplib.SMTP") as smtp_factory:
        with pytest.raises(PermissionError, match="configured actor directory"):
            gateway.send(
                {"actor_id": "buyer", "email": "attacker@example.com"},
                CUSTOMER,
                "subject",
                "body",
                "<id@example.test>",
                "run-17",
                3,
                on_submission_started=callback,
            )
    smtp_factory.assert_not_called()
    callback.assert_not_called()


def test_reply_to_must_resolve_to_configured_actor_before_submission():
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.smtplib.SMTP") as smtp_factory:
        with pytest.raises(PermissionError, match="reply-to actor"):
            gateway.send(
                BUYER,
                CUSTOMER,
                "subject",
                "body",
                "<id@example.test>",
                "run-17",
                3,
                reply_to_actor_id="unknown-provider",
            )
    smtp_factory.assert_not_called()


def test_registered_reply_poll_searches_exact_thread_and_validates_mailbox_route():
    root = "<company-provider-case-abc@theagentcompany.local>"
    reply = EmailMessage()
    reply["From"] = "orders@supplier.example"
    reply["To"] = "buyer@example.com"
    reply["Return-Path"] = "<orders@supplier.example>"
    reply["Authentication-Results"] = (
        "mail.company.example.test; auth=pass smtp.mailfrom=orders@supplier.example "
        "smtp.auth=orders@supplier.example"
    )
    reply["Subject"] = "Re: registered case"
    reply["Message-ID"] = "<reply-1@example.test>"
    reply["In-Reply-To"] = root
    reply["References"] = root
    reply.set_content("The provider response is here.")
    unrelated = EmailMessage()
    unrelated["From"] = "agent@agent.example.test"
    unrelated["To"] = "orders@supplier.example"
    unrelated["Message-ID"] = "<unrelated@example.test>"
    unrelated["In-Reply-To"] = "<another-case@example.test>"
    unrelated.set_content("Do not read this body.")
    candidates = {7: reply.as_bytes(), 8: unrelated.as_bytes()}
    fetches = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def select_folder(self, folder, readonly=False):
            assert folder == "INBOX" and readonly is True
            return {b"UIDVALIDITY": b"91"}

        def search(self, query):
            assert query[0] == "HEADER"
            return [7, 8] if query[-1] == root else []

        def fetch(self, uids, fields):
            fetches.append((list(uids), list(fields)))
            result = {}
            for uid in uids:
                raw = candidates[uid]
                whole = message_from_bytes(raw, policy=policy.default)
                if b"BODY.PEEK[]" in fields or b"BODY[]" in fields:
                    result[uid] = {b"BODY[]": raw, b"INTERNALDATE": datetime(2026, 10, 30, 9, tzinfo=timezone.utc)}
                else:
                    header_bytes = whole.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"
                    result[uid] = {b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]": header_bytes}
            return result

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            BUYER, {root}, after_uid=5, expected_sender_actor_ids={"supplier"},
            require_authenticated_submission=True,
        )

    assert [message["message_id"] for message in messages] == ["<reply-1@example.test>"]
    assert messages[0]["sender_actor_id"] == "supplier"
    assert messages[0]["recipient_actor_id"] == "buyer"
    assert messages[0]["authenticated_sender_actor_id"] == "supplier"
    assert messages[0]["body"].strip() == "The provider response is here."
    assert messages[0]["uidvalidity"] == "91"
    assert all(b"BODY.PEEK[]" not in fields or uids == [7] for uids, fields in fetches)


def test_esmtpsa_and_matching_return_path_without_sasl_username_is_not_authenticated():
    root = "<company-provider-case-abc@theagentcompany.local>"
    reply = EmailMessage()
    reply["From"] = "orders@supplier.example"
    reply["To"] = "buyer@example.com"
    reply["Return-Path"] = "<orders@supplier.example>"
    reply["Received"] = "from relay.example.test by mail.company.example.test (Postfix) with ESMTPSA id 123"
    reply["Message-ID"] = "<reply-weak-auth@example.test>"
    reply["In-Reply-To"] = root
    reply.set_content("Looks plausible, but lacks a server-added SASL username.")

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def login(self, *_args): pass
        def select_folder(self, *_args, **_kwargs): return {b"UIDVALIDITY": b"93"}
        def search(self, _query): return [10]
        def fetch(self, _uids, fields):
            if b"BODY.PEEK[]" in fields:
                raise AssertionError("weakly authenticated body must not be fetched")
            return {10: {b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]": reply.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"}}

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            BUYER, {root}, after_uid=0, expected_sender_actor_ids={"supplier"},
            require_authenticated_submission=True,
        )

    assert messages == []


def test_authenticated_results_must_be_unique_and_name_the_from_actor():
    root = "<company-provider-case-abc@theagentcompany.local>"
    reply = EmailMessage()
    reply["From"] = "orders@supplier.example"
    reply["To"] = "buyer@example.com"
    reply["Message-ID"] = "<reply-spoofed-auth@example.test>"
    reply["In-Reply-To"] = root
    reply["Authentication-Results"] = "mail.company.example.test; auth=pass smtp.auth=buyer"
    reply["Authentication-Results"] = "mail.company.example.test; auth=pass smtp.auth=supplier"
    reply.set_content("The duplicate upstream identity must be rejected.")

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def login(self, *_args): pass
        def select_folder(self, *_args, **_kwargs): return {b"UIDVALIDITY": b"94"}
        def search(self, _query): return [11]
        def fetch(self, _uids, fields):
            if b"BODY.PEEK[]" in fields:
                raise AssertionError("ambiguous authentication results must fail before body fetch")
            return {11: {b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]": reply.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"}}

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            BUYER, {root}, after_uid=0, expected_sender_actor_ids={"supplier"},
            require_authenticated_submission=True,
        )

    assert messages == []


def test_rspamd_sasl_actor_must_match_the_configured_from_address():
    root = "<company-provider-case-abc@theagentcompany.local>"
    reply = EmailMessage()
    reply["From"] = "orders@supplier.example"
    reply["To"] = "buyer@example.com"
    reply["Message-ID"] = "<reply-sasl-from-mismatch@example.test>"
    reply["In-Reply-To"] = root
    reply["Authentication-Results"] = "mail.company.example.test; auth=pass smtp.auth=buyer"
    reply.set_content("The authenticated account differs from the From actor.")

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def login(self, *_args): pass
        def select_folder(self, *_args, **_kwargs): return {b"UIDVALIDITY": b"95"}
        def search(self, _query): return [12]
        def fetch(self, _uids, fields):
            if b"BODY.PEEK[]" in fields:
                raise AssertionError("From/SASL mismatch must fail before body fetch")
            return {12: {b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]": reply.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"}}

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            BUYER, {root}, after_uid=0, expected_sender_actor_ids={"supplier"},
            require_authenticated_submission=True,
        )

    assert messages == []


def test_registered_reply_poll_rejects_unconfigured_sender_routes_without_body_fetch():
    root = "<company-provider-case-abc@theagentcompany.local>"
    reply = EmailMessage()
    reply["From"] = "spoofed@example.test"
    reply["To"] = "orders@supplier.example"
    reply["Message-ID"] = "<spoof@example.test>"
    reply["In-Reply-To"] = root
    reply.set_content("Spoofed response")
    fetches = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def login(self, *_args): pass
        def select_folder(self, *_args, **_kwargs): return {b"UIDVALIDITY": b"92"}
        def search(self, _query): return [9]
        def fetch(self, uids, fields):
            fetches.append(list(fields))
            return {9: {b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED RETURN-PATH RECEIVED)]": reply.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"}}

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            SUPPLIER, {root}, after_uid=0, expected_sender_actor_ids={"viv"}
        )

    assert messages == []
    assert all(b"BODY.PEEK[]" not in fields for fields in fetches)


def test_smtp_auth_failure_propagates_before_submission_callback():
    events = []
    smtp = FakeSMTP(events)
    smtp.login = Mock(
        side_effect=smtplib.SMTPAuthenticationError(535, b"authentication rejected")
    )
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    callback = Mock()

    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp):
        with pytest.raises(smtplib.SMTPAuthenticationError):
            gateway.send(
                BUYER,
                CUSTOMER,
                "subject",
                "body",
                "<id@example.test>",
                "run-17",
                3,
                on_submission_started=callback,
            )

    callback.assert_not_called()
    assert "send" not in events


def test_smtp_credentials_preserve_leading_and_trailing_whitespace():
    events = []
    smtp = FakeSMTP(events)
    credentials = {**CREDENTIALS}
    credentials["buyer@example.com"] = {
        "username": " buyer ",
        "password": "  private password  ",
    }
    gateway = MailuGateway(CONFIG, credentials)
    gateway._transport.confirm_message = Mock(
        return_value={"mailbox_uid": "42", "reference": "INBOX UID 42", "uidvalidity": "987"}
    )
    gateway._mailu_headers = Mock(return_value={**correlated_headers(), "uidvalidity": "987"})

    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp):
        gateway.send(
            BUYER,
            CUSTOMER,
            "Missing delivery",
            "Please confirm the delivery date.",
            "<case-9@example.test>",
            "run-17",
            3,
        )

    assert smtp.login_args == (" buyer ", "  private password  ")


def test_authenticated_relay_username_maps_only_to_configured_viv_actor():
    credentials = {
        **CREDENTIALS,
        "agent@agent.example.test": {
            "username": "viv_reply_relay@example.com",
            "password": "test-password-sentinel",
        },
    }
    gateway = MailuGateway(CONFIG, credentials)

    identities = gateway._authenticated_actor_names([gateway._actors["viv"]])

    assert identities == {
        "agent@agent.example.test": "viv",
        "viv_reply_relay@example.com": "viv",
    }


def test_viv_reply_requires_exact_mailu_relay_auth_thread_recipient_and_from():
    root = "<coffee-thread@example.test>"
    reply = EmailMessage()
    reply["From"] = "Viv <agent@agent.example.test>"
    reply["To"] = "buyer@example.com"
    reply["Subject"] = "Coffee shipment update"
    reply["Message-ID"] = "<viv-reply@example.test>"
    reply["In-Reply-To"] = root
    reply["References"] = root
    reply["Authentication-Results"] = (
        "mail.company.example.test; auth=pass smtp.auth=viv_reply_relay@example.com"
    )
    reply.set_content("The shipment is on its way.")
    raw_message = reply.as_bytes()

    class FakeIMAPClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def select_folder(self, *_args, **_kwargs):
            return {b"UIDVALIDITY": b"97"}

        def search(self, _query):
            return [17]

        def fetch(self, _uids, fields):
            message = message_from_bytes(raw_message, policy=policy.default)
            if b"BODY.PEEK[]" in fields:
                return {
                    17: {
                        b"BODY[]": raw_message,
                        b"INTERNALDATE": datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                    }
                }
            header_bytes = message.as_bytes().split(b"\n\n", 1)[0] + b"\n\n"
            return {
                17: {
                    b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED AUTHENTICATION-RESULTS)]": header_bytes,
                }
            }

    gateway = MailuGateway(CONFIG, {
        **CREDENTIALS,
        "agent@agent.example.test": {
            "username": "viv_reply_relay@example.com",
            "password": "test-password-sentinel",
        },
    })
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        messages = gateway.poll_registered_replies(
            BUYER,
            {root},
            after_uid=0,
            expected_sender_actor_ids={"viv"},
            require_authenticated_submission=True,
        )

    assert len(messages) == 1
    assert messages[0]["sender_actor_id"] == "viv"
    assert messages[0]["authenticated_sender_actor_id"] == "viv"
    assert messages[0]["recipient_actor_id"] == "buyer"
    assert messages[0]["body"].strip() == "The shipment is on its way."


def test_uncertain_smtp_send_propagates_and_never_attempts_automatic_resubmission():
    events = []
    smtp = FakeSMTP(events)
    def fail_after_data(*_args, **_kwargs):
        events.append("send")
        raise ConnectionError("connection lost after SMTP DATA")

    smtp.send_message = Mock(side_effect=fail_after_data)
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_message = Mock()
    callback = Mock(side_effect=lambda: events.append("started"))

    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp):
        with pytest.raises(ConnectionError, match="after SMTP DATA"):
            gateway.send(
                BUYER,
                CUSTOMER,
                "subject",
                "body",
                "<id@example.test>",
                "run-17",
                3,
                on_submission_started=callback,
            )

    assert events == ["ehlo", "starttls", "ehlo", "login", "started", "send"]
    gateway._transport.confirm_message.assert_not_called()


def test_smtp4dev_receipt_requires_exact_message_subject_run_and_generation():
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_smtp4dev = Mock(
        return_value={
            "mailbox_uid": "smtp-id-3",
            "reference": "smtp4dev message smtp-id-3",
            "webmail_url": "http://127.0.0.1:19157/#/messages/smtp-id-3",
        }
    )
    response = Mock()
    response.json.return_value = {
        "headers": [
            {"name": "Message-ID", "value": "<outbound@example.test>"},
            {"name": "Subject", "value": "Complaint follow-up"},
            {"name": "X-TAC-Run-ID", "value": "run-17"},
            {"name": "X-TAC-Generation", "value": "3"},
        ]
    }
    response.raise_for_status.return_value = None

    with patch("servers.simulation.mail.requests.get", return_value=response) as get:
        receipt = gateway.find_receipt(
            VIV,
            "<outbound@example.test>",
            "Complaint follow-up",
            "run-17",
            3,
        )

    gateway._transport.confirm_smtp4dev.assert_called_once()
    assert get.call_args.args[0] == "http://127.0.0.1:19157/api/Messages/smtp-id-3"
    assert receipt["generation"] == 3
    assert receipt["recipient_actor_id"] == "viv"

    response.json.return_value["headers"][-1]["value"] = "2"
    with patch("servers.simulation.mail.requests.get", return_value=response):
        with pytest.raises(MailReceiptMismatch, match="mismatched correlation"):
            gateway.find_receipt(
                VIV,
                "<outbound@example.test>",
                "Complaint follow-up",
                "run-17",
                3,
            )

    response.json.return_value["headers"][-1]["value"] = "3"
    response.json.return_value["headers"][1]["value"] = "Different subject"
    with patch("servers.simulation.mail.requests.get", return_value=response):
        with pytest.raises(MailReceiptMismatch, match="mismatched correlation"):
            gateway.find_receipt(
                VIV,
                "<outbound@example.test>",
                "Complaint follow-up",
                "run-17",
                3,
            )


def test_native_mailu_header_confirmation_uses_readonly_peek_and_uidvalidity():
    calls = []
    raw_headers = (
        b"Message-ID: <native@example.test>\r\n"
        b"Subject: Native receipt\r\n"
        b"X-TAC-Run-ID: run-17\r\n"
        b"X-TAC-Generation: 3\r\n\r\n"
    )

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            calls.append(("connect", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username))

        def select_folder(self, name, readonly=False):
            calls.append(("select", name, readonly))
            return {b"UIDVALIDITY": 31}

        def fetch(self, uids, fields):
            calls.append(("fetch", uids, fields))
            return {
                8: {
                    b"BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT X-TAC-RUN-ID X-TAC-GENERATION)]": (
                        raw_headers
                    )
                }
            }

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        result = gateway._mailu_headers("8", gateway._credentials["customer@example.test"])

    assert result == {
        "message_id": "<native@example.test>",
        "subject": "Native receipt",
        "run_id": "run-17",
        "generation": "3",
        "uidvalidity": "31",
    }
    assert ("select", "INBOX", True) in calls
    assert calls[-1][2] == [
        b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT X-TAC-RUN-ID X-TAC-GENERATION)]"
    ]


def test_mailu_receipt_reuses_native_confirmation_and_verifies_actual_imap_headers():
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_message = Mock(
        return_value={"mailbox_uid": "8", "reference": "INBOX UID 8", "uidvalidity": "31"}
    )
    gateway._mailu_headers = Mock(return_value={**correlated_headers(), "uidvalidity": "31"})

    receipt = gateway.find_receipt(
        CUSTOMER,
        "<case-9@example.test>",
        "Missing delivery",
        "run-17",
        3,
    )

    gateway._transport.confirm_message.assert_called_once()
    gateway._mailu_headers.assert_called_once_with(
        "8", gateway._credentials["customer@example.test"]
    )
    assert receipt["mailbox_uid"] == "8"


def test_mailu_receipt_rejects_matching_message_id_with_wrong_run_generation():
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    gateway._transport.confirm_message = Mock(
        return_value={"mailbox_uid": "8", "reference": "INBOX UID 8", "uidvalidity": "31"}
    )
    gateway._mailu_headers = Mock(
        return_value={**correlated_headers(), "generation": "2", "uidvalidity": "31"}
    )

    with pytest.raises(MailReceiptMismatch, match="mismatched correlation"):
        gateway.find_receipt(
            CUSTOMER,
            "<case-9@example.test>",
            "Missing delivery",
            "run-17",
            3,
        )


def test_poll_uses_readonly_peek_and_filters_run_and_generation_without_trusting_from():
    messages = {}
    for uid, run_id, generation, subject in [
        (12, "run-old", "3", "Old run"),
        (13, "run-17", "2", "Old generation"),
        (14, "run-17", "3", "Current complaint"),
        (15, None, None, "Normal customer reply"),
        (16, None, None, "Unrelated reply"),
    ]:
        message = EmailMessage()
        message["From"] = "finance-approver@example.com"
        message["To"] = "customer@example.test"
        message["Subject"] = subject
        message["Message-ID"] = f"<mail-{uid}@example.test>"
        message["In-Reply-To"] = (
            "<request-11@example.test>" if uid == 15 else "<prior@example.test>"
        )
        message["References"] = (
            "<root@example.test> <request-11@example.test>"
            if uid == 15
            else "<root@example.test> <prior@example.test>"
        )
        if run_id is not None:
            message["X-TAC-Run-ID"] = run_id
            message["X-TAC-Generation"] = generation
        if uid == 15:
            message["Auto-Submitted"] = "auto-replied"
        elif uid == 16:
            message["In-Reply-To"] = "<other-run@example.test>"
        message.set_content("Customer complaint body")
        messages[uid] = message.as_bytes()

    calls = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            calls.append(("connect", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username))

        def select_folder(self, name, readonly=False):
            calls.append(("select", name, readonly))
            return {b"UIDVALIDITY": 123}

        def search(self, criteria):
            calls.append(("search", criteria))
            return [12, 13, 14, 15, 16]

        def fetch(self, uids, fields):
            calls.append(("fetch", uids, fields))
            return {uid: {b"BODY[]": messages[uid]} for uid in uids}

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        found = gateway.poll(
            CUSTOMER,
            11,
            "run-17",
            3,
            known_message_ids={"<request-11@example.test>"},
        )

    assert len(found) == 2
    assert found[0]["mailbox_uid"] == "14"
    assert found[0]["message_id"] == "<mail-14@example.test>"
    assert found[0]["in_reply_to"] == "<prior@example.test>"
    assert found[0]["references"] == "<root@example.test> <prior@example.test>"
    assert found[0]["body"] == "Customer complaint body\n"
    assert found[0]["auto_submitted"] == "no"
    assert found[0]["from_header"] == "finance-approver@example.com"
    assert "approval" not in found[0]
    assert found[1]["mailbox_uid"] == "15"
    assert found[1]["auto_submitted"] == "auto-replied"
    assert found[1]["in_reply_to"] == "<request-11@example.test>"
    assert "16" not in {message["mailbox_uid"] for message in found}
    assert ("select", "INBOX", True) in calls
    assert ("fetch", [12, 13, 14, 15, 16], [b"BODY.PEEK[]"]) in calls


def test_poll_auth_failure_propagates_and_never_returns_empty_success():
    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            raise PermissionError("IMAP authentication rejected")

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        with pytest.raises(PermissionError, match="IMAP authentication rejected"):
            gateway.poll(CUSTOMER, 0, "run-17", 3, known_message_ids=set())


def test_wait_for_mail_uses_bounded_imap_idle_and_returns_correlated_normal_reply():
    message = EmailMessage()
    message["From"] = "customer@example.test"
    message["To"] = "buyer@example.com"
    message["Subject"] = "Re: Shipment status"
    message["Message-ID"] = "<reply-12@example.test>"
    message["In-Reply-To"] = "<request-11@example.test>"
    message["References"] = "<request-11@example.test>"
    message.set_content("The replacement is needed by Friday.")
    raw_message = message.as_bytes()
    calls = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            self.message_arrived = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username))

        def select_folder(self, name, readonly=False):
            calls.append(("select", name, readonly))
            return {b"UIDVALIDITY": 321}

        def search(self, criteria):
            calls.append(("search", criteria))
            return [22] if self.message_arrived else []

        def fetch(self, uids, fields):
            calls.append(("fetch", uids, fields))
            return {22: {b"BODY[]": raw_message}}

        def idle(self):
            calls.append(("idle",))

        def idle_check(self, timeout):
            calls.append(("idle_check", timeout))
            self.message_arrived = True
            return [(1, b"EXISTS")]

        def idle_done(self):
            calls.append(("idle_done",))

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        found = gateway.wait_for_mail(
            CUSTOMER,
            21,
            "run-17",
            3,
            known_message_ids={"<request-11@example.test>"},
            timeout=4,
        )

    assert len(found) == 1
    assert found[0]["message_id"] == "<reply-12@example.test>"
    assert found[0]["in_reply_to"] == "<request-11@example.test>"
    assert found[0]["body"] == "The replacement is needed by Friday.\n"
    assert ("idle",) in calls
    assert any(call[0] == "idle_check" and 0 < call[1] <= 4 for call in calls)
    assert ("idle_done",) in calls
    assert ("select", "INBOX", True) in calls


class FakeHTTPResponse:
    def __init__(self, *, payload=None, content=b"", status=200):
        self._payload = payload
        self.content = content
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"provider status {self.status_code}", response=self)


def _smtp4dev_message(provider_id, *, run_id=None, generation=None, reference=None, subject="Supplier update", body="Supplier response"):
    message = EmailMessage()
    message["From"] = "Supplier Desk <supplier@example.test>"
    message["To"] = "Viv <agent@agent.example.test>"
    message["Subject"] = subject
    message["Message-ID"] = f"<{provider_id}@example.test>"
    if run_id is not None:
        message["X-TAC-Run-ID"] = run_id
        message["X-TAC-Generation"] = str(generation)
    if reference is not None:
        message["In-Reply-To"] = reference
        message["References"] = f"<root@example.test> {reference}"
    message.set_content(body)
    return message


def _smtp4dev_detail(provider_id, message):
    return {
        "id": provider_id,
        "headers": [
            {"name": name, "value": value}
            for name, value in message.items()
        ],
    }


def test_poll_viv_reads_actual_paged_summary_detail_mime_and_filters_shared_inbox():
    messages = {
        "current": _smtp4dev_message("current", run_id="run-17", generation=3, body="Current supplier reply"),
        "wrong-run": _smtp4dev_message("wrong-run", run_id="other-run", generation=3, reference="<request-9@example.test>", body="Unrelated body"),
        "already-seen": _smtp4dev_message("already", run_id="run-17", generation=3, body="Previously processed"),
        "referenced": _smtp4dev_message("referenced", reference="<request-17@example.test>", body="Referenced reply"),
    }
    summaries = {
        1: [
            {"id": "current", "subject": "Supplier update"},
            {"id": "wrong-run", "subject": "Supplier update"},
            {"id": "already-seen", "subject": "Supplier update"},
        ],
        2: [{"id": "referenced", "subject": "Referenced reply"}],
    }
    calls = []

    def get(url, *, params=None, timeout=None):
        calls.append((url, params, timeout))
        if url.endswith("/api/Messages"):
            page = params["page"]
            return FakeHTTPResponse(payload={"results": summaries[page], "pageCount": 2})
        if url.endswith("/download"):
            provider_id = url.split("/")[-2]
            return FakeHTTPResponse(content=messages[provider_id].as_bytes())
        provider_id = url.rsplit("/", 1)[-1]
        return FakeHTTPResponse(payload=_smtp4dev_detail(provider_id, messages[provider_id]))

    gateway = MailuGateway(
        {**CONFIG, "smtp4dev_api_base_url": "http://127.0.0.1:19157"}, CREDENTIALS
    )
    with patch("servers.simulation.mail.requests.get", side_effect=get):
        found = gateway.poll_viv(
            "run-17", 3, {"<request-17@example.test>", "<already@example.test>"}
        )

    assert [item["provider_id"] for item in found] == ["current", "referenced"]
    assert found[0] == {
        "message_id": "<current@example.test>",
        "subject": "Supplier update",
        "body": "Current supplier reply\n",
        "from": "Supplier Desk <supplier@example.test>",
        "to": "Viv <agent@agent.example.test>",
        "in_reply_to": "",
        "references": "",
        "provider_id": "current",
        "webmail_url": "http://127.0.0.1:19157/#/messages/mailbox/Default/folder/INBOX/message/current",
        "run_id": "run-17",
        "generation": 3,
    }
    assert found[1]["body"] == "Referenced reply\n"
    assert all("Unrelated body" not in str(item) for item in found)
    list_calls = [call for call in calls if call[0].endswith("/api/Messages")]
    assert [call[1]["page"] for call in list_calls] == [1, 2]
    assert all(call[1]["mailboxName"] == "Default" and call[1]["folderName"] == "INBOX" for call in list_calls)
    downloads = [call[0] for call in calls if call[0].endswith("/download")]
    assert downloads == [
        "http://127.0.0.1:19157/api/Messages/current/download",
        "http://127.0.0.1:19157/api/Messages/referenced/download",
    ]


def test_poll_viv_provider_failure_is_typed_and_retains_underlying_cause_without_body():
    secret_body = "private captured mail must not appear in errors"
    response = FakeHTTPResponse(status=503, content=secret_body.encode())
    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.requests.get", return_value=response):
        with pytest.raises(SMTP4DevAPIError) as raised:
            gateway.poll_viv("run-17", 3, set())

    assert raised.value.operation == "list messages"
    assert raised.value.status == 503
    assert isinstance(raised.value.__cause__, requests.HTTPError)
    assert secret_body not in str(raised.value)


def test_poll_viv_matching_message_without_mime_body_fails_visibly():
    message = EmailMessage()
    message["From"] = "supplier@example.test"
    message["To"] = "agent@agent.example.test"
    message["Subject"] = "No body"
    message["Message-ID"] = "<no-body@example.test>"
    message["X-TAC-Run-ID"] = "run-17"
    message["X-TAC-Generation"] = "3"
    provider_id = "no-body"

    def get(url, *, params=None, timeout=None):
        if url.endswith("/api/Messages"):
            return FakeHTTPResponse(payload={"results": [{"id": provider_id}], "pageCount": 1})
        if url.endswith("/download"):
            return FakeHTTPResponse(content=message.as_bytes())
        return FakeHTTPResponse(payload=_smtp4dev_detail(provider_id, message))

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.requests.get", side_effect=get):
        with pytest.raises(SMTP4DevAPIError) as raised:
            gateway.poll_viv("run-17", 3, set())

    assert raised.value.operation == "read message body"
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize("signals, expected", [([(1, b"EXISTS")], True), ([], False)])
def test_wait_for_activity_uses_native_bounded_imap_idle(signals, expected):
    calls = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            calls.append(("connect", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username))

        def select_folder(self, folder, readonly=False):
            calls.append(("select", folder, readonly))

        def idle(self):
            calls.append(("idle",))

        def idle_check(self, timeout):
            calls.append(("idle_check", timeout))
            return signals

        def idle_done(self):
            calls.append(("idle_done",))

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        assert gateway.wait_for_activity(CUSTOMER, timeout=2.5) is expected
    assert ("select", "INBOX", True) in calls
    assert any(call[0] == "idle_check" and call[1] == 2.5 for call in calls)
    assert calls.count(("idle",)) == 1 and calls.count(("idle_done",)) == 1


def test_inspect_issue_notification_searches_exact_id_readonly_and_returns_internaldate():
    message_id = "<issue-42@example.test>"
    raw_headers = (
        b"Message-ID: <issue-42@example.test>\r\n"
        b"To: Customer <customer@example.test>\r\n\r\n"
    )
    calls = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            calls.append(("connect", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, username, password):
            calls.append(("login", username))

        def list_folders(self):
            return [
                ([b"\\Noselect"], b"/", b"Root"),
                ([b"\\HasNoChildren"], b"/", b"INBOX"),
                ([b"\\HasNoChildren"], b"/", b"Archive"),
            ]

        def select_folder(self, folder, readonly=False):
            calls.append(("select", folder, readonly))
            return {b"UIDVALIDITY": b"901" if folder == "Archive" else b"900"}

        def search(self, criteria):
            calls.append(("search", criteria))
            return [24] if calls[-2][1] == "Archive" else []

        def fetch(self, uids, fields):
            calls.append(("fetch", uids, fields))
            internal_date = datetime(2026, 10, 30, 9, 0, tzinfo=timezone(timedelta(hours=1)))
            if getattr(self, "normalise_times", True):
                internal_date = internal_date.replace(tzinfo=None)
            return {
                24: {
                    b"FLAGS": (b"\\Seen",),
                    b"INTERNALDATE": internal_date,
                    b"BODY[HEADER.FIELDS (MESSAGE-ID TO)]": raw_headers,
                }
            }

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        receipt = gateway.inspect_issue_notification(CUSTOMER, message_id)

    assert receipt == {
        "message_id": message_id,
        "received_at": "2026-10-30T08:00:00+00:00",
        "seen": True,
        "mailbox_uid": "24",
        "uidvalidity": "901",
        "folder": "Archive",
        "reference": "Archive UID 24 UIDVALIDITY 901",
    }
    assert ("select", "INBOX", True) in calls
    assert ("select", "Archive", True) in calls
    searches = [call for call in calls if call[0] == "search"]
    assert searches == [("search", ["HEADER", "Message-ID", message_id])] * 2
    fetch = next(call for call in calls if call[0] == "fetch")
    assert fetch[2] == [
        b"FLAGS", b"INTERNALDATE", b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO)]"
    ]
    assert all(b"BODY[]" not in field and b"BODY.PEEK[]" not in field for field in fetch[2])


def test_inspect_issue_notification_returns_none_and_rejects_header_mismatch():
    class FakeIMAPClient:
        actual_headers = (
            b"Message-ID: <different@example.test>\r\n"
            b"To: customer@example.test\r\n\r\n"
        )

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def list_folders(self):
            return [([], b"/", b"INBOX")]

        def select_folder(self, *_args, **_kwargs):
            return {b"UIDVALIDITY": 1}

        def search(self, _criteria):
            return [] if not self.actual_headers else [1]

        def fetch(self, *_args):
            return {
                1: {
                    b"FLAGS": (),
                    b"INTERNALDATE": datetime(2026, 10, 30, tzinfo=timezone.utc),
                    b"BODY[HEADER.FIELDS (MESSAGE-ID TO)]": self.actual_headers,
                }
            }

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        FakeIMAPClient.actual_headers = b""
        assert gateway.inspect_issue_notification(CUSTOMER, "<wanted@example.test>") is None
        FakeIMAPClient.actual_headers = (
            b"Message-ID: <other@example.test>\r\n"
            b"To: customer@example.test\r\n\r\n"
        )
        with pytest.raises(MailReceiptMismatch, match="different Message-ID"):
            gateway.inspect_issue_notification(CUSTOMER, "<wanted@example.test>")


def test_inspect_issue_notification_uses_earliest_duplicate_and_seen_any():
    message_id = "<duplicate@example.test>"
    headers = (
        b"Message-ID: <duplicate@example.test>\r\n"
        b"To: customer@example.test\r\n\r\n"
    )

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            self.folder = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def list_folders(self):
            return [([], b"/", b"INBOX"), ([], b"/", b"Archive")]

        def select_folder(self, folder, readonly=False):
            self.folder = folder
            return {b"UIDVALIDITY": 10 if folder == "INBOX" else 20}

        def search(self, _criteria):
            return [9]

        def fetch(self, _uids, _fields):
            received_at = datetime(
                2026, 10, 30, 10 if self.folder == "INBOX" else 11, tzinfo=timezone.utc
            )
            return {
                9: {
                    b"FLAGS": (b"\\Seen",) if self.folder == "Archive" else (),
                    b"INTERNALDATE": received_at,
                    b"BODY[HEADER.FIELDS (MESSAGE-ID TO)]": headers,
                }
            }

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        receipt = gateway.inspect_issue_notification(CUSTOMER, message_id)

    assert receipt == {
        "message_id": message_id,
        "received_at": "2026-10-30T10:00:00+00:00",
        "seen": True,
        "mailbox_uid": "9",
        "uidvalidity": "10",
        "folder": "INBOX",
        "reference": "INBOX UID 9 UIDVALIDITY 10",
    }


def test_inspect_issue_notification_rejects_wrong_recipient_header():
    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def list_folders(self):
            return [([], b"/", b"INBOX")]

        def select_folder(self, *_args, **_kwargs):
            return {b"UIDVALIDITY": 1}

        def search(self, _criteria):
            return [5]

        def fetch(self, *_args):
            return {
                5: {
                    b"FLAGS": (),
                    b"INTERNALDATE": datetime(2026, 10, 30, tzinfo=timezone.utc),
                    b"BODY[HEADER.FIELDS (MESSAGE-ID TO)]": (
                        b"Message-ID: <wanted@example.test>\r\n"
                        b"To: other@example.test\r\n\r\n"
                    ),
                }
            }

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        with pytest.raises(MailReceiptMismatch, match="recipient does not match"):
            gateway.inspect_issue_notification(CUSTOMER, "<wanted@example.test>")


def test_wait_for_activity_preserves_idle_failure_and_cleanup_failure_as_cause_note():
    failure = TimeoutError("provider idle failed")
    calls = []

    class FakeIMAPClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            pass

        def select_folder(self, *_args, **_kwargs):
            pass

        def idle(self):
            pass

        def idle_check(self, *, timeout):
            raise failure

        def idle_done(self):
            calls.append("done")
            raise OSError("cleanup")

    gateway = MailuGateway(CONFIG, CREDENTIALS)
    with patch("servers.simulation.mail.IMAPClient", FakeIMAPClient):
        with pytest.raises(TimeoutError) as raised:
            gateway.wait_for_activity(CUSTOMER, timeout=1)
    assert raised.value is failure
    assert "IMAP IDLE cleanup also failed: OSError" in raised.value.__notes__[0]
    assert calls == ["done"]


def test_support_alias_uses_native_mailbox_receipt_and_rejects_unconfigured_target():
    smtp = FakeSMTP([])
    gateway = MailuGateway({**CONFIG, "delivery_aliases": {"support@example.com": "buyer"}}, CREDENTIALS)
    gateway._transport.confirm_message = Mock(return_value={"mailbox_uid": "42", "uidvalidity": "987"})
    gateway._mailu_headers = Mock(return_value={**correlated_headers(), "uidvalidity": "987"})
    with patch("servers.simulation.mail.smtplib.SMTP", return_value=smtp):
        receipt = gateway.send(CUSTOMER, BUYER, "Missing delivery", "Please check my order.",
                               "<case-9@example.test>", "run-17", 3,
                               delivery_address="support@example.com")
    assert smtp.to_addrs == ["support@example.com"]
    assert str(smtp.message["To"]) == "support@example.com"
    assert receipt["recipient_actor_id"] == "buyer"
    assert gateway._transport.confirm_message.call_args.args[1]["username"] == "buyer"
    with patch("servers.simulation.mail.smtplib.SMTP") as forbidden_send:
        with pytest.raises(PermissionError):
            gateway.send(CUSTOMER, BUYER, "Missing delivery", "Please check.",
                         "<bad@example.test>", "run-17", 3, delivery_address="outside@example.org")
        forbidden_send.assert_not_called()
