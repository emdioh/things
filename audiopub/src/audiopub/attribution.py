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
from collections import Counter

from .dialogue import split_paragraph
from .llm import LLMClient, LLMError, LLMRefusal
from .models import AGES, GENDERS, NARRATOR, Chapter, Character, Roster, Segment, Span
from .progress import NullReporter, roster_changes, snapshot

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
    reporter = NullReporter()

    def attribute_chapter(self, chapter: Chapter, roster: Roster, checkpoint=None) -> list[Segment]:
        self.reporter.chapter_start(chapter.title, sum(len(p.split()) for p in chapter.paragraphs), 0, 0,
                                    " (built-in heuristics, no LLM)")
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
    def __init__(self, llm: LLMClient, chunk_chars: int = 12000, context_paragraphs: int = 6, attempts: int = 3):
        self.llm = llm
        self.attempts = attempts
        self.reporter = NullReporter()
        self.name = llm.name
        self.chunk_chars = chunk_chars
        self.context_paragraphs = context_paragraphs
        self.fallback = HeuristicAttributor()

    def attribute_chapter(self, chapter: Chapter, roster: Roster, checkpoint=None) -> list[Segment]:
        """Label a chapter chunk by chunk.

        ``checkpoint`` (if given) lets an interrupted chapter resume, records the
        chunks that fell back to heuristics, and - when its ``retry`` list is set -
        re-runs just those chunks on top of previously saved labels.
        """
        spans = chapter_spans(chapter)
        labels: dict[tuple[int, int], str] = dict(checkpoint.labels) if checkpoint else {}
        done = checkpoint.done if checkpoint else 0
        failed: list[tuple[int, int]] = list(checkpoint.failed) if checkpoint else []
        retry = checkpoint.retry if checkpoint else None
        chunks = retry if retry else list(self._chunks(chapter.paragraphs, spans))
        with_dialogue = [c for c in chunks if any(sp.dialogue for p in range(*c) for sp in spans[p])]
        todo = [c for c in with_dialogue if retry or c[1] > done]
        note = ""
        if retry:
            note = f" (retrying {len(todo)} failed passage(s))"
        elif done:
            note = f" (resuming at ¶{done + 1})"
        self.reporter.chapter_start(chapter.title, sum(len(p.split()) for p in chapter.paragraphs),
                                    len(todo), len(chunks) - len(with_dialogue), note)
        for n, (start, end) in enumerate(todo, 1):
            if retry:
                failed = [c for c in failed if c != (start, end)]
            ids: dict[int, tuple[int, int]] = {}
            body = self._render(spans, start, end, ids)
            ctx_start = max(0, start - self.context_paragraphs)
            context = self._render(spans, ctx_start, start, None, labels)
            prompt = self._prompt(chapter, roster, context, body)
            self.reporter.passage_start(n, len(todo), start, end, len(chapter.paragraphs), len(ids),
                                        len(body), self.name)
            before = snapshot(roster)
            got = self._ask(prompt, ids, roster, f"paragraphs {start + 1}-{end} of {chapter.title!r}")
            if got is None:
                failed.append((start, end))
                got = {}
            labels.update(got)
            missing = set(ids.values()) - set(got)
            if missing:
                labels = self.fallback.label(spans, roster, only=missing, given=labels)
            added, updated = roster_changes(before, roster)
            self.reporter.passage_done(Counter(got.values()), len(missing), added, updated)
            if checkpoint:
                checkpoint.save(labels, max(done, end), failed)
        if checkpoint:
            checkpoint.failed = failed
        return _segments(spans, labels)

    def _ask(self, prompt: str, ids: dict, roster: Roster, where: str) -> dict | None:
        """Query the model, retrying unusable replies; None if every attempt failed."""
        for attempt in range(1, self.attempts + 1):
            try:
                return self._apply(self.llm.complete_json(SYSTEM_PROMPT, prompt, SCHEMA), ids, roster)
            except LLMRefusal as e:
                log.info("%s: %s: %s", self.name, where, e)
                self.reporter.passage_failed(str(e))
                return None
            except LLMError as e:
                log.info("%s: %s: attempt %d/%d failed: %s", self.name, where, attempt, self.attempts, e)
                if attempt < self.attempts:
                    self.reporter.retry(attempt + 1, self.attempts, str(e))
                else:
                    self.reporter.passage_failed(f"failed {self.attempts} times ({e})")
        return None

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
