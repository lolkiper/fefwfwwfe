from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread, Signal

from dramastill.cancel import CancelToken, PipelineCancelled, PipelinePaused
from dramastill.core.database import Database
from dramastill.core.redact import redact
from dramastill.core.secrets import SecretStore
from dramastill.media.ffmpeg_util import stop_active_ffmpeg
from dramastill.media.resources import cooling_decision, memory_allows
from dramastill.pipeline.runner import Callbacks, run_project
from dramastill.remote.client import (
    begin_local,
    end_local,
    hold_active,
    remote_configured,
    run_on_worker,
    worker_count,
)


class CallWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, action, secrets: list[str]):
        super().__init__()
        self._action = action
        self._secrets = secrets

    def run(self) -> None:
        try:
            self.succeeded.emit(str(self._action()))
        except Exception as exc:
            self.failed.emit(redact(str(exc), self._secrets))


class Scheduler(QThread):
    log_line = Signal(str, str, str)
    progress = Signal(str, float, str)
    changed = Signal()

    def __init__(self, db: Database, secrets: SecretStore):
        super().__init__()
        self.db = db
        self.secrets = secrets
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[str, tuple[CancelToken, threading.Thread]] = {}

    def wake(self) -> None:
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        with self._lock:
            tokens = [item[0] for item in self._active.values()]
        for token in tokens:
            token.request("pause")
        self._wake.set()
        self.wait(1500)
        stop_active_ffmpeg()

    def pause(self, project_id: str) -> None:
        self._request(project_id, "pause", "paused")

    def cancel(self, project_id: str) -> None:
        self._request(project_id, "cancel", "cancelled")

    def resume(self, project_id: str) -> None:
        project = self.db.get(project_id)
        if project is None or project.status not in {"paused", "failed", "cancelled"}:
            return
        self.db.update(project_id, status="queued", error=None)
        self.changed.emit()
        self.wake()

    def run(self) -> None:
        while not self._stop.is_set():
            settings = self.db.load_settings()
            decision = cooling_decision(settings.video)
            remote = remote_configured(settings.video)
            if remote and hold_active():
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            limit = decision.projects
            if remote:
                limit = max(limit, worker_count(settings.video))
            with self._lock:
                running = len(self._active)
            local_ok = decision.projects > running and memory_allows(settings.video)
            if running >= limit or (not local_ok and not remote):
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            project = self.db.claim_next_queued()
            if project is not None:
                self._start(project.id)
                continue
            self._wake.wait(0.4)
            self._wake.clear()

    def _start(self, project_id: str) -> None:
        token = CancelToken()
        thread = threading.Thread(target=self._execute, args=(project_id, token), name=f"drama-{project_id}", daemon=True)
        with self._lock:
            self._active[project_id] = (token, thread)
        thread.start()
        self.changed.emit()

    def _execute(self, project_id: str, token: CancelToken) -> None:
        callbacks = Callbacks(
            log=lambda level, message: self.log_line.emit(project_id, level, message),
            progress=lambda fraction, stage: self.progress.emit(project_id, fraction, stage),
            title=lambda _title: self.changed.emit(),
        )
        try:
            project = self.db.get(project_id)
            if project is not None and remote_configured(project.settings.video):
                outcome = run_on_worker(self.db, project_id, self.secrets, token, callbacks)
                if outcome == "done":
                    return
                if outcome == "wait":
                    self.db.update(project_id, status="queued")
                    return
            project = self.db.get(project_id)
            if project is None:
                return
            decision = cooling_decision(project.settings.video)
            if not begin_local(decision.projects) or not memory_allows(project.settings.video):
                self.db.update(project_id, status="queued")
                time.sleep(2)
                return
            try:
                run_project(self.db, project_id, self.secrets, token, callbacks=callbacks)
            finally:
                end_local()
        except (PipelinePaused, PipelineCancelled):
            pass
        except Exception:
            pass
        finally:
            with self._lock:
                self._active.pop(project_id, None)
            self.changed.emit()
            self._wake.set()

    def _request(self, project_id: str, mode: str, idle_status: str) -> None:
        with self._lock:
            active = self._active.get(project_id)
        if active is not None:
            active[0].request(mode)
            return
        project = self.db.get(project_id)
        if project is None:
            return
        if project.status in {"queued", "paused", "running"}:
            error = "Остановлено пользователем" if mode == "cancel" else project.error
            self.db.update(project_id, status=idle_status, error=error)
            self.changed.emit()
