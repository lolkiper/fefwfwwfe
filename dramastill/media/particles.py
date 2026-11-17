from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dramastill.config.defaults import VideoSettings
from dramastill.media.ffmpeg_util import probe, run_ffmpeg
from dramastill.util import even

LOOP_SECONDS = 8.0


@dataclass
class Particle:
    x0: float
    y0: float
    ax: float
    ay: float
    fx: int
    fy: int
    phx: float
    phy: float
    fo: int
    pho: float
    radius: float
    color: tuple[float, float, float]
    strength: float
    kind: str


def ensure_particles(
    *,
    ffmpeg: str,
    ffprobe: str,
    dest: Path,
    settings: VideoSettings,
    seed_text: str,
    log,
) -> Path | None:
    if not (settings.sparks or settings.dust or settings.lights):
        return None
    signature = _signature(settings, seed_text)
    meta_path = dest.with_suffix(".json")
    if dest.exists() and meta_path.exists():
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
        if saved == signature:
            try:
                info = probe(ffprobe, dest)
            except Exception:
                info = None
            if info and info.duration > 1 and info.has_video:
                log("info", "Слой искр уже собран")
                return dest
    width, height, fps, seconds = _canvas(settings)
    log("info", "Собирается бесшовный слой искр, пыли и света")
    particles = build_particles(settings, seed_text)
    _render(ffmpeg, dest, particles, width, height, fps, seconds, seed_text, settings)
    meta_path.write_text(json.dumps(signature), encoding="utf-8")
    return dest


def build_particles(settings: VideoSettings, seed_text: str) -> list[Particle]:
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    particles: list[Particle] = []
    if settings.sparks:
        count = max(8, int(72 * settings.spark_density))
        for _ in range(count):
            particles.append(
                Particle(
                    x0=float(rng.uniform(0, 1)),
                    y0=float(rng.uniform(0, 1)),
                    ax=float(rng.uniform(0.012, 0.055)),
                    ay=float(rng.uniform(0.02, 0.07)),
                    fx=int(rng.integers(1, 4)),
                    fy=int(rng.integers(1, 3)),
                    phx=float(rng.uniform(0, math.tau)),
                    phy=float(rng.uniform(0, math.tau)),
                    fo=int(rng.integers(1, 5)),
                    pho=float(rng.uniform(0, math.tau)),
                    radius=float(rng.uniform(1.1, 2.8)),
                    color=_warm(rng),
                    strength=float(settings.spark_opacity),
                    kind="spark",
                )
            )
    if settings.dust:
        count = max(6, int(58 * max(0.25, settings.spark_density)))
        for _ in range(count):
            particles.append(
                Particle(
                    x0=float(rng.uniform(0, 1)),
                    y0=float(rng.uniform(0, 1)),
                    ax=float(rng.uniform(0.008, 0.03)),
                    ay=float(rng.uniform(0.01, 0.04)),
                    fx=int(rng.integers(1, 3)),
                    fy=int(rng.integers(1, 3)),
                    phx=float(rng.uniform(0, math.tau)),
                    phy=float(rng.uniform(0, math.tau)),
                    fo=int(rng.integers(1, 3)),
                    pho=float(rng.uniform(0, math.tau)),
                    radius=float(rng.uniform(0.7, 1.5)),
                    color=(0.85, 0.82, 0.75),
                    strength=float(settings.dust_opacity) * 0.55,
                    kind="dust",
                )
            )
    if settings.lights:
        for x0, y0 in ((0.12, 0.18), (0.86, 0.16), (0.84, 0.78)):
            particles.append(
                Particle(
                    x0=x0,
                    y0=y0,
                    ax=0.03,
                    ay=0.02,
                    fx=1,
                    fy=1,
                    phx=float(rng.uniform(0, math.tau)),
                    phy=float(rng.uniform(0, math.tau)),
                    fo=1,
                    pho=float(rng.uniform(0, math.tau)),
                    radius=float(rng.uniform(28, 52)),
                    color=(1.0, 0.78, 0.45),
                    strength=float(settings.light_opacity),
                    kind="light",
                )
            )
    return particles


def position(particle: Particle, t: float, duration: float) -> tuple[float, float]:
    x = (particle.x0 + particle.ax * math.sin(math.tau * particle.fx * t / duration + particle.phx)) % 1.0
    y = (particle.y0 + particle.ay * math.sin(math.tau * particle.fy * t / duration + particle.phy)) % 1.0
    return x, y


def presence(particle: Particle, t: float, duration: float, x: float, y: float) -> float:
    fade = 0.5 + 0.5 * math.sin(math.tau * particle.fo * t / duration + particle.pho)
    fade = fade**1.8
    dx = (x - 0.50) / 0.22
    dy = (y - 0.40) / 0.30
    face = math.exp(-(dx * dx + dy * dy))
    mask = 1.0 - 0.94 * face
    if particle.kind == "light":
        mask = min(mask, 0.45)
    return fade * mask * particle.strength


def render_particle_frame(
    particles: list[Particle],
    width: int,
    height: int,
    t: float,
    duration: float,
    kernels: dict[int, np.ndarray],
) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.float32)
    for particle in particles:
        x_norm, y_norm = position(particle, t, duration)
        alpha = presence(particle, t, duration, x_norm, y_norm)
        if alpha < 0.015:
            continue
        radius_key = max(1, int(round(particle.radius)))
        kernel = kernels.get(radius_key)
        if kernel is None:
            kernel = _kernel(radius_key)
            kernels[radius_key] = kernel
        _stamp(frame, kernel, x_norm * width, y_norm * height, particle.color, alpha)
    return np.clip(frame * 255.0, 0, 255).astype(np.uint8)


def _render(ffmpeg, dest, particles, width, height, fps, seconds, seed_text, settings) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * fps)
    kernels: dict[int, np.ndarray] = {}

    def frames_iter():
        for index in range(frames):
            t = index / fps
            yield render_particle_frame(particles, width, height, t, seconds, kernels).tobytes()

    partial = dest.with_suffix(".partial.mp4")
    if partial.exists():
        partial.unlink()
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "14",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        str(partial),
    ]
    try:
        run_ffmpeg(cmd, frame_iter=frames_iter())
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    partial.replace(dest)


def _kernel(radius: int) -> np.ndarray:
    span = max(2, int(math.ceil(radius * (3.2 if radius < 8 else 2.2))))
    axis = np.arange(-span, span + 1, dtype=np.float32)
    xx, yy = np.meshgrid(axis, axis)
    sigma = max(0.6, radius * 0.72)
    kernel = np.exp(-(xx * xx + yy * yy) / (2 * sigma * sigma))
    return kernel.astype(np.float32)


def _stamp(frame: np.ndarray, kernel: np.ndarray, x: float, y: float, color: tuple[float, float, float], alpha: float) -> None:
    height, width = frame.shape[:2]
    radius = kernel.shape[0] // 2
    cx = int(round(x))
    cy = int(round(y))
    x0 = max(0, cx - radius)
    y0 = max(0, cy - radius)
    x1 = min(width, cx + radius + 1)
    y1 = min(height, cy + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return
    kx0 = x0 - (cx - radius)
    ky0 = y0 - (cy - radius)
    patch = kernel[ky0 : ky0 + (y1 - y0), kx0 : kx0 + (x1 - x0)] * alpha
    color_array = np.array(color, dtype=np.float32)
    frame[y0:y1, x0:x1] += patch[:, :, None] * color_array


def _warm(rng: np.random.Generator) -> tuple[float, float, float]:
    choice = int(rng.integers(0, 3))
    if choice == 0:
        return (1.0, 0.93, 0.78)
    if choice == 1:
        return (1.0, 0.78, 0.42)
    return (0.95, 0.88, 0.7)


def _canvas(settings: VideoSettings) -> tuple[int, int, int, float]:
    width = even(min(settings.width, 1920))
    height = even(min(settings.height, 1080))
    fps = max(8, min(24, int(settings.fps)))
    return width, height, fps, LOOP_SECONDS


def _signature(settings: VideoSettings, seed_text: str) -> dict:
    width, height, fps, seconds = _canvas(settings)
    return {
        "seed": seed_text,
        "w": width,
        "h": height,
        "fps": fps,
        "seconds": seconds,
        "sparks": settings.sparks,
        "dust": settings.dust,
        "lights": settings.lights,
        "density": round(float(settings.spark_density), 3),
        "spark_opacity": round(float(settings.spark_opacity), 3),
        "dust_opacity": round(float(settings.dust_opacity), 3),
        "light_opacity": round(float(settings.light_opacity), 3),
    }

