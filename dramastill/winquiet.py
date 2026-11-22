from __future__ import annotations

import subprocess
import sys

_APPLIED = False


def hide_console_windows() -> None:
    """Keep helper programs from flashing a console over the DramaStill window."""

    global _APPLIED
    if _APPLIED or sys.platform != "win32":
        _APPLIED = True
        return
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    original = subprocess.Popen

    class QuietPopen(original):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | creation
            if kwargs.get("startupinfo") is None:
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0
                kwargs["startupinfo"] = info
            super().__init__(*args, **kwargs)

    subprocess.Popen = QuietPopen
    _APPLIED = True
