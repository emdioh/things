import json

import pytest

from audiopub.attribution import LLMAttributor
from audiopub.cli import main
from audiopub.epub import read_epub
from audiopub.llm.base import LLMError, LLMRefusal, parse_json_loose
from audiopub.pipeline import Project, project_dir

from conftest import FakeLLM

FAIL_TEXT = "Of course"


def seg_tuples(scripts):
    return {i: [(s.speaker, s.text, s.paragraph) for s in segs] for i, segs in scripts.items()}


class FlakyLLM:
    """Delegates to a FakeLLM; ``fail_when(user, n)`` (n = prior calls for that
    prompt) decides whether to raise ``exc`` instead."""

    name = "fake:llm"

    def __init__(self, inner=None, fail_when=None, exc=LLMError):
        self.inner = inner or FakeLLM()
        self.fail_when = fail_when or (lambda user, n: False)
        self.exc = exc
        self.calls = 0
        self.seen: dict[str, int] = {}

    def complete_json(self, system, user, schema):
        self.calls += 1
        n = self.seen.get(user, 0)
        self.seen[user] = n + 1
        if self.fail_when(user, n):
            raise self.exc("boom")
        return self.inner.complete_json(system, user, schema)


def clean_run(book, tmp_path, name="ref", chunk_chars=60):
    llm = FakeLLM()
    scripts = Project(book, tmp_path / name).analyze(LLMAttributor(llm, chunk_chars=chunk_chars), book.chapters)
    return scripts, len(llm.prompts)


def script_json(work, i=0):
    return json.loads((work / "script" / f"ch{i:03d}.json").read_text())


def failing_state(tmp_path, sample_epub, work="work"):
    book = read_epub(sample_epub)
    project = Project(book, tmp_path / work)
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u)
    scripts = project.analyze(LLMAttributor(llm, chunk_chars=60), book.chapters)
    return book, project, scripts


# ---- 1. parse_json_loose ----------------------------------------------------

def test_parse_json_loose_real_failure():
    bad = '{"characters":[{"name":"Cavaliere Longo","aliases":["Longo"],"gender":"male","age":"adult","description":"a knight","},'
    with pytest.raises(LLMError) as ei:
        parse_json_loose(bad)
    assert "invalid JSON" in str(ei.value)
    assert "near" in str(ei.value)


def test_parse_json_loose_fenced():
    assert parse_json_loose('Here:\n```json\n{"a": 1}\n```\n') == {"a": 1}
    assert parse_json_loose('```\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert parse_json_loose('noise {"a": 1} trailing') == {"a": 1}


# ---- 2. retry succeeds ------------------------------------------------------

def test_retry_succeeds(tmp_path, sample_epub):
    book = read_epub(sample_epub)
    ref, ref_calls = clean_run(book, tmp_path)

    # First two attempts of every prompt fail; the third succeeds.
    llm = FlakyLLM(fail_when=lambda u, n: n < 2)
    project = Project(book, tmp_path / "work")
    scripts = project.analyze(LLMAttributor(llm, chunk_chars=60, attempts=3), book.chapters)
    assert seg_tuples(scripts) == seg_tuples(ref)
    assert llm.calls == ref_calls * 3
    assert len(llm.inner.prompts) == ref_calls
    for i in (0, 1):
        assert script_json(tmp_path / "work", i)["failed_chunks"] == []
    assert [project.failed_chunks(c) for c in book.chapters] == [[], []]


def test_single_transient_failure(tmp_path, sample_epub):
    book = read_epub(sample_epub)
    ref, ref_calls = clean_run(book, tmp_path)
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u and n == 0)
    scripts = Project(book, tmp_path / "work").analyze(LLMAttributor(llm, chunk_chars=60), book.chapters)
    assert seg_tuples(scripts) == seg_tuples(ref)
    assert llm.calls == ref_calls + 1


# ---- 3. refusal not retried -------------------------------------------------

def test_refusal_not_retried(tmp_path, sample_epub):
    book = read_epub(sample_epub)
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u, exc=LLMRefusal)
    project = Project(book, tmp_path / "work")
    scripts = project.analyze(LLMAttributor(llm, chunk_chars=60), book.chapters)
    bad = [u for u in llm.seen if FAIL_TEXT in u]
    assert bad and all(llm.seen[u] == 1 for u in bad)
    failed = project.failed_chunks(book.chapters[0])
    assert len(failed) == 1
    # Heuristics still gave the line a speaker.
    line = [s for s in scripts[0] if FAIL_TEXT in s.text]
    assert line and line[0].speaker


# ---- 4. persistent failure on one chunk, then --retry-failed ----------------

def test_persistent_failure_and_retry(tmp_path, sample_epub):
    book = read_epub(sample_epub)
    ref, _ = clean_run(book, tmp_path)
    book, project, scripts = failing_state(tmp_path, sample_epub)
    work = tmp_path / "work"

    data = script_json(work)
    assert len(data["failed_chunks"]) == 1
    start, end = data["failed_chunks"][0]
    assert start < end
    assert project.failed_chunks(book.chapters[0]) == [(start, end)]
    assert project.failed_chunks(book.chapters[1]) == []
    rows = project.status(book.chapters)
    assert [r.failed_chunks for r in rows] == [1, 0]
    assert "labels" in data

    good = FakeLLM()
    again = Project(book, work).analyze(LLMAttributor(good, chunk_chars=60), book.chapters, retry_failed=True)
    assert len(good.prompts) >= 1
    assert all(FAIL_TEXT in p for p in good.prompts)
    assert Project(book, work).failed_chunks(book.chapters[0]) == []
    assert script_json(work)["failed_chunks"] == []
    assert [r.failed_chunks for r in project.status(book.chapters)] == [0, 0]
    assert seg_tuples(again) == seg_tuples(ref)

    # Nothing left to retry.
    good.prompts.clear()
    third = Project(book, work).analyze(LLMAttributor(good, chunk_chars=60), book.chapters, retry_failed=True)
    assert good.prompts == []
    assert seg_tuples(third) == seg_tuples(ref)


# ---- 5. no retry flag -> cached ---------------------------------------------

def test_failed_chapter_cached_without_flag(tmp_path, sample_epub):
    book, project, scripts = failing_state(tmp_path, sample_epub)
    good = FakeLLM()
    again = Project(book, tmp_path / "work").analyze(LLMAttributor(good, chunk_chars=60), book.chapters)
    assert good.prompts == []
    assert seg_tuples(again) == seg_tuples(scripts)
    assert Project(book, tmp_path / "work").failed_chunks(book.chapters[0])


# ---- 6. rename keeps labels in sync -----------------------------------------

def test_rename_then_retry_keeps_new_name(tmp_path, sample_epub):
    book, project, _ = failing_state(tmp_path, sample_epub)
    work = tmp_path / "work"
    assert any(l[2] == "Marco" for l in script_json(work)["labels"])
    assert project.rename_speaker("Marco", "Marcus") > 0
    data = script_json(work)
    assert not any(l[2] == "Marco" for l in data["labels"])
    assert any(l[2] == "Marcus" for l in data["labels"])

    good = FakeLLM()
    scripts = Project(book, work).analyze(LLMAttributor(good, chunk_chars=60), book.chapters, retry_failed=True)
    assert all(FAIL_TEXT in p for p in good.prompts)
    speakers = {s.speaker for s in scripts[0]}
    assert "Marcus" in speakers
    assert "Marco" not in speakers


# ---- 7. CLI status ----------------------------------------------------------

def test_cli_status_mentions_failed(tmp_path, sample_epub, capsys):
    book = read_epub(sample_epub)
    root = tmp_path / "root"
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u)
    Project(book, project_dir(book, root)).analyze(LLMAttributor(llm, chunk_chars=60), book.chapters)
    capsys.readouterr()
    main(["status", str(sample_epub), "--workdir", str(root)])
    out = capsys.readouterr().out
    assert "failed passage" in out
    assert "--retry-failed" in out
