from __future__ import annotations

import abc
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Voice:
    id: str
    gender: str  # male / female / unknown
    locale: str  # e.g. "en-US"; "*" for multilingual voices

    def matches_language(self, language: str) -> bool:
        return self.locale.lower().split("-")[0] == language.lower().split("-")[0]


@dataclass(frozen=True)
class Job:
    text: str
    voice: str
    speed: float
    out: Path


class TTSBackend(abc.ABC):
    name: str
    extension: str  # file extension of rendered audio, e.g. "mp3"
    concurrency: int = 1

    @abc.abstractmethod
    def voices(self, language: str) -> list[Voice]:
        """Voices usable for a book in ``language``; most suitable first."""

    @abc.abstractmethod
    def render(self, text: str, voice: str, speed: float, out: Path) -> None:
        """Synthesize ``text`` into the audio file ``out``."""

    def render_many(self, jobs: list[Job], progress=None) -> None:
        def run(job: Job):
            tmp = job.out.with_suffix(".part" + job.out.suffix)
            for attempt in range(3):
                try:
                    self.render(job.text, job.voice, job.speed, tmp)
                    tmp.replace(job.out)
                    break
                except Exception as e:  # noqa: BLE001 - network TTS fails in many ways
                    if attempt == 2:
                        raise
                    log.warning("TTS failed (%s), retrying: %.60r", e, job.text)
            if progress:
                progress()

        if self.concurrency <= 1:
            for job in jobs:
                run(job)
        else:
            with ThreadPoolExecutor(self.concurrency) as pool:
                list(pool.map(run, jobs))
