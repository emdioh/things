"""EPUB parsing: metadata, cover, and chapters as lists of paragraphs."""

from __future__ import annotations

import posixpath
import re
import warnings
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub

from .models import Book, Chapter

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="ebooklib")
warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib")

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "div", "dd", "dt"}
HEADINGS = {"h1", "h2", "h3"}


def _meta(book: epub.EpubBook, name: str, default: str = "") -> str:
    values = book.get_metadata("DC", name)
    return values[0][0].strip() if values and values[0][0] else default


def _toc_titles(book: epub.EpubBook) -> dict[str, str]:
    """Map document href (without fragment) -> first TOC title pointing at it."""
    titles: dict[str, str] = {}

    def walk(entries):
        for entry in entries:
            if isinstance(entry, tuple):  # (Section, children)
                section, children = entry
                href = getattr(section, "href", None)
                if href:
                    titles.setdefault(href.split("#")[0], section.title)
                walk(children)
            elif isinstance(entry, list):
                walk(entry)
            else:
                href = getattr(entry, "href", None)
                if href:
                    titles.setdefault(href.split("#")[0], entry.title)

    walk(book.toc)
    return titles


def _cover(book: epub.EpubBook) -> tuple[bytes | None, str | None]:
    candidates = list(book.get_items_of_type(ebooklib.ITEM_COVER))
    for _, attrs in book.get_metadata("OPF", "cover"):
        item = book.get_item_with_id(attrs.get("content", ""))
        if item is not None:
            candidates.append(item)
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if "cover" in (item.get_name() + (item.id or "")).lower():
            candidates.append(item)
    for item in candidates:
        mime = item.media_type or ""
        if mime in ("image/jpeg", "image/png"):
            return item.get_content(), mime
    return None, None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("­", "")).strip()


def html_to_blocks(html: bytes | str) -> list[tuple[str, str]]:
    """Extract (tag, text) for each leaf block element, in document order."""
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "nav"]):
        tag.decompose()
    # Footnote markers and footnote bodies would be read aloud as noise.
    for tag in body.find_all(attrs={"epub:type": re.compile(r"noteref|footnote|endnote|pagebreak")}):
        tag.decompose()
    for tag in body.find_all(attrs={"role": re.compile(r"doc-noteref|doc-footnote|doc-pagebreak")}):
        tag.decompose()
    for tag in body.find_all("sup"):
        if tag.find("a") or re.fullmatch(r"\W*\d+\W*", tag.get_text()):
            tag.decompose()
    for br in body.find_all("br"):
        br.replace_with(" ")

    blocks: list[tuple[str, str]] = []
    for el in body.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):
            continue  # not a leaf; its children will be visited
        text = _clean(el.get_text())
        if text and re.search(r"\w", text):
            blocks.append((el.name, text))
    return blocks


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
    except ImportError:
        return False
    return True


def read_epub(path: str | Path) -> Book:
    book = epub.read_epub(str(path), options={"ignore_ncx": False})
    toc = _toc_titles(book)

    chapters: list[Chapter] = []
    for idref, linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if str(linear).lower() == "no":
            continue
        name = item.get_name()
        blocks = html_to_blocks(item.get_content())
        if not blocks:
            continue

        title = toc.get(name) or toc.get(posixpath.basename(name))
        if title is None and chapters and not _starts_with_heading(blocks):
            # A document with no TOC entry and no heading is usually the
            # continuation of a chapter that was split across files.
            chapters[-1].paragraphs.extend(text for _, text in blocks)
            continue
        if title is None and _starts_with_heading(blocks):
            title = blocks[0][1]
        title = _clean(title or f"Chapter {len(chapters) + 1}")
        paragraphs = [text for _, text in blocks]
        if paragraphs and _same(paragraphs[0], title):
            paragraphs = paragraphs[1:]  # the narrator announces the title
        chapters.append(Chapter(index=len(chapters), title=title, paragraphs=paragraphs))

    cover, cover_mime = _cover(book)
    return Book(
        title=_meta(book, "title", Path(path).stem),
        author=_meta(book, "creator", "Unknown"),
        language=_meta(book, "language", "en").lower(),
        chapters=chapters,
        cover=cover,
        cover_mime=cover_mime,
    )


def _starts_with_heading(blocks: list[tuple[str, str]]) -> bool:
    return bool(blocks) and blocks[0][0] in HEADINGS


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"\W+", "", s).casefold()  # noqa: E731
    return norm(a) == norm(b)
