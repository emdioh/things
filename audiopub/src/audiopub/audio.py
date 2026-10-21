"""Audio assembly with ffmpeg: decode segments to PCM, encode chapters, build the M4B."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono s16le


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required: install it from https://ffmpeg.org or your package manager")


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def decode(path: Path, pitch: float = 0.0) -> bytes:
    """Decode any audio file to mono 24 kHz s16le PCM, optionally pitch-shifted (semitones)."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if pitch:
        f = 2 ** (pitch / 12)
        cmd += ["-af", f"aresample={SAMPLE_RATE},asetrate={SAMPLE_RATE * f:.0f},aresample={SAMPLE_RATE},atempo={1 / f:.6f}"]
    cmd += ["-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


class ChapterEncoder:
    """Streams PCM into an ffmpeg encoder; tracks the exact duration written."""

    def __init__(self, out: Path, fmt: str, bitrate: str = "64k"):
        self.out = out
        self.tmp = out.with_name(out.stem + ".part" + out.suffix)
        codec = ["-c:a", "aac", "-f", "mp4"] if fmt == "m4b" else ["-c:a", "libmp3lame", "-f", "mp3"]
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "-",
             *codec, "-b:a", bitrate, str(self.tmp)],
            stdin=subprocess.PIPE,
        )
        self.bytes = 0

    def write(self, pcm: bytes) -> None:
        self.proc.stdin.write(pcm)
        self.bytes += len(pcm)

    def close(self) -> float:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed encoding {self.out}")
        self.tmp.replace(self.out)
        return self.bytes / BYTES_PER_SECOND


def _concat_quote(path: Path) -> str:
    """Quote a path for ffmpeg's concat demuxer ('it'\\''s' style escaping)."""
    return "'" + path.resolve().as_posix().replace("'", "'\\''") + "'"


def _ffmeta(s: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def build_m4b(
    chapters: list[tuple[str, Path, float]],
    out: Path,
    title: str,
    author: str,
    cover: bytes | None,
    cover_mime: str | None,
    workdir: Path,
) -> None:
    """Concatenate encoded chapters (title, file, seconds) into a single M4B with chapter markers."""
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file {_concat_quote(p)}\n" for _, p, _ in chapters))

    meta = [";FFMETADATA1", f"title={_ffmeta(title)}", f"artist={_ffmeta(author)}", f"album={_ffmeta(title)}", "genre=Audiobook"]
    t = 0.0
    for name, _, seconds in chapters:
        start, t = t, t + seconds
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(t * 1000)}", f"title={_ffmeta(name)}"]
    meta_file = workdir / "metadata.txt"
    meta_file.write_text("\n".join(meta) + "\n")

    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(meta_file)]
    maps = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover:
        cover_file = workdir / ("cover.png" if cover_mime == "image/png" else "cover.jpg")
        cover_file.write_bytes(cover)
        cmd += ["-i", str(cover_file)]
        maps += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    tmp = out.with_name(out.stem + ".part.m4b")
    subprocess.run([*cmd, *maps, "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)], check=True)
    tmp.replace(out)
