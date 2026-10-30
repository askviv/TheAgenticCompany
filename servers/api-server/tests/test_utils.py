import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


UTILS_PATH = pathlib.Path(__file__).resolve().parents[1] / "utils.py"


def load_utils_module():
    rocketchat_package = types.ModuleType("rocketchat_API")
    rocketchat_module = types.ModuleType("rocketchat_API.rocketchat")
    rocketchat_module.RocketChat = object
    redis_om = types.ModuleType("redis_om")
    redis_om.JsonModel = object
    redis_om_model = types.ModuleType("redis_om.model")
    redis_om_model_model = types.ModuleType("redis_om.model.model")
    redis_om_model_model.Field = lambda **kwargs: None

    modules = {
        "rocketchat_API": rocketchat_package,
        "rocketchat_API.rocketchat": rocketchat_module,
        "redis_om": redis_om,
        "redis_om.model": redis_om_model,
        "redis_om.model.model": redis_om_model_model,
    }
    module_spec = importlib.util.spec_from_file_location("controller_utils_under_test", UTILS_PATH)
    module = importlib.util.module_from_spec(module_spec)
    real_open = open

    def open_runtime_file(path, *args, **kwargs):
        if path == "/rocketchat/npc_definition.json":
            return io.StringIO("[]")
        return real_open(path, *args, **kwargs)

    with patch.dict(sys.modules, modules), patch("builtins.open", open_runtime_file):
        module_spec.loader.exec_module(module)
    return module


def load_api_server_module(utils_module):
    module_spec = importlib.util.spec_from_file_location(
        "api_server_under_test", UTILS_PATH.parent / "api-server.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    with patch.dict(sys.modules, {"utils": utils_module}):
        module_spec.loader.exec_module(module)
    return module


class ExecuteCommandTests(unittest.TestCase):
    def test_nonzero_exit_raises_instead_of_returning_success_like_text(self):
        utils = load_utils_module()

        with tempfile.TemporaryDirectory() as execution_dir:
            utils.EXECUTION_DIR = execution_dir
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                utils.execute_command("exit 7")

        self.assertEqual(raised.exception.returncode, 7)

    def test_failed_synchronous_reset_returns_server_error(self):
        utils = load_utils_module()
        api_server = load_api_server_module(utils)
        failure = subprocess.CalledProcessError(1, "make reset-gitlab")

        with patch.object(api_server, "execute_command", side_effect=failure):
            response = api_server.app.test_client().post("/api/reset-gitlab")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"make reset-gitlab", response.data)

    def test_async_failure_is_logged_once_with_exception_details(self):
        utils = load_utils_module()
        failure = RuntimeError("background reset failed")

        with patch.object(utils, "execute_command", side_effect=failure):
            with self.assertLogs(level="ERROR") as captured:
                thread = utils.async_execute_command("make reset-plane")
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(captured.records), 1)
        self.assertIn("RuntimeError: background reset failed", captured.output[0])


class GitLabHealthcheckTests(unittest.TestCase):
    def test_healthcheck_calls_authenticated_projects_api_and_rejects_unauthorized(self):
        class GitLabHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/v4/projects":
                    status = 404
                elif self.headers.get("PRIVATE-TOKEN") == "root-token":
                    status = 200
                else:
                    status = 401
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        utils = load_utils_module()
        api_server = load_api_server_module(utils)
        server = ThreadingHTTPServer(("127.0.0.1", 0), GitLabHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 2)
        self.addCleanup(server.shutdown)

        with patch.object(
            api_server,
            "GITLAB_HEALTH_URL",
            f"http://127.0.0.1:{server.server_port}/api/v4/projects",
        ):
            healthy = api_server.app.test_client().get("/api/healthcheck/gitlab")
            self.assertEqual(healthy.status_code, 200)

            with patch.object(api_server, "GITLAB_HEALTH_HEADERS", {}):
                unauthorized = api_server.app.test_client().get("/api/healthcheck/gitlab")
            self.assertEqual(unauthorized.status_code, 401)

    def test_healthcheck_uses_bounded_http_request(self):
        utils = load_utils_module()
        response = types.SimpleNamespace(status_code=200)
        with patch.object(utils.requests, "get", return_value=response) as get:
            status, _ = utils.check_url(
                "http://gitlab.example/api/v4/projects",
                headers={"PRIVATE-TOKEN": "root-token"},
            )

        self.assertEqual(status, 200)
        get.assert_called_once_with(
            "http://gitlab.example/api/v4/projects",
            headers={"PRIVATE-TOKEN": "root-token"},
            timeout=(5, 15),
        )



if __name__ == "__main__":
    unittest.main()
