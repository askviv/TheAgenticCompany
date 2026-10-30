"""Codex CLI model transport for upstream actor runtimes, not an actor policy.

The upstream runtime owns its prompts, history and tools. Codex returns proposed
tool calls as structured data; it must never execute those tools itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CodexModelError(RuntimeError):
    """A failed inference transport; never replace this with a canned response."""


class CodexConfigurationError(CodexModelError):
    """The CLI rejected its configuration before inference."""


class CodexAuthenticationError(CodexModelError):
    """The configured account could not authenticate."""


class CodexModelAccessError(CodexModelError):
    """The configured account could not access the requested model."""


class CodexRateLimitError(CodexModelError):
    """The provider reported quota or rate limiting."""


class CodexOutputSchemaError(CodexModelError):
    """The CLI/provider rejected the structured output schema."""


def _failure_type(stderr: str) -> type[CodexModelError]:
    # Report a safe operational category, never raw CLI/provider text. Workers
    # retain these exception classes in their private cause-chain diagnostics.
    diagnostic = stderr.casefold()
    if "error loading config" in diagnostic or "unknown configuration field" in diagnostic:
        return CodexConfigurationError
    if any(term in diagnostic for term in ("unauthorized", "authentication", "not logged in", "401")):
        return CodexAuthenticationError
    if any(term in diagnostic for term in ("rate limit", "quota exceeded", "429")):
        return CodexRateLimitError
    if "schema" in diagnostic:
        return CodexOutputSchemaError
    if "model" in diagnostic and any(term in diagnostic for term in ("not available", "not found", "access", "not supported")):
        return CodexModelAccessError
    return CodexModelError


class CodexCLISettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    executable: str = "codex"
    model: str = Field(min_length=1)
    codex_home: Path
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    timeout_seconds: float = Field(default=180, gt=0)


class ProposedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str

    @field_validator("arguments_json")
    @classmethod
    def object_arguments(cls, value):
        if not isinstance(json.loads(value), dict):
            raise ValueError("tool arguments must encode a JSON object")
        return value


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    content: str | None
    tool_calls: list[ProposedToolCall]


# Supported by the installed CLI (0.165.0). Ignore user config while retaining
# its existing ChatGPT authentication; do not import its apps/plugins/hooks.
# See https://developers.openai.com/codex/config-reference and
# https://developers.openai.com/codex/noninteractive .
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "remote_plugin",
    "hooks", "multi_agent", "browser_use", "computer_use", "image_generation",
    "view_image", "code_mode", "code_mode_host", "workspace_dependencies",
    "memories", "goals", "unbounded_connection_retries",
)


class CodexCLIModel:
    def __init__(self, settings: CodexCLISettings):
        self.settings = settings

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, on_submission_started=None,
    ) -> dict:
        """Make one CLI invocation; failures/uncertain outcomes are not retried."""
        settings = self.settings
        tools = tools or []
        prompt = (
            "Act only as the inference provider for the supplied conversation. "
            "Follow its system/developer instructions and reply to its latest turn. "
            "The supplied tools belong to the calling runtime: return tool calls as data, "
            "never execute commands, inspect files, or contact services. "
            "Return content and tool_calls matching the output schema.\n"
            + json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
        )
        with tempfile.TemporaryDirectory(prefix="tac-model-") as directory:
            root = Path(directory)
            schema = root / "response-schema.json"
            output = root / "response.json"
            schema.write_text(json.dumps(ModelReply.model_json_schema()), encoding="utf-8")
            schema.chmod(0o600)
            command = [settings.executable, "--ask-for-approval", "never", "exec",
                "--ignore-user-config", "--ignore-rules", "--strict-config",
                "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--model", settings.model, "--cd", str(root), "--json",
                "--output-schema", str(schema), "--output-last-message", str(output)]
            for feature in DISABLED_FEATURES:
                command.extend(["--disable", feature])
            for override in (
                'web_search="disabled"', "project_doc_max_bytes=0",
                'shell_environment_policy.inherit="none"',
                f'model_reasoning_effort="{settings.reasoning_effort}"',
            ):
                command.extend(["--config", override])
            command.append("-")
            environment = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(root), "CODEX_HOME": str(settings.codex_home),
                "NO_COLOR": "1", "LANG": "C.UTF-8",
            }
            try:
                # The write-ahead caller flips its model-turn phase only after
                # all framework-native messages/tools are serialized and just
                # before the external process can begin.
                if on_submission_started is not None:
                    on_submission_started()
                completed = subprocess.run(command, input=prompt, text=True,
                    capture_output=True, cwd=root, env=environment,
                    timeout=settings.timeout_seconds, check=False)
            except subprocess.TimeoutExpired as error:
                cause = subprocess.TimeoutExpired([settings.executable, "exec"], error.timeout)
                raise CodexModelError("Codex CLI inference timed out without a confirmed result") from cause
            except OSError as error:
                raise CodexModelError("Codex CLI inference failed before a confirmed result") from error
            if completed.returncode:
                # Do not attach stdout/stderr: either can include private prompts
                # or account/provider diagnostics. Preserve the safe exit cause.
                cause = subprocess.CalledProcessError(completed.returncode, [settings.executable, "exec"])
                failure_type = _failure_type(completed.stderr)
                raise failure_type(f"Codex CLI exited with status {completed.returncode}") from cause
            try:
                events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
                if any(event.get("type") in {"turn.failed", "error"} for event in events):
                    raise ValueError("Codex CLI reported an unsuccessful turn")
                if any(event.get("item", {}).get("type") in {
                    "command_execution", "file_change", "mcp_tool_call", "web_search",
                } for event in events):
                    raise ValueError("Codex CLI attempted a native tool instead of returning runtime data")
                reply = ModelReply.model_validate_json(output.read_text(encoding="utf-8"))
                names = {tool["function"]["name"] for tool in tools}
                if any(call.name not in names for call in reply.tool_calls):
                    raise ValueError("model proposed an unregistered runtime tool")
                if not reply.tool_calls and not reply.content:
                    raise ValueError("model returned neither text nor runtime tool calls")
                terminal = next((event for event in reversed(events)
                                 if event.get("type") == "turn.completed"), None)
                if terminal is None:
                    raise ValueError("Codex CLI did not confirm turn completion")
                usage = terminal.get("usage")
                if usage is not None:
                    usage = {key: usage[key] for key in ("input_tokens", "output_tokens")}
                    if any(type(value) is not int or value < 0 for value in usage.values()):
                        raise ValueError("Codex CLI returned invalid token counts")
                return {**reply.model_dump(), "usage": usage, "model": settings.model,
                        "reasoning_effort": settings.reasoning_effort}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise CodexModelError("Codex CLI did not return a valid structured runtime response") from error
