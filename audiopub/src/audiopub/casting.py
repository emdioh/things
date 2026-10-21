"""Assign a voice to the narrator and each character.

The cast is saved as JSON so it can be reviewed and hand-edited between the
``analyze`` and ``render`` steps; existing assignments are never overwritten.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import NARRATOR, Roster
from .tts import Voice

# When a voice has to be reused, shift its pitch (in semitones) so the two
# characters still sound different.
_PITCH_VARIANTS = [0.0, 2.0, -2.0, 3.5, -3.5, 1.0, -1.0]


def build_cast(
    existing: dict,
    roster: Roster,
    line_counts: Counter,
    voices: list[Voice],
    narrator_voice: str | None = None,
) -> dict:
    if not voices:
        raise SystemExit("the TTS backend offers no voices for this language")
    cast: dict = {k: dict(v) for k, v in existing.items()}
    usage: Counter = Counter(entry["voice"] for entry in cast.values())

    if NARRATOR not in cast:
        voice = narrator_voice or voices[0].id
        cast[NARRATOR] = {"voice": voice, "pitch": 0.0, "speed": 1.0}
        usage[voice] += 1
    narrator = cast[NARRATOR]["voice"]

    speakers = [s for s, _ in line_counts.most_common() if s != NARRATOR and s not in cast]
    for speaker in speakers:
        char = roster.characters.get(speaker)
        gender = char.gender if char else "unknown"
        age = char.age if char else "unknown"

        candidates = [v for v in voices if gender == "unknown" or v.gender == gender] or voices
        others = [v for v in candidates if v.id != narrator] or candidates
        # Least-used voice first; native voices (listed first) break ties.
        best = min(others, key=lambda v: (usage[v.id], others.index(v)))
        pitch = _PITCH_VARIANTS[usage[best.id] % len(_PITCH_VARIANTS)]
        speed = 1.0
        if age == "child":
            pitch += 3.0
            speed = 1.05
        elif age == "elderly":
            pitch -= 1.0
            speed = 0.92
        usage[best.id] += 1
        cast[speaker] = {
            "voice": best.id,
            "pitch": pitch,
            "speed": speed,
            "gender": gender,
            "age": age,
            "lines": line_counts[speaker],
        }
    return cast


def load_cast(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_cast(path: Path, cast: dict) -> None:
    path.write_text(json.dumps(cast, indent=2, ensure_ascii=False) + "\n")
