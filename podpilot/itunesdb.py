"""Reader/writer for the iTunesDB used by click-wheel iPods (classic 1G-5.5G, mini, nano 1G-2G).

The database is a tree of chunks. Every chunk starts with a 4-byte magic and a
header length; most also store a total length (header + children), while list
chunks (mhlt, mhlp, mhla, mhli) store a child count instead.

The parser keeps every header byte-for-byte and leaves unknown chunks untouched, so
loading and saving an unmodified database reproduces the original file exactly.
Only the fields we understand are ever rewritten.

Nano 3G+ and classic 6G+ use a signed database (hash58/hash72), which is not supported.
"""

from __future__ import annotations

import datetime as _dt
import random
import struct
import unicodedata

MAC_EPOCH = _dt.datetime(1904, 1, 1)

LISTS = {b"mhlt", b"mhlp", b"mhla", b"mhli"}
CONTAINERS = {b"mhbd", b"mhsd", b"mhit", b"mhyp", b"mhip", b"mhia", b"mhii"}

# mhsd dataset types
DS_TRACKS = 1
DS_PLAYLISTS = 2
DS_PODCASTS = 3
DS_ALBUMS = 4
DS_SMART = 5

# string mhod types
MHOD_TITLE = 1
MHOD_LOCATION = 2
MHOD_ALBUM = 3
MHOD_ARTIST = 4
MHOD_GENRE = 5
MHOD_FILETYPE = 6
MHOD_COMMENT = 8
MHOD_COMPOSER = 12
MHOD_ALBUM_ARTIST = 22
MHOD_SORT_ARTIST = 23
MHOD_SORT_TITLE = 27
MHOD_SORT_ALBUM = 28
MHOD_SORT_ALBUM_ARTIST = 29
MHOD_SORT_COMPOSER = 30

MHOD_SMART_PREFS = 50
MHOD_INDEX = 52
MHOD_JUMP_TABLE = 53
MHOD_PLAYLIST_POSITION = 100

# Sort orders used by the library index mhods (type 52/53) in the master playlist.
SORT_TITLE = 0x03
SORT_ALBUM = 0x04
SORT_ARTIST = 0x05
SORT_GENRE = 0x07
SORT_COMPOSER = 0x12
DEFAULT_INDEX_SORTS = (SORT_TITLE, SORT_ARTIST, SORT_ALBUM, SORT_GENRE, SORT_COMPOSER)

# filetype code -> (description, mhit 0x90 value)
FILETYPES = {
    "MP3 ": ("MPEG audio file", 0x0C),
    "M4A ": ("AAC audio file", 0x33),
    "M4P ": ("Protected AAC audio file", 0x33),
    "WAV ": ("WAV audio file", 0x01),
    "AIFF": ("AIFF audio file", 0x01),
}


def mac_now() -> int:
    return int((_dt.datetime.now() - MAC_EPOCH).total_seconds())


def mac_to_datetime(value: int) -> _dt.datetime | None:
    if not value:
        return None
    return MAC_EPOCH + _dt.timedelta(seconds=value)


class DatabaseError(Exception):
    pass


# --------------------------------------------------------------------------- chunks


class Chunk:
    """One node of the iTunesDB tree.

    `header` is the full raw header (magic included). Containers have `children`;
    leaves (mhod, unknown chunks) keep their payload in `body`.
    """

    __slots__ = ("magic", "header", "children", "body")

    def __init__(self, magic: bytes, header: bytearray, children: list[Chunk] | None = None,
                 body: bytes = b""):
        self.magic = magic
        self.header = header
        self.children = children
        self.body = body

    def get(self, fmt: str, offset: int):
        if offset + struct.calcsize(fmt) > len(self.header):
            return 0
        return struct.unpack_from("<" + fmt, self.header, offset)[0]

    def set(self, fmt: str, offset: int, value) -> None:
        if offset + struct.calcsize(fmt) <= len(self.header):
            struct.pack_into("<" + fmt, self.header, offset, value)

    def kids(self, magic: bytes) -> list[Chunk]:
        return [c for c in self.children or () if c.magic == magic]

    def copy(self) -> Chunk:
        kids = None if self.children is None else [c.copy() for c in self.children]
        return Chunk(self.magic, bytearray(self.header), kids, self.body)


def _u32(data, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


MAX_DEPTH = 8


def parse_chunk(data: bytes, offset: int, depth: int = 0) -> tuple[Chunk, int]:
    magic = bytes(data[offset:offset + 4])
    if len(magic) != 4 or not magic.startswith(b"mh"):
        raise DatabaseError(f"bad chunk magic {magic!r} at {offset:#x}")
    if depth > MAX_DEPTH or offset + 12 > len(data):
        raise DatabaseError(f"malformed chunk {magic!r} at {offset:#x}")
    header_len = _u32(data, offset + 4)
    # every chunk consumes at least 12 bytes, so the loops below always advance
    if header_len < 12 or offset + header_len > len(data):
        raise DatabaseError(f"chunk {magic!r} at {offset:#x} has a bad header length")
    header = bytearray(data[offset:offset + header_len])

    if magic in LISTS:
        pos = offset + header_len
        count = _u32(data, offset + 8)
        if count > (len(data) - pos) // 12:
            raise DatabaseError(f"list {magic!r} at {offset:#x} claims too many children")
        children = []
        for _ in range(count):
            child, pos = parse_chunk(data, pos, depth + 1)
            children.append(child)
        return Chunk(magic, header, children), pos

    end = offset + _u32(data, offset + 8)
    if end > len(data) or end < offset + header_len:
        raise DatabaseError(f"chunk {magic!r} at {offset:#x} overruns the file")
    pos = offset + header_len

    if magic in CONTAINERS and not (magic == b"mhsd" and data[pos:pos + 4] not in LISTS):
        children = []
        while pos < end:
            child, pos = parse_chunk(data, pos, depth + 1)
            children.append(child)
        if pos != end:
            raise DatabaseError(f"children of {magic!r} at {offset:#x} overrun it")
        return Chunk(magic, header, children), end

    return Chunk(magic, header, None, bytes(data[pos:end])), end


def serialize(chunk: Chunk) -> bytes:
    if chunk.children is None:
        body = chunk.body
    else:
        body = b"".join(serialize(c) for c in chunk.children)
        n_mhod = sum(1 for c in chunk.children if c.magic == b"mhod")
        if chunk.magic == b"mhbd":
            chunk.set("I", 0x14, len(chunk.children))
        elif chunk.magic in (b"mhit", b"mhia", b"mhii"):
            chunk.set("I", 0x0C, n_mhod)
        elif chunk.magic == b"mhyp":
            chunk.set("I", 0x0C, n_mhod)
            chunk.set("I", 0x10, len(chunk.children) - n_mhod)
        elif chunk.magic == b"mhip":
            chunk.set("I", 0x0C, len(chunk.children))

    if chunk.magic in LISTS:
        chunk.set("I", 0x08, len(chunk.children))
    else:
        chunk.set("I", 0x08, len(chunk.header) + len(body))
    return bytes(chunk.header) + body


# --------------------------------------------------------------------------- mhods


def mhod_type(chunk: Chunk) -> int:
    return chunk.get("I", 0x0C)


def is_string_mhod(chunk: Chunk) -> bool:
    t = mhod_type(chunk)
    return chunk.magic == b"mhod" and (1 <= t <= 14 or 18 <= t <= 31 or 200 <= t <= 300)


def read_string_mhod(chunk: Chunk) -> str:
    body = chunk.body
    if len(body) < 16:
        return ""
    length, encoding = struct.unpack_from("<II", body, 4)
    raw = body[16:16 + length]
    if encoding == 2:
        return raw.decode("utf-8", "replace")
    return raw.decode("utf-16-le", "replace")


def make_string_mhod(mtype: int, text: str) -> Chunk:
    raw = text.encode("utf-16-le")
    header = bytearray(struct.pack("<4sIIIII", b"mhod", 24, 0, mtype, 0, 0))
    body = struct.pack("<IIII", 1, len(raw), 1, 0) + raw
    return Chunk(b"mhod", header, None, body)


def make_position_mhod(position: int) -> Chunk:
    header = bytearray(struct.pack("<4sIIIII", b"mhod", 24, 0, MHOD_PLAYLIST_POSITION, 0, 0))
    return Chunk(b"mhod", header, None, struct.pack("<I16x", position))


# --------------------------------------------------------------------------- tracks


class _Field:
    def __init__(self, fmt: str, offset: int):
        self.fmt, self.offset = fmt, offset

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.chunk.get(self.fmt, self.offset)

    def __set__(self, obj, value):
        obj.chunk.set(self.fmt, self.offset, value)


class _StrField:
    def __init__(self, mtype: int):
        self.mtype = mtype

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.get_string(self.mtype)

    def __set__(self, obj, value):
        obj.set_string(self.mtype, value)


class Track:
    """View over an mhit chunk."""

    id = _Field("I", 0x10)
    visible = _Field("I", 0x14)
    filetype_code = _Field("I", 0x18)
    compilation = _Field("B", 0x1E)
    rating = _Field("B", 0x1F)  # 0-100, 20 per star
    time_modified = _Field("I", 0x20)
    size = _Field("I", 0x24)
    length_ms = _Field("I", 0x28)
    track_number = _Field("I", 0x2C)
    track_count = _Field("I", 0x30)
    year = _Field("I", 0x34)
    bitrate = _Field("I", 0x38)
    play_count = _Field("I", 0x50)
    play_count2 = _Field("I", 0x54)
    last_played = _Field("I", 0x58)
    disc_number = _Field("I", 0x5C)
    disc_count = _Field("I", 0x60)
    date_added = _Field("I", 0x68)
    bookmark_ms = _Field("I", 0x6C)
    dbid = _Field("Q", 0x70)
    skip_count = _Field("I", 0x9C)
    last_skipped = _Field("I", 0xA0)

    title = _StrField(MHOD_TITLE)
    location = _StrField(MHOD_LOCATION)
    album = _StrField(MHOD_ALBUM)
    artist = _StrField(MHOD_ARTIST)
    genre = _StrField(MHOD_GENRE)
    filetype = _StrField(MHOD_FILETYPE)
    comment = _StrField(MHOD_COMMENT)
    composer = _StrField(MHOD_COMPOSER)
    album_artist = _StrField(MHOD_ALBUM_ARTIST)

    def __init__(self, chunk: Chunk):
        self.chunk = chunk

    def __repr__(self):
        return f"<Track {self.id} {self.artist!r} - {self.title!r}>"

    @property
    def sample_rate(self) -> int:
        return self.chunk.get("I", 0x3C) >> 16

    @property
    def kind(self) -> str:
        return self.filetype_code.to_bytes(4, "big").decode("latin-1").strip()

    def _string_mhod(self, mtype: int) -> Chunk | None:
        for c in self.chunk.children:
            if c.magic == b"mhod" and mhod_type(c) == mtype:
                return c
        return None

    def get_string(self, mtype: int) -> str:
        c = self._string_mhod(mtype)
        return read_string_mhod(c) if c else ""

    def set_string(self, mtype: int, value: str | None) -> None:
        old = self._string_mhod(mtype)
        if not value:
            if old is not None:
                self.chunk.children.remove(old)
            return
        new = make_string_mhod(mtype, value)
        if old is not None:
            self.chunk.children[self.chunk.children.index(old)] = new
        else:
            self.chunk.children.append(new)

    def sort_value(self, mtype: int, sort_mtype: int) -> str:
        return self.get_string(sort_mtype) or self.get_string(mtype)


# --------------------------------------------------------------------------- playlists


class Playlist:
    """View over an mhyp chunk from the playlists dataset (type 2)."""

    def __init__(self, db: ITunesDB, chunk: Chunk):
        self.db = db
        self.chunk = chunk

    @property
    def id(self) -> int:
        return self.chunk.get("Q", 0x1C)

    @property
    def is_master(self) -> bool:
        return bool(self.chunk.get("B", 0x14))

    @property
    def is_smart(self) -> bool:
        return any(c.magic == b"mhod" and mhod_type(c) == MHOD_SMART_PREFS
                   for c in self.chunk.children)

    @property
    def is_podcasts(self) -> bool:
        return bool(self.chunk.get("H", 0x2A))

    @property
    def name(self) -> str:
        for c in self.chunk.children:
            if c.magic == b"mhod" and mhod_type(c) == MHOD_TITLE:
                return read_string_mhod(c)
        return ""

    @property
    def track_ids(self) -> list[int]:
        return [c.get("I", 0x18) for c in self.chunk.kids(b"mhip")]

    @property
    def editable(self) -> bool:
        return not (self.is_master or self.is_smart or self.is_podcasts)

    def __repr__(self):
        return f"<Playlist {self.name!r} ({len(self.track_ids)} tracks)>"


# --------------------------------------------------------------------------- sorting


_ARTICLES = ("the ", "a ", "an ")


def _sort_key(text: str) -> tuple:
    """Approximates iTunes ordering: letters, then digits/symbols, then empty values."""
    s = unicodedata.normalize("NFKD", text or "").casefold().strip()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    s = " ".join(s.split())
    for article in _ARTICLES:
        if s.startswith(article) and len(s) > len(article):
            s = s[len(article):]
            break
    if not s:
        return (2, "")
    return (0 if s[0].isalpha() else 1, s)


def _jump_letter(key: tuple) -> int:
    group, s = key
    if group == 2:
        return 0
    if group == 1:
        return ord("0")
    return ord(s[0].upper()) if s[0].isascii() else ord(unicodedata.normalize("NFC", s[0]).upper()[0])


# --------------------------------------------------------------------------- database


class ITunesDB:
    def __init__(self, data: bytes):
        if data[:4] != b"mhbd":
            raise DatabaseError("not an iTunesDB file")
        try:
            self.root, end = parse_chunk(data, 0)
        except (struct.error, IndexError) as exc:
            raise DatabaseError(f"corrupt database: {exc}") from exc
        if end != len(data):
            raise DatabaseError("trailing data after database")
        if any(self.root.header[0x58:0x6C]) or any(self.root.header[0x72:0xA0]):
            raise DatabaseError("this iPod uses a signed (hashed) database, which is not supported")
        if self._list(DS_TRACKS) is None:
            raise DatabaseError("database has no track list")
        self.tracks = [Track(c) for c in self._list(DS_TRACKS).children]
        self.dirty = False
        self._next_id = max([t.id for t in self.tracks] + self._mhip_ids() + [100]) + 1

    @classmethod
    def load(cls, path) -> ITunesDB:
        with open(path, "rb") as fh:
            return cls(fh.read())

    def to_bytes(self) -> bytes:
        if self.dirty:
            self.rebuild_indices()
        return serialize(self.root)

    # ---- structure helpers

    def _section(self, ds_type: int) -> Chunk | None:
        for c in self.root.children:
            if c.magic == b"mhsd" and c.get("I", 0x0C) == ds_type:
                return c
        return None

    def _list(self, ds_type: int) -> Chunk | None:
        sec = self._section(ds_type)
        if sec is None or not sec.children:
            return None
        return sec.children[0]

    def _all_mhyps(self, *ds_types: int) -> list[Chunk]:
        out = []
        for ds in ds_types:
            lst = self._list(ds)
            if lst is not None:
                out.extend(lst.kids(b"mhyp"))
        return out

    def _mhip_ids(self) -> list[int]:
        return [ip.get("I", 0x14) for yp in self._all_mhyps(DS_PLAYLISTS, DS_PODCASTS, DS_SMART)
                for ip in yp.kids(b"mhip")]

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _masters(self) -> list[Chunk]:
        return [yp for yp in self._all_mhyps(DS_PLAYLISTS, DS_PODCASTS) if yp.get("B", 0x14)]

    def _mirrors(self, playlist_id: int) -> list[Chunk]:
        """The same playlist lives in dataset 2 and (if present) dataset 3."""
        return [yp for yp in self._all_mhyps(DS_PLAYLISTS, DS_PODCASTS) if yp.get("Q", 0x1C) == playlist_id]

    # ---- queries

    @property
    def name(self) -> str:
        master = self.master
        return master.name if master else "iPod"

    @property
    def playlists(self) -> list[Playlist]:
        lst = self._list(DS_PLAYLISTS)
        return [Playlist(self, c) for c in lst.kids(b"mhyp")] if lst is not None else []

    @property
    def master(self) -> Playlist | None:
        return next((p for p in self.playlists if p.is_master), None)

    def track_by_id(self, track_id: int) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    # ---- track mutations

    def _mhit_template(self) -> bytearray:
        if self.tracks:
            src = self.tracks[0].chunk.header
            header = bytearray(len(src))
            header[0:8] = src[0:8]
            header[0x7E:0x80] = src[0x7E:0x80]
            return header
        header = bytearray(0x184)
        header[0:8] = struct.pack("<4sI", b"mhit", 0x184)
        header[0x7E:0x80] = b"\xff\xff"
        return header

    def add_track(self, meta: dict) -> Track:
        """Adds a track entry. `meta` must contain `location` (iPod-style, colon separated)."""
        code = meta.get("filetype_code", "MP3 ")
        desc, unk144 = FILETYPES.get(code, ("Audio file", 0x0C))
        now = mac_now()
        dbid = random.getrandbits(64)
        sample_rate = int(meta.get("sample_rate") or 44100)

        chunk = Chunk(b"mhit", self._mhit_template(), [])
        t = Track(chunk)
        t.id = self._new_id()
        t.visible = 1
        t.filetype_code = int.from_bytes(code.encode("latin-1"), "big")
        chunk.set("B", 0x1D, 1 if code == "MP3 " else 0)
        t.compilation = 1 if meta.get("compilation") else 0
        t.time_modified = now
        t.size = int(meta.get("size") or 0)
        t.length_ms = int(meta.get("length_ms") or 0)
        t.track_number = int(meta.get("track_number") or 0)
        t.track_count = int(meta.get("track_count") or 0)
        t.year = int(meta.get("year") or 0)
        t.bitrate = int(meta.get("bitrate") or 0)
        chunk.set("I", 0x3C, (sample_rate & 0xFFFF) << 16)
        t.disc_number = int(meta.get("disc_number") or 0)
        t.disc_count = int(meta.get("disc_count") or 0)
        t.date_added = now
        t.dbid = dbid
        chunk.set("f", 0x88, float(sample_rate))
        chunk.set("I", 0x90, unk144)
        chunk.set("B", 0xA4, 2)        # no artwork
        chunk.set("Q", 0xA8, dbid)
        chunk.set("B", 0xB2, 2)        # not played yet
        chunk.set("I", 0xD0, 1)        # media type: audio

        for mtype, key in ((MHOD_TITLE, "title"), (MHOD_ARTIST, "artist"), (MHOD_ALBUM, "album"),
                           (MHOD_ALBUM_ARTIST, "album_artist"), (MHOD_GENRE, "genre"),
                           (MHOD_COMPOSER, "composer"), (MHOD_COMMENT, "comment")):
            t.set_string(mtype, meta.get(key) or "")
        t.set_string(MHOD_FILETYPE, desc)
        t.set_string(MHOD_LOCATION, meta["location"])

        self._list(DS_TRACKS).children.append(chunk)
        self.tracks.append(t)
        for master in self._masters():
            self._append_mhip(master, t)
        self.dirty = True
        return t

    def _append_mhip(self, mhyp: Chunk, track: Track) -> None:
        position = 1
        for ip in mhyp.kids(b"mhip"):
            for c in ip.children:
                if c.magic == b"mhod" and mhod_type(c) == MHOD_PLAYLIST_POSITION and len(c.body) >= 4:
                    position = max(position, struct.unpack_from("<I", c.body)[0] + 1)
        header = bytearray(76)
        struct.pack_into("<4sI", header, 0, b"mhip", 76)
        struct.pack_into("<I", header, 0x14, self._new_id())
        struct.pack_into("<I", header, 0x18, track.id)
        struct.pack_into("<I", header, 0x1C, mac_now())
        struct.pack_into("<Q", header, 0x2C, track.dbid)
        mhyp.children.append(Chunk(b"mhip", header, [make_position_mhod(position)]))

    def remove_tracks(self, track_ids) -> None:
        ids = set(track_ids)
        tl = self._list(DS_TRACKS)
        tl.children = [c for c in tl.children if c.get("I", 0x10) not in ids]
        self.tracks = [t for t in self.tracks if t.id not in ids]
        for yp in self._all_mhyps(DS_PLAYLISTS, DS_PODCASTS, DS_SMART):
            yp.children = [c for c in yp.children
                           if not (c.magic == b"mhip" and c.get("I", 0x18) in ids)]
        self.dirty = True

    def mark_dirty(self) -> None:
        self.dirty = True

    # ---- playlist mutations

    def create_playlist(self, name: str) -> Playlist:
        masters = self._masters()
        if not masters:
            raise DatabaseError("database has no master playlist")
        template = masters[0]
        header = bytearray(template.header)
        header[0x10:] = bytes(len(header) - 0x10)
        struct.pack_into("<I", header, 0x18, mac_now())
        struct.pack_into("<Q", header, 0x1C, random.getrandbits(64))
        struct.pack_into("<H", header, 0x28, 1)  # one string mhod
        struct.pack_into("<I", header, 0x2C, 1)  # manual sort order
        children = [make_string_mhod(MHOD_TITLE, name)]
        prefs = [c for c in template.children if c.magic == b"mhod" and mhod_type(c) == 100]
        if prefs:
            children.append(prefs[0].copy())
        chunk = Chunk(b"mhyp", header, children)
        for ds in (DS_PLAYLISTS, DS_PODCASTS):
            lst = self._list(ds)
            if lst is not None:
                lst.children.append(chunk if ds == DS_PLAYLISTS else chunk.copy())
        self.dirty = True
        return Playlist(self, chunk)

    def rename_playlist(self, playlist: Playlist, name: str) -> None:
        for yp in self._mirrors(playlist.id):
            for i, c in enumerate(yp.children):
                if c.magic == b"mhod" and mhod_type(c) == MHOD_TITLE:
                    yp.children[i] = make_string_mhod(MHOD_TITLE, name)
        self.dirty = True

    def delete_playlist(self, playlist: Playlist) -> None:
        if not playlist.editable:
            raise DatabaseError("this playlist can't be deleted")
        for ds in (DS_PLAYLISTS, DS_PODCASTS):
            lst = self._list(ds)
            if lst is not None:
                lst.children = [c for c in lst.children if c.get("Q", 0x1C) != playlist.id]
        self.dirty = True

    def add_to_playlist(self, playlist: Playlist, tracks: list[Track]) -> None:
        for yp in self._mirrors(playlist.id):
            for t in tracks:
                self._append_mhip(yp, t)
        self.dirty = True

    def remove_from_playlist(self, playlist: Playlist, track_ids) -> None:
        ids = set(track_ids)
        for yp in self._mirrors(playlist.id):
            yp.children = [c for c in yp.children
                           if not (c.magic == b"mhip" and c.get("I", 0x18) in ids)]
        self.dirty = True

    # ---- library indices (mhod 52/53)

    def _index_order(self, sort: int) -> list[int]:
        def k(t: Track, mtype: int, smtype: int = 0) -> tuple:
            return _sort_key(t.sort_value(mtype, smtype) if smtype else t.get_string(mtype))

        def album_tail(t: Track) -> tuple:
            return (t.disc_number, t.track_number, k(t, MHOD_TITLE, MHOD_SORT_TITLE))

        keys = {
            SORT_TITLE: lambda t: (k(t, MHOD_TITLE, MHOD_SORT_TITLE),),
            SORT_ALBUM: lambda t: (k(t, MHOD_ALBUM, MHOD_SORT_ALBUM),) + album_tail(t),
            SORT_ARTIST: lambda t: (k(t, MHOD_ARTIST, MHOD_SORT_ARTIST), k(t, MHOD_ALBUM, MHOD_SORT_ALBUM)) + album_tail(t),
            SORT_GENRE: lambda t: (k(t, MHOD_GENRE), k(t, MHOD_ARTIST, MHOD_SORT_ARTIST),
                                   k(t, MHOD_ALBUM, MHOD_SORT_ALBUM)) + album_tail(t),
            SORT_COMPOSER: lambda t: (k(t, MHOD_COMPOSER, MHOD_SORT_COMPOSER), k(t, MHOD_ALBUM, MHOD_SORT_ALBUM)) + album_tail(t),
            0x23: lambda t: (k(t, MHOD_ALBUM, MHOD_SORT_ALBUM), k(t, MHOD_ALBUM_ARTIST, MHOD_SORT_ALBUM_ARTIST)) + album_tail(t),
            0x24: lambda t: (k(t, MHOD_ALBUM_ARTIST, MHOD_SORT_ALBUM_ARTIST) if t.album_artist else k(t, MHOD_ARTIST, MHOD_SORT_ARTIST),
                             k(t, MHOD_ALBUM, MHOD_SORT_ALBUM)) + album_tail(t),
        }
        keyfn = keys.get(sort, keys[SORT_TITLE])
        return sorted(range(len(self.tracks)), key=lambda i: keyfn(self.tracks[i]))

    def _primary_key(self, sort: int, t: Track) -> tuple:
        mtype, smtype = {
            SORT_TITLE: (MHOD_TITLE, MHOD_SORT_TITLE),
            SORT_ALBUM: (MHOD_ALBUM, MHOD_SORT_ALBUM),
            SORT_ARTIST: (MHOD_ARTIST, MHOD_SORT_ARTIST),
            SORT_GENRE: (MHOD_GENRE, 0),
            SORT_COMPOSER: (MHOD_COMPOSER, MHOD_SORT_COMPOSER),
        }.get(sort, (MHOD_TITLE, MHOD_SORT_TITLE))
        return _sort_key(t.sort_value(mtype, smtype) if smtype else t.get_string(mtype))

    def _make_index_mhod(self, sort: int, order: list[int]) -> Chunk:
        header = bytearray(struct.pack("<4sIIIII", b"mhod", 24, 0, MHOD_INDEX, 0, 0))
        body = struct.pack("<II40x", sort, len(order)) + struct.pack(f"<{len(order)}I", *order)
        return Chunk(b"mhod", header, None, body)

    def _make_jump_mhod(self, sort: int, order: list[int]) -> Chunk:
        entries: list[list[int]] = []
        for pos, idx in enumerate(order):
            letter = _jump_letter(self._primary_key(sort, self.tracks[idx]))
            if entries and entries[-1][0] == letter:
                entries[-1][2] += 1
            else:
                entries.append([letter, pos, 1])
        header = bytearray(struct.pack("<4sIIIII", b"mhod", 24, 0, MHOD_JUMP_TABLE, 0, 0))
        body = struct.pack("<II8x", sort, len(entries)) + b"".join(
            struct.pack("<H2xII", letter & 0xFFFF, start, count) for letter, start, count in entries)
        return Chunk(b"mhod", header, None, body)

    def rebuild_indices(self) -> None:
        orders: dict[int, list[int]] = {}

        def order(sort: int) -> list[int]:
            if sort not in orders:
                orders[sort] = self._index_order(sort)
            return orders[sort]

        for master in self._masters():
            kids = master.children
            if not any(c.magic == b"mhod" and mhod_type(c) == MHOD_INDEX for c in kids):
                insert_at = next((i for i, c in enumerate(kids) if c.magic == b"mhip"), len(kids))
                new = []
                for s in DEFAULT_INDEX_SORTS:
                    new += [self._make_index_mhod(s, order(s)), self._make_jump_mhod(s, order(s))]
                kids[insert_at:insert_at] = new
                continue
            for i, c in enumerate(kids):
                if c.magic != b"mhod" or mhod_type(c) not in (MHOD_INDEX, MHOD_JUMP_TABLE):
                    continue
                sort = struct.unpack_from("<I", c.body, 0)[0]
                if mhod_type(c) == MHOD_INDEX:
                    kids[i] = self._make_index_mhod(sort, order(sort))
                else:
                    kids[i] = self._make_jump_mhod(sort, order(sort))
        self.dirty = False
