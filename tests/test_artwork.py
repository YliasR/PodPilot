"""Album art: RGB565, ArtworkDB, cache, sidecars, and a library-wide sync."""

import os
import plistlib
import time
import wave
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from podpilot import artwork, device, ipodart
from podpilot.device import IPod
from podpilot.ipodart import ArtworkLibrary, ThumbFormat, pack_rgb565

from test_itunesdb import empty_db_bytes


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def tiny_jpeg() -> bytes:
    img = QImage(8, 12, QImage.Format_RGB888)
    img.fill(QColor(200, 20, 20))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert img.save(buf, "JPEG", 90)
    data = bytes(buf.data())
    assert data[:2] == b"\xff\xd8"
    return data


def write_wav(path: Path, seconds: float = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


@pytest.fixture
def ipod(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "app_data_dir", lambda: tmp_path / "appdata")
    root = tmp_path / "ipod"
    (root / "iPod_Control" / "iTunes").mkdir(parents=True)
    (root / "iPod_Control" / "Music" / "F00").mkdir(parents=True)
    (root / "iPod_Control" / "iTunes" / "iTunesDB").write_bytes(empty_db_bytes())
    return IPod(root)


def test_pack_rgb565_channels_and_endian():
    red = pack_rgb565(bytes([255, 0, 0]), 3, 1, 1, False)
    assert red == bytes([0x00, 0xF8])
    assert pack_rgb565(bytes([255, 0, 0]), 3, 1, 1, True) == bytes([0xF8, 0x00])
    green = pack_rgb565(bytes([0, 255, 0]), 3, 1, 1, False)
    assert green == bytes([0xE0, 0x07])
    # A padded stride is skipped; only the real pixels are packed.
    padded = pack_rgb565(bytes([255, 0, 0, 1, 2, 3]), 6, 1, 1, False)
    assert padded == red


def test_sysinfo_album_art_skips_interlaced_and_non_rgb565(tmp_path):
    device_dir = tmp_path / "Device"
    device_dir.mkdir()
    (device_dir / "SysInfoExtended").write_bytes(plistlib.dumps({
        "AlbumArt": [
            {"FormatId": 42, "RenderWidth": 16, "RenderHeight": 16, "PixelFormat": "4C353635"},
            {"FormatId": 7, "Width": 8, "Height": 8, "PixelFormat": "B565", "Crop": 1},
            {"FormatId": 99, "Width": 8, "Height": 8, "PixelFormat": "UYVY", "Interlaced": 1},
            {"FormatId": 3, "Width": 8, "Height": 8, "PixelFormat": "JPEG"},
        ]
    }))
    formats = ipodart.formats_from_sysinfo(device_dir)
    assert [(f.format_id, f.big_endian, f.crop) for f in formats] == [(42, False, False), (7, True, True)]


def test_artworkdb_create_append_and_share_pixels(tmp_path):
    folder = tmp_path / "Artwork"
    fmt = ThumbFormat(1055, 2, 2)
    pixels = pack_rgb565(bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]), 6, 2, 2)
    lib = ArtworkLibrary(folder, formats=[fmt])
    lib.load()
    first = lib.add_rendered(0xABC, 100, [(fmt, pixels, 0, 1)], share=b"same")
    second = lib.add_rendered(0xDEF, 100, [(fmt, pixels, 0, 1)], share=b"same")
    assert first and second and first != second
    lib.save()

    ithmb = folder / "F1055_1.ithmb"
    assert ithmb.stat().st_size == fmt.pixel_bytes
    blob = (folder / "ArtworkDB").read_bytes()
    assert blob.startswith(b"mhfd")
    assert blob[0x10:0x14] == (2).to_bytes(4, "little")  # iTunes 7 deletes the DB if this is 1

    again = ArtworkLibrary(folder, formats=[fmt])
    again.load()
    assert again.dbids == {0xABC: first, 0xDEF: second}
    plain = ArtworkLibrary(folder)
    plain.load()
    assert [f.format_id for f in plain.write_formats()] == [1055]
    # A byte iTunes left in the header, outside the fields we patch, survives an append.
    header = bytearray(blob[:0x84])
    header[0x40] = 0xAB
    (folder / "ArtworkDB").write_bytes(bytes(header) + blob[0x84:])
    again = ArtworkLibrary(folder, formats=[fmt])
    again.load()
    third = again.add_rendered(0x111, 40, [(fmt, pixels, 0, 0)])
    again.save()
    saved = (folder / "ArtworkDB").read_bytes()
    assert saved[0x40] == 0xAB
    assert ithmb.stat().st_size == fmt.pixel_bytes * 2
    reloaded = ArtworkLibrary(folder, formats=[fmt])
    reloaded.load()
    assert reloaded.dbids[0xABC] == first
    assert reloaded.dbids[0x111] == third
    assert (folder / "ArtworkDB.podpilot-bak").is_file()


def test_bad_artworkdb_is_left_alone(tmp_path):
    folder = tmp_path / "Artwork"
    folder.mkdir()
    target = folder / "ArtworkDB"
    target.write_bytes(b"this is not an artwork database")
    lib = ArtworkLibrary(folder)
    with pytest.raises(ipodart.ArtError):
        lib.load()
    assert target.read_bytes() == b"this is not an artwork database"
    assert not lib.dirty


def test_sidecar_requires_a_real_image(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    song = folder / "song.mp3"
    song.write_bytes(b"\x00")
    (folder / "cover.jpg").write_bytes(b"not audio")
    assert artwork.sidecar_cover(song) is None
    (folder / "Folder.PNG").write_bytes(b"\x89PNG\r\n\x1a\n" + b"rest")
    found = artwork.sidecar_cover(song)
    assert found is not None and found.startswith(b"\x89PNG")


def test_cache_miss_expires(tmp_path):
    cache = artwork.CoverCache(tmp_path / "art")
    key = artwork.cache_key("Radiohead", "OK Computer")
    assert key is not None
    cache.store_miss(key)
    assert cache.remembered_miss(key)
    cache.index[key]["at"] = int(time.time()) - 31 * 24 * 3600
    assert not cache.remembered_miss(key)
    assert artwork.cache_key("", "", "", "Only a title") is None


def test_itunes_artwork_url_prefers_the_album():
    payload = {"results": [
        {"artistName": "Radiohead", "collectionName": "The Bends",
         "artworkUrl100": "https://is1.mzstatic.com/100x100bb-bends.jpg"},
        {"artistName": "Radiohead", "collectionName": "OK Computer",
         "artworkUrl100": "https://is1.mzstatic.com/image/100x100bb.jpg"},
    ]}
    url = artwork.pick_itunes_artwork_url(payload, "Radiohead", "OK Computer")
    assert url == "https://is1.mzstatic.com/image/600x600bb.jpg"
    assert artwork.pick_itunes_artwork_url(
        {"results": [{"artistName": "A", "collectionName": "Nope", "artworkUrl100": "https://x/100x100bb.jpg"}]},
        "A", "In Rainbows") is None


def test_embed_cover_roundtrip(tmp_path, qapp):
    path = tmp_path / "song.mp3"
    # Several MPEG frames: mutagen ignores a file it can't sync as audio.
    frame = b"\xff\xfb\x90\x00" + bytes(413)
    path.write_bytes(frame * 4)
    jpeg = tiny_jpeg()
    assert artwork.embed_cover(path, jpeg)
    assert artwork.embedded_cover(path) == jpeg
    assert artwork.ensure_jpeg(b"not audio") is None


def test_sidecar_sync_writes_artworkdb_without_the_network(ipod, qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(artwork, "lookup_online", lambda *a, **k: pytest.fail("network"))
    folder = tmp_path / "in"
    write_wav(folder / "Track.wav")
    (folder / "Cover.JPG").write_bytes(tiny_jpeg())
    result = ipod.add_files([folder / "Track.wav"])
    assert len(result.added) == 1 and not result.errors
    assert result.art is not None and result.art.updated == 1 and not result.art.errors
    track = result.added[0]
    assert track.has_artwork
    art = ipod.control / "Artwork" / "ArtworkDB"
    assert art.is_file() and art.read_bytes().startswith(b"mhfd")
    fresh = IPod(ipod.root)
    assert fresh.db.tracks[0].has_artwork


def test_library_sync_groups_an_album_and_skips_a_second_download(ipod, qapp, monkeypatch, tmp_path):
    jpeg = tiny_jpeg()
    calls = []

    def lookup(artist, album, title="", timeout=12):
        calls.append((artist, album, title))
        return jpeg

    monkeypatch.setattr(artwork, "lookup_online", lookup)
    write_wav(tmp_path / "a" / "One.wav")
    write_wav(tmp_path / "b" / "Two.wav")
    added = ipod.add_files([tmp_path / "a" / "One.wav", tmp_path / "b" / "Two.wav"])
    assert added.art is not None and added.art.updated == 0 and added.art.missing == 2
    assert calls == []
    for track in ipod.db.tracks:
        track.artist = "Radiohead"
        track.album = "OK Computer"
    ipod.db.mark_dirty()
    art = ipod.sync_artwork()
    ipod.save()
    assert calls == [("Radiohead", "OK Computer", ipod.db.tracks[0].title)]
    assert art.updated == 2 and art.missing == 0
    assert all(track.has_artwork for track in ipod.db.tracks)
    fmt = next(f for f in ipodart.FALLBACK_FORMATS if f.format_id == 1055)
    ithmb = ipod.control / "Artwork" / f"F{fmt.format_id}_1.ithmb"
    assert ithmb.stat().st_size == fmt.pixel_bytes
    again = ipod.sync_artwork()
    assert again.already == 2 and again.updated == 0
    assert len(calls) == 1
    assert ithmb.stat().st_size == fmt.pixel_bytes


def test_unreadable_artworkdb_does_not_fail_the_import(ipod, qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(artwork, "lookup_online", lambda *a, **k: pytest.fail("network"))
    folder = ipod.control / "Artwork"
    folder.mkdir()
    (folder / "ArtworkDB").write_bytes(b"garbage-not-mhfd")
    src = tmp_path / "album"
    write_wav(src / "Track.wav")
    (src / "cover.jpg").write_bytes(tiny_jpeg())
    result = ipod.add_files([src / "Track.wav"])
    assert len(result.added) == 1
    assert not result.added[0].has_artwork
    assert result.art is not None and result.art.errors
    assert (folder / "ArtworkDB").read_bytes() == b"garbage-not-mhfd"
