"""Assign a voice to the narrator and each character.

The cast is saved as JSON so it can be reviewed and hand-edited between the
``analyze`` and ``render`` steps; existing assignments are kept unless the
cast is reset.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import NARRATOR, Roster
from .tts import Voice

# When there are more characters than voices, a voice is reused with a small
# pitch (semitones) and speed change so the characters still sound different.
# Ordered from most to least distinct from the plain voice; kept small, since
# larger shifts sound artificial.
VARIANTS = [(0.0, 1.0), (2.0, 1.0), (-2.0, 1.0), (1.0, 1.06), (-1.0, 0.94),
            (3.0, 1.0), (-3.0, 1.0), (1.0, 0.94), (-1.0, 1.06), (2.0, 1.06), (-2.0, 0.94)]


def pick_voices(voices: list[Voice], language: str | None, multilingual: bool = False) -> list[Voice]:
    """The voices to cast from: the book's language only, unless asked otherwise.

    Foreign "multilingual" voices can read the book's language but with an
    accent, so they are only used when requested or when nothing else exists.
    """
    if language is None or multilingual:
        return voices
    native = [v for v in voices if v.matches_language(language)]
    return native or voices


def build_cast(
    existing: dict,
    roster: Roster,
    line_counts: Counter,
    voices: list[Voice],
    narrator_voice: str | None = None,
) -> dict:
    """Give every speaker without a cast entry a (voice, pitch, speed).

    Speakers are cast in order of how much they speak, so the main characters
    get plain voices and the most distinct variants. The narrator's voice is
    used for characters only once every other voice and variant is taken, and
    then only with a clear pitch shift.
    """
    if not voices:
        raise SystemExit("the TTS backend offers no voices for this language")
    cast: dict = {k: dict(v) for k, v in existing.items()}

    if NARRATOR not in cast:
        cast[NARRATOR] = {"voice": narrator_voice or voices[0].id, "pitch": 0.0, "speed": 1.0}
    narrator = cast[NARRATOR]["voice"]
    taken = Counter(_slot(e) for e in cast.values())

    speakers = [s for s, _ in line_counts.most_common() if s != NARRATOR and s not in cast]
    for speaker in speakers:
        char = roster.characters.get(speaker)
        gender = char.gender if char else "unknown"
        age = char.age if char else "unknown"

        candidates = [v for v in voices if gender == "unknown" or v.gender == gender] or voices
        others = [v for v in candidates if v.id != narrator]
        slots = [(v.id, pitch, speed) for pitch, speed in VARIANTS for v in others]
        if narrator in {v.id for v in candidates}:
            # Last resort, and only clearly shifted, so it isn't mistaken for the narration.
            slots += [(narrator, pitch, speed) for pitch, speed in VARIANTS if abs(pitch) >= 2]
        voice, pitch, speed = min(slots, key=lambda slot: taken[slot])  # first unused, else least reused
        taken[(voice, pitch, speed)] += 1
        if age == "child":
            pitch, speed = pitch + 3.0, speed * 1.05
        elif age == "elderly":
            pitch, speed = pitch - 1.0, speed * 0.92
        cast[speaker] = {
            "voice": voice,
            "pitch": pitch,
            "speed": round(speed, 2),
            "gender": gender,
            "age": age,
            "lines": line_counts[speaker],
        }
    return cast


def _slot(entry: dict) -> tuple:
    return entry["voice"], float(entry.get("pitch", 0.0)), float(entry.get("speed", 1.0))


def load_cast(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_cast(path: Path, cast: dict) -> None:
    path.write_text(json.dumps(cast, indent=2, ensure_ascii=False) + "\n")
