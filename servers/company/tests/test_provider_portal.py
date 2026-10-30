from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import json
import runpy
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from servers.company.provider_portal import create_app
from servers.simulation.provider_contracts import (
    ProviderPortalCaseRegistration, ProviderResponse, ProviderCasePrepareResponse,
    ProviderBankActionReceipt, ProviderSourceFact,
    ProviderPortalForm,
)
from servers.simulation.store import SQLiteStore


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.prepare_requests = []
        self.portal_actions = []

    def prepare_case(self, request):
        self.prepare_requests.append(request)
        return ProviderCasePrepareResponse(
            provider_id=request.provider_id, task_id=request.task_id,
            conversation_id=request.conversation_id, status="prepared",
            initialization_status="applied", initialization_revision="a" * 64,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            business_description="You are contacting the bank for account support.",
            subject="You are contacting the bank for account support.",
            requested_outcome="Review the linked provider account and respond to the customer request.",
            private_factual_context=[ProviderSourceFact(
                label="customer_request", text="I need support with my linked account.",
                source_task_sha256="a" * 64, source_json_pointer="/user_scenario/instructions",
                start_char=0, end_char=39,
            )],
            forms=[ProviderPortalForm(
                native_user_tool="apply_for_credit_card", source_tool_name="apply_for_credit_card",
                title="Apply for a credit card", argument_schema={
                    "type": "object", "properties": {"customer_name": {"type": "string"},
                    "annual_income": {"type": "number"}}, "required": ["customer_name", "annual_income"],
                },
            )],
        )

    def handle(self, request):
        self.requests.append(request)
        return ProviderResponse(
            provider_id=request.provider_id, operation_id=request.operation_id,
            conversation_id=request.conversation_id, task_id=request.task_id,
            company_actor_id=request.company_actor_id,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            received_message_id=request.message_id, status="completed",
            body="Your request is being reviewed.", native_source="pinned", native_state_revision="rev-1",
            initialization_status="applied", in_reply_to=request.message_id,
        )

    def apply_portal_action(self, request):
        self.portal_actions.append(request)
        return ProviderBankActionReceipt(
            provider_id="tau2-banking", operation_id=request.operation_id,
            conversation_id=request.conversation_id, status="applied",
            native_tool=request.native_user_tool, native_result_hash="b" * 64,
            native_state_revision="c" * 64, source_references=request.evidence_references,
        )


class FakeMail:
    def __init__(self):
        self.sent = []
        self.receipts = {}

    def send(self, sender, recipient, subject, body, message_id, run_id, generation, **kwargs):
        callback = kwargs.get("on_submission_started")
        if callback:
            callback()
        receipt = {"message_id": message_id, "subject": subject, "run_id": run_id,
                   "generation": generation, "recipient_actor_id": recipient["actor_id"]}
        self.sent.append((sender["actor_id"], recipient["actor_id"], message_id,
                          kwargs.get("in_reply_to"), body, kwargs.get("reply_to_actor_id")))
        self.receipts[message_id] = receipt
        return receipt

    def find_receipt(self, recipient, message_id, subject, run_id, generation):
        return self.receipts.get(message_id)


class ProviderPortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStore(Path(self.temp.name) / "state.sqlite")
        self.provider = FakeProvider()
        self.mail = FakeMail()
        self.auth_calls = []
        self.staff = [
            {"actor_id": "employee-1", "email": "one@example.test", "kind": "employee"},
            {"actor_id": "employee-2", "email": "two@example.test", "kind": "employee"},
            {"actor_id": "bank-contact", "email": "bank@example.test", "kind": "counterparty"},
            {"actor_id": "airline-contact", "email": "airline@example.test", "kind": "counterparty"},
            {"actor_id": "telecom-contact", "email": "telecom@example.test", "kind": "counterparty"},
            {"actor_id": "requester", "email": "support@example.test", "kind": "support"},
            {"actor_id": "viv", "email": "hello@example.test", "kind": "agent"},
        ]
        profile = {"company": {"assistantEmail": "hello@example.test"}, "employees": [
            {"id": "employee-one", "displayName": "Employee One", "email": "one@example.test"},
            {"id": "employee-two", "displayName": "Employee Two", "email": "two@example.test"},
        ]}
        self.app = create_app(
            store=self.store, profile=profile,
            mail_config={"actors": self.staff, "mailu_imap_host": "mail.test", "mailu_imap_port": 993},
            mail_credentials={
                "one@example.test": {"username": "one@example.test", "password": "private"},
                "two@example.test": {"username": "two@example.test", "password": "private"},
                "bank@example.test": {"username": "bank@example.test", "password": "private"},
                "airline@example.test": {"username": "airline@example.test", "password": "private"},
                "telecom@example.test": {"username": "telecom@example.test", "password": "private"},
                "support@example.test": {"username": "support@example.test", "password": "private"},
            }, session_secret="s" * 40, integration_secret="i" * 40,
            provider_worker=self.provider, mail_gateway=self.mail,
            provider_mail_actors={"tau2-airline": "airline-contact", "tau2-telecom": "telecom-contact",
                                  "tau2-banking": "bank-contact"},
            portal_public_url="http://provider.test",
            mail_authenticator=lambda email, password, **kwargs: self.auth_calls.append((email, password)),
            now=lambda: datetime(2026, 10, 30, tzinfo=timezone.utc),
        )
        self.client = TestClient(self.app)
        self.registration = ProviderPortalCaseRegistration(
            case_id="case-1", provider_id="tau2-banking", task_id="task_001",
            conversation_id="conversation-1", responsible_actor_id="employee-1",
            responsible_email="one@example.test", company_account_reference="account-1",
            issue_reference="issue-1", subject="Review a bank request",
            business_description="A company banking request needs follow-up.",
            requested_outcome="Review the request and contact the provider.",
            created_at="2026-10-30T00:00:00+00:00",
        )
        self.store_for_portal = self.app.state.portal_store
        self.store_for_portal.register_case(self.registration.model_dump(mode="json"))

    def _login(self, email="one@example.test"):
        result = self.client.post("/api/login", json={"email": email, "password": "mailbox-password"})
        self.assertEqual(result.status_code, 200)
        return result.json()["csrf_token"]

    def test_employee_auth_and_case_scoping_exclude_native_task_and_criteria(self):
        self.assertEqual(self.client.get("/api/cases").status_code, 401)
        self._login("two@example.test")
        self.assertEqual(self.client.get("/api/cases").json(), {"cases": []})
        self.assertEqual(self.client.get("/api/cases/case-1").status_code, 404)
        self._login()
        value = self.client.get("/api/cases/case-1").json()
        self.assertEqual(value["provider_id"], "tau2-banking")
        self.assertNotIn("task_id", value)
        self.assertNotIn("evaluation_criteria", value)
        self.assertGreater(len(self.provider.prepare_requests), 0)

    def test_portal_message_uses_native_provider_and_normal_email_with_durable_idempotency(self):
        csrf = self._login()
        payload = {"client_message_id": "message-1", "body": "Please review the submitted request."}
        headers = {"X-CSRF-Token": csrf}
        result = self.client.post("/api/cases/case-1/messages", json=payload, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "sent")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(self.mail.sent), 2)
        self.assertEqual(self.mail.sent[0][0:2], ("employee-1", "bank-contact"))
        self.assertEqual(self.mail.sent[1][0:2], ("bank-contact", "employee-1"))
        self.assertEqual(self.mail.sent[1][3], result.json()["message_id"])
        replay = self.client.post("/api/cases/case-1/messages", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(self.mail.sent), 2)
        history = self.client.get("/api/cases/case-1").json()["messages"]
        self.assertEqual(history[0]["reply_body"], "Your request is being reviewed.")

    def test_portal_message_requires_csrf_and_internal_registration_secret(self):
        csrf = self._login()
        bad_csrf = self.client.post("/api/cases/case-1/messages", json={
            "client_message_id": "message-1", "body": "Please review this request."
        })
        self.assertEqual(bad_csrf.status_code, 403)
        registration = self.registration.model_dump(mode="json")
        self.assertEqual(self.client.post("/internal/cases", json=registration).status_code, 401)
        changed = dict(registration, company_account_reference="different-company-account")
        response = self.client.post("/internal/cases", json=changed,
                                    headers={"Authorization": "Bearer " + "i" * 40})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(csrf)

    def test_native_customer_form_runs_only_the_case_bound_customer_tool(self):
        csrf = self._login()
        case = self.client.get("/api/cases/case-1").json()
        self.assertEqual(case["forms"][0]["source_tool_name"], "apply_for_credit_card")
        result = self.client.post("/api/cases/case-1/actions", json={
            "client_action_id": "action-1", "form_id": "apply_for_credit_card",
            "arguments": {"customer_name": "Synthetic Applicant", "annual_income": 50000},
        }, headers={"X-CSRF-Token": csrf})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "applied")
        self.assertEqual(len(self.provider.portal_actions), 1)
        self.assertEqual(self.provider.portal_actions[0].native_user_tool, "apply_for_credit_card")
        self.assertNotIn("Synthetic Applicant", result.text)
        rejected = self.client.post("/api/cases/case-1/actions", json={
            "client_action_id": "action-2", "form_id": "call_discoverable_agent_tool",
            "arguments": {},
        }, headers={"X-CSRF-Token": csrf})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(len(self.provider.portal_actions), 1)

    def test_library_case_registration_delivers_viv_handoff_once_and_login_uses_native_mail_password(self):
        registration = self.registration.model_copy(update={
            "case_id": "case-2", "conversation_id": "conversation-2", "issue_reference": "issue-2",
        })
        headers = {"Authorization": "Bearer " + "i" * 40}
        first = self.client.post("/internal/cases", json=registration.model_dump(mode="json"), headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.mail.sent[-1][0:2], ("employee-1", "viv"))
        self.assertIn("You are contacting the bank for account support.", self.mail.sent[-1][4])
        self.assertIn("Requested outcome: Review the linked provider account and respond to the customer request.", self.mail.sent[-1][4])
        self.assertIn("Customer-provided context:", self.mail.sent[-1][4])
        self.assertIn("I need support with my linked account.", self.mail.sent[-1][4])
        self.assertIn("http://provider.test/?case=case-2", self.mail.sent[-1][4])
        self.assertEqual(self.mail.sent[-1][5], "bank-contact")
        employee_csrf = self._login()
        case_detail = self.client.get("/api/cases/case-2").json()
        self.assertEqual(case_detail["private_factual_context"][0]["text"],
                         "I need support with my linked account.")
        activity = self.store_for_portal.company_case_activity("case-2", "http://provider.test")
        self.assertNotIn("private_factual_context", activity)
        self.assertNotIn("I need support with my linked account.", str(activity))
        self.assertIsNotNone(employee_csrf)
        sent_count = len(self.mail.sent)
        replay = self.client.post("/internal/cases", json=registration.model_dump(mode="json"), headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(len(self.mail.sent), sent_count)

        # The injected authentication boundary receives the managed email and
        # transient password; no password is stored in the resulting session.
        cookie = self.client.post("/api/login", json={"email": "one@example.test", "password": "secret"})
        self.assertEqual(cookie.status_code, 200)
        self.assertEqual(self.auth_calls[-1], ("one@example.test", "secret"))
        set_cookie = cookie.headers["set-cookie"]
        self.assertIn("httponly", set_cookie.casefold())
        self.assertNotIn("secure", set_cookie.casefold())

    def test_registered_viv_email_reply_uses_exact_thread_and_durable_native_callback(self):
        registration = self.registration.model_copy(update={
            "case_id": "email-case", "conversation_id": "email-conversation", "issue_reference": "email-issue",
        })
        headers = {"Authorization": "Bearer " + "i" * 40}
        created = self.client.post("/internal/cases", json=registration.model_dump(mode="json"), headers=headers)
        self.assertEqual(created.status_code, 200)
        root_message_id = self.mail.sent[-1][2]
        self.assertEqual(self.client.get("/internal/registered-mail-cases").status_code, 401)
        listed = self.client.get("/internal/registered-mail-cases", headers=headers).json()["cases"]
        email_case = next(row for row in listed if row["case_id"] == "email-case")
        self.assertEqual(email_case["root_message_id"], root_message_id)
        reply = {
            "provider_actor_id": "bank-contact", "message_id": "<viv-reply-1@example.test>",
            "sender_actor_id": "viv", "recipient_actor_id": "bank-contact",
            "in_reply_to": root_message_id, "references": [root_message_id],
            "subject": "Re: Review a bank request", "body": "Please handle the customer request.",
            "received_at": "2026-10-30T12:00:00+00:00", "mailbox_uid": 47, "uidvalidity": 9,
        }
        observed = self.client.post("/internal/observe-mail", json=reply, headers=headers)
        self.assertEqual(observed.status_code, 200)
        self.assertEqual(observed.json()["status"], "processed")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.provider.requests[0].message_id, reply["message_id"])
        self.assertEqual(self.provider.requests[0].in_reply_to, root_message_id)
        self.assertEqual(self.mail.sent[-1][0:2], ("bank-contact", "viv"))
        self.assertEqual(self.mail.sent[-1][3], reply["message_id"])
        self.assertEqual(self.app.state.portal_store.mail_cursor("bank-contact", "email-case"), {
            "uidvalidity": 9, "after_uid": 47,
        })
        sent_count = len(self.mail.sent)
        replay = self.client.post("/internal/observe-mail", json=reply, headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), observed.json())
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(self.mail.sent), sent_count)
        employee_reply = dict(
            reply, message_id="<employee-reply-1@example.test>", sender_actor_id="employee-1",
            body="Please follow up with the provider.", mailbox_uid=48,
        )
        employee_result = self.client.post("/internal/observe-mail", json=employee_reply, headers=headers)
        self.assertEqual(employee_result.status_code, 200)
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(self.mail.sent[-1][0:2], ("bank-contact", "employee-1"))
        activity = self.app.state.portal_store.company_case_activity("email-case", "http://provider.test")
        self.assertEqual(len(activity["activity"]), 5)
        directions = [row["direction"] for row in activity["activity"]]
        self.assertEqual(directions.count("outbound"), 3)
        self.assertEqual(directions.count("inbound"), 2)
        self.assertTrue(any(row["body"] == reply["body"] and row["received_at"] == reply["received_at"]
                            for row in activity["activity"]))
        self.assertNotIn("task_id", activity)
        self.assertNotIn("native_response_json", str(activity))
        wrong_recipient = dict(reply, message_id="<wrong-recipient@example.test>", recipient_actor_id="employee-1",
                               mailbox_uid=49)
        self.assertEqual(self.client.post("/internal/observe-mail", json=wrong_recipient,
                                          headers=headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()


def test_portal_accepts_the_real_roster_and_bootstrap_actor_shape(tmp_path):
    root = Path(__file__).resolve().parents[3]
    profile = json.loads((root / "deploy/single-host/company-identities.json").read_text())
    bootstrap = runpy.run_path(str(root / "deploy/enterprise-simulation/coordinator/bootstrap.py"))
    _, providers = bootstrap["external_provider_mail_directory"](profile["company"])
    actors = [*bootstrap["ACTORS"], *bootstrap["managed_employee_actors"](profile), *providers]
    credentials = {actor["email"]: {"username": actor["email"], "password": "synthetic-password"}
                   for actor in actors if actor["actor_id"] != "viv"}
    worker, mail, authenticated = FakeProvider(), FakeMail(), []
    app = create_app(
        store=SQLiteStore(tmp_path / "actual-profile.sqlite"), profile=profile,
        mail_config={"actors": actors}, mail_credentials=credentials,
        session_secret="s" * 40, integration_secret="i" * 40,
        provider_worker=worker, mail_gateway=mail,
        provider_mail_actors={key: value["actor_id"] for key, value in
                              profile["company"]["background"]["external_providers"].items()},
        portal_public_url="http://provider.test",
        mail_authenticator=lambda email, password, **kwargs: authenticated.append(email),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for employee in profile["employees"]:
            response = client.post("/api/login", json={"email": employee["email"],
                                                       "password": "synthetic-password"})
            assert response.status_code == 200
            assert client.get("/api/session").json()["employee"]["email"] == employee["email"]
    assert authenticated == [employee["email"] for employee in profile["employees"]]
    assert worker.prepare_requests == worker.requests == worker.portal_actions == mail.sent == []
