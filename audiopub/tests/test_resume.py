import json

import pytest

from audiopub.attribution import LLMAttributor
from audiopub.casting import load_cast, save_cast
from audiopub.cli import main
from audiopub.epub import read_epub
from audiopub.models import NARRATOR, Character, Roster
from audiopub.pipeline import Project, default_root, project_dir

from conftest import FakeLLM, make_epub


def seg_tuples(scripts):
    return {i: [(s.speaker, s.text, s.paragraph) for s in segs] for i, segs in scripts.items()}


class InterruptingLLM:
    """Delegates to a FakeLLM but raises KeyboardInterrupt on the Nth call."""

    name = "fake:llm"

    def __init__(self, inner, die_on):
        self.inner, self.die_on, self.calls = inner, die_on, 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        if self.calls == self.die_on:
            raise KeyboardInterrupt
        return self.inner.complete_json(system, user, schema)


def analyzed(tmp_path, sample_epub, fake_llm, name="work"):
    book = read_epub(sample_epub)
    project = Project(book, tmp_path / name)
    scripts = project.analyze(LLMAttributor(fake_llm), book.chapters)
    return book, project, scripts


# ---- 1. project_dir ---------------------------------------------------------

def test_project_dir_independent_of_file_location(tmp_path):
    a = read_epub(make_epub(tmp_path / "a.epub"))
    (tmp_path / "sub").mkdir()
    b = read_epub(make_epub(tmp_path / "sub" / "renamed.epub"))
    assert project_dir(a, tmp_path / "root") == project_dir(b, tmp_path / "root")


def test_project_dir_differs_per_book(tmp_path):
    a = read_epub(make_epub(tmp_path / "a.epub"))
    b = read_epub(make_epub(tmp_path / "b.epub"))
    b.title = "Another Book"
    assert project_dir(a, tmp_path) != project_dir(b, tmp_path)
    c = read_epub(make_epub(tmp_path / "c.epub"))
    c.author = "Someone Else"
    assert project_dir(a, tmp_path) != project_dir(c, tmp_path)
    d = read_epub(make_epub(tmp_path / "d.epub"))
    d.identifier = "other-id"
    assert project_dir(a, tmp_path) != project_dir(d, tmp_path)


def test_project_dir_roots(tmp_path, monkeypatch, sample_epub):
    book = read_epub(sample_epub)
    monkeypatch.setenv("AUDIOPUB_HOME", str(tmp_path / "home"))
    assert default_root() == tmp_path / "home"
    d = project_dir(book)
    assert d.parent == tmp_path / "home" / "books"
    assert d.name.startswith("A_Test_Book-")
    # An explicit root wins over the environment.
    assert project_dir(book, tmp_path / "explicit").parent == tmp_path / "explicit" / "books"
    monkeypatch.delenv("AUDIOPUB_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_root() == tmp_path / "xdg" / "audiopub"


# ---- 2. analysis is not repeated -------------------------------------------

def test_second_analyze_makes_no_llm_calls(tmp_path, sample_epub, fake_llm):
    book, project, scripts = analyzed(tmp_path, sample_epub, fake_llm)
    assert fake_llm.prompts
    fake_llm.prompts.clear()
    again = Project(book, tmp_path / "work").analyze(LLMAttributor(fake_llm), book.chapters)
    assert fake_llm.prompts == []
    assert seg_tuples(again) == seg_tuples(scripts)


# ---- 3. mid-chapter resume --------------------------------------------------

def test_mid_chapter_resume(tmp_path, sample_epub):
    book = read_epub(sample_epub)

    ref_llm = FakeLLM()
    ref = Project(book, tmp_path / "ref").analyze(LLMAttributor(ref_llm, chunk_chars=60), book.chapters)
    full_calls = len(ref_llm.prompts)
    assert full_calls >= 3  # several chunks, or this test proves nothing

    work = tmp_path / "work"
    project = Project(book, work)
    dying = InterruptingLLM(FakeLLM(), die_on=2)
    with pytest.raises(KeyboardInterrupt):
        project.analyze(LLMAttributor(dying, chunk_chars=60), book.chapters)
    partial = work / "script" / "ch000.partial.json"
    assert partial.exists()
    assert not (work / "script" / "ch000.json").exists()
    st = project.status(book.chapters)[0]
    assert st.analysis == "partial" and st.paragraphs_done > 0

    llm = FakeLLM()
    scripts = Project(book, work).analyze(LLMAttributor(llm, chunk_chars=60), book.chapters)
    assert 0 < len(llm.prompts) < full_calls
    assert seg_tuples(scripts) == seg_tuples(ref)
    assert not partial.exists()
    assert [s.analysis for s in project.status(book.chapters)] == ["done", "done"]


# ---- 4. --reanalyze / force -------------------------------------------------

def test_force_all_resets_roster(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    roster = project.load_roster()
    roster.upsert(Character("Ghost", gender="male"))
    project.save_roster(roster)
    project.analyze(LLMAttributor(fake_llm), book.chapters, force=True)
    assert "Ghost" not in project.load_roster().characters


def test_force_subset_keeps_roster(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    roster = project.load_roster()
    roster.upsert(Character("Ghost", gender="male"))
    project.save_roster(roster)
    fake_llm.prompts.clear()
    project.analyze(LLMAttributor(fake_llm), book.chapters[1:], force=True)
    assert fake_llm.prompts  # it really re-ran
    assert "Ghost" in project.load_roster().characters


# ---- 5. rename_speaker ------------------------------------------------------

def all_speakers(project):
    return {s["speaker"] for p in (project.dir / "script").glob("ch???.json")
            for s in json.loads(p.read_text())["segments"]}


def test_rename_plain(tmp_path, sample_epub, fake_llm, fake_tts):
    book, project, scripts = analyzed(tmp_path, sample_epub, fake_llm)
    project.cast(fake_tts, scripts)
    n = project.rename_speaker("Baker", "Fishmonger")
    assert n == 1
    assert "Fishmonger" in all_speakers(project) and "Baker" not in all_speakers(project)
    roster = project.load_roster()
    assert "Baker" not in roster.characters
    fm = roster.characters["Fishmonger"]
    assert (fm.gender, fm.age, fm.description) == ("male", "elderly", "loud baker")
    assert roster.resolve("Baker") == "Fishmonger"
    cast = load_cast(project.cast_path(fake_tts))
    assert "Baker" not in cast and "Fishmonger" in cast


def test_rename_renames_across_all_chapters(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    assert project.rename_speaker("Marco", "Anna") == 2
    project.rename_speaker("Anna", "Zed")
    assert "Anna" not in all_speakers(project)
    # Chapter 2's speaker (Baker) is renamed in its own file too.
    project.rename_speaker("Baker", "Zed")
    ch1 = json.loads((project.dir / "script" / "ch001.json").read_text())
    assert {s["speaker"] for s in ch1["segments"]} == {NARRATOR, "Zed"}


def test_rename_merge_keeps_target_gender_and_adds_alias(tmp_path, sample_epub, fake_llm, fake_tts):
    book, project, scripts = analyzed(tmp_path, sample_epub, fake_llm)
    cast = project.cast(fake_tts, scripts)
    marco_voice = cast["Marco"]["voice"]
    project.rename_speaker("Anna", "Marco")  # female into male
    roster = project.load_roster()
    assert "Anna" not in roster.characters
    marco = roster.characters["Marco"]
    assert marco.gender == "male"
    assert "Anna" in marco.aliases and "Marc" in marco.aliases
    assert roster.resolve("Anna") == "Marco"
    assert "Anna" not in all_speakers(project)
    cast = load_cast(project.cast_path(fake_tts))
    assert "Anna" not in cast and cast["Marco"]["voice"] == marco_voice


def test_rename_merge_fills_unknown_gender(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    roster = project.load_roster()
    roster.characters["Marco"].gender = "unknown"
    project.save_roster(roster)
    project.rename_speaker("Anna", "Marco")
    marco = project.load_roster().characters["Marco"]
    assert marco.gender == "female"


def test_rename_to_narrator(tmp_path, sample_epub, fake_llm, fake_tts):
    book, project, scripts = analyzed(tmp_path, sample_epub, fake_llm)
    project.cast(fake_tts, scripts)
    assert project.rename_speaker("Baker", "narrator") == 1
    assert "Baker" not in all_speakers(project)
    roster = project.load_roster()
    assert "Baker" not in roster.characters and NARRATOR not in roster.characters
    cast = load_cast(project.cast_path(fake_tts))
    assert "Baker" not in cast and NARRATOR in cast


def test_rename_by_alias(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    assert project.rename_speaker("Marc", "Marco Polo") == 2
    assert "Marco Polo" in project.load_roster().characters


def test_rename_unknown_speaker_exits(tmp_path, sample_epub, fake_llm):
    book, project, _ = analyzed(tmp_path, sample_epub, fake_llm)
    with pytest.raises(SystemExit):
        project.rename_speaker("Nobody", "Somebody")


# ---- 6. status after render -------------------------------------------------

def test_status_after_render_and_voice_change(tmp_path, sample_epub, fake_llm, fake_tts):
    book, project, scripts = analyzed(tmp_path, sample_epub, fake_llm)
    before = project.status(book.chapters, "fake", "wav")
    assert all(r.lines_total == 0 and not r.encoded for r in before)  # no cast yet

    cast = project.cast(fake_tts, scripts)
    project.render(fake_tts, scripts, cast, out_dir=tmp_path / "out")
    rows = project.status(book.chapters, "fake", "wav")
    assert all(r.analysis == "done" for r in rows)
    for r in rows:
        assert r.lines_total > 0 and r.lines_rendered == r.lines_total and r.encoded

    cast["Anna"]["voice"] = "m1"
    save_cast(project.cast_path(fake_tts), cast)
    rows = project.status(book.chapters, "fake", "wav")
    assert not rows[0].encoded and rows[0].lines_rendered < rows[0].lines_total
    assert rows[1].encoded and rows[1].lines_rendered == rows[1].lines_total


# ---- 7. CLI smoke -----------------------------------------------------------

def test_cli_analyze_status_speaker(tmp_path, sample_epub, capsys):
    work = tmp_path / "cliwork"
    main(["analyze", str(sample_epub), "--no-llm", "--workdir", str(work)])
    capsys.readouterr()
    main(["status", str(sample_epub), "--speakers", "--workdir", str(work)])
    out = capsys.readouterr().out
    assert "Chapter One" in out and "Chapter Two" in out
    assert "2/2 chapters analyzed" in out
    assert "Anna" in out

    main(["speaker", str(sample_epub), "Anna", "Annie", "--workdir", str(work)])
    assert "line(s) reassigned" in capsys.readouterr().out
    main(["status", str(sample_epub), "--speakers", "--workdir", str(work)])
    out = capsys.readouterr().out
    assert "Annie" in out and "Anna " not in out

    with pytest.raises(SystemExit):
        main(["speaker", str(sample_epub), "Nobody", "X", "--workdir", str(work)])
