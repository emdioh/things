"""Speaker attribution: decide who speaks each piece of dialogue.

Paragraphs are first split deterministically into narration / dialogue spans
(see :mod:`audiopub.dialogue`), so the text that ends up being read aloud never
passes through a model and cannot be altered by one. The attributor only
labels each dialogue span with a speaker and maintains the character roster.
"""

from __future__ import annotations

import html
import json
import logging
import re

from .dialogue import split_paragraph
from .llm import LLMClient, LLMError
from .models import AGES, GENDERS, NARRATOR, Chapter, Character, Roster, Segment, Span

log = logging.getLogger(__name__)

SPEECH_VERBS = (
    "said|asked|replied|answered|cried|shouted|whispered|muttered|murmured|called|exclaimed|added|"
    "continued|began|sighed|laughed|snapped|yelled|told|insisted|declared|demanded|growled|"
    "disse|chiese|rispose|esclamò|gridò|sussurrò|mormorò|aggiunse|domandò|"
    "dijo|preguntó|respondió|gritó|susurró|"
    "dit|demanda|répondit|cria|murmura|"
    "sagte|fragte|antwortete|rief|flüsterte"
)
_NAME = r"((?:[A-Z][\w'’\-]+)(?:\s+[A-Z][\w'’\-]+){0,2})"
_AFTER = re.compile(rf"^\W*(?:{SPEECH_VERBS})\s+{_NAME}")
_BEFORE = re.compile(rf"{_NAME}\s+(?:{SPEECH_VERBS})\b")
_PRONOUN_M = re.compile(r"\b(?:he|lui|él|il|er)\s+(?:" + SPEECH_VERBS + r")\b|\b(?:" + SPEECH_VERBS + r")\s+(?:he)\b", re.I)
_PRONOUN_F = re.compile(r"\b(?:she|lei|ella|elle|sie)\s+(?:" + SPEECH_VERBS + r")\b|\b(?:" + SPEECH_VERBS + r")\s+(?:she)\b", re.I)
_NOT_NAMES = {"I", "He", "She", "They", "We", "You", "It", "The", "A", "Then", "But", "And", "Lui", "Lei"}


def chapter_spans(chapter: Chapter) -> list[list[Span]]:
    return [split_paragraph(p) for p in chapter.paragraphs]


def _segments(spans: list[list[Span]], speaker_of: dict[tuple[int, int], str]) -> list[Segment]:
    out: list[Segment] = []
    for p, para in enumerate(spans):
        for s, span in enumerate(para):
            speaker = speaker_of.get((p, s), NARRATOR) if span.dialogue else NARRATOR
            if out and out[-1].paragraph == p and out[-1].speaker == speaker:
                out[-1].text += " " + span.text
            else:
                out.append(Segment(speaker=speaker, text=span.text, paragraph=p))
    return out


class HeuristicAttributor:
    """No-LLM fallback: 'said X' patterns, then alternation between the last two speakers."""

    name = "heuristic"

    def attribute_chapter(self, chapter: Chapter, roster: Roster) -> list[Segment]:
        spans = chapter_spans(chapter)
        return _segments(spans, self.label(spans, roster))

    def label(self, spans: list[list[Span]], roster: Roster, only: set[tuple[int, int]] | None = None,
              given: dict[tuple[int, int], str] | None = None) -> dict[tuple[int, int], str]:
        labels: dict[tuple[int, int], str] = dict(given or {})
        recent: list[str] = []
        for p, para in enumerate(spans):
            para_speaker = None
            for s, span in enumerate(para):
                if not span.dialogue:
                    continue
                key = (p, s)
                if key in labels:
                    speaker = labels[key]
                elif only is not None and key not in only:
                    continue
                else:
                    speaker = para_speaker or self._guess(para, s, roster) or self._alternate(recent)
                    labels[key] = speaker
                para_speaker = speaker
                if speaker != NARRATOR and (not recent or recent[-1] != speaker):
                    recent.append(speaker)
        return labels

    @staticmethod
    def _guess(para: list[Span], s: int, roster: Roster) -> str | None:
        neighbours = [para[i].text for i in (s + 1, s - 1) if 0 <= i < len(para) and not para[i].dialogue]
        for text in neighbours:
            for rx in (_AFTER, _BEFORE):
                m = rx.search(text)
                if m and m.group(1).split()[0] not in _NOT_NAMES:
                    return roster.upsert(Character(m.group(1)))
            if _PRONOUN_F.search(text):
                return roster.upsert(Character("Unnamed woman", gender="female"))
            if _PRONOUN_M.search(text):
                return roster.upsert(Character("Unnamed man", gender="male"))
        return None

    @staticmethod
    def _alternate(recent: list[str]) -> str:
        if len(recent) >= 2:
            return recent[-2]
        return "Unknown speaker"


SYSTEM_PROMPT = """You are casting an audiobook. You will receive a passage from a book in which every piece of quoted dialogue is wrapped in a tag like <q id="12">…</q>. Your job is to say who speaks each tagged piece, so that each character can be read by a different voice actor.

For every <q id> in the passage, give the speaker's canonical name:
- Use the exact name of a character from the known-characters list when it is the same person, even if the text refers to them by a nickname, title, or pronoun. Resolve pronouns and alternating unattributed lines from conversational context.
- For a speaker not in the list, invent a short canonical name (their full name if known, otherwise a descriptive label such as "Innkeeper" or "Old woman") and describe them in `characters`.
- A first-person narrator who speaks dialogue is a character too: use their name, or "Narrator (character)" if it is never given.
- Use "NARRATOR" only for quoted text that nobody speaks aloud: titles, signs, written words read by no one, scare quotes, quoted terms.
- Thoughts and letters read by a character are attributed to that character.

In `characters`, list every character who is new in this passage, plus known characters for whom you learned something new (a gender, age, or alias). Give gender as male/female/unknown and age as child/young/adult/elderly/unknown, inferred from the text when possible (names, pronouns, titles, relations). `description` is a short phrase useful for picking a voice ("gruff old sailor", "excitable teenage girl"). `aliases` lists other names the text uses for them.

The passage may be in any language; keep names as written in the book. Lines in <context> were already labelled and are only there to help you follow the conversation."""

SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "gender": {"type": "string", "enum": list(GENDERS)},
                    "age": {"type": "string", "enum": list(AGES)},
                    "description": {"type": "string"},
                },
                "required": ["name", "aliases", "gender", "age", "description"],
                "additionalProperties": False,
            },
        },
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "speaker": {"type": "string"}},
                "required": ["id", "speaker"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["characters", "labels"],
    "additionalProperties": False,
}


class LLMAttributor:
    def __init__(self, llm: LLMClient, chunk_chars: int = 12000, context_paragraphs: int = 6):
        self.llm = llm
        self.name = llm.name
        self.chunk_chars = chunk_chars
        self.context_paragraphs = context_paragraphs
        self.fallback = HeuristicAttributor()

    def attribute_chapter(self, chapter: Chapter, roster: Roster) -> list[Segment]:
        spans = chapter_spans(chapter)
        labels: dict[tuple[int, int], str] = {}
        for start, end in self._chunks(chapter.paragraphs, spans):
            ids: dict[int, tuple[int, int]] = {}
            body = self._render(spans, start, end, ids)
            if not ids:
                continue
            ctx_start = max(0, start - self.context_paragraphs)
            context = self._render(spans, ctx_start, start, None, labels)
            prompt = self._prompt(chapter, roster, context, body)
            try:
                result = self.llm.complete_json(SYSTEM_PROMPT, prompt, SCHEMA)
                got = self._apply(result, ids, roster)
            except LLMError as e:
                log.warning("%s: chunk %d-%d of %r failed (%s); using heuristics", self.name, start, end, chapter.title, e)
                got = {}
            labels.update(got)
            missing = set(ids.values()) - set(got)
            if missing:
                if got:
                    log.info("%d dialogue span(s) left unlabelled; using heuristics", len(missing))
                labels = self.fallback.label(spans, roster, only=missing, given=labels)
        return _segments(spans, labels)

    def _chunks(self, paragraphs: list[str], spans: list[list[Span]]):
        start, size = 0, 0
        for i, para in enumerate(paragraphs):
            size += len(para)
            if size >= self.chunk_chars:
                yield start, i + 1
                start, size = i + 1, 0
        if start < len(paragraphs):
            yield start, len(paragraphs)

    @staticmethod
    def _render(spans, start, end, ids: dict | None, labels: dict | None = None) -> str:
        lines = []
        for p in range(start, end):
            parts = []
            for s, span in enumerate(spans[p]):
                text = html.escape(span.text, quote=False)
                if not span.dialogue:
                    parts.append(text)
                elif ids is not None:
                    qid = len(ids) + 1
                    ids[qid] = (p, s)
                    parts.append(f'<q id="{qid}">{text}</q>')
                else:
                    who = html.escape((labels or {}).get((p, s), "?"))
                    parts.append(f'<q speaker="{who}">{text}</q>')
            lines.append(" ".join(parts))
        return "\n\n".join(lines)

    @staticmethod
    def _prompt(chapter: Chapter, roster: Roster, context: str, body: str) -> str:
        known = [
            {k: v for k, v in c.to_dict().items() if v not in ("", [], "unknown")}
            for c in roster.characters.values()
        ]
        return (
            f"<known_characters>\n{json.dumps(known, ensure_ascii=False)}\n</known_characters>\n\n"
            f"<chapter_title>{html.escape(chapter.title)}</chapter_title>\n\n"
            + (f"<context>\n{context}\n</context>\n\n" if context else "")
            + f"<passage>\n{body}\n</passage>"
        )

    @staticmethod
    def _apply(result: dict, ids: dict[int, tuple[int, int]], roster: Roster) -> dict[tuple[int, int], str]:
        for c in result.get("characters", []):
            name = (c.get("name") or "").strip()
            if not name or name.upper() == NARRATOR:
                continue
            roster.upsert(Character(
                name=name,
                gender=c.get("gender") if c.get("gender") in GENDERS else "unknown",
                age=c.get("age") if c.get("age") in AGES else "unknown",
                description=c.get("description") or "",
                aliases=[a for a in c.get("aliases") or [] if isinstance(a, str)],
            ))
        out: dict[tuple[int, int], str] = {}
        for item in result.get("labels", []):
            key = ids.get(item.get("id")) if isinstance(item, dict) else None
            speaker = (item.get("speaker") or "").strip() if key else ""
            if not speaker:
                continue
            if speaker.upper() == NARRATOR:
                out[key] = NARRATOR
            else:
                out[key] = roster.resolve(speaker) or roster.upsert(Character(speaker))
        return out
