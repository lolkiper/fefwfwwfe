from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "DramaStill"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        bundled = Path(frozen) / "dramastill" / "resources"
        if bundled.exists():
            return bundled
        direct = Path(frozen) / "resources"
        if direct.exists():
            return direct
    return package_dir() / "resources"


def font_path() -> Path | None:
    candidate = resource_dir() / "fonts" / "NotoSans-Bold.ttf"
    if candidate.exists():
        return candidate
    fallbacks = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    ]
    for path in fallbacks:
        if path.exists():
            return path
    return None


def data_dir() -> Path:
    override = os.environ.get("DRAMASTILL_DATA")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        path = root / APP_NAME
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    path = data_dir() / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path
