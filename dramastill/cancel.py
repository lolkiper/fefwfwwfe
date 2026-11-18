from __future__ import annotations

import threading


class PipelinePaused(Exception):
    """Сборка остановлена и может быть продолжена."""


class PipelineCancelled(Exception):
    """Сборка остановлена пользователем и не продолжится сама."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.mode = "pause"

    def request(self, mode: str) -> None:
        self.mode = "cancel" if mode == "cancel" else "pause"
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if not self._event.is_set():
            return
        if self.mode == "cancel":
            raise PipelineCancelled("Сборка остановлена")
        raise PipelinePaused("Сборка поставлена на паузу")
