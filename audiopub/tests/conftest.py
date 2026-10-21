import re
import subprocess
from pathlib import Path

import pytest
from ebooklib import epub

from audiopub.tts.base import TTSBackend, Voice

CHAPTERS = [
    ("Chapter One", [
        "It was a cold morning in the village.",
        "“Where are you going?” asked Anna.",
        "“To the market,” Marco replied. “Do you want to come?”",
        "“Of course,” she said.",
    ]),
    ("Chapter Two", [
        "The market was crowded.",
        "“Fresh bread!” shouted the baker.",
    ]),
]


def make_epub(path: Path, chapters=CHAPTERS, language="en") -> Path:
    book = epub.EpubBook()
    book.set_identifier("test-book")
    book.set_title("A Test Book")
    book.set_language(language)
    book.add_author("Jane Writer")
    items = []
    for i, (title, paras) in enumerate(chapters):
        c = epub.EpubHtml(title=title, file_name=f"ch{i}.xhtml", lang=language)
        body = "".join(f"<p>{p}</p>" for p in paras)
        c.content = f"<html><body><h1>{title}</h1>{body}<p>Note<sup><a href='#n1'>1</a></sup> here.</p></body></html>"
        book.add_item(c)
        items.append(c)
    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]
    epub.write_epub(str(path), book)
    return path


@pytest.fixture
def sample_epub(tmp_path) -> Path:
    return make_epub(tmp_path / "book.epub")


class FakeTTS(TTSBackend):
    """Renders a short tone whose frequency depends on the voice."""

    name = "fake"
    extension = "wav"

    def __init__(self):
        self.calls = []

    def voices(self, language):
        return [Voice("narr", "male", "en-US"), Voice("f1", "female", "en-US"),
                Voice("m1", "male", "en-US"), Voice("f2", "female", "en-GB")]

    def render(self, text, voice, speed, out):
        self.calls.append((voice, text))
        freq = 200 + 100 * (abs(hash(voice)) % 5)
        dur = min(2.0, 0.05 * len(text))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:d={dur}",
                        "-ar", "24000", "-ac", "1", "-f", "wav", str(out)], check=True)


@pytest.fixture
def fake_tts():
    return FakeTTS()


class FakeLLM:
    """Labels each <q> by looking for a known name right after it, like a (very) small model."""

    name = "fake:llm"

    def __init__(self):
        self.prompts = []

    def complete_json(self, system, user, schema):
        self.prompts.append(user)
        passage = user.split("<passage>")[1]
        labels = []
        for m in re.finditer(r'<q id="(\d+)">(.*?)</q>([^<]*)', passage):
            after = m.group(3)
            if not after.strip() and labels:
                speaker = labels[-1]["speaker"]
            else:
                speaker = "Anna" if ("Anna" in after or "she" in after) else "Marco" if "Marco" in after else "Baker"
            labels.append({"id": int(m.group(1)), "speaker": speaker})
        return {
            "characters": [
                {"name": "Anna", "aliases": [], "gender": "female", "age": "young", "description": "curious girl"},
                {"name": "Marco", "aliases": ["Marc"], "gender": "male", "age": "adult", "description": ""},
                {"name": "Baker", "aliases": [], "gender": "male", "age": "elderly", "description": "loud baker"},
            ],
            "labels": labels,
        }


@pytest.fixture
def fake_llm():
    return FakeLLM()
