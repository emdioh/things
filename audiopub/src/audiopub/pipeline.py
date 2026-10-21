"""Orchestration in resumable steps: analyze -> cast -> render.

Each book gets a project directory (by default under ~/.local/share/audiopub,
keyed by the book's identity, so it is found again from any folder and after
renaming the file):

    book.json            what this project is
    characters.json      the character roster built during analysis
    script/chNNN.json    who says what, per chapter (hand-editable)
    script/chNNN.partial.json   checkpoint of a chapter being analyzed
    cast.<backend>.json  voice per speaker (hand-editable)
    audio/               one file per synthesized line, keyed by content
    chapters/            encoded chapters, keyed by content

Every step only does work whose result is missing, so interrupting and
re-running a command resumes it, and edits only redo what they affect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import audio
from .casting import build_cast, load_cast, pick_voices, save_cast
from .models import NARRATOR, Book, Chapter, Character, Roster, Segment
from .progress import NullReporter
from .tts import TTSBackend
from .tts.base import Job

log = logging.getLogger(__name__)

PAUSE_SAME_PARAGRAPH = 0.25
PAUSE_PARAGRAPH = 0.6
PAUSE_AFTER_TITLE = 1.2
PAUSE_END_OF_CHAPTER = 1.5


def slug(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.U).strip("_")[:80] or "book"


def _hash(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, data, indent: int | None = 1) -> None:
    """Write atomically, so an interrupted run never leaves a truncated file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n")
    tmp.replace(path)


def default_root() -> Path:
    if os.environ.get("AUDIOPUB_HOME"):
        return Path(os.environ["AUDIOPUB_HOME"])
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "audiopub"


def project_dir(book: Book, root: Path | None = None) -> Path:
    key = _hash(book.identifier or "", book.title, book.author)[:8]
    return (root or default_root()) / "books" / f"{slug(book.title)[:60]}-{key}"


def _labels_to_json(labels: dict[tuple[int, int], str]) -> list:
    return [[p, s, who] for (p, s), who in sorted(labels.items())]


def _labels_from_json(data: list) -> dict[tuple[int, int], str]:
    return {(p, s): who for p, s, who in data}


class Checkpoint:
    """Progress through one chapter's analysis.

    ``labels`` maps (paragraph, span) -> speaker, ``done`` counts the paragraphs
    processed, ``failed`` lists (start, end) paragraph ranges where the LLM gave
    no usable answer and heuristics were used, and ``retry`` (when set) asks the
    attributor to re-run just those ranges.
    """

    def __init__(self, path: Path, source: str, roster: Roster, save_roster):
        self.path = path
        self.source = source
        self.roster = roster
        self._save_roster = save_roster
        self.labels: dict[tuple[int, int], str] = {}
        self.done = 0
        self.failed: list[tuple[int, int]] = []
        self.retry: list[tuple[int, int]] | None = None
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("source") == source:
                self.labels = _labels_from_json(data["labels"])
                self.done = data["done"]
                self.failed = [tuple(c) for c in data.get("failed", [])]

    def save(self, labels: dict[tuple[int, int], str], done: int,
             failed: list[tuple[int, int]] | None = None) -> None:
        self.labels, self.done = dict(labels), done
        if failed is not None:
            self.failed = list(failed)
        self._save_roster(self.roster)
        write_json(self.path, {
            "source": self.source,
            "done": done,
            "failed": [list(c) for c in self.failed],
            "labels": _labels_to_json(labels),
        }, indent=None)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass
class ChapterStatus:
    chapter: Chapter
    analysis: str  # "done" | "partial" | "-"
    attributor: str = ""
    paragraphs_done: int = 0
    failed_chunks: int = 0
    lines_total: int = 0
    lines_rendered: int = 0
    encoded: bool = False


class Project:
    def __init__(self, book: Book, workdir: Path, epub_path: Path | None = None):
        self.book = book
        self.dir = workdir
        for sub in ("script", "audio", "chapters"):
            (workdir / sub).mkdir(parents=True, exist_ok=True)
        self.roster_path = workdir / "characters.json"
        manifest = {"title": book.title, "author": book.author, "language": book.language,
                    "identifier": book.identifier, "epub": str(epub_path.resolve()) if epub_path else None}
        manifest_path = workdir / "book.json"
        if not manifest_path.exists() or json.loads(manifest_path.read_text()) != manifest:
            write_json(manifest_path, manifest, indent=2)

    # ---- analysis ---------------------------------------------------------

    def load_roster(self) -> Roster:
        if self.roster_path.exists():
            return Roster.from_list(json.loads(self.roster_path.read_text()))
        return Roster()

    def save_roster(self, roster: Roster) -> None:
        write_json(self.roster_path, roster.to_list(), indent=2)

    def _script_path(self, chapter: Chapter) -> Path:
        return self.dir / "script" / f"ch{chapter.index:03d}.json"

    def _read_script(self, chapter: Chapter) -> dict | None:
        path = self._script_path(chapter)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return data if data.get("source") == _hash(chapter.paragraphs) else None

    def load_script(self, chapter: Chapter) -> list[Segment] | None:
        data = self._read_script(chapter)
        return [Segment(**s) for s in data["segments"]] if data else None

    def load_scripts(self, chapters: list[Chapter]) -> dict[int, list[Segment]]:
        """Scripts for the chapters that have been analyzed (others are left out)."""
        out = {}
        for c in chapters:
            segs = self.load_script(c)
            if segs is not None:
                out[c.index] = segs
        return out

    def failed_chunks(self, chapter: Chapter) -> list[tuple[int, int]]:
        data = self._read_script(chapter)
        return [tuple(c) for c in data.get("failed_chunks", [])] if data else []

    def analyze(self, attributor, chapters: list[Chapter], force: bool = False,
                retry_failed: bool = False, reporter=None) -> dict[int, list[Segment]]:
        reporter = reporter or NullReporter()
        attributor.reporter = reporter
        if force and len(chapters) == len(self.book.chapters):
            # Re-analyzing the whole book: start the character list from scratch too.
            self.roster_path.unlink(missing_ok=True)
        roster = self.load_roster()
        scripts: dict[int, list[Segment]] = {}
        cached_scripts = {} if force else {c.index: self._read_script(c) for c in chapters}
        todo = sum(1 for c in chapters if cached_scripts.get(c.index) is None
                   or (retry_failed and cached_scripts[c.index].get("failed_chunks")))
        reporter.analysis_start(self.book.title, attributor.name, todo, len(chapters) - todo)
        for chapter in chapters:
            label = f"[{chapter.index + 1}/{len(self.book.chapters)}] {chapter.title}"
            source = _hash(chapter.paragraphs)
            checkpoint = Checkpoint(self._script_path(chapter).with_suffix(".partial.json"),
                                    source, roster, self.save_roster)
            cached = cached_scripts.get(chapter.index)
            if cached is not None:
                retry = [tuple(c) for c in cached.get("failed_chunks", [])]
                if not (retry_failed and retry and "labels" in cached):
                    scripts[chapter.index] = [Segment(**s) for s in cached["segments"]]
                    reporter.chapter_cached(label, sum(s.speaker != NARRATOR for s in scripts[chapter.index]),
                                            len(retry))
                    continue
                # Re-run only the passages that fell back to heuristics last time.
                checkpoint.labels = _labels_from_json(cached["labels"])
                checkpoint.done = len(chapter.paragraphs)
                checkpoint.failed = retry
                checkpoint.retry = retry
            elif force:
                checkpoint.clear()
                checkpoint = Checkpoint(checkpoint.path, source, roster, self.save_roster)
            reporter.label = label
            segments = attributor.attribute_chapter(chapter, roster, checkpoint=checkpoint)
            scripts[chapter.index] = segments
            write_json(self._script_path(chapter), {
                "title": chapter.title,
                "attributor": attributor.name,
                "source": source,
                "failed_chunks": [list(c) for c in checkpoint.failed],
                "labels": _labels_to_json(checkpoint.labels),
                "segments": [s.to_dict() for s in segments],
            })
            self.save_roster(roster)
            checkpoint.clear()
            reporter.chapter_done(label, sum(s.speaker != NARRATOR for s in segments),
                                  len({s.speaker for s in segments} - {NARRATOR}), len(checkpoint.failed))
        reporter.analysis_done(len(roster.characters))
        return scripts

    def rename_speaker(self, old: str, new: str) -> int:
        """Rename a speaker everywhere, merging into ``new`` if it already exists.

        Returns the number of lines changed. Works on saved scripts only; nothing
        is sent to the LLM again.
        """
        roster = self.load_roster()
        old_name = roster.resolve(old) or old
        target = roster.resolve(new) or (NARRATOR if new.upper() == NARRATOR else new)

        changed = 0
        for path in sorted((self.dir / "script").glob("ch???.json")):
            data = json.loads(path.read_text())
            hits = [s for s in data["segments"] if s["speaker"] == old_name]
            for s in hits:
                s["speaker"] = target
            for label in data.get("labels", []):  # keep them in sync for --retry-failed
                if label[2] == old_name:
                    label[2] = target
            if hits:
                changed += len(hits)
                write_json(path, data)
        if not changed and old_name not in roster.characters:
            raise SystemExit(f"no speaker named {old!r}; see 'audiopub status --speakers'")

        if old_name in roster.characters and old_name != target:
            gone = roster.characters.pop(old_name)
            rebuilt = Roster(list(roster.characters.values()))
            if target != NARRATOR:
                if target not in rebuilt.characters:  # a plain rename keeps what we knew
                    rebuilt.upsert(Character(target, gone.gender, gone.age, gone.description))
                rebuilt.upsert(Character(target, aliases=[old_name, *gone.aliases]))
                merged = rebuilt.characters[target]
                merged.gender = merged.gender if merged.gender != "unknown" else gone.gender
                merged.age = merged.age if merged.age != "unknown" else gone.age
                merged.description = merged.description or gone.description
            roster = rebuilt
        self.save_roster(roster)

        # Drop the old cast entry; the merged speaker keeps (or gets) its own voice.
        for cast_file in self.dir.glob("cast.*.json"):
            cast = load_cast(cast_file)
            if old_name in cast and old_name != target:
                entry = cast.pop(old_name)
                if target not in cast and target != NARRATOR:
                    cast[target] = entry
                save_cast(cast_file, cast)
        return changed

    # ---- casting ----------------------------------------------------------

    def cast_path(self, tts: TTSBackend) -> Path:
        return self.cast_path_for(tts.name)

    def cast_path_for(self, backend: str) -> Path:
        return self.dir / f"cast.{backend}.json"

    def cast(self, tts: TTSBackend, scripts: dict[int, list[Segment]], narrator_voice: str | None = None,
             reset: bool = False, multilingual: bool = False) -> dict:
        roster = self.load_roster()
        lines = Counter(s.speaker for segs in scripts.values() for s in segs)
        path = self.cast_path(tts)
        existing = {} if reset else load_cast(path)
        if narrator_voice and existing.get(NARRATOR, {}).get("voice") != narrator_voice:
            existing[NARRATOR] = {"voice": narrator_voice, "pitch": 0.0, "speed": 1.0}
        voices = pick_voices(tts.voices(self.book.language), self.book.language, multilingual)
        cast = build_cast(existing, roster, lines, voices, narrator_voice)
        save_cast(path, cast)
        return cast

    # ---- rendering --------------------------------------------------------

    def _plan(self, tts_name: str, ext: str, chapter: Chapter, segments: list[Segment], cast: dict,
              announce_titles: bool) -> list[tuple[Path, dict, int, str]]:
        segs = list(segments)
        if announce_titles:
            segs.insert(0, Segment(NARRATOR, chapter.title, -1))
        items = []
        for seg in segs:
            voice = cast.get(seg.speaker) or cast[NARRATOR]
            name = _hash(tts_name, voice["voice"], voice.get("speed", 1.0), seg.text)
            items.append((self.dir / "audio" / f"{name}.{ext}", voice, seg.paragraph, seg.text))
        return items

    def _chapter_file(self, chapter: Chapter, items, fmt: str) -> Path:
        key = _hash(fmt, [(p.name, v.get("pitch", 0.0), para) for p, v, para, _ in items])[:10]
        return self.dir / "chapters" / f"ch{chapter.index:03d}-{key}.{'m4a' if fmt == 'm4b' else 'mp3'}"

    def render(
        self,
        tts: TTSBackend,
        scripts: dict[int, list[Segment]],
        cast: dict,
        fmt: str = "m4b",
        out_dir: Path | None = None,
        announce_titles: bool = True,
    ) -> Path:
        audio.require_ffmpeg()
        out_dir = out_dir or self.dir
        chapters = [c for c in self.book.chapters if c.index in scripts]
        plan = {c.index: self._plan(tts.name, tts.extension, c, scripts[c.index], cast, announce_titles)
                for c in chapters}

        # 1. Synthesize every line that has no audio yet.
        jobs: dict[Path, Job] = {}
        for items in plan.values():
            for path, voice, _, text in items:
                if not path.exists():
                    jobs[path] = Job(text, voice["voice"], float(voice.get("speed", 1.0)), path)
        if jobs:
            done = [0]

            def tick():
                done[0] += 1
                if sys.stderr.isatty() or done[0] % 100 == 0 or done[0] == len(jobs):
                    _status(f"Synthesizing speech {done[0]}/{len(jobs)}")

            tts.render_many(list(jobs.values()), progress=tick)
            _status("")

        # 2. Encode each chapter that changed.
        encoded: list[tuple[str, Path, float]] = []
        for chapter in chapters:
            items = plan[chapter.index]
            target = self._chapter_file(chapter, items, fmt)
            meta = target.with_suffix(".json")
            if target.exists() and meta.exists():
                encoded.append((chapter.title, target, json.loads(meta.read_text())["seconds"]))
                continue
            _status(f"Encoding [{chapter.index + 1}] {chapter.title}")
            enc = audio.ChapterEncoder(target, fmt)
            prev_para = None
            for path, voice, para, _ in items:
                if prev_para is not None:
                    if prev_para == -1:
                        enc.write(audio.silence(PAUSE_AFTER_TITLE))
                    else:
                        enc.write(audio.silence(PAUSE_SAME_PARAGRAPH if para == prev_para else PAUSE_PARAGRAPH))
                enc.write(audio.decode(path, float(voice.get("pitch", 0.0))))
                prev_para = para
            enc.write(audio.silence(PAUSE_END_OF_CHAPTER))
            seconds = enc.close()
            write_json(meta, {"seconds": seconds})
            encoded.append((chapter.title, target, seconds))
        _status("")

        # 3. Package.
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "m4b":
            out = out_dir / f"{slug(self.book.title)}.m4b"
            _status("Building audiobook")
            audio.build_m4b(encoded, out, self.book.title, self.book.author,
                            self.book.cover, self.book.cover_mime, self.dir)
            _status("")
            return out
        folder = out_dir / slug(self.book.title)
        folder.mkdir(exist_ok=True)
        for n, (title, path, _) in enumerate(encoded, 1):
            (folder / f"{n:03d} - {slug(title)}.mp3").write_bytes(path.read_bytes())
        return folder

    # ---- status -----------------------------------------------------------

    def status(self, chapters: list[Chapter], tts_name: str | None = None, ext: str | None = None,
               fmt: str = "m4b", announce_titles: bool = True) -> list[ChapterStatus]:
        cast = load_cast(self.cast_path_for(tts_name)) if tts_name else {}
        out = []
        for chapter in chapters:
            data = self._read_script(chapter)
            if data is None:
                st = ChapterStatus(chapter, "-")
                partial = self._script_path(chapter).with_suffix(".partial.json")
                if partial.exists():
                    cp = json.loads(partial.read_text())
                    if cp.get("source") == _hash(chapter.paragraphs):
                        st = ChapterStatus(chapter, "partial", paragraphs_done=cp["done"])
                out.append(st)
                continue
            st = ChapterStatus(chapter, "done", attributor=data.get("attributor", ""),
                               failed_chunks=len(data.get("failed_chunks", [])))
            segments = [Segment(**s) for s in data["segments"]]
            if cast and all(s.speaker in cast for s in segments) and NARRATOR in cast:
                items = self._plan(tts_name, ext, chapter, segments, cast, announce_titles)
                st.lines_total = len(items)
                st.lines_rendered = sum(p.exists() for p, *_ in items)
                st.encoded = self._chapter_file(chapter, items, fmt).exists()
            out.append(st)
        return out


def _status(msg: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K" + msg)
        if not msg:
            sys.stderr.write("\r")
        sys.stderr.flush()
    elif msg:
        print(msg, file=sys.stderr)
