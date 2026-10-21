"""Plain data structures shared across the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

NARRATOR = "NARRATOR"

GENDERS = ("male", "female", "unknown")
AGES = ("child", "young", "adult", "elderly", "unknown")


@dataclass
class Chapter:
    index: int
    title: str
    paragraphs: list[str]


@dataclass
class Book:
    title: str
    author: str
    language: str
    chapters: list[Chapter]
    cover: bytes | None = None
    cover_mime: str | None = None


@dataclass
class Span:
    """A piece of a paragraph: either narration or quoted dialogue."""

    text: str
    dialogue: bool


@dataclass
class Segment:
    """A unit of speech: one speaker reading one piece of text."""

    speaker: str
    text: str
    paragraph: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Character:
    name: str
    gender: str = "unknown"
    age: str = "unknown"
    description: str = ""
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Roster:
    """The set of known characters, with alias resolution."""

    def __init__(self, characters: list[Character] | None = None):
        self.characters: dict[str, Character] = {}
        self._alias: dict[str, str] = {}
        for c in characters or []:
            self.upsert(c)

    def resolve(self, name: str) -> str | None:
        return self._alias.get(name.strip().casefold())

    def upsert(self, char: Character) -> str:
        """Add a character or merge it into an existing one; returns the canonical name."""
        canonical = self.resolve(char.name)
        if canonical is None:
            for alias in char.aliases:
                canonical = self.resolve(alias)
                if canonical:
                    break
        if canonical is None:
            canonical = char.name.strip()
            self.characters[canonical] = Character(canonical)
        existing = self.characters[canonical]
        if char.gender != "unknown":
            existing.gender = char.gender
        if char.age != "unknown":
            existing.age = char.age
        if char.description and not existing.description:
            existing.description = char.description
        for alias in [char.name, *char.aliases]:
            alias = alias.strip()
            if alias and alias != canonical and alias not in existing.aliases:
                existing.aliases.append(alias)
        for alias in [canonical, *existing.aliases]:
            self._alias[alias.casefold()] = canonical
        return canonical

    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self.characters.values()]

    @classmethod
    def from_list(cls, data: list[dict]) -> Roster:
        return cls([Character(**d) for d in data])
