from __future__ import annotations

from dramastill.config.defaults import VideoSettings


def motion_plan(settings: VideoSettings) -> dict:
    amplitude = max(0.0, float(settings.zoom_amplitude))
    base = max(1.05, float(settings.zoom_base))
    secondary = amplitude * 0.45
    if base - amplitude - secondary < 1.035:
        amplitude = max(0.0, (base - 1.035) / 1.45)
        secondary = amplitude * 0.45
    min_zoom = max(1.035, base - amplitude - secondary)
    margin_x = max(0.0, settings.width * (min_zoom - 1.0) / 2.0 - 4.0)
    margin_y = max(0.0, settings.height * (min_zoom - 1.0) / 2.0 - 4.0)
    shake = min(abs(float(settings.shake_px)), margin_x * 0.3)
    pan_x = min(abs(float(settings.pan_x_px)), max(0.0, margin_x - shake * 0.55))
    pan_y = min(abs(float(settings.pan_y_px)), max(0.0, margin_y - shake * 0.4))
    return {
        "base": base,
        "amplitude": amplitude,
        "secondary": secondary,
        "min_zoom": min_zoom,
        "margin_x": margin_x,
        "margin_y": margin_y,
        "pan_x": pan_x,
        "pan_y": pan_y,
        "shake": shake,
    }


def motion_expressions(settings: VideoSettings, start_frame: int, fps: int) -> tuple[str, str, str]:
    """Zoom and pan for scale+crop.

    ``n`` is the frame inside the chunk. Adding the chunk start keeps one
    continuous float across the whole film. Expressions stay free of commas
    because a comma would split the filter graph.
    """

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
            f"{main:.{places}f}*sin(2*PI*(n+{origin})/{cycles(period)})"
            f"+{glide:.{places}f}*sin(2*PI*(n+{origin})/{cycles(slow)})"
        )

    z = (
        f"{plan['base']:.5f}"
        f"+{drift(plan['amplitude'], z_period, 5)}"
        f"+{drift(plan['secondary'], z_period * 1.73, 5)}"
    )
    x = (
        "(in_w-out_w)/2"
        f"+{drift(plan['pan_x'], x_period, 3)}"
        f"+{drift(plan['shake'] * 0.55, max(shake_period, 5.5), 3)}"
    )
    y = (
        "(in_h-out_h)/2"
        f"+{drift(plan['pan_y'], y_period, 3)}"
        f"+{drift(plan['shake'] * 0.4, max(shake_period * 1.37, 6.5), 3)}"
    )
    return z, x, y


def motion_rate(fps: int) -> int:
    """Frames actually drawn per second.

    Output stays at the project rate. Slow camera moves and sparse sparks do
    not need a new picture on every output frame, so the expensive filters run
    at this rate and the result is repeated up to the output rate.
    """

    fps = max(8, int(fps))
    if fps >= 24:
        return 8
    return fps


def ken_burns_filter(settings: VideoSettings, start_frame: int, fps: int, width: int, height: int) -> str:
    z_expr, x_expr, y_expr = motion_expressions(settings, start_frame, fps)
    return (
        f"scale=w='{width}*({z_expr})':h='{height}*({z_expr})':"
        "force_original_aspect_ratio=increase:eval=frame:flags=bilinear,"
        f"crop={width}:{height}:x='{x_expr}':y='{y_expr}'"
    )
