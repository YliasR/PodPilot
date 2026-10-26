import struct

import pytest

from podpilot.itunesdb import (DS_PLAYLISTS, DS_PODCASTS, DS_TRACKS, DatabaseError, ITunesDB, make_string_mhod,
                               serialize, Chunk)


def _mhsd(ds_type: int, child: Chunk) -> Chunk:
    header = bytearray(96)
    struct.pack_into("<4sII", header, 0, b"mhsd", 96, 0)
    struct.pack_into("<I", header, 0x0C, ds_type)
    return Chunk(b"mhsd", header, [child])


def _list(magic: bytes, children) -> Chunk:
    header = bytearray(92)
    struct.pack_into("<4sI", header, 0, magic, 92)
    return Chunk(magic, header, list(children))


def _master(name: str) -> Chunk:
    header = bytearray(184)
    struct.pack_into("<4sI", header, 0, b"mhyp", 184)
    header[0x14] = 1
    struct.pack_into("<Q", header, 0x1C, 0x1122334455667788)
    return Chunk(b"mhyp", header, [make_string_mhod(1, name)])


def empty_db_bytes(name="Test iPod") -> bytes:
    header = bytearray(244)
    struct.pack_into("<4sIIII", header, 0, b"mhbd", 244, 0, 1, 0x75)
    root = Chunk(b"mhbd", header, [
        _mhsd(DS_TRACKS, _list(b"mhlt", [])),
        _mhsd(DS_PODCASTS, _list(b"mhlp", [_master(name)])),
        _mhsd(DS_PLAYLISTS, _list(b"mhlp", [_master(name)])),
    ])
    return serialize(root)


def track_meta(title, artist="Artist", album="Album", n=1):
    return {"title": title, "artist": artist, "album": album, "track_number": n, "length_ms": 180000,
            "bitrate": 256, "sample_rate": 44100, "size": 1234, "filetype_code": "MP3 ",
            "location": f":iPod_Control:Music:F00:{title[:4].upper()}.mp3"}


def test_roundtrip_is_byte_identical():
    raw = empty_db_bytes()
    assert ITunesDB(raw).to_bytes() == raw


def test_add_tracks_and_reload():
    db = ITunesDB(empty_db_bytes())
    db.add_track(track_meta("Zebra", "The Band", n=2))
    db.add_track(track_meta("apple", "Another", n=1))
    db.add_track(track_meta("\"Quoted\"", "Ümlaut"))
    db2 = ITunesDB(db.to_bytes())
    assert [t.title for t in db2.tracks] == ["Zebra", "apple", "\"Quoted\""]
    assert db2.tracks[2].artist == "Ümlaut"
    assert len(db2.master.track_ids) == 3
    # both master playlists (datasets 2 and 3) list every track
    assert all(len(m.kids(b"mhip")) == 3 for m in db2._masters())
    # title index ignores punctuation and leading articles
    idx = [c for c in db2._masters()[0].children if c.magic == b"mhod" and c.get("I", 0x0C) == 52
           and struct.unpack_from("<I", c.body)[0] == 0x03][0]
    order = struct.unpack_from("<3I", idx.body, 48)
    assert [db2.tracks[i].title for i in order] == ["apple", "\"Quoted\"", "Zebra"]


def test_remove_restores_original_bytes():
    db = ITunesDB(empty_db_bytes())
    db.add_track(track_meta("Keep"))
    before = db.to_bytes()
    db = ITunesDB(before)
    t = db.add_track(track_meta("Drop"))
    db.remove_tracks([t.id])
    assert db.to_bytes() == before


def test_playlists():
    db = ITunesDB(empty_db_bytes())
    a = db.add_track(track_meta("One"))
    b = db.add_track(track_meta("Two"))
    pl = db.create_playlist("Road trip")
    db.add_to_playlist(pl, [b, a])
    db = ITunesDB(db.to_bytes())
    pl = [p for p in db.playlists if p.name == "Road trip"][0]
    assert pl.editable and pl.track_ids == [b.id, a.id]
    db.rename_playlist(pl, "Gym")
    db.remove_tracks([a.id])
    db = ITunesDB(db.to_bytes())
    pl = [p for p in db.playlists if p.editable][0]
    assert pl.name == "Gym" and pl.track_ids == [b.id]
    db.delete_playlist(pl)
    assert not [p for p in ITunesDB(db.to_bytes()).playlists if p.editable]


def test_rejects_hashed_database():
    raw = bytearray(empty_db_bytes())
    raw[0x58] = 1
    with pytest.raises(DatabaseError):
        ITunesDB(bytes(raw))
