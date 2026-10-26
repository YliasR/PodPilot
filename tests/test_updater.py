import hashlib

import pytest

from podpilot import updater


def release_json(tag="v0.3.0", installer="PodPilot-Setup-0.3.0.exe", **extra):
    data = {
        "tag_name": tag, "body": "notes", "html_url": "https://github.com/YliasR/PodPilot/releases/tag/" + tag,
        "assets": [
            {"name": installer, "size": 123,
             "browser_download_url": f"https://github.com/YliasR/PodPilot/releases/download/{tag}/{installer}"},
            {"name": "SHA256SUMS.txt", "size": 90,
             "browser_download_url": f"https://github.com/YliasR/PodPilot/releases/download/{tag}/SHA256SUMS.txt"},
        ],
    }
    data.update(extra)
    return data


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2.0") == updater.parse_version("1.2")
    assert updater.is_newer("0.10.0", "0.9.9")
    assert not updater.is_newer("0.2.0", "0.2.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("1.2-beta")


def test_release_from_json():
    rel = updater.release_from_json(release_json())
    assert rel.version == "0.3.0" and rel.installer_name == "PodPilot-Setup-0.3.0.exe"
    assert updater.release_from_json(release_json(prerelease=True)) is None
    assert updater.release_from_json(release_json(draft=True)) is None
    # installer name must match the tag's version
    assert updater.release_from_json(release_json(installer="PodPilot-Setup-0.2.0.exe")) is None
    assert updater.release_from_json(release_json(installer="evil.exe")) is None


def test_expected_hash():
    h = hashlib.sha256(b"x").hexdigest()
    text = f"{'0' * 64}  other.exe\n{h} *PodPilot-Setup-0.3.0.exe\n"
    assert updater.expected_hash(text, "PodPilot-Setup-0.3.0.exe") == h
    with pytest.raises(updater.UpdateError):
        updater.expected_hash(text, "PodPilot-Setup-9.9.9.exe")


@pytest.mark.parametrize("url", [
    "http://github.com/x", "https://evil.example.com/PodPilot-Setup-1.0.0.exe",
    "https://github.com.evil.com/x", "file:///C:/Windows/notepad.exe",
])
def test_rejects_untrusted_urls(url):
    with pytest.raises(updater.UpdateError):
        updater._check_url(url)


class FakeResponse:
    def __init__(self, body: bytes, url: str):
        self.body, self.url, self.pos = body, url, 0
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n=-1):
        chunk = self.body[self.pos:] if n < 0 else self.body[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_github(monkeypatch, installer: bytes, listed_hash: str):
    def urlopen(req, timeout):
        url = req.full_url
        if url.endswith("SHA256SUMS.txt"):
            return FakeResponse(f"{listed_hash}  PodPilot-Setup-0.3.0.exe\n".encode(), url)
        return FakeResponse(installer, "https://objects.githubusercontent.com/blob")
    monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)


def test_download_verifies_checksum(monkeypatch):
    payload = b"MZ" + b"\x00" * 500_000
    rel = updater.release_from_json(release_json())
    fake_github(monkeypatch, payload, hashlib.sha256(payload).hexdigest())
    path = updater.download_installer(rel)
    assert path.read_bytes() == payload

    fake_github(monkeypatch, payload, "0" * 64)
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download_installer(rel)


def test_download_rejects_offsite_redirect(monkeypatch):
    payload = b"MZ"
    rel = updater.release_from_json(release_json())

    def urlopen(req, timeout):
        if req.full_url.endswith("SHA256SUMS.txt"):
            return FakeResponse(f"{hashlib.sha256(payload).hexdigest()}  PodPilot-Setup-0.3.0.exe".encode(),
                                req.full_url)
        return FakeResponse(payload, "https://evil.example.com/x.exe")
    monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
    with pytest.raises(updater.UpdateError):
        updater.download_installer(rel)
