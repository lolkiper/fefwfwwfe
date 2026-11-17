from __future__ import annotations

import os
from pathlib import Path

from dramastill.config.defaults import VideoSettings


def cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def memory_snapshot() -> tuple[float, float] | None:
    path = Path("/proc/meminfo")
    if not path.exists():
        return None
    info: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            info[key] = int(raw.strip().split()[0])
        total = info["MemTotal"]
        available = info["MemAvailable"]
    except (OSError, KeyError, ValueError):
        return None
    if total <= 0:
        return None
    used = 100.0 * (1.0 - available / total)
    available_mb = available / 1024
    return used, available_mb


def memory_allows(settings: VideoSettings) -> bool:
    snapshot = memory_snapshot()
    if snapshot is None:
        return True
    used, available_mb = snapshot
    if available_mb >= 800:
        return True
    limit = min(95, max(50, int(settings.memory_limit_percent or 90)))
    return used < limit


def project_slots(settings: VideoSettings) -> int:
    requested = max(1, int(settings.max_parallel_projects or 1))
    if settings.performance_mode == "low" or settings.render_mode == "low_resource":
        return 1
    if settings.performance_mode == "maximum":
        return min(requested, 6)
    return min(requested, 2)


def chunk_workers(settings: VideoSettings) -> int:
    if settings.performance_mode == "low" or settings.render_mode == "low_resource":
        return 1
    budget = settings.cpu_threads if settings.cpu_threads > 0 else cpu_count()
    if settings.performance_mode == "maximum" and settings.render_mode == "fast":
        return max(1, min(3, max(1, budget - 1)))
    return max(1, min(2, max(1, budget // 2)))


def encode_threads(settings: VideoSettings, workers: int) -> int:
    budget = settings.cpu_threads if settings.cpu_threads > 0 else cpu_count()
    if settings.performance_mode == "low" or settings.render_mode == "low_resource":
        budget = min(budget, 2)
    return max(1, budget // max(1, workers))


def allow_nvenc(settings: VideoSettings) -> bool:
    if settings.encoder == "libx264":
        return False
    if settings.encoder == "h264_nvenc":
        return True
    if settings.performance_mode == "low" or settings.render_mode == "low_resource":
        return False
    return settings.performance_mode == "maximum"
