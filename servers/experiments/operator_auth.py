"""Private operator-session helpers for the experiments dashboard."""

from __future__ import annotations

import os
from pathlib import Path
import secrets

from flask import current_app, session


OPERATOR_SESSION_KEY = "experiments_operator_authenticated"
PUBLIC_ENDPOINTS = frozenset({
    "static", "index", "experiment_detail", "run_detail", "runs", "experiment_input",
    "api_session", "api_operator_status", "operator_login", "operator_logout",
    "health", "chat_events",
})


class OperatorKeyUnavailable(RuntimeError):
    """The private operator key is not mounted or readable."""


def is_operator_authenticated() -> bool:
    return session.get(OPERATOR_SESSION_KEY) is True


def verify_operator_key(candidate: str) -> bool:
    path = Path(current_app.config["OPERATOR_KEY_FILE"])
    try:
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise OperatorKeyUnavailable("operator key file permissions are not private")
        expected = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise OperatorKeyUnavailable from exc
    if not expected:
        raise OperatorKeyUnavailable("operator key file is empty")
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def mark_operator_authenticated() -> None:
    session[OPERATOR_SESSION_KEY] = True


def clear_operator_authenticated() -> None:
    session.pop(OPERATOR_SESSION_KEY, None)
