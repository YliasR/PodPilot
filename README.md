# PodPilot

A small desktop music manager for click-wheel iPods. Plug the iPod in, see what's on it, drag songs onto the window to add them, delete what you don't want — no iTunes needed.

## Features

- **Browse** everything on the iPod: title, artist, album, genre, length, plays, format, size, date added. Sort any column, search across all of them.
- **Add music** by drag & drop, file picker or whole folders. MP3, AAC/M4A, ALAC, WAV and AIFF are copied as-is; FLAC, OGG, Opus, WMA etc. are converted to MP3 (V0) on the fly with ffmpeg. Duplicates are skipped.
- **Album art** from the file itself, or from `cover.jpg` / `folder.jpg` (and a few other names) sitting next to it. Anything still missing is looked up online and written into the iPod's artwork database, which is what the click wheel actually shows. The same pass fills in songs already on the iPod. Your original files are left alone. Turn the automatic pass off under ☰ → *Fetch Album Art Automatically*, or run *Fetch Album Art* when you want it.
- **Sync from a folder**, including all subfolders. A preview shows what will be added, what changed since the last sync (updated in place, so songs keep their playlists) and what's on the iPod but not in the folder. Removing those is optional and off by default. Re-syncs are fast because PodPilot remembers which file became which song (`iPod_Control/PodPilot/sync.json`), and songs already on the iPod are matched by title, artist and length instead of being copied twice.
- **Delete** songs (file and database entry, removed from every playlist).
- **Playlists**: create, rename, delete, add/remove songs.
- **Edit info** (title, artist, album, genre, year, track/disc numbers) for one or many songs.
- **Copy to PC**: pull songs off the iPod into `Artist/Album/NN Title.ext` folders.
- **Library check**: finds files that take up space but aren't in the library (re-add or delete them) and library entries whose file is gone.
- **Preview** a song by double-clicking it.
- **Play counts** recorded on the iPod are merged into the database, like iTunes does.
- **Eject** safely from the app.

## Supported devices

| iPod | Database | PodPilot |
|---|---|---|
| classic 1G–5.5G (incl. video), mini, nano 1G–2G | unsigned | Full support |
| classic 6G/6.5G/7G, nano 3G–4G | signed with **hash58** | Full support (experimental) |
| nano 5G | signed with hash72 | Read-only: browse and copy to PC |
| nano 6G/7G, shuffle, touch | hashAB / other formats | Not supported |

**How hash58 support stays safe:** the signature is keyed with the iPod's FireWire GUID, which PodPilot reads from `SysInfoExtended`/`SysInfo` or from the USB serial number. When an iPod is opened, PodPilot first recomputes the signature iTunes left on the existing database. Writing is only enabled if it matches exactly, which proves both the GUID and the algorithm are right for that device. If it doesn't match, the iPod opens read-only. hash58 support is ported from [libgpod](https://sourceforge.net/projects/gtkpod/) and hasn't been tested on every model yet, so reports are welcome.

## Download

Grab `PodPilot-Setup-x.y.z.exe` from the [latest release](https://github.com/YliasR/PodPilot/releases/latest) and run it. It installs for your user only (no admin prompt) and adds a Start menu entry.

PodPilot checks for updates once a day (turn this off under ☰ → *Check for Updates Automatically*). Updates are downloaded from GitHub, verified against the release's `SHA256SUMS.txt`, and installed in the background, and then PodPilot reopens.

The installer isn't code-signed, so Windows SmartScreen may warn the first time. Click *More info → Run anyway*.

For converting FLAC/OGG/Opus etc., install [ffmpeg](https://ffmpeg.org/) and make sure it's on `PATH` (`winget install Gyan.FFmpeg`).

## Run from source

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python -m podpilot
```

## Safety

- Before every write, the current `iTunesDB` is backed up to `%APPDATA%\PodPilot\backups\<iPod name>\` (last 30 kept). To restore, copy a backup over `iPod_Control\iTunes\iTunesDB`.
- The new database is written to a temp file, read back to verify it, and then swapped in.
- Unknown parts of the database are preserved byte-for-byte. Loading and saving an unchanged library reproduces the original file exactly, including the browse indices.
- If you still sync the iPod with iTunes/Apple Music, it may overwrite changes made here. Pick one manager.
- A hash58 iPod that has never been synced with iTunes has no signature to check against yet, so PodPilot treats it as unsigned. Sync it with iTunes once first.

## How it works

`podpilot/itunesdb.py` parses the iTunesDB chunk tree (`mhbd → mhsd → mhlt/mhlp → mhit/mhyp → mhod/mhip`), edits tracks and playlists in place, and rebuilds the sorted library indices (mhod 52/53) the iPod uses for its Artists/Albums/Songs menus. Songs are copied into `iPod_Control/Music/Fxx/` with random 4-letter names, like iTunes does.

## Development

```bash
pip install pytest
python -m pytest
```

### Building the installer

```bash
pip install pyinstaller
winget install JRSoftware.InnoSetup
```

Then run `./build.ps1`. It writes `dist/PodPilot/PodPilot.exe`, `dist/PodPilot-Setup-<version>.exe` and `dist/SHA256SUMS.txt`. `PodPilot.exe --selftest` checks that the packaged build starts and can decode audio.

### Releasing

Bump `__version__` in `podpilot/__init__.py` (and `pyproject.toml`), commit, then push a matching tag:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The release workflow tests, builds and self-tests the exe, then publishes the GitHub release that the in-app updater picks up.

If the UI hangs, run `python -m podpilot --debug`: every stall longer than 0.4 s is logged with a stack trace to `%APPDATA%\PodPilot\debug.log`.

## License

MIT. `podpilot/hash58.py` is a port of libgpod code and keeps its BSD license notice.
