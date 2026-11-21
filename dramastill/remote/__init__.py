from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerStatus:
    url: str
    ok: bool
    busy: int = 0
    cpu_percent: float | None = None
    temp_c: float | None = None
    accept: bool = False


def parse_worker_urls(value: str) -> list[str]:
    found: list[str] = []
    for chunk in (value or "").replace(";", ",").replace("\n", ",").split(","):
        item = chunk.strip().rstrip("/")
        if item and item not in found:
            found.append(item)
    return found


def choose_worker(workers: list[WorkerStatus], *, temp_limit: float = 82, cpu_limit: float = 0) -> str | None:
    usable = []
    for worker in workers:
        if not worker.ok or not worker.accept:
            continue
        if worker.temp_c is not None and worker.temp_c >= temp_limit:
            continue
        if _cpu_blocked(worker, cpu_limit):
            continue
        usable.append(worker)
    if not usable:
        return None
    usable.sort(key=lambda item: (item.busy, item.cpu_percent if item.cpu_percent is not None else 0, item.url))
    return usable[0].url


def assign_jobs(job_ids: list[str], workers: list[WorkerStatus], *, temp_limit: float = 82, cpu_limit: float = 0) -> dict[str, str]:
    usable = []
    for worker in workers:
        if worker.ok and worker.accept and (worker.temp_c is None or worker.temp_c < temp_limit) and not _cpu_blocked(worker, cpu_limit):
            usable.append(worker)
    if not usable:
        return {}
    load = {worker.url: worker.busy for worker in usable}
    assigned: dict[str, str] = {}
    for job_id in job_ids:
        url = min(load, key=lambda item: (load[item], item))
        assigned[job_id] = url
        load[url] += 1
    return assigned


def _cpu_blocked(worker: WorkerStatus, cpu_limit: float) -> bool:
    if worker.cpu_percent is None:
        return False
    cap = float(cpu_limit) if cpu_limit and cpu_limit > 0 else 98.0
    return worker.cpu_percent >= cap
