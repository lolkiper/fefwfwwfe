from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VideoSettings


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    has_video: bool
    video_codec: str
    audio_codec: str


_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def subprocess_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": info,
    }


def _tool_filename(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _beside(root: Path, name: str) -> list[Path]:
    filename = _tool_filename(name)
    paths = [root / filename, root / "ffmpeg" / "bin" / filename]
    if filename != name:
        paths.append(root / name)
    return paths


def resolve_tool(explicit: str, name: str) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        sibling = path.with_name(_tool_filename(name))
        if sibling.is_file():
            return str(sibling)
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    for root in roots:
        for candidate in _beside(root, name):
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for candidate in _beside(Path.cwd(), name):
        if candidate.is_file():
            return str(candidate)
    raise FFmpegError(
        f"Не найден {name}. Положите {_tool_filename(name)} рядом с DramaStill.exe "
        "или укажите путь в настройках видео."
    )


def resolve_ffmpeg(settings: VideoSettings) -> tuple[str, str]:
    ffmpeg = resolve_tool(settings.ffmpeg_path, "ffmpeg")
    ffprobe = resolve_tool(settings.ffprobe_path, "ffprobe")
    if not settings.ffprobe_path:
        beside = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
        if beside.exists():
            ffprobe = str(beside)
    return ffmpeg, ffprobe


def probe(ffprobe: str, path: Path) -> MediaInfo:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.STDOUT, **subprocess_kwargs())
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(f"ffprobe не прочитал {path.name}") from exc
    data = json.loads(raw.decode("utf-8", errors="replace"))
    duration = float((data.get("format") or {}).get("duration") or 0)
    width = height = 0
    has_audio = has_video = False
    video_codec = audio_codec = ""
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not has_video:
            has_video = True
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            video_codec = str(stream.get("codec_name") or "")
        elif stream.get("codec_type") == "audio" and not has_audio:
            has_audio = True
            audio_codec = str(stream.get("codec_name") or "")
    return MediaInfo(duration, width, height, has_audio, has_video, video_codec, audio_codec)


def decode_f32_mono(ffmpeg: str, path: Path, sample_rate: int = 22050):
    import numpy as np

    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.PIPE, **subprocess_kwargs())
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[-500:]
        raise FFmpegError(f"Не удалось декодировать аудио: {detail}") from exc
    if not raw:
        raise FFmpegError("Аудиофайл пустой")
    return np.frombuffer(raw, dtype=np.float32).copy()


def run_ffmpeg(
    cmd: list[str],
    *,
    frame_iter=None,
    token: CancelToken | None = None,
    on_progress=None,
) -> None:
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if frame_iter is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **subprocess_kwargs(),
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)
    writer_error: list[BaseException] = []

    def write_frames() -> None:
        assert process.stdin is not None
        try:
            for frame in frame_iter:
                if token is not None:
                    token.check()
                process.stdin.write(frame)
            process.stdin.close()
        except BrokenPipeError as exc:
            writer_error.append(exc)
        except Exception as exc:
            writer_error.append(exc)
            try:
                process.kill()
            except OSError:
                return

    def read_stderr() -> bytes:
        assert process.stderr is not None
        chunks: list[bytes] = []
        for line in process.stderr:
            chunks.append(line)
            if on_progress is None:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("out_time="):
                seconds = _progress_seconds(text.split("=", 1)[1])
                if seconds is not None:
                    on_progress(seconds)
        return b"".join(chunks)

    stderr_box: list[bytes] = []

    def read() -> None:
        stderr_box.append(read_stderr())

    stop_watch = threading.Event()

    def watch_cancel() -> None:
        if token is None:
            return
        while not stop_watch.wait(0.2):
            if not token.requested:
                continue
            try:
                process.kill()
            except OSError:
                return
            return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    if frame_iter is not None:
        writer = threading.Thread(target=write_frames, daemon=True)
        writer.start()
        writer.join()
    code = process.wait()
    stop_watch.set()
    reader.join()
    watcher.join(timeout=1)
    with _ACTIVE_LOCK:
        _ACTIVE.discard(process)
    stderr = (stderr_box[0] if stderr_box else b"").decode("utf-8", errors="replace")
    for exc in writer_error:
        if isinstance(exc, BrokenPipeError):
            continue
        raise exc
    if token is not None and token.requested:
        token.check()
    if code != 0:
        tail = ffmpeg_error_text(stderr)
        raise FFmpegError(tail or f"ffmpeg завершился с кодом {code}")


def stop_active_ffmpeg() -> None:
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE)
    for process in processes:
        try:
            process.kill()
        except OSError:
            continue


def encoder_args(ffmpeg: str, settings: VideoSettings, cache: dict, threads: int = 0) -> list[str]:
    from dramastill.media.resources import allow_nvenc

    choice = settings.encoder
    if choice == "auto":
        choice = "h264_nvenc" if allow_nvenc(settings) and nvenc_works(ffmpeg, cache) else "libx264"
    thread_args = ["-threads", str(max(1, threads))] if threads > 0 else []
    if choice == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-b:v",
            settings.video_bitrate,
            "-maxrate",
            settings.video_bitrate,
            "-pix_fmt",
            "yuv420p",
            *thread_args,
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.x264_preset or "veryfast",
        "-tune",
        "stillimage",
        "-b:v",
        settings.video_bitrate,
        "-pix_fmt",
        "yuv420p",
        *thread_args,
    ]


def nvenc_works(ffmpeg: str, cache: dict) -> bool:
    if "nvenc" in cache:
        return bool(cache["nvenc"])
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=0.2",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, **subprocess_kwargs())
        cache["nvenc"] = result.returncode == 0
    except OSError:
        cache["nvenc"] = False
    return bool(cache["nvenc"])


def ffmpeg_error_text(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip() and not line.strip().startswith("out_time")]
    return "\n".join(lines[-12:])


def _progress_seconds(value: str) -> float | None:
    text = value.strip()
    if not text or text == "N/A":
        return None
    try:
        hours, minutes, seconds = text.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
