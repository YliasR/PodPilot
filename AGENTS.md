# PodPilot

Desktop music manager for click-wheel iPods (classic, mini, nano). Python 3.10+, PySide6, mutagen. No iTunes required. Run with `python -m podpilot`; build the Windows installer with `./build.ps1`.

## Layout

- `podpilot/itunesdb.py`: iTunesDB chunk-tree parser and writer (tracks, playlists, sorted indices). Unknown chunks and header bytes are preserved so an unmodified load/save round-trips exactly.
- `podpilot/device.py`: high-level operations on a mounted iPod: add, remove, sync, export, backup, save (including hash58 signing and artwork), eject.
- `podpilot/media.py`: tag reading and format conversion (ffmpeg) for files headed to the iPod.
- `podpilot/gui.py`: PySide6 UI.
- `podpilot/hash58.py`: hash58 iTunesDB signature, ported from libgpod. Keeps its BSD license header.
- `podpilot/updater.py`: update checks against GitHub Releases; HTTPS-only downloads verified against `SHA256SUMS.txt` before the installer runs.
- `podpilot/artwork.py`: finds a cover for a song (embedded, sidecar image, online lookup). Does not write to the iPod.
- `podpilot/ipodart.py`: reads and writes `ArtworkDB` and `.ithmb` thumbnail files under `iPod_Control/Artwork/`.
- `tests/`: pytest suite (itunesdb, hash58, updater, delete, sync, artwork).
- `packaging/`: PyInstaller spec, launcher, icon generator and Inno Setup script used by `build.ps1`.

## Devices

| iPod | Database | Support |
|---|---|---|
| classic 1G-5.5G, mini, nano 1G-2G | unsigned | Full |
| classic 6G/6.5G/7G, nano 3G-4G | hash58 | Full, experimental |
| nano 5G | hash72 | Read-only (browse, copy to PC) |
| nano 6G/7G, shuffle, touch | hashAB / other | Unsupported |

## Album art

- Click-wheel iPods do not display art embedded in the audio file. PodPilot also writes `iPod_Control/Artwork/ArtworkDB` and `F{formatId}_1.ithmb` (RGB565).
- Embedded JPEG is still written into the MP3/M4A/M4B copies on the device so exports keep it.
- Sources, in order: embedded tags; sidecar images next to the source file (cover, folder, front, album); iTunes Search, then MusicBrainz / Cover Art Archive. No API key.
- The user's original files are never modified.
- A failed lookup must not fail the import.
- An existing ArtworkDB is parsed and appended to. If it cannot be parsed it is left untouched.
- Fetching also covers songs already on the iPod (sync), not only newly added ones.

## Safety

- Before every write, `iTunesDB` is backed up to `%APPDATA%\PodPilot\backups`.
- Writes go to a temp file, then replace the target.
- Unknown database bytes are preserved.
- hash58 is verified against the existing signature before a signed database may be written.
- Do not clobber `ArtworkDB`.

## Checks

- `python -m pytest` before reporting work as done.
- `python -m podpilot` to run the app.
- `./build.ps1` builds the installer.
- There is no web UI; do not invent browser checks.

## Releases

Every update that ships must be tagged. This is required.

1. Bump `__version__` in `podpilot/__init__.py` and `version` in `pyproject.toml` together. They must match.
2. Commit that change.
3. Tag `vX.Y.Z` (the same version with a `v` prefix).
4. Push the tag. The GitHub release workflow tests, builds, self-tests the exe, and publishes the release the in-app updater downloads.

Do not bump, tag, or publish unless the user asked for a release.

## Git

Author is already `mystic <thefox@mystic.gay>` in global git config. Never set a repo-local `user.name` or `user.email`. Never put a real name or a gmail address in a commit. Never add a `Co-Authored-By` trailer or any other AI attribution.

## Working style

Change only what was asked. Match the surrounding code. Do not commit, push, or open a pull request unless the user asks.
