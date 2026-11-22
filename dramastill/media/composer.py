from __future__ import annotations

import json
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VideoSettings
from dramastill.media.ffmpeg_util import (
    FFmpegError,
    cuda_motion_works,
    encoder_args,
    probe,
    run_ffmpeg,
)
from dramastill.media.motion import ken_burns_filter, ken_burns_gpu_filter, motion_rate
from dramastill.media.particles import LOOP_SECONDS
from dramastill.media.resources import chunk_workers, encode_threads
from dramastill.media.visualizer import BarRenderer, parse_color
from dramastill.util import concat_path, even


def _chapter_slices(
    total_frames: int,
    fps: int,
    chunk_seconds: int,
    spans: list[tuple[float, float, Path]],
    fallback: Path,
) -> list[tuple[int, int, Path]]:
    usable = [(start, end, path) for start, end, path in spans if end > start and path is not None]
    if not usable:
        return [(start, count, fallback) for start, count in frame_slices(total_frames, fps, chunk_seconds)]
    pieces: list[tuple[int, int, Path]] = []
    cursor = 0
    for index, (_start, end, path) in enumerate(usable):
        if index == len(usable) - 1:
            end_frame = total_frames
        else:
            end_frame = max(cursor, min(total_frames, int(round(end * fps))))
        if end_frame <= cursor:
            continue
        for offset, count in frame_slices(end_frame - cursor, fps, chunk_seconds):
            pieces.append((cursor + offset, count, path))
        cursor = end_frame
    if cursor < total_frames:
        pieces.append((cursor, total_frames - cursor, usable[-1][2]))
    return pieces


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
    chapter_spans: list[tuple[float, float, Path]] | None = None,
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
    slices = _chapter_slices(
        total_frames,
        fps,
        max(5, int(settings.chunk_seconds)),
        chapter_spans or [],
        image,
    )
    manifest = chunks_dir / "manifest.json"
    signature = _movie_signature(
        settings=settings,
        fps=fps,
        total_frames=total_frames,
        slices=slices,
        image=image,
        audio=audio,
        particles=particles,
    )
    reusable = False
    if manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = None
        reusable = isinstance(saved, dict) and saved.get("signature") == signature
    manifest.write_text(
        json.dumps(
            {"signature": signature, "fps": fps, "frames": total_frames, "slices": signature["slices"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache: dict = {}
    cache["cuda_filters"] = cuda_motion_works(ffmpeg, cache)
    paths: list[Path | None] = [None] * len(slices)
    jobs: list[tuple[int, int, int, Path, float, Path]] = []
    for index, (start, count, still) in enumerate(slices):
        token.check()
        dest = chunks_dir / f"{index:05d}.mkv"
        expected = count / fps
        if reusable and _chunk_ready(ffprobe, dest, expected):
            log("info", f"Фрагмент {index + 1} из {len(slices)} уже готов, кадр {still.name}")
            paths[index] = dest
            continue
        slice_audio = audio_dir / f"{index:05d}.wav"
        _slice_audio(ffmpeg, audio, slice_audio, start / fps, expected)
        jobs.append((index, start, count, dest, expected, still))
    done = len(slices) - len(jobs)
    if slices:
        progress(done / len(slices))
    workers = 1 if len(jobs) <= 1 else chunk_workers(settings)
    threads = encode_threads(settings, workers)
    if jobs:
        from dramastill.media.resources import allow_nvenc

        if cache.get("cuda_filters") and allow_nvenc(settings):
            log(
                "info",
                "Кодирование: NVIDIA NVENC. Зум и сдвиг кадра считает видеокарта. "
                "Виньетка, искры и полоски остаются на процессоре.",
            )
        elif cache.get("cuda_filters"):
            log(
                "info",
                "Зум и сдвиг кадра считает видеокарта. Кодирование фрагментов на процессоре. "
                "Виньетка, искры и полоски тоже остаются на процессоре.",
            )
        elif allow_nvenc(settings):
            log(
                "info",
                "Кодирование: NVIDIA NVENC. Зум остаётся на процессоре: видеокарта не приняла Vulkan.",
            )
        else:
            log("info", "Кодирование фрагментов на процессоре")
        log("info", f"Сборка фрагментов: {len(jobs)}, одновременно {workers}, потоков фильтра на фрагмент {threads}")

    def _job(item: tuple[int, int, int, Path, float, Path]) -> int:
        index, start, count, dest, expected, still = item
        token.check()
        log("info", f"Собирается фрагмент {index + 1} из {len(slices)}, кадр {still.name}")
        partial = dest.with_suffix(".partial.mkv")
        if partial.exists():
            partial.unlink()
        slice_audio = audio_dir / f"{index:05d}.wav"
        try:
            _render_chunk(
                ffmpeg=ffmpeg,
                image=still,
                audio=slice_audio,
                particles=particles,
                bands=None if bands is None else bands[start : start + count],
                dest=partial,
                settings=settings,
                start_frame=start,
                frames=count,
                token=token,
                cache=cache,
                threads=threads,
                log=log,
                on_fraction=lambda fraction, index=index: progress((done + fraction) / len(slices)),
            )
        except Exception:
            if partial.exists():
                partial.unlink()
            raise
        partial.replace(dest)
        if not _chunk_ready(ffprobe, dest, expected):
            dest.unlink(missing_ok=True)
            raise FFmpegError(f"Фрагмент {index + 1} собрался с неверной длительностью")
        return index

    if workers <= 1:
        for item in jobs:
            index = _job(item)
            paths[index] = item[3]
            done += 1
            progress(done / len(slices))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(_job, item): item for item in jobs}
            try:
                for future in as_completed(future_map):
                    item = future_map[future]
                    index = future.result()
                    paths[index] = item[3]
                    done += 1
                    progress(done / len(slices))
            except Exception:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
    ordered = [path for path in paths if path is not None]
    if len(ordered) != len(slices):
        raise FFmpegError("Не все фрагменты видео собрались")
    final = work_dir / "final.mp4"
    _concat(ffmpeg, ordered, final, settings.audio_bitrate)
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
    if _slice_reusable(dest, master, duration):
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
    threads: int = 0,
    log=None,
) -> None:
    fps = max(8, int(settings.fps))
    drawn = motion_rate(fps)
    duration = frames / fps
    width = even(settings.width)
    height = even(settings.height)
    origin = int(round(start_frame * drawn / fps))
    cuda = bool(cache.get("cuda_filters"))
    inputs = [ffmpeg, "-y"]
    inputs.extend(
        [
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:2",
        "-filter_threads",
        str(max(1, threads or 1)),
        "-filter_complex_threads",
        str(max(1, threads or 1)),
        "-loop",
        "1",
        "-framerate",
        str(drawn),
        "-i",
        str(image),
        ]
    )
    particle_index = None
    next_index = 1
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
                "rgba",
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
        motion=ken_burns_filter(settings, origin, drawn, width, height),
        cuda_motion=ken_burns_gpu_filter(settings, origin, drawn, width, height) if cuda else "",
        drawn=drawn,
        fps=fps,
        width=width,
        height=height,
        duration=duration,
        audio_index=audio_index,
        particle_index=particle_index,
        visual_index=visual_index,
        visual_height=visual_height,
    )
    video_args = encoder_args(ffmpeg, settings, cache, threads)
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
        if cuda and _command_uses_gpu(cmd):
            cache["cuda_filters"] = False
            if log is not None:
                log("warning", "Видеокарта не приняла зум, фрагмент считается на процессоре")
            cpu_cmd = _without_cuda(cmd, settings, origin, drawn, fps, width, height, duration, audio_index, particle_index, visual_index, visual_height)
            try:
                run_ffmpeg(cpu_cmd, frame_iter=make_frames(), token=token, on_progress=on_progress)
                return
            except FFmpegError:
                cmd = cpu_cmd
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


def _command_uses_gpu(cmd: list[str]) -> bool:
    if "-filter_complex" not in cmd:
        return False
    return "libplacebo=" in cmd[cmd.index("-filter_complex") + 1]


def _without_cuda(
    cmd: list[str],
    settings: VideoSettings,
    origin: int,
    drawn: int,
    fps: int,
    width: int,
    height: int,
    duration: float,
    audio_index: int,
    particle_index: int | None,
    visual_index: int | None,
    visual_height: int,
) -> list[str]:
    cleaned = []
    skip = 0
    for item in cmd:
        if skip:
            skip -= 1
            continue
        if item in {"-init_hw_device", "-filter_hw_device"}:
            skip = 1
            continue
        cleaned.append(item)
    graph = _filter_graph(
        settings=settings,
        motion=ken_burns_filter(settings, origin, drawn, width, height),
        cuda_motion="",
        drawn=drawn,
        fps=fps,
        width=width,
        height=height,
        duration=duration,
        audio_index=audio_index,
        particle_index=particle_index,
        visual_index=visual_index,
        visual_height=visual_height,
    )
    if "-filter_complex" in cleaned:
        cleaned[cleaned.index("-filter_complex") + 1] = graph
    return cleaned


def _filter_graph(
    *,
    settings: VideoSettings,
    motion: str,
    drawn: int,
    fps: int,
    width: int,
    height: int,
    duration: float,
    audio_index: int,
    particle_index: int | None,
    visual_index: int | None,
    visual_height: int,
    cuda_motion: str = "",
) -> str:
    tail: list[str] = []
    if settings.vignette > 0.01:
        angle = min(1.2, max(0.05, settings.vignette))
        tail.append(f"vignette=PI*{angle:.3f}")
    if abs(settings.grade_contrast - 1) > 0.001 or abs(settings.grade_saturation - 1) > 0.001:
        tail.append(f"eq=contrast={settings.grade_contrast:.3f}:saturation={settings.grade_saturation:.3f}")
    tail.append("format=gbrp[base]" if particle_index is not None else f"fps={fps},format=yuv420p[base]")
    if cuda_motion:
        # The label must sit against the next filter. A comma here makes
        # ffmpeg reject the graph, and every chunk falls back to the CPU.
        head = [f"[0:v]{cuda_motion}[moved]"]
        chain = "[moved]" + ",".join(tail)
    else:
        head = []
        chain = f"[0:v]{motion}," + ",".join(tail)
    current = "[base]"
    parts = [*head, chain]
    if particle_index is not None:
        parts.append(
            f"[{particle_index}:v]fps={drawn},scale={width}:{height}:flags=bilinear,format=gbrp[par]"
        )
        parts.append(f"{current}[par]blend=all_mode=screen:shortest=1,fps={fps}[withpar]")
        current = "[withpar]"
    if visual_index is not None:
        top = height - visual_height
        parts.append(f"[{visual_index}:v]format=rgba[vis]")
        parts.append(f"{current}[vis]overlay=0:{top}:shortest=1:format=auto[withvis]")
        current = "[withvis]"
    parts.append(f"{current}format=yuv420p,setpts=PTS-STARTPTS[vout]")
    parts.append(
        f"[{audio_index}:a]atrim=start=0:end={duration:.4f},asetpts=PTS-STARTPTS,"
        f"apad=whole_dur={duration:.4f}[aout]"
    )
    return ";".join(parts)


def _movie_signature(
    *,
    settings: VideoSettings,
    fps: int,
    total_frames: int,
    slices: list[tuple[int, int]],
    image: Path,
    audio: Path,
    particles: Path | None,
) -> dict:
    def mark(path: Path) -> list[int]:
        stat = path.stat()
        return [int(stat.st_size), int(stat.st_mtime)]

    return {
        "width": even(settings.width),
        "height": even(settings.height),
        "fps": fps,
        "frames": total_frames,
        "slices": [
            [int(item[0]), int(item[1]), str(item[2])] if len(item) > 2 else [int(item[0]), int(item[1])]
            for item in slices
        ],
        "bitrate": settings.video_bitrate,
        "audio_bitrate": settings.audio_bitrate,
        "encoder": settings.encoder,
        "preset": settings.x264_preset,
        "visualizer": bool(settings.visualizer),
        "bars": int(settings.visualizer_bars),
        "color": settings.visualizer_color,
        "opacity": f"{float(settings.visualizer_opacity):.4f}",
        "bar_height": int(settings.visualizer_height),
        "gap": f"{float(settings.visualizer_gap):.4f}",
        "smooth": f"{float(settings.visualizer_smooth):.4f}",
        "vignette": f"{float(settings.vignette):.4f}",
        "contrast": f"{float(settings.grade_contrast):.4f}",
        "saturation": f"{float(settings.grade_saturation):.4f}",
        "zoom": f"{float(settings.zoom_base):.5f}/{float(settings.zoom_amplitude):.5f}/{float(settings.zoom_period):.4f}",
        "pan": (
            f"{float(settings.pan_x_px):.3f}/{float(settings.pan_y_px):.3f}/"
            f"{float(settings.pan_period_x):.3f}/{float(settings.pan_period_y):.3f}"
        ),
        "shake": f"{float(settings.shake_px):.3f}/{float(settings.shake_period):.3f}",
        "image": mark(image),
        "audio": mark(audio),
        "particles": None if particles is None else mark(particles),
    }


def _slice_reusable(dest: Path, master: Path, expected: float) -> bool:
    if not dest.exists() or dest.stat().st_size <= 100:
        return False
    try:
        if dest.stat().st_mtime_ns + 1_000_000 < master.stat().st_mtime_ns:
            return False
    except OSError:
        return False
    measured = _wav_seconds(dest)
    if measured is None:
        return False
    return abs(measured - expected) < 0.12


def _wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError, OSError):
        return None
    if rate <= 0:
        return None
    return frames / float(rate)


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
