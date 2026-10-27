"""High-level operations on a mounted iPod: add, remove, export, save, eject."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
import re
import shutil
import string
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import artwork, hash58, ipodart, media
from .itunesdb import ITunesDB, Track

ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]

KEEP_BACKUPS = 30


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "PodPilot"


_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3


def drive_signature():
    """Cheap fingerprint of the mounted volumes (no disk I/O), to know when to rescan."""
    if os.name == "nt":
        import ctypes
        return ctypes.windll.kernel32.GetLogicalDrives()
    found = []
    user = os.environ.get("USER", "")
    for base in (Path("/Volumes"), Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
        try:
            found.extend(sorted(os.listdir(base)))
        except OSError:
            pass
    return tuple(found)


def find_ipods() -> list[Path]:
    """Mounted volumes that look like an iPod (have an iTunesDB).

    Can block on slow volumes, so call it off the UI thread.
    """
    candidates: list[Path] = []
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            # iPods are local USB disks; skipping network/optical drives avoids long timeouts
            if mask >> i & 1 and kernel32.GetDriveTypeW(f"{letter}:\\") in (_DRIVE_REMOVABLE, _DRIVE_FIXED):
                candidates.append(Path(f"{letter}:\\"))
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/Volumes"), Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
            if base.is_dir():
                candidates.extend(p for p in base.iterdir() if p.is_dir())
    found = []
    for root in candidates:
        try:
            if (root / "iPod_Control" / "iTunes" / "iTunesDB").is_file():
                found.append(root)
        except OSError:
            continue
    return found


def volume_label(root: Path) -> str:
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(str(root), buf, 261, None, None, None, None, 0):
            return buf.value
    return root.name or str(root)


def _guid_from_sysinfo(device_dir: Path) -> str | None:
    """FireWire GUID from SysInfoExtended (plist) or SysInfo (key: value lines)."""
    import plistlib
    try:
        ext = plistlib.loads((device_dir / "SysInfoExtended").read_bytes())
        if isinstance(ext, dict) and ext.get("FireWireGUID"):
            return str(ext["FireWireGUID"])
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    try:
        for line in (device_dir / "SysInfo").read_text("latin-1").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "firewireguid" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


def _guid_from_os(root: Path) -> str | None:
    """The iPod reports its FireWire GUID as the USB serial number."""
    try:
        if os.name == "nt":
            drive = str(root)[:2]
            script = (f"$ld = Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='{drive}'\"; "
                      "$p = Get-CimAssociatedInstance -InputObject $ld -ResultClassName Win32_DiskPartition; "
                      "(Get-CimAssociatedInstance -InputObject $p -ResultClassName Win32_DiskDrive).PNPDeviceID")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                 timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        elif sys.platform.startswith("linux"):
            out = ""
            with open("/proc/mounts") as fh:
                dev = next((line.split()[0] for line in fh if Path(line.split()[1]) == root), "")
            block = Path("/sys/class/block") / Path(dev).name
            node = block.resolve()
            while node != node.parent:
                serial = node / "serial"
                if serial.is_file():
                    out = serial.read_text()
                    break
                node = node.parent
        else:
            return None
    except (OSError, subprocess.SubprocessError, StopIteration):
        return None
    match = re.search(r"(?<![0-9A-Fa-f])(000A27[0-9A-Fa-f]{10})(?![0-9A-Fa-f])", out)
    return match.group(1) if match else None


class UnsafeLocation(Exception):
    """A track location that points outside the iPod's music folder."""


class ReadOnlyError(Exception):
    """Raised when trying to change an iPod PodPilot can only read."""


@dataclass
class AddResult:
    added: list[Track] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    art: artwork.ArtResult | None = None


@dataclass
class SyncPlan:
    """What syncing a folder would do. Built by IPod.plan_sync without touching the iPod."""
    folder: Path
    to_add: list[tuple[Path, dict]] = field(default_factory=list)
    to_update: list[tuple[Path, Track, dict]] = field(default_factory=list)  # source changed since last sync
    unchanged: list[tuple[Path, Track]] = field(default_factory=list)
    linked: list[tuple[Path, Track]] = field(default_factory=list)  # already on the iPod, matched by tags
    duplicates: list[Path] = field(default_factory=list)  # same song twice in the folder
    not_in_folder: list[Track] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    add_bytes: int = 0


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    art: artwork.ArtResult | None = None


@dataclass
class HealthReport:
    orphan_files: list[Path]
    missing_tracks: list[Track]


class IPod:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.control = self.root / "iPod_Control"
        self.itunes_dir = self.control / "iTunes"
        self.db_path = self.itunes_dir / "iTunesDB"
        raw = self.db_path.read_bytes()
        self.db = ITunesDB(raw)
        self.guid: bytes | None = None
        self.read_only_reason = self._check_signature(raw)
        self.play_counts_merged = self._merge_play_counts()
        self._pending_covers: dict[int, bytes] = {}

    def _check_signature(self, raw: bytes) -> str | None:
        """Works out whether we can write this iPod's database. Returns why not, or None."""
        scheme = self.db.signature
        if scheme == hash58.SCHEME_NONE:
            return None
        if scheme == hash58.SCHEME_HASH72:
            return "This iPod (nano 5G or newer) signs its database with hash72, which PodPilot can't produce."
        if scheme != hash58.SCHEME_HASH58:
            return "This iPod uses a database signature PodPilot doesn't know."
        text = _guid_from_sysinfo(self.control / "Device") or _guid_from_os(self.root)
        if not text:
            return "Couldn't find this iPod's FireWire ID, which is needed to sign its database."
        try:
            guid = hash58.parse_guid(text)
        except hash58.Hash58Error as exc:
            return str(exc)
        # Only write if we can reproduce the signature iTunes left: proves the ID and algorithm match.
        if not hash58.verify(raw, guid):
            return "Couldn't reproduce this iPod's existing database signature, so changes are disabled to be safe."
        self.guid = guid
        return None

    @property
    def writable(self) -> bool:
        return self.read_only_reason is None

    def require_writable(self) -> None:
        if not self.writable:
            raise ReadOnlyError(self.read_only_reason)

    # ---- info

    @property
    def label(self) -> str:
        return volume_label(self.root)

    @property
    def name(self) -> str:
        return self.db.name or self.label

    def storage(self) -> tuple[int, int, int, int]:
        """(capacity, used, free, bytes used by tracks in the database)."""
        usage = shutil.disk_usage(self.root)
        music = sum(t.size for t in self.db.tracks)
        return usage.total, usage.used, usage.free, music

    def path_of(self, track: Track) -> Path:
        """Resolves a track's location, refusing anything outside iPod_Control/Music.

        The location comes from the device, so it's treated as untrusted: a crafted
        database must not be able to point deletes or copies at host files.
        """
        parts = [p for p in track.location.split(":") if p]
        if not parts or any(p in (".", "..") or "/" in p or "\\" in p or os.path.isabs(p) for p in parts):
            raise UnsafeLocation(track.location)
        music = (self.control / "Music").resolve()
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(music):
            raise UnsafeLocation(track.location)
        return path

    def safe_path_of(self, track: Track) -> Path | None:
        try:
            return self.path_of(track)
        except UnsafeLocation:
            return None

    def _location_of(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root.resolve())
        return ":" + ":".join(rel.parts)

    # ---- play counts written by the iPod since the last sync

    def _merge_play_counts(self) -> bool:
        path = self.itunes_dir / "Play Counts"
        if not path.is_file():
            return False
        data = path.read_bytes()
        if data[:4] != b"mhdp" or len(data) < 16:
            return False
        header_len, entry_len, count = struct.unpack_from("<III", data, 4)
        if count != len(self.db.tracks) or entry_len < 12 or header_len + count * entry_len > len(data):
            return False
        for i, track in enumerate(self.db.tracks):
            off = header_len + i * entry_len
            plays, last_played, bookmark = struct.unpack_from("<III", data, off)
            if plays:
                track.play_count += plays
                track.play_count2 = plays
                track.chunk.set("B", 0xB2, 1)
            if last_played:
                track.last_played = last_played
            if bookmark:
                track.bookmark_ms = bookmark
            if entry_len >= 0x10:
                rating = struct.unpack_from("<I", data, off + 0x0C)[0]
                if rating <= 100:
                    track.rating = rating
            if entry_len >= 0x1C:
                skips, last_skipped = struct.unpack_from("<II", data, off + 0x14)
                track.skip_count += skips
                if last_skipped:
                    track.last_skipped = last_skipped
        self.db.mark_dirty()
        return True

    # ---- saving

    def backup_dir(self) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.label or "iPod").strip("_-") or "iPod"
        return app_data_dir() / "backups" / safe

    def backup(self) -> Path:
        dest_dir = self.backup_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{_dt.datetime.now():%Y%m%d-%H%M%S}"
        dest = dest_dir / f"iTunesDB-{stamp}"
        n = 2
        while dest.exists():
            dest = dest_dir / f"iTunesDB-{stamp}-{n}"
            n += 1
        shutil.copy2(self.db_path, dest)
        old = sorted(dest_dir.glob("iTunesDB-*"))[:-KEEP_BACKUPS]
        for p in old:
            p.unlink(missing_ok=True)
        return dest

    def save(self) -> None:
        """Backs up the current database, then writes the new one atomically."""
        self.require_writable()
        data = self.db.to_bytes()
        if self.guid is not None:
            data = hash58.sign(data, self.guid)
            if not hash58.verify(data, self.guid):
                raise hash58.Hash58Error("signature check failed after signing")
        ITunesDB(data)  # refuse to write anything we can't read back
        self.backup()
        tmp = self.db_path.with_name("iTunesDB.podpilot-tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.db_path)
        if self.play_counts_merged:
            (self.itunes_dir / "Play Counts").unlink(missing_ok=True)
            self.play_counts_merged = False

    # ---- adding

    def _music_folders(self) -> list[Path]:
        music = self.control / "Music"
        folders = sorted(p for p in music.glob("F[0-9][0-9]") if p.is_dir()) if music.is_dir() else []
        if not folders:
            folders = [music / f"F{i:02d}" for i in range(20)]
            for f in folders:
                f.mkdir(parents=True, exist_ok=True)
        return folders

    def _new_file_path(self, ext: str) -> Path:
        folders = self._music_folders()
        while True:
            name = "".join(random.choices(string.ascii_uppercase, k=4)) + ext.lower()
            path = random.choice(folders) / name
            if not path.exists():
                return path

    def find_duplicate(self, meta: dict) -> Track | None:
        title, artist = meta["title"].casefold(), meta["artist"].casefold()
        for t in self.db.tracks:
            if (t.title.casefold() == title and t.artist.casefold() == artist
                    and abs(t.length_ms - meta["length_ms"]) <= 2000):
                return t
        return None

    def add_files(self, paths, progress: ProgressFn | None = None, cancelled: CancelFn | None = None,
                  skip_duplicates: bool = True) -> AddResult:
        self.require_writable()
        files = media.collect_audio_files(paths)
        result = AddResult()
        workdir = Path(tempfile.mkdtemp(prefix="podpilot-"))
        try:
            for i, src in enumerate(files):
                if cancelled and cancelled():
                    break
                if progress:
                    progress(i, len(files), src.name)
                try:
                    track = self._add_one(src, workdir, skip_duplicates)
                except (media.MediaError, OSError) as exc:
                    result.errors.append(str(exc))
                    continue
                if track is None:
                    result.skipped.append(src.name)
                else:
                    result.added.append(track)
            if progress:
                progress(len(files), len(files), "Writing database")
            if result.added:
                # A cancelled copy still keeps the songs already written. Art for those
                # waits for the next fetch, same as a cancelled folder sync.
                if not (cancelled and cancelled()):
                    result.art = self.sync_artwork(progress, cancelled)
                self.save()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return result

    def _add_one(self, src: Path, workdir: Path, skip_duplicates: bool, meta: dict | None = None) -> Track | None:
        meta = meta or media.read_metadata(src)
        if skip_duplicates and self.find_duplicate(meta):
            return None
        uploaded = self._upload(src, workdir, meta)
        track = self.db.add_track(uploaded)
        self._remember_cover(track, uploaded)
        return track

    def _remember_cover(self, track: Track, meta: dict) -> None:
        cover = meta.pop("_cover", None)
        if not cover:
            return
        self._pending_covers[track.dbid] = cover
        # A replaced file may carry a different picture than the one already on the iPod.
        if track.has_artwork:
            track.set_artwork_id(0)

    def _upload(self, src: Path, workdir: Path, meta: dict) -> dict:
        """Converts `src` if needed and copies it into the music folders. Returns meta with location."""
        meta = dict(meta)
        cover = artwork.local_cover(src)
        upload = src
        if media.needs_conversion(src, meta):
            upload = media.convert_to_mp3(src, workdir)
            converted = media.read_metadata(upload)
            for key in ("length_ms", "bitrate", "sample_rate", "codec"):
                meta[key] = converted[key]

        _, _, free, _ = self.storage()
        size = upload.stat().st_size
        if size + 5 * 1024 * 1024 > free:
            raise media.MediaError(f"not enough space on the iPod for {src.name}")

        dest = self._new_file_path(upload.suffix)
        shutil.copyfile(upload, dest)
        jpeg = artwork.ensure_jpeg(cover) if cover else None
        if jpeg and not artwork.embedded_cover(dest) and artwork.embed_cover(dest, jpeg):
            size = dest.stat().st_size
        meta["size"] = size
        meta["filetype_code"] = media.NATIVE[upload.suffix.lower()]
        meta["location"] = self._location_of(dest)
        if jpeg:
            meta["_cover"] = jpeg
        return meta

    def preview_cover(self, track: Track) -> bytes | None:
        """Cover bytes already on hand for the song list. Never touches the network."""
        pending = self._pending_covers.get(track.dbid)
        if pending:
            return pending
        path = self.safe_path_of(track)
        if path is not None and path.is_file():
            found = artwork.embedded_cover(path)
            if found:
                return found
        key = artwork.cache_key(track.artist, track.album, track.album_artist, track.title)
        if key is None:
            return None
        return artwork.CoverCache(app_data_dir() / "artwork").get(key)

    def artwork_pending(self) -> bool:
        """True when opening the iPod should look for covers. Recent misses are skipped."""
        cache = artwork.CoverCache(app_data_dir() / "artwork")
        for track in self.db.tracks:
            if track.has_artwork:
                continue
            key = artwork.cache_key(track.artist, track.album, track.album_artist, track.title)
            if key is None or (cache.remembered_miss(key) and track.dbid not in self._pending_covers):
                continue
            return True
        return False

    def sync_artwork(self, progress: ProgressFn | None = None, cancelled: CancelFn | None = None,
                     refresh: bool = False) -> artwork.ArtResult:
        """Fills in covers for songs that don't have one yet, including ones already on the iPod.

        Embedded art and pictures named cover.jpg / folder.jpg win. Otherwise the album
        (or the song, if it has no album) is looked up online. The same picture is shared
        by every song on that album. A song is only marked as having art once ArtworkDB
        is safely on disk.
        """
        self.require_writable()
        result = artwork.ArtResult()
        cache = artwork.CoverCache(app_data_dir() / "artwork")
        library = ipodart.ArtworkLibrary(self.control / "Artwork", device_dir=self.control / "Device")
        try:
            library.load()
        except ipodart.ArtError as exc:
            result.errors.append(str(exc))
            library = None

        groups: dict[str, list[Track]] = {}
        order: list[str] = []
        solos: list[Track] = []
        for track in self.db.tracks:
            if track.has_artwork:
                result.already += 1
                continue
            if library is not None and track.dbid in library.dbids:
                track.set_artwork_id(library.dbids[track.dbid])
                result.updated += 1
                self.db.mark_dirty()
                continue
            key = artwork.cache_key(track.artist, track.album, track.album_artist, track.title)
            if key is None:
                solos.append(track)
            elif key not in groups:
                order.append(key)
                groups[key] = [track]
            else:
                groups[key].append(track)

        work: list[tuple[str | None, list[Track]]] = [(key, groups[key]) for key in order]
        work.extend((None, [track]) for track in solos)
        linked: list[tuple[Track, int, int]] = []
        offline = False
        rendered: dict[bytes, list] = {}

        def thumbs_for(jpeg: bytes) -> list:
            if library is None:
                return []
            digest = hashlib.sha1(jpeg).digest()
            if digest not in rendered:
                built = []
                for fmt in library.write_formats():
                    thumb = artwork.render_thumb(jpeg, fmt)
                    if thumb is not None:
                        pixels, vpad, hpad = thumb
                        built.append((fmt, pixels, vpad, hpad))
                rendered[digest] = built
            return rendered[digest]

        for index, (key, tracks) in enumerate(work):
            if cancelled and cancelled():
                result.cancelled = True
                break
            label = tracks[0].album or tracks[0].title or "song"
            if progress:
                progress(index, len(work), f"Album art: {label}")
            jpeg = self._cover_for(tracks, key, cache, refresh, result, offline_box := [offline])
            offline = offline_box[0]
            if not jpeg:
                result.missing += len(tracks)
                continue
            if key and cache.get(key) != jpeg:
                try:
                    cache.store(key, jpeg)
                except OSError as exc:
                    result.errors.append(f"couldn't save the cover cache: {exc}")
            built = thumbs_for(jpeg)
            if library is not None and not built:
                result.errors.append(f"{label}: cover couldn't be prepared for this iPod")
            share = hashlib.sha1(jpeg).digest()
            for track in tracks:
                self._embed_on_device(track, jpeg, result)
                if library is None or not built:
                    continue
                image_id = library.add_rendered(track.dbid, len(jpeg), built, share)
                if image_id:
                    linked.append((track, image_id, len(jpeg)))
                else:
                    result.errors.append(f"{track.title}: cover couldn't be stored")

        if library is not None and library.dirty:
            try:
                library.save()
            except (ipodart.ArtError, OSError) as exc:
                result.errors.append(str(exc))
                linked.clear()
        for track, image_id, size in linked:
            track.set_artwork_id(image_id, size)
            result.updated += 1
            self.db.mark_dirty()
        return result

    def _cover_for(self, tracks: list[Track], key: str | None, cache: artwork.CoverCache,
                   refresh: bool, result: artwork.ArtResult, offline_box: list[bool]) -> bytes | None:
        for track in tracks:
            pending = self._pending_covers.get(track.dbid)
            if pending:
                return pending
            path = self.safe_path_of(track)
            if path is not None and path.is_file():
                found = artwork.local_cover(path)
                jpeg = artwork.ensure_jpeg(found) if found else None
                if jpeg:
                    return jpeg
        if key is None or offline_box[0]:
            return None
        if not refresh:
            cached = cache.get(key)
            if cached:
                return cached
            if cache.remembered_miss(key):
                return None
        sample = tracks[0]
        try:
            raw = artwork.lookup_online(sample.album_artist or sample.artist, sample.album, sample.title)
        except artwork.Offline as exc:
            offline_box[0] = True
            result.errors.append(f"Stopped looking online ({exc}). Covers already on the songs were still used.")
            return None
        jpeg = artwork.ensure_jpeg(raw) if raw else None
        if jpeg is None and key is not None:
            try:
                cache.store_miss(key)
            except OSError:
                pass
        return jpeg

    def _embed_on_device(self, track: Track, jpeg: bytes, result: artwork.ArtResult) -> None:
        path = self.safe_path_of(track)
        if path is None or not path.is_file() or path.suffix.lower() not in (".mp3", ".m4a", ".m4b"):
            return
        if artwork.embedded_cover(path):
            return
        if not artwork.embed_cover(path, jpeg):
            return
        try:
            track.size = path.stat().st_size
            self.db.mark_dirty()
        except OSError as exc:
            result.errors.append(f"{track.title}: {exc}")

    # ---- folder sync

    @property
    def manifest_path(self) -> Path:
        return self.control / "PodPilot" / "sync.json"

    def load_manifest(self) -> dict:
        """{'folder': str|None, 'files': {normalized source path: {dbid, size, mtime}}}, minus stale entries."""
        try:
            data = json.loads(self.manifest_path.read_text("utf-8"))
            files = data.get("files", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data, files = {}, {}
        live = {f"{t.dbid:016x}" for t in self.db.tracks}
        files = {k: v for k, v in files.items()
                 if isinstance(v, dict) and v.get("dbid") in live}
        return {"folder": data.get("folder") if isinstance(data, dict) else None, "files": files}

    def save_manifest(self, manifest: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.manifest_path)

    @staticmethod
    def _source_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _manifest_entry(track: Track, path: Path) -> dict:
        st = path.stat()
        return {"dbid": f"{track.dbid:016x}", "size": st.st_size, "mtime": round(st.st_mtime, 3)}

    def plan_sync(self, folder: Path, progress: ProgressFn | None = None,
                  cancelled: CancelFn | None = None) -> SyncPlan | None:
        """Compares a folder (recursively) with the iPod. Changes nothing. None if cancelled."""
        folder = Path(folder)
        files = media.collect_audio_files([folder])
        manifest = self.load_manifest()["files"]
        by_dbid = {f"{t.dbid:016x}": t for t in self.db.tracks}
        by_name: dict[tuple[str, str], list[Track]] = {}
        for t in self.db.tracks:
            by_name.setdefault((t.title.casefold(), t.artist.casefold()), []).append(t)

        plan = SyncPlan(folder=folder)
        claimed: set[int] = set()
        pending: dict[tuple[str, str], list[int]] = {}
        for i, src in enumerate(files):
            if cancelled and cancelled():
                return None
            if progress:
                progress(i, len(files), str(src.relative_to(folder)))
            try:
                st = src.stat()
                entry = manifest.get(self._source_key(src))
                track = by_dbid.get(entry["dbid"]) if entry else None
                if track is not None and track.id not in claimed:
                    claimed.add(track.id)
                    if entry.get("size") == st.st_size and entry.get("mtime") == round(st.st_mtime, 3):
                        plan.unchanged.append((src, track))
                    else:
                        plan.to_update.append((src, track, media.read_metadata(src)))
                    continue

                meta = media.read_metadata(src)
                key = (meta["title"].casefold(), meta["artist"].casefold())
                match = next((t for t in by_name.get(key, ())
                              if t.id not in claimed and abs(t.length_ms - meta["length_ms"]) <= 2000), None)
                if match is not None:
                    claimed.add(match.id)
                    plan.linked.append((src, match))
                elif any(abs(n - meta["length_ms"]) <= 2000 for n in pending.get(key, ())):
                    plan.duplicates.append(src)
                else:
                    pending.setdefault(key, []).append(meta["length_ms"])
                    plan.to_add.append((src, meta))
                    plan.add_bytes += st.st_size
            except (media.MediaError, OSError) as exc:
                plan.errors.append(f"{src.name}: {exc}")
        plan.not_in_folder = [t for t in self.db.tracks if t.id not in claimed]
        return plan

    def apply_sync(self, plan: SyncPlan, update_changed: bool = True, remove_missing: bool = False,
                   progress: ProgressFn | None = None, cancelled: CancelFn | None = None) -> SyncResult:
        """Carries out a plan from plan_sync. Saves the database and the sync manifest."""
        self.require_writable()
        result = SyncResult()
        manifest = self.load_manifest()
        files = manifest["files"]
        for src, track in plan.unchanged + plan.linked:
            try:
                files[self._source_key(src)] = self._manifest_entry(track, src)
            except OSError:
                pass

        if remove_missing and plan.not_in_folder:
            if progress:
                progress(0, 1, "Removing songs that aren't in the folder")
            result.errors += self.remove_tracks(plan.not_in_folder)
            result.removed = len(plan.not_in_folder)

        work = ([("update", item) for item in plan.to_update] if update_changed else []) + \
               [("add", item) for item in plan.to_add]
        replaced: list[Path] = []
        workdir = Path(tempfile.mkdtemp(prefix="podpilot-sync-"))
        try:
            for i, (kind, item) in enumerate(work):
                if cancelled and cancelled():
                    result.cancelled = True
                    break
                src = item[0]
                if progress:
                    progress(i, len(work), str(src.relative_to(plan.folder)))
                try:
                    if kind == "add":
                        uploaded = self._upload(src, workdir, item[1])
                        track = self.db.add_track(uploaded)
                        self._remember_cover(track, uploaded)
                        result.added += 1
                    else:
                        track, meta = item[1], item[2]
                        old = self.safe_path_of(track)
                        uploaded = self._upload(src, workdir, meta)
                        self.db.apply_meta(track, uploaded)
                        self._remember_cover(track, uploaded)
                        if old is not None:
                            replaced.append(old)  # deleted only once the new database is saved
                        result.updated += 1
                    files[self._source_key(src)] = self._manifest_entry(track, src)
                except (media.MediaError, OSError) as exc:
                    result.errors.append(f"{src.name}: {exc}")
            if progress:
                progress(len(work), len(work), "Writing database")
            if not result.cancelled and (result.added or result.updated or self._pending_covers):
                result.art = self.sync_artwork(progress, cancelled)
            self.save()
            manifest["folder"] = str(plan.folder)
            self.save_manifest(manifest)
            for old in replaced:
                try:
                    old.unlink(missing_ok=True)
                except OSError as exc:
                    result.errors.append(f"{old.name}: {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return result

    # ---- removing / exporting

    def remove_tracks(self, tracks: list[Track], progress: ProgressFn | None = None) -> list[str]:
        """Removes tracks from the database first, then deletes their files.

        Saving first means a failure can never leave songs listed whose files are gone. A file
        that can't be deleted (e.g. a USB hiccup) is just a stray the library check can remove.
        """
        self.require_writable()
        errors = []
        doomed: list[tuple[str, Path]] = []
        for t in tracks:
            try:
                path = self.safe_path_of(t)
            except OSError as exc:
                errors.append(f"{t.title}: {exc}")
                continue
            if path is None:
                errors.append(f"{t.title}: location outside the music folder, file left alone")
            else:
                doomed.append((t.title, path))

        if progress:
            progress(0, len(doomed), "Updating the iPod's library")
        self.db.remove_tracks(t.id for t in tracks)
        self.save()

        for i, (title, path) in enumerate(doomed):
            if progress:
                progress(i, len(doomed), title)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{title}: {exc}")
        return errors

    def export_tracks(self, tracks: list[Track], dest: Path, progress: ProgressFn | None = None,
                      cancelled: CancelFn | None = None) -> list[str]:
        """Copies tracks off the iPod as 'Artist/Album/NN Title.ext'."""
        def clean(s: str) -> str:
            s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
            return s[:120] or "Unknown"

        errors = []
        for i, t in enumerate(tracks):
            if cancelled and cancelled():
                break
            if progress:
                progress(i, len(tracks), t.title)
            src = self.safe_path_of(t)
            if src is None:
                errors.append(f"{t.title}: location outside the music folder, skipped")
                continue
            suffix = src.suffix.lower() if src.suffix.lower() in media.AUDIO_EXTENSIONS else ".mp3"
            folder = dest / clean(t.album_artist or t.artist or "Unknown Artist") / clean(t.album or "Unknown Album")
            prefix = f"{t.track_number:02d} " if t.track_number else ""
            target = folder / f"{prefix}{clean(t.title or src.stem)}{suffix}"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                n = 2
                while target.exists():
                    target = target.with_stem(f"{target.stem.rsplit(' (', 1)[0]} ({n})")
                    n += 1
                shutil.copy2(src, target)
            except OSError as exc:
                errors.append(f"{t.title}: {exc}")
        return errors

    # ---- health check

    def health(self) -> HealthReport:
        paths = {t.id: self.safe_path_of(t) for t in self.db.tracks}
        known = {os.path.normcase(str(p)) for p in paths.values() if p}
        music = (self.control / "Music").resolve()
        orphans = []
        if music.is_dir():
            for f in music.rglob("*"):
                if f.is_file() and f.suffix.lower() in media.AUDIO_EXTENSIONS and os.path.normcase(str(f)) not in known:
                    orphans.append(f)
        missing = [t for t in self.db.tracks if paths[t.id] is None or not paths[t.id].is_file()]
        return HealthReport(sorted(orphans), missing)

    def import_orphans(self, files: list[Path]) -> AddResult:
        """Registers audio files that are on the iPod but not in its database."""
        self.require_writable()
        result = AddResult()
        for f in files:
            try:
                meta = media.read_metadata(f)
                if f.suffix.lower() not in media.NATIVE:
                    raise media.MediaError(f"{f.name}: format not playable on the iPod")
                meta["filetype_code"] = media.NATIVE[f.suffix.lower()]
                meta["location"] = self._location_of(f)
                track = self.db.add_track(meta)
                result.added.append(track)
            except media.MediaError as exc:
                result.errors.append(str(exc))
        if result.added:
            result.art = self.sync_artwork()
            self.save()
        return result

    def delete_files(self, files: list[Path]) -> None:
        self.require_writable()
        for f in files:
            f.unlink(missing_ok=True)

    def drop_missing(self, tracks: list[Track]) -> None:
        self.require_writable()
        self.db.remove_tracks(t.id for t in tracks)
        self.save()

    # ---- eject

    def eject(self) -> bool:
        if os.name == "nt":
            drive = str(self.root)[:2]
            script = f"(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{drive}').InvokeVerb('Eject')"
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.run(["diskutil", "eject", str(self.root)], capture_output=True)
        else:
            subprocess.run(["udisksctl", "unmount", "-b", self._block_device()], capture_output=True)
        return True

    def _block_device(self) -> str:
        with open("/proc/mounts") as fh:
            for line in fh:
                dev, mnt = line.split()[:2]
                if Path(mnt) == self.root:
                    return dev
        return ""
