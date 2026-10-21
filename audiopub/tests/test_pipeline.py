import json
import subprocess
from collections import Counter

import pytest

from audiopub.attribution import HeuristicAttributor, LLMAttributor
from audiopub.casting import build_cast
from audiopub.cli import main, parse_selection
from audiopub.epub import read_epub
from audiopub.llm import LLMError, make_llm, parse_spec
from audiopub.llm.base import parse_json_loose
from audiopub.models import NARRATOR, Character, Roster
from audiopub.pipeline import Project
from audiopub.tts import Voice


def test_read_epub(sample_epub):
    book = read_epub(sample_epub)
    assert (book.title, book.author, book.language) == ("A Test Book", "Jane Writer", "en")
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    first = book.chapters[0].paragraphs
    assert first[0] == "It was a cold morning in the village."  # heading removed
    assert first[-1] == "Note here."  # footnote marker removed


def test_roster_aliases():
    r = Roster()
    assert r.upsert(Character("Elizabeth Bennet", gender="female", aliases=["Lizzy"])) == "Elizabeth Bennet"
    assert r.upsert(Character("Lizzy", age="young")) == "Elizabeth Bennet"
    assert r.resolve("lizzy") == "Elizabeth Bennet"
    assert r.characters["Elizabeth Bennet"].age == "young"


def test_heuristic_attribution(sample_epub):
    book = read_epub(sample_epub)
    roster = Roster()
    segs = HeuristicAttributor().attribute_chapter(book.chapters[0], roster)
    speakers = [(s.speaker, s.text) for s in segs]
    assert ("Anna", "Where are you going?") in speakers
    assert ("Marco", "To the market,") in speakers
    assert ("Marco", "Do you want to come?") in speakers  # same paragraph -> same speaker
    assert (NARRATOR, "It was a cold morning in the village.") in speakers


def test_llm_attribution_and_fallback(sample_epub, fake_llm):
    book = read_epub(sample_epub)
    roster = Roster()
    segs = LLMAttributor(fake_llm).attribute_chapter(book.chapters[0], roster)
    assert [s.speaker for s in segs if s.speaker != NARRATOR] == ["Anna", "Marco", "Marco", "Anna"]
    assert roster.characters["Marco"].aliases == ["Marc"]
    # Text sent to the model is tagged; text read aloud comes from the book, not the model.
    assert '<q id="1">Where are you going?</q>' in fake_llm.prompts[0]

    class Broken:
        name = "broken"

        def complete_json(self, *a):
            raise LLMError("nope")

    segs = LLMAttributor(Broken()).attribute_chapter(book.chapters[0], Roster())
    assert any(s.speaker == "Anna" for s in segs)  # heuristics took over


def test_casting_distinct_voices_and_gender():
    roster = Roster([Character("Anna", "female"), Character("Bea", "female"), Character("Cleo", "female"),
                     Character("Marco", "male"), Character("Kid", "male", "child")])
    voices = [Voice("narr", "male", "en"), Voice("f1", "female", "en"), Voice("f2", "female", "en"),
              Voice("m1", "male", "en")]
    lines = Counter({"Anna": 9, "Bea": 5, "Cleo": 1, "Marco": 7, "Kid": 2, NARRATOR: 50})
    cast = build_cast({}, roster, lines, voices)
    assert cast[NARRATOR]["voice"] == "narr"
    assert {cast["Anna"]["voice"], cast["Bea"]["voice"]} == {"f1", "f2"}
    assert cast["Marco"]["voice"] == "m1"
    # Third woman reuses a voice but with a pitch shift.
    assert cast["Cleo"]["voice"] in ("f1", "f2") and cast["Cleo"]["pitch"] != 0
    assert cast["Kid"]["pitch"] > 0
    # Manual edits survive re-casting.
    cast["Anna"]["voice"] = "custom"
    assert build_cast(cast, roster, lines, voices)["Anna"]["voice"] == "custom"


def test_end_to_end_m4b(tmp_path, sample_epub, fake_llm, fake_tts):
    book = read_epub(sample_epub)
    project = Project(book, tmp_path / "work")
    scripts = project.analyze(LLMAttributor(fake_llm), book.chapters)
    cast = project.cast(fake_tts, scripts)
    out = project.render(fake_tts, scripts, cast, out_dir=tmp_path / "out")
    assert out.suffix == ".m4b" and out.stat().st_size > 0

    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", "-show_format", str(out)],
        check=True, capture_output=True).stdout)
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["Chapter One", "Chapter Two"]
    assert probe["format"]["tags"]["title"] == "A Test Book"

    # Re-rendering is fully cached.
    n = len(fake_tts.calls)
    project.render(fake_tts, scripts, cast, out_dir=tmp_path / "out")
    assert len(fake_tts.calls) == n
    # Different voices were used for different characters.
    voices = {v for v, _ in fake_tts.calls}
    assert len(voices) >= 3


def test_mp3_output(tmp_path, sample_epub, fake_tts):
    book = read_epub(sample_epub)
    project = Project(book, tmp_path / "work")
    scripts = project.analyze(HeuristicAttributor(), book.chapters[:1])
    out = project.render(fake_tts, scripts, project.cast(fake_tts, scripts), fmt="mp3", out_dir=tmp_path)
    assert [p.name for p in out.iterdir()] == ["001 - Chapter_One.mp3"]


def test_selection_and_specs():
    assert parse_selection("1-3,7", 10) == [0, 1, 2, 6]
    assert parse_selection("8-", 10) == [7, 8, 9]
    assert parse_selection(None, 3) == [0, 1, 2]
    assert parse_spec("ollama:qwen3:14b") == ("ollama", "qwen3:14b")
    assert parse_spec("anthropic") == ("anthropic", None)
    with pytest.raises(ValueError):
        parse_spec("nope:model")
    llm = make_llm("ollama:llama3.1")
    assert str(llm.client.base_url).startswith("http://localhost:11434")
    assert parse_json_loose('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}


def test_cli_chapters(sample_epub, capsys):
    main(["chapters", str(sample_epub)])
    out = capsys.readouterr().out
    assert "Chapter One" in out and "Chapter Two" in out
