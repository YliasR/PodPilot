"""ArtworkDB and .ithmb thumbnails for click-wheel iPods.

The iPod does not show pictures embedded in the audio file. Covers live in
iPod_Control/Artwork/: an ArtworkDB (mhfd → three mhsd lists) plus one
RGB565 .ithmb file per thumbnail format. Layout matches what libgpod writes.
Photo databases under iPod_Control/Photos are never touched.

A track points at its mhii image with mhit+0x160 (mhii_link) and byte 0xA4
set to 1. The mhii's song id is the track dbid, which is how older firmware
finds the same picture.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from .itunesdb import mac_now


class ArtError(Exception):
    """ArtworkDB is present but not safe to rewrite."""


@dataclass(frozen=True)
class ThumbFormat:
    format_id: int
    width: int
    height: int
    big_endian: bool = False
    crop: bool = False

    @property
    def pixel_bytes(self) -> int:
        return self.width * self.height * 2


# Cover formats for the click-wheel models PodPilot can write, used when the
# device doesn't describe itself (no SysInfoExtended AlbumArt) and has no
# ArtworkDB yet. Every id is RGB565. Writing the ones a model doesn't use is
# harmless; leaving out the one it does use means no art on screen.
FALLBACK_FORMATS = (
    ThumbFormat(1017, 56, 56),     # photo
    ThumbFormat(1016, 140, 140),
    ThumbFormat(1031, 42, 42),     # nano 1G/2G
    ThumbFormat(1027, 100, 100),
    ThumbFormat(1028, 100, 100),   # video / classic 5G
    ThumbFormat(1029, 200, 200),
    ThumbFormat(1061, 56, 56),     # classic / nano 3G
    ThumbFormat(1055, 128, 128),
    ThumbFormat(1068, 128, 128),
    ThumbFormat(1060, 320, 320),
    ThumbFormat(1074, 50, 50),     # nano 4G
    ThumbFormat(1078, 80, 80),
    ThumbFormat(1071, 240, 240),
    ThumbFormat(1084, 240, 240),
)
_FORMATS_BY_ID = {f.format_id: f for f in FALLBACK_FORMATS}

_MHFD = 0x84
_MHSD = 0x60
_LIST = 0x5C
_MHII = 0x98
_MHNI = 0x4C
_MHIF = 0x7C


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def pack_rgb565(rgb: bytes, stride: int, width: int, height: int, big_endian: bool = False) -> bytes:
    """Packs tightly or stride-padded RGB888 rows into RGB565."""
    out = bytearray(width * height * 2)
    o = 0
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            if big_endian:
                out[o] = value >> 8
                out[o + 1] = value & 0xFF
            else:
                out[o] = value & 0xFF
                out[o + 1] = value >> 8
            o += 2
    return bytes(out)


def _byte_order(pixel_format: str) -> str | None:
    text = (pixel_format or "").strip()
    if len(text) == 8 and all(c in "0123456789abcdefABCDEF" for c in text):
        try:
            text = bytes.fromhex(text).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            return None
    code = text.upper().replace("-", "").replace("_", "")
    if code in ("L565", "RGB565LE"):
        return "le"
    if code in ("B565", "RGB565BE"):
        return "be"
    return None


def formats_from_sysinfo(device_dir: Path | None) -> list[ThumbFormat]:
    """AlbumArt entries from SysInfoExtended. Empty if the device doesn't say."""
    if device_dir is None:
        return []
    try:
        data = plistlib.loads((Path(device_dir) / "SysInfoExtended").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return []
    items = data.get("AlbumArt") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("Interlaced") in (1, True):
            continue
        order = _byte_order(str(item.get("PixelFormat") or ""))
        try:
            fid = int(item.get("FormatId") or 0)
            width = int(item.get("RenderWidth") or item.get("Width") or 0)
            height = int(item.get("RenderHeight") or item.get("Height") or 0)
        except (TypeError, ValueError):
            continue
        if order is None or fid <= 0 or not (0 < width <= 800 and 0 < height <= 800):
            continue
        out.append(ThumbFormat(fid, width, height, order == "be", item.get("Crop") in (1, True)))
    return out


def resolve_formats(device_dir: Path | None, existing_ids: list[int]) -> list[ThumbFormat]:
    """Formats to write. An existing ArtworkDB wins, so we append to its files."""
    described = {f.format_id: f for f in formats_from_sysinfo(device_dir)}
    if existing_ids:
        out = []
        for fid in existing_ids:
            fmt = described.get(fid) or _FORMATS_BY_ID.get(fid)
            if fmt is not None:
                out.append(fmt)
        return out
    if described:
        return list(described.values())
    return list(FALLBACK_FORMATS)


def _mhod_filename(name: str) -> bytes:
    """ArtworkDB string mhod, type 3, UTF-16 LE. header_len stays 24, like iTunes."""
    raw = name.encode("utf-16le")
    base = 36
    pad = (4 - ((base + len(raw)) % 4)) % 4
    buf = bytearray(base + len(raw) + pad)
    struct.pack_into("<4sII", buf, 0, b"mhod", 24, len(buf))
    struct.pack_into("<H", buf, 12, 3)
    buf[15] = pad
    struct.pack_into("<I", buf, 24, len(raw))
    buf[28] = 2
    buf[36:36 + len(raw)] = raw
    return bytes(buf)


def _mhni(fmt: ThumbFormat, offset: int, size: int, vpad: int, hpad: int) -> bytes:
    child = _mhod_filename(f":F{fmt.format_id}_1.ithmb")
    header = bytearray(_MHNI)
    struct.pack_into("<4sIIII", header, 0, b"mhni", _MHNI, _MHNI + len(child), 1, fmt.format_id)
    struct.pack_into("<II", header, 0x14, offset & 0xFFFFFFFF, size & 0xFFFFFFFF)
    struct.pack_into("<hhhh", header, 0x1C, vpad, hpad, fmt.height, fmt.width)
    return bytes(header) + child


def _thumb_mhod(mhni: bytes) -> bytes:
    header = bytearray(24)
    struct.pack_into("<4sII", header, 0, b"mhod", 24, 24 + len(mhni))
    struct.pack_into("<H", header, 12, 2)
    return bytes(header) + mhni


def _mhii(image_id: int, dbid: int, jpeg_size: int, thumbs: list[bytes]) -> bytes:
    kids = b"".join(thumbs)
    header = bytearray(_MHII)
    now = mac_now()
    struct.pack_into("<4sI", header, 0, b"mhii", _MHII)
    struct.pack_into("<I", header, 8, _MHII + len(kids))
    struct.pack_into("<I", header, 0x0C, len(thumbs))
    struct.pack_into("<I", header, 0x10, image_id)
    struct.pack_into("<Q", header, 0x14, dbid)
    struct.pack_into("<III", header, 0x28, now, now, jpeg_size & 0xFFFFFFFF)
    return bytes(header) + kids


def _mhif(fmt: ThumbFormat) -> bytes:
    header = bytearray(_MHIF)
    struct.pack_into("<4sII", header, 0, b"mhif", _MHIF, _MHIF)
    struct.pack_into("<II", header, 0x10, fmt.format_id, fmt.pixel_bytes)
    return bytes(header)


def _list(magic: bytes, count: int, payload: bytes) -> bytes:
    header = bytearray(_LIST)
    struct.pack_into("<4sII", header, 0, magic, _LIST, count)
    return bytes(header) + payload


def _mhsd(index: int, payload: bytes) -> bytes:
    header = bytearray(_MHSD)
    struct.pack_into("<4sII", header, 0, b"mhsd", _MHSD, _MHSD + len(payload))
    struct.pack_into("<H", header, 0x0C, index)
    return bytes(header) + payload


def _take(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 12 > len(data) or not data[offset:offset + 2] == b"mh":
        raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
    total = _u32(data, offset + 8)
    if total < 12 or offset + total > len(data):
        raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
    return data[offset:offset + total], offset + total


class ArtworkLibrary:
    """The Artwork folder. Load, append covers, save. Existing records stay byte-for-byte."""

    def __init__(self, folder: Path, formats: list[ThumbFormat] | None = None,
                 device_dir: Path | None = None):
        self.folder = Path(folder)
        self.path = self.folder / "ArtworkDB"
        self.formats = formats
        self.device_dir = device_dir
        self.mhfd_header: bytes | None = None
        self.records: list[bytes] = []
        self.dbids: dict[int, int] = {}
        self.album_section: bytes | None = None
        self.mhifs: list[bytes] = []
        self.existing_format_ids: list[int] = []
        self.next_image_id = 100
        self._appends: dict[int, bytearray] = {}
        self._base_size: dict[int, int] = {}
        self._shared: dict[bytes, list[bytes]] = {}
        self.dirty = False
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.is_file():
            return
        data = self.path.read_bytes()
        if not data:
            return
        if not data.startswith(b"mhfd") or len(data) < 32:
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
        header_len = _u32(data, 4)
        total_len = _u32(data, 8)
        if header_len < 32 or header_len > len(data) or total_len < header_len or total_len > len(data):
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
        self.mhfd_header = data[:header_len]
        claimed = _u32(data, 0x14) if header_len >= 0x18 else 0
        stored_next = _u32(data, 0x1C) if header_len >= 0x20 else 0
        pos = header_len
        end = total_len
        seen = 0
        highest = 99
        while pos + 12 <= end and data[pos:pos + 4] == b"mhsd":
            blob, pos = _take(data, pos)
            seen += 1
            self._read_mhsd(blob, highest_box := [highest])
            highest = highest_box[0]
            if claimed and seen >= claimed:
                break
        if seen == 0:
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
        self.next_image_id = max(highest, stored_next, 99) + 1

    def _read_mhsd(self, blob: bytes, highest_box: list[int]) -> None:
        header_len = _u32(blob, 4)
        index16 = _u16(blob, 0x0C)
        index = index16 if index16 in (1, 2, 3) else _u32(blob, 0x0C)
        interior = blob[header_len:_u32(blob, 8)]
        if index == 2:
            self.album_section = blob
            return
        if len(interior) < 12:
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
        magic = interior[:4]
        list_len = _u32(interior, 4)
        count = _u32(interior, 8)
        if list_len < 12 or list_len > len(interior) or count > 200_000:
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
        pos = list_len
        if index == 1 and magic == b"mhli":
            for _ in range(count):
                rec, pos = _take(interior, pos)
                if rec[:4] != b"mhii" or len(rec) < 0x1C:
                    raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
                image_id = _u32(rec, 0x10)
                dbid = _u64(rec, 0x14)
                highest_box[0] = max(highest_box[0], image_id)
                self.records.append(rec)
                if dbid:
                    self.dbids.setdefault(dbid, image_id)
            return
        if index == 3 and magic == b"mhlf":
            for _ in range(count):
                rec, pos = _take(interior, pos)
                if rec[:4] != b"mhif" or len(rec) < 0x18:
                    raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")
                self.mhifs.append(rec)
                self.existing_format_ids.append(_u32(rec, 0x10))
            return
        if index not in (1, 2, 3):
            raise ArtError("ArtworkDB isn't in a format PodPilot can update safely, so it was left unchanged.")

    def write_formats(self) -> list[ThumbFormat]:
        if self.formats is not None:
            return self.formats
        return resolve_formats(self.device_dir, self.existing_format_ids)

    def _stage(self, format_id: int, pixels: bytes) -> int:
        if format_id not in self._base_size:
            path = self.folder / f"F{format_id}_1.ithmb"
            try:
                self._base_size[format_id] = path.stat().st_size if path.is_file() else 0
            except OSError:
                self._base_size[format_id] = 0
            self._appends[format_id] = bytearray()
        buf = self._appends[format_id]
        offset = self._base_size[format_id] + len(buf)
        buf.extend(pixels)
        return offset

    def add_rendered(self, dbid: int, jpeg_size: int,
                     parts: list[tuple[ThumbFormat, bytes, int, int]],
                     share: bytes | None = None) -> int | None:
        """One mhii for `dbid`. `share` reuses pixels already staged for the same picture."""
        if dbid in self.dbids:
            return self.dbids[dbid]
        if share is not None and share in self._shared:
            thumbs = self._shared[share]
        else:
            if any(len(pixels) != fmt.pixel_bytes for fmt, pixels, _vpad, _hpad in parts):
                return None
            thumbs = []
            for fmt, pixels, vpad, hpad in parts:
                offset = self._stage(fmt.format_id, pixels)
                thumbs.append(_thumb_mhod(_mhni(fmt, offset, len(pixels), vpad, hpad)))
            if share is not None:
                self._shared[share] = thumbs
        if not thumbs:
            return None
        image_id = self.next_image_id
        self.next_image_id += 1
        self.records.append(_mhii(image_id, dbid, jpeg_size, thumbs))
        self.dbids[dbid] = image_id
        self.dirty = True
        return image_id

    def save(self) -> None:
        if not self.dirty:
            return
        formats = {f.format_id: f for f in self.write_formats()}
        mhifs = self.mhifs
        if not mhifs:
            mhifs = [_mhif(formats[fid]) for fid in self._appends if fid in formats]
            if not mhifs:
                raise ArtError("This iPod's cover formats aren't ones PodPilot can write, so on-device art was skipped.")
        images = _mhsd(1, _list(b"mhli", len(self.records), b"".join(self.records)))
        albums = self.album_section if self.album_section is not None else _mhsd(2, _list(b"mhla", 0, b""))
        files = _mhsd(3, _list(b"mhlf", len(mhifs), b"".join(mhifs)))
        body = images + albums + files
        if self.mhfd_header and len(self.mhfd_header) >= 32:
            header = bytearray(self.mhfd_header)
        else:
            header = bytearray(_MHFD)
            struct.pack_into("<4sI", header, 0, b"mhfd", _MHFD)
            struct.pack_into("<I", header, 0x10, 2)  # iTunes 7 deletes the database if this is 1
            header[0x30] = 2
        total = len(header) + len(body)
        struct.pack_into("<I", header, 8, total)
        if len(header) >= 0x18:
            struct.pack_into("<I", header, 0x14, 3)
        if len(header) >= 0x20:
            struct.pack_into("<I", header, 0x1C, max(self.next_image_id - 1, 0))
        blob = bytes(header) + body

        self.folder.mkdir(parents=True, exist_ok=True)
        for fid, extra in self._appends.items():
            if not extra:
                continue
            path = self.folder / f"F{fid}_1.ithmb"
            with open(path, "ab") as fh:
                fh.write(extra)
                fh.flush()
                os.fsync(fh.fileno())
        self._appends.clear()

        if self.path.is_file():
            shutil.copy2(self.path, self.path.with_name("ArtworkDB.podpilot-bak"))
        tmp = self.path.with_name("ArtworkDB.podpilot-tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self.dirty = False
