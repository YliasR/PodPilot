"""Renders assets/podpilot.ico (multi-size, PNG-compressed entries) from the in-app icon."""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from podpilot.gui import icon_pixmap  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    icon_pixmap(size).save(buf, "PNG")
    return bytes(buf.data())


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 (QPixmap needs an app)
    images = [png_bytes(s) for s in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    offset = 6 + 16 * len(SIZES)
    for size, data in zip(SIZES, images):
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    dest = Path(__file__).resolve().parents[1] / "assets" / "podpilot.ico"
    dest.write_bytes(bytes(out))
    print(f"wrote {dest} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
