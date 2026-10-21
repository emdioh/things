from collections import Counter

import pytest

from audiopub import cli
from audiopub.attribution import HeuristicAttributor
from audiopub.casting import VARIANTS, build_cast, pick_voices
from audiopub.epub import read_epub
from audiopub.models import NARRATOR, Character, Roster
from audiopub.pipeline import Project
from audiopub.tts import Voice
from audiopub.tts.base import TTSBackend

GIUSEPPE = "it-IT-GiuseppeMultilingualNeural"
DIEGO = "it-IT-DiegoNeural"
ELSA = "it-IT-ElsaNeural"
ISABELLA = "it-IT-IsabellaNeural"

IT_VOICES = [
    Voice(GIUSEPPE, "male", "it-IT"),
    Voice(DIEGO, "male", "it-IT"),
    Voice(ELSA, "female", "it-IT"),
    Voice(ISABELLA, "female", "it-IT"),
]
FOREIGN = [
    Voice("en-US-AndrewMultilingualNeural", "male", "*"),
    Voice("fr-FR-RemyMultilingualNeural", "male", "*"),
    Voice("en-US-JennyNeural", "female", "en-US"),
]
ALL = IT_VOICES + FOREIGN


def test_pick_voices_native_only_by_default():
    assert pick_voices(ALL, "it") == IT_VOICES
    assert pick_voices(ALL, "it-IT") == IT_VOICES


def test_pick_voices_multilingual_and_fallbacks():
    assert pick_voices(ALL, "it", multilingual=True) == ALL
    assert pick_voices(ALL, "fi") == ALL  # no native voice -> everything
    assert pick_voices(ALL, None) == ALL


def _italian_cast():
    chars = [Character(f"M{i:02d}", "male") for i in range(15)]
    chars += [Character("Lei", "female"), Character("Boh", "unknown")]
    roster = Roster(chars)
    lines = Counter({NARRATOR: 500})
    for i in range(15):
        lines[f"M{i:02d}"] = 100 - i
    lines["Lei"] = 40
    lines["Boh"] = 3
    voices = pick_voices(ALL, "it")
    return build_cast({}, roster, lines, voices, narrator_voice=GIUSEPPE)


def test_italian_cast_uses_only_italian_voices():
    cast = _italian_cast()
    italian = {v.id for v in IT_VOICES}
    assert {e["voice"] for e in cast.values()} <= italian
    assert cast[NARRATOR]["voice"] == GIUSEPPE
    assert cast["M00"]["voice"] == DIEGO
    assert cast["M00"]["pitch"] == 0.0 and cast["M00"]["speed"] == 1.0
    assert cast["Lei"]["voice"] in (ELSA, ISABELLA)


def test_italian_cast_slots_unique_and_narrator_voice_last_resort():
    cast = _italian_cast()
    males = [cast[f"M{i:02d}"] for i in range(15)]
    slots = [(e["voice"], e["pitch"], e["speed"]) for e in males]
    n_diego = len(VARIANTS)
    # Diego's variants come first, in order, with no repeats.
    assert slots[:n_diego] == [(DIEGO, p, s) for p, s in VARIANTS]
    # Then the narrator's voice, only with a clear pitch shift.
    narr_slots = [s for s in slots if s[0] == GIUSEPPE]
    assert all(abs(p) >= 2 for _, p, _ in narr_slots)
    assert all(s[0] == DIEGO for s in slots[:n_diego])
    # No duplicates while unique slots remain.
    unique_capacity = n_diego + sum(1 for p, _ in VARIANTS if abs(p) >= 2)
    assert len(males) <= unique_capacity
    assert len(set(slots)) == len(slots)
    # Narrator voice never plain for a character.
    for name, e in cast.items():
        if name != NARRATOR and e["voice"] == GIUSEPPE:
            assert abs(e["pitch"]) >= 2


def test_unknown_gender_not_on_plain_narrator_voice():
    cast = _italian_cast()
    assert not (cast["Boh"]["voice"] == GIUSEPPE and cast["Boh"]["pitch"] == 0)


def test_pitch_range_adults():
    cast = _italian_cast()
    for name, e in cast.items():
        assert -3 <= e["pitch"] <= 3, name


def test_child_and_elderly_adjustments():
    voices = [Voice("narr", "male", "it-IT"), Voice("m1", "male", "it-IT")]
    roster = Roster([Character("Adult", "male"), Character("Kid", "male", "child"),
                     Character("Nonno", "male", "elderly")])
    lines = Counter({"Adult": 9, "Kid": 5, "Nonno": 2, NARRATOR: 50})
    cast = build_cast({}, roster, lines, voices)
    # Kid takes the 2nd slot (pitch 2.0, speed 1.0) and is raised by 3.
    base = VARIANTS[1]
    assert cast["Kid"]["pitch"] == base[0] + 3.0 > base[0]
    assert cast["Nonno"]["speed"] < 1.0
    assert cast["Nonno"]["speed"] == round(VARIANTS[2][1] * 0.92, 2)


def test_existing_entries_preserved():
    roster = Roster([Character("A", "male"), Character("B", "male")])
    lines = Counter({"A": 5, "B": 3, NARRATOR: 10})
    edited = {NARRATOR: {"voice": ELSA, "pitch": 0.0, "speed": 1.0},
              "A": {"voice": "custom", "pitch": 1.5, "speed": 0.9}}
    cast = build_cast(edited, roster, lines, IT_VOICES)
    assert cast["A"] == {"voice": "custom", "pitch": 1.5, "speed": 0.9}
    assert cast[NARRATOR]["voice"] == ELSA
    assert "B" in cast
    assert edited["A"]["voice"] == "custom"  # input not mutated


class StarTTS(TTSBackend):
    name = "star"
    extension = "wav"

    def voices(self, language):
        return [Voice("narr", "male", "en-US"), Voice("f1", "female", "en-US"),
                Voice("m1", "male", "en-US"), Voice("f2", "female", "en-GB"),
                Voice("x-Multilingual", "male", "*")]

    def render(self, text, voice, speed, out):
        raise NotImplementedError


def _project(tmp_path, sample_epub):
    book = read_epub(sample_epub)
    project = Project(book, tmp_path / "work")
    scripts = project.analyze(HeuristicAttributor(), book.chapters)
    return project, scripts


def test_project_cast_multilingual_flag(tmp_path, sample_epub):
    project, scripts = _project(tmp_path, sample_epub)
    speakers = sorted({seg.speaker for segs in scripts.values() for seg in segs} - {NARRATOR})
    assert speakers
    project.save_roster(Roster([Character(n, "male") for n in speakers]))
    tts = StarTTS()
    cast = project.cast(tts, scripts, "narr")
    assert "x-Multilingual" not in {e["voice"] for e in cast.values()}
    cast = project.cast(tts, scripts, "narr", reset=True, multilingual=True)
    # narr is the narrator, so the free male slots are m1 and x-Multilingual.
    assert {cast[n]["voice"] for n in speakers} >= {"m1", "x-Multilingual"} or len(speakers) < 2
    assert any(cast[n]["voice"] == "x-Multilingual" for n in speakers)


def test_project_cast_reset(tmp_path, sample_epub, fake_tts):
    project, scripts = _project(tmp_path, sample_epub)
    cast = project.cast(fake_tts, scripts, "narr")
    who = next(k for k in cast if k != NARRATOR)
    cast[who]["voice"] = "hand-edited"
    from audiopub.casting import save_cast
    save_cast(project.cast_path(fake_tts), cast)

    assert project.cast(fake_tts, scripts, "narr")[who]["voice"] == "hand-edited"
    assert project.cast(fake_tts, scripts, "narr", reset=True)[who]["voice"] != "hand-edited"


@pytest.mark.parametrize("cmd", ["cast", "render"])
def test_cli_flags_wired(monkeypatch, cmd):
    seen = {}
    monkeypatch.setattr(cli, f"cmd_{cmd}", lambda args: seen.update(args=args))
    argv = [cmd, "x.epub", "--multilingual-voices"]
    if cmd == "cast":
        argv.append("--reset")
    cli.main(argv)
    assert seen["args"].multilingual_voices is True
    if cmd == "cast":
        assert seen["args"].reset is True


def test_cli_flags_default_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_cast", lambda args: seen.update(args=args))
    cli.main(["cast", "x.epub"])
    assert seen["args"].reset is False and seen["args"].multilingual_voices is False
