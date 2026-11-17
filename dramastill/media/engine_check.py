from __future__ import annotations

import shutil
import wave
from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VideoSettings
from dramastill.media.audio_analysis import ensure_bands
from dramastill.media.composer import render_movie
from dramastill.media.ffmpeg_util import resolve_ffmpeg
from dramastill.media.particles import ensure_particles
from dramastill.paths import font_path
from dramastill.util import even


def run_engine_check(settings: VideoSettings, output_dir: Path, work_dir: Path, log) -> Path:
    """Короткая проверка FFmpeg на тестовой карточке, не на истории и не через AI."""
    ffmpeg, ffprobe = resolve_ffmpeg(settings)
    check = deepcopy(settings)
    check.width = even(min(settings.width, 1280))
    check.height = even(min(settings.height, 720))
    check.fps = min(24, max(12, int(settings.fps)))
    check.chunk_seconds = 3
    check.encoder = "libx264"
    work_dir.mkdir(parents=True, exist_ok=True)
    image = work_dir / "card.png"
    audio = work_dir / "tone.wav"
    _draw_card(image, check.width, check.height)
    _write_tone(audio)
    from dramastill.media.ffmpeg_util import probe

    duration = probe(ffprobe, audio).duration
    frames = max(1, int(round(duration * check.fps)))
    bands = None
    if check.visualizer:
        bands = ensure_bands(
            ffmpeg=ffmpeg,
            audio=audio,
            dest=work_dir / "bands.npy",
            meta_path=work_dir / "bands.json",
            fps=check.fps,
            n_bars=max(8, min(32, int(check.visualizer_bars))),
            smooth=check.visualizer_smooth,
            n_frames=frames,
        )
    particles = ensure_particles(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        dest=work_dir / "particles.mp4",
        settings=check,
        seed_text="engine-check",
        log=log,
    )
    final = render_movie(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        image=image,
        audio=audio,
        particles=particles,
        bands=bands,
        work_dir=work_dir,
        settings=check,
        token=CancelToken(),
        log=log,
        progress=lambda _fraction: None,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "engine-check.mp4"
    shutil.copy2(final, destination)
    log("info", f"Проверка видеодвижка сохранена: {destination}")
    return destination


def _draw_card(path: Path, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height), (18, 16, 20))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = int(18 + 40 * y / height)
        draw.line([(0, y), (width, y)], fill=(shade, shade // 2, 28))
    draw.ellipse((width * 0.34, height * 0.16, width * 0.66, height * 0.78), fill=(176, 132, 108))
    draw.rectangle((width * 0.42, height * 0.78, width * 0.58, height * 0.98), fill=(42, 48, 64))
    font_file = font_path()
    font = ImageFont.truetype(str(font_file), size=max(28, width // 28)) if font_file else ImageFont.load_default()
    label = "ПРОВЕРКА ДВИЖКА"
    text_width = draw.textlength(label, font=font)
    draw.text(((width - text_width) / 2, height * 0.08), label, font=font, fill=(255, 236, 210))
    image.save(path)


def _write_tone(path: Path) -> None:
    sample_rate = 48000
    silence = np.zeros(sample_rate * 2, dtype=np.float32)
    time = np.arange(sample_rate * 2) / sample_rate
    tone = (0.35 * np.sin(2 * np.pi * 196 * time)).astype(np.float32)
    samples = np.concatenate([silence, tone, silence])
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
