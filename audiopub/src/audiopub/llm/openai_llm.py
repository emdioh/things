from __future__ import annotations

import json

from .base import LLMError, LLMRefusal, parse_json_loose


class OpenAICompatibleLLM:
    """Any server speaking the OpenAI Chat Completions API (OpenAI, Ollama, OpenRouter, Gemini, ...)."""

    def __init__(self, model: str, base_url: str | None, api_key: str, effort: str | None = None, label: str = "openai"):
        try:
            import openai
        except ImportError as e:
            raise SystemExit("This provider needs the openai package: pip install 'audiopub[openai]'") from e
        self.model = model
        self.name = f"{label}:{model}"
        self.effort = effort
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=5, timeout=600)
        self._json_schema_ok = True

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import openai

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = {"reasoning_effort": self.effort} if self.effort else {}
        if self._json_schema_ok:
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "result", "schema": schema, "strict": True},
                    },
                    **extra,
                )
                return self._parse(resp)
            except openai.BadRequestError:
                # Server/model doesn't support strict schemas; fall back to JSON mode.
                self._json_schema_ok = False

        messages[0]["content"] = (
            system + "\n\nReply with a single JSON object matching this JSON Schema:\n" + json.dumps(schema)
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, response_format={"type": "json_object"}, **extra
            )
        except openai.BadRequestError:
            resp = self.client.chat.completions.create(model=self.model, messages=messages, **extra)
        return self._parse(resp)

    @staticmethod
    def _parse(resp) -> dict:
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("response was truncated (length)")
        if getattr(choice.message, "refusal", None):
            raise LLMRefusal(f"model declined: {choice.message.refusal}")
        return parse_json_loose(choice.message.content or "")
