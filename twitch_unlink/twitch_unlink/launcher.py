"""Точка входа для .exe (PyInstaller) — настольное окно."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _show_fatal(exc: BaseException) -> None:
    text = f"{exc}\n\n{traceback.format_exc()[-1800:]}"
    try:
        log_dir = Path.home() / "TwitchDropsUnlink"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0,
                f"{exc}\n\nПодробности: %USERPROFILE%\\TwitchDropsUnlink\\crash.log",
                "Twitch Drops Unlink",
                0x10,
            )
        except Exception:
            pass
    else:
        print(text, file=sys.stderr)


def main() -> int:
    try:
        from twitch_unlink.gui.app import main as gui_main

        return gui_main()
    except Exception as exc:  # noqa: BLE001
        _show_fatal(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())