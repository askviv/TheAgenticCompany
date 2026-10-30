from servers.simulation.provider_mail_observer import RegisteredProviderMailObserver


def test_observer_uses_only_registered_thread_and_durable_case_cursor():
    class Portal:
        def __init__(self):
            self.submitted = []

        def registered_mail_cases(self):
            return [{
                "case_id": "case-1",
                "provider_id": "tau2-banking",
                "provider_actor_id": "bank-provider",
                "responsible_actor_id": "employee-1",
                "root_message_id": "<root@company.test>",
                "after_uid": 12,
                "uidvalidity": 44,
            }]

        def process_registered_email(self, message):
            self.submitted.append(message)
            return {
                "status": "processed",
                "case_id": "case-1",
                "message_id": message["message_id"],
                "reply_message_id": "<reply@company.test>",
            }

    class Gateway:
        def __init__(self):
            self.calls = []

        def poll_registered_replies(self, mailbox, roots, after_uid, senders, *, expected_uidvalidity):
            self.calls.append((mailbox["actor_id"], roots, after_uid, senders, expected_uidvalidity))
            return [{
                "message_id": "<inbound@provider.test>",
                "sender_actor_id": "viv",
                "recipient_actor_id": "bank-provider",
                "in_reply_to": "<root@company.test>",
                "references": "<root@company.test>",
                "subject": "Re: case",
                "body": "Private reply body",
                "received_at": "2026-10-30T09:01:00+00:00",
                "mailbox_uid": 13,
                "uidvalidity": 44,
            }]

    portal = Portal()
    gateway = Gateway()
    observer = RegisteredProviderMailObserver(
        portal,
        gateway,
        [
            {"actor_id": "bank-provider", "email": "bank@example.test"},
            {"actor_id": "viv", "email": "agent@agent.example.test"},
        ],
        "viv",
    )

    result = observer.observe()

    assert result == {"status": "ok", "processed": 1, "failures": []}
    assert gateway.calls == [(
        "bank-provider", {"<root@company.test>"}, 12, {"employee-1", "viv"}, "44"
    )]
    assert portal.submitted[0]["mailbox_uid"] == 13
    assert portal.submitted[0]["uidvalidity"] == 44
    assert portal.submitted[0]["body"] == "Private reply body"


def test_observer_reports_provider_failure_and_continues_other_mailbox():
    class Portal:
        def registered_mail_cases(self):
            return [
                {"case_id": "case-1", "provider_actor_id": "bad-provider", "responsible_actor_id": "employee-1", "root_message_id": "<a@x.test>", "after_uid": 0, "uidvalidity": None},
                {"case_id": "case-2", "provider_actor_id": "good-provider", "responsible_actor_id": "employee-2", "root_message_id": "<b@x.test>", "after_uid": 0, "uidvalidity": None},
            ]

        def process_registered_email(self, message):
            return {"status": "processed", "case_id": "case-2", "message_id": message["message_id"], "reply_message_id": "<r@x.test>"}

    class Gateway:
        def poll_registered_replies(self, mailbox, *_args, **_kwargs):
            if mailbox["actor_id"] == "bad-provider":
                raise ConnectionError("private transport detail")
            return [{
                "message_id": "<reply@x.test>", "sender_actor_id": "viv",
                "recipient_actor_id": "good-provider", "in_reply_to": "<b@x.test>",
                "references": "<b@x.test>", "subject": "Re", "body": "reply",
                "received_at": "2026-10-30T09:00:00+00:00", "mailbox_uid": 3, "uidvalidity": 1,
            }]

    observer = RegisteredProviderMailObserver(
        Portal(), Gateway(),
        [
            {"actor_id": "bad-provider", "email": "bad@example.test"},
            {"actor_id": "good-provider", "email": "good@example.test"},
            {"actor_id": "viv", "email": "agent@agent.example.test"},
        ],
        "viv",
    )

    result = observer.observe()

    assert result == {
        "status": "partial",
        "processed": 1,
        "failures": [{"provider_actor_id": "bad-provider", "error_code": "ConnectionError"}],
    }


def test_ambiguous_message_referencing_two_cases_is_never_submitted():
    class Portal:
        def registered_mail_cases(self):
            return [
                {"case_id": "case-1", "provider_id": "tau2-banking", "provider_actor_id": "provider", "responsible_actor_id": "employee", "root_message_id": "<a@x.test>", "after_uid": 0, "uidvalidity": None},
                {"case_id": "case-2", "provider_id": "tau2-airline", "provider_actor_id": "provider", "responsible_actor_id": "employee", "root_message_id": "<b@x.test>", "after_uid": 0, "uidvalidity": None},
            ]

        def process_registered_email(self, _message):
            raise AssertionError("ambiguous message must not reach the native processor")

    class Gateway:
        def poll_registered_replies(self, _mailbox, roots, *_args, **_kwargs):
            root = next(iter(roots))
            other = "<b@x.test>" if root == "<a@x.test>" else "<a@x.test>"
            return [{
                "message_id": "<ambiguous@x.test>", "sender_actor_id": "viv",
                "recipient_actor_id": "provider", "in_reply_to": root,
                "references": f"{root} {other}", "subject": "Re", "body": "reply",
                "received_at": "2026-10-30T09:00:00+00:00", "mailbox_uid": "3", "uidvalidity": "1",
            }]

    observer = RegisteredProviderMailObserver(
        Portal(), Gateway(),
        [
            {"actor_id": "provider", "email": "provider@example.test"},
            {"actor_id": "viv", "email": "agent@agent.example.test"},
        ],
        "viv",
    )

    result = observer.observe()

    assert result["status"] == "partial"
    assert result["processed"] == 0
    assert result["failures"] == [{
        "provider_actor_id": "provider",
        "error_code": "ambiguous_registered_email_thread",
    }]
