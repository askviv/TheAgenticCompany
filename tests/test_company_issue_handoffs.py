"""Issue-to-mail integration at the SMTP/IMAP and native scheduler boundaries."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from servers.simulation.issue_handoffs import (
    IssueHandoffService, IssueHandoffStore, Stage, SystemdIssueScheduler,
)
from servers.simulation.store import SQLiteStore

KEY = "a" * 64
ACTORS = [{"actor_id": "logistics", "email": "employee@example.test", "kind": "logistics"},
          {"actor_id": "viv", "email": "agent@agent.example.test", "kind": "agent"}]
DELIVERED = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
RECORD = {
    "issue_key": KEY, "issue_name": "ISS-1", "email_queue": "QUEUE-1", "message_id": "<native-1@example.test>",
    "subject": "Shipment disruption", "recipient_email": "employee@example.test", "status": "Sent",
    "business_description": "A transport disruption has affected the order's delivery lead time.",
    "requested_outcome": "Review the delivery commitment and invoice implications.",
    "affected_orders": [{"doctype": "Purchase Order", "name": "PO-001"}],
}


class Mail:
    def __init__(self):
        self.seen = False
        self.sent = []
        self.receipts = {}
        self.before_submit = None
        self.fail_before_submit = False
        self.fail_after_submit = False

    def inspect_issue_notification(self, actor, message_id):
        assert actor == ACTORS[0]
        assert message_id == RECORD["message_id"]
        return {"message_id": message_id, "received_at": DELIVERED.isoformat(), "seen": self.seen,
                "mailbox_uid": "42", "uidvalidity": "7", "folder": "INBOX", "reference": "INBOX UID 42"}

    def send(self, sender, recipient, subject, body, message_id, run_id, generation, **kwargs):
        if self.fail_before_submit:
            self.fail_before_submit = False
            raise ConnectionError("SMTP connection unavailable before submission")
        if self.before_submit:
            self.before_submit()
        kwargs["on_submission_started"]()
        self.sent.append({"sender": sender, "recipient": recipient, "subject": subject, "body": body,
                          "message_id": message_id, "run_id": run_id})
        receipt = {"message_id": message_id, "reference": "captured SMTP delivery"}
        self.receipts[message_id] = receipt
        if self.fail_after_submit:
            raise ConnectionError("Receipt connection interrupted after submission")
        return receipt

    def find_receipt(self, _recipient, message_id, *_args):
        return self.receipts.get(message_id)


def service(path, mail, clock, scheduler=None):
    return IssueHandoffService(IssueHandoffStore(SQLiteStore(path)), mail, scheduler or Mock(),
                              actors=ACTORS, erp_public_url="https://erp.example.test", now=lambda: clock[0])


def test_five_real_minutes_from_mail_delivery_then_one_employee_handoff_across_restart(tmp_path):
    clock = [DELIVERED + timedelta(seconds=299)]
    mail = Mail(); scheduler = Mock(); path = tmp_path / "state.sqlite"
    first = service(path, mail, clock, scheduler)
    first.register([RECORD]); first.process(KEY)
    assert mail.sent == []
    scheduler.schedule.assert_called_once_with(KEY, DELIVERED + timedelta(minutes=5))
    clock[0] += timedelta(seconds=1)
    restarted = service(path, mail, clock)
    restarted.process(KEY); restarted.process(KEY)
    assert len(mail.sent) == 1
    assert mail.sent[0]["sender"] == ACTORS[0]
    assert mail.sent[0]["recipient"] == ACTORS[1]
    assert mail.sent[0]["body"].startswith("Please handle this,")
    assert "https://erp.example.test/app/purchase-order/PO-001" in mail.sent[0]["body"]
    assert RECORD["requested_outcome"] in mail.sent[0]["body"]
    assert restarted.store.get(KEY)["stage"] == Stage.SENT
    activity = restarted.activity()
    assert [event["event"] for event in activity] == ["ISSUE_NOTIFICATION_DELIVERED", "ISSUE_HANDED_TO_VIV"]
    assert activity[1]["actor"] == "logistics"


def test_read_before_deadline_is_terminal_even_if_later_marked_unread(tmp_path):
    clock = [DELIVERED + timedelta(minutes=4)]
    mail = Mail(); path = tmp_path / "state.sqlite"
    workflow = service(path, mail, clock); workflow.register([RECORD])
    mail.seen = True; workflow.process(KEY)
    mail.seen = False; clock[0] += timedelta(minutes=2)
    service(path, mail, clock).process(KEY)
    assert workflow.store.get(KEY)["stage"] == Stage.READ
    assert mail.sent == []


def test_read_at_smtp_boundary_skips_handoff(tmp_path):
    clock = [DELIVERED + timedelta(minutes=5)]
    mail = Mail(); workflow = service(tmp_path / "state.sqlite", mail, clock)
    workflow.register([RECORD]); mail.before_submit = lambda: setattr(mail, "seen", True)
    workflow.process(KEY)
    assert mail.sent == []
    assert workflow.store.get(KEY)["stage"] == Stage.READ


def test_uncertain_submission_reconciles_original_receipt_without_duplicate(tmp_path):
    clock = [DELIVERED + timedelta(minutes=5)]
    mail = Mail(); mail.fail_after_submit = True; path = tmp_path / "state.sqlite"
    workflow = service(path, mail, clock); workflow.register([RECORD])
    with pytest.raises(ConnectionError): workflow.process(KEY)
    assert workflow.store.get(KEY)["stage"] == Stage.UNCERTAIN
    service(path, mail, clock).process(KEY)
    assert len(mail.sent) == 1
    assert workflow.store.get(KEY)["stage"] == Stage.SENT


def test_definite_pre_submission_failure_can_retry(tmp_path):
    clock = [DELIVERED + timedelta(minutes=5)]
    mail = Mail(); mail.fail_before_submit = True
    workflow = service(tmp_path / "state.sqlite", mail, clock); workflow.register([RECORD])
    with pytest.raises(ConnectionError): workflow.process(KEY)
    assert workflow.store.get(KEY)["stage"] == Stage.WAITING
    workflow.process(KEY)
    assert len(mail.sent) == 1


def test_untracked_company_mail_is_not_inspected(tmp_path):
    mail = Mail(); mail.inspect_issue_notification = Mock(side_effect=AssertionError("untracked mail accessed"))
    workflow = service(tmp_path / "state.sqlite", mail, [DELIVERED])
    assert workflow.process_pending() == []
    mail.inspect_issue_notification.assert_not_called()


def test_native_systemd_timer_uses_real_deadline_and_private_config_reference():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=3)
    scheduler = SystemdIssueScheduler(Path("/private/runtime.json"), executable="/venv/bin/python",
                                     source_root=Path("/source"), state_root=Path("/private/state"), runner=run)
    scheduler.schedule(KEY, DELIVERED + timedelta(minutes=5))
    assert "--on-calendar=2026-10-30 10:05:00 UTC" in calls[1]
    assert "--timer-property=AccuracySec=1s" in calls[1]
    assert calls[1][-4:] == ["--config", "/private/runtime.json", "--issue-key", KEY]


def test_concurrent_deadline_workers_submit_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    clock = [DELIVERED + timedelta(minutes=5)]
    mail = Mail(); path = tmp_path / "state.sqlite"
    first = service(path, mail, clock); first.register([RECORD])
    second = service(path, mail, clock)
    boundary = Barrier(2)
    mail.before_submit = lambda: boundary.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(worker.process, KEY) for worker in (first, second)]
        for result in results:
            result.result(timeout=10)
    assert len(mail.sent) == 1
    assert first.store.get(KEY)["stage"] == Stage.SENT


def test_uncertain_submission_without_receipt_is_visible_and_not_resent(tmp_path):
    clock = [DELIVERED + timedelta(minutes=5)]
    mail = Mail(); mail.fail_after_submit = True; path = tmp_path / "state.sqlite"
    workflow = service(path, mail, clock); workflow.register([RECORD])
    with pytest.raises(ConnectionError):
        workflow.process(KEY)
    mail.receipts.clear()
    restarted = service(path, mail, clock)
    restarted.process(KEY)
    assert len(mail.sent) == 1
    assert restarted.store.get(KEY)["stage"] == Stage.UNCERTAIN
    assert restarted.store.get(KEY)["last_error"] == "ConnectionError"


def test_concurrent_native_timer_creation_is_idempotent_but_other_failures_propagate():
    import subprocess

    def runner(active_after_failure):
        checks = iter([3, active_after_failure])
        def run(args, **kwargs):
            if args[0] == "systemctl":
                return SimpleNamespace(returncode=next(checks))
            raise subprocess.CalledProcessError(1, args)
        return run
    def scheduler(active_after_failure):
        return SystemdIssueScheduler(Path("/private/runtime.json"), executable="/venv/bin/python",
                                     source_root=Path("/source"), state_root=Path("/state"),
                                     runner=runner(active_after_failure))
    scheduler(0).schedule(KEY, DELIVERED + timedelta(minutes=5))
    with pytest.raises(subprocess.CalledProcessError):
        scheduler(3).schedule(KEY, DELIVERED + timedelta(minutes=5))
