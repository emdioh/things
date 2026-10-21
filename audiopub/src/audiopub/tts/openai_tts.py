from __future__ import annotations

import os
from pathlib import Path

from .base import TTSBackend, Voice

# Voices of OpenAI's speech models. The API doesn't report gender; these are
# approximate perceived genders, used only to pick a plausible voice.
_OPENAI_VOICES = {
    "alloy": "unknown", "ash": "male", "ballad": "male", "coral": "female", "echo": "male",
    "fable": "male", "nova": "female", "onyx": "male", "sage": "female", "shimmer": "female",
    "verse": "male", "marin": "female", "cedar": "male",
}


class OpenAITTS(TTSBackend):
    """OpenAI /v1/audio/speech, or any server implementing it.

    For a self-hosted server whose voices differ from OpenAI's, list them in
    AUDIOPUB_TTS_VOICES as ``id:gender`` pairs, e.g. ``af_bella:female,am_adam:male``.
    """

    name = "openai"
    extension = "mp3"

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 concurrency: int = 4):
        try:
            import openai
        except ImportError as e:
            raise SystemExit("The openai TTS backend needs: pip install 'audiopub[openai]'") from e
        self.model = model or "gpt-4o-mini-tts"
        self.concurrency = concurrency
        self.client = openai.OpenAI(
            base_url=base_url, api_key=api_key or os.environ.get("OPENAI_API_KEY") or "not-needed", max_retries=5
        )

    def voices(self, language: str) -> list[Voice]:
        custom = os.environ.get("AUDIOPUB_TTS_VOICES")
        if custom:
            pairs = [p.split(":", 1) + ["unknown"] for p in custom.split(",") if p.strip()]
            return [Voice(p[0].strip(), p[1].strip(), "*") for p in pairs]
        return [Voice(v, g, "*") for v, g in _OPENAI_VOICES.items()]

    def render(self, text: str, voice: str, speed: float, out: Path) -> None:
        with self.client.audio.speech.with_streaming_response.create(
            model=self.model, voice=voice, input=text, speed=speed, response_format="mp3"
        ) as resp:
            resp.stream_to_file(out)
