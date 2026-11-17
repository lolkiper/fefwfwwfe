from __future__ import annotations

import json
from pathlib import Path

from dramastill.config.defaults import VideoSettings
from dramastill.media.ffmpeg_util import FFmpegError, probe, run_ffmpeg
from dramastill.util import concat_path, even


def deliver_movie(
    *,
    ffmpeg: str,
    ffprobe: str,
    body: Path,
    work_dir: Path,
    settings: VideoSettings,
    log,
) -> Path:
    intro = _existing_clip(settings.intro_path, "начале")
    outro = _existing_clip(settings.outro_path, "конце")
    if intro is None and outro is None:
        return body
    dest = work_dir / "with_clips.mp4"
    signature = {
        "body": _mark(body),
        "intro": None if intro is None else _mark(intro),
        "outro": None if outro is None else _mark(outro),
        "width": even(settings.width),
        "height": even(settings.height),
        "fps": max(8, int(settings.fps)),
        "video_bitrate": settings.video_bitrate,
        "audio_bitrate": settings.audio_bitrate,
    }
    meta_path = work_dir / "with_clips.json"
    if dest.exists() and meta_path.exists():
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = None
        if saved == signature and _playable(ffprobe, dest):
            log("info", "Видео с вашими фрагментами уже собрано")
            return dest
    folder = work_dir / "bookends"
    folder.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    if intro is not None:
        log("info", f"Подгоняется фрагмент в начало: {intro.name}")
        parts.append(_normalize(ffmpeg, ffprobe, intro, folder / "intro.mp4", settings))
    parts.append(body)
    if outro is not None:
        log("info", f"Подгоняется фрагмент в конец: {outro.name}")
        parts.append(_normalize(ffmpeg, ffprobe, outro, folder / "outro.mp4", settings))
    where = []
    if intro is not None:
        where.append("начало")
    if outro is not None:
        where.append("конец")
    log("info", "Ваши фрагменты встают в " + " и ".join(where))
    _join(ffmpeg, ffprobe, parts, dest, settings)
    meta_path.write_text(json.dumps(signature), encoding="utf-8")
    return dest


def _existing_clip(raw: str, place: str) -> Path | None:
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_file():
        raise FFmpegError(f"Фрагмент в {place} не найден: {path}")
    return path


def _mark(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _playable(ffprobe: str, path: Path) -> bool:
    try:
        info = probe(ffprobe, path)
    except FFmpegError:
        return False
    return info.has_video and info.has_audio and info.duration > 0.2


def _normalize(ffmpeg: str, ffprobe: str, source: Path, dest: Path, settings: VideoSettings) -> Path:
    info = probe(ffprobe, source)
    if not info.has_video:
        raise FFmpegError(f"Во фрагменте нет изображения: {source.name}")
    width = even(settings.width)
    height = even(settings.height)
    fps = max(8, int(settings.fps))
    video = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={fps},format=yuv420p,setsar=1"
    )
    partial = dest.with_suffix(".partial.mp4")
    if partial.exists():
        partial.unlink()
    tail = [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        settings.video_bitrate or "8M",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        settings.audio_bitrate or "192k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    if info.has_audio:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vf", video, *tail]
    else:
        duration = max(0.2, info.duration or 0.2)
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-vf",
            video,
            *tail,
        ]
    try:
        run_ffmpeg(cmd)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest


def _join(ffmpeg: str, ffprobe: str, parts: list[Path], dest: Path, settings: VideoSettings) -> None:
    expected = sum(probe(ffprobe, path).duration for path in parts)
    partial = dest.with_suffix(".partial.mp4")
    if partial.exists():
        partial.unlink()
    listing = dest.with_suffix(".txt")
    listing.write_text("\n".join(f"file '{concat_path(path)}'" for path in parts) + "\n", encoding="utf-8")
    copy_cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    copied = False
    try:
        run_ffmpeg(copy_cmd)
        copied = _duration_matches(ffprobe, partial, expected)
    except FFmpegError:
        copied = False
    if not copied:
        if partial.exists():
            partial.unlink()
        _reencode(ffmpeg, parts, partial, settings)
        if not _duration_matches(ffprobe, partial, expected):
            partial.unlink(missing_ok=True)
            raise FFmpegError("Не удалось соединить ваши фрагменты с роликом")
    partial.replace(dest)


def _duration_matches(ffprobe: str, path: Path, expected: float) -> bool:
    if not path.exists() or path.stat().st_size < 1000:
        return False
    try:
        info = probe(ffprobe, path)
    except FFmpegError:
        return False
    return info.has_video and info.has_audio and abs(info.duration - expected) < 1.25


def _reencode(ffmpeg: str, parts: list[Path], dest: Path, settings: VideoSettings) -> None:
    fps = max(8, int(settings.fps))
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, path in enumerate(parts):
        inputs.extend(["-i", str(path)])
        filters.append(f"[{index}:v]fps={fps},format=yuv420p,setsar=1[v{index}]")
        filters.append(f"[{index}:a]aformat=sample_fmts=fltp:channel_layouts=mono,aresample=48000[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    graph = ";".join(filters) + ";" + "".join(labels) + f"concat=n={len(parts)}:v=1:a=1[v][a]"
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        settings.video_bitrate or "8M",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        settings.audio_bitrate or "192k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    run_ffmpeg(cmd)
