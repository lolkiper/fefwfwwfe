# -*- mode: python ; coding: utf-8 -*-
"""Окно Windows: папка dist/DramaStill с DramaStill.exe."""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = collect_submodules("dramastill")
hiddenimports += [
    "PySide6.QtMultimedia",
    "certifi",
    "aiohttp",
    "edge_tts",
    "PIL",
    "numpy",
    "httpx",
    "anyio",
    "h11",
    "cryptography",
]

_SKIP = ("QtWebEngine", "Qt6WebEngine", "Qt3D", "Qt6Quick3D", "QtPdf", "QtCharts", "QtDataVisualization")


def _keep(item) -> bool:
    source = item[0] if isinstance(item, tuple) else str(item)
    return not any(name in source for name in _SKIP)


for package in ("dramastill", "PySide6", "edge_tts", "aiohttp", "certifi", "cryptography"):
    collected_data, collected_binaries, collected_hidden = collect_all(package)
    datas += [item for item in collected_data if _keep(item)]
    binaries += [item for item in collected_binaries if _keep(item)]
    hiddenimports += [name for name in collected_hidden if _keep(name)]

a = Analysis(
    ["dramastill/__main__.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["packaging/rthook_ssl.py"],
    excludes=[
        "tkinter",
        "pytest",
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtDesigner",
    ],
    noarchive=False,
)


def _unused(entry) -> bool:
    name = str(entry[0]).replace("\\", "/")
    banned = (
        "WebEngine",
        "webengine",
        "Qt6Designer",
        "designer.exe",
        "linguist.exe",
        "assistant.exe",
        "lrelease.exe",
        "lupdate.exe",
        "balsam",
    )
    return any(part in name for part in banned)


a.binaries = [entry for entry in a.binaries if not _unused(entry)]
a.datas = [entry for entry in a.datas if not _unused(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DramaStill",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="dramastill/resources/icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DramaStill",
)
