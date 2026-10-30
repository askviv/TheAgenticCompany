"""Bridge delivered, exact-thread provider replies to the durable portal workflow."""

from __future__ import annotations

import re
import logging
import traceback
from typing import Any

from .provider_portal_client import ProviderPortalClient, ProviderPortalClientError


LOG = logging.getLogger(__name__)


class RegisteredProviderMailObserver:
    def __init__(
        self,
        portal: ProviderPortalClient,
        gateway: Any,
        mail_actors: list[dict[str, Any]],
        viv_actor_id: str,
    ):
        self.portal = portal
        self.gateway = gateway
        self.mail_actors = {actor["actor_id"]: actor for actor in mail_actors}
        self.viv_actor_id = viv_actor_id
        if viv_actor_id not in self.mail_actors:
            raise ValueError("Viv mailbox identity is absent from the configured mail directory")

    def observe(self) -> dict[str, Any]:
        try:
            cases = self.portal.registered_mail_cases()
        except Exception as error:
            self._log_failure("list_registered_cases", None, None, error)
            raise
        processed = 0
        failures: list[dict[str, str]] = []
        case_by_root = {case["root_message_id"]: case for case in cases}
        discovered: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for case in cases:
            provider_actor_id = case["provider_actor_id"]
            mailbox = self.mail_actors.get(provider_actor_id)
            if mailbox is None:
                failures.append({
                    "provider_actor_id": provider_actor_id,
                    "error_code": "provider_mailbox_not_configured",
                })
                continue
            try:
                messages = self.gateway.poll_registered_replies(
                    mailbox,
                    {case["root_message_id"]},
                    case["after_uid"],
                    {self.viv_actor_id, case["responsible_actor_id"]},
                    expected_uidvalidity=(
                        str(case["uidvalidity"]) if case["uidvalidity"] is not None else None
                    ),
                )
                for message in messages:
                    root_ids = {
                        message["in_reply_to"],
                        *re.findall(r"<[^<>\s]+>", message["references"]),
                    }
                    related_cases = {
                        case_by_root[root_id]["case_id"]
                        for root_id in root_ids
                        if root_id in case_by_root
                    }
                    if related_cases != {case["case_id"]}:
                        failure = {
                            "provider_actor_id": provider_actor_id,
                            "error_code": "ambiguous_registered_email_thread",
                        }
                        if failure not in failures:
                            failures.append(failure)
                        self._log_failure(
                            "ambiguous_thread", case["case_id"],
                            message.get("message_id"),
                            ValueError("message references zero or multiple registered cases"),
                        )
                        continue
                    reply = {
                        "provider_actor_id": provider_actor_id,
                        "message_id": message["message_id"],
                        "sender_actor_id": message["sender_actor_id"],
                        "recipient_actor_id": message["recipient_actor_id"],
                        "in_reply_to": message["in_reply_to"],
                        "references": re.findall(r"<[^<>\s]+>", message["references"]),
                        "subject": message["subject"],
                        "body": message["body"],
                        "received_at": message["received_at"],
                        "mailbox_uid": int(message["mailbox_uid"]),
                        "uidvalidity": int(message["uidvalidity"]),
                    }
                    prior = discovered.get(message["message_id"])
                    if prior is not None and prior[0]["case_id"] != case["case_id"]:
                        raise ProviderPortalClientError(None, "ambiguous_registered_email_thread")
                    discovered[message["message_id"]] = (case, reply)
            except Exception as error:
                code = getattr(error, "code", None)
                failure = {
                    "provider_actor_id": provider_actor_id,
                    "error_code": code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else type(error).__name__,
                }
                if failure not in failures:
                    failures.append(failure)
                self._log_failure("poll_registered_mail", case["case_id"], None, error)
        for message_id, (case, reply) in discovered.items():
            try:
                receipt = self.portal.process_registered_email(reply)
                if receipt["case_id"] != case["case_id"]:
                    raise ProviderPortalClientError(None, "mail_case_receipt_mismatch")
                processed += 1
            except Exception as error:
                code = getattr(error, "code", None)
                failure = {
                    "provider_actor_id": case["provider_actor_id"],
                    "error_code": code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else type(error).__name__,
                }
                if failure not in failures:
                    failures.append(failure)
                self._log_failure("process_registered_mail", case["case_id"], message_id, error)
        return {
            "status": "partial" if failures else "ok",
            "processed": processed,
            "failures": failures,
        }

    @staticmethod
    def _log_failure(operation: str, case_id: str | None, message_id: str | None,
                     error: BaseException) -> None:
        frames = [(frame.filename, frame.lineno, frame.name)
                  for frame in traceback.extract_tb(error.__traceback__)]
        causes = []
        cause = error.__cause__ or error.__context__
        while cause is not None:
            causes.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        LOG.error(
            "registered provider mail failed operation=%s case_id=%s message_id=%s "
            "exception_type=%s causes=%s stack=%s",
            operation, case_id, message_id, type(error).__name__, causes, frames,
        )
