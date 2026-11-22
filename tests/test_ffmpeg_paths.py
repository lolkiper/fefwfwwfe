import threading
import time
from pathlib import Path

from dramastill.cancel import CancelToken, PipelinePaused
from dramastill.media import ffmpeg_util
from dramastill.media.ffmpeg_util import ffmpeg_error_text, run_ffmpeg


def test_frozen_exe_uses_ffmpeg_beside_itself(tmp_path, monkeypatch):
    folder = tmp_path / "DramaStill"
    folder.mkdir()
    tool = folder / "ffmpeg.exe"
    tool.write_bytes(b"ffmpeg")
    monkeypatch.setattr(ffmpeg_util.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg_util.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ffmpeg_util.sys, "executable", str(folder / "DramaStill.exe"))
    monkeypatch.setattr(ffmpeg_util.shutil, "which", lambda _name: r"C:\somewhere\ffmpeg.EXE")

    assert Path(ffmpeg_util.resolve_tool("", "ffmpeg")) == tool


def test_explicit_path_wins(tmp_path, monkeypatch):
    chosen = tmp_path / "custom-ffmpeg.exe"
    chosen.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_util.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg_util.sys, "frozen", False, raising=False)

    assert Path(ffmpeg_util.resolve_tool(str(chosen), "ffmpeg")) == chosen


def test_pause_stops_ffmpeg_without_waiting_for_the_whole_clip():
    token = CancelToken()

    def later() -> None:
        time.sleep(0.4)
        token.request("pause")

    threading.Thread(target=later, daemon=True).start()
    started = time.perf_counter()
    try:
        run_ffmpeg(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=8000:cl=mono",
                "-t",
                "30",
                "-f",
                "null",
                "-",
            ],
            token=token,
        )
    except PipelinePaused:
        assert time.perf_counter() - started < 5
        return
    raise AssertionError("пауза не остановила ffmpeg")


def test_windows_helpers_are_started_without_a_console(monkeypatch):
    class Info:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = 7

    monkeypatch.setattr(ffmpeg_util.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg_util.subprocess, "STARTUPINFO", Info, raising=False)
    monkeypatch.setattr(ffmpeg_util.subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(ffmpeg_util.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    kwargs = ffmpeg_util.subprocess_kwargs()
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0


def test_ffmpeg_error_keeps_the_invalid_argument_line():
    stderr = "out_time=00:00:01.00\nError opening input files: Invalid argument\n"
    assert ffmpeg_error_text(stderr) == "Error opening input files: Invalid argument"
