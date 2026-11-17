from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import httpx

from dramastill.providers.errors import ProviderError

T = TypeVar("T")

RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


def with_retries(action: Callable[[], T], *, attempts: int = 4, base_delay: float = 1.6) -> T:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return action()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2**attempt) + random.random() * 0.4)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == attempts - 1:
                raise ProviderError(
                    "Сеть недоступна или сервис не ответил вовремя",
                    retryable=True,
                ) from exc
            time.sleep(base_delay * (2**attempt) + random.random() * 0.4)
    assert last is not None
    raise last
