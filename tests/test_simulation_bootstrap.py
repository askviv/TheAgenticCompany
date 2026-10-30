import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "deploy/enterprise-simulation/coordinator/bootstrap.py"
SPEC = importlib.util.spec_from_file_location("coordinator_bootstrap", MODULE_PATH)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
OPS_PATH = MODULE_PATH.with_name("ops.py")
OPS_SPEC = importlib.util.spec_from_file_location("coordinator_ops", OPS_PATH)
ops = importlib.util.module_from_spec(OPS_SPEC)
OPS_SPEC.loader.exec_module(ops)


class FakeMailu:
    def __init__(self):
        self.users = set()
        self.created = []

    def user_exists(self, address):
        return address in self.users

    def create_user(self, address, display_name, password):
        self.users.add(address)
        self.created.append((address, display_name, password))


def test_bootstrap_provisions_only_two_counterparty_accounts_and_preserves_credentials(tmp_path):
    client = FakeMailu()
    ca_file = tmp_path / "ca.pem"

    first_credentials = bootstrap.provision_counterparties(client, tmp_path, ca_file=ca_file)
    saved = json.loads((tmp_path / "enterprise-counterparty-credentials.json").read_text())
    second_credentials = bootstrap.provision_counterparties(client, tmp_path, ca_file=ca_file)

    expected = {
        "northwind-supplier@example.com",
        "cornerstone-customer@example.com",
    }
    assert client.users == expected
    assert {email for email, _name, _password in client.created} == expected
    assert set(first_credentials) == expected == set(second_credentials)
    assert first_credentials == second_credentials
    assert {credential["password"] for credential in first_credentials.values()} == {"theagentcompany"}
    assert all(saved["accounts"][key]["mail"]["status"] == "ready"
               for key in bootstrap.COUNTERPARTIES)
    assert (tmp_path / "enterprise-counterparty-credentials.json").stat().st_mode & 0o777 == 0o600


def test_bootstrap_refuses_to_adopt_existing_unmanaged_mailbox(tmp_path):
    client = FakeMailu()
    client.users.add("northwind-supplier@example.com")

    try:
        bootstrap.provision_counterparties(client, tmp_path, ca_file=None)
    except RuntimeError as error:
        assert "without managed credentials" in str(error)
    else:
        raise AssertionError("an existing unmanaged mailbox must not be adopted or reset")

    assert client.created == []


def test_bootstrap_preserves_principal_tokens_and_exports_only_supported_roles(tmp_path):
    path = tmp_path / "principals.json"
    original = {
        "controller-private-token": {"actor_id": "simulation-controller", "role": "controller"},
        "evaluator-private-token": {"actor_id": "private-evaluator", "role": "evaluator"},
        "agent-private-token": {"actor_id": "viv", "role": "agent"},
    }
    bootstrap.write_private_json(path, original)

    result = bootstrap.principals(path)

    assert result == original
    assert {entry["role"] for entry in result.values()} == {"controller", "evaluator", "agent"}


def test_employee_credentials_are_selected_by_exact_mail_address_without_rewriting_them(tmp_path):
    addresses = {
        "sarahjohnson@example.com", "alexturner@example.com",
        "chenxinyi@example.com", "liming@example.com",
    }
    original = {"schema_version": 1, "employees": {
        f"opaque-{index}": {"mail": {"address": address, "password": f"existing-{index}"}}
        for index, address in enumerate(sorted(addresses))
    }}
    path = tmp_path / "employee-credentials.json"
    bootstrap.write_private_json(path, original)

    credentials = bootstrap.existing_employee_credentials(tmp_path)

    assert set(credentials) == addresses
    assert {value["password"] for value in credentials.values()} == {
        "existing-0", "existing-1", "existing-2", "existing-3"
    }
    assert json.loads(path.read_text()) == original
    actors = {actor["actor_id"]: actor for actor in bootstrap.ACTORS}
    assert actors["warehouse"]["email"] == "alexturner@example.com"
    assert actors["logistics"]["email"] == "chenxinyi@example.com"
    assert actors["manager"]["email"] == "liming@example.com"
    assert actors["manager"]["kind"] == "approver"


def test_bootstrap_accepts_dedicated_rsync_source_without_git_metadata(tmp_path):
    source = tmp_path / "enterprise-source"
    runtime = source / "servers/simulation/runtime.py"
    requirements = source / "servers/simulation/requirements.txt"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("# runtime source\n")
    requirements.write_text("# pinned runtime dependencies\n")

    bootstrap.verify_source(source)


def test_operator_review_command_builds_run_scoped_request_with_idempotency_key(tmp_path, monkeypatch):
    principals_path = tmp_path / "principals.json"
    bootstrap.write_private_json(principals_path, {
        "agent-secret-in-file-only": {"actor_id": "viv", "role": "agent"},
    })
    args = ops.parser_for_cli().parse_args([
        "--base-url", "http://coordinator.example:8131",
        "--principals", str(principals_path),
        "review", "run-1", "PI-9", "--key", "review-request-1",
    ])

    calls = {}

    class FakeSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.update(method=method, url=url, kwargs=kwargs)
            return object()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(ops.requests, "Session", FakeSession)
    ops.request_for(args)

    assert calls["method"] == "POST"
    assert calls["url"] == "http://coordinator.example:8131/runs/run-1/invoice-review/PI-9"
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer agent-secret-in-file-only"
    assert calls["kwargs"]["headers"]["Idempotency-Key"] == "review-request-1"
    assert calls["kwargs"]["json"] is None
    assert calls["closed"] is True


def test_operator_controller_event_commands_map_to_privileged_http_routes(tmp_path, monkeypatch):
    principals_path = tmp_path / "principals.json"
    bootstrap.write_private_json(principals_path, {
        "controller-secret-in-file-only": {"actor_id": "simulation-controller", "role": "controller"},
    })
    run_id = "00000000-0000-4000-8000-000000000001"
    event_payload = {
        "event_id": "b1-day-2-shipment-delayed",
        "run_id": run_id,
        "scenario_id": "B1",
        "simulated_time": "2026-10-30T09:00:00+00:00",
        "event_type": "SHIPMENT_DELAYED",
        "source": "controller",
        "entity_type": "shipment",
        "entity_id": "SHIP-1",
        "related_entities": ["CASE-1"],
        "payload": {"new_eta_days": 5},
        "visibility": ["logistics"],
    }
    payload_path = tmp_path / "shipment-delay.json"
    payload_path.write_text(json.dumps(event_payload))
    calls = []

    class FakeSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, "kwargs": kwargs, "trust_env": self.trust_env})
            return object()

        def close(self):
            pass

    monkeypatch.setattr(ops.requests, "Session", FakeSession)
    common = ["--base-url", "http://coordinator.example:8131", "--principals", str(principals_path)]

    schedule_args = ops.parser_for_cli().parse_args([
        *common, "schedule-event", "--payload", str(payload_path), "--key", "b1-delay-1",
    ])
    ops.request_for(schedule_args)
    pending_args = ops.parser_for_cli().parse_args([*common, "pending-events", run_id])
    ops.request_for(pending_args)

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://coordinator.example:8131/events"
    assert calls[0]["kwargs"]["json"] == event_payload
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer controller-secret-in-file-only"
    assert calls[0]["kwargs"]["headers"]["Idempotency-Key"] == "b1-delay-1"
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == f"http://coordinator.example:8131/events?run_id={run_id}"
    assert calls[1]["kwargs"]["headers"]["Authorization"] == "Bearer controller-secret-in-file-only"
    assert "Idempotency-Key" not in calls[1]["kwargs"]["headers"]
    assert all(call["trust_env"] is False for call in calls)


def test_operator_shipping_action_command_targets_native_commit_operation(tmp_path, monkeypatch):
    principals_path = tmp_path / "principals.json"
    bootstrap.write_private_json(principals_path, {
        "agent-secret-in-file-only": {"actor_id": "viv", "role": "agent"},
    })
    payload_path = tmp_path / "shipping-action.json"
    payload = {
        "run_id": "run-1",
        "entity_id": "SHIP-001",
        "approval_id": "grant-shipping-1",
        "parameters": {"case_id": "S01-case", "quote_message_id": "supplier-quote-1"},
    }
    payload_path.write_text(json.dumps(payload))

    args = ops.parser_for_cli().parse_args([
        "--base-url", "http://coordinator.example:8131",
        "--principals", str(principals_path),
        "action", "run-1", "commit_expedited_shipping",
        "--payload", str(payload_path), "--key", "shipping-commit-1",
    ])
    calls = {}

    class FakeSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.update(method=method, url=url, kwargs=kwargs)
            return object()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(ops.requests, "Session", FakeSession)
    ops.request_for(args)

    assert calls["method"] == "POST"
    assert calls["url"] == "http://coordinator.example:8131/runs/run-1/erp/actions/commit_expedited_shipping"
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer agent-secret-in-file-only"
    assert calls["kwargs"]["headers"]["Idempotency-Key"] == "shipping-commit-1"
    assert calls["kwargs"]["json"] == payload
    assert calls["closed"] is True


def test_support_alias_is_exact_and_repeated_provisioning_does_not_duplicate_routes():
    class Client:
        def __init__(self):
            self.alias = None
            self.writes = 0
        def request(self, method, path, payload=None):
            if method == 'POST':
                self.writes += 1
                self.alias = payload
                return None
            if self.alias is None:
                raise bootstrap.MailuAPIError('GET alias', 404)
            return self.alias
    client = Client()
    destinations = ['sarahjohnson@example.com', 'agent@agent.example.test']
    bootstrap.ensure_recipient_alias(client, 'support@example.com', destinations,
                                     create_alias=lambda address, targets: client.request('POST', '/alias', {'email': address, 'destination': targets, 'wildcard': False}))
    bootstrap.ensure_recipient_alias(client, 'support@example.com', destinations,
                                     create_alias=lambda address, targets: client.request('POST', '/alias', {'email': address, 'destination': targets, 'wildcard': False}))
    assert client.writes == 1
    assert client.alias['wildcard'] is False
    import pytest
    with pytest.raises(RuntimeError):
        bootstrap.ensure_recipient_alias(client, 'support@example.com', ['other@example.com'], create_alias=lambda *_: None)
    assert client.writes == 1
