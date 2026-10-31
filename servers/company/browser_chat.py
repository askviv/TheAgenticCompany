"""Per-employee Playwright access to the managed Rocket.Chat browser session."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


class EmployeeBrowserSessionProvider(Protocol):
    def open_for_employee_email(self, email: str) -> Any:
        """Return a context manager yielding that employee's Playwright page."""


class PlaywrightEmployeeBrowserSessionProvider:
    """Connect to the employee-specific CDP endpoint supplied by the desktop manager."""

    def __init__(self, desktop_manager_factory: Callable[[], Any] | None = None):
        self.desktop_manager_factory = desktop_manager_factory or _configured_desktop_manager

    @contextmanager
    def open_for_employee_email(self, email: str) -> Iterator[Any]:
        manager = self.desktop_manager_factory()
        employee_id = employee_id_for_email(manager.company_profile_path, email)
        with manager.employee_action_lock(employee_id):
            desktop = manager.ensure_session(employee_id)
            endpoint = desktop.get("cdp_endpoint") if isinstance(desktop, dict) else None
            if not isinstance(endpoint, str) or not endpoint.startswith(("ws://", "wss://")):
                raise RuntimeError("EMPLOYEE_BROWSER_ENDPOINT_UNAVAILABLE")

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                contexts = browser.contexts
                context = contexts[0] if contexts else browser.new_context()
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                yield page


def _configured_desktop_manager() -> Any:
    # Importing runtime config is deliberately lazy: email-only scenarios do not
    # need Docker, the private coordinator config, or browser dependencies.
    from servers.simulation.runtime import employee_desktop_manager

    return employee_desktop_manager()


def employee_id_for_email(profile_path: str | Path, email: str) -> str:
    if not isinstance(email, str) or not email.strip():
        raise ValueError("ROCKETCHAT_EMPLOYEE_EMAIL_MISSING")
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("COMPANY_IDENTITY_PROFILE_UNAVAILABLE") from error
    employees = profile.get("employees") if isinstance(profile, dict) else None
    if not isinstance(employees, list):
        raise RuntimeError("COMPANY_IDENTITY_PROFILE_INVALID")
    matches = [item for item in employees if isinstance(item, dict)
               and isinstance(item.get("email"), str)
               and item["email"].casefold() == email.casefold()]
    if len(matches) != 1:
        raise ValueError("ROCKETCHAT_EMPLOYEE_EMAIL_NOT_UNIQUE")
    employee_id = matches[0].get("id")
    if not isinstance(employee_id, str) or not employee_id:
        raise RuntimeError("ROCKETCHAT_EMPLOYEE_ID_UNAVAILABLE")
    return employee_id
