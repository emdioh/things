from __future__ import annotations

import wave
from pathlib import Path

from .base import TTSBackend, Voice

# Kokoro voice ids: first letter = language, second = gender.
_LANG = {"a": "en-US", "b": "en-GB", "e": "es-ES", "f": "fr-FR", "h": "hi-IN", "i": "it-IT",
         "j": "ja-JP", "p": "pt-BR", "z": "zh-CN"}
_VOICES = """
af_heart af_bella af_nicole af_aoede af_kore af_sarah af_nova af_sky af_alloy af_jessica af_river
am_michael am_fenrir am_puck am_echo am_eric am_liam am_onyx am_adam am_santa
bf_emma bf_isabella bf_alice bf_lily bm_george bm_fable bm_lewis bm_daniel
ef_dora em_alex em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola
jf_alpha jf_gongitsune jf_nezumi jf_tebukuro jm_kumo pf_dora pm_alex pm_santa
zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang
""".split()


class KokoroTTS(TTSBackend):
    name = "kokoro"
    extension = "wav"
    sample_rate = 24000

    def __init__(self):
        try:
            import kokoro  # noqa: F401
        except ImportError as e:
            raise SystemExit("The kokoro backend needs: pip install 'audiopub[kokoro]'") from e
        self._pipelines: dict[str, object] = {}

    def voices(self, language: str) -> list[Voice]:
        out = [Voice(v, "female" if v[1] == "f" else "male", _LANG[v[0]]) for v in _VOICES]
        native = [v for v in out if v.matches_language(language)]
        # Kokoro's English voices are the strongest; offer them as extras for English only.
        return native or [v for v in out if v.locale.startswith("en")]

    def render(self, text: str, voice: str, speed: float, out: Path) -> None:
        import numpy as np
        from kokoro import KPipeline

        code = voice[0]
        if code not in self._pipelines:
            self._pipelines[code] = KPipeline(lang_code=code)
        chunks = [np.asarray(audio) for _, _, audio in self._pipelines[code](text, voice=voice, speed=speed)]
        pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        data = (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(data)
