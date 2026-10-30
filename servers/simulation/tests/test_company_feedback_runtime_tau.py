from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from servers.simulation.company_feedback_runtime import CompanyFeedbackRuntime
from servers.simulation.store import SQLiteStore
from servers.simulation.company_feedback import CompanyFeedback
from servers.simulation.evidence_observer import native_document_evidence
from servers.simulation.feedback_contracts import NativeDocumentEvent


def test_tau_mapping_preserves_each_original_write_and_initial_email_reference():
    run_id = "run-retail-1"
    task = {
        "id": "synthetic-task",
        "evaluation_criteria": {
            "actions": [
                {
                    "action_id": "cancel-1",
                    "name": "cancel_pending_order",
                    "arguments": {
                        "order_id": "#W-SYNTHETIC",
                        "reason": "no longer needed",
                    },
                },
                {
                    "action_id": "address-1",
                    "name": "modify_pending_order_address",
                    "arguments": {"order_id": "#W-SYNTHETIC", "address1": "1 New Way"},
                },
                {
                    "action_id": "read-1",
                    "name": "get_order_details",
                    "arguments": {"order_id": "#W-SYNTHETIC"},
                },
            ]
        },
    }
    prepared = {
        "task_id": task["id"],
        "snapshot": {
            "task": task,
            "upstream": {"commit": "7aad81c37a3a78b4ed3e4866769f851400c64415"},
            "original_db": {
                "orders": {
                    "#W-SYNTHETIC": {"order_id": "#W-SYNTHETIC", "status": "pending"}
                }
            },
        },
        "import_receipt": {
            "native_documents": {"orders": {"#W-SYNTHETIC": "SO-SYNTHETIC"}}
        },
        "initial_email": {"message_id": "<run-retail-1.seed@tau-retail.local>"},
    }
    order_document = {
        "doctype": "Sales Order",
        "name": "SO-SYNTHETIC",
        "company": "The Agent Company",
        "custom_source_key": "tau-source-order",
        "custom_source_id": "#W-SYNTHETIC",
        "custom_customer_service_state": "Pending",
        "docstatus": 1,
    }

    class Store:
        def company_experiments(self):
            return [{"experiment_id": run_id, "scenario_id": "TAU_RETAIL"}]

        def get_run(self, _run_id):
            return {"manifest": {"prepared_problem": prepared}}

    class Pipeline:
        def __init__(self):
            self.rows = []

        def map_record(self, mapping, baseline):
            self.rows.append((mapping, baseline))

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = Store()
    runtime.pipeline = Pipeline()
    runtime._register_tau_retail_mappings(
        [
            {
                "doctype": "Sales Order",
                "name": "SO-SYNTHETIC",
                "source_id": "#W-SYNTHETIC",
                "source_key": "tau-source-order",
                "document": order_document,
            }
        ],
        set(),
        datetime(2026, 10, 30, tzinfo=timezone.utc),
    )

    assert len(runtime.pipeline.rows) == 3
    by_action = {
        mapping.native_context["native_action"]: mapping
        for mapping, _ in runtime.pipeline.rows
        if "native_action" in mapping.native_context
    }
    cancel = by_action["cancel_pending_order"]
    address = by_action["modify_pending_order_address"]
    assert cancel.source_record_id == "#W-SYNTHETIC"
    assert cancel.native_context["source_snapshot"]["status"] == "pending"
    assert cancel.native_context["document_fields"] == {
        "before_state": "before_document.custom_customer_service_state",
        "docstatus": "docstatus",
        "current_state": "custom_customer_service_state",
        "reason": "custom_customer_service_cancel_reason",
    }
    assert address.native_context["document_fields"] == {}
    assert cancel.message_ids == ["<run-retail-1.seed@tau-retail.local>"]
    assert cancel.experiment_id == address.experiment_id == run_id
    observation = next(
        mapping
        for mapping, _ in runtime.pipeline.rows
        if mapping.native_context.get("observation_only")
    )
    assert observation.source_record_id == "#W-SYNTHETIC"
    assert observation.native_context["task_id"] == task["id"]
    assert runtime.pipeline.rows[0][1] is order_document


def test_tau_user_address_mapping_uses_imported_customer_and_address_records():
    task = {"id": "address-task", "evaluation_criteria": {"actions": [{
        "action_id": "user-address-1", "name": "modify_user_address",
        "arguments": {"user_id": "user-1", "address1": "2 New Way"},
    }]}}
    user = {"user_id": "user-1", "address": {"address1": "1 Old Way"}}
    prepared = {
        "task_id": task["id"],
        "snapshot": {
            "task": task,
            "upstream": {"commit": "7aad81c37a3a78b4ed3e4866769f851400c64415"},
            "original_db": {"users": {"user-1": user}, "orders": {}},
        },
        "import_receipt": {"native_documents": {
            "users": {"user-1": "CUST-1"},
            "addresses": {"user:user-1": "ADDR-1"},
            "orders": {},
        }},
        "initial_email": {"message_id": "<address.seed@test>"},
    }

    class Store:
        def company_experiments(self):
            return [{"experiment_id": "address-run", "scenario_id": "TAU_RETAIL"}]

        def get_run(self, _run_id):
            return {"manifest": {"prepared_problem": prepared}}

    class Pipeline:
        def __init__(self):
            self.rows = []

        def map_record(self, mapping, baseline):
            self.rows.append((mapping, baseline))

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = Store()
    runtime.pipeline = Pipeline()
    runtime._register_tau_retail_mappings([{
        "doctype": "Address", "name": "ADDR-1", "source_id": "user-1",
        "document": {"doctype": "Address", "name": "ADDR-1"},
    }], set(), datetime(2026, 10, 30, tzinfo=timezone.utc))

    mapping = next(
        mapping for mapping, _ in runtime.pipeline.rows
        if mapping.native_context.get("native_action") == "modify_user_address"
    )
    assert mapping.source_record_id == "user-1"
    assert mapping.erp_doctype == "Address"
    assert mapping.erp_name == "ADDR-1"
    assert mapping.native_context["native_ids"] == {
        "user_id": "user-1", "customer_name": "CUST-1",
    }
    assert mapping.native_context["document_fields"]["address1"] == "address_line1"
    assert mapping.message_ids == ["<address.seed@test>"]


def test_address_evidence_requires_signed_company_link_context():
    payload = {
        "event": "on_update",
        "erp_timezone": "Europe/London",
        "linked_company": "The Agent Company",
        "document": {
            "doctype": "Address", "name": "ADDR-1",
            "modified": "2026-10-30 12:00:00", "modified_by": "operator@example.test",
            "links": [{"link_doctype": "Customer", "link_name": "CUST-1"}],
            "address_line1": "2 New Way",
        },
    }
    evidence = native_document_evidence(
        NativeDocumentEvent.model_validate(payload),
        erp_public_url="https://erp.example.test",
    )
    assert evidence.erp_doctype == "Address"
    assert evidence.document["links"][0]["link_name"] == "CUST-1"

    import pytest
    payload["linked_company"] = None
    with pytest.raises(ValueError, match="outside the bound ERP company"):
        native_document_evidence(
            NativeDocumentEvent.model_validate(payload),
            erp_public_url="https://erp.example.test",
        )


def test_tau_return_task_maps_the_real_linked_service_issue():
    task = {
        "id": "return-task",
        "evaluation_criteria": {"actions": [{
            "action_id": "return-1", "name": "return_delivered_order_items",
            "arguments": {"order_id": "#W-RETURN", "item_ids": ["item-1"],
                           "payment_method_id": "gift-1"},
        }]},
    }
    manifest = {
        "task_id": task["id"], "task": task,
        "upstream": {"commit": "7aad81c37a3a78b4ed3e4866769f851400c64415"},
        "original_db": {"orders": {"#W-RETURN": {
            "order_id": "#W-RETURN", "user_id": "user-return", "status": "delivered",
            "payment_history": [],
        }}},
    }
    prepared = {
        "task_id": task["id"], "snapshot": manifest,
        "import_receipt": {"native_documents": {"orders": {"#W-RETURN": "SO-RETURN"}}},
        "initial_email": {"message_id": "<return.seed@test>"},
    }

    class Store:
        def company_experiments(self):
            return [{"experiment_id": "return-run", "scenario_id": "TAU_RETAIL"}]

        def get_run(self, _run_id):
            return {"manifest": {"prepared_problem": prepared}}

    class Pipeline:
        def __init__(self):
            self.rows = []

        def map_record(self, mapping, baseline):
            self.rows.append((mapping, baseline))

    order_document = {"doctype": "Sales Order", "name": "SO-RETURN",
                      "custom_source_id": "#W-RETURN", "docstatus": 1}
    issue_document = {
        "doctype": "Issue", "name": "ISS-RETURN", "status": "Open",
        "custom_customer_service_order": "SO-RETURN",
        "custom_customer_service_type": "Return",
    }
    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = Store()
    runtime.pipeline = Pipeline()
    runtime._register_tau_retail_mappings([
        {"doctype": "Sales Order", "name": "SO-RETURN", "source_id": "#W-RETURN",
         "document": order_document},
        {"doctype": "Issue", "name": "ISS-RETURN", "document": issue_document},
    ], set(), datetime(2026, 10, 30, tzinfo=timezone.utc))

    request_mapping = next(
        mapping for mapping, _baseline in runtime.pipeline.rows
        if mapping.native_context.get("native_action") == "return_delivered_order_items"
    )
    assert request_mapping.erp_doctype == "Issue"
    assert request_mapping.erp_name == "ISS-RETURN"
    assert request_mapping.source_record_id == "#W-RETURN"
    assert request_mapping.native_context["native_ids"] == {
        "order_id": "#W-RETURN", "sales_order_name": "SO-RETURN"
    }
    assert request_mapping.message_ids == ["<return.seed@test>"]


def test_ecommerce_runtime_bootstraps_from_company_profile_and_advances_to_shared_clock(
    tmp_path,
):
    profile_path = tmp_path / "company.json"
    profile_path.write_text(
        json.dumps(
            {
                "company": {
                    "id": "company",
                    "name": "Company",
                    "background": {
                        "ecommerce": {
                            "sales_order_date_semantics": "source_dispatch_deadline",
                            "money_unit": {
                                "currency": "GBP",
                                "native_units_per_currency_unit": "1",
                            },
                        }
                    },
                }
            }
        )
    )
    calls = []
    imported_receipts = [{"source_id": "catalog:store:product:SKU", "doctype": "Item", "name": "SKU"}]

    class Ecommerce:
        def bootstrap_from_profile(self, profile, seed, started_at):
            calls.append(("bootstrap", profile, seed, started_at))
            return {"status": "initialized"}

        def advance_to(self, company_time):
            calls.append(("advance", company_time))
            return {
                "status": "active",
                "source_records": [],
                "unmapped_native_transactions": [],
            }

        def acknowledge_source_receipts(self, receipts):
            calls.append(("acknowledge", receipts))

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = SQLiteStore(tmp_path / "state.sqlite")
    runtime.store.background_initialization_request(
        {
            "company_id": "company",
            "provider_id": "ecommercebench",
            "seed": 17,
            "started_at": "2026-10-30T09:00:00+00:00",
            "binding": {},
        }
    )
    runtime.config = SimpleNamespace(company_profile_file=profile_path)
    runtime.pipeline = CompanyFeedback(runtime.store, {"ecommercebench": Ecommerce()})
    runtime.lifecycle = {}

    class Native:
        def reconcile_ecommerce_finance(self, records, binding, as_of):
            assert records == []
            assert binding["money_unit"] == {
                "currency": "GBP",
                "native_units_per_currency_unit": "1",
            }
            assert as_of == "2026-10-30T10:00:00+00:00"
            calls.append(("import",))
            return {"receipts": imported_receipts}

    runtime.native = Native()
    now = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)

    runtime._advance_ecommerce_lifecycle(now)

    assert calls == [
        (
            "bootstrap",
            {
                "company": {
                    "id": "company",
                    "name": "Company",
                    "background": {
                        "ecommerce": {
                            "sales_order_date_semantics": "source_dispatch_deadline",
                            "money_unit": {
                                "currency": "GBP",
                                "native_units_per_currency_unit": "1",
                            },
                        }
                    },
                }
            },
            17,
            "2026-10-30T09:00:00+00:00",
        ),
        ("advance", now),
        ("import",),
        ("acknowledge", imported_receipts),
    ]
    assert runtime.lifecycle["ecommercebench"]["advance"]["status"] == "active"
    with runtime.store.transaction() as db:
        saved = json.loads(
            db.execute(
                "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='ecommercebench'"
            ).fetchone()[0]
        )
    assert saved["erp_reconciliation"] == {"receipts": imported_receipts}
    assert saved["advance"]["status"] == "active"


def test_tau_callbacks_resolve_dict_actions_and_use_retail_admin_for_native_evaluation():
    mapping = SimpleNamespace(
        mapping_id="mapping-1",
        benchmark="tau_retail",
        experiment_id="run-1",
        native_context={"task_id": "synthetic-task"},
    )
    action = {"mapping_id": "mapping-1", "action_id": "action-1", "arguments": {}}
    calls = {}

    class Pipeline:
        def mappings(self):
            return [mapping]

    class Store:
        def get_run(self, run_id):
            assert run_id == "run-1"
            return {
                "run_id": "run-1",
                "generation": 0,
                "agent_id": "employee",
                "simulated_time": "2026-10-30T10:00:00+00:00",
                "manifest": {
                    "prepared_problem": {
                        "snapshot": {"task_id": "synthetic-task"},
                        "support_actor_id": "support",
                    }
                },
            }

        def list_actors(self, _run_id, _generation):
            return [{"actor_id": "customer", "kind": "customer"}]

        def list_outbox(self, _run_id, _generation, *, status):
            assert status == "completed"
            return [
                {
                    "outbox_id": "out-1",
                    "private_payload": {
                        "message_id": "<employee@company.test>",
                        "from_actor_id": "employee",
                        "to_actor_id": "customer",
                        "body": "We are reviewing your order.",
                        "simulated_time": "2026-10-30T10:01:00+00:00",
                    },
                }
            ]

        def list_private_messages(self, _run_id, _generation):
            return [
                {
                    "message_id": "<customer@customer.test>",
                    "from_actor_id": "customer",
                    "to_actor_id": "support",
                    "body": "Please help with my order.",
                    "simulated_time": "2026-10-30T10:00:00+00:00",
                }
            ]

    class RetailAdmin:
        def observe_task(self, manifest, experiment_id):
            calls["observe"] = (manifest, experiment_id)
            return {
                "task_id": "synthetic-task",
                "experiment_id": "run-1",
                "observation": {"orders": {}},
                "unmapped": [],
                "source_gaps": [],
            }

    class ActorClient:
        def apply_retail_feedback(self, *args):
            calls["apply"] = args
            return {
                "action_id": "action-1",
                "accepted": True,
                "reconciled": True,
                "native_state_ref": "state",
                "outcome": {},
            }

        def evaluate_retail_feedback(self, *args):
            calls["evaluate"] = args
            return {
                "task_id": "synthetic-task",
                "reward_status": "complete",
                "reward": 1.0,
            }

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.pipeline = Pipeline()
    runtime.store = Store()
    runtime.retail_admin = RetailAdmin()
    runtime.actor_client = ActorClient()

    assert runtime._apply_tau_retail_action(action)["accepted"] is True
    result = runtime._evaluate_tau_retail([mapping], [])

    assert calls["apply"] == ("synthetic-task", "run-1", action)
    assert calls["observe"] == ({"task_id": "synthetic-task"}, "run-1")
    assert calls["evaluate"] == (
        "synthetic-task",
        "run-1",
        {"orders": {}},
        [],
        [
            {"role": "user", "content": "Please help with my order."},
            {"role": "assistant", "content": "We are reviewing your order."},
        ],
    )
    assert result["evaluation"]["reward"] == 1.0


def test_tau_paid_cancellation_enrichment_uses_current_native_erp_payment_observation():
    mapping = SimpleNamespace(
        mapping_id="cancel-map",
        benchmark="tau_retail",
        experiment_id="run-2",
        source_record_id="#W-SYNTHETIC",
        native_context={
            "task_id": "synthetic-task",
            "native_action": "cancel_pending_order",
        },
    )
    calls = {}

    class Pipeline:
        def mappings(self):
            return [mapping]

    class Store:
        def get_run(self, run_id):
            return {
                "run_id": run_id,
                "manifest": {
                    "prepared_problem": {
                        "snapshot": {"task_id": "synthetic-task"},
                    }
                },
            }

    class RetailAdmin:
        def observe_task(self, manifest, experiment_id):
            calls["observer"] = (manifest, experiment_id)
            return {
                "task_id": "synthetic-task",
                "experiment_id": "run-2",
                "observation": {
                    "orders": {
                        "#W-SYNTHETIC": {
                            "order_id": "#W-SYNTHETIC",
                            "user_id": "user-synthetic",
                            "payment_history": [
                                {
                                    "transaction_type": "payment",
                                    "amount": 12.5,
                                    "payment_method_id": "card-1",
                                },
                                {
                                    "transaction_type": "refund",
                                    "amount": 12.5,
                                    "payment_method_id": "card-1",
                                },
                            ],
                        }
                    },
                    "users": {
                        "user-synthetic": {
                            "user_id": "user-synthetic",
                            "payment_methods": {"card-1": {"source": "credit_card"}},
                        }
                    },
                },
                "native_evidence": {
                    "#W-SYNTHETIC": [
                        {
                            "doctype": "Sales Order",
                            "name": "SO-SYNTHETIC",
                            "docstatus": 2,
                            "modified": "2026-10-30 12:00:00",
                        },
                        {
                            "doctype": "Payment Entry",
                            "name": "PE-SYNTHETIC",
                            "docstatus": 1,
                            "modified": "2026-10-30 12:01:00",
                        },
                    ]
                },
            }

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.pipeline = Pipeline()
    runtime.store = Store()
    runtime.retail_admin = RetailAdmin()
    runtime.company_time = lambda: datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    evidence = {"event": "on_cancel", "document": {"docstatus": 2}}

    enriched = runtime._enrich_tau_retail_evidence(
        mapping.model_dump()
        if hasattr(mapping, "model_dump")
        else {
            "mapping_id": mapping.mapping_id,
            "source_record_id": mapping.source_record_id,
            "native_context": mapping.native_context,
        },
        evidence,
    )

    assert calls["observer"] == ({"task_id": "synthetic-task"}, "run-2")
    assert (
        enriched["native_observation"]["order"]["payment_history"][-1][
            "transaction_type"
        ]
        == "refund"
    )
    assert (
        enriched["native_observation"]["native_evidence"][1]["doctype"]
        == "Payment Entry"
    )
    assert enriched["native_observation"]["user"]["user_id"] == "user-synthetic"
    assert enriched["native_observed_company_time"] == "2026-10-30T12:00:00+00:00"


def test_one_failed_provider_does_not_block_other_pending_feedback(tmp_path):
    import pytest

    calls = []
    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.store = SQLiteStore(tmp_path / "state.sqlite")
    CompanyFeedback(runtime.store, {})
    runtime.pipeline = SimpleNamespace(
        reconcile=lambda: calls.append("native_actions"),
        translate_captured=lambda: calls.append("evidence_translation"),
    )
    runtime.company_time = lambda: datetime(2026, 10, 30, tzinfo=timezone.utc)
    runtime._refresh_evidence = lambda: calls.append("native_evidence")
    runtime._observe_linked_mail = lambda: calls.append("mail")

    def unavailable(_time):
        raise ConnectionError("synthetic provider transport unavailable")

    runtime._advance_ecommerce_lifecycle = unavailable
    with pytest.raises(ConnectionError):
        runtime.maintenance()
    assert calls == [
        "native_evidence",
        "mail",
        "evidence_translation",
        "native_actions",
    ]
    with runtime.store.transaction() as db:
        status = json.loads(
            db.execute(
                "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='workflow_health'"
            ).fetchone()[0]
        )
    assert status["status"] == "failed"
    assert status["error_type"] == "ConnectionError"
