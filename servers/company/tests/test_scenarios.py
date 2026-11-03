"""Durable scenario runner tests with network calls replaced at transport boundaries."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from servers.company.scenarios import (
    RealChatTransport,
    RealMailTransport,
    ScenarioStore,
    STEP_CHAT_POSTED,
    STEP_EMAIL_DELIVERED,
    STEP_EMAIL_FORWARDED,
    STEP_FAILED,
    _fixture_message,
    _fixture_hash,
    _roundcube_message_url,
    run_scenario,
)
from servers.company.owncloud import OwnCloudFileReference
from servers.company.tests.test_owncloud import WebDAVSession


class FakeMailTransport:
    def __init__(self):
        self.deliveries = []
        self.delivered_messages = []
        self.forwards = []
        self.inbox_ids = set()
        self.smtp4dev_ids = set()
        self.fail_delivery_once = False
        self.fail_auth_once = False
        self.delivery_attempts = 0
        self.append_attempts = 0
        self.fail_forward_once = False
        self.fail_forward_auth_once = False
        self.forward_attempts = 0
        self.smtp_send_attempts = 0

    def deliver_fixture(self, fixture, credentials, message, digest, on_append_started):
        self.delivery_attempts += 1
        if self.fail_auth_once:
            self.fail_auth_once = False
            raise PermissionError("synthetic IMAP authentication failure")
        on_append_started()
        self.append_attempts += 1
        self.deliveries.append((message["Message-ID"], digest, message["From"], message["To"]))
        self.delivered_messages.append(message)
        if self.fail_delivery_once:
            self.fail_delivery_once = False
            # Model a connection loss after the server may have accepted the message.
            self.inbox_ids.add(message["Message-ID"])
            raise ConnectionError("synthetic transport interruption")
        self.inbox_ids.add(message["Message-ID"])

    def confirm_message(self, fixture, credentials, message_id):
        return ({"mailbox_uid": "uid-1", "reference": "INBOX UID uid-1", "uidvalidity": "1",
                 "webmail_url": _roundcube_message_url(fixture["employee_mailbox"]["webmail_url"], "uid-1")}
                if message_id in self.inbox_ids else None)

    def forward_message(self, fixture, credentials, message, digest, on_send_started):
        self.forward_attempts += 1
        if self.fail_forward_auth_once:
            self.fail_forward_auth_once = False
            raise PermissionError("synthetic SMTP authentication failure")
        on_send_started()
        self.smtp_send_attempts += 1
        self.forwards.append((message["Message-ID"], digest, credentials["username"],
                              message["X-Forwarded-Message-ID"]))
        if self.fail_forward_once:
            self.fail_forward_once = False
            self.smtp4dev_ids.add(message["Message-ID"])
            raise ConnectionError("synthetic SMTP submission interruption")
        self.smtp4dev_ids.add(message["Message-ID"])

    def confirm_smtp4dev(self, fixture, message_id):
        return ({"mailbox_uid": "smtp4dev-id-1", "reference": "smtp4dev message smtp4dev-id-1",
                 "webmail_url": fixture["smtp4dev_base_url"] + "/#/messages/mailbox/Default/folder/INBOX/message/smtp4dev-id-1"}
                if message_id in self.smtp4dev_ids else None)


class FakeChatTransport:
    def __init__(self):
        self.calls = []
        self.posted = {}
        self.fail_once = False
        self.fail_before_submit = False
        self.timeline = []

    def post(self, fixture, credentials, attempt_id, *, on_submission_started=None,
             on_message_id_captured=None):
        self.calls.append((attempt_id, credentials["username"], fixture["chat_message"]))
        if self.fail_before_submit:
            self.fail_before_submit = False
            raise PermissionError("synthetic browser authentication failure")
        if on_submission_started:
            on_submission_started()
        self.timeline.append("submission_marked")
        message_id = "native-" + attempt_id
        room = fixture["employee_mailbox"]["rocketchat_room_id"]
        receipt = {"message_id": message_id, "room_id": room, "thread_id": None,
                   "browser_url": f"https://chat.example.test/channel/general?msg={message_id}"}
        self.posted[message_id] = (receipt, fixture["chat_message"])
        self.timeline.append("message_accepted")
        if self.fail_once:
            self.fail_once = False
            if on_message_id_captured:
                on_message_id_captured(message_id)
            self.timeline.append("native_id_persisted")
            raise ConnectionError("synthetic chat timeout after acceptance")
        if on_message_id_captured:
            on_message_id_captured(message_id)
        self.timeline.append("native_id_persisted")
        return receipt

    def confirm_message(self, fixture, credentials, message_id, message_text, submission_started_at):
        if message_id:
            saved = self.posted.get(message_id)
            return saved[0] if saved and saved[1] == message_text else None
        matches = [receipt for receipt, text in self.posted.values() if text == message_text]
        return matches[0] if len(matches) == 1 else None


class CrossProcessMailTransport:
    def __init__(self, counter, first_started, release_first, duplicate_delivery):
        self.counter = counter
        self.first_started = first_started
        self.release_first = release_first
        self.duplicate_delivery = duplicate_delivery

    def deliver_fixture(self, fixture, credentials, message, digest, on_append_started):
        with self.counter.get_lock():
            self.counter.value += 1
            count = self.counter.value
        if count == 1:
            self.first_started.set()
            if not self.release_first.wait(timeout=5):
                raise TimeoutError("scenario test gate timed out")
        else:
            self.duplicate_delivery.set()
        on_append_started()

    def confirm_message(self, fixture, credentials, message_id):
        return {"mailbox_uid": "process-uid", "reference": "imap://INBOX;UIDVALIDITY=1;UID=process-uid"}


def _run_scenario_process(fixture_path, credentials_path, database, mail_transport, started, result_queue):
    started.set()
    try:
        result = run_scenario(fixture_path, credentials_path, database, mail_transport=mail_transport)
        result_queue.put((result["id"], result["status"], result.get("run_error_types")))
    except Exception as error:
        result_queue.put(("uncaught", type(error).__name__, str(error)))


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("synthetic HTTP error")

    def json(self):
        return self.payload


class FakeChatSession:
    def __init__(self, user_id="expected-employee-id", mention_user_id="assistant-account-id",
                 message_author_id=None, message_room_id="room-safe-id", message_text=None):
        self.user_id = user_id
        self.mention_user_id = mention_user_id
        self.message_author_id = message_author_id or user_id
        self.message_room_id = message_room_id
        self.message_text = message_text
        self.message = None
        self.native_message_id = "rocketchat-native-id"
        self.client_message_id = self.native_message_id
        self.historic_message = None
        self.sent_message_events = 0
        self.typing_events = []
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        raise AssertionError(f"unexpected POST endpoint: {url}")

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/v1/me"):
            return Response({"success": True, "_id": self.user_id, "username": "employee"})
        if url.endswith("/api/v1/users.info"):
            return Response({"success": True, "user": {"_id": self.mention_user_id, "username": "configured_assistant"}})
        if url.endswith("/api/v1/rooms.info"):
            return Response({"success": True, "room": {"_id": "room-safe-id", "t": "c", "name": "general"}})
        if url.endswith("/api/v1/chat.getMessage"):
            requested = kwargs.get("params", {}).get("msgId")
            return Response({"success": True, "message": self.message}, status=200) if self.message and requested == self.message["_id"] else Response({"success": False}, status=400)
        raise AssertionError(f"unexpected GET endpoint: {url}")

    def submit_from_browser(self, text):
        self.message = {"_id": self.client_message_id, "rid": self.message_room_id,
                       "u": {"_id": self.message_author_id}, "msg": self.message_text or text,
                       "tmid": None, "ts": "2026-10-31T12:00:00Z"}
        return {"msg": "changed", "collection": "stream-room-messages",
                "fields": {"args": [self.message]}}


class FakeWebSocket:
    url = "wss://chat.example.test/websocket"

    def __init__(self, session):
        self.session = session
        self.frames = []
        self.listeners = {"framereceived": [], "framesent": []}
        self.closed = False
        self.connected_frames_seen = False

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        if event not in self.listeners:
            raise AssertionError(f"unexpected websocket event: {event}")
        self.listeners[event].append(callback)

    def wait_for_event(self, event, predicate=None, timeout=None):
        if timeout != 0:
            raise AssertionError("DDP initialization wait must have no deadline")
        if event == "framereceived":
            while self.frames:
                frame = self.frames.pop(0)
                if predicate is None or predicate(frame):
                    self.emit(frame)
                    return frame
        raise AssertionError("the synthetic DDP connected frame arrived before the wait")

    def emit(self, frame):
        self.emit_event("framereceived", frame)

    def emit_event(self, event, frame):
        if frame.get("msg") == "connected":
            self.connected_frames_seen = True
        if event == "framesent" and frame.get("method") == "sendMessage":
            self.session.sent_message_events += 1
        for listener in self.listeners[event]:
            listener(frame)


class FakeMessageComposer:
    def __init__(self, page):
        self.page = page
        self.text = ""
        self.first = self

    def count(self):
        return 1

    def is_visible(self):
        return self.page.composer_visible

    def wait_for(self, state=None, timeout=None):
        if state != "visible" or timeout != 0:
            raise AssertionError("composer visibility wait must be unbounded")
        if not self.page.socket.connected_frames_seen:
            raise AssertionError("the DDP handshake must arrive before composer visibility")
        self.page.composer_waits += 1
        self.page.composer_visible = True

    def fill(self, text, timeout=None):
        if timeout != 0:
            raise AssertionError("composer edits must not add a deadline")
        if text != "":
            raise AssertionError("message content must be typed visibly, not filled instantly")
        self.text = ""
        self.page.session.typing_events.append(("clear",))

    def press_sequentially(self, text, delay=None, timeout=None):
        if timeout != 0:
            raise AssertionError("visible typing must not add a deadline")
        if self.page.session.sent_message_events:
            raise AssertionError("message was submitted before visible typing finished")
        self.text += text
        self.page.session.typing_events.append(("type", text, delay, timeout))

    def press(self, key, timeout=None):
        if timeout != 0:
            raise AssertionError("message composer keypresses must not add a deadline")
        if key == "Shift+Enter":
            if self.page.session.sent_message_events:
                raise AssertionError("line break submitted the message")
            self.text += "\n"
            self.page.session.typing_events.append(("newline", key))
            return
        if key != "Enter":
            raise AssertionError(f"unexpected message submit key: {key}")
        if self.page.socket.is_closed():
            raise AssertionError("message submission used a closed websocket")
        if not self.page.socket.listeners["framereceived"]:
            raise AssertionError("DDP frame listener must be installed before message submission")
        self.page.session.typing_events.append(("enter",))
        frame = self.page.session.submit_from_browser(self.text)
        outgoing = {"msg": "method", "method": "sendMessage", "id": "send-rpc-1",
                    "params": [{"_id": self.page.session.client_message_id,
                                "rid": self.page.session.message_room_id, "msg": self.text}]}
        self.page.socket.emit_event("framesent", outgoing)
        # Model a fast DDP acknowledgement and message event during Enter.
        self.page.socket.emit({"msg": "result", "id": "send-rpc-1",
                              "result": {"_id": self.page.session.client_message_id}})
        self.page.socket.emit(frame)


class FakePage:
    def __init__(self, session):
        self.session = session
        self.listeners = {}
        self.socket = None
        self.sockets = []
        self.composer_visible = False
        self.composer_waits = 0
        self.composer = FakeMessageComposer(self)
        self.url = ""

    def on(self, event, callback):
        self.listeners[event] = callback

    def goto(self, url, **kwargs):
        if kwargs.get("timeout") != 0:
            raise AssertionError("browser navigation must not add a deadline")
        self.url = url
        if "/channel/" in url and self.socket is not None:
            self.socket.closed = True
        self.socket = FakeWebSocket(self.session)
        self.sockets.append(self.socket)
        if "websocket" in self.listeners:
            self.listeners["websocket"](self.socket)
            # The DDP handshake arrives while the composer is still loading.
            self.socket.emit({"msg": "connected", "session": "synthetic"})
            historic_message = self.session.historic_message
            if "/channel/" in url and historic_message is not None:
                self.socket.emit({"msg": "changed", "collection": "stream-room-messages",
                                  "fields": {"args": [historic_message]}})

    def wait_for_event(self, event, predicate=None, timeout=None):
        if timeout != 0:
            raise AssertionError("websocket initialization wait must have no deadline")
        if event == "websocket":
            candidates = [socket for socket in self.sockets if predicate is None or predicate(socket)]
            if candidates:
                return candidates[-1]
        raise TimeoutError("synthetic websocket wait expired")

    def evaluate(self, _script):
        return {"token": "synthetic-browser-token", "userId": self.session.user_id}

    def locator(self, selector):
        if selector.startswith('textarea[aria-label="Message"]'):
            return self.composer
        return EmptyLocator()


class EmptyLocator:
    count = lambda self: 0


class FakeBrowserSessionProvider:
    def __init__(self, session):
        self.session = session
        self.employee_emails = []
        self.pages = []

    @contextmanager
    def open_for_employee_email(self, email):
        self.employee_emails.append(email)
        page = FakePage(self.session)
        self.pages.append(page)
        yield page


class ScenarioRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture_path = self.root / "scenario.json"
        self.credentials_path = self.root / "credentials.json"
        self.database = self.root / "scenarios.sqlite"
        self.attachment = self.root / "statement.pdf"
        self.attachment.write_bytes(b"synthetic attachment content")
        self.fixture = {
            "scenario_id": "employee-inbox-smoke",
            "fixture_version": "3",
            "environment_version": "pr588",
            "employee_mailbox": {
                "address": "employee@example.test",
                "webmail_url": "https://mail.example.test/webmail",
                "imap_host": "mail.example.test",
                "imap_port": 2993,
                "inbound_smtp_host": "mail.example.test",
                "inbound_smtp_port": 2525,
                "submission_smtp_host": "mail.example.test",
                "submission_smtp_port": 2587,
                "submission_tls": "starttls",
                "tls_ca_file": None,
                "mail_credentials_ref": "employee-mail",
                "chat_credentials_ref": "employee-chat",
                "rocketchat_base_url": "https://chat.example.test",
                "rocketchat_room_id": "room-safe-id",
                "rocketchat_expected_user_id": "expected-employee-id",
            },
            "message_id": "<fixture-001@example.test>",
            "from_address": "vendor@example.test",
            "subject": "Monthly statement",
            "body": "Please review the attached statement.",
            "attachments": [{"filename": "statement.pdf", "path": str(self.attachment)}],
            "forward_to_viv": True,
            "viv_route_address": "viv-route@example.test",
            "smtp4dev_base_url": "http://smtp4dev.test",
            "post_chat": True,
            "chat_message": "Email transport scenario completed.",
            "confirm_timeout_seconds": 0,
        }
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.credentials_path.write_text(json.dumps({
            "employee-mail": {"username": "mail-user", "password": "mail-password"},
            "employee-chat": {"username": "chat-user", "password": "chat-password"},
            "midpoint-directory": {"username": "directory-user", "password": "directory-password"},
            "owncloud-sarah": {"username": "sarah_johnson", "password": "private"},
        }))
        os.chmod(self.credentials_path, 0o600)
        self.mail = FakeMailTransport()
        self.chat = FakeChatTransport()

    def tearDown(self):
        self.temp.cleanup()

    def run_it(self, **kwargs):
        return run_scenario(self.fixture_path, self.credentials_path, self.database,
                            mail_transport=self.mail, chat_transport=self.chat, **kwargs)

    def test_runs_and_persists_only_audit_metadata(self):
        result = self.run_it()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual({step["step"] for step in result["steps"]},
                         {STEP_EMAIL_DELIVERED, STEP_EMAIL_FORWARDED, STEP_CHAT_POSTED})
        self.assertEqual(len(self.mail.deliveries), 1)
        self.assertEqual(len(self.mail.forwards), 1)
        self.assertEqual(self.mail.forwards[0][2], "mail-user")
        self.assertTrue(self.mail.deliveries[0][0].startswith("<"))
        self.assertNotEqual(self.mail.deliveries[0][0], self.fixture["message_id"])
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute("SELECT * FROM scenario_steps").fetchall()
            persisted = json.dumps(rows)
        self.assertNotIn("Monthly statement", persisted)
        self.assertNotIn("attached statement", persisted)
        self.assertNotIn("statement.pdf", persisted)
        self.assertNotIn("mail-password", persisted)
        self.assertNotIn("chat-password", persisted)
        self.assertEqual(len(self.chat.calls), 1)
        email_step = next(step for step in result["steps"] if step["step"] == STEP_EMAIL_DELIVERED)
        forward_step = next(step for step in result["steps"] if step["step"] == STEP_EMAIL_FORWARDED)
        chat_step = next(step for step in result["steps"] if step["step"] == STEP_CHAT_POSTED)
        self.assertEqual(email_step["mailbox_uid"], "uid-1")
        self.assertEqual(email_step["delivery_method"], "IMAP_APPEND")
        self.assertEqual(email_step["mailbox_uidvalidity"], "1")
        self.assertEqual(email_step["webmail_url"],
                         "https://mail.example.test/webmail?_task=mail&_mbox=INBOX&_uid=uid-1&_action=show")
        self.assertEqual(forward_step["delivery_method"], "SMTP_SUBMISSION")
        self.assertEqual(forward_step["submission_started"], 1)
        self.assertEqual(self.mail.forwards[0][3], self.mail.deliveries[0][0])
        self.assertEqual(chat_step["room_id"], "room-safe-id")
        self.assertEqual(chat_step["thread_id"], None)
        self.assertEqual(chat_step["browser_url"],
                         f"https://chat.example.test/channel/general?msg={chat_step['external_id']}")
        self.assertIn(email_step["webmail_url"], self.chat.calls[0][2])

    def test_owncloud_source_bytes_are_attached_and_pinned_in_resume_hash(self):
        reference = OwnCloudFileReference(
            reference_id="invoice-copy", owner_employee_id="sarah-johnson",
            owner_midpoint_oid="midpoint-sarah", owner_shadow_oid="shadow-sarah",
            owner_uid="sarah_johnson", path="Documents/invoice.pdf",
            href="https://owncloud.example.test/remote.php/dav/files/sarah_johnson/Documents/invoice.pdf",
            file_id="oc-file-22", etag='"v3"', sha256="a" * 64, size=12,
        )
        first, first_digest = _fixture_message(
            self.fixture, "employee@example.test", "vendor@example.test", "<source-1@test>",
            [(reference, b"native-source")],
        )
        attachment = list(first.iter_attachments())[-1]
        self.assertEqual(attachment.get_payload(decode=True), b"native-source")
        self.assertEqual(attachment["X-TAC-OwnCloud-FileID"], "oc-file-22")
        self.assertEqual(attachment["X-TAC-OwnCloud-ETag"], '"v3"')
        changed = OwnCloudFileReference(**{
            **reference.__dict__, "etag": '"v4"', "sha256": "b" * 64,
        })
        self.assertNotEqual(
            _fixture_hash(self.fixture, [(reference, b"native-source")]),
            _fixture_hash(self.fixture, [(changed, b"changed-source")]),
        )
        second, second_digest = _fixture_message(
            self.fixture, "employee@example.test", "vendor@example.test", "<source-1@test>",
            [(changed, b"changed-source")],
        )
        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(second["Message-ID"], first["Message-ID"])

    def test_reconciles_accepted_uncertain_imap_append_without_resending(self):
        self.mail.fail_delivery_once = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["steps"][0]["step"], STEP_FAILED)
        self.assertEqual(first["steps"][0]["error_type"], "ConnectionError")
        self.assertEqual(first["steps"][0]["delivery_method"], "IMAP_APPEND")
        self.assertEqual(first["steps"][0]["submission_started"], 1)
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(self.mail.append_attempts, 1)
        self.assertEqual(len(self.mail.forwards), 1)

    def test_imap_login_failure_can_retry_same_run_without_marking_append_uncertain(self):
        self.mail.fail_auth_once = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        delivery = next(step for step in first["steps"] if step["step"] == STEP_FAILED)
        self.assertEqual(delivery["delivery_method"], "IMAP_APPEND")
        self.assertEqual(delivery["submission_started"], 0)
        self.assertEqual(delivery["error_type"], "PermissionError")
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(self.mail.delivery_attempts, 2)
        self.assertEqual(self.mail.append_attempts, 1)
        self.assertEqual(len(self.mail.forwards), 1)

    def test_reconciles_uncertain_forward_via_smtp4dev_without_resubmitting(self):
        self.mail.fail_forward_once = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        self.assertTrue(any(step["step"] == STEP_FAILED and step["external_id"] in self.mail.smtp4dev_ids
                            for step in first["steps"]))
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(len(self.mail.forwards), 1)
        self.assertEqual(self.mail.smtp_send_attempts, 1)
        self.assertEqual(len(self.mail.deliveries), 1)

    def test_smtp_login_failure_can_retry_same_run_without_marking_send_uncertain(self):
        self.mail.fail_forward_auth_once = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        forward = next(step for step in first["steps"] if step["step"] == STEP_FAILED)
        self.assertEqual(forward["delivery_method"], "SMTP_SUBMISSION")
        self.assertEqual(forward["submission_started"], 0)
        self.assertEqual(forward["error_type"], "PermissionError")
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(self.mail.forward_attempts, 2)
        self.assertEqual(self.mail.smtp_send_attempts, 1)
        self.assertEqual(len(self.mail.forwards), 1)

    def test_chat_uncertain_recovery_verifies_native_id_without_resubmitting_or_repeating_email(self):
        self.chat.fail_once = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        first_step = next(step for step in first["steps"] if step["step_key"] == "chat-post")
        self.assertEqual(first_step["step"], STEP_FAILED)
        self.assertEqual(first_step["submission_started"], 1)
        self.assertTrue(first_step["submission_started_at"])
        self.assertTrue(first_step["external_id"].startswith("native-"))
        self.assertEqual(self.chat.timeline[:3], ["submission_marked", "message_accepted", "native_id_persisted"])
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(len(self.mail.deliveries), 1)
        self.assertEqual(len(self.mail.forwards), 1)
        self.assertEqual(len(self.chat.calls), 1)
        final_step = next(step for step in second["steps"] if step["step_key"] == "chat-post")
        self.assertEqual(final_step["external_id"], first_step["external_id"])

    def test_chat_uncertain_outcome_without_matching_message_is_not_resent(self):
        self.chat.fail_once = True
        first = self.run_it()
        self.chat.posted.clear()
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(len(self.chat.calls), 1)
        chat_step = next(step for step in second["steps"] if step["step_key"] == "chat-post")
        self.assertEqual(chat_step["error_type"], "ChatBrowserMessageNotFound")
        self.assertEqual(chat_step["submission_started"], 1)

    def test_chat_failure_before_submission_can_retry(self):
        self.chat.fail_before_submit = True
        first = self.run_it()
        self.assertEqual(first["status"], "FAILED")
        first_step = next(step for step in first["steps"] if step["step_key"] == "chat-post")
        self.assertEqual(first_step["submission_started"], 0)
        second = self.run_it(run_id=first["id"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(len(self.chat.calls), 2)

    def test_requires_explicit_versions_and_smtp4dev_receipt_configuration(self):
        bad = dict(self.fixture)
        bad.pop("environment_version")
        self.fixture_path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "environment_version"):
            self.run_it()

        self.fixture["environment_version"] = "pr588"
        self.fixture.pop("smtp4dev_base_url")
        self.fixture_path.write_text(json.dumps(self.fixture))
        with self.assertRaisesRegex(ValueError, "smtp4dev_base_url"):
            self.run_it()

    def test_fixture_content_cannot_change_without_a_fixture_version_bump(self):
        first = self.run_it()
        self.fixture["body"] = "Changed fixture content"
        self.fixture_path.write_text(json.dumps(self.fixture))
        with self.assertRaisesRegex(ValueError, "does not match the requested run"):
            self.run_it(run_id=first["id"])
        with self.assertRaisesRegex(ValueError, "fixture_version update"):
            self.run_it(new_run=True)

    def test_explicit_new_run_has_distinct_message_ids(self):
        first = self.run_it()
        second = self.run_it(new_run=True)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(self.mail.deliveries[0][0], self.mail.deliveries[1][0])

    def test_email_only_fixture_does_not_require_chat_configuration_or_credentials(self):
        self.fixture["forward_to_viv"] = False
        self.fixture["post_chat"] = False
        self.fixture.pop("chat_message")
        self.fixture["employee_mailbox"].pop("chat_credentials_ref")
        self.fixture["employee_mailbox"].pop("rocketchat_base_url")
        self.fixture["employee_mailbox"].pop("rocketchat_room_id")
        self.fixture["employee_mailbox"].pop("rocketchat_expected_user_id")
        self.fixture["employee_mailbox"].pop("submission_smtp_host")
        self.fixture["employee_mailbox"].pop("submission_smtp_port")
        self.fixture["employee_mailbox"].pop("submission_tls")
        self.fixture["employee_mailbox"].pop("inbound_smtp_host")
        self.fixture["employee_mailbox"].pop("inbound_smtp_port")
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.credentials_path.write_text(json.dumps({"employee-mail": {"username": "mail-user", "password": "mail-password"}}))
        os.chmod(self.credentials_path, 0o600)
        result = self.run_it()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual([step["step"] for step in result["steps"]], [STEP_EMAIL_DELIVERED])

    def test_inbound_fixture_preserves_arbitrary_from_without_using_smtp(self):
        from email import message_from_bytes
        from email.policy import default

        events = []

        class FakeIMAPClient:
            def __init__(self, *args, **kwargs):
                events.append("connect")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def login(self, username, password):
                events.append("login")

            def select_folder(self, folder):
                events.append(("select", folder))

            def append(self, folder, content):
                events.append(("append", folder))
                events.append(message_from_bytes(content, policy=default)["From"])

        self.fixture["from_address"] = "customer@example.test"
        message, digest = _fixture_message(self.fixture, self.fixture["employee_mailbox"]["address"],
                                          self.fixture["from_address"], "<customer-fixture@example.test>")
        module = type("Module", (), {"IMAPClient": FakeIMAPClient})
        with patch.dict(sys.modules, {"imapclient": module}), patch("servers.company.scenarios.smtplib.SMTP") as smtp:
            RealMailTransport().deliver_fixture(
                self.fixture, {"username": "mail-user", "password": "private"}, message, digest,
                lambda: events.append("append_attempt_started"))
        self.assertEqual(events, ["connect", "login", ("select", "INBOX"), "append_attempt_started",
                                  ("append", "INBOX"), "customer@example.test"])
        smtp.assert_not_called()

    def test_concurrent_cli_attempts_claim_one_durable_run(self):
        self.fixture["forward_to_viv"] = False
        self.fixture["post_chat"] = False
        self.fixture.pop("chat_message")
        self.fixture["employee_mailbox"].pop("chat_credentials_ref")
        self.fixture["employee_mailbox"].pop("rocketchat_base_url")
        self.fixture["employee_mailbox"].pop("rocketchat_room_id")
        self.fixture["employee_mailbox"].pop("rocketchat_expected_user_id")
        self.fixture_path.write_text(json.dumps(self.fixture))
        context = multiprocessing.get_context("spawn")
        counter = context.Value("i", 0)
        first_delivery_started, release_first = context.Event(), context.Event()
        first_started, second_started, duplicate_delivery = context.Event(), context.Event(), context.Event()
        results = context.Queue()
        mail = CrossProcessMailTransport(counter, first_delivery_started, release_first, duplicate_delivery)
        first = context.Process(target=_run_scenario_process,
                                args=(str(self.fixture_path), str(self.credentials_path), str(self.database), mail, first_started, results))
        second = context.Process(target=_run_scenario_process,
                                 args=(str(self.fixture_path), str(self.credentials_path), str(self.database), mail, second_started, results))
        first.start()
        self.assertTrue(first_started.wait(timeout=3))
        if not first_delivery_started.wait(timeout=3):
            first.join(timeout=0.1)
            detail = results.get(timeout=1) if not first.is_alive() else f"process exit={first.exitcode}"
            self.fail(f"first scenario did not reach SMTP delivery: {detail}")
        second.start()
        self.assertTrue(second_started.wait(timeout=3))
        self.assertFalse(duplicate_delivery.wait(timeout=0.1))
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        self.assertEqual([outcome[1] for outcome in outcomes], ["COMPLETED", "COMPLETED"])
        self.assertEqual(len({outcome[0] for outcome in outcomes}), 1)
        self.assertEqual(counter.value, 1)
        self.assertFalse(duplicate_delivery.is_set())

    def test_credentials_file_permissions_are_required(self):
        os.chmod(self.credentials_path, 0o644)
        with self.assertRaises(PermissionError):
            self.run_it()

    def test_real_chat_transport_sends_in_employee_browser_and_verifies_native_ddp_message(self):
        self.fixture["chat_message"] = "line one\nline two"
        session = FakeChatSession()
        browser = FakeBrowserSessionProvider(session)
        message_id = session.native_message_id
        result = RealChatTransport(session, browser).post(
            self.fixture, {"username": "employee", "password": "private"}, "attempt-reference")
        self.assertEqual(result, {"message_id": message_id, "room_id": "room-safe-id", "thread_id": None,
                                  "browser_url": f"https://chat.example.test/channel/general?msg={message_id}"})
        self.assertEqual(session.message["_id"], message_id)
        self.assertEqual(session.message["msg"], self.fixture["chat_message"])
        self.assertEqual(session.typing_events, [
            ("clear",), ("type", "line one", 150, 0), ("newline", "Shift+Enter"),
            ("type", "line two", 150, 0), ("enter",),
        ])
        self.assertEqual(session.sent_message_events, 1)
        self.assertEqual(session.calls[0][2]["headers"]["X-User-Id"], "expected-employee-id")
        self.assertEqual(browser.employee_emails, ["employee@example.test"])
        page = browser.pages[0]
        self.assertEqual(len(page.sockets), 2)
        self.assertTrue(page.sockets[0].is_closed())
        self.assertFalse(page.sockets[1].is_closed())
        self.assertGreater(page.composer_waits, 0)
        self.assertTrue(any(call[1].endswith("/api/v1/rooms.info") for call in session.calls))
        self.assertFalse(any(call[1].endswith("/api/v1/users.info") for call in session.calls))
        self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_visible_typing_timeout_scales_for_long_messages(self):
        self.fixture["chat_message"] = "x" * 1200
        session = FakeChatSession()
        browser = FakeBrowserSessionProvider(session)

        RealChatTransport(session, browser).post(
            self.fixture, {"username": "employee", "password": "private"}, "long-message")

        typed = next(event for event in session.typing_events if event[0] == "type")
        self.assertEqual(typed[1], self.fixture["chat_message"])
        self.assertEqual(typed[2], 150)
        self.assertEqual(typed[3], 0)

    def test_old_identical_room_message_is_not_attributed_to_new_ddp_send(self):
        session = FakeChatSession()
        session.client_message_id = "new-client-generated-id"
        session.native_message_id = session.client_message_id
        session.historic_message = {
            "_id": "old-historic-id", "rid": "room-safe-id",
            "u": {"_id": "expected-employee-id"}, "msg": self.fixture["chat_message"],
            "ts": "2026-10-31T11:59:00Z",
        }
        browser = FakeBrowserSessionProvider(session)

        result = RealChatTransport(session, browser).post(
            self.fixture, {"username": "employee", "password": "private"}, "attempt-reference")

        self.assertEqual(result["message_id"], "new-client-generated-id")
        self.assertNotEqual(result["message_id"], session.historic_message["_id"])
        self.assertEqual(session.message["msg"], session.historic_message["msg"])
        self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_real_chat_transport_rejects_wrong_logged_in_user_before_posting(self):
        session = FakeChatSession(user_id="different-user")
        browser = FakeBrowserSessionProvider(session)
        with self.assertRaisesRegex(RuntimeError, "ROCKETCHAT_AUTHOR_MISMATCH"):
            RealChatTransport(session, browser).post(
                self.fixture, {"username": "wrong", "password": "private"}, "scenario-id")
        self.assertIsNone(session.message)

    def test_real_chat_transport_resolves_immutable_mention_id_and_records_receipt(self):
        self.fixture["mention_account_id"] = "assistant-account-id"
        session = FakeChatSession()
        browser = FakeBrowserSessionProvider(session)
        message_id = "scenario-with-mention"
        result = RealChatTransport(session, browser).post(
            self.fixture, {"username": "employee", "password": "private"}, message_id)
        lookup = next(call for call in session.calls if call[1].endswith("/api/v1/users.info"))
        self.assertEqual(lookup[2]["params"], {"userId": "assistant-account-id"})
        self.assertEqual(lookup[2]["headers"]["X-User-Id"], "expected-employee-id")
        self.assertEqual(session.message["rid"], "room-safe-id")
        self.assertTrue(session.message["msg"].startswith("@configured_assistant "))
        self.assertEqual(session.message["msg"].split(" ", 1)[1], self.fixture["chat_message"])
        self.assertEqual(result["mention_account_id"], "assistant-account-id")
        self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_real_chat_transport_rejects_mention_lookup_id_mismatch_before_posting(self):
        self.fixture["mention_account_id"] = "assistant-account-id"
        session = FakeChatSession(mention_user_id="different-account-id")
        browser = FakeBrowserSessionProvider(session)
        with self.assertRaisesRegex(RuntimeError, "ROCKETCHAT_MENTION_ACCOUNT_MISMATCH"):
            RealChatTransport(session, browser).post(
                self.fixture, {"username": "employee", "password": "private"}, "scenario-bad-mention")
        self.assertIsNone(session.message)

    def test_chat_recovery_does_not_claim_text_and_time_history_match_without_native_id(self):
        session = FakeChatSession()
        receipt = RealChatTransport(session, FakeBrowserSessionProvider(session)).confirm_message(
            self.fixture, {"username": "employee", "password": "private"}, None,
            self.fixture["chat_message"], "2026-10-31T12:00:00Z")
        self.assertIsNone(receipt)
        self.assertFalse(any(call[1].endswith("/api/v1/channels.history") for call in session.calls))

    def test_scenario_audit_persists_resolved_mention_account_id(self):
        self.fixture["mention_account_id"] = "assistant-account-id"
        self.fixture_path.write_text(json.dumps(self.fixture))
        session = FakeChatSession()
        result = run_scenario(
            self.fixture_path, self.credentials_path, self.database,
            mail_transport=self.mail,
            chat_transport=RealChatTransport(session, FakeBrowserSessionProvider(session)))
        chat_step = next(step for step in result["steps"] if step["step_key"] == "chat-post")
        self.assertEqual(chat_step["mention_account_id"], "assistant-account-id")

    def test_email_only_fixture_does_not_resolve_optional_mention_account(self):
        self.fixture["post_chat"] = False
        self.fixture.pop("chat_message")
        self.fixture["mention_account_id"] = "unavailable-account"
        self.fixture_path.write_text(json.dumps(self.fixture))
        result = run_scenario(
            self.fixture_path, self.credentials_path, self.database, mail_transport=self.mail)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(any(step["step_key"] == "chat-post" for step in result["steps"]))

    def test_existing_scenario_step_receipts_survive_additive_mention_column_migration(self):
        database = self.root / "legacy.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("""CREATE TABLE scenario_steps (
                run_id TEXT NOT NULL, step_key TEXT NOT NULL, step TEXT NOT NULL,
                status TEXT NOT NULL, external_id TEXT, delivery_method TEXT,
                submission_started INTEGER NOT NULL DEFAULT 0, mailbox_uid TEXT,
                mailbox_uidvalidity TEXT, webmail_url TEXT, reference TEXT,
                content_hash TEXT, error_type TEXT, room_id TEXT, thread_id TEXT,
                browser_url TEXT, file_references_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL, PRIMARY KEY(run_id, step_key))""")
            connection.execute(
                "INSERT INTO scenario_steps(run_id,step_key,step,status,external_id,room_id,thread_id,browser_url,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("old-run", "chat-post", STEP_CHAT_POSTED, STEP_CHAT_POSTED,
                 "old-message", "old-room", "old-thread", "https://chat.test/old", "2026-10-30T00:00:00Z"),
            )
        ScenarioStore(database)
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT external_id,room_id,thread_id,browser_url,mention_account_id "
                "FROM scenario_steps WHERE run_id='old-run' AND step_key='chat-post'").fetchone()
        self.assertEqual(row, ("old-message", "old-room", "old-thread", "https://chat.test/old", None))

    def test_smtp4dev_delivery_receipt_searches_subject_then_matches_exact_message_id_header(self):
        class ApiResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        message_id = "<outbound-exact@example.test>"
        responses = [
            ApiResponse({"results": [{"id": "other-record"}, {"id": "message-record-id"}]}),
            ApiResponse({"headers": [{"name": "Subject", "value": message_id + " in unrelated content"}]}),
            ApiResponse({"headers": [{"name": "Message-Id", "value": message_id}]})
        ]
        with patch("servers.company.scenarios.requests.get", side_effect=responses) as get:
            result = RealMailTransport().confirm_smtp4dev(self.fixture, message_id)
        self.assertEqual(result, {"mailbox_uid": "message-record-id",
                                  "reference": "smtp4dev message message-record-id",
                                  "webmail_url": "http://smtp4dev.test/#/messages/mailbox/Default/folder/INBOX/message/message-record-id"})
        self.assertEqual(get.call_args_list[0].args[0], "http://smtp4dev.test/api/Messages")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["searchTerms"], self.fixture["subject"])

    def test_imap_confirmation_matches_folded_id_after_idle_and_rejects_other_candidate(self):
        events = []

        class FakeIMAPClient:
            def __init__(self, *args, **kwargs):
                events.append(("connect", kwargs["ssl_context"].check_hostname))
                self.search_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def login(self, username, password):
                events.append(("login", username))

            def select_folder(self, name, readonly=True):
                return {b"UIDVALIDITY": 987}

            def search(self, criteria):
                self.search_count += 1
                return [] if self.search_count == 1 else [41, 42]

            def idle(self):
                events.append(("idle",))

            def idle_check(self, timeout):
                events.append(("wait", timeout))
                return [(1, b"EXISTS")]

            def idle_done(self):
                events.append(("idle_done",))

            def fetch(self, uids, fields):
                uid = uids[0]
                header = (b"Message-ID: <other@example.test>\r\n\r\n" if uid == 41 else
                          b"Message-ID:\r\n <exact@example.test>\r\n\r\n")
                return {uid: {b"BODY[HEADER.FIELDS (MESSAGE-ID)]": header}}

        module = type("Module", (), {"IMAPClient": FakeIMAPClient})
        self.fixture["confirm_timeout_seconds"] = 3
        with patch.dict(sys.modules, {"imapclient": module}):
            result = RealMailTransport().confirm_message(
                self.fixture, {"username": "mail-user", "password": "private"}, "<exact@example.test>")
        self.assertEqual(result, {"mailbox_uid": "42", "reference": "INBOX UID 42",
                                  "webmail_url": "https://mail.example.test/webmail?_task=mail&_mbox=INBOX&_uid=42&_action=show",
                                  "uidvalidity": "987"})
        self.assertIn(("idle",), events)
        self.assertIn(("idle_done",), events)

    def test_run_persists_only_actor_ids_and_mailbox_address_snapshot(self):
        self.fixture["employee_id"] = "employee-id-123"
        self.fixture["employee_mailbox"]["mailbox_account_id"] = "mailbox-id-456"
        self.fixture_path.write_text(json.dumps(self.fixture))
        result = self.run_it()
        actors = json.loads(result["actors"])
        self.assertEqual(actors, {
            "chat_account_id": "expected-employee-id",
            "employee_id": "employee-id-123",
            "mailbox_account_id": "mailbox-id-456",
            "mailbox_address": "employee@example.test",
        })
        self.assertNotIn("mail-password", result["actors"])
        self.assertNotIn("Monthly statement", result["actors"])

    def test_midpoint_exact_name_resolution_sets_authoritative_employee_mailbox_and_chat_ids(self):
        central_oid = "midpoint-oid-123"
        central_email = "sarah.johnson@example.com"
        mail_account_id = "sarah.johnson@example.com"
        central_chat_uid = "rocketchat-uid-456"
        mail_shadow_oid = "mail-shadow-123"
        chat_shadow_oid = "chat-shadow-456"
        owncloud_shadow_oid = "owncloud-shadow-789"
        owncloud_session = WebDAVSession()
        self.fixture["directory"] = {
            "mode": "midpoint",
            "base_url": "https://midpoint.example.test",
            "credentials_ref": "midpoint-directory",
        }
        self.fixture["employee_id"] = "Sarah Johnson"
        self.fixture["mention_account_id"] = "assistant-account-id"
        self.fixture["owncloud"] = {
            "base_url": "https://owncloud.example.test",
            "file_references": [{
                "reference_id": "invoice-source",
                "owner_employee_id": "Sarah Johnson",
                "path": "Documents/statement final.pdf",
                "credentials_ref": "owncloud-sarah",
            }],
        }
        self.fixture["employee_mailbox"].pop("address")
        self.fixture["employee_mailbox"].pop("rocketchat_expected_user_id")
        self.fixture_path.write_text(json.dumps(self.fixture))

        class DirectoryResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def directory_get(url, **kwargs):
            if url.endswith("/ws/rest/users"):
                return DirectoryResponse({"object": {"object": [
                    {"oid": central_oid, "name": "Sarah Johnson", "emailAddress": central_email,
                     "linkRef": [{"oid": mail_shadow_oid}, {"oid": chat_shadow_oid},
                                 {"oid": owncloud_shadow_oid}]},
                    {"oid": "similar-name-oid", "name": "Sarah Johnson Smith",
                     "linkRef": []},
                ]}})
            if url.endswith(f"/ws/rest/shadows/{mail_shadow_oid}"):
                return DirectoryResponse({"shadow": {
                    "oid": mail_shadow_oid,
                    "resourceRef": {"oid": "57a19684-cfe8-4f21-b7b4-0fb02b5ec002"},
                    "attributes": {"email": mail_account_id, "_id": mail_account_id},
                }})
            if url.endswith(f"/ws/rest/shadows/{chat_shadow_oid}"):
                return DirectoryResponse({"shadow": {
                    "oid": chat_shadow_oid,
                    "resourceRef": {"oid": "57a19684-cfe8-4f21-b7b4-0fb02b5ec001"},
                    "attributes": {"_id": central_chat_uid, "username": "sarah.j"},
                }})
            if url.endswith(f"/ws/rest/shadows/{owncloud_shadow_oid}"):
                return DirectoryResponse({"shadow": {
                    "oid": owncloud_shadow_oid,
                    "resourceRef": {"oid": "57a19684-cfe8-4f21-b7b4-0fb02b5ec003"},
                    "attributes": {"_id": "sarah_johnson"},
                }})
            if url.endswith(f"/ws/rest/shadows/{owncloud_shadow_oid}/owner"):
                return DirectoryResponse({"user": {"oid": central_oid}})
            raise AssertionError("unexpected directory endpoint")

        chat_session = FakeChatSession(user_id=central_chat_uid, mention_user_id="assistant-account-id")
        chat_browser = FakeBrowserSessionProvider(chat_session)
        with patch("servers.company.scenarios.requests.get", side_effect=directory_get) as get:
            result = run_scenario(self.fixture_path, self.credentials_path, self.database,
                                  mail_transport=self.mail,
                                  chat_transport=RealChatTransport(chat_session, chat_browser),
                                  owncloud_session=owncloud_session)
        actors = json.loads(result["actors"])
        self.assertEqual(actors["employee_id"], "Sarah Johnson")
        self.assertEqual(actors["midpoint_oid"], central_oid)
        self.assertEqual(actors["mailbox_account_id"], mail_account_id)
        self.assertEqual(actors["mailbox_address"], mail_account_id)
        self.assertEqual(actors["chat_account_id"], central_chat_uid)
        self.assertEqual(self.mail.deliveries[0][3], mail_account_id)
        self.assertEqual([call[0] for call in owncloud_session.calls], ["PROPFIND", "GET", "PROPFIND"])
        method, request_url, _kwargs = owncloud_session.calls[0]
        self.assertEqual(method, "PROPFIND")
        self.assertIn("sarah_johnson/Documents/statement%20final.pdf", request_url)
        source_attachment = next(
            part for part in self.mail.delivered_messages[0].iter_attachments()
            if part.get("X-TAC-OwnCloud-Reference-ID") == "invoice-source")
        self.assertEqual(source_attachment.get_payload(decode=True), b"source statement v7")
        self.assertEqual(source_attachment["X-TAC-OwnCloud-Reference-ID"], "invoice-source")
        self.assertEqual(source_attachment["X-TAC-OwnCloud-FileID"], "82415")
        self.assertEqual(source_attachment["X-TAC-OwnCloud-ETag"], '"v7-55a"')
        self.assertTrue(chat_session.message["msg"].startswith("@configured_assistant "))
        chat_step = next(step for step in result["steps"] if step["step_key"] == "chat-post")
        mail_step = next(step for step in result["steps"] if step["step_key"] == "mail-delivery")
        self.assertEqual(chat_step["file_references"], mail_step["file_references"])
        self.assertEqual(chat_step["file_references"][0]["file_id"], "82415")
        self.assertEqual(chat_step["mention_account_id"], "assistant-account-id")
        self.assertEqual(get.call_args_list[0].args[0], "https://midpoint.example.test/ws/rest/users")
        self.assertEqual(get.call_args_list[0].kwargs["auth"], ("directory-user", "directory-password"))
        self.assertEqual(chat_session.message["rid"], "room-safe-id")
        self.assertIn("/webmail?_task=mail", chat_session.message["msg"])
        self.assertIn("file ID 82415", chat_session.message["msg"])
        self.assertFalse(any(call[0] == "POST" for call in chat_session.calls))

        central_email = "contact.updated@example.com"
        resumed_chat_session = FakeChatSession(user_id=central_chat_uid, mention_user_id="assistant-account-id")
        resumed_chat_browser = FakeBrowserSessionProvider(resumed_chat_session)
        with patch("servers.company.scenarios.requests.get", side_effect=directory_get):
            resumed = run_scenario(self.fixture_path, self.credentials_path, self.database,
                                   run_id=result["id"], mail_transport=self.mail,
                                   chat_transport=RealChatTransport(resumed_chat_session, resumed_chat_browser),
                                   owncloud_session=WebDAVSession())
        self.assertEqual(resumed["id"], result["id"])
        self.assertEqual(len(self.mail.deliveries), 1)
        self.assertFalse(any(call[1].endswith("/api/v1/chat.sendMessage") for call in resumed_chat_session.calls))

        central_chat_uid = "changed-native-chat-id"
        with patch("servers.company.scenarios.requests.get", side_effect=directory_get):
            with self.assertRaisesRegex(ValueError, "does not match the requested run"):
                run_scenario(self.fixture_path, self.credentials_path, self.database,
                             run_id=result["id"], mail_transport=self.mail,
                             chat_transport=RealChatTransport(FakeChatSession(user_id=central_chat_uid)),
                             owncloud_session=WebDAVSession())
        self.assertEqual(len(self.mail.deliveries), 1)

    def test_midpoint_http_failure_stops_before_any_mail_delivery(self):
        self.fixture["directory"] = {
            "mode": "midpoint",
            "base_url": "https://midpoint.example.test",
            "credentials_ref": "midpoint-directory",
        }
        self.fixture["employee_id"] = "Sarah Johnson"
        self.fixture_path.write_text(json.dumps(self.fixture))

        class FailedDirectoryResponse:
            def raise_for_status(self):
                raise RuntimeError("synthetic directory HTTP failure")

        with patch("servers.company.scenarios.requests.get", return_value=FailedDirectoryResponse()):
            with self.assertRaisesRegex(RuntimeError, "directory HTTP failure"):
                self.run_it()
        self.assertEqual(self.mail.deliveries, [])
        self.assertFalse(self.database.exists())

    def test_midpoint_single_native_link_delivers_mail_only_using_email_as_mailu_uid(self):
        self.fixture["directory"] = {
            "mode": "midpoint",
            "base_url": "https://midpoint.example.test",
            "credentials_ref": "midpoint-directory",
        }
        self.fixture["employee_id"] = "Sarah Johnson"
        self.fixture["forward_to_viv"] = False
        self.fixture["post_chat"] = False
        self.fixture["employee_mailbox"].pop("address")
        self.fixture_path.write_text(json.dumps(self.fixture))

        class DirectoryResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def directory_get(url, **kwargs):
            if url.endswith("/ws/rest/users"):
                return DirectoryResponse({"object": {"object": [
                    {"oid": "midpoint-sarah-oid", "name": "Sarah Johnson",
                     "linkRef": {"oid": "mail-shadow-only"}},
                ]}})
            if url.endswith("/ws/rest/shadows/mail-shadow-only"):
                return DirectoryResponse({"shadow": {
                    "oid": "mail-shadow-only",
                    "resourceRef": {"oid": "57a19684-cfe8-4f21-b7b4-0fb02b5ec002"},
                    "attributes": {"email": "sarah.johnson@example.com"},
                }})
            raise AssertionError("unexpected directory endpoint")

        with patch("servers.company.scenarios.requests.get", side_effect=directory_get):
            result = run_scenario(self.fixture_path, self.credentials_path, self.database,
                                  mail_transport=self.mail)
        actors = json.loads(result["actors"])
        self.assertEqual(actors["employee_id"], "Sarah Johnson")
        self.assertEqual(actors["midpoint_oid"], "midpoint-sarah-oid")
        self.assertEqual(actors["mailbox_address"], "sarah.johnson@example.com")
        self.assertEqual(actors["mailbox_account_id"], "sarah.johnson@example.com")
        self.assertEqual(self.mail.deliveries[0][3], "sarah.johnson@example.com")

    def test_roundcube_message_url_preserves_base_path_and_uses_native_message_parameters(self):
        url = _roundcube_message_url("https://mail.example.test/webmail/?_task=login", "17")
        self.assertEqual(url, "https://mail.example.test/webmail/?_task=mail&_mbox=INBOX&_uid=17&_action=show")


if __name__ == "__main__":
    unittest.main()
