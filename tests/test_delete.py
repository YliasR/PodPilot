import os

import pytest

from podpilot import device
from podpilot.device import IPod
from podpilot.itunesdb import ITunesDB

from test_itunesdb import empty_db_bytes, track_meta


@pytest.fixture
def ipod(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "app_data_dir", lambda: tmp_path / "appdata")
    music = tmp_path / "iPod_Control" / "Music" / "F00"
    music.mkdir(parents=True)
    (tmp_path / "iPod_Control" / "iTunes").mkdir(parents=True)
    db = ITunesDB(empty_db_bytes())
    for name in ("AAAA", "BBBB", "CCCC"):
        db.add_track(dict(track_meta(name), location=f":iPod_Control:Music:F00:{name}.mp3"))
        (music / f"{name}.mp3").write_bytes(b"audio")
    (tmp_path / "iPod_Control" / "iTunes" / "iTunesDB").write_bytes(db.to_bytes())
    return IPod(tmp_path)


def files(ipod):
    return sorted(p.name for p in (ipod.control / "Music" / "F00").iterdir())


def test_failed_save_deletes_no_files(ipod, monkeypatch):
    def broken_save():
        raise OSError(1117, "I/O device error")
    monkeypatch.setattr(ipod, "save", broken_save)
    with pytest.raises(OSError):
        ipod.remove_tracks(list(ipod.db.tracks))
    assert files(ipod) == ["AAAA.mp3", "BBBB.mp3", "CCCC.mp3"]
    assert len(IPod(ipod.root).db.tracks) == 3


def test_undeletable_file_still_removes_entry(ipod, monkeypatch):
    real_unlink = os.unlink

    def flaky_unlink(path, *a, **k):
        if str(path).endswith("BBBB.mp3"):
            raise PermissionError(13, "in use")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "unlink", flaky_unlink)
    errors = ipod.remove_tracks(list(ipod.db.tracks))
    assert len(errors) == 1 and "AAAA" not in errors[0]
    assert IPod(ipod.root).db.tracks == []
    assert files(ipod) == ["BBBB.mp3"]
    assert ipod.health().orphan_files[0].name == "BBBB.mp3"


def test_playing_marker_does_not_emit_layout_changed():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from podpilot import gui

    app = QApplication.instance() or QApplication([])  # noqa: F841
    db = ITunesDB(empty_db_bytes())
    tracks = [db.add_track(track_meta(n)) for n in ("One", "Two", "Three")]
    model = gui.TrackModel()
    proxy = gui.TrackFilter()
    proxy.setSourceModel(model)
    model.set_tracks(tracks)

    layout, changed = [], []
    model.layoutChanged.connect(lambda *a: layout.append(a))
    model.dataChanged.connect(lambda tl, br, roles: changed.append(tl.row()))
    model.set_playing(tracks[1].id)
    model.set_playing(tracks[2].id)
    model.set_playing(None)
    assert layout == []
    assert changed == [1, 1, 2, 2]
    assert proxy.rowCount() == 3
