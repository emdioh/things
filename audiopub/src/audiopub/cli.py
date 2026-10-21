"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from .env import load_env, user_env_file
from .epub import read_epub
from .llm import DEFAULT_LLM, PROVIDERS
from .models import NARRATOR
from .pipeline import Project, default_root, project_dir
from .progress import Reporter
from .tts import BACKENDS, make_tts


def parse_selection(spec: str | None, count: int) -> list[int]:
    """'1-3,7' -> [0, 1, 2, 6] (chapters are numbered from 1 on the command line)."""
    if not spec:
        return list(range(count))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, (int(b) if b else count)))
        elif part:
            out.append(int(part) - 1)
    return sorted({i for i in out if 0 <= i < count})


def _add_book_args(p: argparse.ArgumentParser, multi: bool = False) -> None:
    if multi:
        p.add_argument("epubs", nargs="+", type=Path, help="EPUB file(s)")
    else:
        p.add_argument("epub", type=Path, help="EPUB file")
    p.add_argument("--chapters", help="chapters to process, e.g. '1-3,7' (default: all; see 'audiopub chapters')")
    p.add_argument("--workdir", type=Path,
                   help="where projects (analysis, cast, audio) are stored "
                        f"(default: $AUDIOPUB_HOME or {default_root()})")


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("speaker attribution")
    g.add_argument("--llm", default=DEFAULT_LLM,
                   help=f"provider:model used to identify speakers (default: {DEFAULT_LLM}). "
                        f"Providers: {', '.join(PROVIDERS)}")
    g.add_argument("--llm-base-url", help="override the provider's API URL (any OpenAI-compatible server)")
    g.add_argument("--llm-api-key", help="API key (default: the provider's usual environment variable)")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   help="reasoning effort, for models that support it")
    g.add_argument("--no-llm", action="store_true", help="use simple built-in heuristics instead of an LLM")
    g.add_argument("--reanalyze", action="store_true", help="ignore cached speaker attribution")
    g.add_argument("--retry-failed", action="store_true",
                   help="re-run only the passages where the LLM failed and heuristics were used")


def _add_tts_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("speech")
    g.add_argument("--tts", choices=BACKENDS, default="edge", help="text-to-speech backend (default: edge)")
    g.add_argument("--tts-model", help="model for the openai backend (default: gpt-4o-mini-tts)")
    g.add_argument("--tts-base-url", help="URL of an OpenAI-compatible speech server")
    g.add_argument("--tts-api-key", help="API key for the openai backend")
    g.add_argument("--narrator-voice", help="voice id for the narrator (see 'audiopub voices')")
    g.add_argument("--multilingual-voices", action="store_true",
                   help="when casting, also use other languages' multilingual voices (more variety, "
                        "but they speak the book's language with a foreign accent)")
    g.add_argument("--concurrency", type=int, default=4, help="parallel TTS requests (default: 4)")


def _add_output_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("--format", choices=["m4b", "mp3"], default="m4b",
                   help="m4b: one audiobook file with chapters; mp3: one file per chapter")
    g.add_argument("-o", "--out", type=Path, default=Path("."), help="output directory (default: .)")
    g.add_argument("--no-titles", action="store_true", help="don't announce chapter titles")


def _attributor(args):
    from .attribution import HeuristicAttributor, LLMAttributor
    from .llm import make_llm

    if args.no_llm:
        return HeuristicAttributor()
    llm = make_llm(args.llm, base_url=args.llm_base_url, api_key=args.llm_api_key, effort=args.effort)
    return LLMAttributor(llm)


def _tts(args):
    return make_tts(args.tts, model=args.tts_model, base_url=args.tts_base_url,
                    api_key=args.tts_api_key, concurrency=args.concurrency)


def _open(epub: Path, args):
    book = read_epub(epub)
    project = Project(book, project_dir(book, args.workdir), epub)
    chapters = [book.chapters[i] for i in parse_selection(args.chapters, len(book.chapters))]
    return book, project, chapters


def _print_cast(project: Project, cast: dict, scripts, cast_file: Path) -> None:
    roster = project.load_roster()
    lines = Counter(s.speaker for segs in scripts.values() for s in segs)
    print(f"\n{'speaker':<28} {'lines':>5}  {'gender':<8} {'voice':<34} pitch  speed")
    for speaker in [NARRATOR] + [s for s, _ in lines.most_common() if s != NARRATOR]:
        entry = cast.get(speaker, {})
        char = roster.characters.get(speaker)
        gender = char.gender if char else ""
        print(f"{speaker[:28]:<28} {lines[speaker]:>5}  {gender:<8} {entry.get('voice', '?'):<34} "
              f"{entry.get('pitch', 0):+5.1f}  {entry.get('speed', 1.0):.2f}")
    print(f"\nEdit {cast_file} to change voices, then run 'audiopub render'.")


def cmd_chapters(args) -> None:
    book = read_epub(args.epub)
    print(f"{book.title} — {book.author} [{book.language}]")
    for c in book.chapters:
        words = sum(len(p.split()) for p in c.paragraphs)
        print(f"{c.index + 1:>4}. {c.title[:60]:<60} {words:>7} words")


def cmd_voices(args) -> None:
    tts = _tts(args)
    for v in tts.voices(args.lang):
        print(f"{v.id:<40} {v.gender:<8} {v.locale}")


def cmd_analyze(args) -> None:
    attributor = _attributor(args)
    book, project, chapters = _open(args.epub, args)
    scripts = project.analyze(attributor, chapters, force=args.reanalyze, retry_failed=args.retry_failed,
                               reporter=Reporter(detail=not args.quiet))
    lines = Counter(s.speaker for segs in scripts.values() for s in segs)
    roster = project.load_roster()
    print(f"\n{len(roster.characters)} characters found. Most frequent speakers:")
    for speaker, n in lines.most_common(25):
        char = roster.characters.get(speaker)
        desc = f"{char.gender}, {char.age}: {char.description}" if char else ""
        print(f"  {speaker[:30]:<30} {n:>5}  {desc}")
    print(f"\nScripts and characters saved in {project.dir}")
    _warn_failed(project, chapters)


def _cast_and_render(args, project: Project, chapters, scripts, render: bool) -> None:
    tts = _tts(args)
    cast = project.cast(tts, scripts, args.narrator_voice, reset=getattr(args, "reset", False),
                        multilingual=args.multilingual_voices)
    _print_cast(project, cast, scripts, project.cast_path(tts))
    if render:
        out = project.render(tts, scripts, cast, fmt=args.format, out_dir=args.out,
                             announce_titles=not args.no_titles)
        print(f"\nDone: {out}")


def _cached_scripts(project: Project, chapters):
    scripts = project.load_scripts(chapters)
    missing = [c.index + 1 for c in chapters if c.index not in scripts]
    if missing:
        listed = ", ".join(map(str, missing[:10])) + (" ..." if len(missing) > 10 else "")
        raise SystemExit(f"chapters not analyzed yet: {listed}. Run 'audiopub analyze' first "
                         "(it resumes where it stopped), or limit with --chapters.")
    return scripts


def _warn_failed(project: Project, chapters) -> None:
    failed = {c.index + 1: project.failed_chunks(c) for c in chapters}
    failed = {n: ranges for n, ranges in failed.items() if ranges}
    if failed:
        where = "; ".join(f"chapter {n}: paragraphs " + ", ".join(f"{a + 1}-{b}" for a, b in ranges)
                          for n, ranges in failed.items())
        print(f"\nThe LLM failed on some passages and heuristics were used instead ({where}).\n"
              "Re-run with --retry-failed to try those passages again.")


def cmd_status(args) -> None:
    book, project, chapters = _open(args.epub, args)
    tts_name, ext = args.tts, {"edge": "mp3", "kokoro": "wav", "openai": "mp3"}[args.tts]
    rows = project.status(chapters, tts_name, ext, fmt=args.format, announce_titles=not args.no_titles)
    print(f"{book.title} — {book.author}\nproject: {project.dir}\n")
    print(f"{'#':>4}  {'chapter':<40} {'analysis':<26} {'audio (' + tts_name + ')':<16} encoded")
    for r in rows:
        if r.analysis == "done":
            analysis = f"done ({r.attributor})"
            if r.failed_chunks:
                analysis = f"done, {r.failed_chunks} failed passage{'s' if r.failed_chunks > 1 else ''}"
        elif r.analysis == "partial":
            analysis = f"partial {r.paragraphs_done}/{len(r.chapter.paragraphs)} par."
        else:
            analysis = "-"
        lines = f"{r.lines_rendered}/{r.lines_total}" if r.lines_total else "-"
        print(f"{r.chapter.index + 1:>4}  {r.chapter.title[:40]:<40} {analysis[:26]:<26} {lines:<16} "
              f"{'yes' if r.encoded else '-'}")
    done = sum(r.analysis == "done" for r in rows)
    if any(r.failed_chunks for r in rows):
        print("\nSome passages fell back to heuristics; 'audiopub analyze --retry-failed' re-runs just those.")
    print(f"\n{done}/{len(rows)} chapters analyzed", end="")
    cast_file = project.cast_path_for(tts_name)
    if cast_file.exists():
        print(f"\ncast ({tts_name}): {cast_file}", end="")
    else:
        print(f"; no cast for '{tts_name}' yet (run 'audiopub cast')", end="")
    print()
    if args.speakers:
        scripts = project.load_scripts(chapters)
        lines = Counter(s.speaker for segs in scripts.values() for s in segs)
        roster = project.load_roster()
        print(f"\n{'speaker':<30} {'lines':>5}  details")
        for speaker, n in lines.most_common():
            char = roster.characters.get(speaker)
            details = ""
            if char:
                details = f"{char.gender}, {char.age}"
                if char.aliases:
                    details += f"; aka {', '.join(char.aliases)}"
                if char.description:
                    details += f"; {char.description}"
            print(f"{speaker[:30]:<30} {n:>5}  {details}")


def cmd_speaker(args) -> None:
    book, project, _ = _open(args.epub, args)
    changed = project.rename_speaker(args.old, args.new)
    print(f"{changed} line(s) reassigned from {args.old!r} to {args.new!r}. "
          "Run 'audiopub render' to update the audio (only affected lines are re-synthesized).")


def cmd_cast(args) -> None:
    book, project, chapters = _open(args.epub, args)
    scripts = _cached_scripts(project, chapters)
    if scripts is None:
        raise SystemExit("run 'audiopub analyze' first (some chapters have no speaker attribution yet)")
    _cast_and_render(args, project, chapters, scripts, render=False)


def cmd_render(args) -> None:
    book, project, chapters = _open(args.epub, args)
    scripts = _cached_scripts(project, chapters)
    if scripts is None:
        raise SystemExit("run 'audiopub analyze' first (some chapters have no speaker attribution yet)")
    _cast_and_render(args, project, chapters, scripts, render=True)


def cmd_convert(args) -> None:
    attributor = _attributor(args)
    for epub in args.epubs:
        print(f"== {epub}")
        book, project, chapters = _open(epub, args)
        scripts = project.analyze(attributor, chapters, force=args.reanalyze, retry_failed=args.retry_failed,
                                  reporter=Reporter(detail=not args.quiet))
        _warn_failed(project, chapters)
        _cast_and_render(args, project, chapters, scripts, render=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="audiopub", description="Turn EPUB books into multi-voice audiobooks.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v: debug details from audiopub; -vv: also every HTTP request")
    parser.add_argument("-q", "--quiet", action="store_true", help="one progress line per chapter only")
    parser.add_argument("--env-file", type=Path,
                        help=f"read API keys from this file (default: ./.env, then {user_env_file()})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("convert", help="analyze + cast + render, for one or more books")
    _add_book_args(p, multi=True)
    _add_llm_args(p)
    _add_tts_args(p)
    _add_output_args(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("chapters", help="list the chapters found in a book")
    p.add_argument("epub", type=Path)
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser("analyze", help="identify characters and who speaks each line")
    _add_book_args(p)
    _add_llm_args(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("cast", help="assign voices to characters (editable JSON)")
    _add_book_args(p)
    _add_tts_args(p)
    p.add_argument("--reset", action="store_true",
                   help="re-assign every voice from scratch (discards edits to the cast file)")
    p.set_defaults(func=cmd_cast)

    p = sub.add_parser("render", help="synthesize the audiobook from the analysis and cast")
    _add_book_args(p)
    _add_tts_args(p)
    _add_output_args(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("status", help="show what's been analyzed and rendered (and where it's stored)")
    _add_book_args(p)
    p.add_argument("--tts", choices=BACKENDS, default="edge", help="backend whose audio progress to show")
    p.add_argument("--format", choices=["m4b", "mp3"], default="m4b")
    p.add_argument("--no-titles", action="store_true")
    p.add_argument("--speakers", action="store_true", help="also list all speakers and their line counts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("speaker", help="rename a speaker, or merge one into another, across the book")
    _add_book_args(p)
    p.add_argument("old", help="current speaker name (or alias)")
    p.add_argument("new", help="new name, or an existing character to merge into (NARRATOR for non-speech)")
    p.set_defaults(func=cmd_speaker)

    p = sub.add_parser("voices", help="list the voices a TTS backend offers for a language")
    p.add_argument("--lang", default="en", help="language code, e.g. en, it, fr (default: en)")
    _add_tts_args(p)
    p.set_defaults(func=cmd_voices)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.verbose < 2:
        # HTTP client libraries log one INFO line per request ("HTTP/1.1 200 OK").
        for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    for path in load_env(args.env_file):
        logging.info("loaded settings from %s", path)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\ninterrupted — re-run the same command to resume")


if __name__ == "__main__":
    main()
