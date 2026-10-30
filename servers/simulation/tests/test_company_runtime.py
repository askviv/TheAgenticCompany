from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from uuid import UUID

from fastapi.testclient import TestClient
from servers.simulation.app import create_app
from servers.simulation.engine import EngineConflict, SimulationEngine
from servers.simulation.company_feedback import CompanyFeedback
from servers.simulation.feedback_contracts import RecordMapping
from servers.simulation.models import AdvanceClock, CanonicalEvent, RelatedDocument, SendMessage, StartScenario
from servers.simulation.problem_adapters import PreparedEmail, PreparedProblem, ProblemOrigin
from servers.simulation.store import IdempotencyConflict, SQLiteStore
from servers.simulation.runtime import initialize_company_runtime


class _Engine(SimulationEngine):
    """Use the production scenario setup while isolating native/mail providers."""
    def _capture_native_baseline(self, _snapshot):
        return None

    def pump(self, _run_id, **_kwargs):
        return {"delivered": [], "uncertain": [], "inbound_messages": 0}

    def _apply_event(self, run_id, run, event, connection):
        self.applied_events.append((str(run_id), event["event_id"]))
        return super()._apply_event(run_id, run, event, connection)


class _NativeAdapter:
    def get_purchase_order(self, _name):
        return {"name": "PO-001", "modified": "native-version-1", "company": "The Agent Company",
                "supplier": "SUP-001", "currency": "GBP", "items": [{"name": "row-1", "item_code": "COMP-001",
                    "qty": 10000, "received_qty": 8000, "rate": 1, "warehouse": "Main - TAC"}]}

    def get_stock_balance(self, _item, _warehouse):
        return 8000

    def assess_invoice(self, _name):
        return {"lines": [{"unreceived_billed_qty": "2000", "receipt_ids": ["PR-001"]}]}


class _Mail:
    def __init__(self):
        self.poll_calls = 0
        self.send_calls = 0

    def poll(self, *_args):
        self.poll_calls += 1
        return []

    def send(self, _sender, _recipient, _subject, _body, message_id, run_id, generation, *, on_submission_started, **_kwargs):
        self.send_calls += 1
        on_submission_started()
        return {"message_id": message_id, "run_id": run_id, "generation": generation}


class _PreparedRetailAdapter:
    scenario_id = "TAU_RETAIL"

    def prepare(self, **values):
        return PreparedProblem(
            problem_id="tau2:retail", task_id=values["task_id"], run_id=values["run_id"],
            generation=values["generation"],
            origin=ProblemOrigin(source="tau2-bench", domain="retail", commit="abc123",
                                 task_id=values["task_id"], database_sha256="0" * 64),
            snapshot={"task_id": values["task_id"],
                      "upstream": {"project": "tau2-bench", "domain": "retail", "commit": "abc123"},
                      "source_database": {"sha256": "0" * 64},
                      "original_db": {}, "id_mapping": {"products": {}, "users": {}, "orders": {}, "items": {}},
                      "source_integrity": {"status": "valid", "issues": []},
                      "task": {"id": values["task_id"],
                          "user_scenario": {"instructions": "private"},
                          "evaluation_criteria": {"secret": "private"}}},
            customer_instructions={"instructions": "private"},
            customer_actor_id=values["customer_actor_id"], support_actor_id=values["support_actor_id"],
            customer_user_state={"user_state": "private"},
            initial_email=PreparedEmail(subject="Help", body="Please help with my order.",
                                        message_id="<retail-seed@example.test>"),
            import_receipt={"task_id": values["task_id"], "experiment_id": values["run_id"]},
        )

    def evaluate(self, *, prepared, trajectory):
        return {"evaluation": {"reward_status": "complete", "reward": 1.0},
                "trajectory": list(trajectory), "task_id": prepared.task_id}

    def public_catalog(self):
        return {"domain": "retail", "tasks": []}


class CompanyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temporary.name) / "state.sqlite")
        self.site_admin = Mock()
        self.binding = {
            "site_name": "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost",
            "credentials_ref": "/private/company-baseline.json",
            "documents": {
                "company": "The Agent Company", "supplier": "SUP-001", "item": "COMP-001",
                "warehouse": "Main - TAC", "purchase_order": {"name": "PO-001", "item_row": "row-1"},
                "purchase_receipt": "PR-001", "purchase_invoice": "PI-001",
                "customer": "CUS-001", "customer_order": "SO-001",
            },
        }
        actors = [
            {"actor_id": "viv", "kind": "agent"},
            {"actor_id": "requester", "kind": "employee", "email": "requester@example.test"},
            {"actor_id": "customer", "kind": "customer", "email": "customer@example.test"},
        ]
        self.engine = _Engine(self.store, Mock(), Mock(), self.site_admin, actors)
        self.engine.applied_events = []

    def tearDown(self):
        self.temporary.cleanup()

    def test_company_experiments_share_existing_native_binding_without_provisioning(self):
        self.engine.start_company(self.binding, "start-company")
        first = self.engine.start_company_experiment(StartScenario(scenario_id="A1"), "experiment-a1", "controller")
        second = self.engine.start_company_experiment(StartScenario(scenario_id="E1"), "experiment-e1", "controller")

        self.assertEqual(first["erp_binding"], self.binding)
        self.assertEqual(second["erp_binding"], self.binding)
        self.assertEqual(len(self.store.company_experiments()), 2)
        expected_native_owner = str(UUID(hex=self.binding["site_name"].removeprefix("erp-sim-").removesuffix(".localhost")))
        self.assertEqual(self.engine._erp_owner_id(first), expected_native_owner)
        self.assertEqual(self.engine._erp_owner_id(second), expected_native_owner)
        self.site_admin.create.assert_not_called()

    def test_tau_retail_uses_shared_binding_and_delivers_prepared_customer_email(self):
        self.engine.start_company(self.binding, "start-company")
        adapter = _PreparedRetailAdapter()
        self.engine.problem_adapters = {"TAU_RETAIL": adapter}
        run = self.engine.start_company_experiment(
            StartScenario(scenario_id="TAU_RETAIL", task_id="retail-task-1"),
            "retail-experiment", "controller",
        )

        self.assertEqual(run["erp_binding"], self.binding)
        self.site_admin.create.assert_not_called()
        outbox = self.store.list_outbox(run["run_id"], 0)
        customer_message = next(item["private_payload"] for item in outbox
                                if item["outbox_id"] == "retail-customer-initial-email")
        self.assertEqual(customer_message["from_actor_id"], "customer")
        self.assertEqual(customer_message["to_actor_id"], "requester")
        self.assertEqual(customer_message["delivery_address"], "support@example.com")
        self.assertEqual(customer_message["body"], "Please help with my order.")
        self.assertNotIn("initial-instruction", {item["outbox_id"] for item in outbox})
        customer = self.store.get_actor(run["run_id"], 0, "customer")
        self.assertEqual(customer["private_state"]["support_actor_id"], "requester")
        self.assertNotIn("prepared_problem", self.engine.trajectory(run["run_id"])["run"]["manifest"])

    def test_tau_retail_cannot_create_a_private_company_site(self):
        self.engine.problem_adapters = {"TAU_RETAIL": _PreparedRetailAdapter()}
        with self.assertRaises(EngineConflict):
            self.engine.start(StartScenario(scenario_id="TAU_RETAIL", task_id="retail-task-1"),
                              "retail-standalone", "controller")
        self.site_admin.create.assert_not_called()

    def test_tau_retail_customer_reply_requires_run_correlated_support_email(self):
        self.engine.start_company(self.binding, "start-company")
        self.engine.problem_adapters = {"TAU_RETAIL": _PreparedRetailAdapter()}
        run = self.engine.start_company_experiment(
            StartScenario(scenario_id="TAU_RETAIL", task_id="retail-task-1"),
            "retail-correlated-email", "controller",
        )
        initial = next(item for item in self.store.list_outbox(run["run_id"], 0)
                       if item["outbox_id"] == "retail-customer-initial-email")
        self.store.complete_outbox(run["run_id"], 0, initial["outbox_id"],
                                   {"message_id": initial["private_payload"]["message_id"]})
        customer = self.store.get_actor(run["run_id"], 0, "customer")
        email = {"message_id": "<support-reply@example.test>",
                 "in_reply_to": initial["private_payload"]["message_id"],
                 "references": initial["private_payload"]["message_id"],
                 "from_header": "Customer Support <requester@example.test>",
                 "subject": "Re: Help", "body": "Can you send the order number?"}
        current = self.store.get_run(run["run_id"])
        with self.store.transaction() as connection:
            self.engine._ingest_message(current, customer, email, connection)
        events = self.store.list_events(run["run_id"], 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["actor_id"], "customer")
        self.assertEqual(events[0]["payload"]["from_actor_id"], "requester")
        request = self.engine._actor_turn_request(current, customer, events[0])
        self.assertEqual(request.actor_type, "customer")
        self.assertEqual(request.inbound_message.message_id, "<support-reply@example.test>")

    def test_tau_retail_evaluation_uses_received_company_email_trajectory(self):
        self.engine.start_company(self.binding, "start-company")
        self.engine.problem_adapters = {"TAU_RETAIL": _PreparedRetailAdapter()}
        run = self.engine.start_company_experiment(
            StartScenario(scenario_id="TAU_RETAIL", task_id="retail-task-1"),
            "retail-email-eval", "controller",
        )
        for message_id, sender, recipient, subject, body in (
            ("<customer@example.test>", "customer", "requester", "My order", "Where is my order?"),
            ("<support@example.test>", "requester", "customer", "Re: My order", "I am checking this for you."),
        ):
            self.store.put_private_message(run["run_id"], 0, {
                "message_id": message_id, "subject": subject, "body": body,
                "from_header": sender, "to_header": recipient, "reference": "INBOX UID 42",
            }, simulated_time=run["simulated_time"], from_actor_id=sender,
                to_actor_id=recipient)
            self.store.append_audit(run["run_id"], 0, {
                "event": "email_received", "message_id": message_id,
                "from_actor_id": sender, "to_actor_id": recipient,
                "message_hash": "test-hash", "mailbox_reference": "INBOX UID 42",
                "simulated_time": run["simulated_time"],
            })

        result = self.engine.evaluate(run["run_id"])
        self.assertEqual(result["problem_evaluation"]["trajectory"], [
            {"role": "user", "content": "Where is my order?"},
            {"role": "assistant", "content": "I am checking this for you."},
        ])

    def test_received_email_content_is_private_conversation_state_not_audit_payload(self):
        self.engine.start_company(self.binding, "start-company")
        self.engine.problem_adapters = {"TAU_RETAIL": _PreparedRetailAdapter()}
        run = self.engine.start_company_experiment(
            StartScenario(scenario_id="TAU_RETAIL", task_id="retail-task-1"),
            "retail-private-mail", "controller",
        )
        message = {
            "message_id": "<received@example.test>", "subject": "Order status",
            "body": "Customer private email body", "from_header": "Customer <customer@example.test>",
            "to_header": "support@example.test", "in_reply_to": "<retail-seed@example.test>",
            "references": "<retail-seed@example.test>", "mailbox_uid": "42", "uidvalidity": "7",
            "reference": "INBOX UID 42", "webmail_url": "https://mail.example.test/message/42",
        }
        class _InboxMail:
            def poll(self, *_args):
                return [message]
        self.engine.mail = _InboxMail()
        customer = self.store.get_actor(run["run_id"], 0, "customer")
        self.engine._poll_actor_mail(run, customer)

        stored = self.store.list_private_messages(run["run_id"], 0)
        received = next(item for item in stored if item["message_id"] == message["message_id"])
        self.assertEqual(received["body"], message["body"])
        self.assertEqual(received["subject"], message["subject"])
        audit = next(item["private_record"] for item in self.store.list_audit(run["run_id"], 0)
                     if item["private_record"].get("message_id") == message["message_id"])
        self.assertNotIn("body", audit)
        self.assertNotIn("subject", audit)
        self.assertEqual(audit["message_hash"], received["message_hash"])

    def test_common_company_tick_advances_and_processes_events_for_each_experiment(self):
        self.engine.actors.extend([
            {"actor_id": "warehouse", "kind": "warehouse", "email": "warehouse@example.test"},
            {"actor_id": "logistics", "kind": "logistics", "email": "logistics@example.test"},
        ])
        self.engine._actor_config = {actor["actor_id"]: actor for actor in self.engine.actors}
        self.engine.start_company(self.binding, "start-company")
        first = self.engine.start_company_experiment(StartScenario(scenario_id="A1"), "experiment-a1", "controller")
        second = self.engine.start_company_experiment(StartScenario(scenario_id="E1"), "experiment-e1", "controller")
        company = self.store.get_company()
        anchor = datetime.fromisoformat(company["wall_anchor"])
        for run, seconds in ((first, 4), (second, 2)):
            event = CanonicalEvent(event_id="delay", run_id=run["run_id"], scenario_id=run["scenario_id"],
                simulated_time=datetime.fromisoformat(run["simulated_time"]) + timedelta(seconds=seconds),
                event_type="SHIPMENT_DELAYED", source="test", entity_type="shipment", entity_id="SHIP-001",
                related_entities=[f"{run['scenario_id']}-case"],
                payload={"shipment_id": "SHIP-001", "revised_eta_days": 1}, visibility=["agent"])
            self.store.enqueue_event(event, 0)

        target = anchor + timedelta(seconds=5)
        self.engine.tick_company_clock(target)
        run_times = [self.store.get_run(run["run_id"])["simulated_time"] for run in (first, second)]
        self.assertEqual(run_times, [target.isoformat(), target.isoformat()])
        for run in (first, second):
            event_rows = self.store.list_events(run["run_id"], 0, status="completed")
            self.assertEqual([row["event_id"] for row in event_rows], ["delay"])
        self.assertEqual(self.engine.applied_events, [(second["run_id"], "delay"), (first["run_id"], "delay")])

    def test_paused_company_skips_production_mail_pump_and_keeps_completed_messages_visible(self):
        mail = _Mail()
        engine = SimulationEngine(self.store, mail, lambda _binding: _NativeAdapter(), self.site_admin, [
            {"actor_id": "viv", "kind": "agent"},
            {"actor_id": "requester", "kind": "employee", "email": "requester@example.test"},
        ])
        engine.start_company(self.binding, "start-company")
        run = engine.start_company_experiment(StartScenario(scenario_id="A1"), "experiment-a1", "controller")
        self.assertEqual(mail.send_calls, 1)
        self.assertTrue(self.store.list_outbox(run["run_id"], 0, status="completed"))

        engine.pause_company(True, "pause-company")
        mail.send_calls = 0
        mail.poll_calls = 0
        engine.pump(run["run_id"])
        self.assertEqual((mail.poll_calls, mail.send_calls), (0, 0))

        engine.company_binding = self.binding
        client = TestClient(create_app(engine, {"controller-token": {"actor_id": "controller", "role": "controller"}}))
        activity = client.get("/company", headers={"Authorization": "Bearer controller-token"}).json()["activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["type"], "email")
        self.assertEqual(activity[0]["title"], "Please investigate the business case")
        self.assertNotIn("credentials_ref", str(activity))
        self.assertNotIn("private_state", str(activity))

    def test_company_and_isolated_experiment_keys_do_not_collide(self):
        self.engine.start_company(self.binding, "start-company")
        shared = self.engine.start_company_experiment(StartScenario(scenario_id="A1"), "same-key", "controller")
        self.site_admin.create.return_value = self.binding
        isolated = self.engine.start(StartScenario(scenario_id="A1"), "same-key", "controller")
        self.assertNotEqual(shared["run_id"], isolated["run_id"])
        self.assertEqual(self.engine._erp_owner_id(shared), str(UUID(hex=self.binding["site_name"].removeprefix("erp-sim-").removesuffix(".localhost"))))
        self.assertEqual(self.engine._erp_owner_id(isolated), isolated["run_id"])
        self.assertEqual(self.store.company_experiments()[0]["experiment_id"], shared["run_id"])

    def test_company_clock_is_common_and_pause_excludes_paused_wall_time(self):
        start = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
        company = self.store.start_company("company", start, start, self.binding)
        ticked = self.engine.tick_company_clock(start + timedelta(seconds=5))
        self.assertEqual(ticked["simulated_time"], (start + timedelta(seconds=5)).isoformat())

        paused = self.engine.pause_company(True, "pause-company", now=start + timedelta(seconds=5))
        self.assertTrue(paused["paused"])
        self.engine.tick_company_clock(start + timedelta(seconds=100))
        still_paused = self.store.get_company()
        self.assertEqual(still_paused["simulated_time"], ticked["simulated_time"])

        resumed = self.engine.pause_company(False, "resume-company", now=start + timedelta(seconds=100))
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["simulated_time"], ticked["simulated_time"])
        resumed_tick = self.engine.tick_company_clock(start + timedelta(seconds=105))
        self.assertEqual(resumed_tick["simulated_time"], (start + timedelta(seconds=10)).isoformat())

    def test_shared_company_experiment_cannot_be_reset_independently(self):
        self.engine.start_company(self.binding, "start-company")
        run = self.engine.start_company_experiment(StartScenario(scenario_id="A1"), "experiment-a1", "controller")
        with self.assertRaises(EngineConflict):
            self.engine.reset(run["run_id"], "reset", "controller")
        with self.assertRaises(EngineConflict):
            self.engine.advance(AdvanceClock(run_id=UUID(run["run_id"]), until=datetime.now(timezone.utc)), "manual-advance")
        self.site_admin.reset.assert_not_called()

    def test_company_start_key_conflict_does_not_change_existing_binding(self):
        self.engine.start_company(self.binding, "start-company")
        changed = {**self.binding, "site_name": "erp-sim-00000000000000000000000000000000.localhost"}
        with self.assertRaises(IdempotencyConflict):
            self.engine.start_company(changed, "start-company")
        self.assertEqual(self.store.get_company()["erp_binding"], self.binding)

    def test_runtime_initialization_creates_metadata_only_and_preserves_pause(self):
        initialized = initialize_company_runtime(self.engine, self.binding)
        self.assertEqual(initialized["status"], "running")
        self.assertEqual(self.store.company_experiments(), [])
        self.site_admin.create.assert_not_called()

        paused = self.engine.pause_company(True, "pause-company")
        self.assertTrue(paused["paused"])
        restarted_store = SQLiteStore(Path(self.temporary.name) / "state.sqlite")
        restarted_engine = _Engine(restarted_store, Mock(), Mock(), self.site_admin, self.engine.actors)
        after_restart = initialize_company_runtime(restarted_engine, self.binding)
        self.assertTrue(after_restart["paused"])
        self.site_admin.reset.assert_not_called()
        self.site_admin.create.assert_not_called()

    def test_company_binding_change_is_not_silently_adopted(self):
        self.engine.start_company(self.binding, "start-company")
        changed = {**self.binding, "site_name": "erp-sim-00000000000000000000000000000000.localhost"}
        with self.assertRaises(EngineConflict):
            self.engine.start_company(changed, "another-key")
        self.assertEqual(self.store.get_company()["erp_binding"], self.binding)

    def test_coffee_email_delivery_requires_exact_mapped_company_document_and_same_thread(self):
        self.engine.actors.append({
            "actor_id": "coffee_farmer_A", "kind": "counterparty",
            "email": "coffee-farmer_A@example.com",
        })
        self.engine.mail = _Mail()
        self.engine.start_company(self.binding, "start-company")
        run = self.engine.start_company_experiment(
            StartScenario(scenario_id="A1"), "experiment-coffee-mail", "controller"
        )
        mapping = RecordMapping(
            mapping_id="coffee-source-map-1", benchmark="coffeebench",
            upstream_commit="9e2f3950b5812c8753de81ede9c68babc024b5c8",
            source_record_id="invoice-1", source_entity_type="invoice",
            erp_doctype="Purchase Invoice", erp_name="PINV-1",
            company_time="2026-10-30T09:00:00Z",
            native_context={"email_reply": {
                "native_action": "send_message", "counterparty_id": "farmer_A",
                "allowed_recipient_actor_ids": ["coffee_farmer_A"],
            }},
        )
        CompanyFeedback(self.store, {}).map_record(mapping, {
            "doctype": "Purchase Invoice", "name": "PINV-1",
        })
        related = RelatedDocument(doctype="Purchase Invoice", name="PINV-1")
        first = self.engine.send_message(
            SendMessage(
                run_id=UUID(run["run_id"]), case_id="A1-case",
                to_actor_id="coffee_farmer_A", subject="Invoice follow-up",
                body="Please confirm the recorded invoice terms.", related_document=related,
            ),
            "coffee-email-1", "viv",
        )
        completed = self.store.list_outbox(
            run["run_id"], run["generation"], status="completed"
        )
        first_payload = next(
            item["private_payload"] for item in completed
            if item["private_payload"].get("message_id") == first["message_id"]
        )
        second = self.engine.send_message(
            SendMessage(
                run_id=UUID(run["run_id"]), case_id="A1-case",
                to_actor_id="coffee_farmer_A", subject="Re: Invoice follow-up",
                body="We have the exact invoice record and will reconcile it.",
                related_document=related, in_reply_to=first["message_id"],
            ),
            "coffee-email-2", "viv",
        )
        completed = self.store.list_outbox(
            run["run_id"], run["generation"], status="completed"
        )
        second_payload = next(
            item["private_payload"] for item in completed
            if item["private_payload"].get("message_id") == second["message_id"]
        )

        self.assertEqual(first_payload["related_document"], {
            "doctype": "Purchase Invoice", "name": "PINV-1",
        })
        self.assertIsNone(first_payload["in_reply_to"])
        self.assertEqual(second_payload["in_reply_to"], first["message_id"])
        self.assertEqual(second_payload["related_document"], first_payload["related_document"])
        self.assertEqual(second["message_id"], second_payload["message_id"])
        with self.assertRaises(ValueError, msg="Coffee mail must use an exact mapped source document"):
            self.engine.send_message(
                SendMessage(
                    run_id=UUID(run["run_id"]), case_id="A1-case",
                    to_actor_id="coffee_farmer_A", subject="Unmapped follow-up",
                    body="This document is not mapped.",
                    related_document=RelatedDocument(doctype="Purchase Invoice", name="PINV-404"),
                ),
                "coffee-email-invalid", "viv",
            )

    def test_http_company_flow_returns_safe_shared_state_and_activity(self):
        self.engine.company_binding = self.binding
        client = TestClient(create_app(self.engine, {
            "controller-token": {"actor_id": "controller", "role": "controller"},
        }))
        headers = {"Authorization": "Bearer controller-token", "Idempotency-Key": "start-company"}
        started = client.post("/company/start", json={}, headers=headers)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["erp_site_id"], self.binding["site_name"])
        self.assertNotIn("credentials_ref", started.text)

        experiment = client.post("/company/experiments", json={"scenario_id": "A1"}, headers={
            **headers, "Idempotency-Key": "experiment-a1",
        })
        self.assertEqual(experiment.status_code, 200, experiment.text)
        self.assertEqual(experiment.json()["erp_site_id"], self.binding["site_name"])
        status = client.get("/company", headers={"Authorization": "Bearer controller-token"})
        self.assertEqual(len(status.json()["experiments"]), 1)
        self.assertEqual(status.json()["activity"], [])  # pending outbox messages are never shown
        self.assertEqual(client.get("/company").status_code, 401)
        agent_client = TestClient(create_app(self.engine, {
            "agent-token": {"actor_id": "viv", "role": "agent"},
        }))
        self.assertEqual(agent_client.get("/company", headers={"Authorization": "Bearer agent-token"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
