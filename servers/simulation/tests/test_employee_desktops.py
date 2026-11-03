"""Synthetic Docker contract tests for the Selkies employee desktop runtime."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import URLError

from servers.simulation.employee_desktops import (
    EMPLOYEE_LABEL,
    MANAGED_LABEL,
    EmployeeDesktopError,
    EmployeeDesktopImageUnavailable,
    EmployeeDesktopRuntime,
)
from servers.simulation.employee_desktop_proxy import CaddyEmployeeDesktopProxy


class _ProxyFixture:
    def __init__(self):
        self.routes: dict[str, str] = {}

    @staticmethod
    def subfolder_for(employee_id: str) -> str:
        return CaddyEmployeeDesktopProxy.subfolder_for(employee_id)

    @staticmethod
    def host_mappings() -> list[tuple[str, str]]:
        return [("desktop.example.test", "192.0.2.10")]

    def url_for(self, employee_id: str, container_ip: str, credentials: dict[str, str]) -> str:
        self.routes[employee_id] = container_ip
        return f"https://desktop.example.test{self.subfolder_for(employee_id)}"

    def remove(self, employee_id: str) -> None:
        self.routes.pop(employee_id, None)


class _DockerFixtureRuntime(EmployeeDesktopRuntime):
    def __init__(self, profile: Path, state: Path):
        self.proxy = _ProxyFixture()
        super().__init__(profile, state, caddy_proxy=self.proxy)
        self.commands: list[tuple[str, ...]] = []
        self.containers: dict[str, dict] = {}
        self.volumes: set[str] = set()
        self.networks: dict[str, dict] = {}
        self.env_contents: list[str] = []
        self.launches = 0
        self.relays = 0
        self.ports_open = {9222: True, 9223: True}
        self.display_probe_count = 0
        self.display_ready_after = 1
        self.xvfb_supervision = "up (pid 123) 3 seconds"

    def _docker(self, *args: str, check: bool = True):
        self.commands.append(args)
        if args[:2] == ("image", "inspect"):
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[:2] == ("volume", "inspect"):
            volume = args[2]
            if volume not in self.volumes:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            employee_id = next(
                key for key in self._profile_ids()
                if self._volume_name(key) == volume
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Labels": {
                MANAGED_LABEL: "true", EMPLOYEE_LABEL: self._employee_key(employee_id),
            }}]), stderr="")
        if args[:2] == ("volume", "create"):
            self.volumes.add(args[-1])
            return SimpleNamespace(returncode=0, stdout=args[-1], stderr="")
        if args[:2] == ("network", "inspect"):
            detail = self.networks.get(args[2])
            if detail is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            return SimpleNamespace(returncode=0, stdout=json.dumps([detail]), stderr="")
        if args[:2] == ("network", "create"):
            network = args[-1]
            employee_id = next(key for key in self._profile_ids()
                                if self._network_name(key) == network)
            self.networks[network] = {
                "Driver": "bridge", "Internal": False,
                "Labels": {MANAGED_LABEL: "true", EMPLOYEE_LABEL: self._employee_key(employee_id)},
                "Containers": {},
            }
            return SimpleNamespace(returncode=0, stdout=network, stderr="")
        if args[0] == "ps":
            rows = []
            for name, record in self.containers.items():
                labels = record["Config"]["Labels"]
                state = "running" if record["State"]["Running"] else "exited"
                rows.append(f"{name}|{state}|{labels[EMPLOYEE_LABEL]}")
            return SimpleNamespace(returncode=0, stdout="\n".join(rows), stderr="")
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            employee_id = next(key for key in self._profile_ids()
                                if self._container_name(key) == name)
            volume = args[args.index("--volume") + 1].split(":", 1)[0]
            network = args[args.index("--network") + 1]
            env_file = Path(args[args.index("--env-file") + 1])
            self.env_contents.append(env_file.read_text())
            self.containers[name] = self._record(employee_id, volume, running=False)
            self.networks[network]["Containers"]["synthetic-container-id"] = {"Name": name}
            return SimpleNamespace(returncode=0, stdout=name, stderr="")
        if args[0] == "inspect":
            record = self.containers.get(args[1])
            if record is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            return SimpleNamespace(returncode=0, stdout=json.dumps([record]), stderr="")
        if args[0] == "info":
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if args[0] == "start":
            self.containers[args[1]]["State"]["Running"] = True
            return SimpleNamespace(returncode=0, stdout=args[1], stderr="")
        if args[0] == "stop":
            self.containers[args[1]]["State"]["Running"] = False
            return SimpleNamespace(returncode=0, stdout=args[1], stderr="")
        if args[0] == "exec":
            if "google-chrome" in args:
                self.launches += 1
            if "socat" in args:
                self.relays += 1
            if "nc" in args:
                port = int(args[-1])
                return SimpleNamespace(returncode=0 if self.ports_open[port] else 1,
                                       stdout="", stderr="")
            if "xdpyinfo" in args:
                self.display_probe_count += 1
                ready = self.display_probe_count >= self.display_ready_after
                return SimpleNamespace(returncode=0 if ready else 1, stdout="", stderr="")
            if any("s6-svstat" in argument for argument in args):
                return SimpleNamespace(returncode=0, stdout=self.xvfb_supervision, stderr="")
            if "pgrep" in args:
                return SimpleNamespace(returncode=0, stdout="123", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected synthetic Docker command: {args}")

    def _record(self, employee_id: str, volume: str, *, running: bool) -> dict:
        return {
            "Config": {"Image": self.image, "Labels": {
                MANAGED_LABEL: "true", EMPLOYEE_LABEL: self._employee_key(employee_id),
            }},
            "State": {"Running": running},
            "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/home/ubuntu"}],
            "NetworkSettings": {"Ports": {
                "9223/tcp": [{"HostIp": "127.0.0.1", "HostPort": "34122"}],
            }, "Networks": {self._network_name(employee_id): {"IPAddress": "172.30.0.2"}}},
        }

    def _cdp_document(self, port: int) -> dict:
        return {"webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/test-id"}

    def _profile_ids(self):
        return [employee["id"] for employee in json.loads(self.company_profile_path.read_text())["employees"]]


class EmployeeDesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.profile = root / "company-identities.json"
        self.profile.write_text(json.dumps({"employees": [
            {"id": "sarah-johnson", "displayName": "Sarah Johnson", "department": "Technical"},
            {"id": "li-ming", "displayName": "Li Ming", "department": "Finance"},
        ]}), encoding="utf-8")
        self.state = root / "state"
        self.runtime = _DockerFixtureRuntime(self.profile, self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def test_ensure_session_creates_isolated_persistent_desktop_and_private_cdp_mapping(self):
        result = self.runtime.ensure_session("sarah-johnson")

        self.assertEqual(result["status"], "running")
        subfolder = self.runtime.proxy.subfolder_for("sarah-johnson")
        self.assertEqual(result["viewing_url"], f"https://desktop.example.test{subfolder}")
        self.assertEqual(result["cdp_endpoint"], "ws://127.0.0.1:34122/devtools/browser/test-id")
        self.assertEqual(result["viewing_credentials"]["username"][:9], "employee-")
        self.assertGreaterEqual(len(result["viewing_credentials"]["password"]), 32)
        self.assertNotIn(result["viewing_credentials"]["password"], result["viewing_url"])

        create = next(command for command in self.runtime.commands if command[0] == "create")
        self.assertFalse(any(argument.endswith("8080/tcp") for argument in create))
        self.assertIn("127.0.0.1::9223/tcp", create)
        self.assertIn("--network", create)
        add_host_index = create.index("--add-host")
        self.assertEqual(create[add_host_index + 1], "desktop.example.test:192.0.2.10")
        self.assertNotIn(result["viewing_credentials"]["password"], create)
        self.assertIn("SELKIES_ENABLE_BASIC_AUTH=true", self.runtime.env_contents[0])
        self.assertIn("SELKIES_SUBFOLDER=" + subfolder, self.runtime.env_contents[0])
        self.assertIn("SELKIES_ENABLE_HTTPS=false", self.runtime.env_contents[0])
        self.assertIn(result["viewing_credentials"]["password"], self.runtime.env_contents[0])
        credential_file = self.runtime._credentials_path("sarah-johnson")
        self.assertEqual(credential_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.state.glob(".selkies-env-*")))

    def test_retry_reuses_container_profile_and_generated_authentication(self):
        first = self.runtime.ensure_session("sarah-johnson")
        second = self.runtime.ensure_session("sarah-johnson")

        self.assertEqual(first["viewing_credentials"], second["viewing_credentials"])
        self.assertEqual(sum(command[0] == "create" for command in self.runtime.commands), 1)
        self.assertEqual(sum(command[:2] == ("volume", "create") for command in self.runtime.commands), 1)
        self.assertEqual(self.runtime.launches, 0)

    def test_missing_browser_starts_headed_chrome_with_persistent_profile_and_cdp(self):
        self.runtime.ports_open = {9222: False, 9223: False}
        result = self.runtime.ensure_session("sarah-johnson")

        command = next(command for command in self.runtime.commands if "google-chrome" in command)
        self.assertIn("google-chrome", command)
        self.assertIn("DISPLAY=:20", command)
        self.assertIn("--remote-debugging-port=9222", command)
        self.assertIn("--user-data-dir=/home/ubuntu/.employee-browser-profile", command)
        self.assertFalse(any(argument.startswith("--headless") for argument in command))
        relay = next(command for command in self.runtime.commands if "socat" in command)
        self.assertIn("TCP-LISTEN:9223,bind=0.0.0.0,reuseaddr,fork", relay)
        self.assertIn("TCP:127.0.0.1:9222", relay)
        self.assertEqual(result["cdp_endpoint"], "ws://127.0.0.1:34122/devtools/browser/test-id")
        self.assertEqual(self.runtime.launches, 1)
        self.assertEqual(self.runtime.relays, 1)
        self.assertEqual(self.runtime.display_probe_count, 1)

    def test_chrome_launch_waits_for_x11_readiness_without_a_start_deadline(self):
        self.runtime.ports_open = {9222: False, 9223: False}
        self.runtime.display_ready_after = 3

        self.runtime.ensure_session("sarah-johnson")

        self.assertEqual(self.runtime.display_probe_count, 3)
        display_probe_index = next(index for index, command in enumerate(self.runtime.commands)
                                   if "xdpyinfo" in command)
        chrome_launch_index = next(index for index, command in enumerate(self.runtime.commands)
                                   if "google-chrome" in command)
        self.assertLess(display_probe_index, chrome_launch_index)

    def test_x11_start_reports_s6_service_failure_without_waiting_for_a_deadline(self):
        self.runtime.ports_open = {9222: False, 9223: False}
        self.runtime.display_ready_after = 10
        self.runtime.xvfb_supervision = "down (exitcode 1) 0 seconds, normally up, want up"

        with self.assertRaisesRegex(EmployeeDesktopError, "Xvfb service exited"):
            self.runtime.ensure_session("sarah-johnson")

        self.assertEqual(self.runtime.launches, 0)
        self.assertEqual(self.runtime.display_probe_count, 1)

    def test_cdp_wait_reports_container_or_chrome_exit_without_a_deadline(self):
        name = self.runtime._container_name("sarah-johnson")
        self.runtime.containers[name] = self.runtime._record(
            "sarah-johnson", self.runtime._volume_name("sarah-johnson"), running=False,
        )
        self.runtime.containers[name]["State"]["ExitCode"] = 137

        def unavailable(port: int):
            raise URLError("not ready")

        self.runtime._cdp_document = unavailable
        with self.assertRaisesRegex(EmployeeDesktopError, "exit code 137"):
            self.runtime._wait_for_cdp(name, 34122)

        self.runtime.containers[name]["State"]["Running"] = True
        self.runtime._chrome_process_running = lambda container: False
        with self.assertRaisesRegex(EmployeeDesktopError, "Chrome exited"):
            self.runtime._wait_for_cdp(name, 34122)

    def test_profile_is_the_complete_identity_authority_for_listing_and_start(self):
        self.assertEqual(self.runtime.list_sessions(), [
            {"id": "sarah-johnson", "name": "Sarah Johnson", "role": "Technical", "status": "stopped"},
            {"id": "li-ming", "name": "Li Ming", "role": "Finance", "status": "stopped"},
        ])
        self.assertEqual(len(self.runtime.commands), 1)
        with self.assertRaises(KeyError):
            self.runtime.ensure_session("not-an-employee")
        self.assertEqual(len(self.runtime.commands), 1)

    def test_profile_id_with_unicode_underscore_dot_and_slash_is_preserved_safely(self):
        employee_id = "sales_東京.2/active"
        profile = json.loads(self.profile.read_text())
        profile["employees"].append({"id": employee_id, "displayName": "International Employee"})
        self.profile.write_text(json.dumps(profile), encoding="utf-8")

        result = self.runtime.ensure_session(employee_id)

        subfolder = self.runtime.proxy.subfolder_for(employee_id)
        self.assertEqual(result["employee_id"], employee_id)
        self.assertEqual(result["viewing_url"], f"https://desktop.example.test{subfolder}")
        self.assertRegex(subfolder, r"^/employee-desktops/[0-9a-f]{64}/$")
        self.assertNotIn(employee_id, result["viewing_url"])
        self.assertTrue(self.runtime._credentials_path(employee_id).name.startswith("employee-"))
        self.assertNotIn("/", self.runtime._credentials_path(employee_id).name)
        self.assertEqual(self.runtime._credentials_path(employee_id).stat().st_mode & 0o777, 0o600)
        container = self.runtime.containers[self.runtime._container_name(employee_id)]
        self.assertEqual(container["Config"]["Labels"][EMPLOYEE_LABEL], self.runtime._employee_key(employee_id))

    def test_existing_session_is_read_only_and_returns_endpoint_only_for_running_profile_employee(self):
        unknown_before = list(self.runtime.commands)
        with self.assertRaises(KeyError):
            self.runtime.existing_session("not-in-profile")
        self.assertEqual(self.runtime.commands, unknown_before)

        started = self.runtime.ensure_session("sarah-johnson")
        before_lookup = list(self.runtime.commands)
        routes_before_lookup = dict(self.runtime.proxy.routes)
        existing = self.runtime.existing_session("sarah-johnson")

        self.assertEqual(existing, {
            "employee_id": "sarah-johnson",
            "cdp_endpoint": started["cdp_endpoint"],
        })
        new_commands = self.runtime.commands[len(before_lookup):]
        self.assertTrue(all(command[0] not in {"start", "create", "stop"} for command in new_commands))
        self.assertEqual(self.runtime.proxy.routes, routes_before_lookup)
        self.assertNotIn("viewing_credentials", existing)
        self.runtime.stop_session("sarah-johnson")
        self.assertIsNone(self.runtime.existing_session("sarah-johnson"))

    def test_stop_preserves_container_profile_and_credentials_for_the_next_start(self):
        started = self.runtime.ensure_session("sarah-johnson")
        volume = self.runtime._volume_name("sarah-johnson")
        stopped = self.runtime.stop_session("sarah-johnson")
        self.assertNotIn("sarah-johnson", self.runtime.proxy.routes)
        restarted = self.runtime.ensure_session("sarah-johnson")

        self.assertEqual(stopped, {"employee_id": "sarah-johnson", "status": "stopped"})
        self.assertEqual(started["viewing_credentials"], restarted["viewing_credentials"])
        self.assertIn(volume, self.runtime.volumes)
        self.assertEqual(sum(command[0] == "create" for command in self.runtime.commands), 1)
        self.assertEqual(sum(command[0] == "start" for command in self.runtime.commands), 2)
        self.assertEqual(sum(command[0] == "stop" for command in self.runtime.commands), 1)
        self.assertEqual(self.runtime.proxy.routes["sarah-johnson"], "172.30.0.2")

    def test_preexisting_container_with_wrong_identity_label_is_rejected(self):
        name = self.runtime._container_name("sarah-johnson")
        wrong = self.runtime._record("li-ming", self.runtime._volume_name("sarah-johnson"), running=True)
        self.runtime.containers[name] = wrong
        self.runtime.volumes.add(self.runtime._volume_name("sarah-johnson"))

        with self.assertRaisesRegex(EmployeeDesktopError, "owned by another resource"):
            self.runtime.ensure_session("sarah-johnson")

    def test_employee_action_lock_blocks_same_employee_but_allows_other_employee(self):
        other_manager = _DockerFixtureRuntime(self.profile, self.state)
        waiting = threading.Event()
        acquired = threading.Event()
        failures = []

        def acquire_sarah():
            waiting.set()
            try:
                with other_manager.employee_action_lock("sarah-johnson"):
                    acquired.set()
            except BaseException as error:
                failures.append(error)

        with self.runtime.employee_action_lock("sarah-johnson"):
            with other_manager.employee_action_lock("li-ming"):
                pass
            worker = threading.Thread(target=acquire_sarah)
            worker.start()
            self.assertTrue(waiting.wait(1))
            self.assertFalse(acquired.wait(0.05))

        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(acquired.is_set())

        action_locks = list(self.state.glob("employee-action-*.lock"))
        self.assertEqual(len(action_locks), 2)
        self.assertEqual(action_locks[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("sarah-johnson", str(action_locks[0]))

    def test_lifecycle_lock_for_one_employee_does_not_block_another(self):
        other_manager = _DockerFixtureRuntime(self.profile, self.state)
        self.runtime._ensure_private_state_root()
        with self.runtime._lifecycle_lock("sarah-johnson"):
            # A separate employee lock can be acquired while Sarah's startup is
            # in progress; this guards against serializing independent startups.
            acquired = threading.Event()

            def acquire_li_lock():
                with other_manager._lifecycle_lock("li-ming"):
                    acquired.set()

            worker = threading.Thread(target=acquire_li_lock)
            worker.start()
            self.assertTrue(acquired.wait(timeout=1))
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())

    def test_shared_network_peer_is_rejected_before_session_is_returned(self):
        self.runtime.ensure_session("sarah-johnson")
        network = self.runtime._network_name("sarah-johnson")
        self.runtime.networks[network]["Containers"]["other-container-id"] = {"Name": "unrelated"}

        with self.assertRaisesRegex(EmployeeDesktopError, "shared with another container"):
            self.runtime.ensure_session("sarah-johnson")

    def test_loopback_or_host_published_web_path_is_rejected(self):
        self.runtime._ensure_private_state_root()
        self.runtime._credentials("sarah-johnson")
        name = self.runtime._container_name("sarah-johnson")
        record = self.runtime._record("sarah-johnson", self.runtime._volume_name("sarah-johnson"), running=True)
        record["NetworkSettings"]["Networks"][self.runtime._network_name("sarah-johnson")]["IPAddress"] = "127.0.0.1"
        self.runtime.containers[name] = record
        self.runtime.volumes.add(self.runtime._volume_name("sarah-johnson"))
        self.runtime.networks[self.runtime._network_name("sarah-johnson")] = {
            "Driver": "bridge", "Internal": False,
            "Labels": {MANAGED_LABEL: "true", EMPLOYEE_LABEL: self.runtime._employee_key("sarah-johnson")},
            "Containers": {"synthetic-container-id": {"Name": name}},
        }

        with self.assertRaisesRegex(EmployeeDesktopError, "valid network address"):
            self.runtime.ensure_session("sarah-johnson")

        record["NetworkSettings"]["Networks"][self.runtime._network_name("sarah-johnson")]["IPAddress"] = "172.30.0.2"
        record["NetworkSettings"]["Ports"]["8080/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "34121"}]
        with self.assertRaisesRegex(EmployeeDesktopError, "must not be published"):
            self.runtime.ensure_session("sarah-johnson")

    def test_container_address_accepts_configured_ipv4_and_ipv6_unicast(self):
        for address in ("8.8.8.8", "2001:4860:4860::8888"):
            with self.subTest(address=address):
                self.assertEqual(self.runtime._container_ip({
                    "NetworkSettings": {"Networks": {"employee-net": {
                        "IPAddress": address if ":" not in address else "",
                        "GlobalIPv6Address": address if ":" in address else "",
                    }}},
                }, "employee-net"), address)

    def test_existing_internal_employee_bridge_is_accepted(self):
        network = self.runtime._network_name("sarah-johnson")
        self.runtime.networks[network] = {
            "Driver": "bridge", "Internal": True,
            "Labels": {MANAGED_LABEL: "true", EMPLOYEE_LABEL: self.runtime._employee_key("sarah-johnson")},
            "Containers": {},
        }

        self.assertEqual(self.runtime.ensure_session("sarah-johnson")["status"], "running")

    def test_missing_local_image_has_actionable_error_without_creating_resources(self):
        def docker_without_image(*args, check=True):
            self.runtime.commands.append(args)
            if args[:2] == ("inspect", self.runtime._container_name("sarah-johnson")):
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            if args[:2] == ("image", "inspect"):
                return SimpleNamespace(returncode=1, stdout="", stderr="missing image")
            if args[0] == "info":
                return SimpleNamespace(returncode=0, stdout="available", stderr="")
            raise AssertionError(args)

        self.runtime._docker = docker_without_image
        with self.assertRaisesRegex(EmployeeDesktopImageUnavailable, "rerun coordinator bootstrap"):
            self.runtime.ensure_session("sarah-johnson")
        self.assertFalse(self.runtime.volumes)
        self.assertFalse(self.runtime.networks)


if __name__ == "__main__":
    unittest.main()
