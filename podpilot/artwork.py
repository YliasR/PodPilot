"""Find a cover for a song: embedded, a picture next to the file, then online.

Online lookups use the iTunes Search API and, if that misses, MusicBrainz plus
the Cover Art Archive. Nothing here needs an API key. Failures are remembered
so the same album isn't requested again for a month. The iPod still needs the
bytes written into ArtworkDB; that part lives in ipodart.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from . import __version__, ipodart
from .ipodart import ThumbFormat

UA = f"PodPilot/{__version__} (https://github.com/YliasR/PodPilot)"
_HOSTS = ("itunes.apple.com", "mzstatic.com", "musicbrainz.org", "coverartarchive.org", "archive.org")
_SIDECARS = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png",
             "front.jpg", "front.png", "album.jpg", "albumart.jpg")
_MISS_TTL = 30 * 24 * 3600
_MAX_BYTES = 8 * 1024 * 1024
_last_musicbrainz = 0.0


class Offline(Exception):
    """The cover services couldn't be reached. Not the same as 'no cover exists'."""


@dataclass
class ArtResult:
    updated: int = 0
    already: int = 0
    missing: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def cache_key(artist: str, album: str, album_artist: str = "", title: str = "") -> str | None:
    """Stable key for one album, or one song when it has no album. None if there's nothing to search."""
    who = _norm(album_artist or artist)
    album_n = _norm(album)
    if album_n:
        return f"album\n{who}\n{album_n}"
    title_n = _norm(title)
    if who and title_n:
        return f"song\n{who}\n{title_n}"
    return None


def _match_score(got_artist: str, got_name: str, want_artist: str, want_name: str) -> int:
    got_a, got_n = _norm(got_artist), _norm(got_name)
    want_a, want_n = _norm(want_artist), _norm(want_name)
    if not want_n:
        return 0
    if got_n == want_n:
        score = 10
    elif want_n in got_n or got_n in want_n:
        score = 6
    else:
        return 0
    if want_a and (got_a == want_a or want_a in got_a or got_a in want_a):
        score += 3
    return score


def pick_itunes_artwork_url(payload: dict, artist: str, album: str, title: str = "") -> str | None:
    """Best artwork URL from an iTunes Search response, or None. Album match beats a loose one."""
    best_url = None
    best_score = 0
    for row in payload.get("results") or []:
        if not isinstance(row, dict) or not row.get("artworkUrl100"):
            continue
        if album:
            score = _match_score(row.get("artistName", ""), row.get("collectionName", ""), artist, album)
        else:
            score = _match_score(row.get("artistName", ""), row.get("trackName", ""), artist, title)
        if score > best_score:
            best_score = score
            best_url = str(row["artworkUrl100"])
    if not best_url:
        return None
    return best_url.replace("100x100bb", "600x600bb").replace("100x100", "600x600")


def _allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and any(host == name or host.endswith("." + name) for name in _HOSTS)


def _get(url: str, timeout: float, accept: str) -> bytes:
    if not _allowed(url):
        raise Offline(f"refusing {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not _allowed(resp.geturl()):
                raise Offline(f"refusing redirect {resp.geturl()}")
            data = resp.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return b""
        raise Offline(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Offline(str(exc)) from exc
    if len(data) > _MAX_BYTES:
        raise Offline("cover download was too large")
    return data


def _itunes(term: str, entity: str, timeout: float) -> dict:
    query = urllib.parse.urlencode({"term": term, "entity": entity, "limit": 8, "media": "music"})
    body = _get(f"https://itunes.apple.com/search?{query}", timeout, "application/json")
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _coverart_for_release(artist: str, album: str, timeout: float) -> bytes:
    global _last_musicbrainz
    wait = 1.1 - (time.monotonic() - _last_musicbrainz)
    if wait > 0:
        time.sleep(wait)
    _last_musicbrainz = time.monotonic()
    album_q = album.replace('"', "")
    artist_q = artist.replace('"', "")
    query = f'release:"{album_q}"'
    if artist_q:
        query += f' AND artist:"{artist_q}"'
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 5})
    body = _get(url, timeout, "application/json")
    if not body:
        return b""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return b""
    best_id = None
    best_score = 0
    for release in payload.get("releases") or []:
        if not isinstance(release, dict) or not release.get("id"):
            continue
        credit = release.get("artist-credit") or []
        name = credit[0].get("name", "") if credit and isinstance(credit[0], dict) else ""
        score = _match_score(name, release.get("title", ""), artist, album)
        if score > best_score:
            best_score = score
            best_id = release["id"]
    if not best_id:
        return b""
    return _get(f"https://coverartarchive.org/release/{best_id}/front-500", timeout, "image/jpeg")


def lookup_online(artist: str, album: str, title: str = "", timeout: float = 12) -> bytes | None:
    """JPEG/PNG bytes for this album or song. None if nobody has a cover. Raises Offline if the network fails."""
    artist, album, title = artist.strip(), album.strip(), title.strip()
    if album:
        url = pick_itunes_artwork_url(_itunes(f"{artist} {album}".strip(), "album", timeout), artist, album)
        if url:
            data = _get(url, timeout, "image/jpeg")
            if data:
                return data
        data = _coverart_for_release(artist, album, timeout)
        return data or None
    if artist and title:
        url = pick_itunes_artwork_url(_itunes(f"{artist} {title}", "song", timeout), artist, "", title)
        if url:
            data = _get(url, timeout, "image/jpeg")
            return data or None
    return None


class CoverCache:
    """JPEG bytes and remembered misses under app data. The index is small; the pictures are separate files."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
            self.index: dict = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.index = {}

    def _path(self, key: str) -> Path:
        name = hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg"
        return self.root / name

    def get(self, key: str) -> bytes | None:
        entry = self.index.get(key)
        if not isinstance(entry, dict) or entry.get("miss"):
            return None
        try:
            data = self._path(key).read_bytes()
        except OSError:
            return None
        return data or None

    def remembered_miss(self, key: str) -> bool:
        entry = self.index.get(key)
        if not isinstance(entry, dict) or not entry.get("miss"):
            return False
        return time.time() - float(entry.get("at") or 0) < _MISS_TTL

    def _write_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.index_path)

    def store(self, key: str, jpeg: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_bytes(jpeg)
        self.index[key] = {"at": int(time.time())}
        self._write_index()

    def store_miss(self, key: str) -> None:
        self.index[key] = {"miss": True, "at": int(time.time())}
        self._write_index()


def _image_bytes(audio) -> bytes | None:
    tags = getattr(audio, "tags", None)
    if isinstance(tags, ID3):
        pictures = list(tags.getall("APIC"))
        front = [p for p in pictures if getattr(p, "type", None) == 3] or pictures
        if front and front[0].data:
            return bytes(front[0].data)
    if isinstance(audio, MP4) and audio.get("covr"):
        return bytes(audio["covr"][0])
    if isinstance(audio, FLAC) and audio.pictures:
        return bytes(audio.pictures[0].data)
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return bytes(pictures[0].data)
    block = tags.get("metadata_block_picture") if tags else None
    if not block:
        return None
    try:
        raw = block[0]
        blob = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
        return bytes(Picture(blob).data)
    except Exception:
        return None


def embedded_cover(path: Path) -> bytes | None:
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None
    try:
        return _image_bytes(audio)
    except Exception:
        return None


def sidecar_cover(path: Path) -> bytes | None:
    folder = path if path.is_dir() else path.parent
    try:
        names = {p.name.casefold(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in _SIDECARS:
        candidate = names.get(name.casefold())
        if candidate is None:
            continue
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:6] in (b"GIF87a", b"GIF89a"):
            return data
    return None


def local_cover(path: Path) -> bytes | None:
    return embedded_cover(path) or sidecar_cover(path)


def _qimage(data: bytes) -> QImage | None:
    img = QImage()
    if not data or not img.loadFromData(data) or img.isNull():
        return None
    return img


def ensure_jpeg(data: bytes, quality: int = 90) -> bytes | None:
    """JPEG bytes, or None if `data` isn't a picture we can decode."""
    img = _qimage(data)
    if img is None:
        return None
    if data[:2] == b"\xff\xd8":
        return data
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not img.save(buf, "JPEG", quality):
        return None
    out = bytes(buf.data())
    return out or None


def render_thumb(data: bytes, fmt: ThumbFormat) -> tuple[bytes, int, int] | None:
    """RGB565 pixels for one iPod thumbnail format, plus the letterbox padding (vertical, horizontal)."""
    img = _qimage(data)
    if img is None:
        return None
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h = fmt.width, fmt.height
    if fmt.crop:
        scaled = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        canvas = scaled.copy(x, y, w, h).convertToFormat(QImage.Format_RGB888)
        vpad = hpad = 0
    else:
        scaled = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB888)
        canvas = QImage(w, h, QImage.Format_RGB888)
        canvas.fill(QColor(0, 0, 0))
        hpad = max(0, (w - scaled.width()) // 2)
        vpad = max(0, (h - scaled.height()) // 2)
        painter = QPainter(canvas)
        painter.drawImage(hpad, vpad, scaled)
        painter.end()
    canvas = canvas.convertToFormat(QImage.Format_RGB888)
    stride = canvas.bytesPerLine()
    raw = bytes(canvas.constBits())[:canvas.height() * stride]
    if len(raw) < canvas.height() * stride:
        return None
    return ipodart.pack_rgb565(raw, stride, w, h, fmt.big_endian), vpad, hpad


def embed_cover(path: Path, jpeg: bytes) -> bool:
    """Writes a front cover into an MP3 or M4A. Leaves the source alone when the format can't hold one."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
            tags.save(path)
            return True
        if ext in (".m4a", ".m4b"):
            audio = MP4(path)
            audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True
    except Exception:
        return False
    return False
