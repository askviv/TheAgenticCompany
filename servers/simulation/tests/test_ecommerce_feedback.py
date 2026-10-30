from datetime import date

import pytest

from servers.simulation.ecommerce_feedback import (
    EcommerceNativeActionCheckpoint,
    apply_native,
    initialize_native_store,
    translate,
)
from servers.simulation.ecommerce_background import calendar_supply_conditions
from servers.simulation.ecommerce_background import PROVIDER_ID, UPSTREAM_COMMIT
from servers.simulation.store import SQLiteStore, StoreConflict
from third_party.ecommercebench.upstream.tools.ecommerce_env import EcommerceEnv


def _mapping(**changes):
    mapping = {
        "mapping_id": "map-ecom-1",
        "company_id": "company",
        "benchmark": "ecommercebench",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": "delivery-note-source-1",
        "source_entity_type": "pending-sale",
        "erp_doctype": "Sales Order",
        "erp_name": "SO-1",
        "experiment_id": None,
        "source_time": None,
        "company_time": "2026-10-30T12:00:00Z",
        "native_context": {
            "native_action": "ship_orders",
            "events": ["on_submit", "on_update_after_submit", "on_cancel", "on_trash"],
            "native_ids": {
                "shipment_id": 7,
                "store_id": "store_001",
                "product_id": "auto-product-1",
            },
            "source_item_code": "ECOM-auto-product-1",
            "source_quantity": 4,
            "document_fields": {"speed": "custom_source_shipping_speed"},
        },
        "message_ids": [],
    }
    mapping.update(changes)
    return mapping


def _evidence(**changes):
    evidence = {
        "evidence_id": "evidence-1",
        "company_id": "company",
        "erp_doctype": "Delivery Note",
        "erp_name": "DN-1",
        "event": "on_submit",
        "actor": "employee-1",
        "occurred_at": "2026-10-30T12:00:00Z",
        "company_time": "2026-10-30T12:00:00Z",
        "document": {
            "doctype": "Delivery Note",
            "name": "DN-1",
            "docstatus": 1,
            "items": [
                {
                    "qty": 4,
                    "against_sales_order": "SO-1",
                    "item_code": "ECOM-auto-product-1",
                }
            ],
            "custom_source_shipping_speed": "standard",
        },
        "evidence_refs": ["erp://Delivery Note/DN-1"],
        "message_id": None,
        "in_reply_to": None,
        "experiment_id": None,
    }
    evidence.update(changes)
    evidence["document"].setdefault("doctype", evidence["erp_doctype"])
    evidence["document"].setdefault("name", evidence["erp_name"])
    evidence["document"].setdefault("docstatus", 1)
    return evidence


def _environment():
    environment = EcommerceEnv(seed=17, start_date="2026-10-30", max_day=30)
    store = environment.open_store("auto_hardware", "Mapped company store")
    assert store["success"]
    product_id = "auto-product-1"
    environment.products[product_id] = {
        "product_id": product_id,
        "title": "Synthetic upstream fixture product",
        "store_type": "auto_hardware",
        "category": "Auto Parts",
        "brand": "Fixture",
        "size": "Small",
        "reference_price": 25.0,
    }
    environment.pending_shipments.append(
        {
            "shipment_id": 7,
            "store_id": store["store_id"],
            "product_id": product_id,
            "quantity": 4,
            "unit_price": 25.0,
            "revenue_gross": 100.0,
            "revenue_net": 98.0,
            "commission": 2.0,
            "base_return_rate": 0.05,
            "rr_base": 0.05,
            "rr_after_defect": 0.05,
            "rr_after_price": 0.05,
            "sale_date": date(2026, 10, 30),
            "deadline": date(2026, 11, 1),
            "ref_price": 25.0,
        }
    )
    return environment


def _profile(store_type="auto_hardware"):
    return {
        "company": {
            "id": "company",
            "name": "The Agentic Company",
            "background": {
                "ecommerce": {
                    "store_type": store_type,
                    "money_unit": {
                        "currency": "GBP",
                        "native_units_per_currency_unit": "1",
                    },
                }
            },
        }
    }


def test_translate_requires_exact_pending_shipment_binding_and_native_speed():
    translated = translate(_mapping(), _evidence())
    assert translated["status"] == "supported"
    assert translated["coverage"] == [
        f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
        "EcommerceEnv.ship_orders",
    ]
    assert translated["action"]["native_action"] == "ship_orders"
    assert translated["action"]["arguments"] == {
        "shipment_id": 7,
        "store_id": "store_001",
        "product_id": "auto-product-1",
        "quantity": 4,
        "speed": "standard",
        "delivery_note_names": ["DN-1"],
        "delivery_note_evidence_ids": ["evidence-1"],
    }

    no_speed = translate(
        _mapping(
            native_context={
                **_mapping()["native_context"],
                "document_fields": {"quantity": "items.0.qty"},
            }
        ),
        _evidence(),
    )
    wrong_meaning = translate(
        _mapping(native_context={"native_action": "withdraw", "events": ["on_submit"]}),
        _evidence(),
    )
    assert no_speed["status"] == "inconclusive"
    assert wrong_meaning["status"] == "unmapped"
    assert no_speed["action"] is None and wrong_meaning["action"] is None

    wrong_parent = translate(
        _mapping(),
        _evidence(
            document={
                "items": [
                    {
                        "qty": 4,
                        "against_sales_order": "SO-OTHER",
                        "item_code": "ECOM-auto-product-1",
                    }
                ],
                "custom_source_shipping_speed": "standard",
            }
        ),
    )
    partial_quantity = translate(
        _mapping(),
        _evidence(
            document={
                "items": [
                    {
                        "qty": 3,
                        "against_sales_order": "SO-1",
                        "item_code": "ECOM-auto-product-1",
                    }
                ],
                "custom_source_shipping_speed": "standard",
            }
        ),
    )
    assert wrong_parent["status"] == "unmapped"
    assert partial_quantity["status"] == "inconclusive"


def test_partial_delivery_notes_accumulate_to_one_atomic_native_shipment(tmp_path):
    from servers.simulation.company_feedback import CompanyFeedback
    from servers.simulation.feedback_contracts import CompanyEvidence

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    feedback = CompanyFeedback(store, {"ecommercebench": adapter})
    first = _evidence(
        evidence_id="dn-one-submit",
        erp_name="DN-ONE",
        occurred_at="2026-10-30T10:00:00+00:00",
        company_time="2026-10-30T10:00:00+00:00",
        document={
            "docstatus": 1,
            "modified": "2026-10-30 10:00:00",
            "items": [{
                "qty": 1,
                "against_sales_order": "SO-1",
                "item_code": "ECOM-auto-product-1",
            }],
            "custom_source_shipping_speed": "standard",
        },
    )
    second = _evidence(
        evidence_id="dn-two-submit",
        erp_name="DN-TWO",
        occurred_at="2026-10-30T11:00:00+00:00",
        company_time="2026-10-30T11:00:00+00:00",
        document={
            "docstatus": 1,
            "modified": "2026-10-30 11:00:00",
            "items": [{
                "qty": 3,
                "against_sales_order": "SO-1",
                "item_code": "ECOM-auto-product-1",
            }],
            "custom_source_shipping_speed": "standard",
        },
    )
    feedback.capture(CompanyEvidence.model_validate(first))
    partial = adapter.translate(_mapping(), first)
    assert partial["status"] == "inconclusive"
    assert partial["action"] is None

    feedback.capture(CompanyEvidence.model_validate(second))
    first_after_completion = adapter.translate(_mapping(), first)
    complete = adapter.translate(_mapping(), second)
    assert first_after_completion["status"] == "inconclusive"
    assert complete["status"] == "supported"
    assert complete["action"]["arguments"]["quantity"] == 4
    assert complete["action"]["supporting_evidence"] == [
        {"evidence_id": "dn-one-submit", "doctype": "Delivery Note", "name": "DN-ONE"},
        {"evidence_id": "dn-two-submit", "doctype": "Delivery Note", "name": "DN-TWO"},
    ]

    environment = _environment()
    receipt = apply_native(environment, complete["action"])
    assert receipt["status"] == "accepted"
    assert receipt["evidence"]["quantity"] == 4
    assert environment.get_pending_shipments()["count"] == 0


def test_cancelled_delivery_note_is_removed_from_cumulative_source_quantity(tmp_path):
    from servers.simulation.company_feedback import CompanyFeedback
    from servers.simulation.feedback_contracts import CompanyEvidence

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    feedback = CompanyFeedback(store, {"ecommercebench": adapter})
    active = _evidence(
        evidence_id="dn-active-submit",
        erp_name="DN-ACTIVE",
        document={
            "docstatus": 1,
            "modified": "2026-10-30 10:00:00",
            "items": [{
                "qty": 2,
                "against_sales_order": "SO-1",
                "item_code": "ECOM-auto-product-1",
            }],
            "custom_source_shipping_speed": "standard",
        },
    )
    cancelled = _evidence(
        evidence_id="dn-cancelled",
        erp_name="DN-ACTIVE",
        event="on_cancel",
        occurred_at="2026-10-30T11:00:00+00:00",
        document={
            "docstatus": 2,
            "modified": "2026-10-30 11:00:00",
            "items": [{
                "qty": 2,
                "against_sales_order": "SO-1",
                "item_code": "ECOM-auto-product-1",
            }],
            "custom_source_shipping_speed": "standard",
        },
    )
    feedback.capture(CompanyEvidence.model_validate(active))
    feedback.capture(CompanyEvidence.model_validate(cancelled))
    refreshed_active = adapter.translate(_mapping(), active)
    cancellation = adapter.translate(_mapping(), cancelled)
    assert refreshed_active["status"] == "unmapped"
    assert refreshed_active["action"] is None
    assert cancellation["status"] == "unmapped"


def test_posted_marketplace_payment_traverses_invoice_to_source_order_and_withdraws_once(tmp_path):
    from servers.simulation.company_feedback import CompanyFeedback
    from servers.simulation.feedback_contracts import CompanyEvidence, RecordMapping

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    feedback = CompanyFeedback(store, {"ecommercebench": adapter})
    mapping = _mapping(
        native_context={
            **_mapping()["native_context"],
            "native_actions": ["ship_orders", "withdraw"],
            "marketplace_customer": "E-CommerceBench Marketplace (auto_hardware)",
            "marketplace_bank_account": "E-CommerceBench auto_hardware Marketplace Bank",
            "currency": "GBP",
        }
    )
    feedback.map_record(
        RecordMapping.model_validate(mapping),
        {"doctype": "Sales Order", "name": "SO-1", "items": []},
    )
    invoice = _evidence(
        evidence_id="invoice-submit",
        erp_doctype="Sales Invoice",
        erp_name="SI-1",
        event="on_submit",
        document={
            "doctype": "Sales Invoice",
            "name": "SI-1",
            "docstatus": 1,
            "modified": "2026-10-30 10:00:00",
            "items": [{
                "sales_order": "SO-1",
                "item_code": "ECOM-auto-product-1",
                "qty": 4,
                "rate": 25,
            }],
        },
    )
    payment = _evidence(
        evidence_id="marketplace-payment-submit",
        erp_doctype="Payment Entry",
        erp_name="PE-1",
        event="on_submit",
        document={
            "doctype": "Payment Entry",
            "name": "PE-1",
            "docstatus": 1,
            "payment_type": "Receive",
            "party_type": "Customer",
            "party": "E-CommerceBench Marketplace (auto_hardware)",
            "paid_to": "E-CommerceBench auto_hardware Marketplace Bank",
            "paid_from_account_currency": "GBP",
            "paid_to_account_currency": "GBP",
            "paid_amount": "98.00",
            "received_amount": "98.00",
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": "SI-1",
                "allocated_amount": "98.00",
            }],
        },
    )
    feedback.capture(CompanyEvidence.model_validate(invoice))
    result = feedback.observe(CompanyEvidence.model_validate(payment))
    assert len(result) == 1
    assert result[0]["status"] == "pending"
    translated = result[0]["translation"]
    assert translated["status"] == "supported"
    assert translated["action"]["native_action"] == "withdraw"
    assert translated["action"]["arguments"]["amount"] == "98.00"
    assert translated["action"]["supporting_evidence"] == [
        {"evidence_id": "invoice-submit", "doctype": "Sales Invoice", "name": "SI-1"},
    ]

    environment = _environment()
    environment.platform_wallet = 98.0
    opening_bank = environment.bank_balance
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    first = adapter.apply(translated["action"])
    replay = adapter.apply(translated["action"])
    assert first == replay
    assert first["accepted"] is True
    assert first["outcome"]["evidence"]["amount"] == "98.00"
    assert environment.platform_wallet == 98.0
    with store.transaction() as connection:
        path = connection.execute(
            "SELECT checkpoint_path FROM ecommerce_native_world_state WHERE singleton=1",
        ).fetchone()[0]
    import pickle
    with open(path, "rb") as checkpoint:
        restored = pickle.load(checkpoint)
    assert restored.platform_wallet == 0.0
    assert restored.bank_balance == opening_bank + 98.0


def test_apply_calls_native_ship_orders_and_verifies_exact_result():
    translated = translate(_mapping(), _evidence())["action"]
    environment = _environment()
    before = environment.bank_balance
    result = apply_native(environment, translated)

    assert result["status"] == "accepted"
    assert result["evidence"]["shipment_id"] == 7
    assert result["evidence"]["quantity"] == 4
    assert result["evidence"]["speed"] == "standard"
    assert environment.get_pending_shipments()["count"] == 0
    assert environment.bank_balance < before

    wrong_binding = {
        **translated,
        "arguments": {**translated["arguments"], "store_id": "other-store"},
    }
    unchanged = _environment()
    result = apply_native(unchanged, wrong_binding)
    assert result["status"] == "inconclusive"
    assert unchanged.get_pending_shipments()["count"] == 1


def test_native_initialization_opens_an_actual_mapped_upstream_store():
    environment, opened = initialize_native_store(
        seed=17,
        start_date="2026-10-30",
        store_type="auto_hardware",
        store_name="Mapped company store",
    )
    assert opened["success"] is True
    assert environment.stores[opened["store_id"]].store_type == "auto_hardware"
    assert environment.get_balance()["bank_balance"] < environment.initial_balance


def test_bootstrap_is_bound_to_profile_identity_and_uses_shared_native_calendar(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    started = "2026-12-19T00:00:00+00:00"
    bootstrapped = adapter.bootstrap_from_profile(_profile(), 17, started)
    repeated = EcommerceNativeActionCheckpoint(
        SQLiteStore(tmp_path / "state.sqlite"), tmp_path / "native"
    )
    same = repeated.bootstrap_from_profile(_profile(), 17, started)
    assert bootstrapped["status"] == "initialized"
    assert same["status"] == "already_initialized"
    assert same["store_id"] == bootstrapped["store_id"]

    advanced = repeated.advance_to("2026-12-21T09:00:00+00:00")
    replay = repeated.advance_to("2026-12-21T09:00:00+00:00")
    assert replay == advanced
    assert advanced["daily_triggers"] == [
        {"date": "2026-12-20", "day": 1},
        {"date": "2026-12-21", "day": 2},
    ]
    assert "Cold Wave" in advanced["active_calendar_events"]
    assert any(
        row["event_name"] == "Cold Wave" for row in advanced["calendar_conditions"]
    )
    assert advanced["coverage"]["currency_available"] is True
    assert advanced["source_records"]
    kinds = {row["kind"] for row in advanced["source_records"]}
    assert {"catalog_product", "catalog_supplier", "bank_transaction"} <= kinds
    assert advanced["pending_native_shipments"] == 0

    with pytest.raises(StoreConflict):
        repeated.bootstrap_from_profile(_profile("pet"), 17, started)
    changed_unit = _profile()
    changed_unit["company"]["background"]["ecommerce"]["money_unit"] = {
        "currency": "USD",
        "native_units_per_currency_unit": "1",
    }
    with pytest.raises(ValueError, match="approved 1:1 GBP unit"):
        repeated.bootstrap_from_profile(changed_unit, 17, started)


def test_advance_exports_native_sales_with_explicit_company_money_mapping(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    identity = {
        "company_id": "company",
        "company_name": "The Agentic Company",
        "seed": 17,
        "started_at": "2026-10-30T00:00:00+00:00",
        "store_type": "auto_hardware",
        "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
        "upstream_commit": UPSTREAM_COMMIT,
    }
    adapter.initialize(environment, store_id="store_001", identity=identity)
    advanced = adapter.advance_to("2026-10-30T09:00:00+00:00")

    assert advanced["pending_native_shipments"] == 1
    sale = next(row for row in advanced["source_records"] if row["kind"] == "order")
    assert sale["id"] == "store_001:shipment:7"
    assert (
        sale["source"]
        == "qwenlm-ecommercebench-native-economy"
    )
    assert sale["upstream_commit"] == UPSTREAM_COMMIT
    assert sale["currency"] == "GBP"
    assert sale["amount"] == "100.0"
    assert sale["native_record"]["sale_date"] == "2026-10-30"
    assert any(
        row["native_record"]["product_id"] == "auto-product-1"
        for row in advanced["source_records"]
        if row["kind"] == "catalog_product"
    )
    catalog_product = next(row for row in advanced["source_records"] if row["kind"] == "catalog_product")
    catalog_supplier = next(row for row in advanced["source_records"] if row["kind"] == "catalog_supplier")
    assert "amount" not in catalog_product
    assert "amount" not in catalog_supplier
    adapter.acknowledge_source_receipts([
        {"source_id": catalog_product["id"], "doctype": "Item",
         "name": f"ECOM-{catalog_product['native_record']['product_id']}"},
        {"source_id": catalog_supplier["id"], "doctype": "Supplier",
         "name": catalog_supplier["native_record"]["supplier_id"]},
    ])
    assert advanced["unmapped_native_transactions"] == []
    assert advanced["coverage"]["transactions_mapped_to_erp"] == 0
    observed_at = sale["occurred_at"]
    later = adapter.advance_to("2026-10-30T10:00:00+00:00")
    later_sale = next(row for row in later["source_records"] if row["kind"] == "order")
    assert later_sale["occurred_at"] == observed_at
    assert catalog_product["id"] not in {row["id"] for row in later["source_records"]}
    assert catalog_supplier["id"] not in {row["id"] for row in later["source_records"]}


def test_clock_jump_preserves_native_sale_before_unlinked_cancellation(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )

    advanced = adapter.advance_to("2026-11-03T09:00:00+00:00")

    assert advanced["pending_native_shipments"] == 0
    assert [record["id"] for record in advanced["source_records"] if record["kind"] == "order"] == [
        "store_001:shipment:7"
    ]
    cancellation = advanced["native_outcomes"][0]
    assert cancellation["event_type"] == "shipment_cancelled"
    assert cancellation["source_id"] == "store_001:shipment:7"
    assert cancellation["mapping_status"] == "source_linked"
    assert cancellation["erp_reversal_status"] == "not_applied"
    cancellation_source = next(
        row for row in advanced["source_records"]
        if row["id"] == "outcome:store_001:shipment_cancelled:7"
    )
    assert cancellation_source["kind"] == "native_outcome"
    assert cancellation_source["native_record"]["source_shipment_id"] == "store_001:shipment:7"
    assert "amount" not in cancellation_source
    assert advanced["unmapped_native_outcomes"] == []
    assert (
        advanced["coverage"]["cancellation_shipment_linkage"]
        == "derived_from_native_pending_state_transition"
    )


def test_native_refund_outcome_links_back_through_shipment_batch_receipt(tmp_path):
    import pickle
    from datetime import date

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    adapter.advance_to("2026-10-30T09:00:00+00:00")
    shipped = adapter.apply(translate(_mapping(), _evidence())["action"])
    batch_id = shipped["outcome"]["evidence"]["batch_id"]

    with store.transaction() as connection:
        path = connection.execute(
            "SELECT checkpoint_path FROM ecommerce_native_world_state WHERE singleton=1",
        ).fetchone()[0]
        with open(path, "rb") as checkpoint:
            native = pickle.load(checkpoint)
        batch = next(row for row in native.escrow_batches if row["batch_id"] == batch_id)
        batch["amount"] = 10.0
        returns = [
            row for row in native.pending_returns if row.get("batch_id") == batch_id
        ]
        if not returns:
            native.pending_returns.append(
                {
                    "store_id": "store_001",
                    "product_id": "auto-product-1",
                    "quantity": 1,
                    "refund_per_unit": 25.0,
                    "arrival_date": date(2026, 10, 31),
                    "batch_id": batch_id,
                    "ship_cost_per_unit": 1.0,
                    "defect_frac": 0.0,
                }
            )
        else:
            returns[0]["arrival_date"] = date(2026, 10, 31)
        next_path = adapter._write_checkpoint(native)
        connection.execute(
            "UPDATE ecommerce_native_world_state SET checkpoint_path=? WHERE singleton=1",
            (str(next_path),),
        )

    advanced = adapter.advance_to("2026-11-01T09:00:00+00:00")
    refund = next(
        row
        for row in advanced["native_outcomes"]
        if row["event_type"] == "refund_processed"
    )
    assert refund["source_id"] == "store_001:shipment:7"
    assert refund["mapping_status"] == "source_linked"
    assert refund["native_record"]["batch_id"] == batch_id
    assert refund["native_record"]["refund_amount_native_units"] == "25.0"
    assert refund["native_record"]["escrow_reclaimed_native_units"] == "10.0"
    assert refund["native_record"]["commission_reversed_native_units"] == "15.0"
    assert refund["erp_reversal_status"] == "not_applied"
    refund_source = next(
        row for row in advanced["source_records"]
        if row["id"] == f"outcome:store_001:refund:{batch_id}"
    )
    assert refund_source["kind"] == "native_outcome"
    assert refund_source["source_ref"] == "store_001:shipment:7"
    assert refund_source["amount"] == "25.0"
    assert refund_source["native_record"]["shipment_id"] == 7
    assert refund_source["native_record"]["source_delivery_note_names"] == ["DN-1"]
    adapter.acknowledge_source_receipts([{
        "schema": "native_outcome_v1", "source_id": refund_source["id"],
        "status": "pending", "documents": [], "pending_reasons": ["invoice pending"],
    }])
    retry = adapter.advance_to("2026-11-02T09:00:00+00:00")
    assert refund_source["id"] in {row["id"] for row in retry["source_records"]}
    adapter.acknowledge_source_receipts([{
        "schema": "native_outcome_v1", "source_id": refund_source["id"],
        "status": "completed", "documents": [
            {"doctype": "Delivery Note", "name": "DN-RETURN-1", "docstatus": 1},
            {"doctype": "Sales Invoice", "name": "SI-RETURN-1", "docstatus": 1},
        ], "pending_reasons": [],
    }])
    after_ack = adapter.advance_to("2026-11-03T09:00:00+00:00")
    assert refund_source["id"] not in {row["id"] for row in after_ack["source_records"]}


def test_calendar_conditions_share_provider_effects_for_the_same_seed_and_range():
    rows = calendar_supply_conditions(
        17,
        "auto_hardware",
        "2026-12-19T00:00:00+00:00",
        "2026-12-21T09:00:00+00:00",
    )
    cold_wave = next(row for row in rows if row["event_name"] == "Cold Wave")
    assert cold_wave["occurred_at"] == "2026-12-20T00:00:00+00:00"
    assert cold_wave["upstream_calendar_duration_days"] == 5
    assert cold_wave["affected_categories"] == "all"
    assert cold_wave["delivery_delay_multiplier"] == 2.0


def test_checkpoint_commits_native_state_with_receipt_and_replays_once(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    checkpoint_dir = tmp_path / "native"
    adapter = EcommerceNativeActionCheckpoint(store, checkpoint_dir)
    environment = _environment()
    opening_balance = environment.bank_balance
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    action = translate(_mapping(), _evidence())["action"]

    first = adapter.apply(action)
    restarted = EcommerceNativeActionCheckpoint(
        SQLiteStore(tmp_path / "state.sqlite"), checkpoint_dir
    )
    replay = restarted.apply(action)
    reconciled = restarted.reconcile(action)

    assert first == replay == reconciled
    assert first["accepted"] is True and first["reconciled"] is True
    assert first["outcome"]["evidence"]["shipment_id"] == 7
    import pickle

    with store.transaction() as connection:
        active = connection.execute(
            "SELECT checkpoint_path FROM ecommerce_native_world_state WHERE singleton=1"
        ).fetchone()[0]
    with open(active, "rb") as file:
        restored = pickle.load(file)
    assert restored.bank_balance < opening_balance
    assert restored.get_pending_shipments()["count"] == 0
    with open(active, "rb") as file:
        active_bytes_before_evaluation = file.read()
    import hashlib
    expected_state_hash = hashlib.sha256(active_bytes_before_evaluation).hexdigest()
    unrelated_order_event = _evidence(
        evidence_id="sales-order-submit",
        erp_doctype="Sales Order",
        erp_name="SO-1",
        document={"items": []},
    )
    evaluation = restarted.evaluate([_mapping()], [unrelated_order_event, _evidence()])
    assert evaluation["native_actions_reconciled"] == 1
    assert len(evaluation["translations"]) == 1
    assert evaluation["upstream_commit"] == UPSTREAM_COMMIT
    assert evaluation["scope"] == "shared_company"
    assert evaluation["native_state_hash"] == expected_state_hash
    assert evaluation["native_company_time"] == "2026-10-30T00:00:00+00:00"
    with store.transaction() as connection:
        state_after = connection.execute(
            "SELECT checkpoint_path FROM ecommerce_native_world_state WHERE singleton=1",
        ).fetchone()[0]
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM ecommerce_native_action_receipts",
        ).fetchone()[0]
    assert state_after == active
    with open(active, "rb") as file:
        assert file.read() == active_bytes_before_evaluation
    assert receipt_count == 1

    conflicting = {**action, "arguments": {**action["arguments"], "speed": "fast"}}
    with pytest.raises(StoreConflict):
        restarted.apply(conflicting)


def test_captured_provider_evaluation_uses_immutable_checkpoint_after_live_clock_advances(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    store.background_initialization_request({
        "company_id": "company", "provider_id": "ecommercebench", "seed": 17,
        "started_at": "2026-10-30T00:00:00+00:00",
        "binding": {"store_type": "auto_hardware"}, "actor_private_states": {},
    })
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company", "company_name": "The Agent Company", "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00", "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    captured = adapter.capture_evaluation_snapshot()
    advanced = adapter.advance_to("2026-11-02T09:00:00+00:00")
    evaluated = adapter.evaluate_captured_snapshot(captured)

    assert evaluated["upstream_commit"] == UPSTREAM_COMMIT
    assert evaluated["scope"] == "shared_company"
    assert evaluated["native_state_ref"] == captured["native_state_ref"]
    assert evaluated["native_state_hash"] == captured["native_state_hash"]
    assert evaluated["identity_fingerprint"] == captured["identity_fingerprint"]
    assert evaluated["native_status"] == "incomplete"
    assert evaluated["money_unit"] == {"currency": "GBP", "native_units_per_currency_unit": "1"}
    assert evaluated["native_company_time"] == "2026-10-30T00:00:00+00:00"
    assert adapter.verify_evaluation_snapshot(captured)
    assert advanced["company_time"] == "2026-11-02T09:00:00+00:00"
    with store.transaction() as connection:
        state = connection.execute(
            "SELECT checkpoint_path,company_cursor FROM ecommerce_native_world_state WHERE singleton=1"
        ).fetchone()
    assert state["company_cursor"] == "2026-11-02T09:00:00+00:00"
    assert evaluated["report"]


def test_submitted_purchase_order_uses_upstream_supplier_order_processor_once(tmp_path):
    import pickle
    import json
    from servers.simulation.company_feedback import CompanyFeedback
    from servers.simulation.feedback_contracts import CompanyEvidence, RecordMapping

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    feedback = CompanyFeedback(store, {"ecommercebench": adapter})
    environment = _environment()
    environment.products["auto-product-2"] = {
        "product_id": "auto-product-2", "title": "Synthetic second fixture product",
        "store_type": "auto_hardware", "category": "Auto Parts", "brand": "Fixture",
        "size": "Small", "reference_price": 40.0,
    }
    store_id = next(iter(environment.stores))
    supplier_name = next(
        name for name, kind in environment.supplier_types.items() if kind == "good"
    )
    adapter.initialize(
        environment,
        store_id=store_id,
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    route = _mapping(
        mapping_id="mapped-source-item",
        source_record_id="auto-product-1",
        source_entity_type="product",
        erp_doctype="Item",
        erp_name="ECOM-auto-product-1",
        native_context={},
    )
    evidence = _evidence(
        evidence_id="po-submit-1",
        erp_doctype="Purchase Order",
        erp_name="PO-1",
        document={
            "doctype": "Purchase Order",
            "name": "PO-1",
            "docstatus": 1,
            "supplier": environment.supplier_info[supplier_name]["supplier_id"],
            "supplier_name": supplier_name,
            "address_display": "1 Source Road, London",
            "items": [
                {"item_code": "ECOM-auto-product-1", "qty": 3, "rate": "12.505"},
                {"item_code": "ECOM-auto-product-2", "qty": 2, "rate": "8.75"},
            ],
        },
    )
    mapping = RecordMapping.model_validate(route)
    product = environment.products["auto-product-1"]
    product_snapshot = {
        key: product.get(key) for key in (
            "product_id", "title", "category", "store_type", "brand", "size", "reference_price",
        )
    }
    feedback.map_record(mapping, {
        "doctype": "Item", "name": "ECOM-auto-product-1",
        "item_code": "ECOM-auto-product-1",
        "custom_source_system": "qwenlm-ecommercebench-native-economy",
        "custom_source_id": "auto-product-1",
        "custom_source_snapshot": json.dumps({
            "source": "qwenlm-ecommercebench-native-economy",
            "upstream_commit": UPSTREAM_COMMIT,
            "product_id": "auto-product-1",
            "product": product_snapshot,
        }),
    })
    captured = CompanyEvidence.model_validate(evidence)
    assert ("Item", "ECOM-auto-product-1") in feedback._references(captured)
    translated = feedback.observe(captured)
    assert translated[0]["translation"]["status"] == "supported"
    assert translated[0]["translation"]["action"]["native_action"] == "process_structured_order"
    assert translated[0]["translation"]["action"]["arguments"]["items"] == [
        {"sku_id": "auto-product-1", "quantity": 3, "unit_price": "12.505"},
        {"sku_id": "auto-product-2", "quantity": 2, "unit_price": "8.75"},
    ]
    before = environment.bank_balance
    feedback.reconcile()
    replay = adapter.reconcile(translated[0]["translation"]["action"])
    assert replay["accepted"] is True
    assert replay["outcome"]["evidence"]["purchase_order"] == "PO-1"
    assert [row["quantity_expected"] for row in replay["outcome"]["evidence"]["native_orders"]] == [3, 2]
    assert environment.bank_balance == before  # adapter persisted a checkpoint copy
    with store.transaction() as connection:
        checkpoint_path = connection.execute(
            "SELECT checkpoint_path FROM ecommerce_native_world_state WHERE singleton=1"
        ).fetchone()[0]
    with open(checkpoint_path, "rb") as checkpoint:
        restored = pickle.load(checkpoint)
    assert restored.bank_balance < before
    assert len(restored.pending_deliveries) == 2
    assert {row["product_id"] for row in restored.pending_deliveries} == {
        "auto-product-1", "auto-product-2",
    }


def test_native_shipping_cash_movement_is_one_replayable_bank_source_fact(tmp_path):
    from decimal import Decimal

    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company", "company_name": "The Agentic Company",
            "seed": 17, "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    action = dict(translate(_mapping(), _evidence())["action"])
    action["company_time"] = "2026-10-30T12:00:00+00:00"
    applied = adapter.apply(action)
    assert applied["accepted"] is True
    advanced = adapter.advance_to("2026-10-30T13:00:00+00:00")
    source_id = f"bank:store_001:{action['action_id']}"
    transactions = [row for row in advanced["source_records"] if row["id"] == source_id]
    assert len(transactions) == 1
    fact = transactions[0]["native_record"]
    assert transactions[0]["kind"] == "bank_transaction"
    assert fact["direction"] == "withdrawal"
    assert fact["movement_type"] == "shipment"
    assert fact["occurred_at"] == "2026-10-30T12:00:00+00:00"
    assert Decimal(fact["amount_native_units"]) == Decimal(
        str(applied["outcome"]["evidence"]["shipping_cost"])
    )
    adapter.acknowledge_source_receipts([
        {"source_id": source_id, "doctype": "Bank Transaction", "name": "ACC-BTN-2026-00001"},
    ])
    later = adapter.advance_to("2026-10-30T14:00:00+00:00")
    assert source_id not in {row["id"] for row in later["source_records"]}


def test_apply_advances_native_checkpoint_to_evidence_time_and_rejects_stale_time(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    adapter = EcommerceNativeActionCheckpoint(store, tmp_path / "native")
    environment = _environment()
    adapter.initialize(
        environment,
        store_id="store_001",
        identity={
            "company_id": "company",
            "company_name": "The Agentic Company",
            "seed": 17,
            "started_at": "2026-10-30T00:00:00+00:00",
            "store_type": "auto_hardware",
            "money_unit": {"currency": "GBP", "native_units_per_currency_unit": "1"},
            "upstream_commit": UPSTREAM_COMMIT,
        },
    )
    action = dict(translate(_mapping(), _evidence())["action"])
    action["company_time"] = "2026-10-30T12:00:47+00:00"

    receipt = adapter.apply(action)
    with store.transaction() as connection:
        cursor = connection.execute(
            "SELECT company_cursor FROM ecommerce_native_world_state WHERE singleton=1"
        ).fetchone()["company_cursor"]
    assert receipt["accepted"] is True
    assert receipt["outcome"]["company_time"] == "2026-10-30T12:00:47+00:00"
    assert receipt["outcome"]["native_action_time"] == "2026-10-30T12:00:00+00:00"
    assert cursor == "2026-10-30T12:00:47+00:00"

    stale = {**action, "action_id": "stale-evidence"}
    stale["company_time"] = "2026-10-30T11:59:59+00:00"
    stale_receipt = adapter.apply(stale)
    assert stale_receipt["accepted"] is False
    assert stale_receipt["reconciled"] is False
    assert stale_receipt["outcome"]["status"] == "inconclusive"
    assert stale_receipt["outcome"]["company_time"] == "2026-10-30T11:59:59+00:00"
