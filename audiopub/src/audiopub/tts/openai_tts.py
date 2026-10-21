from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import TTSBackend, Voice

# Voices of OpenAI's speech models. The API doesn't report gender; these are
# approximate perceived genders, used only to pick a plausible voice.
_OPENAI_VOICES = {
    "alloy": "unknown", "ash": "male", "ballad": "male", "coral": "female", "echo": "male",
    "fable": "male", "nova": "female", "onyx": "male", "sage": "female", "shimmer": "female",
    "verse": "male", "marin": "female", "cedar": "male",
}


def custom_voices() -> list[Voice] | None:
    """Voices listed in AUDIOPUB_TTS_VOICES as ``id:gender[:locale]``, comma separated.

    E.g. ``voice_3150:male:it-IT,voice_478:female:it-IT,af_bella:female``.
    A voice without a locale is treated as multilingual.
    """
    spec = os.environ.get("AUDIOPUB_TTS_VOICES", "").strip()
    if not spec:
        return None
    out = []
    for item in spec.split(","):
        parts = [p.strip() for p in item.split(":")]
        if not parts[0]:
            continue
        gender = parts[1] if len(parts) > 1 and parts[1] in ("male", "female") else "unknown"
        locale = parts[2] if len(parts) > 2 and parts[2] else "*"
        out.append(Voice(parts[0], gender, locale))
    return out


class OpenAITTS(TTSBackend):
    """OpenAI /v1/audio/speech, or any server implementing it.

    For a server whose voices differ from OpenAI's, list them in
    AUDIOPUB_TTS_VOICES (see :func:`custom_voices`).
    """

    name = "openai"
    extension = "mp3"
    default_model = "gpt-4o-mini-tts"
    default_base_url: str | None = None
    key_env = "OPENAI_API_KEY"
    send_speed = True  # False: the API has no speed option; change the tempo afterwards

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 concurrency: int = 4):
        try:
            import openai
        except ImportError as e:
            raise SystemExit(f"The {self.name} TTS backend needs: pip install 'audiopub[openai]'") from e
        self.model = model or self.default_model
        self.concurrency = concurrency
        self.client = openai.OpenAI(
            base_url=base_url or self.default_base_url,
            api_key=api_key or os.environ.get(self.key_env) or "not-needed",
            max_retries=5,
        )

    def catalog(self) -> list[Voice]:
        return [Voice(v, g, "*") for v, g in _OPENAI_VOICES.items()]

    def voices(self, language: str) -> list[Voice]:
        voices = custom_voices() or self.catalog()
        # Voices in the book's language first, then the rest.
        return sorted(voices, key=lambda v: not v.matches_language(language))

    def render(self, text: str, voice: str, speed: float, out: Path) -> None:
        extra = {"speed": speed} if self.send_speed else {}
        with self.client.audio.speech.with_streaming_response.create(
            model=self.model, voice=voice, input=text, response_format="mp3", **extra
        ) as resp:
            resp.stream_to_file(out)
        if not self.send_speed and abs(speed - 1.0) > 1e-3:
            change_tempo(out, speed)


def change_tempo(path: Path, speed: float) -> None:
    tmp = path.with_name(path.stem + ".tempo" + path.suffix)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-filter:a", f"atempo={speed:.4f}", str(tmp)],
                   check=True)
    tmp.replace(path)


# Mistral Voxtral TTS on OpenRouter. The named voices are documented with their
# language; the numbered ones are not (listen with `audiopub voices --sample`
# and label them in AUDIOPUB_TTS_VOICES).
_VOXTRAL_NAMED = [
    Voice("en_paul_neutral", "male", "en-US"),
    Voice("gb_oliver_neutral", "male", "en-GB"),
    Voice("gb_jane_neutral", "female", "en-GB"),
    Voice("fr_marie_neutral", "female", "fr-FR"),
]
_VOXTRAL_NUMBERED = """
voice_3150 voice_478 voice_447 voice_485 voice_3149 voice_3147 voice_591 voice_590 voice_1788
voice_1787 voice_1786 voice_479 voice_3143 voice_481 voice_483 voice_592 voice_3137 voice_589
voice_3141 voice_3153 voice_3139 voice_3146 voice_482 voice_3133 voice_3136 voice_3135 voice_3145
voice_3134 voice_3154 voice_480 voice_3155 voice_3132 voice_3138 voice_3140 voice_3144 voice_3142
""".split()


class OpenRouterTTS(OpenAITTS):
    """Speech models on OpenRouter (Mistral Voxtral TTS)."""

    name = "openrouter"
    default_model = "mistralai/voxtral-mini-tts-3-0"
    default_base_url = "https://openrouter.ai/api/v1"
    key_env = "OPENROUTER_API_KEY"
    send_speed = False

    def catalog(self) -> list[Voice]:
        numbered = [] if "2603" in self.model else [Voice(v, "unknown", "*") for v in _VOXTRAL_NUMBERED]
        return _VOXTRAL_NAMED + numbered
