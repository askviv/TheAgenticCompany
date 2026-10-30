import asyncio
import json
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4
from fastapi.testclient import TestClient

from servers.simulation.models import StartScenario
from servers.simulation.runtime import MailWorkers, _erp_service_credentials, build_app, environment_manifest
from servers.simulation.store import SQLiteStore


def test_mail_worker_pumps_only_runs_returned_by_public_active_run_query(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    active_ids = [uuid4(), uuid4()]
    finished_id = uuid4()
    for run_id in (*active_ids, finished_id):
        store.create_run(run_id, StartScenario(scenario_id="C1", agent_id="agent-a"))
    store.update_run(finished_id, 0, status="completed")

    pumped = []
    first_pump = Event()

    class Engine:
        def __init__(self):
            self.store = store
            self.mail = None

        def pump(self, run_id):
            pumped.append(str(run_id))
            first_pump.set()

    workers = MailWorkers(Engine(), actors=[])
    workers.start()
    assert first_pump.wait(timeout=2)
    workers.stop()

    assert store.list_active_runs() == [
        {"run_id": str(run_id), "generation": 0} for run_id in sorted(active_ids, key=str)
    ]
    assert set(pumped) == {str(run_id) for run_id in active_ids}
    assert str(finished_id) not in pumped


def test_mail_workers_subscribe_to_smtp4dev_native_mailbox_notifications():
    class Hub:
        def __init__(self, url):
            self.url = url
            self.handlers = {}
            self.opened = None
            self.closed = None
            self.stopped = False

        def on(self, method, callback):
            self.handlers[method] = callback

        def on_open(self, callback):
            self.opened = callback

        def on_close(self, callback):
            self.closed = callback

        def on_error(self, _callback):
            pass

        def start(self):
            self.opened()

        def stop(self):
            self.stopped = True
            self.closed()

    class Engine:
        def __init__(self):
            self.mail = SimpleNamespace(config=SimpleNamespace(
                smtp4dev_api_base_url="http://127.0.0.1:19157"
            ))
            self.store = None

    hub = Hub("not-yet-created")
    workers = MailWorkers(Engine(), actors=[], hub_connection_factory=lambda url: setattr(hub, "url", url) or hub)
    watcher = Thread(target=workers._watch_viv)
    watcher.start()
    assert workers._hub_connected.wait(timeout=2)
    assert hub.url == "http://127.0.0.1:19157/hubs/notifications"

    version = workers.viv_activity_version()
    workers.changed.clear()
    # signalrcore 1.0.2 dispatches HubMessage.arguments (a list) to handlers.
    hub.handlers["messageschanged"](["Other"])
    assert not workers.changed.is_set()
    hub.handlers["messageschanged"](["default"])
    assert workers.changed.is_set()
    assert workers.wait_for_viv_activity(version, timeout=0.1)

    workers.stop()
    watcher.join(timeout=2)
    assert hub.stopped
    assert not watcher.is_alive()


def test_health_reports_uncertain_delivery_without_exposing_outbox_identity(tmp_path):
    store = SQLiteStore(tmp_path / "health.sqlite")
    run_id = uuid4()
    store.create_run(run_id, StartScenario(scenario_id="C1", agent_id="agent-a"))
    store.enqueue_outbox(run_id, 0, "private-outbox-identifier", {"body": "sensitive message"})
    store.mark_outbox_submission_started(run_id, 0, "private-outbox-identifier")
    store.mark_outbox_failure(run_id, 0, "private-outbox-identifier", error_type="TimeoutError")

    class Engine:
        def __init__(self):
            self.mail = None
            self.store = store

    components = MailWorkers(Engine(), actors=[]).health_components()

    assert components == {"mail_delivery": "uncertain"}
    assert "private-outbox-identifier" not in str(components)
    assert "sensitive message" not in str(components)


def test_completed_submission_is_not_reported_as_uncertain(tmp_path):
    store = SQLiteStore(tmp_path / "completed-health.sqlite")
    run_id = uuid4()
    store.create_run(run_id, StartScenario(scenario_id="C1", agent_id="agent-a"))
    store.enqueue_outbox(run_id, 0, "completed-outbox", {"body": "sensitive message"})
    store.mark_outbox_submission_started(run_id, 0, "completed-outbox")
    store.complete_outbox(run_id, 0, "completed-outbox", {"provider_id": "provider-message"})

    class Engine:
        def __init__(self):
            self.mail = None
            self.store = store

    assert MailWorkers(Engine(), actors=[]).health_components() == {}


def test_run_listing_failure_is_visible_to_health_and_does_not_kill_worker():
    failed = Event()

    class Store:
        def list_active_runs(self):
            raise OSError("sensitive sqlite path")

    class Engine:
        store = Store()
        mail = None

    workers = MailWorkers(Engine(), actors=[])
    original_failure = workers._failure

    def capture_failure(component, error):
        original_failure(component, error)
        failed.set()

    workers._failure = capture_failure
    workers.start()
    assert failed.wait(timeout=2)
    workers.stop()
    assert workers.health_components() == {"run_processor": "failed"}
    assert workers.errors["run:processor"] == "OSError"


def test_signalr_error_logs_only_safe_type_and_context(caplog):
    workers = MailWorkers(SimpleNamespace(), actors=[])
    workers._viv_error(RuntimeError("private provider response"))

    assert workers.errors == {"smtp4dev_notifications": "SignalRTransportError"}
    assert "RuntimeError" in caplog.text
    assert "private provider response" not in caplog.text


def test_environment_manifest_records_source_hashes_revision_and_packages():
    manifest = environment_manifest()

    assert len(manifest["source_tree_sha256"]) == 64
    assert "servers/simulation/runtime.py" in manifest["source_sha256"]
    assert "servers/simulation/requirements.txt" in manifest["source_sha256"]
    assert "deploy/enterprise-simulation/erpnext/admin/seed_a1.py" in manifest["source_sha256"]
    assert "deploy/enterprise-simulation/erpnext/compose.yml" in manifest["source_sha256"]
    assert manifest["source_dirty"] is None or isinstance(manifest["source_dirty"], bool)
    assert manifest["source_revision"] is None or len(manifest["source_revision"]) == 40
    assert manifest["packages"]["signalrcore"] == "1.0.2"


def test_missing_background_worker_config_keeps_company_api_available(tmp_path):
    site_name = "erp-sim-" + "c" * 32 + ".localhost"
    config_path = tmp_path / "runtime.json"
    mail_credentials = tmp_path / "mail-credentials.json"
    principals = tmp_path / "principals.json"
    mail_credentials.write_text(json.dumps({"agent@agent.example.test": {"username": "runtime", "password": "private"}}))
    principals.write_text(json.dumps({"controller-secret": {"actor_id": "controller", "role": "controller"}}))
    for path in (mail_credentials, principals):
        path.chmod(0o600)
    config_path.write_text(json.dumps({
        "state_root": str(tmp_path / "state"), "erp_compose": str(tmp_path / "compose.yml"),
        "erp_credentials_root": str(tmp_path / "erp-secrets"),
        "company_binding": {
            "site_name": site_name, "credentials_ref": "/private/company.json",
            "documents": {name: name for name in (
                "company", "supplier", "item", "warehouse", "purchase_order", "purchase_receipt",
                "purchase_invoice", "customer", "customer_order",
            )},
        },
        "mail": {"actors": [{"actor_id": "requester", "kind": "employee", "email": "requester@example.test"}],
                 "smtp4dev_api_base_url": "http://127.0.0.1:19158"},
        "mail_credentials_file": str(mail_credentials), "principals_file": str(principals),
        "workers_enabled": False,
    }))
    config_path.chmod(0o600)

    app, _config = build_app(config_path)
    with TestClient(app) as client:
        company = client.get("/company", headers={"Authorization": "Bearer controller-secret"})
        health = client.get("/health")

    assert company.status_code == 200
    assert company.json()["status"] == "running"
    assert health.json()["components"]["company_background"]["code"] == "background_worker_not_configured"


def test_erp_credentials_require_shipping_writer_key_and_secret(tmp_path):
    credentials_path = tmp_path / "site-credentials.json"
    credentials_path.write_text(json.dumps({
        "site_name": "erp-sim-" + "a" * 32 + ".localhost",
        "api_key": "coordinator-key",
        "api_secret": "coordinator-secret",
        "receipt_api_key": "receipt-key",
        "receipt_api_secret": "receipt-secret",
    }))
    credentials_path.chmod(0o600)

    import pytest
    with pytest.raises(RuntimeError, match="shipping-writer credentials") as failure:
        _erp_service_credentials(credentials_path)
    import traceback
    diagnostic = "".join(traceback.format_exception(failure.value))
    assert "coordinator-secret" not in diagnostic
    assert "receipt-secret" not in diagnostic
    assert failure.value.__cause__ is not None


def test_runtime_passes_shipping_credentials_and_closes_all_erp_sessions(tmp_path, monkeypatch):
    from servers.simulation import runtime

    site_name = "erp-sim-" + "b" * 32 + ".localhost"
    credentials_path = tmp_path / "secrets" / (site_name + ".json")
    credentials_path.parent.mkdir()
    credentials = {
        "site_name": site_name,
        "api_key": "coordinator-key",
        "api_secret": "coordinator-secret",
        "receipt_api_key": "receipt-key",
        "receipt_api_secret": "receipt-secret",
        "shipping_api_key": "shipping-key",
        "shipping_api_secret": "shipping-secret",
    }
    credentials_path.write_text(json.dumps(credentials))
    credentials_path.chmod(0o600)

    config_path = tmp_path / "runtime.json"
    mail_credentials_path = tmp_path / "mail-credentials.json"
    principals_path = tmp_path / "principals.json"
    mail_credentials_path.write_text(json.dumps({
        "agent@agent.example.test": {"username": "runtime", "password": "private"},
    }))
    principals_path.write_text(json.dumps({"controller-secret": {"actor_id": "controller", "role": "controller"}}))
    for path in (mail_credentials_path, principals_path):
        path.chmod(0o600)
    config_path.write_text(json.dumps({
        "state_root": str(tmp_path / "state"),
        "erp_compose": str(tmp_path / "compose.yml"),
        "erp_credentials_root": str(credentials_path.parent),
        "mail": {
            "actors": [{"actor_id": "viv", "kind": "agent", "email": "agent@agent.example.test"}],
            "smtp4dev_api_base_url": "http://127.0.0.1:19158",
        },
        "mail_credentials_file": str(mail_credentials_path),
        "principals_file": str(principals_path),
        "workers_enabled": False,
    }))
    config_path.chmod(0o600)

    class Session:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.session = Session()
            self.receipt_session = Session()
            self.shipping_session = Session()

    class Engine:
        def __init__(self, _store, mail, erp_factory, _site_admin, _actors):
            self.mail = mail
            self.store = None
            self.erp_factory = erp_factory

    monkeypatch.setattr(runtime, "ERPNextAdapter", Adapter)
    monkeypatch.setattr(runtime, "SimulationEngine", Engine)
    monkeypatch.setattr(runtime, "build_evaluation_service", lambda *_args: object())
    app, _config = build_app(config_path)
    adapter = app.state.engine.erp_factory({
        "site_name": site_name,
        "credentials_ref": str(credentials_path),
    })

    assert adapter.kwargs["shipping_api_key"] == "shipping-key"
    assert adapter.kwargs["shipping_api_secret"] == "shipping-secret"

    async def close_lifespan():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(close_lifespan())
    assert adapter.session.closed
    assert adapter.receipt_session.closed
    assert adapter.shipping_session.closed


def test_runtime_fingerprint_changes_when_upstream_data_or_actor_lock_changes(tmp_path, monkeypatch):
    from servers.simulation import runtime

    runtime_file = tmp_path / "servers/simulation/runtime.py"
    transport = tmp_path / "servers/company/scenarios.py"
    catalog = tmp_path / "third_party/ecommercebench/upstream/data/products.csv"
    lock = tmp_path / "servers/simulation/tau2_actor_runtime/uv.lock"
    for path, content in ((runtime_file, "# coordinator\n"), (transport, "# mail\n"),
                          (catalog, "sku,price\ncomponent,50\n"), (lock, "version = 1\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(runtime, "__file__", str(runtime_file))
    before = runtime.environment_manifest()["source_sha256"]
    catalog.write_text("sku,price\ncomponent,55\n")
    lock.write_text("version = 2\n")
    after = runtime.environment_manifest()["source_sha256"]
    assert before[str(catalog.relative_to(tmp_path))] != after[str(catalog.relative_to(tmp_path))]
    assert before[str(lock.relative_to(tmp_path))] != after[str(lock.relative_to(tmp_path))]
    assert before[str(runtime_file.relative_to(tmp_path))] == after[str(runtime_file.relative_to(tmp_path))]
