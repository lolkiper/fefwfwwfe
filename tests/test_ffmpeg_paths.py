from pathlib import Path

from dramastill.media import ffmpeg_util


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
