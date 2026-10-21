from __future__ import annotations

import anthropic

from .base import LLMError, LLMRefusal, parse_json_loose


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, effort: str | None = None):
        self.model = model
        self.name = f"anthropic:{model}"
        self.effort = effort or "medium"
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=5)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        kwargs: dict = dict(
            model=self.model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            cache_control={"type": "ephemeral"},
        )
        if _supports_server_fallback(self.model):
            # If the primary model declines, the API re-runs the request on
            # Anthropic's recommended fallback model instead of refusing.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            stream_ctx = self.client.beta.messages.stream(**kwargs)
        else:
            stream_ctx = self.client.messages.stream(**kwargs)

        with stream_ctx as stream:
            response = stream.get_final_message()

        if response.stop_reason == "refusal":
            raise LLMRefusal("model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response was truncated (max_tokens)")
        text = "".join(b.text for b in response.content if b.type == "text")
        return parse_json_loose(text)


def _supports_server_fallback(model: str) -> bool:
    return model in {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
