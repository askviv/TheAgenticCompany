"""LiteLLM custom-provider bridge from upstream τ-bench to the Codex CLI."""

from __future__ import annotations

from typing import Any


class UnsupportedCodexModelParameter(ValueError):
    """A requested LiteLLM parameter cannot be honored by the CLI transport."""


def register_codex_provider(model: Any) -> None:
    """Register the adapter under the configured τ model provider prefix."""
    import litellm

    from litellm.llms.custom_llm import CustomLLM, CustomLLMError
    from litellm.types.utils import Choices, Message, ModelResponse, Usage

    class CodexProvider(CustomLLM):
        def completion(
            self,
            model: str,
            messages: list,
            api_base: str | None = None,
            custom_prompt_dict: dict | None = None,
            model_response: Any = None,
            print_verbose=None,
            encoding=None,
            api_key=None,
            logging_obj=None,
            optional_params: dict | None = None,
            **kwargs,
        ):
            optional = optional_params or {}
            unsupported = {
                name: value for name, value in optional.items()
                if value is not None and name not in {"tools", "tool_choice", "num_retries", "max_retries"}
            }
            if unsupported:
                raise UnsupportedCodexModelParameter(
                    "Codex transport does not support requested LiteLLM parameters: "
                    + ", ".join(sorted(unsupported))
                )
            tool_choice = optional.get("tool_choice")
            if tool_choice not in (None, "auto"):
                raise UnsupportedCodexModelParameter("Codex transport supports automatic tool selection only")
            tools = optional.get("tools") or []
            serialized_messages = [
                item.model_dump(mode="json", exclude_none=True)
                if hasattr(item, "model_dump") else dict(item)
                for item in messages
            ]
            try:
                reply = model_transport.complete(serialized_messages, tools=tools or None)
                if reply.get("model") not in (None, model_transport.settings.model):
                    raise ValueError("Codex transport returned a different configured model")
                tool_calls = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments_json"],
                        },
                    }
                    for call in reply.get("tool_calls", [])
                ]
                usage_data = reply.get("usage")
                usage = None
                if usage_data is not None:
                    usage = Usage(
                        prompt_tokens=usage_data.get("input_tokens"),
                        completion_tokens=usage_data.get("output_tokens"),
                        total_tokens=(
                            usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0)
                            if usage_data.get("input_tokens") is not None
                            and usage_data.get("output_tokens") is not None else None
                        ),
                    )
                choice = Choices(
                    index=0,
                    finish_reason="tool_calls" if tool_calls else "stop",
                    message=Message(
                        role="assistant",
                        content=reply.get("content"),
                        tool_calls=tool_calls or None,
                    ),
                )
                return ModelResponse(
                    id="codex-cli-turn",
                    model=model_transport.settings.model,
                    choices=[choice],
                    usage=usage,
                )
            except UnsupportedCodexModelParameter:
                raise
            except Exception as exc:
                # LiteLLM may log the exception itself. Keep the safe type and
                # cause; never include the prompt, tool inputs, CLI output, or
                # raw provider diagnostics in this message.
                raise CustomLLMError(502, f"Codex transport failed ({type(exc).__name__})") from exc

    # ``model`` is the shared CodexCLIModel. Keep one handler per process so
    # repeated app factories do not create ambiguous provider registrations.
    model_transport = model
    provider_name = "tac_codex"
    litellm.custom_provider_map = [
        item for item in litellm.custom_provider_map if item.get("provider") != provider_name
    ] + [{"provider": provider_name, "custom_handler": CodexProvider()}]
    if provider_name not in litellm.provider_list:
        litellm.provider_list.append(provider_name)
    if provider_name not in litellm._custom_providers:
        litellm._custom_providers.append(provider_name)
