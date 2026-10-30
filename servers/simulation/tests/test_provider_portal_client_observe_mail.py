from unittest.mock import Mock

import pytest

from servers.simulation.provider_portal_client import (
    ProviderPortalClient,
    ProviderPortalClientError,
)


def test_registered_mail_client_reads_thread_cursor_and_posts_exact_reply():
    session = Mock()
    cases_response = Mock(status_code=200)
    cases_response.json.return_value = {"cases": [{
        "case_id": "case-1",
        "provider_id": "tau2-banking",
        "provider_actor_id": "bank-provider",
        "responsible_actor_id": "employee-1",
        "root_message_id": "<root@company.test>",
        "after_uid": 9,
        "uidvalidity": 31,
    }]}
    receipt_response = Mock(status_code=200)
    receipt_response.json.return_value = {
        "status": "processed",
        "case_id": "case-1",
        "message_id": "<reply@provider.test>",
        "reply_message_id": "<native-reply@company.test>",
    }
    session.get.return_value = cases_response
    session.post.return_value = receipt_response
    client = ProviderPortalClient("http://portal.internal", "private-token", session=session)

    cases = client.registered_mail_cases()
    reply = {
        "provider_actor_id": "bank-provider",
        "message_id": "<reply@provider.test>",
        "sender_actor_id": "viv",
        "recipient_actor_id": "bank-provider",
        "in_reply_to": "<root@company.test>",
        "references": ["<root@company.test>"],
        "subject": "Re: case",
        "body": "Native response text",
        "received_at": "2026-10-30T09:00:00+00:00",
        "mailbox_uid": 10,
        "uidvalidity": 31,
    }
    receipt = client.process_registered_email(reply)

    assert cases[0]["after_uid"] == 9
    assert receipt["status"] == "processed"
    session.get.assert_called_once_with(
        "http://portal.internal/internal/registered-mail-cases",
        headers={"Authorization": "Bearer private-token"},
        timeout=15,
    )
    session.post.assert_called_once_with(
        "http://portal.internal/internal/observe-mail",
        json=reply,
        headers={"Authorization": "Bearer private-token"},
        timeout=15,
    )


@pytest.mark.parametrize("payload", [
    {"status": "partial", "case_id": "case-1", "message_id": "<reply@provider.test>", "reply_message_id": "<native@company.test>"},
    {"status": "processed", "case_id": "case-1", "message_id": "<wrong@provider.test>", "reply_message_id": "<native@company.test>"},
    {"status": "processed", "case_id": "case-1", "message_id": "<reply@provider.test>", "reply_message_id": ""},
])
def test_registered_mail_client_rejects_invalid_processing_receipt(payload):
    session = Mock()
    response = Mock(status_code=200)
    response.json.return_value = payload
    session.post.return_value = response
    client = ProviderPortalClient("http://portal.internal", "private-token", session=session)

    with pytest.raises(ProviderPortalClientError) as error:
        client.process_registered_email({
            "provider_actor_id": "bank-provider",
            "message_id": "<reply@provider.test>",
            "sender_actor_id": "viv",
            "recipient_actor_id": "bank-provider",
            "in_reply_to": "<root@company.test>",
            "body": "response",
            "received_at": "2026-10-30T09:00:00+00:00",
            "mailbox_uid": 10,
            "uidvalidity": 31,
        })

    assert error.value.code == "invalid_mail_observation_response"
