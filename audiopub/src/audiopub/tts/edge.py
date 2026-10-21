from __future__ import annotations

import asyncio
from pathlib import Path

from .base import TTSBackend, Voice


class EdgeTTS(TTSBackend):
    name = "edge"
    extension = "mp3"

    def __init__(self, concurrency: int = 4):
        import edge_tts  # noqa: F401  (fail early if missing)

        self.concurrency = concurrency
        self._catalog: list[dict] | None = None

    def _all(self) -> list[dict]:
        if self._catalog is None:
            import edge_tts

            self._catalog = asyncio.run(edge_tts.list_voices())
        return self._catalog

    def voices(self, language: str) -> list[Voice]:
        lang = language.lower().split("-")[0]
        native, multi = [], []
        for v in self._all():
            gender = (v.get("Gender") or "unknown").lower()
            voice = Voice(v["ShortName"], gender if gender in ("male", "female") else "unknown", v["Locale"])
            if voice.matches_language(lang):
                native.append(voice)
            elif "Multilingual" in v["ShortName"]:
                multi.append(Voice(voice.id, voice.gender, "*"))
        # Prefer the book's main regional variant (it-IT over it-CH, en-US over en-IN, ...).
        main = {"en": "en-US", "pt": "pt-BR", "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "zh": "zh-CN"}.get(
            lang, f"{lang}-{lang.upper()}"
        )
        native.sort(key=lambda v: (v.locale != main, "Multilingual" not in v.id, v.id))
        return native + multi

    def render(self, text: str, voice: str, speed: float, out: Path) -> None:
        import edge_tts

        rate = f"{round((speed - 1) * 100):+d}%"
        asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))
