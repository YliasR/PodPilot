"""Tag reading and format conversion for files headed to the iPod."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import mutagen

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".oga",
    ".opus", ".wma", ".ape", ".wv", ".mka", ".webm", ".alac",
}

# Extensions the click-wheel iPods play natively, mapped to iTunesDB filetype codes.
NATIVE = {".mp3": "MP3 ", ".m4a": "M4A ", ".m4b": "M4A ", ".wav": "WAV ", ".aif": "AIFF", ".aiff": "AIFF"}
MAX_SAMPLE_RATE = 48000


class MediaError(Exception):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _first(tags, *keys) -> str:
    for key in keys:
        value = tags.get(key) if tags else None
        if value:
            return str(value[0]).strip()
    return ""


def _number_pair(text: str) -> tuple[int, int]:
    """Parses '3', '3/12' or '' into (number, total)."""
    parts = (text or "").split("/")
    out = []
    for p in parts[:2]:
        try:
            out.append(int(p.strip()))
        except ValueError:
            out.append(0)
    while len(out) < 2:
        out.append(0)
    return out[0], out[1]


def read_metadata(path: Path) -> dict:
    """Returns the tag and stream info the iTunesDB needs for `path`."""
    try:
        easy = mutagen.File(path, easy=True)
    except Exception as exc:  # mutagen raises many different types for broken files
        raise MediaError(f"can't read {path.name}: {exc}") from exc
    if easy is None:
        raise MediaError(f"{path.name} is not a recognised audio file")

    tags = easy.tags or {}
    info = easy.info
    track, tracks = _number_pair(_first(tags, "tracknumber"))
    disc, discs = _number_pair(_first(tags, "discnumber"))
    year_text = _first(tags, "date", "originaldate", "year")
    codec = getattr(info, "codec", "") or ""

    return {
        "title": _first(tags, "title") or path.stem,
        "artist": _first(tags, "artist"),
        "album": _first(tags, "album"),
        "album_artist": _first(tags, "albumartist", "album artist"),
        "genre": _first(tags, "genre"),
        "composer": _first(tags, "composer"),
        "compilation": _first(tags, "compilation") in ("1", "true", "True"),
        "year": int(year_text[:4]) if year_text[:4].isdigit() else 0,
        "track_number": track,
        "track_count": tracks,
        "disc_number": disc,
        "disc_count": discs,
        "length_ms": int((getattr(info, "length", 0) or 0) * 1000),
        "bitrate": int((getattr(info, "bitrate", 0) or 0) / 1000),
        "sample_rate": int(getattr(info, "sample_rate", 0) or 44100),
        "codec": codec.lower(),
        "size": path.stat().st_size,
    }


def needs_conversion(path: Path, meta: dict) -> bool:
    ext = path.suffix.lower()
    if ext not in NATIVE:
        return True
    if meta["sample_rate"] > MAX_SAMPLE_RATE:
        return True
    if ext in (".m4a", ".m4b") and meta["codec"] and not meta["codec"].startswith(("mp4a", "alac")):
        return True
    return False


def convert_to_mp3(src: Path, workdir: Path | None = None) -> Path:
    """Transcodes `src` to a VBR V0 MP3 with tags preserved. Returns the temp file path."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise MediaError(f"{src.name} needs converting, but ffmpeg isn't installed")
    fd, out = tempfile.mkstemp(suffix=".mp3", dir=workdir)
    os.close(fd)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
        "-map", "0:a:0", "-map_metadata", "0", "-id3v2_version", "3",
        "-codec:a", "libmp3lame", "-q:a", "0", "-ar", "44100", out,
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    if proc.returncode != 0:
        Path(out).unlink(missing_ok=True)
        raise MediaError(f"ffmpeg failed on {src.name}: {proc.stderr.strip()[-300:]}")
    return Path(out)


def collect_audio_files(paths) -> list[Path]:
    """Expands folders recursively and keeps only audio files, in a stable order."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS))
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            out.append(p)
    return out
