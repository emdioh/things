"""Split paragraphs into narration and dialogue spans.

Handles the common typographic conventions:
  “English”  "straight"  «guillemets»  „German“  ‘single’ (paragraph-initial)
  and dash-introduced dialogue (— Ciao — disse Marco — come stai?) used in
  Italian, Spanish, French, Russian, ... books.
"""

from __future__ import annotations

import re

from .models import Span

PAIRS = {"“": "”", "«": "»", "„": "“", "‹": "›", "「": "」", "『": "』", "»": "«"}
DASHES = "—―–"
_STRIP = " \t,;:"


def _span(text: str, dialogue: bool) -> Span | None:
    text = text.strip()
    if not dialogue:
        text = text.strip(_STRIP)
    if not re.search(r"\w", text):
        return None
    return Span(text, dialogue)


def _merge(spans: list[Span | None]) -> list[Span]:
    out: list[Span] = []
    for s in spans:
        if s is None:
            continue
        if out and out[-1].dialogue == s.dialogue and not s.dialogue:
            out[-1] = Span(f"{out[-1].text} {s.text}", False)
        else:
            out.append(s)
    return out


def _split_dashes(text: str) -> list[Span]:
    body = text.lstrip(DASHES + " ")
    # Dashes that separate speech from narration are surrounded by whitespace
    # or adjacent to punctuation; hyphen-like dashes inside words are not.
    parts = re.split(rf"\s[{DASHES}]\s?|[{DASHES}]\s", body)
    spans = [_span(part, dialogue=(i % 2 == 0)) for i, part in enumerate(parts)]
    return _merge(spans)


def split_paragraph(text: str) -> list[Span]:
    stripped = text.lstrip()
    if stripped[:1] in DASHES:
        return _split_dashes(stripped)

    spans: list[Span | None] = []
    buf: list[str] = []
    closer: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if closer is None:
            opener = None
            if ch in PAIRS and (ch != "»" or "«" in text[i:]):
                opener, close = ch, PAIRS[ch]
            elif ch == '"':
                opener, close = ch, '"'
            elif ch == "‘" and not "".join(buf).strip():
                opener, close = ch, "’"
            if opener is not None:
                spans.append(_span("".join(buf), False))
                buf, closer = [], close
                i += 1
                continue
        elif ch == closer and not (closer == "’" and i + 1 < n and text[i + 1].isalpha()):
            spans.append(_span("".join(buf), True))
            buf, closer = [], None
            i += 1
            continue
        buf.append(ch)
        i += 1
    # An unclosed quote runs to the end of the paragraph (multi-paragraph speech).
    spans.append(_span("".join(buf), closer is not None))
    return _merge(spans)

