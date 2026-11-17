from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from dramastill.media.ffmpeg_util import decode_f32_mono


def ensure_bands(
    *,
    ffmpeg: str,
    audio: Path,
    dest: Path,
    meta_path: Path,
    fps: int,
    n_bars: int,
    smooth: float,
    n_frames: int,
) -> np.ndarray:
    signature = {
        "fps": fps,
        "bars": n_bars,
        "smooth": round(float(smooth), 3),
        "frames": n_frames,
        "audio_size": audio.stat().st_size if audio.exists() else 0,
    }
    if dest.exists() and meta_path.exists():
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
        if saved == signature:
            data = np.load(dest)
            if data.shape == (n_frames, n_bars):
                return data
    samples = decode_f32_mono(ffmpeg, audio, 22050)
    levels = analyze_samples(samples, 22050, fps=fps, n_bars=n_bars, smooth=smooth, n_frames=n_frames)
    dest.parent.mkdir(parents=True, exist_ok=True)
    np.save(dest, levels)
    meta_path.write_text(json.dumps(signature), encoding="utf-8")
    return levels


def analyze_samples(
    samples: np.ndarray,
    sample_rate: int,
    *,
    fps: int,
    n_bars: int,
    smooth: float,
    n_frames: int | None = None,
) -> np.ndarray:
    if n_frames is None:
        n_frames = max(1, int(round(len(samples) / sample_rate * fps)))
    hop = sample_rate / float(fps)
    window = 2048
    padded = np.pad(samples.astype(np.float32, copy=False), (0, window + 8))
    hann = np.hanning(window).astype(np.float32)
    freqs = np.fft.rfftfreq(window, d=1.0 / sample_rate)
    high = min(7600, sample_rate / 2 - 50)
    edges = np.geomspace(80, max(160, high), n_bars + 1)
    masks = [(freqs >= edges[index]) & (freqs < edges[index + 1]) for index in range(n_bars)]
    raw = np.zeros((n_frames, n_bars), dtype=np.float32)
    positions = np.arange(window)
    for start in range(0, n_frames, 256):
        end = min(n_frames, start + 256)
        starts = (np.arange(start, end) * hop).astype(np.int64)
        index = np.clip(starts[:, None] + positions[None, :], 0, padded.shape[0] - 1)
        chunk = padded[index] * hann
        spectrum = np.abs(np.fft.rfft(chunk, axis=1)) ** 2
        for bar, mask in enumerate(masks):
            if not np.any(mask):
                continue
            raw[start:end, bar] = np.log1p(spectrum[:, mask].mean(axis=1) * 80.0)
    peak = float(np.percentile(raw, 95)) if raw.size else 1.0
    if peak <= 1e-6:
        peak = 1.0
    normalized = np.clip(raw / peak, 0.0, 1.0)
    normalized[normalized < 0.045] = 0.0
    return _smooth(normalized, smooth)


def _smooth(raw: np.ndarray, smooth: float) -> np.ndarray:
    amount = min(1.0, max(0.0, float(smooth)))
    release = np.float32(0.05 + (1.0 - amount) ** 1.35 * 0.9)
    attack = np.float32(0.42 + (1.0 - amount) * 0.45)
    output = np.empty_like(raw)
    previous = np.zeros(raw.shape[1], dtype=np.float32)
    for index in range(raw.shape[0]):
        sample = raw[index]
        coeff = np.where(sample > previous, attack, release).astype(np.float32)
        previous = previous + coeff * (sample - previous)
        output[index] = previous
    return output
