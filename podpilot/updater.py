"""Update checks against GitHub Releases, and installing them on Windows builds.

A release must carry `PodPilot-Setup-<version>.exe` and a `SHA256SUMS.txt` listing its
hash. Downloads are HTTPS-only from GitHub hosts and the installer is only started after
its SHA-256 matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import __version__

REPO = "YliasR/PodPilot"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CHECKSUMS_NAME = "SHA256SUMS.txt"
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
_INSTALLER_RE = re.compile(r"^PodPilot-Setup-(\d+(?:\.\d+){1,3})\.exe$")


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    notes: str
    page_url: str
    installer_name: str
    installer_url: str
    installer_size: int
    checksums_url: str


def parse_version(text: str) -> tuple[int, ...]:
    parts = text.strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise UpdateError(f"not a version number: {text!r}")
    nums = [int(p) for p in parts]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def can_self_update() -> bool:
    """Only the installed Windows build can replace itself; source checkouts use git."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"refusing to download from {url}")


def _open(url: str, timeout: float):
    _check_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"PodPilot/{__version__}",
        "Accept": "application/vnd.github+json" if "api.github.com" in url else "application/octet-stream",
    })
    resp = urllib.request.urlopen(req, timeout=timeout)
    _check_url(resp.geturl())  # redirects must stay on GitHub too
    return resp


def release_from_json(data: dict) -> Release | None:
    """Picks the installer and checksum assets out of a GitHub release payload."""
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name", ""))
    try:
        parse_version(tag)
    except UpdateError:
        return None
    version = tag.strip().lstrip("vV")
    installer = checksums = None
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        match = _INSTALLER_RE.match(name)
        if match and parse_version(match.group(1)) == parse_version(version):
            installer = asset
        elif name == CHECKSUMS_NAME:
            checksums = asset
    if installer is None or checksums is None:
        return None
    return Release(
        version=version,
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or RELEASES_PAGE),
        installer_name=installer["name"],
        installer_url=installer["browser_download_url"],
        installer_size=int(installer.get("size") or 0),
        checksums_url=checksums["browser_download_url"],
    )


def fetch_latest(timeout: float = 10) -> Release | None:
    try:
        with _open(API_LATEST, timeout) as resp:
            data = json.loads(resp.read(1024 * 1024))
    except UpdateError:
        raise
    except Exception as exc:  # network errors come in many shapes
        raise UpdateError(f"couldn't reach GitHub: {exc}") from exc
    return release_from_json(data)


def expected_hash(checksums_text: str, filename: str) -> str:
    for line in checksums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"{CHECKSUMS_NAME} has no entry for {filename}")


def download_installer(release: Release,
                       progress: Callable[[int, int, str], None] | None = None,
                       cancelled: Callable[[], bool] | None = None) -> Path | None:
    """Downloads and verifies the installer. Returns its path, or None if cancelled."""
    if not _INSTALLER_RE.match(release.installer_name):
        raise UpdateError(f"unexpected installer name {release.installer_name!r}")
    try:
        with _open(release.checksums_url, 30) as resp:
            want = expected_hash(resp.read(64 * 1024).decode("utf-8", "replace"), release.installer_name)

        workdir = Path(tempfile.mkdtemp(prefix="podpilot-update-"))
        dest = workdir / release.installer_name
        digest = hashlib.sha256()
        done = 0
        with _open(release.installer_url, 60) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or release.installer_size or 0)
            while True:
                if cancelled and cancelled():
                    fh.close()
                    dest.unlink(missing_ok=True)
                    return None
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_INSTALLER_BYTES:
                    raise UpdateError("download is larger than expected")
                digest.update(chunk)
                fh.write(chunk)
                if progress:
                    progress(done, total or done, f"{done / 1048576:.1f} MB")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"download failed: {exc}") from exc

    if digest.hexdigest() != want:
        dest.unlink(missing_ok=True)
        raise UpdateError("the downloaded installer doesn't match its published checksum")
    return dest


def launch_installer(path: Path) -> None:
    """Starts the installer silently; it closes PodPilot, updates it and relaunches it."""
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
                     creationflags=flags, close_fds=True)
