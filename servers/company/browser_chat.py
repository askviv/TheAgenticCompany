"""Employee desktop identity resolution shared by native CUA transports."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

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
