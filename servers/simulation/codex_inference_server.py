"""Host-only entry point for the authenticated Codex CLI inference service."""

from servers.simulation.codex_remote import host_app_from_environment

app = host_app_from_environment()
