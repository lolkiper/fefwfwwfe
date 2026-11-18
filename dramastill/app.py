from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtWidgets import QApplication

from dramastill.ui.main_window import MainWindow


def _keep_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _write_startup_error(text: str | None = None) -> None:
    try:
        from dramastill.paths import data_dir

        (data_dir() / "startup-error.log").write_text(text or traceback.format_exc(), encoding="utf-8")
    except Exception:
        return


def _excepthook(exc_type, exc, tb) -> None:
    _write_startup_error("".join(traceback.format_exception(exc_type, exc, tb)))
    sys.__excepthook__(exc_type, exc, tb)


def main() -> None:
    import multiprocessing

    multiprocessing.freeze_support()
    _keep_stdio()
    sys.excepthook = _excepthook
    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setApplicationName("DramaStill")
        window = MainWindow()
        window.show()
        code = app.exec()
    except Exception:
        _write_startup_error()
        raise
    sys.exit(code)
