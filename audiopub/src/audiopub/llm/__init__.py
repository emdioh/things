"""Pluggable LLM providers used for speaker attribution.

A provider is selected with a spec string ``provider:model``, e.g.

    anthropic:claude-opus-5-5
    openai:gpt-5.1
    ollama:qwen3:14b
    openrouter:meta-llama/llama-4-maverick
    gemini:gemini-2.5-pro
    lmstudio:local-model

Any other OpenAI-compatible server works with ``openai:<model>`` plus
``--llm-base-url``.
"""

from __future__ import annotations

import os

from .base import LLMClient, LLMError, LLMRefusal

DEFAULT_LLM = "anthropic:claude-opus-5-5"

# provider -> (base_url, api key env var, default model)
OPENAI_COMPATIBLE = {
    "openai": (None, "OPENAI_API_KEY", "gpt-5.1"),
    "ollama": ("http://localhost:11434/v1", None, "qwen3:14b"),
    "lmstudio": ("http://localhost:1234/v1", None, "local-model"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "anthropic/claude-opus-5-5"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-2.5-pro"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-large-latest"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
}

PROVIDERS = ["anthropic", *OPENAI_COMPATIBLE]


def parse_spec(spec: str) -> tuple[str, str | None]:
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown LLM provider {provider!r}; choose one of: {', '.join(PROVIDERS)}")
    return provider, (model.strip() or None)


def make_llm(
    spec: str = DEFAULT_LLM,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    effort: str | None = None,
) -> LLMClient:
    provider, model = parse_spec(spec)
    if provider == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM(model=model or "claude-opus-5-5", api_key=api_key, base_url=base_url, effort=effort)

    from .openai_llm import OpenAICompatibleLLM

    default_url, key_env, default_model = OPENAI_COMPATIBLE[provider]
    key = api_key or (os.environ.get(key_env) if key_env else None) or "not-needed"
    return OpenAICompatibleLLM(
        model=model or default_model,
        base_url=base_url or default_url,
        api_key=key,
        effort=effort,
        label=provider,
    )


__all__ = ["DEFAULT_LLM", "LLMClient", "LLMError", "LLMRefusal", "PROVIDERS", "make_llm", "parse_spec"]
