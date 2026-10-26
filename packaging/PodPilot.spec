# PyInstaller spec: one-folder windowed build in dist/PodPilot/.
# Build with:  python -m PyInstaller --noconfirm packaging/PodPilot.spec

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "podpilot" / "__init__.py").read_text()).group(1)
NUMS = tuple(int(x) for x in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMS, prodvers=NUMS, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                      subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "mystic"),
            StringStruct("FileDescription", "PodPilot"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "PodPilot"),
            StringStruct("LegalCopyright", "MIT License"),
            StringStruct("OriginalFilename", "PodPilot.exe"),
            StringStruct("ProductName", "PodPilot"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)
version_file = ROOT / "build" / "version_info.txt"
version_file.parent.mkdir(exist_ok=True)
version_file.write_text(str(version_info), encoding="utf-8")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    excludes=["tkinter", "unittest", "pydoc", "pytest", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
)
# Qt pieces a widgets app never loads; dropping them saves ~45 MB. `PodPilot.exe --selftest`
# checks the audio backend still works after trimming.
TRIM = ("opengl32sw.dll", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "qpdf.dll", "Qt6VirtualKeyboard", "Qt6OpenGL.dll",
        "Qt6ShaderTools", "Qt6QuickControls", "Qt6Svg")


def keep(entry):
    name = entry[0].replace("\\", "/")
    return not any(t in name for t in TRIM) and "/translations/" not in name and "/qml/" not in name


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PodPilot",
    console=False,
    icon=str(ROOT / "assets" / "podpilot.ico"),
    version=str(version_file),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PodPilot", upx=False)
