import subprocess
from pathlib import Path

import numpy as np
import pytest

from dramastill.config.defaults import VideoSettings
from dramastill.media.bookends import deliver_movie
from dramastill.media.ffmpeg_util import FFmpegError, probe, resolve_ffmpeg


def _clip(ffmpeg: str, path: Path, color: str, seconds: float, size: str = "160x90") -> None:
    subprocess.check_call(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r=12:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )


def _pixel(path: Path, second: float) -> np.ndarray:
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
    frame = np.frombuffer(raw, dtype=np.uint8).reshape((-1, 320, 3))
    return frame.reshape((-1, 3)).mean(axis=0)


def test_intro_and_outro_wrap_the_same_movie(tmp_path):
    settings = VideoSettings()
    settings.width = 320
    settings.height = 180
    settings.fps = 12
    settings.video_bitrate = "600k"
    settings.audio_bitrate = "96k"
    ffmpeg, ffprobe = resolve_ffmpeg(settings)
    intro = tmp_path / "intro.mp4"
    body = tmp_path / "body.mp4"
    outro = tmp_path / "outro.mp4"
    _clip(ffmpeg, intro, "red", 0.5, "640x360")
    _clip(ffmpeg, body, "green", 1.0, "320x180")
    _clip(ffmpeg, outro, "blue", 0.5, "200x200")
    settings.intro_path = str(intro)
    settings.outro_path = str(outro)
    logs = []
    result = deliver_movie(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        body=body,
        work_dir=tmp_path / "work",
        settings=settings,
        log=lambda _level, message: logs.append(message),
    )
    info = probe(ffprobe, result)
    assert info.width == 320 and info.height == 180
    assert info.has_audio and info.has_video
    assert abs(info.duration - 2.0) < 0.8
    assert _pixel(result, 0.12)[0] > _pixel(result, 0.12)[1] + 40
    middle = _pixel(result, 0.9)
    assert middle[1] > middle[0] + 20
    end = _pixel(result, info.duration - 0.15)
    assert end[2] > end[0] + 20
    assert any("начало" in line for line in logs)

    stamp = result.stat().st_mtime_ns
    again = deliver_movie(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        body=body,
        work_dir=tmp_path / "work",
        settings=settings,
        log=lambda *_args: None,
    )
    assert again == result
    assert again.stat().st_mtime_ns == stamp


def test_missing_clip_is_reported(tmp_path):
    settings = VideoSettings()
    settings.intro_path = str(tmp_path / "missing.mp4")
    ffmpeg, ffprobe = resolve_ffmpeg(settings)
    with pytest.raises(FFmpegError, match="не найден"):
        deliver_movie(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            body=tmp_path / "body.mp4",
            work_dir=tmp_path / "work",
            settings=settings,
            log=lambda *_args: None,
        )
