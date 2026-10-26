import os
import time
import wave
from pathlib import Path

import pytest

from podpilot import device
from podpilot.device import IPod
from podpilot.itunesdb import ITunesDB

from test_itunesdb import empty_db_bytes


def write_wav(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


@pytest.fixture
def ipod(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "app_data_dir", lambda: tmp_path / "appdata")
    root = tmp_path / "ipod"
    (root / "iPod_Control" / "iTunes").mkdir(parents=True)
    (root / "iPod_Control" / "Music" / "F00").mkdir(parents=True)
    (root / "iPod_Control" / "iTunes" / "iTunesDB").write_bytes(empty_db_bytes())
    return IPod(root)


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "Music"
    write_wav(lib / "Alpha.wav", 3)
    write_wav(lib / "Artist" / "Album" / "Bravo.wav", 4)
    write_wav(lib / "Artist" / "Album" / "Disc 2" / "Charlie.wav", 5)
    (lib / "cover.jpg").write_bytes(b"not audio")
    return lib


def reopen(ipod):
    return IPod(ipod.root)


def sync(ipod, folder, **kw):
    plan = ipod.plan_sync(folder)
    return plan, ipod.apply_sync(plan, **kw)


def test_first_sync_adds_everything_recursively(ipod, library):
    plan, result = sync(ipod, library)
    assert sorted(p.name for p, _ in plan.to_add) == ["Alpha.wav", "Bravo.wav", "Charlie.wav"]
    assert result.added == 3 and not result.errors
    fresh = reopen(ipod)
    assert sorted(t.title for t in fresh.db.tracks) == ["Alpha", "Bravo", "Charlie"]
    assert all(fresh.path_of(t).is_file() for t in fresh.db.tracks)
    assert fresh.load_manifest()["folder"] == str(library)


def test_resync_is_a_no_op(ipod, library):
    sync(ipod, library)
    ipod = reopen(ipod)
    db_before = ipod.db_path.read_bytes()
    plan = ipod.plan_sync(library)
    assert len(plan.unchanged) == 3
    assert not (plan.to_add or plan.to_update or plan.linked or plan.not_in_folder)


def test_changed_file_updates_in_place_and_keeps_playlists(ipod, library):
    sync(ipod, library)
    ipod = reopen(ipod)
    bravo = next(t for t in ipod.db.tracks if t.title == "Bravo")
    old_file = ipod.path_of(bravo)
    pl = ipod.db.create_playlist("Faves")
    ipod.db.add_to_playlist(pl, [bravo])
    ipod.save()

    src = library / "Artist" / "Album" / "Bravo.wav"
    write_wav(src, 9)
    os.utime(src, (time.time() + 5, time.time() + 5))
    ipod = reopen(ipod)
    plan, result = sync(ipod, library)
    assert [p.name for p, _, _ in plan.to_update] == ["Bravo.wav"] and result.updated == 1

    fresh = reopen(ipod)
    bravo2 = next(t for t in fresh.db.tracks if t.title == "Bravo")
    assert bravo2.id == bravo.id and bravo2.dbid == bravo.dbid
    assert abs(bravo2.length_ms - 9000) < 100
    assert bravo2.id in next(p for p in fresh.db.playlists if p.name == "Faves").track_ids
    assert not old_file.exists() and fresh.path_of(bravo2).is_file()


def test_mirror_removes_only_when_asked(ipod, library):
    sync(ipod, library)
    (library / "Alpha.wav").unlink()

    ipod = reopen(ipod)
    plan, result = sync(ipod, library)
    assert [t.title for t in plan.not_in_folder] == ["Alpha"] and result.removed == 0
    assert len(reopen(ipod).db.tracks) == 3

    ipod = reopen(ipod)
    plan, result = sync(ipod, library, remove_missing=True)
    assert result.removed == 1
    assert sorted(t.title for t in reopen(ipod).db.tracks) == ["Bravo", "Charlie"]


def test_existing_songs_are_linked_not_copied_again(ipod, library):
    ipod.db.add_track({"title": "Alpha", "artist": "", "length_ms": 3000, "size": 10,
                       "location": ":iPod_Control:Music:F00:OLD1.wav", "filetype_code": "WAV "})
    ipod.save()
    ipod = reopen(ipod)
    plan, result = sync(ipod, library)
    assert [p.name for p, _ in plan.linked] == ["Alpha.wav"]
    assert result.added == 2
    ipod = reopen(ipod)
    assert len(ipod.db.tracks) == 3
    assert len(ipod.plan_sync(library).unchanged) == 3


def test_duplicate_files_in_folder_are_added_once(ipod, library):
    write_wav(library / "Copies" / "Alpha.wav", 3)
    plan, result = sync(ipod, library)
    assert len(plan.duplicates) == 1 and result.added == 3


def test_failed_save_keeps_old_files(ipod, library, monkeypatch):
    sync(ipod, library)
    ipod = reopen(ipod)
    bravo = next(t for t in ipod.db.tracks if t.title == "Bravo")
    old_file = ipod.path_of(bravo)
    src = library / "Artist" / "Album" / "Bravo.wav"
    write_wav(src, 7)
    os.utime(src, (time.time() + 5, time.time() + 5))
    plan = ipod.plan_sync(library)

    def broken_save():
        raise OSError(1117, "I/O device error")
    monkeypatch.setattr(ipod, "save", broken_save)
    with pytest.raises(OSError):
        ipod.apply_sync(plan)
    assert old_file.exists()
    assert reopen(ipod).path_of(bravo).exists()
