"""Pluggable text-to-speech backends.

    edge    Microsoft Edge neural voices (free, online, hundreds of voices in ~80 languages)
    kokoro  Kokoro-82M, runs locally/offline (pip install 'audiopub[kokoro]')
    openai      OpenAI speech API or any compatible server, e.g. Kokoro-FastAPI,
                openedai-speech (pip install 'audiopub[openai]')
    openrouter  speech models on OpenRouter, e.g. Mistral Voxtral TTS (pip install 'audiopub[openai]')
"""

from __future__ import annotations

from .base import TTSBackend, Voice

BACKENDS = ["edge", "kokoro", "openai", "openrouter"]


def make_tts(name: str, *, model: str | None = None, base_url: str | None = None,
             api_key: str | None = None, concurrency: int = 4) -> TTSBackend:
    if name == "edge":
        from .edge import EdgeTTS

        return EdgeTTS(concurrency=concurrency)
    if name == "kokoro":
        from .kokoro import KokoroTTS

        return KokoroTTS()
    if name == "openai":
        from .openai_tts import OpenAITTS

        return OpenAITTS(model=model, base_url=base_url, api_key=api_key, concurrency=concurrency)
    if name == "openrouter":
        from .openai_tts import OpenRouterTTS

        return OpenRouterTTS(model=model, base_url=base_url, api_key=api_key, concurrency=concurrency)
    raise ValueError(f"unknown TTS backend {name!r}; choose one of: {', '.join(BACKENDS)}")


__all__ = ["BACKENDS", "TTSBackend", "Voice", "make_tts"]
