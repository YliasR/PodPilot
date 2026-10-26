import hashlib
import math
import struct

import pytest

from podpilot import hash58
from podpilot.device import IPod, ReadOnlyError
from podpilot.itunesdb import ITunesDB

from test_itunesdb import empty_db_bytes, track_meta

GUID = bytes.fromhex("000A27001933C4C7")


def libgpod_style(guid: bytes, data: bytes) -> bytes:
    """Independent re-implementation of itdb_compute_hash: manual HMAC, as libgpod does it."""
    y = bytearray()
    for i in range(4):
        a, b = guid[2 * i], guid[2 * i + 1]
        lcm = 1 if a == 0 or b == 0 else math.lcm(a, b)
        hi, lo = lcm >> 8, lcm & 0xFF
        y += bytes((hash58._TABLE1[hi], hash58._TABLE2[hi], hash58._TABLE1[lo], hash58._TABLE2[lo]))
    key = bytearray(hashlib.sha1(hash58._FIXED + y).digest()) + bytearray(44)
    inner = hashlib.sha1(bytes(k ^ 0x36 for k in key) + data).digest()
    return hashlib.sha1(bytes(k ^ 0x5C for k in key) + inner).digest()


@pytest.mark.parametrize("guid", [GUID, bytes(8), bytes.fromhex("000A2700FFFFFFFF"), bytes(range(8))])
def test_matches_libgpod_construction(guid):
    data = empty_db_bytes() * 3
    assert hash58.compute(guid, data) == libgpod_style(guid, data)


def test_sign_and_verify():
    db = empty_db_bytes()
    signed = hash58.sign(db, GUID)
    assert struct.unpack_from("<H", signed, 0x30)[0] == hash58.SCHEME_HASH58
    assert hash58.verify(signed, GUID)
    assert not hash58.verify(signed, bytes.fromhex("000A27001933C4C8"))
    tampered = bytearray(signed)
    tampered[-1] ^= 1
    assert not hash58.verify(bytes(tampered), GUID)
    # db_id and 0x32..0x46 are excluded from the hash, like libgpod
    other = bytearray(signed)
    other[0x18:0x20] = b"\x11" * 8
    other[0x32:0x46] = b"\x22" * 20
    assert hash58.verify(bytes(other), GUID)
    assert hash58.sign(bytes(other), GUID)[0x18:0x20] == b"\x11" * 8


def test_parse_guid():
    assert hash58.parse_guid("0x000A27001933C4C7") == GUID
    assert hash58.parse_guid("000a27001933c4c7") == GUID
    for bad in ("", "0x1234", "zz0A27001933C4C7"):
        with pytest.raises(hash58.Hash58Error):
            hash58.parse_guid(bad)


def make_ipod(tmp_path, db: bytes, sysinfo_guid: str | None):
    (tmp_path / "iPod_Control" / "Music" / "F00").mkdir(parents=True)
    (tmp_path / "iPod_Control" / "iTunes").mkdir(parents=True)
    (tmp_path / "iPod_Control" / "Device").mkdir(parents=True)
    (tmp_path / "iPod_Control" / "iTunes" / "iTunesDB").write_bytes(db)
    if sysinfo_guid:
        (tmp_path / "iPod_Control" / "Device" / "SysInfo").write_text(
            f"ModelNumStr: MB029\nFirewireGuid: {sysinfo_guid}\n")
    return tmp_path


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from podpilot import device
    monkeypatch.setattr(device, "app_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(device, "_guid_from_os", lambda root: None)


def test_hash58_ipod_is_writable_and_saves_signed(tmp_path):
    root = make_ipod(tmp_path, hash58.sign(empty_db_bytes(), GUID), "0x000A27001933C4C7")
    ipod = IPod(root)
    assert ipod.writable and ipod.guid == GUID
    ipod.db.add_track(track_meta("New song"))
    ipod.save()
    saved = ipod.db_path.read_bytes()
    assert hash58.verify(saved, GUID)
    assert [t.title for t in ITunesDB(saved).tracks] == ["New song"]


def test_wrong_guid_makes_ipod_read_only(tmp_path):
    root = make_ipod(tmp_path, hash58.sign(empty_db_bytes(), GUID), "0x000A27001933C4C8")
    (root / "iPod_Control" / "Music" / "F00" / "AAAA.mp3").write_bytes(b"x")
    ipod = IPod(root)
    assert not ipod.writable and "reproduce" in ipod.read_only_reason
    before = ipod.db_path.read_bytes()
    with pytest.raises(ReadOnlyError):
        ipod.save()
    with pytest.raises(ReadOnlyError):
        ipod.delete_files([root / "iPod_Control" / "Music" / "F00" / "AAAA.mp3"])
    assert ipod.db_path.read_bytes() == before
    assert (root / "iPod_Control" / "Music" / "F00" / "AAAA.mp3").exists()


def test_missing_guid_and_hash72_are_read_only(tmp_path):
    ipod = IPod(make_ipod(tmp_path / "a", hash58.sign(empty_db_bytes(), GUID), None))
    assert not ipod.writable and "FireWire ID" in ipod.read_only_reason
    raw = bytearray(empty_db_bytes())
    raw[0x80] = 7
    ipod = IPod(make_ipod(tmp_path / "b", bytes(raw), None))
    assert not ipod.writable and "hash72" in ipod.read_only_reason


def test_sysinfo_extended_plist(tmp_path):
    import plistlib
    root = make_ipod(tmp_path, hash58.sign(empty_db_bytes(), GUID), None)
    (root / "iPod_Control" / "Device" / "SysInfoExtended").write_bytes(
        plistlib.dumps({"FireWireGUID": "000A27001933C4C7", "DBVersion": 3}))
    assert IPod(root).writable
