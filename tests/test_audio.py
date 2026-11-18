import numpy as np

from dramastill.media.audio_analysis import analyze_samples
from dramastill.media.visualizer import BarRenderer, parse_color


def test_bars_follow_real_spectrum():
    sample_rate = 22050
    seconds = np.arange(sample_rate * 2) / sample_rate
    samples = np.zeros(sample_rate * 2, dtype=np.float32)
    samples[sample_rate:] = 0.6 * np.sin(2 * np.pi * 200 * seconds[sample_rate:])
    levels = analyze_samples(samples, sample_rate, fps=25, n_bars=16, smooth=0.2, n_frames=50)
    assert levels[:8].mean() < 0.08
    assert levels[-8:, :4].mean() > 0.45
    assert levels[-10:, :3].mean() > levels[-10:, -3:].mean()


def test_renderer_grows_with_level():
    color = parse_color("#FFFFFF", 1)
    renderer = BarRenderer(120, 40, color, 0.2)
    quiet = renderer.frame(np.zeros(4, dtype=np.float32))
    loud = renderer.frame(np.ones(4, dtype=np.float32))
    assert quiet.sum() == 0
    assert loud.sum() > 1000
