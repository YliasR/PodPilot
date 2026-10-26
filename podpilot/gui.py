"""PySide6 desktop UI."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QRectF, QSize, QSortFilterProxyModel, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QProgressDialog, QPushButton, QSpinBox, QSplitter, QStackedWidget, QStyle,
                               QTableView, QToolBar, QVBoxLayout, QWidget)

from . import __version__, media
from .device import IPod, find_ipods
from .itunesdb import DatabaseError, Track, mac_to_datetime

ACCENT = "#4f8cff"


# --------------------------------------------------------------------------- helpers


def fmt_duration(ms: int) -> str:
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_date(mac_ts: int) -> str:
    dt = mac_to_datetime(mac_ts)
    return dt.strftime("%Y-%m-%d") if dt else ""


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e8e8ec"))
    p.drawRoundedRect(QRectF(14, 2, 36, 60), 8, 8)
    p.setBrush(QColor("#1d1f24"))
    p.drawRoundedRect(QRectF(19, 7, 26, 20), 3, 3)
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(QRectF(21, 9, 22, 16), 2, 2)
    p.setBrush(QColor("#b9bcc4"))
    p.drawEllipse(QRectF(20, 32, 24, 24))
    p.setBrush(QColor("#e8e8ec"))
    p.drawEllipse(QRectF(27, 39, 10, 10))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- track table


COLUMNS = [
    ("Title", lambda t: t.title, lambda t: t.title.casefold()),
    ("Artist", lambda t: t.artist, lambda t: (t.artist.casefold(), t.album.casefold(), t.disc_number, t.track_number)),
    ("Album", lambda t: t.album, lambda t: (t.album.casefold(), t.disc_number, t.track_number)),
    ("Genre", lambda t: t.genre, lambda t: t.genre.casefold()),
    ("#", lambda t: t.track_number or "", lambda t: (t.disc_number, t.track_number)),
    ("Time", lambda t: fmt_duration(t.length_ms), lambda t: t.length_ms),
    ("Year", lambda t: t.year or "", lambda t: t.year),
    ("Plays", lambda t: t.play_count or "", lambda t: t.play_count),
    ("Format", lambda t: f"{t.kind} {t.bitrate}k" if t.bitrate else t.kind, lambda t: (t.kind, t.bitrate)),
    ("Size", lambda t: fmt_size(t.size), lambda t: t.size),
    ("Added", lambda t: fmt_date(t.date_added), lambda t: t.date_added),
]
RIGHT_ALIGNED = {"#", "Time", "Year", "Plays", "Size"}


class TrackModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.tracks: list[Track] = []
        self.playing_id: int | None = None

    def set_tracks(self, tracks: list[Track]) -> None:
        self.beginResetModel()
        self.tracks = list(tracks)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][0]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole and COLUMNS[section][0] in RIGHT_ALIGNED:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        t = self.tracks[index.row()]
        name = COLUMNS[index.column()][0]
        if role == Qt.DisplayRole:
            text = COLUMNS[index.column()][1](t)
            if name == "Title" and t.id == self.playing_id:
                return f"▶  {text}"
            return text
        if role == Qt.TextAlignmentRole and name in RIGHT_ALIGNED:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole and name == "Title":
            return t.location
        return None

    def sort_key(self, row: int, column: int):
        return COLUMNS[column][2](self.tracks[row])


class TrackFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.needle = ""

    def set_needle(self, text: str) -> None:
        self.needle = text.casefold().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self.needle:
            return True
        t = self.sourceModel().tracks[row]
        hay = " ".join((t.title, t.artist, t.album, t.album_artist, t.genre)).casefold()
        return all(word in hay for word in self.needle.split())

    def lessThan(self, left, right):
        model = self.sourceModel()
        return model.sort_key(left.row(), left.column()) < model.sort_key(right.row(), right.column())


# --------------------------------------------------------------------------- widgets


class StorageBar(QWidget):
    def __init__(self):
        super().__init__()
        self.values = (0, 0, 0, 0)
        self.setMinimumWidth(260)
        self.setFixedHeight(34)

    def set_values(self, total: int, used: int, free: int, music: int) -> None:
        self.values = (total, used, free, music)
        self.setToolTip(f"Music {fmt_size(music)}\nOther {fmt_size(max(used - music, 0))}\n"
                        f"Free {fmt_size(free)}\nCapacity {fmt_size(total)}")
        self.update()

    def paintEvent(self, _):
        total, used, free, music = self.values
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bar = QRectF(0, 4, self.width(), 10)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#33363d"))
        p.drawRoundedRect(bar, 5, 5)
        if total:
            music_w = bar.width() * min(music, used) / total
            other_w = bar.width() * max(used - music, 0) / total
            p.save()
            clip = QRectF(bar)
            p.setClipRect(QRectF(0, 0, music_w + other_w, self.height()))
            p.setBrush(QColor("#8a8f99"))
            p.drawRoundedRect(clip, 5, 5)
            p.setClipRect(QRectF(0, 0, music_w, self.height()))
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(clip, 5, 5)
            p.restore()
        p.setPen(QPen(self.palette().color(QPalette.PlaceholderText)))
        f = p.font()
        f.setPointSizeF(f.pointSizeF() * 0.85)
        p.setFont(f)
        text = f"{fmt_size(music)} music  ·  {fmt_size(max(used - music, 0))} other  ·  {fmt_size(free)} free" if total else ""
        p.drawText(QRectF(0, 16, self.width(), 18), Qt.AlignLeft | Qt.AlignVCenter, text)


class Worker(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancel_requested = False

    def run(self):
        try:
            result = self.fn(lambda i, n, s: self.progress.emit(i, n, s), lambda: self.cancel_requested)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class EditDialog(QDialog):
    """Edits database info for one or more tracks. Fields left on 'Keep' are untouched."""

    TEXT_FIELDS = [("title", "Title"), ("artist", "Artist"), ("album", "Album"),
                   ("album_artist", "Album artist"), ("genre", "Genre"), ("composer", "Composer")]
    NUM_FIELDS = [("year", "Year", 9999), ("track_number", "Track", 999), ("track_count", "Track count", 999),
                  ("disc_number", "Disc", 99), ("disc_count", "Disc count", 99)]

    def __init__(self, tracks: list[Track], parent=None):
        super().__init__(parent)
        self.tracks = tracks
        self.setWindowTitle("Edit info" if len(tracks) == 1 else f"Edit info ({len(tracks)} tracks)")
        self.setMinimumWidth(420)
        form = QFormLayout()
        self.inputs = {}
        for key, label in self.TEXT_FIELDS:
            if key == "title" and len(tracks) > 1:
                continue
            values = {getattr(t, key) for t in tracks}
            w = QLineEdit(values.pop() if len(values) == 1 else "")
            if len(values) >= 1:
                w.setPlaceholderText("Multiple values")
            form.addRow(label, w)
            self.inputs[key] = w
        for key, label, maximum in self.NUM_FIELDS:
            if key in ("track_number",) and len(tracks) > 1:
                continue
            values = {getattr(t, key) for t in tracks}
            w = QSpinBox()
            w.setRange(-1, maximum)
            w.setSpecialValueText("Multiple values")
            w.setValue(values.pop() if len(values) == 1 else -1)
            form.addRow(label, w)
            self.inputs[key] = w
        note = QLabel("Changes are written to the iPod's database, not to the audio files' tags.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(buttons)
        self._initial = {k: self._value(w) for k, w in self.inputs.items()}

    @staticmethod
    def _value(w):
        return w.text().strip() if isinstance(w, QLineEdit) else w.value()

    def apply(self) -> bool:
        changed = False
        for key, w in self.inputs.items():
            value = self._value(w)
            if value == self._initial[key] or value == -1:
                continue
            for t in self.tracks:
                setattr(t, key, value)
            changed = True
        return changed


class HealthDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.choice = None
        self.setWindowTitle("Library check")
        self.setMinimumSize(560, 380)
        lay = QVBoxLayout(self)
        if not report.orphan_files and not report.missing_tracks:
            lay.addWidget(QLabel("Everything checks out: every track has its file and there are no stray files."))
        if report.orphan_files:
            lay.addWidget(QLabel(f"<b>{len(report.orphan_files)} file(s)</b> on the iPod aren't in its library "
                                 "(they use space but don't show up):"))
            lst = QListWidget()
            lst.addItems(str(p) for p in report.orphan_files)
            lay.addWidget(lst)
            row = QHBoxLayout()
            imp = QPushButton("Add them to the library")
            imp.clicked.connect(lambda: self._pick("import"))
            dele = QPushButton("Delete these files")
            dele.clicked.connect(lambda: self._pick("delete"))
            row.addWidget(imp)
            row.addWidget(dele)
            row.addStretch()
            lay.addLayout(row)
        if report.missing_tracks:
            lay.addWidget(QLabel(f"<b>{len(report.missing_tracks)} track(s)</b> in the library have no file "
                                 "(they show up but won't play):"))
            lst = QListWidget()
            lst.addItems(f"{t.artist} – {t.title}" for t in report.missing_tracks)
            lay.addWidget(lst)
            row = QHBoxLayout()
            drop = QPushButton("Remove these entries")
            drop.clicked.connect(lambda: self._pick("drop"))
            row.addWidget(drop)
            row.addStretch()
            lay.addLayout(row)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay.addWidget(close)

    def _pick(self, choice: str):
        self.choice = choice
        self.accept()


# --------------------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    LIBRARY = None

    def __init__(self):
        super().__init__()
        self.ipod: IPod | None = None
        self.worker: Worker | None = None
        self.current_playlist_id = self.LIBRARY
        self.player = None

        self.setWindowTitle("PodPilot")
        self.setWindowIcon(app_icon())
        self.resize(1200, 720)
        self.setAcceptDrops(True)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._update_actions()

        self.device_timer = QTimer(self, interval=3000, timeout=self.scan_devices)
        self.device_timer.start()
        QTimer.singleShot(0, self.scan_devices)

    # ---- construction

    def _action(self, text, slot, icon=None, shortcut=None, tip=None) -> QAction:
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_toolbar(self):
        SP = QStyle.StandardPixmap
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        self.device_combo.currentIndexChanged.connect(self._device_chosen)
        tb.addWidget(self.device_combo)
        self.act_refresh = self._action("Reload", self.reload_device, SP.SP_BrowserReload, "F5",
                                        "Re-read the iPod's database")
        tb.addAction(self.act_refresh)
        tb.addSeparator()

        self.act_add = self._action("Add Music", self.add_files, SP.SP_FileDialogNewFolder, "Ctrl+O",
                                    "Copy songs onto the iPod (or drag files onto the window)")
        self.act_add_folder = self._action("Add Folder…", self.add_folder, None, "Ctrl+Shift+O")
        self.act_remove = self._action("Delete", self.remove_selected, SP.SP_TrashIcon, "Del",
                                       "Delete the selected songs from the iPod")
        self.act_export = self._action("Copy to PC", self.export_selected, SP.SP_DialogSaveButton, "Ctrl+E",
                                       "Copy the selected songs from the iPod to a folder")
        self.act_edit = self._action("Edit Info", self.edit_selected, SP.SP_FileDialogDetailedView, "Ctrl+I")
        self.act_playlist = self._action("Add to Playlist", self.show_playlist_menu, SP.SP_FileDialogListView)
        self.act_health = self._action("Check Library", self.check_health, SP.SP_DialogApplyButton, None,
                                       "Find stray files and broken entries")
        for a in (self.act_add, self.act_remove, self.act_export, self.act_edit, self.act_playlist, self.act_health):
            tb.addAction(a)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        self.storage = StorageBar()
        tb.addWidget(self.storage)
        tb.addSeparator()
        self.act_eject = self._action("Eject", self.eject, SP.SP_ArrowUp, "Ctrl+J", "Safely eject the iPod")
        tb.addAction(self.act_eject)

        self._action("Search", lambda: self.search.setFocus(), None, "Ctrl+F")

    def _build_body(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        empty = QWidget()
        lay = QVBoxLayout(empty)
        lay.addStretch()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(96, 96))
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)
        self.empty_label = QLabel("Connect your iPod")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        lay.addWidget(self.empty_label)
        hint = QLabel("PodPilot finds it automatically once it shows up as a drive.\n"
                      "Supports iPod classic (up to 5.5G), mini and nano (1st–2nd gen).")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(hint)
        lay.addStretch()
        self.stack.addWidget(empty)

        splitter = QSplitter()
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(160)
        self.sidebar.currentItemChanged.connect(self._sidebar_changed)
        self.sidebar.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sidebar.customContextMenuRequested.connect(self._sidebar_menu)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.addWidget(self.sidebar)
        new_pl = QPushButton("+  New Playlist")
        new_pl.clicked.connect(self.new_playlist)
        side_lay.addWidget(new_pl)
        splitter.addWidget(side)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, album, genre…")
        self.search.setClearButtonEnabled(True)
        rl.addWidget(self.search)

        self.model = TrackModel()
        self.proxy = TrackFilter()
        self.proxy.setSourceModel(self.model)
        self.search.textChanged.connect(self._search_changed)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        for i, w in enumerate((250, 170, 190, 90, 36, 52, 48, 46, 92, 70, 84)):
            hh.resizeSection(i, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(self.preview)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        rl.addWidget(self.table)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 1020])

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(8, 8, 8, 0)
        wl.addWidget(splitter)
        self.stack.addWidget(wrap)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.summary = QLabel()
        sb.addWidget(self.summary, 1)
        self.now_playing = QLabel()
        sb.addPermanentWidget(self.now_playing)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setFlat(True)
        self.stop_btn.hide()
        self.stop_btn.clicked.connect(self.stop_preview)
        sb.addPermanentWidget(self.stop_btn)

    # ---- device handling

    def scan_devices(self):
        if self.worker is not None:
            return
        roots = find_ipods()
        known = [self.device_combo.itemData(i) for i in range(self.device_combo.count())]
        if [str(r) for r in roots] == known:
            return
        current = str(self.ipod.root) if self.ipod else None
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for r in roots:
            self.device_combo.addItem(f"{r}  ({_label_for(r)})", str(r))
        self.device_combo.blockSignals(False)
        if current in [str(r) for r in roots]:
            self.device_combo.setCurrentIndex([str(r) for r in roots].index(current))
        elif roots:
            self.device_combo.setCurrentIndex(0)
            self._device_chosen(0)
        else:
            self.close_device()

    def _device_chosen(self, index: int):
        root = self.device_combo.itemData(index)
        if root and (self.ipod is None or str(self.ipod.root) != root):
            self.open_device(Path(root))

    def open_device(self, root: Path):
        self.stop_preview()
        try:
            self.ipod = IPod(root)
        except (DatabaseError, OSError) as exc:
            self.ipod = None
            self.empty_label.setText(f"Can't read the iPod at {root}")
            QMessageBox.warning(self, "PodPilot", f"Couldn't open the iPod at {root}:\n\n{exc}")
            self.stack.setCurrentIndex(0)
            return
        name = self.ipod.name
        self.device_combo.setItemText(self.device_combo.currentIndex(), f"{name}  ({root})")
        self.stack.setCurrentIndex(1)
        self.current_playlist_id = self.LIBRARY
        self.refresh_view()
        if self.ipod.play_counts_merged:
            self.statusBar().showMessage("Merged play counts recorded on the iPod since the last sync.", 6000)

    def close_device(self):
        self.stop_preview()
        self.ipod = None
        self.model.set_tracks([])
        self.sidebar.clear()
        self.empty_label.setText("Connect your iPod")
        self.stack.setCurrentIndex(0)
        self._update_actions()

    def reload_device(self):
        if self.ipod:
            playlist = self.current_playlist_id
            self.open_device(self.ipod.root)
            self.current_playlist_id = playlist
            self.refresh_view()
        else:
            self.scan_devices()

    def eject(self):
        if not self.ipod:
            return
        self.stop_preview()
        root, name = self.ipod.root, self.ipod.name
        self.ipod.eject()
        self.close_device()
        self.device_combo.clear()

        def check():
            if root.exists():
                QMessageBox.warning(self, "Eject", f"Windows couldn't eject {name}. Close anything using the drive "
                                                   "and try again.")
                self.scan_devices()
            else:
                self.statusBar().showMessage(f"{name} ejected. It's safe to unplug it.", 8000)
        QTimer.singleShot(2500, check)

    # ---- view

    def refresh_view(self):
        if not self.ipod:
            return
        db = self.ipod.db
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        lib = QListWidgetItem(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume), "Music")
        lib.setData(Qt.UserRole, self.LIBRARY)
        self.sidebar.addItem(lib)
        header = QListWidgetItem("PLAYLISTS")
        header.setFlags(Qt.NoItemFlags)
        header.setData(Qt.UserRole, "header")
        self.sidebar.addItem(header)
        selected = lib
        for pl in db.playlists:
            if not pl.editable:
                continue
            item = QListWidgetItem(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView),
                                   f"{pl.name}")
            item.setData(Qt.UserRole, pl.id)
            item.setToolTip(f"{len(pl.track_ids)} songs")
            self.sidebar.addItem(item)
            if pl.id == self.current_playlist_id:
                selected = item
        self.sidebar.setCurrentItem(selected)
        self.sidebar.blockSignals(False)
        self.current_playlist_id = selected.data(Qt.UserRole)
        self._load_tracks()
        total, used, free, music = self.ipod.storage()
        self.storage.set_values(total, used, free, music)

    def _current_playlist(self):
        if self.ipod is None or self.current_playlist_id is self.LIBRARY:
            return None
        return next((p for p in self.ipod.db.playlists if p.id == self.current_playlist_id), None)

    def _load_tracks(self):
        db = self.ipod.db
        pl = self._current_playlist()
        if pl is None:
            tracks = db.tracks
        else:
            by_id = {t.id: t for t in db.tracks}
            tracks = [by_id[i] for i in pl.track_ids if i in by_id]
        self.model.set_tracks(tracks)
        self._update_summary()
        self._update_actions()

    def _update_summary(self):
        tracks = self.model.tracks
        dur = sum(t.length_ms for t in tracks)
        size = sum(t.size for t in tracks)
        shown = self.proxy.rowCount()
        prefix = f"{shown} of " if shown != len(tracks) else ""
        self.summary.setText(f"{prefix}{len(tracks)} songs  ·  {fmt_duration(dur)}  ·  {fmt_size(size)}")

    def _sidebar_changed(self, item, _prev):
        if item is None or item.data(Qt.UserRole) == "header":
            return
        self.current_playlist_id = item.data(Qt.UserRole)
        self.table.sortByColumn(-1, Qt.AscendingOrder)
        self._load_tracks()

    def _search_changed(self, text):
        self.proxy.set_needle(text)
        self._update_summary()

    def selected_tracks(self) -> list[Track]:
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.tracks[r] for r in rows]

    def _update_actions(self):
        has_dev = self.ipod is not None and self.worker is None
        has_sel = has_dev and bool(self.table.selectionModel().selectedRows())
        in_playlist = self._current_playlist() is not None
        for a in (self.act_add, self.act_add_folder, self.act_health, self.act_eject, self.act_refresh):
            a.setEnabled(has_dev)
        for a in (self.act_remove, self.act_export, self.act_edit, self.act_playlist):
            a.setEnabled(has_sel)
        self.act_remove.setText("Remove from Playlist" if in_playlist else "Delete")
        self.act_remove.setToolTip("Remove the selected songs from this playlist (they stay on the iPod)"
                                   if in_playlist else "Delete the selected songs from the iPod")

    # ---- context menus

    def _table_menu(self, pos):
        if not self.selected_tracks():
            return
        m = QMenu(self)
        m.addAction("Play Preview", lambda: self.preview(self.table.currentIndex()))
        m.addSeparator()
        m.addAction(self.act_edit)
        m.addMenu(self._playlist_menu())
        m.addAction(self.act_export)
        m.addSeparator()
        if self._current_playlist() is not None:
            m.addAction("Remove from Playlist", self.remove_selected)
            m.addAction("Delete from iPod…", self.delete_selected)
        else:
            m.addAction(self.act_remove)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _sidebar_menu(self, pos):
        item = self.sidebar.itemAt(pos)
        m = QMenu(self)
        m.addAction("New Playlist…", self.new_playlist)
        if item is not None and item.data(Qt.UserRole) not in (None, "header"):
            m.addAction("Rename…", lambda: self.rename_playlist(item.data(Qt.UserRole)))
            m.addAction("Delete Playlist", lambda: self.delete_playlist(item.data(Qt.UserRole)))
        m.exec(self.sidebar.viewport().mapToGlobal(pos))

    def _playlist_menu(self) -> QMenu:
        menu = QMenu("Add to Playlist", self)
        menu.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView))
        menu.addAction("New Playlist…", lambda: self.new_playlist(with_selection=True))
        menu.addSeparator()
        for pl in self.ipod.db.playlists if self.ipod else []:
            if pl.editable:
                menu.addAction(pl.name, lambda pid=pl.id: self.add_selection_to_playlist(pid))
        return menu

    def show_playlist_menu(self):
        self._playlist_menu().exec(self.cursor().pos())

    # ---- background jobs

    def run_job(self, title: str, fn, on_done):
        self.stop_preview()
        dlg = QProgressDialog(title, "Cancel", 0, 0, self)
        dlg.setWindowTitle("PodPilot")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setMinimumWidth(420)
        self.worker = Worker(fn)
        self._update_actions()

        def progress(i, n, text):
            dlg.setMaximum(max(n, 1))
            dlg.setValue(min(i, n))
            dlg.setLabelText(f"{title}\n{text}")

        def finish():
            dlg.reset()
            self.worker = None
            self._update_actions()

        def ok(result):
            finish()
            on_done(result)
            self.refresh_view()

        def fail(message):
            finish()
            QMessageBox.critical(self, "PodPilot", f"Something went wrong:\n\n{message}")
            self.reload_device()

        dlg.canceled.connect(lambda: setattr(self.worker, "cancel_requested", True) if self.worker else None)
        self.worker.progress.connect(progress)
        self.worker.done.connect(ok)
        self.worker.failed.connect(fail)
        self.worker.start()

    # ---- adding

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(media.AUDIO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add music to the iPod", "", f"Audio files ({exts})")
        if files:
            self.import_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of music to the iPod")
        if folder:
            self.import_paths([folder])

    def import_paths(self, paths):
        if not self.ipod or self.worker:
            return
        files = media.collect_audio_files(paths)
        if not files:
            QMessageBox.information(self, "PodPilot", "No audio files found there.")
            return
        if any(media.needs_conversion(f, {"sample_rate": 0, "codec": ""}) for f in files) and not media.ffmpeg_path():
            QMessageBox.warning(self, "PodPilot", "Some files need converting (FLAC, OGG, Opus…) but ffmpeg "
                                                  "wasn't found on PATH. Those files will be skipped.")
        target = self._current_playlist()
        ipod = self.ipod

        def job(progress, cancelled):
            result = ipod.add_files(files, progress, cancelled)
            if target is not None and result.added:
                ipod.db.add_to_playlist(target, result.added)
                ipod.save()
            return result

        def done(result):
            lines = [f"Added {len(result.added)} song(s)."]
            if result.skipped:
                lines.append(f"Skipped {len(result.skipped)} already on the iPod.")
            if result.errors:
                lines.append(f"{len(result.errors)} file(s) failed.")
            box = QMessageBox(QMessageBox.Warning if result.errors else QMessageBox.Information,
                              "PodPilot", "\n".join(lines), parent=self)
            if result.errors or result.skipped:
                details = result.errors + [f"Already on iPod: {s}" for s in result.skipped]
                box.setDetailedText("\n".join(details))
            box.exec()

        self.run_job(f"Copying {len(files)} file(s) to {ipod.name}", job, done)

    def dragEnterEvent(self, event):
        if self.ipod and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.import_paths(paths)

    # ---- removing

    def remove_selected(self):
        pl = self._current_playlist()
        if pl is not None:
            tracks = self.selected_tracks()
            self.ipod.db.remove_from_playlist(pl, [t.id for t in tracks])
            self.ipod.save()
            self.refresh_view()
            self.statusBar().showMessage(f"Removed {len(tracks)} song(s) from “{pl.name}”.", 5000)
        else:
            self.delete_selected()

    def delete_selected(self):
        tracks = self.selected_tracks()
        if not tracks:
            return
        what = f"“{tracks[0].title}”" if len(tracks) == 1 else f"{len(tracks)} songs"
        if QMessageBox.question(self, "Delete from iPod",
                                f"Delete {what} from the iPod?\n\nThe files are removed from the device "
                                "and from every playlist.") != QMessageBox.Yes:
            return
        ipod = self.ipod
        self.run_job("Deleting songs", lambda progress, _c: ipod.remove_tracks(tracks, progress),
                     lambda errors: errors and QMessageBox.warning(self, "PodPilot", "\n".join(errors)))

    # ---- exporting / editing

    def export_selected(self):
        tracks = self.selected_tracks()
        dest = QFileDialog.getExistingDirectory(self, "Copy songs to…")
        if not tracks or not dest:
            return
        ipod = self.ipod

        def done(errors):
            if errors:
                QMessageBox.warning(self, "PodPilot", "Some songs couldn't be copied:\n\n" + "\n".join(errors[:20]))
            else:
                self.statusBar().showMessage(f"Copied {len(tracks)} song(s) to {dest}", 8000)

        self.run_job(f"Copying {len(tracks)} song(s) to your PC",
                     lambda progress, cancelled: ipod.export_tracks(tracks, Path(dest), progress, cancelled), done)

    def edit_selected(self):
        tracks = self.selected_tracks()
        if not tracks:
            return
        dlg = EditDialog(tracks, self)
        if dlg.exec() == QDialog.Accepted and dlg.apply():
            self.ipod.db.mark_dirty()
            self.ipod.save()
            self.refresh_view()

    # ---- playlists

    def new_playlist(self, with_selection=False):
        if not self.ipod:
            return
        name, ok = QInputDialog.getText(self, "New Playlist", "Name:")
        if not ok or not name.strip():
            return
        pl = self.ipod.db.create_playlist(name.strip())
        if with_selection:
            self.ipod.db.add_to_playlist(pl, self.selected_tracks())
        self.ipod.save()
        if not with_selection:
            self.current_playlist_id = pl.id
        self.refresh_view()

    def rename_playlist(self, pid):
        pl = next((p for p in self.ipod.db.playlists if p.id == pid), None)
        if pl is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Playlist", "Name:", text=pl.name)
        if ok and name.strip():
            self.ipod.db.rename_playlist(pl, name.strip())
            self.ipod.save()
            self.refresh_view()

    def delete_playlist(self, pid):
        pl = next((p for p in self.ipod.db.playlists if p.id == pid), None)
        if pl is None:
            return
        if QMessageBox.question(self, "Delete Playlist",
                                f"Delete the playlist “{pl.name}”?\n\nThe songs stay on the iPod.") != QMessageBox.Yes:
            return
        self.ipod.db.delete_playlist(pl)
        self.ipod.save()
        if self.current_playlist_id == pid:
            self.current_playlist_id = self.LIBRARY
        self.refresh_view()

    def add_selection_to_playlist(self, pid):
        pl = next((p for p in self.ipod.db.playlists if p.id == pid), None)
        tracks = self.selected_tracks()
        if pl is None or not tracks:
            return
        existing = set(pl.track_ids)
        new = [t for t in tracks if t.id not in existing]
        self.ipod.db.add_to_playlist(pl, new)
        self.ipod.save()
        self.refresh_view()
        self.statusBar().showMessage(f"Added {len(new)} song(s) to “{pl.name}”.", 5000)

    # ---- health check

    def check_health(self):
        ipod = self.ipod

        def done(report):
            dlg = HealthDialog(report, self)
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.choice == "import":
                self.run_job("Adding files to the library", lambda p, c: ipod.import_orphans(report.orphan_files),
                             lambda r: QMessageBox.information(self, "PodPilot", f"Added {len(r.added)} song(s)."))
            elif dlg.choice == "delete":
                if QMessageBox.question(self, "Delete files", f"Permanently delete {len(report.orphan_files)} "
                                        "file(s) from the iPod?") == QMessageBox.Yes:
                    self.run_job("Deleting files", lambda p, c: ipod.delete_files(report.orphan_files), lambda r: None)
            elif dlg.choice == "drop":
                self.run_job("Cleaning up", lambda p, c: ipod.drop_missing(report.missing_tracks), lambda r: None)

        self.run_job("Checking the library", lambda p, c: ipod.health(), done)

    # ---- preview playback

    def preview(self, index):
        if not index.isValid() or not self.ipod:
            return
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        track = self.model.tracks[self.proxy.mapToSource(index).row()]
        path = self.ipod.path_of(track)
        if not path.is_file():
            QMessageBox.warning(self, "PodPilot", "That song's file is missing from the iPod.")
            return
        if self.player is None:
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.mediaStatusChanged.connect(
                lambda s: self.stop_preview() if s == QMediaPlayer.MediaStatus.EndOfMedia else None)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self.model.playing_id = track.id
        self.model.layoutChanged.emit()
        self.now_playing.setText(f"Playing: {track.artist} – {track.title}" if track.artist else f"Playing: {track.title}")
        self.stop_btn.show()

    def stop_preview(self):
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.model.playing_id = None
        self.model.layoutChanged.emit()
        self.now_playing.clear()
        self.stop_btn.hide()


def _label_for(root: Path) -> str:
    from .device import volume_label
    return volume_label(root)


# --------------------------------------------------------------------------- styling / entry point


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    base, window, text = QColor("#17181c"), QColor("#1f2026"), QColor("#e6e7ea")
    pal.setColor(QPalette.Window, window)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, QColor("#1c1d22"))
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, QColor("#2a2c33"))
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor("#2a2c33"))
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.PlaceholderText, QColor("#8a8f99"))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5d616b"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5d616b"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5d616b"))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QToolBar {{ border: none; padding: 6px; spacing: 4px; background: #1f2026; }}
        QToolButton {{ padding: 5px 9px; border-radius: 6px; }}
        QToolButton:hover {{ background: #2d3038; }}
        QToolButton:disabled {{ color: #5d616b; }}
        QComboBox, QLineEdit, QSpinBox {{ padding: 5px 8px; border: 1px solid #33363d; border-radius: 6px;
                                         background: #17181c; }}
        QLineEdit:focus {{ border-color: {ACCENT}; }}
        QTableView {{ border: 1px solid #2a2c33; border-radius: 6px; selection-background-color: {ACCENT}; }}
        QHeaderView::section {{ background: #1f2026; border: none; border-bottom: 1px solid #2a2c33;
                                padding: 5px 6px; color: #a9adb6; }}
        QListWidget#sidebar {{ border: none; background: transparent; font-size: 13px; }}
        QListWidget#sidebar::item {{ padding: 6px 8px; border-radius: 6px; }}
        QListWidget#sidebar::item:selected {{ background: #2d3038; color: #ffffff; }}
        QPushButton {{ padding: 6px 12px; border-radius: 6px; background: #2a2c33; border: 1px solid #33363d; }}
        QPushButton:hover {{ background: #33363d; }}
        QPushButton:flat {{ border: none; background: transparent; color: {ACCENT}; }}
        QStatusBar {{ color: #a9adb6; }}
        QSplitter::handle {{ background: transparent; }}
    """)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("PodPilot")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    apply_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()
