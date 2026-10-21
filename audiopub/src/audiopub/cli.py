"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from .epub import read_epub
from .llm import DEFAULT_LLM, PROVIDERS
from .models import NARRATOR
from .pipeline import Project, slug
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
    p.add_argument("--workdir", type=Path, default=Path("audiopub-work"),
                   help="cache directory for scripts, cast and audio (default: ./audiopub-work)")


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


def _add_tts_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("speech")
    g.add_argument("--tts", choices=BACKENDS, default="edge", help="text-to-speech backend (default: edge)")
    g.add_argument("--tts-model", help="model for the openai backend (default: gpt-4o-mini-tts)")
    g.add_argument("--tts-base-url", help="URL of an OpenAI-compatible speech server")
    g.add_argument("--tts-api-key", help="API key for the openai backend")
    g.add_argument("--narrator-voice", help="voice id for the narrator (see 'audiopub voices')")
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
    project = Project(book, args.workdir / slug(epub.stem))
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
    scripts = project.analyze(attributor, chapters, force=args.reanalyze)
    lines = Counter(s.speaker for segs in scripts.values() for s in segs)
    roster = project.load_roster()
    print(f"\n{len(roster.characters)} characters found. Most frequent speakers:")
    for speaker, n in lines.most_common(25):
        char = roster.characters.get(speaker)
        desc = f"{char.gender}, {char.age}: {char.description}" if char else ""
        print(f"  {speaker[:30]:<30} {n:>5}  {desc}")
    print(f"\nScripts and characters saved in {project.dir}")


def _cast_and_render(args, project: Project, chapters, scripts, render: bool) -> None:
    tts = _tts(args)
    cast = project.cast(tts, scripts, args.narrator_voice)
    _print_cast(project, cast, scripts, project.cast_path(tts))
    if render:
        out = project.render(tts, scripts, cast, fmt=args.format, out_dir=args.out,
                             announce_titles=not args.no_titles)
        print(f"\nDone: {out}")


def _cached_scripts(project: Project, chapters):
    scripts = {}
    for c in chapters:
        segs = project.load_script(c)
        if segs is None:
            return None
        scripts[c.index] = segs
    return scripts


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
        scripts = project.analyze(attributor, chapters, force=args.reanalyze)
        _cast_and_render(args, project, chapters, scripts, render=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="audiopub", description="Turn EPUB books into multi-voice audiobooks.")
    parser.add_argument("-v", "--verbose", action="store_true")
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
    p.set_defaults(func=cmd_cast)

    p = sub.add_parser("render", help="synthesize the audiobook from the analysis and cast")
    _add_book_args(p)
    _add_tts_args(p)
    _add_output_args(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("voices", help="list the voices a TTS backend offers for a language")
    p.add_argument("--lang", default="en", help="language code, e.g. en, it, fr (default: en)")
    _add_tts_args(p)
    p.set_defaults(func=cmd_voices)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\ninterrupted — re-run the same command to resume")


if __name__ == "__main__":
    main()
