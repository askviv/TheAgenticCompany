"""Native CUA actions in the employee's streamed Roundcube session."""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlsplit

from .employee_cua import EmployeeCuaAgent


class EmployeeWebmailError(RuntimeError):
    pass


class EmployeeWebmailSender:
    """Drive the real UI with CUA; the mail gateway owns native receipt checks."""

    def __init__(self, employee_desktops: Any, webmail_url: str):
        self.employee_desktops = employee_desktops
        self.webmail_url = webmail_url
        self.cua = EmployeeCuaAgent(employee_desktops)

    def _identity(self, email: str, username: str) -> str:
        return (f"Use the employee's Chrome window at {self.webmail_url}. "
                f"The account must be {email}; sign in as {username} if necessary using "
                "type_credential for the private password. Keep the browser full screen. "
                "Use native CUA mouse and keyboard actions, never shell, scripts, clipboard or backend sends. ")

    def send(self, employee_id: str, sender_email: str, sender_username: str,
             sender_password: str, recipient_email: str, subject: str, body: str,
             message_id: str, run_id: str, generation: int, *,
             in_reply_to: str | None, reply_message_url: str | None,
             verify_reply_source: Callable[[], None] | None,
             reply_to: str | None, automated: bool,
             on_submission_started: Callable[[], None],
             require_reply_to: str | None = None,
             on_submission_completed: Callable[[], None] | None = None) -> None:
        # Routing, idempotency and evidence stay with the existing mail gateway.
        del message_id, run_id, generation, automated, in_reply_to
        if reply_message_url:
            self._validate_same_origin(reply_message_url)
        instruction = self._identity(sender_email, sender_username)
        if reply_message_url:
            instruction += (f"Open this exact source message: {reply_message_url}. "
                            "Read it and use Roundcube's native Reply action to preserve its thread. ")
        else:
            instruction += "Use Roundcube's native Compose action. "
        instruction += (
            "Prepare and send exactly one email with these immutable fields: "
            + json.dumps({"from": sender_email, "to": recipient_email, "subject": subject,
                          "body": body, "reply_to": require_reply_to or reply_to}, ensure_ascii=False)
            + ". Use plain-text composition, clear any default signature or quoted text, and type "
              "the body exactly with native keystrokes at twice normal human speed. "
              "Do not add a signature, alter the content or submit more than once. "
              "Use the visible Send action for the current layout. "
        )
        with self.employee_desktops.employee_activity(employee_id):
            if verify_reply_source:
                verify_reply_source()
            with self.cua.session(employee_id, credentials={"username": sender_username,
                                                          "password": sender_password}) as action:
                def submitting():
                    if verify_reply_source:
                        verify_reply_source()
                    on_submission_started()
                action.run(instruction, on_submission_started=submitting)
            if on_submission_completed:
                on_submission_completed()

    def open_inbox(self, employee_id: str, email: str, username: str, password: str,
                   *, session: dict[str, Any] | None = None) -> None:
        with self.employee_desktops.employee_action_lock(employee_id):
            if session is None:
                self.employee_desktops.ensure_session(employee_id)
            with self.cua.session(employee_id, credentials={"username": username, "password": password}) as action:
                action.run(self._identity(email, username) +
                           "Open the Inbox and check for new mail. Leave the Inbox visible; do not send or open a message.")

    def refresh_inbox(self, employee_id: str, email: str, username: str, password: str,
                      *, session: dict[str, Any]) -> None:
        self.open_inbox(employee_id, email, username, password, session=session)

    def open_message(self, employee_id: str, email: str, username: str, password: str,
                     message_url: str) -> None:
        self._validate_same_origin(message_url)
        with self.employee_desktops.employee_activity(employee_id):
            with self.cua.session(employee_id, credentials={"username": username, "password": password}) as action:
                action.run(self._identity(email, username) +
                           f"Open and read this exact email, leaving it visible: {message_url}. Do not send a reply.")

    def _validate_same_origin(self, url: str) -> None:
        expected, actual = urlsplit(self.webmail_url), urlsplit(url)
        if (actual.scheme, actual.hostname, actual.port) != (expected.scheme, expected.hostname, expected.port):
            raise EmployeeWebmailError("Employee message URL is outside the configured webmail origin")
