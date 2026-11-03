from fastapi.testclient import TestClient
from types import SimpleNamespace

from servers.simulation.app import create_app
from servers.simulation.runtime import employee_desktop_manager


class Desktops:
    def list_sessions(self):
        return [{"id": "employee-1", "name": "Employee", "role": "Finance", "status": "stopped"}]

    def list_viewers(self):
        return []

    def ensure_session(self, employee_id):
        if employee_id != "employee-1":
            raise KeyError(employee_id)
        return {"employee_id": employee_id, "viewing_url": "https://desktop.example.test/",
                "viewing_credentials": {"username": "viewer", "password": "test-only"},
                "cdp_endpoint": "ws://127.0.0.1/private"}

    def stop_session(self, employee_id):
        if employee_id != "employee-1":
            raise KeyError(employee_id)
        return {"employee_id": employee_id, "status": "stopped"}


class Engine:
    employee_desktops = Desktops()

    class Mail:
        def open_employee_inbox(self, employee_id, desktop):
            assert desktop['employee_id'] == employee_id

    mail = Mail()


def test_employee_desktop_routes_require_controller_and_hide_browser_control_endpoint():
    client = TestClient(create_app(Engine(), {
        "controller-token": {"actor_id": "operator", "role": "controller"},
        "agent-token": {"actor_id": "agent", "role": "agent"},
    }))
    assert client.get('/company/desktops').status_code == 401
    assert client.get('/company/desktops', headers={'Authorization': 'Bearer agent-token'}).status_code == 403
    headers = {'Authorization': 'Bearer controller-token'}
    assert client.get('/company/desktops', headers=headers).json()['employees'][0]['id'] == 'employee-1'
    response = client.post('/company/desktops/start', headers=headers, json={'employee_id': 'employee-1'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'employee_id': 'employee-1', 'status': 'running',
                               'viewing_url': 'https://desktop.example.test/',
                               'viewing_credentials': {'username': 'viewer', 'password': 'test-only'}}
    assert client.post('/company/desktops/start', headers=headers, json={'employee_id': 'absent'}).status_code == 404
    assert client.post('/company/desktops/stop', headers={'Authorization': 'Bearer agent-token'}, json={'employee_id': 'employee-1'}).status_code == 403
    assert client.post('/company/desktops/stop', headers=headers, json={'employee_id': 'employee-1'}).json() == {
        'employee_id': 'employee-1', 'status': 'stopped'}


def test_runtime_manager_uses_profile_state_and_discovers_caddy_host(tmp_path):
    config = SimpleNamespace(company_profile_file=tmp_path / 'company.json', state_root=tmp_path)
    manager = employee_desktop_manager(config)
    assert manager.caddy_proxy.hostname is None
    assert manager.company_profile_path == config.company_profile_file
    assert manager.state_root == tmp_path / 'employee-desktops'
