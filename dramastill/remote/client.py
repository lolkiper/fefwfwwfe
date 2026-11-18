from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dramastill.cancel import CancelToken, PipelineCancelled, PipelinePaused
from dramastill.config.defaults import VideoSettings
from dramastill.core.database import Database
from dramastill.core.secrets import SecretStore
from dramastill.pipeline.runner import Callbacks, _publish
from dramastill.remote import assign_jobs, choose_worker, parse_worker_urls
from dramastill.remote import WorkerStatus

_hold_until = 0.0
_local_lock = threading.Lock()
_local_running = 0


def remote_configured(settings: VideoSettings) -> bool:
    return bool(parse_worker_urls(getattr(settings, "worker_urls", "") or ""))


def worker_count(settings: VideoSettings) -> int:
    return len(parse_worker_urls(getattr(settings, "worker_urls", "") or ""))


def hold_active() -> bool:
    return time.monotonic() < _hold_until


def begin_local(limit: int) -> bool:
    global _local_running
    with _local_lock:
        if _local_running >= max(0, limit):
            return False
        _local_running += 1
        return True


def end_local() -> None:
    global _local_running
    with _local_lock:
        _local_running = max(0, _local_running - 1)


def run_on_worker(
    db: Database,
    project_id: str,
    secrets: SecretStore,
    token: CancelToken,
    callbacks: Callbacks | None,
) -> str:
    """Submit one project and wait for the MP4.

    Returns ``done`` when the worker owned the job, ``wait`` when every
    reachable worker is busy or hot, and ``fallback`` when none answered.
    """

    project = db.get(project_id)
    if project is None:
        return "done"
    work = Path(project.work_dir)
    marker = work / "remote.json"
    if marker.exists():
        info = json.loads(marker.read_text(encoding="utf-8"))
        _finish(db, project, info, token, callbacks)
        return "done"
    urls = parse_worker_urls(project.settings.video.worker_urls)
    if not urls:
        return "fallback"
    statuses, contacted = _health(urls, project.settings.video.worker_token, token)
    choice = choose_worker(statuses, temp_limit=float(project.settings.video.temp_limit_c or 82))
    if choice is None:
        if contacted == 0:
            return "fallback"
        global _hold_until
        _hold_until = time.monotonic() + 5
        return "wait"
    payload = {
        "id": project.id,
        "title": project.title,
        "settings": project.settings.to_dict(),
        "secrets": {
            "llm": secrets.get("llm"),
            "image": secrets.get("image"),
            "tts": secrets.get("tts"),
        },
    }
    try:
        _request("POST", f"{choice}/v1/jobs", project.settings.video.worker_token, payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _hold_until = time.monotonic() + 5
            return "wait"
        return "fallback"
    except (OSError, urllib.error.URLError):
        return "fallback"
    info = {"url": choice, "id": project.id, "token": project.settings.video.worker_token}
    marker.write_text(json.dumps(info), encoding="utf-8")
    if callbacks is not None:
        callbacks.log("info", f"Проект отправлен на сервер {choice}. Компьютер может отключиться, сервер продолжит.")
    _finish(db, project, info, token, callbacks)
    return "done"


def _health(urls: list[str], token: str, cancel: CancelToken) -> tuple[list[WorkerStatus], int]:
    statuses: list[WorkerStatus] = []
    contacted = 0
    for url in urls:
        cancel.check()
        try:
            payload = _request("GET", f"{url}/v1/health", token, None)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            statuses.append(WorkerStatus(url=url, ok=False))
            continue
        contacted += 1
        statuses.append(
            WorkerStatus(
                url=url,
                ok=True,
                busy=int(payload.get("busy") or 0),
                cpu_percent=_float_or_none(payload.get("cpu_percent")),
                temp_c=_float_or_none(payload.get("temp_c")),
                accept=bool(payload.get("accept")),
            )
        )
    return statuses, contacted


def _finish(db: Database, project, info: dict, token: CancelToken, callbacks: Callbacks | None) -> None:
    url = str(info["url"]).rstrip("/")
    job_id = str(info["id"])
    auth = str(info.get("token") or "")
    while True:
        if token.requested:
            if token.mode == "cancel":
                try:
                    _request("POST", f"{url}/v1/jobs/{job_id}/cancel", auth, {"cancel": True})
                except (OSError, urllib.error.URLError, urllib.error.HTTPError):
                    pass
                db.update(project.id, status="cancelled", error="Остановлено пользователем")
                raise PipelineCancelled("Сборка остановлена")
            db.update(project.id, status="paused")
            raise PipelinePaused("Сборка поставлена на паузу")
        try:
            status = _request("GET", f"{url}/v1/jobs/{job_id}", auth, None)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            if callbacks is not None:
                callbacks.log("warning", f"Сервер временно не отвечает: {exc}. Повтор через 5 с.")
            time.sleep(5)
            continue
        stage = str(status.get("stage") or status.get("status") or "server")
        progress = float(status.get("progress") or 0)
        if callbacks is not None:
            callbacks.progress(progress, stage)
        state = str(status.get("status") or "")
        if state == "failed":
            message = str(status.get("error") or "Сервер не собрал видео")
            db.update(project.id, status="failed", error=message)
            raise RuntimeError(message)
        if state == "completed":
            break
        time.sleep(2)
    work = Path(project.work_dir)
    video = work / "remote-output.mp4"
    thumb = work / "remote-thumbnail.jpg"
    meta = work / "remote-youtube.json"
    _download(f"{url}/v1/jobs/{job_id}/output.mp4", auth, video)
    _download(f"{url}/v1/jobs/{job_id}/sidecar.jpg", auth, thumb)
    _download(f"{url}/v1/jobs/{job_id}/sidecar.youtube.json", auth, meta)
    if not thumb.exists() or not meta.exists():
        raise RuntimeError("Сервер закончил работу, но не вернул обложку или описание")
    output = _publish(project.settings, project.title, project.id, video, thumb, meta)
    db.update(project.id, status="completed", stage="done", progress=1, output_path=str(output), error=None)
    if callbacks is not None:
        callbacks.log("info", f"Готовое видео скачано с сервера: {output}")
        callbacks.progress(1, "done")


def _download(url: str, token: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            dest.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise


def _request(method: str, url: str, token: str, payload: dict | None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
    if not body:
        return {}
    loaded = json.loads(body.decode("utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DramaStill-Token"] = token
    return headers


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def preview_assignment(job_ids: list[str], workers: list[WorkerStatus]) -> dict[str, str]:
    return assign_jobs(job_ids, workers)
