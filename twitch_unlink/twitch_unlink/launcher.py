"""Точка входа для .exe (PyInstaller) — настольное окно."""

from __future__ import annotations

from twitch_unlink.gui.app import main


if __name__ == "__main__":
    raise SystemExit(main())