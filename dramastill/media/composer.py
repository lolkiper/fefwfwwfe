from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VideoSettings
from dramastill.media.ffmpeg_util import (
    FFmpegError,
    encoder_args,
    probe,
    run_ffmpeg,
)
from dramastill.media.motion import motion_expressions, motion_plan
from dramastill.media.particles import LOOP_SECONDS
from dramastill.media.visualizer import BarRenderer, parse_color
from dramastill.util import concat_path, even


def frame_slices(total_frames: int, fps: int, chunk_seconds: int) -> list[tuple[int, int]]:
    size = max(1, int(chunk_seconds * fps))
    slices = []
    frame = 0
    while frame < total_frames:
        count = min(size, total_frames - frame)
        slices.append((frame, count))
        frame += count
    return slices


def render_movie(
    *,
    ffmpeg: str,
    ffprobe: str,
    image: Path,
    audio: Path,
    particles: Path | None,
    bands: np.ndarray | None,
    work_dir: Path,
    settings: VideoSettings,
    token: CancelToken,
    log,
    progress,
) -> Path:
    info = probe(ffprobe, audio)
    if info.duration <= 0.2:
        raise FFmpegError("Озвучка слишком короткая")
    fps = max(8, int(settings.fps))
    total_frames = max(1, int(round(info.duration * fps)))
    chunks_dir = work_dir / "chunks"
    audio_dir = work_dir / "audio" / "slices"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    slices = frame_slices(total_frames, fps, max(5, int(settings.chunk_seconds)))
    manifest = chunks_dir / "manifest.json"
    manifest.write_text(
        json.dumps({"fps": fps, "frames": total_frames, "slices": slices}),
        encoding="utf-8",
    )
    cache: dict = {}
    paths: list[Path] = []
    for index, (start, count) in enumerate(slices):
        token.check()
        dest = chunks_dir / f"{index:05d}.mkv"
        expected = count / fps
        if _chunk_ready(ffprobe, dest, expected):
            log("info", f"Фрагмент {index + 1} из {len(slices)} уже готов")
            paths.append(dest)
            progress((index + 1) / len(slices))
            continue
        slice_audio = audio_dir / f"{index:05d}.wav"
        _slice_audio(ffmpeg, audio, slice_audio, start / fps, expected)
        log("info", f"Собирается фрагмент {index + 1} из {len(slices)}")
        partial = dest.with_suffix(".partial.mkv")
        if partial.exists():
            partial.unlink()
        try:
            _render_chunk(
                ffmpeg=ffmpeg,
                image=image,
                audio=slice_audio,
                particles=particles,
                bands=None if bands is None else bands[start : start + count],
                dest=partial,
                settings=settings,
                start_frame=start,
                frames=count,
                token=token,
                cache=cache,
                on_fraction=lambda fraction, index=index: progress((index + fraction) / len(slices)),
            )
        except Exception:
            if partial.exists():
                partial.unlink()
            raise
        partial.replace(dest)
        if not _chunk_ready(ffprobe, dest, expected):
            dest.unlink(missing_ok=True)
            raise FFmpegError(f"Фрагмент {index + 1} собрался с неверной длительностью")
        paths.append(dest)
        progress((index + 1) / len(slices))
    final = work_dir / "final.mp4"
    _concat(ffmpeg, paths, final, settings.audio_bitrate)
    checked = probe(ffprobe, final)
    if not checked.has_video or not checked.has_audio:
        raise FFmpegError("В итоговом файле нет изображения или звука")
    if checked.duration < max(0.3, info.duration - 0.8):
        raise FFmpegError("Итоговое видео короче озвучки")
    return final


def _chunk_ready(ffprobe: str, path: Path, expected: float) -> bool:
    if not path.exists() or path.stat().st_size < 1000:
        return False
    try:
        info = probe(ffprobe, path)
    except FFmpegError:
        return False
    return info.has_video and info.has_audio and abs(info.duration - expected) < 0.35


def _slice_audio(ffmpeg: str, master: Path, dest: Path, start: float, duration: float) -> None:
    if dest.exists() and dest.stat().st_size > 100:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.4f}",
        "-t",
        f"{duration:.4f}",
        "-i",
        str(master),
        "-ar",
        "48000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    run_ffmpeg(cmd)


def _render_chunk(
    *,
    ffmpeg: str,
    image: Path,
    audio: Path,
    particles: Path | None,
    bands: np.ndarray | None,
    dest: Path,
    settings: VideoSettings,
    start_frame: int,
    frames: int,
    token: CancelToken,
    cache: dict,
    on_fraction,
) -> None:
    fps = max(8, int(settings.fps))
    duration = frames / fps
    width = even(settings.width)
    height = even(settings.height)
    plan = motion_plan(settings)
    z_expr, x_expr, y_expr = motion_expressions(settings, start_frame, fps)
    inputs = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:2",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(image),
    ]
    filter_inputs = ["[0:v]"]
    next_index = 1
    particle_index = None
    if particles is not None:
        particle_offset = (start_frame / fps) % LOOP_SECONDS
        inputs.extend(["-stream_loop", "-1", "-ss", f"{particle_offset:.3f}", "-i", str(particles)])
        particle_index = next_index
        next_index += 1
    audio_index = next_index
    inputs.extend(["-i", str(audio)])
    next_index += 1
    visual_index = None
    visual_height = 0
    if settings.visualizer and bands is not None and len(bands):
        visual_height = even(max(48, int(settings.visualizer_height * height / 1080)))
        inputs.extend(
            [
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{width}x{visual_height}",
                "-r",
                str(fps),
                "-i",
                "pipe:0",
            ]
        )
        visual_index = next_index
    graph = _filter_graph(
        settings=settings,
        plan=plan,
        z_expr=z_expr,
        x_expr=x_expr,
        y_expr=y_expr,
        fps=fps,
        width=width,
        height=height,
        duration=duration,
        audio_index=audio_index,
        particle_index=particle_index,
        visual_index=visual_index,
        visual_height=visual_height,
    )
    video_args = encoder_args(ffmpeg, settings, cache)
    cmd = inputs + [
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-frames:v",
        str(frames),
        "-r",
        str(fps),
        *video_args,
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-t",
        f"{duration:.4f}",
        str(dest),
    ]
    rows = None
    renderer = None
    if visual_index is not None:
        color = parse_color(settings.visualizer_color, settings.visualizer_opacity)
        renderer = BarRenderer(width, visual_height, color, settings.visualizer_gap)
        rows = bands
        if len(rows) < frames:
            pad = np.repeat(rows[-1:], frames - len(rows), axis=0)
            rows = np.concatenate([rows, pad], axis=0)
        rows = rows[:frames]

    def make_frames():
        if renderer is None or rows is None:
            return None
        return renderer.frames(rows)

    def on_progress(seconds: float) -> None:
        on_fraction(min(0.99, seconds / duration))

    try:
        run_ffmpeg(cmd, frame_iter=make_frames(), token=token, on_progress=on_progress)
    except FFmpegError:
        if "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "h264_nvenc":
            cache["nvenc"] = False
            soft = [
                "-c:v",
                "libx264",
                "-preset",
                settings.x264_preset or "veryfast",
                "-b:v",
                settings.video_bitrate,
                "-pix_fmt",
                "yuv420p",
            ]
            run_ffmpeg(cmd=_replace_video_codec(cmd, soft), frame_iter=make_frames(), token=token, on_progress=on_progress)
            return
        raise


def _replace_video_codec(cmd: list[str], video_args: list[str]) -> list[str]:
    if "-c:v" not in cmd:
        return cmd
    start = cmd.index("-c:v")
    end = cmd.index("-c:a")
    return cmd[:start] + video_args + cmd[end:]


def _filter_graph(
    *,
    settings: VideoSettings,
    plan: dict,
    z_expr: str,
    x_expr: str,
    y_expr: str,
    fps: int,
    width: int,
    height: int,
    duration: float,
    audio_index: int,
    particle_index: int | None,
    visual_index: int | None,
    visual_height: int,
) -> str:
    filters = [
        (
            f"[0:v]scale={plan['pre_w']}:{plan['pre_h']}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={plan['pre_w']}:{plan['pre_h']},"
            f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps={fps}"
        )
    ]
    if settings.vignette > 0.01:
        angle = min(1.2, max(0.05, settings.vignette))
        filters.append(f"vignette=PI*{angle:.3f}")
    if abs(settings.grade_contrast - 1) > 0.001 or abs(settings.grade_saturation - 1) > 0.001:
        filters.append(f"eq=contrast={settings.grade_contrast:.3f}:saturation={settings.grade_saturation:.3f}")
    filters.append("format=gbrp[base]")
    current = "[base]"
    parts = [",".join(filters)]
    if particle_index is not None:
        parts.append(
            f"[{particle_index}:v]scale={width}:{height}:flags=bilinear,fps={fps},format=gbrp[par]"
        )
        parts.append(f"{current}[par]blend=all_mode=screen:shortest=1[withpar]")
        current = "[withpar]"
    if visual_index is not None:
        top = height - visual_height
        parts.append(
            f"[{visual_index}:v]pad={width}:{height}:0:{top}:black,format=gbrp[vis]"
        )
        parts.append(f"{current}[vis]blend=all_mode=screen:shortest=1[withvis]")
        current = "[withvis]"
    parts.append(f"{current}format=yuv420p,setpts=PTS-STARTPTS[vout]")
    parts.append(
        f"[{audio_index}:a]atrim=start=0:end={duration:.4f},asetpts=PTS-STARTPTS,"
        f"apad=whole_dur={duration:.4f}[aout]"
    )
    return ";".join(parts)


def _concat(ffmpeg: str, chunks: list[Path], dest: Path, audio_bitrate: str) -> None:
    listing = dest.with_suffix(".txt")
    lines = [f"file '{concat_path(path)}'" for path in chunks]
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
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
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        run_ffmpeg(cmd)
    except FFmpegError:
        fallback = [
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
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-movflags",
            "+faststart",
            str(dest),
        ]
        run_ffmpeg(fallback)
