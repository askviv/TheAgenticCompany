"""Synthetic contract tests for the narrow Caddy employee route manager."""

from __future__ import annotations

import copy
import json
import unittest

from servers.simulation.employee_desktop_proxy import (
    CaddyEmployeeDesktopProxy,
    EmployeeDesktopProxyError,
)


class _CaddyApi:
    def __init__(self):
        self.servers = {"srv0": {"listen": [":443"], "routes": [
            {"match": [{"host": ["desktop.example.test"]}],
             "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8110"}]}]},
            {"match": [{"host": ["other.example.test"]}], "handle": [{"handler": "file_server"}]},
        ]}}
        self.etag = 1
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.next_conflict = False

    def request(self, method, path, headers, body):
        self.calls.append((method, path, headers, body))
        if method == "GET" and path == "/config/apps/http/servers":
            return 200, {"ETag": f'"servers-{self.etag}"'}, json.dumps(self.servers).encode()
        if self.next_conflict:
            self.next_conflict = False
            self.etag += 1
            return 412, {}, b""
        route = json.loads(body) if body is not None else None
        if method == "PUT" and path.endswith("/routes/0"):
            server_name = path.split("/")[-3]
            self.servers[server_name]["routes"].insert(0, route)
        elif method == "PATCH" and path.startswith("/id/"):
            route_id = path.removeprefix("/id/")
            index = self._route_index(route_id)
            self.servers["srv0"]["routes"][index] = route
        elif method == "DELETE" and path.startswith("/id/"):
            route_id = path.removeprefix("/id/")
            index = self._route_index(route_id)
            del self.servers["srv0"]["routes"][index]
        else:
            raise AssertionError((method, path))
        self.etag += 1
        return 200, {}, b""

    def _route_index(self, route_id):
        for index, route in enumerate(self.servers["srv0"]["routes"]):
            if route.get("@id") == route_id:
                return index
        raise AssertionError(f"route not found: {route_id}")


class CaddyEmployeeDesktopProxyTests(unittest.TestCase):
    credentials = {"username": "test-user", "password": "synthetic-only-password"}

    def setUp(self):
        self.api = _CaddyApi()
        self.proxy = CaddyEmployeeDesktopProxy("desktop.example.test", api_request=self.api.request)

    def url_for(self, proxy, employee_id, container_ip):
        return proxy.url_for(employee_id, container_ip, self.credentials)

    def test_registers_exact_path_on_existing_host_with_employee_bridge_upstream(self):
        url = self.url_for(self.proxy, "sarah-johnson", "172.30.0.2")

        prefix = self.proxy.subfolder_for("sarah-johnson")
        self.assertEqual(url, "https://desktop.example.test" + prefix)
        route = self.api.servers["srv0"]["routes"][0]
        self.assertEqual(route["match"], [{
            "host": ["desktop.example.test"],
            "path": [prefix.rstrip("/"), prefix, prefix.rstrip("/") + "/*"],
        }])
        proxy_handler = route["handle"][0]
        self.assertEqual(proxy_handler["headers"]["request"]["set"]["Authorization"], [
            "Basic dGVzdC11c2VyOnN5bnRoZXRpYy1vbmx5LXBhc3N3b3Jk",
        ])
        self.assertEqual(proxy_handler["upstreams"], [{"dial": "172.30.0.2:8080"}])
        self.assertTrue(route["terminal"])
        self.assertEqual(self.api.servers["srv0"]["routes"][2]["match"][0]["host"], ["other.example.test"])

    def test_discovers_hostname_from_existing_experiments_upstream(self):
        proxy = CaddyEmployeeDesktopProxy(api_request=self.api.request)
        url = self.url_for(proxy, "sarah-johnson", "172.30.0.2")
        self.assertEqual(url, "https://desktop.example.test" + proxy.subfolder_for("sarah-johnson"))
        self.assertEqual(self.api.servers["srv0"]["routes"][0]["match"][0]["host"],
                         ["desktop.example.test"])
        self.assertEqual(proxy.host_mappings(), [
            ("desktop.example.test", "host-gateway"),
            ("other.example.test", "host-gateway"),
        ])
        self.api.servers["srv0"]["listen"] = ["192.0.2.10:443"]
        self.assertEqual(proxy.host_mappings(), [
            ("desktop.example.test", "192.0.2.10"),
            ("other.example.test", "192.0.2.10"),
        ])

    def test_fails_clearly_if_legacy_experiments_route_is_ambiguous(self):
        self.api.servers["srv1"] = {"routes": [{
            "match": [{"host": ["second.example.test"]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8110"}]}],
        }]}
        with self.assertRaisesRegex(EmployeeDesktopProxyError, "exactly one hostname"):
            self.url_for(CaddyEmployeeDesktopProxy(api_request=self.api.request), "sarah-johnson", "172.30.0.2")

    def test_preserves_configured_and_discovered_nonstandard_https_ports(self):
        configured = CaddyEmployeeDesktopProxy(
            viewing_origin="https://desktop.example.test:8443", api_request=self.api.request,
        )
        self.assertEqual(self.url_for(configured, "sarah-johnson", "172.30.0.2"),
                         "https://desktop.example.test:8443" + configured.subfolder_for("sarah-johnson"))

        autodiscovered_api = _CaddyApi()
        autodiscovered_api.servers["srv0"]["listen"] = [":8443"]
        autodiscovered = CaddyEmployeeDesktopProxy(api_request=autodiscovered_api.request)
        self.assertEqual(self.url_for(autodiscovered, "li-ming", "172.30.0.3"),
                         "https://desktop.example.test:8443" + autodiscovered.subfolder_for("li-ming"))

    def test_accepts_ip_literal_site_and_ipv6_https_authority(self):
        api = _CaddyApi()
        api.servers["srv0"]["routes"][0]["match"][0]["host"] = ["2001:db8::10"]
        proxy = CaddyEmployeeDesktopProxy(
            viewing_origin="https://[2001:db8::10]:9443", api_request=api.request,
        )

        url = self.url_for(proxy, "sarah-johnson", "2001:db8::20")

        self.assertEqual(url, "https://[2001:db8::10]:9443" + proxy.subfolder_for("sarah-johnson"))
        self.assertEqual(api.servers["srv0"]["routes"][0]["handle"][0]["upstreams"],
                         [{"dial": "[2001:db8::20]:8080"}])

    def test_retry_updates_only_owned_route_and_is_idempotent(self):
        self.url_for(self.proxy, "sarah-johnson", "172.30.0.2")
        original_unrelated_routes = copy.deepcopy(self.api.servers["srv0"]["routes"][1:])
        self.url_for(self.proxy, "sarah-johnson", "172.30.0.3")

        employee_routes = [route for route in self.api.servers["srv0"]["routes"]
                           if route.get("@id", "").startswith("tac-employee-desktop-")]
        self.assertEqual(len(employee_routes), 1)
        self.assertEqual(employee_routes[0]["handle"][0]["upstreams"],
                         [{"dial": "172.30.0.3:8080"}])
        self.assertEqual(self.api.servers["srv0"]["routes"][1:], original_unrelated_routes)
        self.assertTrue(all("If-Match" in call[2] for call in self.api.calls if call[0] in {"PUT", "PATCH"}))

    def test_etag_conflict_reloads_and_retries_without_losing_routes(self):
        self.api.next_conflict = True
        self.url_for(self.proxy, "sarah-johnson", "172.30.0.2")
        self.assertEqual(len(self.api.servers["srv0"]["routes"]), 3)
        self.assertGreaterEqual(sum(call[0] == "GET" for call in self.api.calls), 2)

    def test_remove_is_idempotent_and_deletes_only_owned_employee_route(self):
        self.url_for(self.proxy, "sarah-johnson", "172.30.0.2")
        self.url_for(self.proxy, "li-ming", "172.30.0.3")
        before_other = next(route for route in self.api.servers["srv0"]["routes"]
                            if route.get("@id", "").startswith("tac-employee-desktop-")
                            and route["match"][0]["path"][0] == self.proxy.subfolder_for("li-ming").rstrip("/"))
        self.proxy.remove("sarah-johnson")
        self.proxy.remove("sarah-johnson")
        self.assertEqual(len([route for route in self.api.servers["srv0"]["routes"]
                              if route.get("@id", "").startswith("tac-employee-desktop-")]), 1)
        self.assertIn(before_other, self.api.servers["srv0"]["routes"])

    def test_refuses_untrusted_address_and_unexpected_owned_route(self):
        with self.assertRaises(ValueError):
            CaddyEmployeeDesktopProxy("https://desktop.example.test")
        with self.assertRaises(ValueError):
            self.url_for(self.proxy, "sarah-johnson", "127.0.0.1")
        self.url_for(self.proxy, "sarah-johnson", "172.30.0.2")
        self.api.servers["srv0"]["routes"][0]["match"][0]["host"] = ["attacker.example.test"]
        with self.assertRaisesRegex(EmployeeDesktopProxyError, "unexpected ownership"):
            self.url_for(self.proxy, "sarah-johnson", "172.30.0.3")

    def test_subfolder_is_opaque_for_arbitrary_profile_id(self):
        arbitrary_id = "sales_東京.2/active"
        prefix = CaddyEmployeeDesktopProxy.subfolder_for(arbitrary_id)
        self.assertRegex(prefix, r"^/employee-desktops/[0-9a-f]{64}/$")
        self.assertNotIn(arbitrary_id, prefix)
        with self.assertRaises(ValueError):
            CaddyEmployeeDesktopProxy.subfolder_for("")

    def test_ipv6_desktop_upstreams_are_bracketed_in_caddy_config(self):
        self.url_for(self.proxy, "sarah-johnson", "fd12:3456::2")
        self.assertEqual(self.api.servers["srv0"]["routes"][0]["handle"][0]["upstreams"],
                         [{"dial": "[fd12:3456::2]:8080"}])


if __name__ == "__main__":
    unittest.main()
