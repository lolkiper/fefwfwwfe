from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable


def normalize_proxy_line(line: str) -> str | None:
    """Преобразует строку прокси в URL для requests."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        return raw
    if "@" in raw:
        return raw if "://" in raw else f"http://{raw}"
    parts = raw.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 3:
        return f"http://{raw}"
    return f"http://{raw}"


def parse_proxy_list(text: str) -> list[str]:
    proxies: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        url = normalize_proxy_line(line)
        if url and url not in seen:
            seen.add(url)
            proxies.append(url)
    return proxies


def mask_proxy(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"(://)([^:@/]+):([^@/]+)@", r"\1***:***@", url)


@dataclass
class ProxyStatus:
    enabled: bool
    current: str | None
    current_masked: str | None
    index: int
    total: int
    interval_sec: float
    seconds_until_rotation: float

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "current": self.current_masked,
            "index": self.index,
            "total": self.total,
            "interval_sec": self.interval_sec,
            "seconds_until_rotation": round(self.seconds_until_rotation, 1),
        }


class ProxyRotator:
    """Циклически меняет прокси каждые `interval_sec` секунд (по умолчанию 60)."""

    def __init__(
        self,
        proxies: list[str],
        interval_sec: float = 60.0,
        on_rotate: Callable[[str | None], None] | None = None,
    ) -> None:
        self._proxies = list(proxies)
        self._interval = interval_sec
        self._on_rotate = on_rotate
        self._index = 0
        self._lock = threading.Lock()
        self._rotated_at = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def current(self) -> str | None:
        with self._lock:
            if not self._proxies:
                return None
            return self._proxies[self._index]

    def status(self) -> ProxyStatus:
        with self._lock:
            current = self._proxies[self._index] if self._proxies else None
            elapsed = time.monotonic() - self._rotated_at
            remaining = max(0.0, self._interval - elapsed) if self._proxies else 0.0
            return ProxyStatus(
                enabled=bool(self._proxies),
                current=current,
                current_masked=mask_proxy(current),
                index=self._index,
                total=len(self._proxies),
                interval_sec=self._interval,
                seconds_until_rotation=remaining,
            )

    def start(self) -> None:
        if not self._proxies or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="proxy-rotator", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                if not self._proxies:
                    continue
                self._index = (self._index + 1) % len(self._proxies)
                self._rotated_at = time.monotonic()
                proxy = self._proxies[self._index]
            if self._on_rotate:
                on_rotate(proxy)

    def stop(self) -> None:
        self._stop.set()