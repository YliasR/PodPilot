# PodPilot

A small desktop music manager for click-wheel iPods. Plug the iPod in, see what's on it, drag songs onto the window to add them, delete what you don't want — no iTunes needed.

## Features

- **Browse** everything on the iPod: title, artist, album, genre, length, plays, format, size, date added. Sort any column, search across all of them.
- **Add music** by drag & drop, file picker or whole folders. MP3, AAC/M4A, ALAC, WAV and AIFF are copied as-is; FLAC, OGG, Opus, WMA etc. are converted to MP3 (V0) on the fly with ffmpeg. Duplicates are skipped.
- **Delete** songs (file and database entry, removed from every playlist).
- **Playlists**: create, rename, delete, add/remove songs.
- **Edit info** (title, artist, album, genre, year, track/disc numbers) for one or many songs.
- **Copy to PC**: pull songs off the iPod into `Artist/Album/NN Title.ext` folders.
- **Library check**: finds files that take up space but aren't in the library (re-add or delete them) and library entries whose file is gone.
- **Preview** a song by double-clicking it.
- **Play counts** recorded on the iPod are merged into the database, like iTunes does.
- **Eject** safely from the app.

## Supported devices

iPods that use an unsigned iTunesDB:

| Works | Doesn't work (signed database) |
|---|---|
| iPod classic 1G–5.5G (incl. video), mini, nano 1G–2G | nano 3G+, classic 6G/7G, shuffle, touch |

> The nano 3G/4G and classic 6G+ need a device-specific hash in the database. PodPilot detects this and refuses to write rather than corrupting anything.

## Install & run

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python -m podpilot
```

On Windows you can also double-click `PodPilot.bat`. For converting non-native formats, install [ffmpeg](https://ffmpeg.org/) and make sure it's on `PATH`.

## Safety

- Before every write, the current `iTunesDB` is backed up to `%APPDATA%\PodPilot\backups\<iPod name>\` (last 30 kept). To restore, copy a backup over `iPod_Control\iTunes\iTunesDB`.
- The new database is written to a temp file, read back to verify it, and then swapped in.
- Unknown parts of the database are preserved byte-for-byte. Loading and saving an unchanged library reproduces the original file exactly, including the browse indices.
- If you still sync the iPod with iTunes/Apple Music, it may overwrite changes made here. Pick one manager.

## How it works

`podpilot/itunesdb.py` parses the iTunesDB chunk tree (`mhbd → mhsd → mhlt/mhlp → mhit/mhyp → mhod/mhip`), edits tracks and playlists in place, and rebuilds the sorted library indices (mhod 52/53) the iPod uses for its Artists/Albums/Songs menus. Songs are copied into `iPod_Control/Music/Fxx/` with random 4-letter names, like iTunes does.

## Development

```bash
pip install pytest
python -m pytest
```

## License

MIT
