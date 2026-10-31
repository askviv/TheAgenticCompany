"""Rocket.Chat adapter for posting and tracking human-led experiments."""

from __future__ import annotations

import os
import json
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests


class RocketChatError(RuntimeError):
    """The Rocket.Chat API returned an unsuccessful application response."""

    def __init__(self, endpoint: str, code: str) -> None:
        self.code = code
        super().__init__(f"Rocket.Chat {endpoint} failed ({code}).")


class RocketChatClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        viv_username: str,
        channel: str = "viv-experiments",
        *,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.viv_username = viv_username.lstrip("@")
        self.channel = channel.lstrip("#")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._user_id: str | None = None
        self._room_id: str | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "RocketChatClient":
        """Build a client from required service credentials and exact Viv username."""
        return cls(
            base_url=os.environ["ROCKETCHAT_BASE_URL"],
            username=os.environ["ROCKETCHAT_USERNAME"],
            password=os.environ["ROCKETCHAT_PASSWORD"],
            viv_username=os.environ["VIV_ROCKETCHAT_USERNAME"],
            channel=os.getenv("EXPERIMENTS_ROCKETCHAT_CHANNEL", "viv-experiments"),
        )

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method,
            urljoin(self.base_url, f"api/v1/{endpoint.lstrip('/')}"),
            headers={"X-Auth-Token": self._auth_token, "X-User-Id": self._user_id}
            if self._auth_token and self._user_id
            else {},
            timeout=self.timeout,
            **kwargs,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as http_error:
            try:
                payload = response.json()
            except ValueError:
                raise
            if payload.get("success") is False:
                code = payload.get("errorType") or "unsuccessful_response"
                raise RocketChatError(endpoint, str(code)) from http_error
            raise
        payload = response.json()
        if not payload.get("success", False):
            # API messages can contain provider internals; expose only the safe code.
            code = payload.get("errorType") or "unsuccessful_response"
            raise RocketChatError(endpoint, str(code))
        return payload

    @property
    def _auth_token(self) -> str | None:
        return getattr(self, "_token", None)

    def login(self) -> None:
        with self._lock:
            response = self.session.post(
                urljoin(self.base_url, "api/v1/login"),
                json={"user": self.username, "password": self.password},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            if not payload.get("status") == "success" or not data.get("authToken") or not data.get("userId"):
                raise RocketChatError("login", "invalid_credentials")
            self._token = data["authToken"]
            self._user_id = data["userId"]
            user = self._request("GET", "users.info", params={"username": self.viv_username})
            self._viv_user_id = (user.get("user") or {}).get("_id")
            if not self._viv_user_id:
                raise RocketChatError("users.info", "configured_user_not_found")

    def _ensure_login(self) -> None:
        if not self._auth_token or not self._user_id:
            self.login()

    def _ensure_channel(self) -> str:
        with self._lock:
            self._ensure_login()
            if self._room_id:
                return self._room_id
            listing = self._request(
                "GET",
                "channels.list",
                params={"count": 1000, "query": json.dumps({"name": self.channel})},
            )
            room = next(
                (item for item in listing.get("channels", []) if item.get("name") == self.channel),
                None,
            )
            if room is None:
                try:
                    created = self._request(
                        "POST",
                        "channels.create",
                        json={"name": self.channel, "members": [self.viv_username]},
                    )
                    room = created.get("channel")
                except RocketChatError as error:
                    if error.code != "error-duplicate-channel-name":
                        raise
                    # Another concurrent run created the shared room; look it up again.
                    listing = self._request(
                        "GET",
                        "channels.list",
                        params={"count": 1000, "query": json.dumps({"name": self.channel})},
                    )
                    room = next(
                        (item for item in listing.get("channels", []) if item.get("name") == self.channel),
                        None,
                    )
                    if room is None:
                        raise RocketChatError("channels.list", "duplicate_channel_not_visible") from error
            if not room or not room.get("_id"):
                raise RocketChatError("channels.list", "missing_channel_id")
            self._room_id = room["_id"]
        members = self._request("GET", "channels.members", params={"roomId": self._room_id})
        member_ids = {member.get("_id") for member in members.get("members", [])}
        if self._viv_user_id not in member_ids:
            self._request(
                "POST",
                "channels.invite",
                json={"roomId": self._room_id, "userId": self._viv_user_id},
            )
        return self._room_id

    def dispatch_employee_browser(
        self, run_id: int | str, title: str, description: str, inputs=(), *,
        role: str, employee_id: str, employee_user_id: str, coordinator: Any,
    ) -> dict[str, Any]:
        """Prepare channel membership, then send the initial message in employee Chrome."""
        with self._lock:
            room_id = self._ensure_channel()
            legacy_message_id = f"exp-{run_id}"
            legacy_text = (
                f"@{self.viv_username} **Experiment {run_id}: {title}**\n\n"
                f"{description}\n\nPlease reply in this thread with your response."
            )
            if inputs:
                legacy_text += "\n\nSource files:\n" + "\n".join(
                    f"- [{item['name']}]({item['url']})" for item in inputs
                )
                legacy_text += "\nUse these source files for the assignment and share your deliverables in this thread."
            existing = self._get_message(legacy_message_id)
            if existing is not None:
                author = existing.get("u") or {}
                if (existing.get("rid") != room_id or not isinstance(author, dict)
                        or author.get("_id") != self._user_id or existing.get("msg") != legacy_text):
                    raise RocketChatError("chat.getMessage", "legacy_message_mismatch")
                return {
                    "status": "sent", "room_id": room_id,
                    "message_id": legacy_message_id, "legacy_api": True,
                }
            self._ensure_channel_member(room_id, employee_user_id)
            text = (
                f"**Experiment {run_id}: {title}**\n\n"
                f"{description}\n\nPlease reply in this thread with your response."
            )
            if inputs:
                text += "\n\nSource files:\n" + "\n".join(f"- [{item['name']}]({item['url']})" for item in inputs)
                text += "\nUse these source files for the assignment and share your deliverables in this thread."
            mention_account_id = self._viv_user_id
        return coordinator.dispatch_experiment_chat(
            idempotency_key=str(run_id), role=role, employee_id=employee_id,
            room_id=room_id, message_text=text, mention_account_id=mention_account_id,
        )

    def _ensure_channel_member(self, room_id: str, user_id: str) -> None:
        members = self._request("GET", "channels.members", params={"roomId": room_id})
        member_ids = {member.get("_id") for member in members.get("members", [])}
        if user_id not in member_ids:
            self._request("POST", "channels.invite", json={"roomId": room_id, "userId": user_id})

    def _get_message(self, message_id: str) -> dict[str, Any] | None:
        try:
            result = self._request("GET", "chat.getMessage", params={"msgId": message_id})
        except RocketChatError as error:
            cause = error.__cause__
            # Deployed Rocket.Chat 5.3 returns only {"success": false} with 400
            # for an absent ID. Authentication/permission failures use other statuses.
            if (error.code == "unsuccessful_response" and isinstance(cause, requests.HTTPError)
                    and cause.response is not None and cause.response.status_code == 400
                    and cause.response.json() == {"success": False}):
                return None
            raise
        return result.get("message")

    def thread(self, room_id: str, message_id: str) -> list[dict[str, str | None]]:
        """Return the root message and replies in timestamp order."""
        with self._lock:
            self._ensure_login()
            root = self._get_message(message_id)
            payload = self._request(
                "GET", "chat.getThreadMessages", params={"tmid": message_id}
            )
            messages = payload.get("messages") or []
            if root and all(item.get("_id") != message_id for item in messages):
                messages.append(root)
            normalized = [self._normalize_message(item) for item in messages]
            normalized.sort(key=lambda item: item["timestamp"] or "")
            return normalized

    def setup_outgoing_webhook(self, token: str | None = None) -> str:
        """Ensure the named message webhook exists without exposing its secret."""
        with self._lock:
            self._ensure_login()
            self._ensure_channel()
            token = token if token is not None else os.environ["EXPERIMENTS_WEBHOOK_TOKEN"]
            name = "TACExperimentTracking"
            endpoint = "http://192.0.2.10:8110/api/chat-events"
            listed = self._request(
                "GET",
                "integrations.list",
                params={"name": name, "type": "webhook-outgoing", "count": 100},
            )
            existing = next(
                (item for item in listed.get("integrations", []) if item.get("name") == name),
                None,
            )
            if existing:
                channels = existing.get("channel") or []
                channel_names = [item.get("name") if isinstance(item, dict) else item for item in channels]
                matches = (
                    existing.get("event") == "sendMessage"
                    and f"#{self.channel}" in channel_names
                    and existing.get("urls") == [endpoint]
                    and existing.get("scriptEnabled") is False
                    and existing.get("enabled") is True
                    and existing.get("token") == token
                )
                if not matches:
                    raise RocketChatError("integrations.list", "existing_integration_mismatch")
                return existing["_id"]
            created = self._request(
                "POST",
                "integrations.create",
                json={
                    "type": "webhook-outgoing",
                    "username": self.username,
                    "channel": f"#{self.channel}",
                    "scriptEnabled": False,
                    "name": name,
                    "enabled": True,
                    "event": "sendMessage",
                    "urls": [endpoint],
                    "token": token,
                },
            )
            integration_id = (created.get("integration") or {}).get("_id")
            if not integration_id:
                raise RocketChatError("integrations.create", "missing_integration_id")
            return integration_id

    @staticmethod
    def _normalize_message(message: dict[str, Any]) -> dict[str, str | None]:
        author = message.get("u") or {}
        timestamp = message.get("ts")
        if isinstance(timestamp, dict):
            timestamp = timestamp.get("$date")
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        elif timestamp is not None:
            timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        timestamp = timestamp.isoformat() if isinstance(timestamp, datetime) else None
        return {
            "message_id": message.get("_id"),
            "author": author.get("username") or author.get("name"),
            "text": message.get("msg", ""),
            "timestamp": timestamp,
        }
