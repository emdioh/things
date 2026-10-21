"""Human-readable progress reporting for long-running steps.

The pipeline and attributors call these hooks; ``Reporter`` prints them to
stderr (with colour on a terminal), ``NullReporter`` ignores them.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter

from .models import NARRATOR, Character


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


class NullReporter:
    def __getattr__(self, name):
        return lambda *a, **k: None


def snapshot(roster) -> dict[str, tuple]:
    return {n: (frozenset(c.aliases), c.gender, c.age) for n, c in roster.characters.items()}


def roster_changes(before: dict[str, tuple], roster) -> tuple[list[Character], list[tuple[Character, list[str]]]]:
    """Characters added since ``before``, and existing ones with new details (as text)."""
    added, updated = [], []
    for name, char in roster.characters.items():
        if name not in before:
            added.append(char)
            continue
        aliases, gender, age = before[name]
        notes = [f'also "{a}"' for a in char.aliases if a not in aliases]
        if char.gender != gender:
            notes.append(char.gender)
        if char.age != age:
            notes.append(char.age)
        if notes:
            updated.append((char, notes))
    return added, updated


class Reporter:
    def __init__(self, stream=None, detail: bool = True):
        self.out = stream or sys.stderr
        self.detail = detail
        color = self.out.isatty() and not os.environ.get("NO_COLOR")
        self._c = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda code, s: s)
        self._t0 = time.monotonic()
        self._totals = Counter()
        self.label: str | None = None  # numbered chapter label set by the pipeline

    def _p(self, msg: str = "") -> None:
        print(msg, file=self.out, flush=True)

    def ok(self, s): return self._c("32", s)
    def warn(self, s): return self._c("33", s)
    def bad(self, s): return self._c("31", s)
    def dim(self, s): return self._c("2", s)
    def bold(self, s): return self._c("1", s)
    def new(self, s): return self._c("36", s)

    # ---- analysis ---------------------------------------------------------

    def analysis_start(self, title: str, attributor: str, todo: int, cached: int) -> None:
        self._t0 = time.monotonic()
        self._totals.clear()
        self._p(self.bold(f"Analyzing “{title}” with {attributor}"))
        self._todo = todo
        self._p(self.dim(f"  {plural(todo, 'chapter')} to analyze, {cached} already done"))

    def chapter_cached(self, label: str, quotes: int, failed: int) -> None:
        if not self.detail:
            return
        extra = f", {plural(failed, 'failed passage')} — use --retry-failed" if failed else ""
        self._p(self.dim(f"✓ {label}: already analyzed ({plural(quotes, 'quote')}{extra})"))

    def chapter_start(self, label: str, words: int, passages: int, skipped: int, note: str = "") -> None:
        self._p()
        info = f"{plural(words, 'word')}, {plural(passages, 'passage')} with dialogue to send"
        if skipped:
            info += f", {skipped} without dialogue skipped"
        self._p(self.bold(self.label or label) + self.dim(f" — {info}") + (self.warn(note) if note else ""))

    def passage_start(self, n: int, total: int, start: int, end: int, paragraphs: int,
                      quotes: int, chars: int, model: str) -> None:
        self._passage_t0 = time.monotonic()
        if self.detail:
            self._p(f"  passage {n}/{total} · ¶{start + 1}–{end} of {paragraphs} · {plural(quotes, 'quote')} · "
                    f"{plural(chars, 'char')} → {model}")

    def retry(self, attempt: int, attempts: int, error: str) -> None:
        self._totals["retries"] += 1
        self._p(self.warn(f"    ⚠ {error}") + self.dim(f" — retrying ({attempt}/{attempts})"))

    def passage_failed(self, reason: str) -> None:
        self._totals["failed"] += 1
        self._p(self.bad(f"    ✗ {reason} — this passage uses the built-in heuristics "
                         "(redo later with --retry-failed)"))

    def passage_done(self, labelled: Counter, heuristic: int, added, updated) -> None:
        self._totals["quotes"] += sum(labelled.values()) + heuristic
        self._totals["new"] += len(added)
        if not self.detail:
            return
        secs = time.monotonic() - getattr(self, "_passage_t0", time.monotonic())
        speakers = ", ".join(f"{'narrator' if s == NARRATOR else s} ×{n}" for s, n in labelled.most_common())
        if labelled:
            line = self.ok(f"    ✓ {plural(sum(labelled.values()), 'quote')} labelled") + self.dim(f" in {secs:.1f}s")
            if heuristic:
                line += self.warn(f" · {heuristic} left to heuristics")
            self._p(line)
        elif heuristic:
            self._p(self.dim(f"    {plural(heuristic, 'quote')} assigned by heuristics"))
        if speakers:
            self._p(self.dim(f"      speakers: {speakers}"))
        for c in added:
            facts = ", ".join(x for x in (c.gender, c.age) if x != "unknown")
            desc = f" — {c.description}" if c.description else ""
            self._p(self.new(f"      + new character: {c.name}") + self.dim(f" ({facts or '?'}){desc}"))
        for c, notes in updated:
            self._p(self.dim(f"      ~ {c.name}: {', '.join(notes)}"))

    def chapter_done(self, label: str, quotes: int, speakers: int, failed: int) -> None:
        what = f"{plural(quotes, 'quote')}, {plural(speakers, 'speaker')}"
        msg = f"  ✓ {label}: {what}" if not self.detail else f"  ✓ chapter done: {what}"
        if failed:
            self._p(self.warn(msg + f", {plural(failed, 'passage')} via heuristics"))
        else:
            self._p(self.ok(msg))

    def analysis_done(self, characters: int) -> None:
        if not getattr(self, "_todo", 1):
            self._p(self.dim(f"Nothing to analyze; {plural(characters, 'character')} known."))
            return
        secs = int(time.monotonic() - self._t0)
        t = self._totals
        parts = [f"{plural(t['quotes'], 'quote')} labelled",
                 f"{plural(characters, 'character')} known ({t['new']} new)"]
        if t["retries"]:
            parts.append(plural(t["retries"], "retried request"))
        if t["failed"]:
            parts.append(self.warn(f"{plural(t['failed'], 'passage')} via heuristics"))
        self._p()
        self._p(self.bold(f"Analysis finished in {secs // 60}m{secs % 60:02d}s: ") + ", ".join(parts))
