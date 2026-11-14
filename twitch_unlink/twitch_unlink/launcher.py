"""Точка входа для собранного установщика (PyInstaller)."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765


def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Twitch Drops — отвязка игр")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)

    url = f"http://{HOST}:{args.port}/"
    if not args.no_browser:
        webbrowser.open(url)

    import uvicorn

    uvicorn.run(
        "twitch_unlink.web.app:app",
        host=HOST,
        port=args.port,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())