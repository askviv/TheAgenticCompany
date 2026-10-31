"""Narrow Caddy admin API integration for employee Selkies routes.

Routes are installed through Caddy's permissioned Unix admin socket. This
module never edits or reloads the Caddyfile and never publishes arbitrary
upstreams: every route is pinned to one configured hostname, one profile ID,
and an inspected unicast address on that employee's isolated Docker network.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import base64
import subprocess
from typing import Any, Callable
from urllib.parse import quote, urlsplit


class EmployeeDesktopProxyError(RuntimeError):
    """Caddy route registration failed or the existing config was unexpected."""


_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z")


class CaddyEmployeeDesktopProxy:
    """Manage exact employee path routes on an existing Caddy HTTPS vhost.

    ``api_request`` exists for deterministic contract tests. In production the
    admin API is accessed only using curl's Unix socket support, not a TCP
    listener. Caddy must already have the configured host and TLS site loaded.
    """

    def __init__(
        self,
        hostname: str | None = None,
        *,
        viewing_origin: str | None = None,
        admin_socket: str = "/run/tac-https/admin.sock",
        api_request: Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]] | None = None,
    ):
        explicit_port: int | None = None
        if viewing_origin is not None:
            origin = urlsplit(viewing_origin)
            if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                    or origin.password is not None or origin.path not in {"", "/"}
                    or origin.query or origin.fragment):
                raise ValueError("viewing_origin must be an HTTPS origin without credentials or a path")
            if hostname is not None and hostname.lower().rstrip(".") != origin.hostname.lower().rstrip("."):
                raise ValueError("hostname and viewing_origin identify different sites")
            hostname = origin.hostname
            explicit_port = origin.port or 443
        normalized_host = hostname.lower().rstrip(".") if isinstance(hostname, str) else None
        if normalized_host is not None and not self._valid_host(normalized_host):
            raise ValueError("hostname must be a DNS hostname or IP address")
        self.hostname = normalized_host
        self.port = explicit_port or 443 if normalized_host is not None else None
        self.authority = self._authority(normalized_host, self.port) if normalized_host is not None else None
        self.admin_socket = admin_socket
        self._request = api_request or self._curl_request

    @staticmethod
    def subfolder_for(employee_id: str) -> str:
        """Return Selkies' path prefix for the employee's isolated desktop."""
        CaddyEmployeeDesktopProxy._validate_employee_id(employee_id)
        return f"/employee-desktops/{CaddyEmployeeDesktopProxy._employee_key(employee_id)}/"

    def url_for(self, employee_id: str, container_ip: str, credentials: dict[str, str]) -> str:
        """Register (or safely update) the employee route and return its HTTPS URL."""
        self._validate_employee_id(employee_id)
        upstream_ip = self._validate_container_ip(container_ip)
        basic_auth = self._basic_auth(credentials)
        route_id = self._route_id(employee_id)
        conflicted_etag: str | None = None
        while True:
            servers, etag = self._get_servers()
            if conflicted_etag is not None and etag == conflicted_etag:
                raise EmployeeDesktopProxyError("Caddy rejected an employee route update without a newer configuration ETag")
            self._ensure_hostname(servers)
            server_name = self._find_server(servers)
            route = self._route(employee_id, upstream_ip, basic_auth)
            found = self._find_route(servers[server_name], route_id)
            if found is None and self._find_route(servers, route_id) is not None:
                raise EmployeeDesktopProxyError("existing Caddy employee route is outside the configured HTTPS server")
            if found is None:
                path = f"/config/apps/http/servers/{quote(server_name, safe='')}/routes/0"
                status, _, _ = self._request("PUT", path, self._headers(etag), self._json(route))
            else:
                existing = found
                self._validate_existing_route(existing, employee_id)
                path = f"/id/{quote(route_id, safe='')}"
                status, _, _ = self._request("PATCH", path, self._headers(etag), self._json(route))
            if status == 412:
                conflicted_etag = etag
                continue
            if status < 200 or status >= 300:
                raise EmployeeDesktopProxyError(f"Caddy rejected employee route update (HTTP {status})")
            return f"https://{self.authority}{self.subfolder_for(employee_id)}"

    def host_mappings(self) -> list[tuple[str, str]]:
        """Map exact Caddy-hosted names to the listener address reachable by Docker.

        Specific Caddy bind addresses are used directly. Wildcard listeners use
        Docker's host-gateway alias, which leaves all other DNS resolution to
        the configured resolver.
        """
        servers, _ = self._get_servers()
        mappings: set[tuple[str, str]] = set()
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            hosts = self._collect_route_hosts(server.get("routes", []))
            if not hosts:
                continue
            targets = self._https_listener_targets(server)
            if not targets:
                continue
            mappings.update((host, target) for host in hosts for target in targets)
        if not mappings:
            raise EmployeeDesktopProxyError("Caddy has no exact HTTPS host routes with a Docker-reachable listener")
        return sorted(mappings)

    def remove(self, employee_id: str) -> None:
        """Remove only this module's exact route; absent routes are idempotent."""
        self._validate_employee_id(employee_id)
        route_id = self._route_id(employee_id)
        conflicted_etag: str | None = None
        while True:
            servers, etag = self._get_servers()
            if conflicted_etag is not None and etag == conflicted_etag:
                raise EmployeeDesktopProxyError("Caddy rejected an employee route removal without a newer configuration ETag")
            self._ensure_hostname(servers)
            server_name = self._find_server(servers)
            found = self._find_route(servers[server_name], route_id)
            if found is None and self._find_route(servers, route_id) is not None:
                raise EmployeeDesktopProxyError("existing Caddy employee route is outside the configured HTTPS server")
            if found is None:
                return
            status, _, _ = self._request(
                "DELETE", f"/id/{quote(route_id, safe='')}", self._headers(etag), None,
            )
            if status == 412:
                conflicted_etag = etag
                continue
            if status < 200 or status >= 300:
                raise EmployeeDesktopProxyError(f"Caddy rejected employee route removal (HTTP {status})")
            return

    @staticmethod
    def _validate_employee_id(employee_id: str) -> None:
        if not isinstance(employee_id, str) or not employee_id:
            raise ValueError("employee_id is invalid")
        try:
            employee_id.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("employee_id is invalid") from error

    @staticmethod
    def _employee_key(employee_id: str) -> str:
        return hashlib.sha256(employee_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _route_id(employee_id: str) -> str:
        # Avoid exposing employee names in Caddy's config and keep IDs URL-safe.
        return "tac-employee-desktop-" + hashlib.sha256(employee_id.encode("utf-8")).hexdigest()

    def _route(self, employee_id: str, container_ip: str, basic_auth: str) -> dict[str, Any]:
        prefix = self.subfolder_for(employee_id).rstrip("/")
        return {
            "@id": self._route_id(employee_id),
            "match": [{"host": [self.hostname], "path": [prefix, prefix + "/", prefix + "/*"]}],
            "handle": [{
                "handler": "reverse_proxy",
                "headers": {"request": {"set": {"Authorization": [basic_auth]}}},
                "upstreams": [{"dial": f"{self._dial_host(container_ip)}:8080"}],
            }],
            "terminal": True,
        }

    @staticmethod
    def _basic_auth(credentials: dict[str, str]) -> str:
        if (not isinstance(credentials, dict) or not isinstance(credentials.get("username"), str)
                or not credentials["username"] or not isinstance(credentials.get("password"), str)
                or not credentials["password"] or ":" in credentials["username"]):
            raise ValueError("desktop credentials must contain a valid username and password")
        token = base64.b64encode(
            f"{credentials['username']}:{credentials['password']}".encode("utf-8"),
        ).decode("ascii")
        return "Basic " + token

    def _get_servers(self) -> tuple[dict[str, Any], str]:
        status, headers, body = self._request("GET", "/config/apps/http/servers", {}, None)
        if status < 200 or status >= 300:
            raise EmployeeDesktopProxyError(f"Caddy admin API could not read HTTP servers (HTTP {status})")
        try:
            servers = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EmployeeDesktopProxyError("Caddy admin API returned invalid HTTP server configuration") from error
        if not isinstance(servers, dict):
            raise EmployeeDesktopProxyError("Caddy admin API returned invalid HTTP server configuration")
        etag = next((value for key, value in headers.items() if key.lower() == "etag"), None)
        if not isinstance(etag, str) or not etag:
            raise EmployeeDesktopProxyError("Caddy admin API omitted the configuration ETag")
        return servers, etag

    def _find_server(self, servers: dict[str, Any]) -> str:
        candidates = [name for name, server in servers.items()
                      if isinstance(server, dict) and self._contains_host(server.get("routes", []))]
        if len(candidates) != 1:
            raise EmployeeDesktopProxyError("Caddy must have exactly one HTTP server for the employee desktop hostname")
        return candidates[0]

    def _ensure_hostname(self, servers: dict[str, Any]) -> None:
        if self.hostname is None:
            matches: set[str] = set()
            for server in servers.values():
                if isinstance(server, dict):
                    self._collect_legacy_host_matches(server.get("routes", []), None, matches)
            if len(matches) != 1:
                raise EmployeeDesktopProxyError(
                    "Caddy must have exactly one hostname on the existing 127.0.0.1:8110 experiments route"
                )
            discovered = next(iter(matches)).lower().rstrip(".")
            if not self._valid_host(discovered):
                raise EmployeeDesktopProxyError("existing experiments route has an invalid hostname matcher")
            self.hostname = discovered
            server_name = self._find_server(servers)
            self.port = self._server_https_port(servers[server_name])
            self.authority = self._authority(self.hostname, self.port)

    @staticmethod
    def _valid_host(host: str) -> bool:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return bool(_HOST.fullmatch(host))

    @staticmethod
    def _authority(host: str | None, port: int | None) -> str:
        if host is None:
            raise EmployeeDesktopProxyError("employee desktop HTTPS hostname is unavailable")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            authority_host = host
        else:
            authority_host = f"[{host}]" if address.version == 6 else host
        return authority_host if port in {None, 443} else f"{authority_host}:{port}"

    @staticmethod
    def _server_https_port(server: dict[str, Any]) -> int:
        listen = server.get("listen")
        ports: set[int] = set()
        if isinstance(listen, list):
            for address in listen:
                if not isinstance(address, str) or ":" not in address:
                    continue
                try:
                    port = int(address.rsplit(":", 1)[1])
                except ValueError:
                    continue
                if port != 80:
                    ports.add(port)
        if len(ports) != 1:
            raise EmployeeDesktopProxyError("Caddy HTTPS listener port is missing or ambiguous")
        return next(iter(ports))

    @classmethod
    def _https_listener_targets(cls, server: dict[str, Any]) -> set[str]:
        targets: set[str] = set()
        listeners = server.get("listen")
        if not isinstance(listeners, list):
            return targets
        for listener in listeners:
            if not isinstance(listener, str):
                continue
            if listener.startswith(":"):
                try:
                    port = int(listener[1:])
                except ValueError:
                    continue
                if port != 80:
                    targets.add("host-gateway")
                continue
            try:
                parsed = urlsplit("//" + listener)
                port = parsed.port
            except ValueError:
                continue
            if port in {None, 80}:
                continue
            host = parsed.hostname
            if host is None:
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                # Caddy resolves named bind addresses on its own host. The
                # Docker host-gateway target is safe only for wildcard binds.
                continue
            if address.is_unspecified:
                targets.add("host-gateway")
            elif not (address.is_loopback or address.is_link_local or address.is_multicast):
                targets.add(str(address))
        return targets

    @classmethod
    def _collect_route_hosts(cls, value: Any) -> set[str]:
        hosts: set[str] = set()
        if isinstance(value, list):
            for item in value:
                hosts.update(cls._collect_route_hosts(item))
            return hosts
        if not isinstance(value, dict):
            return hosts
        matchers = value.get("match")
        if isinstance(matchers, list):
            for matcher in matchers:
                candidates = matcher.get("host") if isinstance(matcher, dict) else None
                if isinstance(candidates, list):
                    for candidate in candidates:
                        if not isinstance(candidate, str):
                            continue
                        normalized = candidate.lower().rstrip(".")
                        try:
                            ipaddress.ip_address(normalized)
                        except ValueError:
                            if _HOST.fullmatch(normalized):
                                hosts.add(normalized)
        for key, item in value.items():
            if key != "match":
                hosts.update(cls._collect_route_hosts(item))
        return hosts

    @staticmethod
    def _dial_host(host: str) -> str:
        address = ipaddress.ip_address(host)
        return f"[{host}]" if address.version == 6 else host

    @classmethod
    def _collect_legacy_host_matches(cls, value: Any, inherited_host: str | None, result: set[str]) -> None:
        if isinstance(value, list):
            for item in value:
                cls._collect_legacy_host_matches(item, inherited_host, result)
            return
        if not isinstance(value, dict):
            return
        current_host = inherited_host
        matchers = value.get("match")
        if isinstance(matchers, list):
            for matcher in matchers:
                hosts = matcher.get("host") if isinstance(matcher, dict) else None
                if isinstance(hosts, list) and len(hosts) == 1 and isinstance(hosts[0], str):
                    current_host = hosts[0]
        if value.get("handler") == "reverse_proxy" and current_host:
            upstreams = value.get("upstreams")
            if isinstance(upstreams, list) and any(
                isinstance(upstream, dict) and upstream.get("dial") == "127.0.0.1:8110"
                for upstream in upstreams
            ):
                result.add(current_host)
        for key, item in value.items():
            if key != "match":
                cls._collect_legacy_host_matches(item, current_host, result)

    def _contains_host(self, value: Any) -> bool:
        if isinstance(value, dict):
            matcher = value.get("match")
            if isinstance(matcher, list) and any(
                isinstance(item, dict) and self.hostname in item.get("host", [])
                for item in matcher
            ):
                return True
            return any(self._contains_host(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_host(item) for item in value)
        return False

    @classmethod
    def _find_route(cls, value: Any, route_id: str) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if value.get("@id") == route_id:
                return value
            for item in value.values():
                found = cls._find_route(item, route_id)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_route(item, route_id)
                if found is not None:
                    return found
        return None

    def _validate_existing_route(self, route: dict[str, Any], employee_id: str) -> None:
        expected_prefix = self.subfolder_for(employee_id).rstrip("/")
        matchers = route.get("match")
        handlers = route.get("handle")
        expected_matchers = [{"host": [self.hostname], "path": [expected_prefix, expected_prefix + "/", expected_prefix + "/*"]}]
        if matchers != expected_matchers or not isinstance(handlers, list) or len(handlers) != 1:
            raise EmployeeDesktopProxyError("existing Caddy employee route has unexpected ownership or matchers")
        proxy_handler = handlers[0]
        if proxy_handler.get("handler") != "reverse_proxy":
            raise EmployeeDesktopProxyError("existing Caddy employee route has unexpected handler")
        upstreams = proxy_handler.get("upstreams")
        if not isinstance(upstreams, list) or len(upstreams) != 1:
            raise EmployeeDesktopProxyError("existing Caddy employee route has unexpected upstream configuration")
        dial = upstreams[0].get("dial")
        parsed = urlsplit("//" + dial) if isinstance(dial, str) else None
        try:
            self._validate_container_ip(parsed.hostname if parsed else None)
            valid_port = parsed is not None and parsed.port == 8080
        except (ValueError, AttributeError):
            valid_port = False
        if not valid_port:
            raise EmployeeDesktopProxyError("existing Caddy employee route upstream is not a valid desktop container address")

    @staticmethod
    def _validate_container_ip(value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError) as error:
            raise ValueError("container_ip must be an IPv4 or IPv6 unicast address") from error
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError("container_ip must be an IPv4 or IPv6 unicast address")
        return str(address)

    @staticmethod
    def _headers(etag: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "If-Match": etag}

    @staticmethod
    def _json(value: dict[str, Any]) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def _curl_request(self, method: str, path: str, headers: dict[str, str], body: bytes | None):
        command = ["curl", "--silent", "--show-error", "--include", "--unix-socket", self.admin_socket,
                   "--request", method]
        for name, value in headers.items():
            command.extend(["--header", f"{name}: {value}"])
        if body is not None:
            command.extend(["--data-binary", "@-"])
        command.extend(["http://localhost" + path])
        try:
            result = subprocess.run(command, input=body, capture_output=True)
        except OSError as error:
            raise EmployeeDesktopProxyError("Caddy admin Unix socket request failed") from error
        if result.returncode != 0:
            raise EmployeeDesktopProxyError("Caddy admin API request failed")
        response = result.stdout
        separator = response.find(b"\r\n\r\n")
        if separator < 0:
            raise EmployeeDesktopProxyError("Caddy admin API returned an invalid HTTP response")
        head, content = response[:separator], response[separator + 4:]
        lines = head.split(b"\r\n")
        try:
            status = int(lines[0].split(b" ", 2)[1])
            parsed_headers = {}
            for line in lines[1:]:
                name, value = line.split(b":", 1)
                parsed_headers[name.decode("ascii").strip()] = value.decode("latin-1").strip()
        except (IndexError, ValueError, UnicodeDecodeError) as error:
            raise EmployeeDesktopProxyError("Caddy admin API returned an invalid HTTP response") from error
        return status, parsed_headers, content
