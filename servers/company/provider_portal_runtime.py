"""Environment factory for the separately hosted employee provider portal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from servers.simulation.mail import MailuGateway
from servers.simulation.store import SQLiteStore
from .provider_portal import (
    NativeProviderHTTPClient,
    _read_private_key,
    create_app,
)


def _private_json(path: Path) -> dict[str, Any]:
    if path.stat().st_mode & 0o077:
        raise PermissionError("Provider portal runtime configuration must be private to its owner")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Provider portal runtime configuration must be an object")
    return value


def create_provider_portal_app(config_path: str | Path):
    portal_config = _private_json(Path(config_path))
    required = (
        "runtime_config_file", "session_key_file", "integration_key_file",
        "provider_worker_url", "provider_mail_actors", "public_url",
    )
    if any(key not in portal_config for key in required):
        raise ValueError("Provider portal runtime configuration is incomplete")
    from servers.simulation.runtime import RuntimeConfig, employee_desktop_manager, private_json

    runtime_path = Path(portal_config["runtime_config_file"])
    runtime = RuntimeConfig.model_validate(private_json(runtime_path))
    employee_profile = json.loads(runtime.company_profile_file.read_text(encoding="utf-8"))
    mail_credentials = private_json(runtime.mail_credentials_file)
    token = runtime.actor_workers.customer_token
    if token is None:
        raise ValueError("Native τ provider worker authentication is not configured")
    worker = NativeProviderHTTPClient(
        str(portal_config["provider_worker_url"]), token.get_secret_value(),
        timeout=runtime.actor_workers.request_timeout_seconds or 180,
    )
    return create_app(
        store=SQLiteStore(runtime.state_root / "state.sqlite"),
        profile=employee_profile,
        mail_config=runtime.mail,
        mail_credentials=mail_credentials,
        session_secret=_read_private_key(Path(portal_config["session_key_file"])),
        integration_secret=_read_private_key(Path(portal_config["integration_key_file"])),
        provider_worker=worker,
        mail_gateway=MailuGateway(
            runtime.mail,
            mail_credentials,
            employee_desktops=employee_desktop_manager(runtime),
        ),
        provider_mail_actors=portal_config["provider_mail_actors"],
        portal_public_url=portal_config["public_url"],
    )


def app_from_environment():
    config = os.environ.get("PROVIDER_PORTAL_CONFIG_FILE")
    if not config:
        raise RuntimeError("PROVIDER_PORTAL_CONFIG_FILE is required")
    return create_provider_portal_app(config)
