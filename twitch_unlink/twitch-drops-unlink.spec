# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    ["twitch_unlink/launcher.py"],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tkinter",
        "twitch_unlink.gui.app",
        "twitch_unlink.client",
        "twitch_unlink.proxy_rotator",
        "twitch_unlink.accounts",
        "twitch_unlink.errors",
        "twitch_unlink.integrity",
        "twitch_unlink.integrity_browser",
        "playwright",
        "playwright.sync_api",
        "socks",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["uvicorn", "fastapi", "pydantic"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchDropsUnlink",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TwitchDropsUnlink",
)