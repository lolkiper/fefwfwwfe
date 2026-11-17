from __future__ import annotations

from dramastill.config.defaults import VideoSettings
from dramastill.util import even


def motion_plan(settings: VideoSettings) -> dict:
    amplitude = max(0.0, float(settings.zoom_amplitude))
    base = max(1.05, float(settings.zoom_base))
    secondary = amplitude * 0.45
    if base - amplitude - secondary < 1.035:
        amplitude = max(0.0, (base - 1.035) / 1.45)
        secondary = amplitude * 0.45
    max_zoom = base + amplitude + secondary + 0.02
    scale = max(max_zoom, 1.12) * 1.06
    pre_w = even(settings.width * scale)
    pre_h = even(settings.height * scale)
    min_zoom = max(1.03, base - amplitude - secondary)
    margin = pre_w * (1 - 1 / min_zoom) / 2
    pan_limit = margin * 0.72
    pixel_scale = pre_w / float(settings.width)
    pan_x = min(abs(settings.pan_x_px) * pixel_scale, pan_limit)
    pan_y = min(abs(settings.pan_y_px) * pixel_scale, pan_limit * pre_h / pre_w)
    shake = min(abs(settings.shake_px) * pixel_scale, pan_limit * 0.25)
    return {
        "pre_w": pre_w,
        "pre_h": pre_h,
        "base": base,
        "amplitude": amplitude,
        "secondary": secondary,
        "pan_x": pan_x,
        "pan_y": pan_y,
        "shake": shake,
    }


def motion_expressions(settings: VideoSettings, start_frame: int, fps: int) -> tuple[str, str, str]:
    plan = motion_plan(settings)
    origin = int(start_frame)
    z_period = max(8.0, float(settings.zoom_period))
    x_period = max(8.0, float(settings.pan_period_x))
    y_period = max(8.0, float(settings.pan_period_y))
    shake_period = max(0.8, float(settings.shake_period))

    def cycles(period: float) -> str:
        return f"{(fps * period):.4f}"

    def drift(amplitude: float, period: float, places: int) -> str:
        slow = max(period * 2.35, 36.0)
        main = amplitude * 0.72
        glide = amplitude * 0.28
        return (
            f"{main:.{places}f}*sin(2*PI*(on+{origin})/{cycles(period)})"
            f"+{glide:.{places}f}*sin(2*PI*(on+{origin})/{cycles(slow)})"
        )

    z = (
        f"{plan['base']:.5f}"
        f"+{drift(plan['amplitude'], z_period, 5)}"
        f"+{drift(plan['secondary'], z_period * 1.73, 5)}"
    )
    x = (
        "iw/2-(iw/zoom/2)"
        f"+{drift(plan['pan_x'], x_period, 3)}"
        f"+{drift(plan['shake'] * 0.55, max(shake_period, 5.5), 3)}"
    )
    y = (
        "ih/2-(ih/zoom/2)"
        f"+{drift(plan['pan_y'], y_period, 3)}"
        f"+{drift(plan['shake'] * 0.4, max(shake_period * 1.37, 6.5), 3)}"
    )
    return z, x, y
