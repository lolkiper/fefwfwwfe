"""A worker keeps rendering after the desktop app disconnects.

The user's computer only submits a job and later downloads the finished MP4.
Several workers can run at once. Each worker refuses a job when it is full or hot.
"""

from __future__ import annotations

import hmac
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from dramastill.cancel import CancelToken, PipelineCancelled
from dramastill.config.defaults import app_settings_from_dict
from dramastill.core.database import Database
from dramastill.core.secrets import SecretStore
from dramastill.media.cooling import read_machine
from dramastill.pipeline.runner import run_project

Executor = Callable[[dict, Path], None]


_STATUS_LOCK = threading.Lock()
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def pipeline_executor(payload: dict, job_dir: Path) -> None:
    settings = app_settings_from_dict(payload.get("settings") or {})
    settings.video.worker_urls = ""
    secrets = SecretStore(job_dir / "secrets")
    incoming = payload.get("secrets") or {}
    if isinstance(incoming, dict):
        secrets.set_many({str(key): str(value) for key, value in incoming.items() if value})
    database = Database(job_dir / "worker.sqlite3")
    project = database.create_project(settings, str(payload.get("title") or "DramaStill"), job_dir / "work")
    token = CancelToken()
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(0.3):
            if _read_status(job_dir).get("cancel_requested"):
                token.request("cancel")
                return

    watcher = threading.Thread(target=watch, name="dramastill-cancel", daemon=True)
    watcher.start()
    try:
        result = run_project(database, project.id, secrets, token)
    finally:
        stop.set()
    shutil.copy2(result, job_dir / "output.mp4")
    for suffix in (".jpg", ".youtube.json"):
        source = result.with_suffix(suffix)
        if source.exists():
            shutil.copy2(source, job_dir / f"sidecar{suffix}")


def start_worker(
    directory: Path,
    *,
    token: str = "",
    host: str = "127.0.0.1",
    port: int = 0,
    capacity: int = 1,
    executor: Executor | None = None,
) -> ThreadingHTTPServer:
    directory.mkdir(parents=True, exist_ok=True)
    state = _State(directory, token, max(1, capacity), executor or pipeline_executor)
    server = ThreadingHTTPServer((host, port), _handler(state))
    thread = threading.Thread(target=server.serve_forever, name="dramastill-worker", daemon=True)
    thread.start()
    return server


class _State:
    def __init__(self, directory: Path, token: str, capacity: int, executor: Executor):
        self.directory = directory
        self.token = token
        self.capacity = capacity
        self.executor = executor
        self.lock = threading.RLock()
        self.busy = 0

    def health(self) -> dict:
        reading = read_machine()
        temps = [item for item in (reading.cpu_temp_c, reading.gpu_temp_c) if item is not None]
        temp = max(temps) if temps else None
        with self.lock:
            busy = self.busy
        accept = busy < self.capacity and (temp is None or temp < 85)
        return {
            "ok": True,
            "busy": busy,
            "capacity": self.capacity,
            "cpu_percent": reading.cpu_percent,
            "temp_c": temp,
            "accept": accept,
        }


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            if not self._allowed():
                self._json(401, {"error": "Нужен токен сервера"})
                return
            path = urlparse(self.path).path
            if path == "/v1/health":
                self._json(200, state.health())
                return
            job_id, leaf = _job_path(path)
            if job_id is None:
                self._json(404, {"error": "Не найдено"})
                return
            job_dir = state.directory / job_id
            if leaf == "":
                self._json(200, _read_status(job_dir))
                return
            target = job_dir / leaf
            if not target.exists() or not target.is_file():
                self._json(404, {"error": "Файл ещё не готов"})
                return
            self._send_file(target)

        def do_POST(self) -> None:
            if not self._allowed():
                self._json(401, {"error": "Нужен токен сервера"})
                return
            path = urlparse(self.path).path
            if path == "/v1/jobs":
                self._submit()
                return
            job_id, leaf = _job_path(path)
            if job_id and leaf == "cancel":
                _write_status(state.directory / job_id, {"cancel_requested": True})
                self._json(202, {"id": job_id, "status": "cancel_requested"})
                return
            self._json(404, {"error": "Не найдено"})

        def _submit(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 8_000_000:
                self._json(400, {"error": "Пустое или слишком большое задание"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._json(400, {"error": "Задание не разбирается"})
                return
            job_id = str(payload.get("id") or "").strip()
            if not _JOB_ID.fullmatch(job_id):
                self._json(400, {"error": "У задания нет идентификатора"})
                return
            health = state.health()
            if health["temp_c"] is not None and health["temp_c"] >= 85:
                self._json(429, {"error": "Сервер перегрет", "health": health})
                return
            job_dir = state.directory / job_id
            with state.lock:
                existing = _read_status(job_dir)
                if existing.get("status") in {"running", "completed", "queued"}:
                    self._json(202, {"id": job_id, "status": existing.get("status"), "continues_without_client": True})
                    return
                if state.busy >= state.capacity:
                    self._json(429, {"error": "Сервер занят", "health": health})
                    return
                state.busy += 1
                job_dir.mkdir(parents=True, exist_ok=True)
                (job_dir / "request.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                _write_status(job_dir, {"status": "queued", "progress": 0, "stage": "queued", "error": ""})
            thread = threading.Thread(target=_run_job, args=(state, job_dir, payload), name=f"job-{job_id}", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                with state.lock:
                    state.busy = max(0, state.busy - 1)
                self._json(500, {"error": "Сервер не смог запустить задание"})
                return
            self._json(202, {"id": job_id, "status": "queued", "continues_without_client": True})

        def _allowed(self) -> bool:
            supplied = self.headers.get("X-DramaStill-Token", "")
            if state.token:
                return hmac.compare_digest(supplied, state.token)
            return self.client_address[0] in {"127.0.0.1", "::1"}

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, target: Path) -> None:
            length = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with target.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    return Handler


def _run_job(state: _State, job_dir: Path, payload: dict) -> None:
    _write_status(job_dir, {"status": "running", "progress": 0.01, "stage": "running", "error": ""})
    try:
        state.executor(payload, job_dir)
    except PipelineCancelled:
        _write_status(job_dir, {"status": "cancelled", "error": "Остановлено пользователем", "stage": "cancelled"})
    except Exception as exc:
        _write_status(job_dir, {"status": "failed", "error": str(exc), "stage": "failed"})
    else:
        current = _read_status(job_dir)
        if current.get("cancel_requested"):
            _write_status(job_dir, {"status": "cancelled", "error": "Остановлено пользователем", "stage": "cancelled"})
        elif current.get("status") != "failed":
            _write_status(job_dir, {"status": "completed", "progress": 1, "stage": "done", "error": ""})
    finally:
        with state.lock:
            state.busy = max(0, state.busy - 1)


def _job_path(path: str) -> tuple[str | None, str]:
    parts = [item for item in path.split("/") if item]
    if len(parts) < 3 or parts[0] != "v1" or parts[1] != "jobs":
        return None, ""
    job_id = parts[2]
    leaf = parts[3] if len(parts) > 3 else ""
    allowed = {"", "output.mp4", "sidecar.jpg", "sidecar.youtube.json", "cancel"}
    if leaf not in allowed or not _JOB_ID.fullmatch(job_id):
        return None, ""
    return job_id, leaf


def _read_status(job_dir: Path) -> dict:
    path = job_dir / "status.json"
    if not path.exists():
        return {"status": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "missing"}
    return data if isinstance(data, dict) else {"status": "missing"}


def _write_status(job_dir: Path, fields: dict) -> None:
    with _STATUS_LOCK:
        job_dir.mkdir(parents=True, exist_ok=True)
        current = _read_status(job_dir)
        if current.get("status") == "missing":
            current = {}
        current.update(fields)
        temporary = job_dir / "status.json.tmp"
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        temporary.replace(job_dir / "status.json")
