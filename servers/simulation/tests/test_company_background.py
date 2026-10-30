from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from uuid import uuid4

from servers.simulation.company_background import CompanyBackgroundService
from servers.simulation.engine import SimulationEngine
from servers.simulation.models import StartScenario
from servers.simulation.store import SQLiteStore, StoreConflict


class _Engine(SimulationEngine):
    def _capture_native_baseline(self, _snapshot):
        return None

    def pump(self, _run_id, **_kwargs):
        return {"delivered": [], "uncertain": [], "inbound_messages": 0}


class CompanyBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temporary.name) / "state.sqlite")
        self.binding = {
            "site_name": "erp-sim-" + uuid4().hex + ".localhost",
            "credentials_ref": "/private/company.json",
            "erp_company": "The Agent Company",
            "documents": {
                "company": "The Agent Company", "supplier": "SUP-001", "item": "ITEM-001",
                "warehouse": "Main - TAC", "purchase_order": "PO-001", "purchase_receipt": "PR-001",
                "purchase_invoice": "PI-001", "customer": "CUS-001", "customer_order": "SO-001",
            },
        }
        self.engine = _Engine(self.store, Mock(), Mock(), Mock(), [
            {"actor_id": "viv", "kind": "agent"},
            {"actor_id": "requester", "kind": "employee", "email": "requester@example.test"},
            {"actor_id": "supplier", "kind": "supplier", "email": "supplier@example.test",
             "private_state": {"market_config": {"store_type": "auto_hardware"}}},
        ])
        self.engine.start_company(self.binding, "start-company")
        self.service = CompanyBackgroundService(
            self.engine, runtime=Mock(), finance_adapter=Mock(), profile={}, company_binding=self.binding,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _event(self, event_id="source-event-1"):
        return {
            "schema_version": 1, "company_id": "company", "event_id": event_id,
            "event_type": "SUPPLIER_MARKET_DEMAND", "simulated_time": datetime.now(timezone.utc).isoformat(),
            "source": "qwenlm-ecommercebench-calendar", "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
            "entity_type": "supplier", "entity_id": "SUP-001",
            "payload": {"market_date": "2026-10-30", "store_type": "auto_hardware",
                        "private_amount": "999.99"}, "visibility": ["supplier"],
        }

    def test_company_source_event_targets_only_runs_existing_at_first_observation(self):
        first = self.engine.start_company_experiment(
            StartScenario(scenario_id="A1"),
            "experiment-first", "controller",
        )
        event = self._event()
        self.service._fanout([event])
        first_pending = self.store.pending_events(first["run_id"], first["generation"])
        self.assertEqual(len(first_pending), 1)
        self.assertEqual(first_pending[0]["payload"]["company_source_event_id"], event["event_id"])

        second = self.engine.start_company_experiment(
            StartScenario(scenario_id="A1"),
            "experiment-second", "controller",
        )
        self.service._fanout([event])
        self.assertEqual(self.store.pending_events(second["run_id"], second["generation"]), [])
        first_events = self.store.list_events(first["run_id"], first["generation"])
        self.assertEqual(sum(row["payload"].get("company_source_event_id") == event["event_id"]
                             for row in first_events), 1)
        self.assertNotIn("private_amount", self.service.activity()[0]["payload"])

    def test_finance_intent_is_persisted_and_reconciled_once_after_receipt(self):
        intent = {"id": "coffee-source-1", "source": "sakanaai-coffeebench-native-economy",
                  "kind": "invoice", "source_ref": "INV-1", "occurred_at": datetime.now(timezone.utc).isoformat(),
                  "party": "farmer_A", "currency": "USD", "amount": "100.00",
                  "native_record": {"amount": "100.00", "private": True}}
        adapter = Mock()
        adapter.reconcile.return_value = {"as_of": datetime.now(timezone.utc).isoformat(), "receipts": [
            {"source_id": intent["id"], "doctype": "Purchase Invoice", "name": "PINV-001", "created": True},
        ]}
        self.service.finance_adapter = adapter
        self.service.profile = {"coffee": {
            "company_business_ids": ["roaster_A"],
            "counterparties": {"farmer_A": {
                "actor_id": "coffee_farmer_A",
                "email": "coffee-farmer_A@example.com",
                "display_name": "CoffeeBench Farmer A",
                "kind": "counterparty",
            }},
        }}
        self.service._provider_started_at = datetime(2026, 10, 30, tzinfo=timezone.utc)
        self.store.record_company_finance_source_intents([intent])
        self.service._reconcile({}, datetime.now(timezone.utc))
        self.service._reconcile({}, datetime.now(timezone.utc))
        adapter.reconcile.assert_called_once()
        receipts = self.store.list_company_finance_receipts()
        self.assertEqual(receipts[0]["name"], "PINV-001")
        self.assertNotIn("private", str(self.service.activity()))

    def test_source_id_collision_with_different_facts_is_rejected(self):
        event = self._event()
        self.store.record_company_source_events([event])
        changed = {**event, "payload": {"different": True}}
        with self.assertRaises(StoreConflict):
            self.store.record_company_source_events([changed])

    def test_company_profile_builds_explicit_provider_bindings_without_provider_calls(self):
        profile_path = Path(__file__).resolve().parents[3] / "deploy/single-host/company-identities.json"
        worker = Mock()
        service = CompanyBackgroundService.from_runtime(
            self.engine, store=self.store, worker=worker, finance_adapter=Mock(),
            profile_path=profile_path, background_model_id="gpt-6-luna", company_binding=self.binding,
        )
        bindings, actor_states = service._bindings()
        self.assertEqual(bindings["coffeebench"], {
            "company_business_ids": ["roaster_A"], "max_days": 90, "model_id": "gpt-6-luna",
        })
        self.assertNotIn("counterparties", bindings["coffeebench"])
        self.assertNotIn("mail_sender_actor_id", bindings["coffeebench"])
        self.assertEqual(bindings["ecommercebench"]["company_id"], "company")
        self.assertEqual(bindings["ecommercebench"]["supplier_id"], "SUP-001")
        original_calendar_binding = {
            "supplier_actor_id": "supplier", "store_type": "auto_hardware",
            "company_id": "company", "supplier_id": "SUP-001",
        }
        original_request = {"company_id": "company", "provider_id": "ecommercebench",
                            "started_at": "2026-10-29T00:00:00+00:00",
                            "binding": original_calendar_binding}
        self.store.background_initialization_request(original_request)
        replay = self.store.background_initialization_request({
            **original_request, "binding": bindings["ecommercebench"],
        })
        self.assertEqual(replay, original_request)

        supplier = actor_states["supplier"]
        self.assertEqual(supplier["supplier_id"], "SUP-001")
        self.assertEqual(supplier["business_model"]["store_type"], "auto_hardware")
        worker.initialize.assert_not_called()
        worker.advance.assert_not_called()

    def test_background_runtime_does_not_advance_before_next_due(self):
        runtime = Mock()
        service = CompanyBackgroundService(
            self.engine, runtime=runtime, finance_adapter=Mock(),
            profile={"coffee": {}, "ecommerce": {}}, company_binding=self.binding,
        )
        service._initialized = True
        service._initialization_attempted = True
        service._initialized_providers = {"coffeebench", "ecommercebench"}
        service._provider_bindings = {"coffeebench": {}, "ecommercebench": {}}
        service._next_due = {"coffeebench": datetime.now(timezone.utc) + timedelta(hours=1)}
        service._advance_once(self.engine.company_state(), datetime.now(timezone.utc))
        runtime.advance_company.assert_not_called()

    def test_stale_failed_provider_due_does_not_bypass_retry_backoff(self):
        runtime = Mock()
        runtime.advance_company.return_value = {
            "providers": {}, "provider_errors": {"coffeebench": {
                "code": "worker_transport_uncertain", "status_code": None,
            }},
        }
        service = CompanyBackgroundService(
            self.engine, runtime=runtime, finance_adapter=Mock(),
            profile={}, company_binding=self.binding,
        )
        now = datetime.now(timezone.utc)
        service._initialized = True
        service._initialization_attempted = True
        service._initialized_providers = {"coffeebench", "ecommercebench"}
        service._provider_bindings = {"coffeebench": {}, "ecommercebench": {}}
        service._next_due = {"coffeebench": now - timedelta(seconds=1)}
        service.errors = {"coffeebench": {"code": "worker_transport_uncertain"}}
        service._retry_at = now + timedelta(seconds=30)

        service._advance_once(self.engine.company_state(), now)
        runtime.advance_company.assert_not_called()
        service._advance_once(self.engine.company_state(), service._retry_at)
        runtime.advance_company.assert_called_once()

    def test_coupled_coffee_wait_is_visible_during_ecommerce_retry_backoff(self):
        runtime = Mock()
        service = CompanyBackgroundService(
            self.engine, runtime=runtime, finance_adapter=Mock(),
            profile={"coffee": {}, "ecommerce": {}, "coupling": {
                "ecommerce_to_coffee": {"affected_categories": ["all"],
                                         "target_scope": "native_coffee_market",
                                         "native_effect": "delivery_lag_days"},
            }}, company_binding=self.binding,
        )
        now = datetime.now(timezone.utc)
        service._initialization_attempted = True
        service._initialized_providers = {"coffeebench", "ecommercebench"}
        service._provider_bindings = {"coffeebench": {}, "ecommercebench": {}}
        service._next_due = {"coffeebench": now - timedelta(minutes=1),
                             "ecommercebench": now + timedelta(hours=1)}
        service.errors = {"ecommercebench": {"code": "worker_transport_uncertain"}}
        service._retry_at = now + timedelta(seconds=30)

        service._advance_once(self.engine.company_state(), now)

        runtime.advance_company.assert_not_called()
        self.assertEqual(service.status()["errors"]["coffeebench"]["code"],
                         "waiting_for_ecommerce_source")

    def test_successfully_initialized_provider_advances_when_sibling_init_fails(self):
        now = datetime.now(timezone.utc)
        runtime = Mock()
        runtime.initialize_company.return_value = {
            "providers": {"coffeebench": {"cursor": now.isoformat(),
                                           "next_due": (now - timedelta(seconds=1)).isoformat()}},
            "errors": {"ecommercebench": {"code": "worker_transport_uncertain", "status_code": None}},
        }
        runtime.advance_company.return_value = {
            "providers": {"coffeebench": {"next_due": (now + timedelta(hours=1)).isoformat(),
                                           "erp_intents": [], "events": []}},
            "provider_errors": {"ecommercebench": {"code": "worker_transport_uncertain", "status_code": None}},
        }
        finance = Mock()
        finance.read_due_obligations.return_value = []
        service = CompanyBackgroundService(
            self.engine, runtime=runtime, finance_adapter=finance,
            profile={"seed": 17, "model_id": "gpt-6-luna",
                     "coffee": {"company_business_ids": ["roaster_A"], "max_days": 90},
                     "ecommerce": {"supplier_actor_id": "supplier", "store_type": "auto_hardware"}},
            company_binding=self.binding,
        )

        service._advance_once(self.engine.company_state(), now)

        runtime.advance_company.assert_called_once()
        self.assertEqual(runtime.advance_company.call_args.kwargs["provider_ids"], ("coffeebench",))
        self.assertIn("coffeebench", service._initialized_providers)
        self.assertEqual(service.status()["errors"]["ecommercebench"]["code"], "worker_transport_uncertain")

    def test_profile_finance_fields_augment_the_documented_raw_binding(self):
        service = CompanyBackgroundService(
            self.engine, runtime=Mock(), finance_adapter=Mock(),
            profile={"coffee": {
                "company_business_ids": ["roaster_A"],
                "counterparties": {"farmer_A": {
                    "actor_id": "coffee_farmer_A",
                    "email": "coffee-farmer_A@example.com",
                    "display_name": "CoffeeBench Farmer A",
                    "kind": "counterparty",
                }},
            }},
            company_binding={
                "site_name": self.binding["site_name"], "credentials_ref": "/private/site.json",
                "documents": {"company": "The Agent Company", "supplier": "SUP-001"},
            },
        )
        service._provider_started_at = datetime(2026, 10, 30, tzinfo=timezone.utc)

        binding = service._finance_binding()

        self.assertEqual(binding["site_name"], self.binding["site_name"])
        self.assertEqual(binding["company_id"], "company")
        self.assertEqual(binding["erp_company"], "The Agent Company")
        self.assertEqual(binding["company_business_ids"], ["roaster_A"])
        self.assertEqual(binding["started_at"], "2026-10-30T00:00:00+00:00")
        self.assertEqual(binding["counterparty_mail_directory"]["farmer_A"]["actor_id"], "coffee_farmer_A")

    def test_due_obligations_are_observed_by_native_version_without_balance_history(self):
        adapter = Mock()
        adapter.read_due_obligations.return_value = [{
            "doctype": "Purchase Invoice", "name": "PINV-001", "supplier": "SUP-001",
            "due_date": "2026-10-30", "modified": "2026-10-29 12:00:00",
            "currency": "GBP", "grand_total": 120, "outstanding_amount": 80,
        }]
        self.service.finance_adapter = adapter
        observed_at = datetime.now(timezone.utc)
        self.service._read_due_obligations(observed_at)
        self.service._read_due_obligations(observed_at + timedelta(minutes=1))

        status = self.service.status()
        self.assertEqual(status["obligations"][0]["name"], "PINV-001")
        self.assertEqual(status["obligations"][0]["outstanding_amount"], 80)
        observations = self.store.list_company_due_obligation_observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["modified"], "2026-10-29 12:00:00")
        activity = self.service.activity()
        self.assertEqual(len(activity), 1)
        self.assertNotIn("outstanding_amount", activity[0])

    def test_due_read_failure_is_visible_without_failing_provider_advance(self):
        adapter = Mock()
        adapter.read_due_obligations.side_effect = OSError("ERP unavailable")
        runtime = Mock()
        runtime.advance_company.return_value = {
            "providers": {"coffeebench": {"next_due": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}},
            "provider_errors": {},
        }
        self.service.runtime = runtime
        self.service.finance_adapter = adapter
        self.service._provider_bindings = {"coffeebench": {}, "ecommercebench": {}}
        self.service._initialized = True
        self.service._initialization_attempted = True
        self.service._initialized_providers = {"coffeebench", "ecommercebench"}
        self.service._next_due = {"coffeebench": datetime.now(timezone.utc) - timedelta(seconds=1)}

        self.service._advance_once(self.engine.company_state(), datetime.now(timezone.utc))

        runtime.advance_company.assert_called_once()
        self.assertEqual(self.service.status()["obligations_error"], "OSError")

    def _prepare_controlled_background(self):
        now = datetime.now(timezone.utc)
        self.service._initialized = True
        self.service._initialization_attempted = True
        self.service._initialized_providers = {"coffeebench", "ecommercebench"}
        self.service._provider_bindings = {"coffeebench": {}, "ecommercebench": {}}
        self.service._next_due = {"coffeebench": now - timedelta(seconds=1),
                                   "ecommercebench": now - timedelta(seconds=1)}
        self.service.runtime.advance_company.return_value = {"providers": {}, "provider_errors": {}}
        self.service.finance_adapter.read_due_obligations.return_value = []
        return now

    def test_stop_is_durable_and_does_not_stop_ecommerce_or_erase_source_state(self):
        now = self._prepare_controlled_background()
        self.store.persist_background_provider_pending("coffeebench", "interrupted", {"original": True})
        self.service.control(False, "stop-1")
        reopened = SQLiteStore(self.store.db_path)
        self.assertFalse(reopened.background_provider_control("coffeebench")["enabled"])
        self.assertEqual(reopened.get_background_provider_pending("coffeebench")["operation_id"], "interrupted")
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service.runtime.advance_company.call_args.kwargs["provider_ids"], ("ecommercebench",))
        self.service.runtime.recover_pending_coffeebench.assert_not_called()
        self.assertEqual(self.service.status()["providers"]["coffeebench"]["status"], "stopped")

    def test_start_queues_recovery_and_retries_keep_original_operation_key(self):
        self._prepare_controlled_background()
        self.store.persist_background_provider_pending("coffeebench", "interrupted", {"original": True})
        self.service.runtime.recover_pending_coffeebench.return_value = {"status": "uncertain", "code": "worker_uncertain"}
        self.assertEqual(self.service.control(True, "start-1")["status"], "starting")
        self.service.runtime.recover_pending_coffeebench.assert_not_called()
        self.service._recover_requested_coffee()
        first = self.service.runtime.recover_pending_coffeebench.call_args
        self.assertFalse(self.store.background_provider_control("coffeebench")["enabled"])
        self.service.control(True, "start-2")
        self.service._recover_requested_coffee()
        self.assertEqual(self.service.runtime.recover_pending_coffeebench.call_args, first)
        self.assertEqual(first.args[0], "interrupted")
        self.assertEqual(self.service.status()["errors"]["coffeebench"]["code"], "worker_uncertain")

    def test_stop_during_recovery_preserves_stop_and_reports_in_flight(self):
        self._prepare_controlled_background()
        self.store.persist_background_provider_pending("coffeebench", "interrupted", {"original": True})
        def recover(*_):
            state = self.service.control(False, "stop-during-recovery")
            self.assertEqual(state["status"], "stopping")
            self.assertTrue(state["in_flight"])
            return {"status": "completed"}
        self.service.runtime.recover_pending_coffeebench.side_effect = recover
        self.service.control(True, "start-1")
        self.service._recover_requested_coffee()
        self.assertFalse(self.store.background_provider_control("coffeebench")["enabled"])
        self.assertEqual(self.service._provider_status("coffeebench")["status"], "stopped")

    def test_stop_during_native_advance_allows_only_current_operation_to_finish(self):
        now = self._prepare_controlled_background()
        def advance(*_, **kwargs):
            self.assertIn("coffeebench", kwargs["provider_ids"])
            self.assertEqual(self.service.control(False, "stop-in-flight")["status"], "stopping")
            return {"providers": {}, "provider_errors": {}}
        self.service.runtime.advance_company.side_effect = advance
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service._provider_status("coffeebench")["status"], "stopped")
        self.service.runtime.advance_company.reset_mock(side_effect=True)
        self.service.runtime.advance_company.return_value = {"providers": {}, "provider_errors": {}}
        self.service._advance_once(self.engine.company_state(), now)
        self.assertNotIn("coffeebench", self.service.runtime.advance_company.call_args.kwargs["provider_ids"])

    def test_stop_reports_stopping_until_current_finance_reconciliation_finishes(self):
        now = self._prepare_controlled_background()
        def read_obligations(*_):
            self.assertEqual(self.service.control(False, "stop-finishing-finance")["status"], "stopping")
            return []
        self.service.finance_adapter.read_due_obligations.side_effect = read_obligations
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service._provider_status("coffeebench")["status"], "stopped")

    def test_disabled_error_backoff_does_not_keep_an_expired_deadline(self):
        now = self._prepare_controlled_background()
        self.service._next_due = {"coffeebench": now - timedelta(seconds=1)}
        self.service.errors["coffeebench"] = {"code": "background_operation_uncertain"}
        self.service._retry_at = now - timedelta(seconds=1)
        self.service.control(False, "stop-backoff")
        self.service._advance_once(self.engine.company_state(), now)
        self.assertGreater(self.service._retry_at, now)
        self.service.runtime.advance_company.assert_not_called()

    def test_coffee_control_does_not_postpone_a_future_ecommerce_retry(self):
        now = self._prepare_controlled_background()
        self.service._next_due = {}
        future = now + timedelta(seconds=12)
        self.service._retry_at = future
        self.service.control(True, "start-future-backoff")
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service._retry_at, future)
        self.service.runtime.advance_company.assert_not_called()

    def test_start_recovery_does_not_get_stuck_behind_paused_company_clock(self):
        self._prepare_controlled_background()
        self.store.persist_background_provider_pending("coffeebench", "interrupted", {"original": True})
        self.service.runtime.recover_pending_coffeebench.return_value = {"status": "completed"}
        self.service.control(True, "start-paused")
        company = {**self.engine.company_state(), "paused": True}
        self.engine.company_state = Mock(return_value=company)
        self.service.changed.wait = Mock(side_effect=lambda: self.service.stopped.set())
        self.service._run()
        self.service.runtime.recover_pending_coffeebench.assert_called_once()
        self.service.runtime.advance_company.assert_not_called()

    def test_old_control_retry_cannot_reverse_a_newer_stop(self):
        self.service.control(True, "start-1")
        self.service.control(False, "stop-2")
        replay = self.service.control(True, "start-1")
        self.assertFalse(replay["enabled"])

    def test_ecommerce_stop_and_start_are_durable_independent_of_coffee(self):
        now = self._prepare_controlled_background()
        self.store.persist_background_provider_pending("ecommercebench", "ecom-existing", {"source": "unchanged"})
        self.service.control(False, "stop-coffee")
        response = self.service.control(False, "stop-ecom", provider_id="ecommercebench")
        self.assertEqual(response["status"], "stopped")
        reopened = SQLiteStore(self.store.db_path)
        self.assertFalse(reopened.background_provider_control("ecommercebench")["enabled"])
        self.assertEqual(reopened.get_background_provider_pending("ecommercebench")["operation_id"], "ecom-existing")
        self.service._advance_once(self.engine.company_state(), now)
        self.service.runtime.advance_company.assert_not_called()
        self.assertTrue(self.service.control(True, "start-ecom", provider_id="ecommercebench")["enabled"])
        self.assertFalse(self.store.background_provider_control("ecommercebench")["recovery_requested"])
        self.assertFalse(self.store.background_provider_control("coffeebench")["enabled"])
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service.runtime.advance_company.call_args.kwargs["provider_ids"], ("ecommercebench",))
        self.service.runtime.recover_pending_coffeebench.assert_not_called()
        self.assertFalse(self.service.control(False, "stop-ecom-2", provider_id="ecommercebench")["enabled"])
        self.assertFalse(self.service.control(True, "start-ecom", provider_id="ecommercebench")["enabled"])

    def test_ecommerce_stop_in_flight_tracks_full_batch_and_preserves_coffee(self):
        now = self._prepare_controlled_background()
        def advance(*_, **kwargs):
            self.assertIn("ecommercebench", kwargs["provider_ids"])
            state = self.service.control(False, "stop-ecom-inflight", provider_id="ecommercebench")
            self.assertTrue(state["in_flight"])
            self.assertEqual(state["status"], "stopping")
            self.assertTrue(self.service._provider_status("coffeebench")["enabled"])
            return {"providers": {}, "provider_errors": {}}
        self.service.runtime.advance_company.side_effect = advance
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service._provider_status("ecommercebench")["status"], "stopped")
        self.service.runtime.advance_company.reset_mock(side_effect=True)
        self.service.runtime.advance_company.return_value = {"providers": {}, "provider_errors": {}}
        self.service._advance_once(self.engine.company_state(), now)
        self.assertEqual(self.service.runtime.advance_company.call_args.kwargs["provider_ids"], ("coffeebench",))

    def test_coupled_pending_source_selection_is_tracked_before_native_dispatch(self):
        now = self._prepare_controlled_background()
        self.service.profile["coupling"] = {"ecommerce_to_coffee": {"native_effect": "delivery_lag_days"}}
        self.service._next_due["ecommercebench"] = now + timedelta(days=1)
        for provider in ("coffeebench", "ecommercebench"):
            self.store.persist_background_provider_pending(provider, provider + "-pending", {"source": "unchanged"})
        def advance(*_, **kwargs):
            self.assertEqual(set(kwargs["provider_ids"]), {"coffeebench", "ecommercebench"})
            state = self.service.control(False, "stop-coupled-ecom", provider_id="ecommercebench")
            self.assertEqual(state["status"], "stopping")
            self.assertTrue(state["in_flight"])
            return {"providers": {}, "provider_errors": {}}
        self.service.runtime.advance_company.side_effect = advance
        self.service._advance_once(self.engine.company_state(), now)

    def test_both_stopped_providers_wait_for_an_event_not_expired_due_time(self):
        self._prepare_controlled_background()
        self.service.control(False, "stop-both-coffee")
        self.service.control(False, "stop-both-ecom", provider_id="ecommercebench")
        self.service.changed.wait = Mock(side_effect=lambda **_: self.service.stopped.set())
        self.service._run()
        self.service.changed.wait.assert_called_once_with(timeout=None)
        self.service.runtime.advance_company.assert_not_called()

    def test_ecommerce_control_api_is_controller_only_and_rejects_unknown_providers(self):
        from fastapi.testclient import TestClient
        from servers.simulation.app import create_app
        client = TestClient(create_app(self.engine, {
            "operator": {"actor_id": "operator", "role": "controller"},
            "viv": {"actor_id": "viv", "role": "agent"},
        }))
        path = "/company/background/ecommercebench/control"
        self.assertEqual(client.post(path, json={"enabled": False}, headers={
            "Authorization": "Bearer viv", "Idempotency-Key": "no"}).status_code, 403)
        response = client.post(path, json={"enabled": False}, headers={
            "Authorization": "Bearer operator", "Idempotency-Key": "stop-ecom-api"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["provider_id"], "ecommercebench")
        self.assertFalse(response.json()["enabled"])
        invalid = client.post("/company/background/unknown/control", json={"enabled": True}, headers={
            "Authorization": "Bearer operator", "Idempotency-Key": "unknown"})
        self.assertEqual(invalid.status_code, 422)

    def test_control_api_requires_controller_key_and_strict_boolean(self):
        from fastapi.testclient import TestClient
        from servers.simulation.app import create_app
        client = TestClient(create_app(self.engine, {
            "operator": {"actor_id": "operator", "role": "controller"},
            "viv": {"actor_id": "viv", "role": "agent"},
        }))
        path = "/company/background/coffeebench/control"
        self.assertEqual(client.post(path, json={"enabled": False}, headers={"Idempotency-Key": "anonymous"}).status_code, 401)
        self.assertEqual(client.post(path, json={"enabled": False}, headers={
            "Authorization": "Bearer viv", "Idempotency-Key": "denied"}).status_code, 403)
        self.assertEqual(client.post(path, json={"enabled": "false"}, headers={
            "Authorization": "Bearer operator", "Idempotency-Key": "invalid"}).status_code, 422)
        response = client.post(path, json={"enabled": False}, headers={
            "Authorization": "Bearer operator", "Idempotency-Key": "stop-api"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "stopped")
        self.service.runtime.recover_pending_coffeebench.assert_not_called()


if __name__ == "__main__":
    unittest.main()
