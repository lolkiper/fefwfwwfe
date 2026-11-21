import json
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VideoSettings
from dramastill.media.audio_analysis import ensure_bands
from dramastill.media.composer import render_movie
from dramastill.media.ffmpeg_util import probe, resolve_ffmpeg


def _card(path: Path) -> None:
    image = Image.new("RGB", (960, 540), (18, 16, 22))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 70, 540), fill=(190, 40, 40))
    draw.rectangle((890, 0, 960, 540), fill=(30, 50, 190))
    draw.ellipse((330, 80, 630, 430), fill=(176, 132, 110))
    image.save(path)


def _audio(path: Path) -> None:
    rate = 48000
    silence = np.zeros(rate * 2, dtype=np.float32)
    time = np.arange(rate * 2) / rate
    tone = (0.45 * np.sin(2 * np.pi * 180 * time)).astype(np.float32)
    samples = np.concatenate([silence, tone])
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def _frame(path: Path, second: float) -> np.ndarray:
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{second:.3f}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    return np.frombuffer(raw, dtype=np.uint8).reshape((360, 640, 3))


def test_render_signature_changes_with_the_frame_and_roundtrips(tmp_path):
    from dramastill.media.composer import _movie_signature

    image = tmp_path / "still.png"
    audio = tmp_path / "voice.wav"
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    settings = VideoSettings()
    first = _movie_signature(
        settings=settings,
        fps=30,
        total_frames=300,
        slices=[(0, 300)],
        image=image,
        audio=audio,
        particles=None,
    )
    restored = json.loads(json.dumps(first))
    assert restored == first
    settings.width = 1280
    changed = _movie_signature(
        settings=settings,
        fps=30,
        total_frames=300,
        slices=[(0, 300)],
        image=image,
        audio=audio,
        particles=None,
    )
    assert changed != first


def test_audio_slice_is_rebuilt_when_it_does_not_match(tmp_path):
    from dramastill.media.composer import _slice_reusable

    master = tmp_path / "master.wav"
    _audio(master)
    short = tmp_path / "slice.wav"
    with wave.open(str(short), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 4800)
    assert _slice_reusable(short, master, 4.0) is False
    assert _slice_reusable(master, master, 4.0) is True


def test_chunk_command_does_not_queue_the_visualizer_input(tmp_path, monkeypatch):
    commands = []

    def capture(cmd, **_kwargs):
        commands.append(cmd)

    monkeypatch.setattr("dramastill.media.composer.run_ffmpeg", capture)
    monkeypatch.setattr(
        "dramastill.media.composer.encoder_args",
        lambda *_args, **_kwargs: ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1M", "-pix_fmt", "yuv420p"],
    )
    settings = VideoSettings()
    settings.width = 640
    settings.height = 360
    settings.fps = 12
    settings.visualizer = True
    from dramastill.media.composer import _render_chunk

    _render_chunk(
        ffmpeg="ffmpeg",
        image=tmp_path / "still.png",
        audio=tmp_path / "voice.wav",
        particles=tmp_path / "particles.mp4",
        bands=np.ones((4, 8), dtype=np.float32),
        dest=tmp_path / "chunk.mkv",
        settings=settings,
        start_frame=0,
        frames=4,
        token=CancelToken(),
        cache={},
        on_fraction=lambda _value: None,
    )
    assert commands
    command = commands[0]
    assert "thread_queue_size" not in command
    graph = command[command.index("-filter_complex") + 1]
    assert "overlay=" in graph
    assert "eval=frame" in graph
    assert "zoompan" not in graph
    assert graph.count("blend=all_mode=screen") == 1


def test_single_image_moves_and_bars_follow_voice(tmp_path):
    image = tmp_path / "still.png"
    audio = tmp_path / "voice.wav"
    _card(image)
    _audio(audio)
    settings = VideoSettings()
    settings.width = 640
    settings.height = 360
    settings.fps = 20
    settings.chunk_seconds = 2
    settings.encoder = "libx264"
    settings.x264_preset = "ultrafast"
    settings.video_bitrate = "1200k"
    settings.sparks = False
    settings.dust = False
    settings.lights = False
    settings.visualizer = True
    settings.visualizer_bars = 16
    settings.visualizer_color = "#FFFFFF"
    settings.visualizer_opacity = 1
    settings.visualizer_height = 90
    settings.visualizer_smooth = 0.15
    settings.zoom_base = 1.08
    settings.zoom_amplitude = 0.035
    settings.zoom_period = 4
    settings.pan_x_px = 48
    settings.pan_period_x = 4
    settings.shake_px = 0
    ffmpeg, ffprobe = resolve_ffmpeg(settings)
    info = probe(ffprobe, audio)
    frames = int(round(info.duration * settings.fps))
    bands = ensure_bands(
        ffmpeg=ffmpeg,
        audio=audio,
        dest=tmp_path / "bands.npy",
        meta_path=tmp_path / "bands.json",
        fps=settings.fps,
        n_bars=settings.visualizer_bars,
        smooth=settings.visualizer_smooth,
        n_frames=frames,
    )
    final = render_movie(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        image=image,
        audio=audio,
        particles=None,
        bands=bands,
        work_dir=tmp_path / "work",
        settings=settings,
        token=CancelToken(),
        log=lambda *_args: None,
        progress=lambda _value: None,
    )
    media = probe(ffprobe, final)
    assert media.width == 640 and media.height == 360
    assert media.has_audio and media.has_video
    assert abs(media.duration - info.duration) < 0.45
    early = _frame(final, 0.15)
    moved = _frame(final, 3.6)
    assert abs(early.astype(int) - moved.astype(int)).mean() > 2.2
    quiet = float(_frame(final, 0.4)[-70:].mean())
    loud = float(_frame(final, 3.4)[-70:].mean())
    assert loud > quiet + 6
