import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from servers.simulation.background_runtime import BackgroundWorkerError, CompanyBackgroundRuntime
from servers.simulation.ecommerce_background import UPSTREAM_COMMIT as ECOMMERCE_UPSTREAM_COMMIT
from servers.simulation.background_worker import BackgroundWorker
from servers.simulation.businesses import initialize_supplier_business
from servers.simulation.ecommerce_background import events_between, initialize as initialize_ecommerce
from servers.simulation.integration_contracts import BackgroundAdvanceRequest, BackgroundInitializeRequest
from servers.simulation.store import SQLiteStore


def test_worker_keeps_native_finance_provenance():
    source = {"id": "native-order-1", "source": "sakanaai-coffeebench-native-economy",
              "kind": "order", "source_ref": "native-order-1", "currency": "USD", "amount": "120.00",
              "occurred_at": "2026-10-30T00:00:00Z", "party": "supplier_1",
              "native_record": {"order_id": "native-order-1"}}
    result = BackgroundWorker._result(
        "coffeebench", "9e2f3950b5812c8753de81ede9c68babc024b5c8",
        datetime(2026, 10, 30, tzinfo=timezone.utc), erp_intents=[source],
    )
    assert result.erp_intents[0]["source"] == "sakanaai-coffeebench-native-economy"
    assert result.erp_intents[0]["native_record"] == {"order_id": "native-order-1"}


def test_ecommerce_supply_event_carries_exact_upstream_global_condition(tmp_path):
    start = datetime(2026, 1, 14, tzinfo=timezone.utc)
    finish = datetime(2026, 1, 15, tzinfo=timezone.utc)
    binding = {"company_id": "company", "supplier_actor_id": "supplier", "supplier_id": "SUP-001"}
    state = initialize_supplier_business(
        {"market_config": {"store_type": "auto_hardware"}}, seed=17, supplier_id="SUP-001",
    )
    snapshot = initialize_ecommerce(17, start, binding, {"supplier": state})

    disruptions = [event for event in events_between(snapshot, start, finish, binding)
                   if event["event_type"] == "SUPPLIER_SUPPLY_DISRUPTED"]

    storm = next(event for event in disruptions if event["payload"]["upstream_event_name"] == "Winter Storm")
    assert storm["event_id"]
    assert storm["source"] == "qwenlm-ecommercebench-calendar"
    assert storm["upstream_commit"] == "b84678071c424fc94713b0a9e1ae86c519bd74c7"
    assert storm["payload"]["affected_categories"] == "all"
    assert storm["payload"]["delivery_delay_multiplier"] == 2.5
    assert storm["payload"]["upstream_calendar_duration_days"] == 4

    scoped_start = datetime(2026, 11, 19, tzinfo=timezone.utc)
    scoped_finish = datetime(2026, 11, 20, tzinfo=timezone.utc)
    scoped_snapshot = initialize_ecommerce(17, scoped_start, binding, {"supplier": state})
    shortage = next(event for event in events_between(scoped_snapshot, scoped_start, scoped_finish, binding)
                    if event["payload"]["upstream_event_name"] == "Raw Material Shortage")
    assert shortage["payload"]["affected_categories"] == ["appliance_digital", "home_living", "auto_hardware"]
    assert shortage["payload"]["cost_increase"] == 0.15
    assert CompanyBackgroundRuntime(SQLiteStore(tmp_path / "category-scope.sqlite"), object())._coffee_supply_conditions(
        "company", scoped_finish, [shortage],
    ) == []


class _FakeModel:
    def __init__(self, model_id: str, calls: list[str], actor_id: str | None = None):
        self.model = model_id
        self.calls = calls
        self.actor_id = actor_id

    def query(self, *_args, **_kwargs):
        from coffeebench.models.types import ModelResponse

        self.calls.append(self.actor_id or self.model)
        return ModelResponse(
            content="bounded no-op",
            thinking="",
            tool_calls=[],
            stop_reason="end_turn",
            cost=0,
            raw=None,
        )

    def get_usage_stats(self):
        return {"model": self.model, "n_calls": 0, "cost": 0}


def test_provider_initialization_reuses_original_day_after_restart(tmp_path):
    class Worker:
        def __init__(self):
            self.requests = []

        def initialize(self, request):
            self.requests.append(request.model_dump(mode="json"))
            return {"provider_id": request.provider_id, "cursor": request.started_at.isoformat()}

    path = tmp_path / "company.sqlite"
    worker = Worker()
    day = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    kwargs = {"seed": 17, "bindings": {"coffeebench": {}, "ecommercebench": {}},
              "actor_private_states": {}}
    CompanyBackgroundRuntime(SQLiteStore(path), worker).initialize_company(started_at=day, **kwargs)
    CompanyBackgroundRuntime(SQLiteStore(path), worker).initialize_company(started_at=day + timedelta(days=1), **kwargs)
    assert worker.requests[:2] == worker.requests[2:]
    assert all(row["started_at"].startswith("2026-10-30T00:00:00") for row in worker.requests)


def test_background_worker_persists_native_snapshot_and_reuses_completed_operation(tmp_path):
    calls: list[str] = []
    def factory(actor_id, configured):
        return _FakeModel(configured, calls, actor_id)

    model_id = "gpt-6-luna:low"
    worker = BackgroundWorker(
        tmp_path / "worker.sqlite",
        checkpoint_directory=tmp_path / "checkpoints",
        coffee_model_factory=factory,
        configured_model_id=model_id,
    )
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    binding = {"company_business_ids": ["roaster_A"], "model_id": model_id}
    initialize_request = BackgroundInitializeRequest(
        provider_id="coffeebench", seed=31, started_at=start,
        binding=binding, actor_private_states={},
    )
    # A prior process stopped after claiming the deterministic local setup.
    worker.store.claim_background_operation("coffeebench", "initialize", initialize_request.model_dump(mode="json"))
    initialized = asyncio.run(worker.initialize(initialize_request))
    assert initialized.provider_id == "coffeebench"
    assert initialized.cursor == start
    assert calls == []

    request = BackgroundAdvanceRequest(
        provider_id="coffeebench",
        operation_id="company:coffeebench:tick-1",
        until=start + timedelta(minutes=600),
        binding=binding,
    )
    first = asyncio.run(worker.advance(request))
    after_first = len(calls)
    replay = asyncio.run(worker.advance(request))

    assert first == replay
    assert first.cursor == request.until
    assert first.status == "active"
    assert first.next_due == datetime(2026, 10, 30, 19, tzinfo=timezone.utc)
    assert after_first == 5
    assert "roaster_A" not in calls
    assert len(calls) == after_first
    assert worker.store.get_background_provider_state("coffeebench")["cursor"] == request.until.isoformat()
    assert first.events == []
    assert first.erp_intents == []


def test_company_background_runtime_keeps_uncertain_provider_target_across_restart(tmp_path):
    class Worker:
        def __init__(self):
            self.calls = []
            self.fail_coffee_once = True

        def advance(self, request):
            self.calls.append((request.provider_id, request.operation_id, request.until.isoformat()))
            if request.provider_id == "coffeebench" and self.fail_coffee_once:
                self.fail_coffee_once = False
                raise BackgroundWorkerError(202, "background_operation_uncertain")
            return {
                "provider_id": request.provider_id,
                "upstream_commit": (ECOMMERCE_UPSTREAM_COMMIT if request.provider_id == "ecommercebench"
                                    else "9e2f3950b5812c8753de81ede9c68babc024b5c8"),
                "cursor": request.until.isoformat(), "events": [], "erp_intents": [], "next_due": None,
            }

    worker = Worker()
    database_path = tmp_path / "runtime.sqlite"
    runtime = CompanyBackgroundRuntime(SQLiteStore(database_path), worker)
    first_until = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    next_until = first_until + timedelta(minutes=1)
    bindings = {"coffeebench": {"model_id": "gpt-6-luna:low"}, "ecommercebench": {"supplier_id": "supplier-1"}}
    tick_id = first_until.isoformat() + ":coffeebench,ecommercebench"

    partial = runtime.advance_company(first_until, bindings=bindings)
    assert partial["status"] == "partial"
    assert partial["provider_errors"]["coffeebench"]["status_code"] == 202
    assert partial["provider_errors"]["coffeebench"]["pending_until"] == first_until.isoformat()
    assert worker.calls == [
        ("ecommercebench", f"company:ecommercebench:{tick_id}", first_until.isoformat()),
        ("coffeebench", f"company:coffeebench:{tick_id}", first_until.isoformat()),
    ]

    # A new runtime instance models coordinator restart. It retries Coffee's
    # exact pending operation before advancing that provider to the new target;
    # the healthy E-Commerce provider continues independently.
    restarted = CompanyBackgroundRuntime(SQLiteStore(database_path), worker)
    carried = restarted.advance_company(next_until, bindings=bindings)
    assert carried["status"] == "partial"
    assert carried["providers"]["coffeebench"]["provider_id"] == "coffeebench"
    assert set(carried["provider_operation_ids"]) == {"coffeebench", "ecommercebench"}
    assert worker.calls[2:] == [("coffeebench", f"company:coffeebench:{tick_id}", first_until.isoformat())]
    for provider, operation_id in carried["provider_operation_ids"].items():
        restarted.store.acknowledge_background_provider_result(provider, operation_id)

    completed = restarted.advance_company(next_until, bindings=bindings)
    assert completed["status"] == "complete"
    assert worker.calls[3:] == [
        ("ecommercebench", f"company:ecommercebench:{next_until.isoformat()}:coffeebench,ecommercebench", next_until.isoformat()),
        ("coffeebench", f"company:coffeebench:{next_until.isoformat()}:coffeebench,ecommercebench", next_until.isoformat()),
    ]
    assert restarted.store.get_background_provider_pending("coffeebench") is not None

    # The unresolved first tick remains visible as incomplete; no partial tick
    # result was mislabeled as a successful, durable completion.
    first_request = {
        "company_id": "company", "until": first_until.isoformat(),
        "provider_bindings": bindings, "provider_ids": ["coffeebench", "ecommercebench"],
    }
    assert restarted.store.claim_background_tick(tick_id, first_request)["status"] == "started"


@pytest.mark.parametrize("coupled_pending", [False, True])
def test_company_background_tick_can_advance_only_selected_providers(tmp_path, coupled_pending):
    class Worker:
        def __init__(self):
            self.calls = []

        def advance(self, request):
            self.calls.append(request.provider_id)
            return {
                "provider_id": request.provider_id,
                "upstream_commit": (ECOMMERCE_UPSTREAM_COMMIT if request.provider_id == "ecommercebench"
                                    else "9e2f3950b5812c8753de81ede9c68babc024b5c8"),
                "cursor": request.until.isoformat(), "events": [], "erp_intents": [], "next_due": None,
            }

    worker = Worker()
    store = SQLiteStore(tmp_path / "selected.sqlite")
    runtime = CompanyBackgroundRuntime(store, worker)
    until = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    policy = None
    if coupled_pending:
        policy = {"affected_categories": ["all"], "target_scope": "native_coffee_market",
                  "native_effect": "delivery_lag_days"}
        for provider in ("coffeebench", "ecommercebench"):
            request = BackgroundAdvanceRequest(provider_id=provider, operation_id=provider + "-pending",
                                               until=until, binding={})
            store.persist_background_provider_pending(provider, request.operation_id, request.model_dump(mode="json"))
    result = runtime.advance_company(
        until, bindings={"coffeebench": {}, "ecommercebench": {}},
        provider_ids=("coffeebench",), coupling_policy=policy,
    )

    assert worker.calls == ["coffeebench"]
    assert list(result["providers"]) == ["coffeebench"]


def test_ecommerce_global_logistics_condition_reaches_coffee_durably(tmp_path):
    class Worker:
        def __init__(self):
            self.calls = []
            self.fail_coffee_once = True

        def advance(self, request):
            self.calls.append(request)
            if request.provider_id == "ecommercebench":
                return {"provider_id": "ecommercebench", "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
                        "cursor": request.until.isoformat(),
                        "next_due": (request.until + timedelta(days=1)).replace(
                            hour=0, minute=0, second=0, microsecond=0,
                        ).isoformat(), "events": [{
                    "schema_version": 1, "company_id": "company", "event_id": "storm-event-1",
                    "event_type": "SUPPLIER_SUPPLY_DISRUPTED",
                    "simulated_time": "2026-01-15T00:00:00+00:00",
                    "source": "qwenlm-ecommercebench-calendar", "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT,
                    "entity_type": "supplier", "entity_id": "SUP-001",
                    "payload": {
                        "upstream_event_name": "Winter Storm",
                        "upstream_calendar_start_date": "01-15",
                        "upstream_calendar_duration_days": 4,
                        "affected_categories": "all", "delivery_delay_multiplier": 2.5,
                        "store_type": "auto_hardware",
                    }, "visibility": ["supplier"],
                }]}
            if self.fail_coffee_once:
                self.fail_coffee_once = False
                raise BackgroundWorkerError(202, "background_operation_uncertain")
            return {"provider_id": "coffeebench", "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
                    "cursor": request.until.isoformat(), "next_due": None, "events": [], "erp_intents": []}

    worker = Worker()
    database = tmp_path / "condition-handoff.sqlite"
    runtime = CompanyBackgroundRuntime(SQLiteStore(database), worker)
    until = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    policy = {"affected_categories": ["all"], "target_scope": "native_coffee_market",
              "native_effect": "delivery_lag_days"}
    bindings = {"coffeebench": {"company_business_ids": ["roaster_A"], "model_id": "gpt-6-luna"},
                "ecommercebench": {"supplier_id": "SUP-001"}}

    partial = runtime.advance_company(until, bindings=bindings, coupling_policy=policy)
    assert partial["status"] == "partial"
    assert [request.provider_id for request in worker.calls] == ["ecommercebench", "coffeebench"]
    first_coffee_request = worker.calls[1]
    assert first_coffee_request.binding["external_supply_conditions"] == [{
        "event_id": "storm-event-1", "source": "qwenlm-ecommercebench-calendar",
        "upstream_commit": ECOMMERCE_UPSTREAM_COMMIT, "occurred_at": "2026-01-15T00:00:00Z",
        "effective_until": "2026-01-19T00:00:00Z", "duration_days": 4,
        "event_name": "Winter Storm", "store_type": "auto_hardware",
        "affected_categories": "all", "delivery_delay_multiplier": "2.5",
    }]
    assert first_coffee_request.binding["external_supply_condition_policy"] == policy

    # An uncertain Coffee response replays the exact condition-bearing request;
    # the confirmed E-Commerce operation is not called twice or lost.
    replay = CompanyBackgroundRuntime(SQLiteStore(database), worker).advance_company(
        until, bindings=bindings, coupling_policy=policy,
    )
    assert replay["status"] == "complete"
    assert [request.provider_id for request in worker.calls] == [
        "ecommercebench", "coffeebench", "coffeebench",
    ]
    assert worker.calls[2].binding == first_coffee_request.binding
    assert worker.calls[2].operation_id == first_coffee_request.operation_id

    # Once the E-Commerce native daily event is due, a Coffee-only tick cannot
    # claim stale source coverage; the company scheduler must advance E-Commerce first.
    store = SQLiteStore(database)
    store.record_company_source_events(replay["providers"]["ecommercebench"]["events"])
    for provider, operation_id in replay["provider_operation_ids"].items():
        store.acknowledge_background_provider_result(provider, operation_id)
    next_day = runtime.advance_company(
        until + timedelta(days=1), bindings=bindings, provider_ids=("coffeebench",),
        coupling_policy=policy,
    )
    assert next_day["status"] == "partial"
    assert next_day["provider_errors"]["coffeebench"]["code"] == "source_provider_not_current"
    assert [request.provider_id for request in worker.calls] == [
        "ecommercebench", "coffeebench", "coffeebench",
    ]


def test_confirmed_provider_result_survives_coordinator_crash_until_ack(tmp_path):
    class Worker:
        def __init__(self):
            self.calls = 0

        def advance(self, request):
            self.calls += 1
            return {
                "provider_id": request.provider_id,
                "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
                "cursor": request.until.isoformat(),
                "events": [{
                    "schema_version": 1, "company_id": "company", "event_id": "native-event-1",
                    "event_type": "SUPPLIER_MARKET_DEMAND", "simulated_time": request.until.isoformat(),
                    "source": "qwenlm-ecommercebench-calendar", "upstream_commit": "a" * 40,
                    "entity_type": "supplier", "entity_id": "SUP-001", "payload": {"safe": True},
                    "visibility": ["supplier"],
                }],
                "erp_intents": [{"id": "native-finance-1", "amount": "42.00"}],
                "next_due": None,
            }

    database = tmp_path / "durable-result.sqlite"
    worker = Worker()
    until = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    bindings = {"coffeebench": {}}
    first = CompanyBackgroundRuntime(SQLiteStore(database), worker).advance_company(
        until, bindings=bindings, provider_ids=("coffeebench",),
    )
    operation_id = first["provider_operation_ids"]["coffeebench"]
    queued = SQLiteStore(database).get_background_provider_pending("coffeebench")
    assert queued["result"]["events"][0]["event_id"] == "native-event-1"
    assert queued["result"]["erp_intents"][0]["id"] == "native-finance-1"

    # Simulates a coordinator restart after the provider confirmed but before
    # source facts/intents were consumed. Replay serves the stored response.
    replay = CompanyBackgroundRuntime(SQLiteStore(database), worker).advance_company(
        until, bindings=bindings, provider_ids=("coffeebench",),
    )
    assert replay["providers"]["coffeebench"] == first["providers"]["coffeebench"]
    assert replay["provider_operation_ids"]["coffeebench"] == operation_id
    assert worker.calls == 1

    store = SQLiteStore(database)
    store.record_company_source_events(replay["providers"]["coffeebench"]["events"])
    store.record_company_finance_source_intents(replay["providers"]["coffeebench"]["erp_intents"])
    store.acknowledge_background_provider_result("coffeebench", operation_id)
    assert store.get_background_provider_pending("coffeebench") is None
    assert [row["event_id"] for row in store.list_company_source_events()] == ["native-event-1"]
    assert [row["id"] for row in store.list_unreceipted_company_finance_source_intents()] == ["native-finance-1"]


def test_company_background_initialization_reports_partial_provider_failure(tmp_path):
    class Worker:
        def initialize(self, request):
            if request.provider_id == "coffeebench":
                raise BackgroundWorkerError(202, "background_operation_uncertain")
            return {"provider_id": request.provider_id}

    runtime = CompanyBackgroundRuntime(SQLiteStore(tmp_path / "runtime.sqlite"), Worker())
    result = runtime.initialize_company(
        seed=17,
        started_at=datetime(2026, 10, 30, 15, tzinfo=timezone.utc),
        bindings={"coffeebench": {}, "ecommercebench": {}},
        actor_private_states={},
    )

    assert result["status"] == "partial"
    assert result["errors"]["coffeebench"] == {
        "status_code": 202, "code": "background_operation_uncertain",
    }
    assert result["providers"]["ecommercebench"]["provider_id"] == "ecommercebench"
