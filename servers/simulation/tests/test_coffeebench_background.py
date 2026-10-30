from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import threading
from types import SimpleNamespace

from servers.simulation.background_worker import BackgroundWorker
from servers.simulation import coffeebench_background as coffee
from servers.simulation.feedback_contracts import TranslatedAction
from servers.simulation.integration_contracts import BackgroundAdvanceRequest, BackgroundInitializeRequest


class _FakeCoffeeModel:
    def __init__(self, model_id: str):
        self.model = model_id
        self.calls = 0
        self.queries = []

    def query(self, messages, *_args, **_kwargs):
        from coffeebench.models.types import ModelResponse

        self.calls += 1
        self.queries.append(messages)
        return ModelResponse(
            content="bounded test response",
            thinking="",
            tool_calls=[],
            stop_reason="end_turn",
            cost=0,
            raw=None,
        )


def test_initialize_checkpoints_actual_pinned_coffee_environment(tmp_path):
    models = []

    def factory(_actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models.append(model)
        return model

    coffee.configure_model_factory(factory)
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    snapshot = coffee.initialize(
        7,
        start,
        {"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low", "checkpoint_directory": str(tmp_path)},
        {},
    )

    environment = coffee._restore(snapshot["checkpoint"], tmp_path)
    assert type(environment).__module__ == "coffeebench.environment"
    assert len(environment.agents) == 6
    assert snapshot["upstream_commit"] == coffee.UPSTREAM_COMMIT
    assert snapshot["company_business_ids"] == ["roaster_A"]
    assert all(model.calls == 0 for model in models)


def test_native_evaluation_uses_persisted_captured_checkpoint_without_actor_turns(tmp_path, monkeypatch):
    models = []

    def factory(_actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models.append(model)
        return model

    worker = BackgroundWorker(
        tmp_path / "worker.sqlite", checkpoint_directory=tmp_path / "private-checkpoints",
        coffee_model_factory=factory, configured_model_id="gpt-6-luna:low",
    )
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low", "max_days": 1}
    asyncio.run(worker.initialize(BackgroundInitializeRequest(
        provider_id="coffeebench", seed=41, started_at=start, binding=binding,
    )))
    identity = asyncio.run(worker.coffee_evaluation_source_identity())
    captured = asyncio.run(worker.capture_coffee_evaluation_snapshot())
    current = worker.store.get_background_provider_state("coffeebench")
    assert captured["identity_fingerprint"] == identity["identity_fingerprint"]
    assert captured["native_state_hash"] == current["snapshot"]["checkpoint"]["sha256"]

    # Advance only one minute before the native business day opens; no actor
    # turn or model query is possible. The evaluation still restores the exact
    # prior checkpoint referenced by the capture receipt.
    asyncio.run(worker.advance(BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="advance-after-capture",
        until=start + timedelta(minutes=1), binding=binding,
    )))
    after_advance = worker.store.get_background_provider_state("coffeebench")
    assert after_advance["snapshot"]["checkpoint"]["sha256"] != captured["native_state_hash"]

    observed = []
    native_evaluator = coffee._native_evaluation
    def observe_captured(environment, business_ids):
        observed.append((environment._company_clock_cursor, list(business_ids)))
        return native_evaluator(environment, business_ids)
    monkeypatch.setattr("servers.simulation.background_worker.coffee_native_evaluation", observe_captured)

    result = worker.evaluate_captured_coffee_snapshot(captured["capture_id"])

    assert result["native_state_hash"] == captured["native_state_hash"]
    assert result["native_company_time"] == captured["native_company_time"]
    assert result["native_evaluations"][0]["status"] == "incomplete"
    assert result["native_evaluations"][0]["score"] is None
    assert observed == [(0, ["roaster_A"])]
    assert all(model.calls == 0 for model in models)


def test_legacy_coffee_snapshot_migrates_native_delivery_baseline_without_model_calls(tmp_path):
    models = []

    def factory(_actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models.append(model)
        return model

    coffee.configure_model_factory(factory)
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path),
    }
    legacy = coffee.initialize(17, start, binding, {})
    legacy.pop("snapshot_schema_version")
    legacy.pop("native_delivery_lag_baseline")
    legacy.pop("external_supply_conditions")
    legacy.pop("native_message_ids")

    advanced = asyncio.run(coffee.advance(legacy, start + timedelta(minutes=1), binding))
    assert advanced["snapshot"]["snapshot_schema_version"] == 3
    assert advanced["snapshot"]["native_delivery_lag_baseline"] == {
        item_id: 1 for item_id in (
            "green_coffee_kg", "roasted_coffee_kg", "green_specialty_kg", "roasted_specialty_kg",
        )
    }
    assert advanced["snapshot"]["external_supply_conditions"] == []
    assert all(model.calls == 0 for model in models)


def test_native_counterparty_message_export_uses_actual_message_and_excludes_unmapped_messages():
    environment = SimpleNamespace(marketplace=SimpleNamespace(messages=[
        SimpleNamespace(
            id="msg-historical", sender_id="farmer_A", recipient_id="roaster_A",
            title="Old", body="Already checkpointed", sent_at=1,
            ref_listing_id=None, ref_offer_id=None, ref_deal_id=None,
        ),
        SimpleNamespace(
            id="msg-1", sender_id="farmer_A", recipient_id="roaster_A",
            title="Updated delivery", body="The shipment leaves Friday.", sent_at=120,
            ref_listing_id="lst-9", ref_offer_id="offer-3", ref_deal_id=None,
        ),
        SimpleNamespace(
            id="msg-company", sender_id="roaster_A", recipient_id="farmer_A",
            title="Company-authored", body="Do not export back to the company.", sent_at=121,
            ref_listing_id=None, ref_offer_id=None, ref_deal_id=None,
        ),
        SimpleNamespace(
            id="msg-other", sender_id="farmer_B", recipient_id="retailer_B",
            title="Unmapped", body="Not addressed to this company.", sent_at=122,
            ref_listing_id=None, ref_offer_id=None, ref_deal_id=None,
        ),
    ]))
    snapshot = {
        "started_at": "2026-10-30T00:00:00+00:00",
        "company_business_ids": ["roaster_A"],
        "native_message_ids": ["msg-historical"],
    }

    events = coffee._native_message_events(environment, snapshot)

    assert len(events) == 1
    assert events[0]["event_type"] == "COFFEE_NATIVE_MESSAGE_RECEIVED"
    assert events[0]["event_id"] == hashlib.sha256(
        f"{coffee.UPSTREAM_COMMIT}:marketplace_message:msg-1".encode()
    ).hexdigest()
    assert events[0]["simulated_time"] == "2026-10-30T02:00:00+00:00"
    assert events[0]["visibility"] == ["roaster_A"]
    assert events[0]["payload"] == {
        "native_message_id": "msg-1",
        "sender_id": "farmer_A",
        "recipient_id": "roaster_A",
        "title": "Updated delivery",
        "body": "The shipment leaves Friday.",
        "sent_at_virtual_minute": 120,
        "ref_listing_id": "lst-9",
        "ref_offer_id": "offer-3",
        "ref_deal_id": None,
        "native_source": "coffeebench/marketplace.py:Message",
    }
    assert snapshot["native_message_ids"] == [
        "msg-1", "msg-company", "msg-historical", "msg-other",
    ]


def test_global_ecommerce_logistics_condition_uses_native_coffee_delivery_path(tmp_path):
    from servers.simulation.ecommerce_supplier import _upstream_environment

    models = {}

    def factory(actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models[actor_id] = model
        return model

    coffee.configure_model_factory(factory)
    start = datetime(2026, 12, 20, tzinfo=timezone.utc)
    supplier_id = "source-supplier-for-contract-test"
    ecommerce_environment = _upstream_environment(17, start.date())
    ecommerce_environment._process_events_for_date(start.date())
    source_event = next(row for row in ecommerce_environment.active_events if row["event_name"] == "Cold Wave")
    supply = source_event["_supply_effects"]
    assert supply["affected_categories"] == "all"
    condition = {
        "event_id": hashlib.sha256(
            f"qwenlm-ecommercebench-calendar:17:{supplier_id}:supply:2026-12-20:Cold Wave".encode()
        ).hexdigest()[:32],
        "source": "qwenlm-ecommercebench-calendar",
        "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "occurred_at": start.isoformat(),
        "effective_until": (start + timedelta(days=int(source_event["duration_days"]))).isoformat(),
        "duration_days": int(source_event["duration_days"]),
        "event_name": source_event["event_name"],
        "store_type": "auto_hardware",
        "affected_categories": supply["affected_categories"],
        "delivery_delay_multiplier": supply["delivery_delay_multiplier"],
    }
    binding = {
        "company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path), "external_supply_conditions": [condition, condition],
    }
    snapshot = coffee.initialize(17, start, binding, {})

    active = asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=1), binding))
    environment = coffee._restore(
        active["snapshot"]["checkpoint"], tmp_path,
        active["snapshot"]["company_business_ids"],
    )
    assert environment.marketplace.items["green_coffee_kg"].delivery_lag_days == 2
    assert active["snapshot"]["external_supply_conditions"][0]["status"] == "active"
    conflicting_condition = {**condition, "delivery_delay_multiplier": "2.25"}
    try:
        asyncio.run(coffee.advance(
            active["snapshot"], start + timedelta(minutes=2),
            {**binding, "external_supply_conditions": [conflicting_condition]},
        ))
    except ValueError as error:
        assert "reused for different" in str(error)
    else:
        raise AssertionError("a source ID with conflicting condition facts must fail")

    # Exercise the pinned Marketplace API: it consumes the native Item lag
    # and writes a real CoffeeBench Deal ETA using the source condition.
    environment.time_manager.virtual_min = 600
    listing = environment.marketplace.post_listing(
        "farmer_A", "green_coffee_kg", 1, 5.0, 30,
    )
    offer, status = environment.marketplace.make_offer(
        listing.id, "roaster_A", 5.0, 1, 30,
    )
    assert status == "ok" and offer is not None
    deal, status = environment.marketplace.accept_offer(offer.id, "farmer_A")
    assert status == "ok" and deal is not None
    assert deal.delivery_at == 2 * 1440 + 540

    expired = asyncio.run(
        coffee.advance(active["snapshot"], start + timedelta(days=5), binding)
    )
    restored = coffee._restore(
        expired["snapshot"]["checkpoint"], tmp_path,
        expired["snapshot"]["company_business_ids"],
    )
    assert restored.marketplace.items["green_coffee_kg"].delivery_lag_days == 1
    assert expired["snapshot"]["external_supply_conditions"][0]["status"] == "expired"
    assert any(outcome.get("source_event_id") == condition["event_id"] for outcome in expired["outcomes"])
    assert restored.agents["roaster_A"].model.model == "passive"
    assert not models["roaster_A"].queries


def test_coffee_order_source_facts_link_only_conditions_active_at_native_acceptance():
    started_at = datetime(2026, 12, 20, tzinfo=timezone.utc)
    condition = {
        "event_id": "ecom-condition-1", "source": "qwenlm-ecommercebench-calendar",
        "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "event_name": "Cold Wave", "occurred_at": started_at.isoformat(),
        "effective_until": (started_at + timedelta(days=2)).isoformat(),
        "duration_days": 2, "store_type": "auto_hardware", "affected_categories": "all",
        "delivery_delay_multiplier": "2.0", "start_minute": 0, "end_minute": 2880,
        "status": "expired",
    }

    def deal(deal_id, accepted_at):
        return SimpleNamespace(
            id=deal_id, seller_id="roaster_A", buyer_id="retailer_A", listing_id="listing-1",
            offer_id="offer-" + deal_id, item_id="roasted_coffee_kg", qty=4,
            unit_price=11.25, total_price=45.0, payment_terms_days=30,
            deal_at=accepted_at, delivery_at=accepted_at + 900, invoice_id="invoice-" + deal_id,
        )

    snapshot = {
        "company_business_ids": ["roaster_A"], "started_at": started_at.isoformat(),
        "source_record_ids": [], "external_supply_conditions": [condition],
    }
    environment = SimpleNamespace(
        marketplace=SimpleNamespace(deals=[deal("active-deal", 600), deal("expired-deal", 2880)]),
        business_apps={},
    )

    records = coffee._source_records(environment, snapshot)
    by_deal = {record["native_record"]["deal_id"]: record for record in records}

    active = by_deal["active-deal"]
    assert active["occurred_at"] == (started_at + timedelta(minutes=600)).isoformat()
    assert active["native_record"]["accepted_at_virtual_minute"] == 600
    assert active["active_source_condition_ids"] == ["ecom-condition-1"]
    assert active["active_source_conditions"] == [{
        key: condition[key] for key in (
            "event_id", "source", "upstream_commit", "event_name", "occurred_at",
            "effective_until", "duration_days", "store_type", "affected_categories",
            "delivery_delay_multiplier",
        )
    }]
    assert by_deal["expired-deal"]["active_source_condition_ids"] == []
    assert by_deal["expired-deal"]["active_source_conditions"] == []
    result = BackgroundWorker._result(
        "coffeebench", coffee.UPSTREAM_COMMIT,
        datetime.fromisoformat(active["occurred_at"]), erp_intents=[active],
    ).model_dump(mode="json")
    assert result["erp_intents"][0]["active_source_condition_ids"] == ["ecom-condition-1"]
    assert result["erp_intents"][0]["active_source_conditions"][0]["event_id"] == "ecom-condition-1"


def test_coffee_pending_offer_source_preserves_exact_terms_without_marking_a_deal():
    started_at = datetime(2026, 12, 20, tzinfo=timezone.utc)
    listing = SimpleNamespace(
        id="listing-1", seller_id="roaster_A", item_id="roasted_coffee_kg",
        qty=9, asking_price=13.5, payment_terms_days=45, posted_at=100,
    )
    offer = SimpleNamespace(
        id="offer-1", listing_id="listing-1", buyer_id="retailer_A",
        seller_id="roaster_A", offered_price=12.25, qty=4,
        payment_terms_days=30, posted_at=225, message="Net 30 for four units",
        status="pending",
    )
    snapshot = {
        "company_business_ids": ["roaster_A"], "started_at": started_at.isoformat(),
        "source_record_ids": [], "external_supply_conditions": [],
    }
    environment = SimpleNamespace(
        marketplace=SimpleNamespace(listings=[listing], offers=[offer], deals=[]),
        business_apps={},
    )

    records = coffee._source_records(environment, snapshot)

    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "offer"
    assert record["native_record_type"] == "offer"
    assert record["native_record_id"] == "offer-1"
    assert record["party"] == "retailer_A"
    assert record["amount"] == "49.00"
    assert record["occurred_at"] == (started_at + timedelta(minutes=225)).isoformat()
    assert record["native_record"] == {
        "offer_id": "offer-1", "listing_id": "listing-1",
        "buyer_id": "retailer_A", "seller_id": "roaster_A",
        "item_id": "roasted_coffee_kg", "quantity": 4,
        "unit_price": "12.25", "total_price": "49.00",
        "payment_terms_days": 30, "offer_message": "Net 30 for four units",
        "offer_status": "pending", "offer_posted_at_virtual_minute": 225,
        "listing_quantity": 9, "listing_asking_price": "13.5",
        "listing_payment_terms_days": 45, "listing_posted_at_virtual_minute": 100,
        "upstream_source": "coffeebench/marketplace.py:Offer",
    }
    assert snapshot["source_record_ids"] == [record["id"]]

    # Replaying the same native offer is idempotent; an offer belonging to a
    # different seller is not imported into this mapped company.
    assert coffee._source_records(environment, snapshot) == []
    other_seller_offer = SimpleNamespace(**{**offer.__dict__, "id": "offer-2", "seller_id": "roaster_B"})
    other_snapshot = {**snapshot, "source_record_ids": []}
    other_environment = SimpleNamespace(
        marketplace=SimpleNamespace(listings=[listing], offers=[other_seller_offer], deals=[]),
        business_apps={},
    )
    assert coffee._source_records(other_environment, other_snapshot) == []


def test_coffee_rejects_scoped_or_forged_external_supply_condition(tmp_path):
    import asyncio

    coffee.configure_model_factory(lambda _actor_id, model_id: _FakeCoffeeModel(model_id))
    start = datetime(2026, 12, 20, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path),
    }
    snapshot = coffee.initialize(17, start, binding, {})
    condition = {
        "event_id": "source-event", "source": "qwenlm-ecommercebench-calendar",
        "upstream_commit": "b84678071c424fc94713b0a9e1ae86c519bd74c7",
        "occurred_at": start.isoformat(), "effective_until": (start + timedelta(days=2)).isoformat(),
        "duration_days": 2, "event_name": "Scoped",
        "store_type": "auto_hardware", "affected_categories": ["auto_hardware"],
        "delivery_delay_multiplier": 3.0,
    }
    try:
        asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=1), {
            **binding, "external_supply_conditions": [condition],
        }))
    except ValueError as error:
        assert "affected_categories" in str(error)
    else:
        raise AssertionError("Coffee must reject an unmapped category-scoped condition")

    forged = {**condition, "affected_categories": "all", "upstream_commit": "0" * 40}
    try:
        asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=1), {
            **binding, "external_supply_conditions": [forged],
        }))
    except ValueError as error:
        assert "pinned provider" in str(error)
    else:
        raise AssertionError("Coffee must reject an unpinned source condition")


def test_mapped_company_action_uses_native_tool_once_without_company_model_query(tmp_path):
    models = {}

    def factory(actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models[actor_id] = model
        return model

    coffee.configure_model_factory(factory)
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path),
    }
    snapshot = coffee.initialize(29, start, binding, {})
    action = {
        "action_id": "action-price-1", "mapping_id": "map-price-1", "evidence_id": "ev-price-1",
        "benchmark": "coffeebench", "native_action": "set_retail_price", "actor_id": "retailer_A",
        "company_time": (start + timedelta(minutes=600)).isoformat(),
        "arguments": {"item_id": "roasted_coffee_kg", "price_per_unit": 18.75},
    }

    first = asyncio.run(coffee.advance(
        snapshot, start + timedelta(minutes=601), {**binding, "company_actions": [action]},
    ))
    receipt = next(row for row in first["outcomes"] if row.get("action_id") == action["action_id"])
    assert receipt["status"] == "applied"
    assert receipt["native_action"] == "set_retail_price"
    assert receipt["native_result"]["retail_price_per_unit"] == 18.75
    environment = coffee._restore(
        first["snapshot"]["checkpoint"], tmp_path, first["snapshot"]["company_business_ids"],
    )
    assert environment.business_apps["retailer_A"].retail_prices["roasted_coffee_kg"] == 18.75
    assert environment.agents["retailer_A"].model.model == "passive"
    assert not models["retailer_A"].calls
    assert len([message for message in environment.agents["retailer_A"].messages if message.get("role") == "tool"]) == 1

    repeated = asyncio.run(coffee.advance(
        first["snapshot"], start + timedelta(minutes=602), {**binding, "company_actions": [action]},
    ))
    repeated_receipt = next(row for row in repeated["outcomes"] if row.get("action_id") == action["action_id"])
    assert repeated_receipt == receipt
    environment = coffee._restore(
        repeated["snapshot"]["checkpoint"], tmp_path, repeated["snapshot"]["company_business_ids"],
    )
    assert len([message for message in environment.agents["retailer_A"].messages if message.get("role") == "tool"]) == 1


def test_late_company_evidence_is_inconclusive_without_rewinding_native_state(tmp_path):
    coffee.configure_model_factory(lambda actor_id, model_id: _FakeCoffeeModel(model_id))
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path),
    }
    snapshot = coffee.initialize(31, start, binding, {})
    advanced = asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=601), binding))
    action = {
        "action_id": "late-price-1", "mapping_id": "map-price-1", "evidence_id": "ev-price-1",
        "benchmark": "coffeebench", "native_action": "set_retail_price", "actor_id": "retailer_A",
        "company_time": (start + timedelta(minutes=600)).isoformat(),
        "arguments": {"item_id": "roasted_coffee_kg", "price_per_unit": 18.75},
    }
    late = asyncio.run(coffee.advance(
        advanced["snapshot"], start + timedelta(minutes=602), {**binding, "company_actions": [action]},
    ))
    receipt = next(row for row in late["outcomes"] if row.get("action_id") == action["action_id"])
    assert receipt["status"] == "inconclusive"
    assert "predates" in receipt["reason"]
    environment = coffee._restore(
        late["snapshot"]["checkpoint"], tmp_path, late["snapshot"]["company_business_ids"],
    )
    assert environment.business_apps["retailer_A"].retail_prices["roasted_coffee_kg"] == 18.0


def test_email_reply_uses_native_coffee_message_and_reads_back_counterparty_wake(tmp_path):
    models = {}

    def factory(actor_id, model_id):
        model = _FakeCoffeeModel(model_id)
        models[actor_id] = model
        return model

    coffee.configure_model_factory(factory)
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna:low",
        "checkpoint_directory": str(tmp_path),
    }
    snapshot = coffee.initialize(43, start, binding, {})
    action = {
        "action_id": "company-email-thread-2", "mapping_id": "invoice-map-1",
        "evidence_id": "email-evidence-2", "benchmark": "coffeebench",
        "native_action": "send_message", "actor_id": "roaster_A",
        "company_time": (start + timedelta(minutes=600, seconds=30)).isoformat(),
        "arguments": {
            "recipient": "farmer_A", "title": "RE: Invoice INV-42",
            "body": "We will release the invoice after matching the delivered quantity.",
        },
    }

    advanced = asyncio.run(coffee.advance(
        snapshot, start + timedelta(minutes=601), {**binding, "company_actions": [action]},
    ))
    receipt = next(row for row in advanced["outcomes"] if row.get("action_id") == action["action_id"])

    assert receipt["status"] == "applied"
    assert receipt["native_result"]["conversation_receipt"]["message_verified"] is True
    assert receipt["native_result"]["conversation_receipt"]["counterparty_wake_scheduled"] is True
    assert receipt["native_result"]["conversation_receipt"]["sender_id"] == "roaster_A"
    assert receipt["native_result"]["conversation_receipt"]["recipient_id"] == "farmer_A"
    assert advanced["erp_intents"] == []
    environment = coffee._restore(
        advanced["snapshot"]["checkpoint"], tmp_path, advanced["snapshot"]["company_business_ids"],
    )
    native_message = next(
        message for message in environment.marketplace.messages
        if message.id == receipt["native_result"]["message_id"]
    )
    assert (native_message.sender_id, native_message.recipient_id, native_message.title, native_message.body) == (
        "roaster_A", "farmer_A", "RE: Invoice INV-42",
        "We will release the invoice after matching the delivered quantity.",
    )
    assert environment.agents["roaster_A"].model.model == "passive"
    assert not models["roaster_A"].calls


def test_background_worker_commits_native_action_snapshot_and_replay_receipt(tmp_path):
    def factory(_actor_id, model_id):
        return _FakeCoffeeModel(model_id)

    worker = BackgroundWorker(
        tmp_path / "worker.sqlite", checkpoint_directory=tmp_path / "private-checkpoints",
        coffee_model_factory=factory, configured_model_id="gpt-6-luna:low",
    )
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    asyncio.run(worker.initialize(BackgroundInitializeRequest(
        provider_id="coffeebench", seed=41, started_at=start,
        binding={"company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low", "max_days": 1},
    )))
    action = TranslatedAction(
        action_id="worker-action-1", mapping_id="mapping-1", evidence_id="evidence-1",
        benchmark="coffeebench", native_action="set_retail_price", actor_id="retailer_A",
        company_time=start + timedelta(minutes=600, seconds=30),
        arguments={"item_id": "roasted_coffee_kg", "price_per_unit": 20.0},
    )

    # Simulate a worker crash after claiming the operation but before its
    # atomic native checkpoint/receipt commit.
    worker.store.claim_background_operation(
        "coffeebench", f"company-action:{action.action_id}",
        {"company_id": "company", "action": action.model_dump(mode="json")},
    )

    receipt = asyncio.run(worker.apply_company_action(action))
    replay = asyncio.run(worker.apply_company_action(action))
    before_tick = worker.store.get_background_provider_state("coffeebench")

    assert receipt.accepted and not receipt.reconciled
    assert receipt.outcome["status"] == "queued"
    assert receipt.outcome["company_time"].endswith("10:00:30+00:00")
    assert replay == receipt
    assert before_tick["cursor"] == start.isoformat()
    assert action.action_id not in before_tick["snapshot"].get("company_action_receipts", {})

    # Only the authoritative shared-clock advance executes the queued native
    # event; ordinary feedback delivery does not advance the Coffee economy.
    asyncio.run(worker.advance(BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="shared-tick-after-feedback",
        until=start + timedelta(minutes=601),
        binding={"company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low", "max_days": 1},
    )))
    reconciled = worker.reconcile_company_action(action)
    stored = worker.store.get_background_provider_state("coffeebench")

    assert reconciled is not None and reconciled.accepted and reconciled.reconciled
    assert reconciled.outcome["native_applied_at_minute"] == 600
    assert stored["snapshot"]["company_action_receipts"][action.action_id]["outcome"]["status"] == "applied"
    evaluation = worker.evaluate_coffee_businesses(["retailer_A"])[0]
    checkpoint_state = worker.store.get_background_provider_state("coffeebench")
    assert evaluation["status"] == "incomplete"
    assert evaluation["native_state_hash"] == checkpoint_state["snapshot"]["checkpoint"]["sha256"]
    assert evaluation["native_clock"] == checkpoint_state["snapshot"]["cursor"]
    assert evaluation["evaluation_scope"] == "company_global"
    assert evaluation["attribution"] == "company_global_not_experiment"

    result = asyncio.run(worker.advance(BackgroundAdvanceRequest(
        provider_id="coffeebench", operation_id="complete-native-horizon",
        until=start + timedelta(days=2),
        binding={"company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low", "max_days": 1},
    )))
    native_evaluation = next(
        row for row in result.outcomes
        if row.get("evaluation_type") == "company_adapted_native_projection"
    )
    assert result.status == "finished"
    assert native_evaluation["status"] == "final"
    assert native_evaluation["score_metric"] == "audit.annual.true_net_income"
    assert native_evaluation["score"] == native_evaluation["audit"]["annual"]["true_net_income"]


def _native_invoice_payment_fixture(tmp_path, *, late_fee):
    model_id = "gpt-6-luna:low"
    coffee.configure_model_factory(lambda _actor_id, configured: _FakeCoffeeModel(configured))
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {
        "company_business_ids": ["roaster_A"], "model_id": model_id,
        "checkpoint_directory": str(tmp_path),
    }
    snapshot = coffee.initialize(67, start, binding, {})
    environment = coffee._restore(snapshot["checkpoint"], tmp_path, snapshot["company_business_ids"])
    from coffeebench.typings import Invoice

    invoice = Invoice(
        id="native-ap-proof-1", issuer="farmer_A", payer="roaster_A",
        amount=100.0, issue_date=0, due_date=30,
    )
    invoice.late_fees_accrued = late_fee
    environment.business_apps["roaster_A"].accounts_payable.append(invoice)
    environment.business_apps["farmer_A"].accounts_receivable.append(invoice)
    snapshot["checkpoint"] = coffee._checkpoint(environment, tmp_path)
    action = {
        "action_id": "payment-proof-action-1", "mapping_id": "payment-map-1",
        "evidence_id": "erp-payment-evidence-1", "benchmark": "coffeebench",
        "native_action": "pay_invoice", "actor_id": "roaster_A",
        "company_time": (start + timedelta(minutes=600)).isoformat(),
        "arguments": {"invoice_id": invoice.id},
        "supporting_evidence": [{
            "invoice_id": invoice.id, "payment_entry": "PE-ERP-1", "amount": "100.00",
            "currency": "USD", "docstatus": 1, "evidence_id": "erp-payment-evidence-1",
        }],
    }
    return start, binding, snapshot, action


def test_payment_evidence_balance_mismatch_does_not_pay_native_invoice(tmp_path):
    start, binding, snapshot, action = _native_invoice_payment_fixture(tmp_path, late_fee=10.0)
    baseline = asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=601), binding))
    baseline_environment = coffee._restore(
        baseline["snapshot"]["checkpoint"], tmp_path, baseline["snapshot"]["company_business_ids"],
    )

    result = asyncio.run(coffee.advance(
        snapshot, start + timedelta(minutes=601), {**binding, "company_actions": [action]},
    ))
    outcome = next(row for row in result["outcomes"] if row.get("action_id") == action["action_id"])
    after = coffee._restore(result["snapshot"]["checkpoint"], tmp_path,
                            result["snapshot"]["company_business_ids"])
    native_invoice = after.business_apps["roaster_A"].accounts_payable[0]

    assert outcome["status"] == "inconclusive"
    assert "balance differs" in outcome["native_result"]["message"]
    assert outcome["supporting_evidence"] == action["supporting_evidence"]
    assert native_invoice.net_outstanding == 110.0
    assert not native_invoice.paid
    assert after.business_apps["roaster_A"].cash == baseline_environment.business_apps["roaster_A"].cash
    assert after.business_apps["farmer_A"].cash == baseline_environment.business_apps["farmer_A"].cash


def test_exact_native_invoice_payment_preserves_receipt_and_applies_once(tmp_path):
    start, binding, snapshot, action = _native_invoice_payment_fixture(tmp_path, late_fee=0.0)
    baseline = asyncio.run(coffee.advance(snapshot, start + timedelta(minutes=601), binding))
    baseline_environment = coffee._restore(
        baseline["snapshot"]["checkpoint"], tmp_path, baseline["snapshot"]["company_business_ids"],
    )

    first = asyncio.run(coffee.advance(
        snapshot, start + timedelta(minutes=601), {**binding, "company_actions": [action]},
    ))
    outcome = next(row for row in first["outcomes"] if row.get("action_id") == action["action_id"])
    after = coffee._restore(first["snapshot"]["checkpoint"], tmp_path,
                            first["snapshot"]["company_business_ids"])
    native_invoice = after.business_apps["roaster_A"].accounts_payable[0]

    assert outcome["status"] == "applied"
    assert outcome["native_result"]["paid_amount"] == 100.0
    assert outcome["native_result"]["payment_readback"]["verified"] is True
    assert outcome["supporting_evidence"] == action["supporting_evidence"]
    assert native_invoice.paid and native_invoice.paid_date is not None
    assert after.business_apps["roaster_A"].cash == baseline_environment.business_apps["roaster_A"].cash - 100.0
    assert after.business_apps["farmer_A"].cash == baseline_environment.business_apps["farmer_A"].cash + 100.0

    replay = asyncio.run(coffee.advance(
        first["snapshot"], start + timedelta(minutes=602), {**binding, "company_actions": [action]},
    ))
    repeated = next(row for row in replay["outcomes"] if row.get("action_id") == action["action_id"])
    replayed_environment = coffee._restore(replay["snapshot"]["checkpoint"], tmp_path,
                                           replay["snapshot"]["company_business_ids"])
    assert repeated == outcome
    assert replayed_environment.business_apps["roaster_A"].cash == baseline_environment.business_apps["roaster_A"].cash - 100.0
    assert replayed_environment.business_apps["farmer_A"].cash == baseline_environment.business_apps["farmer_A"].cash + 100.0


def test_concurrent_worker_ticks_serialize_checkpoint_and_native_turns(tmp_path):
    calls = []

    class TrackingModel(_FakeCoffeeModel):
        def query(self, *args, **kwargs):
            calls.append(self.model)
            return super().query(*args, **kwargs)

    def factory(_actor_id, model_id):
        return TrackingModel(model_id)

    database = tmp_path / "serialized.sqlite"
    checkpoint_directory = tmp_path / "serialized-checkpoints"
    worker_a = BackgroundWorker(database, checkpoint_directory=checkpoint_directory,
                                coffee_model_factory=factory, configured_model_id="gpt-6-luna:low")
    worker_b = BackgroundWorker(database, checkpoint_directory=checkpoint_directory,
                                coffee_model_factory=factory, configured_model_id="gpt-6-luna:low")
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {"company_business_ids": ["retailer_A"], "model_id": "gpt-6-luna:low"}
    asyncio.run(worker_a.initialize(BackgroundInitializeRequest(
        provider_id="coffeebench", seed=53, started_at=start, binding=binding,
    )))
    target = start + timedelta(minutes=600)
    requests = [
        BackgroundAdvanceRequest(provider_id="coffeebench", operation_id=f"parallel-tick-{index}",
                                 until=target, binding=binding)
        for index in (1, 2)
    ]
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def run(worker, request):
        try:
            barrier.wait(timeout=5)
            results.append(asyncio.run(worker.advance(request)))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(worker_a, requests[0])),
        threading.Thread(target=run, args=(worker_b, requests[1])),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2 and all(result.cursor == target for result in results)
    assert worker_a.store.get_background_provider_state("coffeebench")["cursor"].startswith(target.isoformat())
    # The first tick dispatches the five native actor turns; the second sees
    # its committed cursor and does not replay those turns from a stale snapshot.
    assert len(calls) == 5
