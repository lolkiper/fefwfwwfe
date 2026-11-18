from __future__ import annotations

from dramastill.config.defaults import VideoSettings
from dramastill.media.cooling import Decision, apply_priority, cpu_count, current

__all__ = ["cpu_count"]


def memory_snapshot() -> tuple[float, float] | None:
    from dramastill.media.cooling import read_machine

    reading = read_machine()
    if reading.ram_used_percent is None or reading.ram_available_mb is None:
        return None
    return reading.ram_used_percent, reading.ram_available_mb


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
    return _decision(settings).projects


def chunk_workers(settings: VideoSettings) -> int:
    return _decision(settings).chunk_workers


def encode_threads(settings: VideoSettings, workers: int = 1) -> int:
    del workers
    return _decision(settings).encode_threads


def allow_nvenc(settings: VideoSettings) -> bool:
    return _decision(settings).allow_nvenc


def cooling_decision(settings: VideoSettings) -> Decision:
    return _decision(settings)


def _decision(settings: VideoSettings) -> Decision:
    decision = current(settings)
    apply_priority(decision.nice)
    return decision
