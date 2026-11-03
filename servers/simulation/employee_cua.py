"""Native CUA agent access to one managed employee Selkies desktop."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .clock_rate import kernel_virtual_time_rate


DRIVER_PATH = "/opt/employee-cua/cua-driver"
MANIFEST_PATH = "/opt/employee-cua/capabilities.yaml"
DISPLAY = ":20"
TYPE_TEXT_DELAY_MS = 150

NATIVE_TOOLS = frozenset({
    "list_apps", "list_windows", "get_window_state", "get_desktop_state",
    "get_screen_size", "get_cursor_position", "move_cursor", "click",
    "double_click", "right_click", "drag", "scroll", "type_text",
    "press_key", "hotkey", "set_agent_cursor_enabled", "get_agent_cursor_state",
})
OBSERVATION_TOOLS = frozenset({"get_window_state", "get_desktop_state"})
INPUT_TOOLS = frozenset({
    "move_cursor", "click", "double_click", "right_click", "drag", "scroll",
    "type_text", "press_key", "hotkey",
})


class EmployeeCuaError(RuntimeError):
    """A CUA operation could not produce a confirmed result."""


class EmployeeCuaAgent:
    """Expose the employee's native X11 desktop to the configured Codex model.

    The caller owns `employee_desktops.employee_activity(employee_id)` and its
    action lock around this session. This class never reacquires that lock.
    """

    def __init__(
        self,
        desktop_manager: Any,
        model_factory: Callable[[], Any] | None = None,
        *,
        docker_binary: str = "docker",
    ):
        self.desktop_manager = desktop_manager
        self.model_factory = model_factory or _default_model_factory
        self.docker_binary = docker_binary

    @contextmanager
    def session(
        self,
        employee_id: str,
        credentials: Mapping[str, str] | None = None,
    ) -> Iterator[EmployeeCuaAction]:
        self.desktop_manager.prepare_cua_runtime(employee_id)
        container = self.desktop_manager._container_name(employee_id)
        desktop_env = _employee_desktop_environment(self.desktop_manager, container)
        params = StdioServerParameters(
            command=self.docker_binary,
            args=_docker_exec_args(container, desktop_env),
            cwd=None,
            env=None,
        )
        primary_error: BaseException | None = None
        with start_blocking_portal() as portal, ExitStack() as stack:
            try:
                read_stream, write_stream = stack.enter_context(
                    portal.wrap_async_context_manager(stdio_client(params))
                )
                client = ClientSession(read_stream, write_stream)
                stack.enter_context(portal.wrap_async_context_manager(client))
                portal.call(client.initialize)
                catalog = portal.call(client.list_tools).tools
                tools = _codex_tools(catalog)
                _require_native_observation(tools)
                type_text_definition = next(
                    (tool for tool in tools if tool["function"]["name"] == "type_text"),
                    None,
                )
                if type_text_definition is None:
                    raise EmployeeCuaError("Employee CUA catalog is missing native type_text")
                yield EmployeeCuaAction(
                    employee_id=employee_id,
                    model=self.model_factory(),
                    client=client,
                    portal=portal,
                    tools=tools,
                    credential_tool=_type_credential_tool(type_text_definition),
                    credentials=credentials or {},
                )
            except BaseException as error:
                primary_error = error
                raise
            finally:
                # ExitStack closes SDK resources on the portal's owner task.
                # A cleanup failure must not hide the original operation error.
                if primary_error is not None:
                    try:
                        stack.close()
                    except BaseException as cleanup_error:
                        primary_error.add_note(
                            f"CUA MCP cleanup failed: {type(cleanup_error).__name__}"
                        )


class EmployeeCuaAction:
    def __init__(
        self,
        *,
        employee_id: str,
        model: Any,
        client: ClientSession,
        portal: BlockingPortal,
        tools: list[dict[str, Any]],
        credential_tool: dict[str, Any],
        credentials: Mapping[str, str],
    ):
        self.employee_id = employee_id
        self.model = model
        self.client = client
        self.portal = portal
        self.tools = [*tools, credential_tool, _finish_action_tool()]
        self.native_tools = {tool["function"]["name"] for tool in tools}
        self.credentials = {
            name: value for name, value in credentials.items()
            if name in {"username", "password"} and isinstance(value, str)
        }

    def run(
        self,
        instruction: str,
        on_submission_started: Callable[[], None] | None = None,
    ) -> str:
        """Run native desktop tool calls until the model calls finish_action.

        With a durable submission callback, the first model phase prepares the
        message. The callback runs after the model explicitly finishes that
        phase and before the second phase is allowed to submit it in the UI.
        Any failure after that callback propagates without an automatic retry.
        """
        safe_instruction = self._redact(instruction)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(submission=on_submission_started is not None)},
            {"role": "user", "content": safe_instruction},
        ]
        phase = "prepare" if on_submission_started is not None else "act"
        while True:
            response = self.model.complete(messages, tools=self.tools)
            calls = response.get("tool_calls", [])
            if not calls:
                raise EmployeeCuaError("Codex ended the desktop action without finish_action")
            content = response.get("content")
            assistant_calls = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments_json"]},
                }
                for call in calls
            ]
            assistant_message: dict[str, Any] = {"role": "assistant", "tool_calls": assistant_calls}
            if content:
                assistant_message["content"] = self._redact(str(content))
            messages.append(assistant_message)

            for index, call in enumerate(calls):
                name = call["name"]
                arguments = _object_arguments(call.get("arguments_json", "{}"))
                if name == "finish_action":
                    if index != len(calls) - 1:
                        raise EmployeeCuaError("finish_action must be the final tool call in a model response")
                    summary = self._redact(str(arguments.get("summary", content or "")))
                    if phase == "prepare":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": name,
                            "content": summary or "Message prepared for submission.",
                        })
                        on_submission_started()
                        self._submission_started = True
                        messages.append({
                            "role": "developer",
                            "content": (
                                "The durable submission boundary has now been crossed. Use the prepared UI state "
                                "to perform the requested native Send or Submit action exactly once, then verify "
                                "the resulting UI state and call finish_action. Do not retry or submit again if "
                                "the outcome is uncertain."
                            ),
                        })
                        phase = "submit"
                        break
                    return summary

                result, images = self._call_tool(name, arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": self._redact(result),
                })
                if images:
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Fresh native CUA screenshot from the preceding observation."},
                            *images,
                        ],
                    })
            else:
                continue

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if name == "type_credential":
            credential_name = arguments.get("credential")
            value = self.credentials.get(credential_name) if isinstance(credential_name, str) else None
            if value is None:
                raise EmployeeCuaError("The requested local employee credential is unavailable")
            native_args = {
                key: value for key, value in arguments.items()
                if key not in {"credential"}
            }
            native_args["text"] = value
            result = self._native_type_text(native_args)
            if self._raise_or_report_native_error("type_text", result):
                return "Credential entry was refused by the native UI. Refresh the current field and retry.", []
            return "Credential entered into the requested native field; value withheld.", []

        if name not in self.native_tools or name not in NATIVE_TOOLS:
            raise EmployeeCuaError(f"CUA tool is outside the employee desktop catalog: {name}")
        result = self._native_call(name, arguments)
        images: list[dict[str, Any]] = []
        seen_images: set[str] = set()
        text: list[str] = []
        for block in getattr(result, "content", []):
            if getattr(block, "type", None) == "image":
                data = getattr(block, "data", None)
                mime = getattr(block, "mimeType", None)
                if isinstance(data, str) and isinstance(mime, str):
                    digest = hashlib.sha256((mime + data).encode("ascii")).hexdigest()
                    if digest in seen_images:
                        continue
                    seen_images.add(digest)
                    images.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    })
            elif isinstance(getattr(block, "text", None), str):
                text.append(block.text)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            text.append(json.dumps(structured, ensure_ascii=False, default=str))
        recoverable = self._raise_or_report_native_error(name, result, "\n".join(text))
        if recoverable:
            text.insert(0, "Recoverable native UI refusal. Refresh the relevant window or desktop state, then act on the current UI.")
        if name in OBSERVATION_TOOLS and not text and not images:
            raise EmployeeCuaError(f"CUA {name} returned neither native state nor screenshot content")
        return self._redact("\n".join(text)), images

    @property
    def submission_started(self) -> bool:
        return getattr(self, "_submission_started", False)

    def _native_call(self, name: str, arguments: dict[str, Any]) -> Any:
        definition = next(
            (tool["function"] for tool in self.tools if tool["function"]["name"] == name),
            None,
        )
        if definition is None:
            raise EmployeeCuaError(f"Native CUA tool is not available: {name}")
        properties = definition["parameters"].get("properties", {})
        call_arguments = dict(arguments)
        if name in INPUT_TOOLS:
            delivery_field = next((field for field in ("delivery_mode", "delivery") if field in properties), None)
            if delivery_field:
                call_arguments[delivery_field] = "foreground"
        if name in {"move_cursor", "click", "double_click", "right_click", "drag", "scroll"}:
            if "target" in properties and "element_token" not in call_arguments:
                call_arguments["target"] = {"kind": "desktop", "display_id": "primary"}
        if name in OBSERVATION_TOOLS:
            if "include_screenshot" in properties:
                call_arguments["include_screenshot"] = True
            if "include_accessibility_tree" in properties:
                call_arguments["include_accessibility_tree"] = True
        if name == "type_text" and "delay_ms" in properties:
            call_arguments["delay_ms"] = TYPE_TEXT_DELAY_MS
        if name == "type_text":
            return self._native_type_text(call_arguments)
        return self.portal.call(self.client.call_tool, name, call_arguments)

    def _native_type_text(self, arguments: dict[str, Any]) -> Any:
        text = arguments.get("text")
        if not isinstance(text, str):
            raise EmployeeCuaError("Native type_text requires a text string")
        properties = next(
            tool["function"]["parameters"].get("properties", {})
            for tool in self.tools
            if tool["function"]["name"] == "type_text"
        )
        rate = kernel_virtual_time_rate()
        if "delay_ms" in properties:
            arguments["delay_ms"] = TYPE_TEXT_DELAY_MS * rate
            return self.portal.call(self.client.call_tool, "type_text", arguments)

        result = None
        for index, character in enumerate(text):
            char_arguments = {**arguments, "text": character}
            if index:
                # The first native call establishes focus. Re-clicking a pixel
                # or re-addressing an AX field can reset the caret between keys.
                for field in ("element_token", "x", "y", "coordinate_frame"):
                    char_arguments.pop(field, None)
                if "scope" in properties:
                    char_arguments["scope"] = "desktop"
                    char_arguments.pop("pid", None)
                    char_arguments.pop("window_id", None)
            result = self.portal.call(self.client.call_tool, "type_text", char_arguments)
            if getattr(result, "isError", False):
                return result
            time.sleep(TYPE_TEXT_DELAY_MS / 1000 * rate)
        return result

    def _raise_or_report_native_error(
        self,
        name: str,
        result: Any,
        text: str = "",
    ) -> bool:
        if not getattr(result, "isError", False):
            return False
        if self.submission_started:
            state = " after submission began"
            raise EmployeeCuaError(f"CUA tool {name} failed{state}; outcome may be uncertain")
        details = text
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            details = json.dumps(structured, ensure_ascii=False, default=str)
        if not details:
            details = " ".join(
                block.text for block in getattr(result, "content", [])
                if isinstance(getattr(block, "text", None), str)
            )
        details = self._redact(details)
        if _recoverable_native_refusal(details):
            return True
        raise EmployeeCuaError(f"CUA tool {name} failed: {details or 'native driver returned an error'}")

    def _redact(self, value: str) -> str:
        for secret in (self.credentials.get("password"),):
            if secret:
                value = value.replace(secret, "[credential redacted]")
        return value


def _employee_desktop_environment(desktop_manager: Any, container: str) -> dict[str, str]:
    # Read only display-session variables from the already-running Chrome
    # process. Never return the full process environment or expose it to Codex.
    script = r"""
set -eu
pid="$(pgrep -f 'remote-debugging-port=9222' | head -n 1)"
test -n "$pid"
tr '\000' '\n' < "/proc/$pid/environ" |
  awk -F= '$1 ~ /^(XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|AT_SPI_BUS_ADDRESS|XDG_RUNTIME_DIR|XDG_CURRENT_DESKTOP|XDG_SESSION_TYPE)$/ { print }'
if ! tr '\000' '\n' < "/proc/$pid/environ" | grep -q '^XDG_RUNTIME_DIR='; then
  for runtime in /run/user/1000 /tmp/runtime-ubuntu; do
    if [ -d "$runtime" ]; then
      printf 'XDG_RUNTIME_DIR=%s\n' "$runtime"
      break
    fi
  done
fi
if ! tr '\000' '\n' < "/proc/$pid/environ" | grep -q '^DBUS_SESSION_BUS_ADDRESS='; then
  runtime="$(tr '\000' '\n' < "/proc/$pid/environ" | sed -n 's/^XDG_RUNTIME_DIR=//p' | head -n 1)"
  if [ -z "$runtime" ]; then
    for candidate in /run/user/1000 /tmp/runtime-ubuntu; do
      if [ -d "$candidate" ]; then runtime="$candidate"; break; fi
    done
  fi
  if [ -n "$runtime" ] && [ -S "$runtime/bus" ]; then
    printf 'DBUS_SESSION_BUS_ADDRESS=unix:path=%s/bus\n' "$runtime"
  fi
fi
"""
    result = desktop_manager._docker(
        "exec", "--user", "ubuntu", container, "/bin/sh", "-c", script,
    )
    environment: dict[str, str] = {"DISPLAY": DISPLAY, "HOME": "/home/ubuntu", "XDG_SESSION_TYPE": "x11"}
    allowed = {
        "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS",
        "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
    }
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in allowed:
            if "\x00" in value or "\n" in value or "\r" in value:
                raise EmployeeCuaError(f"Employee desktop returned an invalid {name} value")
            environment[name] = value
    return environment


def _docker_exec_args(container: str, desktop_env: Mapping[str, str]) -> list[str]:
    args = [
        "exec", "--interactive", "--user", "ubuntu",
    ]
    environment = {
        **desktop_env,
        "CUA_DRIVER_PERMISSION_MODE": "bounded",
        "CUA_DRIVER_CAPABILITY_MANIFEST_FILE": MANIFEST_PATH,
        "CUA_DRIVER_CAPABILITY_MANIFEST_APPROVED": "1",
    }
    for name, value in environment.items():
        if "\x00" in value or "\n" in value or "\r" in value:
            raise EmployeeCuaError(f"Employee desktop returned an invalid {name} value")
        args.extend(("--env", f"{name}={value}"))
    args.extend((container, DRIVER_PATH, "mcp"))
    return args


def _codex_tools(definitions: list[Any]) -> list[dict[str, Any]]:
    available: dict[str, Any] = {}
    for definition in definitions:
        name = getattr(definition, "name", None)
        schema = getattr(definition, "inputSchema", None)
        description = getattr(definition, "description", None)
        if name in NATIVE_TOOLS and isinstance(schema, dict):
            available[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description or f"Native CUA desktop action: {name}",
                    "parameters": schema,
                },
            }
    return [available[name] for name in sorted(available)]


def _require_native_observation(tools: list[dict[str, Any]]) -> None:
    names = {tool["function"]["name"] for tool in tools}
    required = {"list_windows", "get_window_state", "get_desktop_state", "click", "type_text"}
    missing = sorted(required - names)
    if missing:
        raise EmployeeCuaError(f"Employee CUA catalog is missing required native tools: {', '.join(missing)}")


def _type_credential_tool(native_definition: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(native_definition["function"]["parameters"])
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or "text" not in properties:
        raise EmployeeCuaError("Native type_text schema cannot support local credential handles")
    properties.pop("text")
    properties["credential"] = {
        "type": "string",
        "enum": ["username", "password"],
        "description": "Which configured local employee credential to type.",
    }
    required = schema.get("required", [])
    schema["required"] = [item for item in required if item != "text"] + ["credential"]
    schema["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": "type_credential",
            "description": (
                "Type a configured employee credential into a currently observed native field. "
                "The credential value is kept local and is never returned to the model."
            ),
            "parameters": schema,
        },
    }


def _finish_action_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "finish_action",
            "description": "Finish the current employee desktop action after verifying its UI outcome.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def _object_arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise EmployeeCuaError("Codex returned non-text desktop tool arguments")
    try:
        arguments = json.loads(value)
    except json.JSONDecodeError as error:
        raise EmployeeCuaError("Codex returned invalid desktop tool arguments") from error
    if not isinstance(arguments, dict):
        raise EmployeeCuaError("Codex desktop tool arguments must be a JSON object")
    return arguments


def _recoverable_native_refusal(details: str) -> bool:
    """Only structured native UI refusals are recoverable before submission."""
    try:
        value: Any = json.loads(details)
    except (TypeError, json.JSONDecodeError):
        value = None
    codes = {
        "stale_element_token", "stale_snapshot", "background_unavailable",
        "element_not_found", "window_not_found", "target_not_found",
        "element_not_interactable", "ambiguous_target",
    }

    def inspect(item: Any) -> bool:
        if isinstance(item, dict):
            code = item.get("code")
            if isinstance(code, str) and code in codes:
                return True
            if item.get("effect") == "refused":
                return True
            return any(inspect(child) for child in item.values())
        if isinstance(item, list):
            return any(inspect(child) for child in item)
        return False

    return inspect(value)


def _system_prompt(*, submission: bool) -> str:
    boundary = (
        "Prepare the requested message in the native UI, but do not click Send or Submit. "
        "Call finish_action only when the message is prepared for submission."
        if submission else
        "Perform the requested action in the native UI and call finish_action after verifying its outcome."
    )
    return (
        "You control one employee's real Selkies X11 desktop using the supplied native CUA MCP tools. "
        "Use fresh get_window_state observations with the accessibility tree and screenshot before acting. "
        "For mouse actions, use screen coordinates from the screenshot with the desktop target so the real "
        "pointer visibly moves; use foreground delivery. Use type_text for ordinary text, never clipboard tools. "
        "For configured account credentials, call type_credential with username or password; credential "
        "values are local and are never in this prompt. Do not infer or request those values. "
        "Credential-entry tool results withhold screenshots; later fresh screenshots remain available, and "
        "text observations redact configured credential values. Do not repeat credential values in text. "
        "Do not create or use browser APIs, CDP, Playwright, shell, "
        "filesystem tools, or external message APIs. Do not claim success without observing the resulting UI. "
        + boundary
    )


def _default_model_factory() -> Any:
    from pydantic import SecretStr

    from .codex_remote import CodexRemoteModel, CodexRemoteSettings
    from .runtime import RuntimeConfig, private_json, private_secret

    config_path = Path(os.environ.get(
        "SIMULATION_CONFIG", str(RuntimeConfig.model_fields["issue_config_path"].default)
    ))
    config = RuntimeConfig.model_validate(private_json(config_path))
    url = config.employee_cua_inference_url
    token_file = config.employee_cua_inference_token_file
    model_name = config.employee_cua_model
    token = private_secret(token_file)
    return CodexRemoteModel(CodexRemoteSettings(
        url=url,
        bearer_token=SecretStr(token),
        model=model_name,
        reasoning_effort="low",
    ))
