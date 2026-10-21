import io
import re
import logging

import pytest

from audiopub.attribution import LLMAttributor
from audiopub.cli import main
from audiopub.epub import read_epub
from audiopub.llm.base import LLMError, LLMRefusal
from audiopub.models import Character, Roster
from audiopub.pipeline import Project
from audiopub.progress import NullReporter, Reporter, roster_changes, snapshot

from conftest import FakeLLM
from test_retry import FAIL_TEXT, FlakyLLM


def run(sample_epub, tmp_path, llm=None, work="work", detail=True, **kw):
    book = read_epub(sample_epub)
    out = io.StringIO()
    project = Project(book, tmp_path / work)
    llm = llm or FakeLLM()
    project.analyze(LLMAttributor(llm, chunk_chars=60), book.chapters,
                    reporter=Reporter(stream=out, detail=detail), **kw)
    return book, project, llm, out.getvalue()


def test_clean_run_output(sample_epub, tmp_path):
    _, _, _, text = run(sample_epub, tmp_path)
    assert "A Test Book" in text and "fake:llm" in text
    for s in ("[1/2] Chapter One", "[2/2] Chapter Two", "passage 1/", "¶", "quote", "speakers:",
              "+ new character: Anna", "chapter done", "Analysis finished"):
        assert s in text, s
    assert "\x1b[" not in text


def test_plurals(sample_epub, tmp_path):
    _, _, _, text = run(sample_epub, tmp_path)
    assert re.search(r"\b1 quote\b(?!s)", text)
    assert re.search(r"\b[2-9] quotes\b", text)
    assert "1 quotes" not in text
    assert "1 passages" not in text and "1 chapters" not in text


def test_transient_failure_retry(sample_epub, tmp_path):
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u and n == 0)
    _, _, _, text = run(sample_epub, tmp_path, llm=llm)
    assert "retrying (2/3)" in text
    assert "boom" in text
    assert "✗" not in text
    assert "retried request" in text


def test_persistent_failure(sample_epub, tmp_path):
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u)
    _, _, _, text = run(sample_epub, tmp_path, llm=llm)
    assert "retrying (2/3)" in text and "retrying (3/3)" in text
    fail_lines = [ln for ln in text.splitlines() if "✗" in ln]
    assert len(fail_lines) == 1
    assert "--retry-failed" in fail_lines[0] and "boom" in fail_lines[0]
    assert "assigned by heuristics" in text
    assert "via heuristics" in text


def test_refusal(sample_epub, tmp_path):
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u, exc=LLMRefusal)
    _, _, _, text = run(sample_epub, tmp_path, llm=llm)
    assert "✗" in text and "boom" in text
    assert "retrying" not in text


def test_cached_second_run(sample_epub, tmp_path):
    run(sample_epub, tmp_path)
    _, _, llm, text = run(sample_epub, tmp_path)
    assert llm.prompts == []
    assert text.count("already analyzed") == 3 - 1  # one line per chapter (+ header line)
    assert "Nothing to analyze" in text
    assert "Analysis finished" not in text


def test_quiet(sample_epub, tmp_path):
    llm = FlakyLLM(fail_when=lambda u, n: FAIL_TEXT in u)
    _, _, _, text = run(sample_epub, tmp_path, llm=llm, detail=False)
    assert "passage 1/" not in text and "speakers:" not in text
    assert "✓ [1/2] Chapter One:" in text and "✓ [2/2] Chapter Two:" in text
    assert "retrying (2/3)" in text
    assert "✗" in text and "--retry-failed" in text


def test_quiet_cached_lines_hidden(sample_epub, tmp_path):
    run(sample_epub, tmp_path)
    _, _, _, text = run(sample_epub, tmp_path, detail=False)
    assert "already analyzed" not in text.replace("0 chapters to analyze, 2 already done", "")
    assert "Nothing to analyze" in text


def test_roster_changes():
    roster = Roster()
    roster.characters["Marco"] = Character(name="Marco", aliases=[], gender="unknown", age="unknown")
    before = snapshot(roster)
    roster.characters["Anna"] = Character(name="Anna", aliases=[], gender="female", age="young")
    m = roster.characters["Marco"]
    m.aliases.append("Marc")
    m.gender = "male"
    added, updated = roster_changes(before, roster)
    assert [c.name for c in added] == ["Anna"]
    assert len(updated) == 1
    char, notes = updated[0]
    assert char.name == "Marco"
    assert 'also "Marc"' in notes and "male" in notes
    assert roster_changes(snapshot(roster), roster) == ([], [])


def test_null_reporter():
    r = NullReporter()
    assert r.passage_done(1, 2, 3) is None
    r.anything(a=1)
    r.label = "x"
    assert r.label == "x"


def test_llm_gets_real_chapter_title(sample_epub, tmp_path):
    _, _, llm, _ = run(sample_epub, tmp_path)
    assert any("<chapter_title>Chapter One</chapter_title>" in p for p in llm.prompts)
    assert not any("[1/2]" in p or "[2/2]" in p for p in llm.prompts)


@pytest.fixture
def clean_loggers():
    names = ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "anthropic")
    saved = {n: logging.getLogger(n).level for n in names}
    yield names
    for n, lv in saved.items():
        logging.getLogger(n).setLevel(lv)


def test_cli_quiet_no_llm(sample_epub, tmp_path, capsys, clean_loggers):
    main(["-q", "analyze", str(sample_epub), "--no-llm", "--workdir", str(tmp_path / "root")])
    err = capsys.readouterr().err
    assert "Analyzing" in err
    assert "(built-in heuristics, no LLM)" in err


def test_cli_http_loggers_quieted(sample_epub, tmp_path, capsys, clean_loggers):
    for n in clean_loggers:
        logging.getLogger(n).setLevel(logging.INFO)
    main(["analyze", str(sample_epub), "--no-llm", "--workdir", str(tmp_path / "root")])
    for n in clean_loggers:
        assert logging.getLogger(n).level == logging.WARNING


def test_cli_vv_keeps_http_logs(sample_epub, tmp_path, capsys, clean_loggers):
    for n in clean_loggers:
        logging.getLogger(n).setLevel(logging.INFO)
    main(["-vv", "analyze", str(sample_epub), "--no-llm", "--workdir", str(tmp_path / "root")])
    assert logging.getLogger("httpx").level == logging.INFO
