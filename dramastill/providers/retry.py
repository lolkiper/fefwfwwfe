from __future__ import annotations

import random
import re
import time
from typing import Callable, TypeVar

import httpx

from dramastill.providers.errors import ProviderError

T = TypeVar("T")

RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


def parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    match = re.search(r"retry after\s+(\d+)", response.text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    if response.status_code in {402, 429} and "free limit" in response.text.lower():
        return 45.0
    return None


def soft_free_limit(response: httpx.Response) -> bool:
    text = response.text.lower()
    return response.status_code in {402, 429} and ("free limit" in text or "retry after" in text)


def daily_quota(response: httpx.Response) -> bool:
    text = response.text.lower()
    return response.status_code in {402, 429} and "quota" in text


def _wait_message(exc: ProviderError, delay: float) -> str:
    seconds = max(1, int(delay + 0.5))
    reason = " ".join(str(exc).split())
    if "free limit" in reason.lower():
        return f"Лимит бесплатной модели: пауза {seconds} с, запрос продолжится сам"
    if len(reason) > 160:
        reason = reason[:160].rstrip() + "…"
    if not reason:
        reason = "Сервис не ответил"
    return f"{reason} Пауза {seconds} с, запрос продолжится сам"


def sleep_until(seconds: float, stop_check: Callable[[], None] | None = None) -> None:
    if seconds <= 0:
        return
    if stop_check is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        stop_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def with_retries(
    action: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.6,
    on_wait: Callable[[str], None] | None = None,
    stop_check: Callable[[], None] | None = None,
) -> T:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return action()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == attempts - 1:
                raise
            if exc.retry_after is None:
                # Без срока от сервера пауза не растёт дальше нескольких секунд.
                delay = min(6.0, base_delay * (2**attempt)) + random.random() * 0.3
            else:
                delay = min(90.0, max(0.0, exc.retry_after))
            if on_wait is not None and (attempt == 0 or delay >= 3):
                on_wait(_wait_message(exc, delay))
            sleep_until(delay, stop_check)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == attempts - 1:
                raise ProviderError(
                    "Сеть недоступна или сервис не ответил вовремя",
                    retryable=True,
                ) from exc
            delay = min(6.0, base_delay * (2**attempt)) + random.random() * 0.3
            if on_wait is not None:
                seconds = max(1, int(delay + 0.5))
                on_wait(f"Сеть не ответила. Пауза {seconds} с, запрос продолжится сам")
            sleep_until(delay, stop_check)
    assert last is not None
    raise last
