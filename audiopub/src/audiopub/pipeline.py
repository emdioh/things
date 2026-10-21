"""Orchestration: analyze (who says what) and render (TTS + assembly), with on-disk caching.

Everything is cached under the work directory, so an interrupted run resumes
where it stopped and a re-run after editing ``cast.<backend>.json`` only
re-renders what changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

from . import audio
from .casting import build_cast, load_cast, save_cast
from .models import NARRATOR, Book, Chapter, Roster, Segment
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


class Project:
    def __init__(self, book: Book, workdir: Path):
        self.book = book
        self.dir = workdir
        for sub in ("script", "audio", "chapters"):
            (workdir / sub).mkdir(parents=True, exist_ok=True)
        self.roster_path = workdir / "characters.json"

    # ---- analysis ---------------------------------------------------------

    def load_roster(self) -> Roster:
        if self.roster_path.exists():
            return Roster.from_list(json.loads(self.roster_path.read_text()))
        return Roster()

    def save_roster(self, roster: Roster) -> None:
        self.roster_path.write_text(json.dumps(roster.to_list(), indent=2, ensure_ascii=False) + "\n")

    def _script_path(self, chapter: Chapter) -> Path:
        return self.dir / "script" / f"ch{chapter.index:03d}.json"

    def load_script(self, chapter: Chapter) -> list[Segment] | None:
        path = self._script_path(chapter)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data.get("source") != _hash(chapter.paragraphs):
            return None
        return [Segment(**s) for s in data["segments"]]

    def analyze(self, attributor, chapters: list[Chapter], force: bool = False) -> dict[int, list[Segment]]:
        roster = self.load_roster()
        scripts: dict[int, list[Segment]] = {}
        for chapter in chapters:
            cached = None if force else self.load_script(chapter)
            if cached is not None:
                scripts[chapter.index] = cached
                continue
            _status(f"Analyzing [{chapter.index + 1}/{len(self.book.chapters)}] {chapter.title}")
            segments = attributor.attribute_chapter(chapter, roster)
            scripts[chapter.index] = segments
            self._script_path(chapter).write_text(json.dumps({
                "title": chapter.title,
                "attributor": attributor.name,
                "source": _hash(chapter.paragraphs),
                "segments": [s.to_dict() for s in segments],
            }, indent=1, ensure_ascii=False))
            self.save_roster(roster)
        _status("")
        return scripts

    # ---- casting ----------------------------------------------------------

    def cast_path(self, tts: TTSBackend) -> Path:
        return self.dir / f"cast.{tts.name}.json"

    def cast(self, tts: TTSBackend, scripts: dict[int, list[Segment]], narrator_voice: str | None = None) -> dict:
        roster = self.load_roster()
        lines = Counter(s.speaker for segs in scripts.values() for s in segs)
        path = self.cast_path(tts)
        existing = load_cast(path)
        if narrator_voice and existing.get(NARRATOR, {}).get("voice") != narrator_voice:
            existing[NARRATOR] = {"voice": narrator_voice, "pitch": 0.0, "speed": 1.0}
        cast = build_cast(existing, roster, lines, tts.voices(self.book.language), narrator_voice)
        save_cast(path, cast)
        return cast

    # ---- rendering --------------------------------------------------------

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

        # 1. Synthesize every distinct (voice, speed, text) once.
        plan: dict[int, list[tuple[Path, dict, int]]] = {}
        jobs: dict[Path, Job] = {}
        for chapter in chapters:
            items = []
            segs = list(scripts[chapter.index])
            if announce_titles:
                segs.insert(0, Segment(NARRATOR, chapter.title, -1))
            for seg in segs:
                voice = cast.get(seg.speaker) or cast[NARRATOR]
                path = self.dir / "audio" / f"{_hash(tts.name, voice['voice'], voice.get('speed', 1.0), seg.text)}.{tts.extension}"
                if not path.exists():
                    jobs[path] = Job(seg.text, voice["voice"], float(voice.get("speed", 1.0)), path)
                items.append((path, voice, seg.paragraph))
            plan[chapter.index] = items

        if jobs:
            done = [0]

            def tick():
                done[0] += 1
                if sys.stderr.isatty() or done[0] % 100 == 0 or done[0] == len(jobs):
                        _status(f"Synthesizing speech {done[0]}/{len(jobs)}")

            tts.render_many(list(jobs.values()), progress=tick)
            _status("")

        # 2. Encode each chapter (cached by content).
        ext = "m4a" if fmt == "m4b" else "mp3"
        encoded: list[tuple[str, Path, float]] = []
        for chapter in chapters:
            items = plan[chapter.index]
            key = _hash(fmt, [(p.name, v.get("pitch", 0.0), para) for p, v, para in items])[:10]
            name = f"ch{chapter.index:03d}-{key}.{ext}"
            target = self.dir / "chapters" / name
            meta = target.with_suffix(".json")
            if target.exists() and meta.exists():
                encoded.append((chapter.title, target, json.loads(meta.read_text())["seconds"]))
                continue
            _status(f"Encoding [{chapter.index + 1}] {chapter.title}")
            enc = audio.ChapterEncoder(target, fmt)
            prev_para = None
            for path, voice, para in items:
                if prev_para is not None:
                    if prev_para == -1:
                        enc.write(audio.silence(PAUSE_AFTER_TITLE))
                    else:
                        enc.write(audio.silence(PAUSE_SAME_PARAGRAPH if para == prev_para else PAUSE_PARAGRAPH))
                enc.write(audio.decode(path, float(voice.get("pitch", 0.0))))
                prev_para = para
            enc.write(audio.silence(PAUSE_END_OF_CHAPTER))
            seconds = enc.close()
            meta.write_text(json.dumps({"seconds": seconds}))
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


def _status(msg: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K" + msg)
        if not msg:
            sys.stderr.write("\r")
        sys.stderr.flush()
    elif msg:
        print(msg, file=sys.stderr)
