from __future__ import annotations

import threading
import time
from typing import Callable

import httpx

from dramastill.core.redact import redact
from dramastill.providers.base import LLMResult
from dramastill.providers.errors import ProviderError
from dramastill.providers.retry import (
    RETRY_STATUSES,
    daily_quota,
    parse_retry_after,
    sleep_until,
    soft_free_limit,
    with_retries,
)


class OpenAICompatLLM:
    """Chat Completions для OpenAI, OpenRouter, Groq, Ollama и совместимых серверов."""

    _pace_lock = threading.Lock()
    _next_allowed = 0.0

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._transport = transport
        self.on_wait: Callable[[str], None] | None = None
        self.stop_check: Callable[[], None] | None = None
        self.model_chain: tuple[str, ...] = ()

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult:
        last: ProviderError | None = None
        for round_index in range(2):
            try:
                return self._complete_chain(
                    system=system,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except ProviderError as exc:
                last = exc
                if round_index == 1 or not _quota_exhausted(exc):
                    raise
                delay = min(90.0, float(exc.retry_after or 60.0))
                self._note(f"Лимит бесплатной модели: пауза {max(1, int(delay + 0.5))} с, запрос продолжится сам")
                sleep_until(delay, self.stop_check)
        assert last is not None
        raise last

    def _complete_chain(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult:
        models: list[str] = []
        for name in (self.model, *self.model_chain):
            if name and name not in models:
                models.append(name)
        last: ProviderError | None = None
        for index, model in enumerate(models):
            self.model = model
            try:
                return self._complete_model(
                    system=system,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except ProviderError as exc:
                last = exc
                if not _quota_exhausted(exc) or index == len(models) - 1:
                    raise
                self._note("Дневной лимит этой модели исчерпан, запрос уходит на следующую бесплатную модель")
        assert last is not None
        raise last

    def _note(self, message: str) -> None:
        if self.on_wait is not None:
            self.on_wait(message)

    def _complete_model(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult:
        def attempt() -> LLMResult:
            tried: set[tuple] = set()

            def once(use_json: bool, token_field: str, reasoning: str | None, token_limit: int) -> LLMResult:
                state = (use_json, token_field, reasoning, token_limit)
                if state in tried:
                    raise ProviderError("Языковая модель повторила тот же неудачный запрос")
                tried.add(state)
                payload: dict = {
                    "model": self.model,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    token_field: token_limit,
                }
                if use_json:
                    payload["response_format"] = {"type": "json_object"}
                if reasoning:
                    payload["reasoning_effort"] = reasoning
                response = self._post("/chat/completions", payload)
                if response.status_code == 400 and _unsupported_reasoning(response):
                    if reasoning == "none":
                        return once(use_json, token_field, "low", token_limit)
                    return once(use_json, token_field, None, token_limit)
                if response.status_code == 400 and _output_budget(response):
                    self._note("Модель потратила лимит на рассуждения, повторяю запрос короче")
                    if reasoning != "low":
                        return once(use_json, token_field, "low", token_limit)
                    if token_limit < 32000:
                        return once(use_json, token_field, "low", min(32000, token_limit * 2))
                if response.status_code == 400 and use_json:
                    body = response.text.lower()
                    if "response_format" in body or "json" in body:
                        return once(False, token_field, reasoning, token_limit)
                if response.status_code == 400 and token_field == "max_tokens":
                    body = response.text.lower()
                    if "max_completion_tokens" in body or "max_tokens" in body:
                        return once(use_json, "max_completion_tokens", reasoning, token_limit)
                self._raise_for_status(response)
                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                else:
                    text = content or ""
                if not text.strip() and (message.get("reasoning") or message.get("reasoning_content")):
                    self._note("Модель потратила лимит на рассуждения, повторяю запрос короче")
                    if reasoning != "low":
                        return once(use_json, token_field, "low", token_limit)
                    if token_limit < 32000:
                        return once(use_json, token_field, "low", min(32000, token_limit * 2))
                if not text.strip():
                    raise ProviderError("Языковая модель вернула пустой ответ")
                self._mark_turn()
                return LLMResult(text=text, finish_reason=str(choice.get("finish_reason") or "stop"))

            initial_reasoning = "low" if self._short_reasoning() else None
            return once(json_mode, "max_tokens", initial_reasoning, max_tokens)

        return with_retries(
            attempt,
            attempts=8,
            on_wait=self.on_wait,
            stop_check=self.stop_check,
        )

    def _short_reasoning(self) -> bool:
        url = self.base_url.lower()
        model = self.model.lower()
        return (
            "pollinations.ai" in url
            or "generativelanguage.googleapis.com" in url
            or "gpt-oss" in model
            or model == "openai-fast"
        )

    def _wait_turn(self) -> None:
        if "pollinations.ai" not in self.base_url:
            return
        with type(self)._pace_lock:
            wait = type(self)._next_allowed - time.monotonic()
        if wait <= 0.3:
            return
        if self.on_wait is not None:
            self.on_wait(f"Лимит бесплатной модели: пауза {max(1, int(wait + 0.5))} с, запрос продолжится сам")
        sleep_until(wait, self.stop_check)

    def _mark_turn(self) -> None:
        if "pollinations.ai" not in self.base_url:
            return
        with type(self)._pace_lock:
            type(self)._next_allowed = time.monotonic() + 46

    def _post(self, path: str, payload: dict) -> httpx.Response:
        self._wait_turn()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://localhost/dramastill"
            headers["X-Title"] = "DramaStill"
        timeout = httpx.Timeout(300, connect=30)
        with httpx.Client(timeout=timeout, transport=self._transport) as client:
            return client.post(self.base_url + path, headers=headers, json=payload)

    def list_model_ids(self) -> list[str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=httpx.Timeout(60, connect=20), transport=self._transport) as client:
            response = client.get(self.base_url + "/models", headers=headers)
        self._raise_for_status(response)
        data = response.json()
        rows = data.get("data", data if isinstance(data, list) else [])
        ids = []
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                ids.append(str(row["id"]))
        return ids

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = _error_message(response)
        if daily_quota(response):
            raise ProviderError(
                redact(message, [self.api_key]),
                status=response.status_code,
                retryable=False,
                retry_after=parse_retry_after(response),
            )
        if soft_free_limit(response):
            delay = parse_retry_after(response) or 46
            if "pollinations.ai" in self.base_url:
                with type(self)._pace_lock:
                    type(self)._next_allowed = time.monotonic() + delay
                retry_after = 0
            else:
                retry_after = delay
            raise ProviderError(
                redact(message, [self.api_key]),
                status=response.status_code,
                retryable=True,
                retry_after=retry_after,
            )
        raise ProviderError(
            redact(message, [self.api_key]),
            status=response.status_code,
            retryable=response.status_code in RETRY_STATUSES,
            retry_after=parse_retry_after(response),
        )


class AnthropicLLM:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult:
        def once() -> LLMResult:
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            with httpx.Client(timeout=httpx.Timeout(300, connect=30), transport=self._transport) as client:
                response = client.post(self.base_url + "/v1/messages", headers=headers, json=payload)
            if response.status_code >= 400:
                raise ProviderError(
                    redact(_error_message(response), [self.api_key]),
                    status=response.status_code,
                    retryable=response.status_code in RETRY_STATUSES,
                )
            data = response.json()
            parts = data.get("content") or []
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            if not text.strip():
                raise ProviderError("Anthropic вернул пустой ответ")
            return LLMResult(text=text, finish_reason=str(data.get("stop_reason") or "end_turn"))

        return with_retries(once)

    def list_model_ids(self) -> list[str]:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        with httpx.Client(timeout=httpx.Timeout(60, connect=20), transport=self._transport) as client:
            response = client.get(self.base_url + "/v1/models", headers=headers)
        if response.status_code >= 400:
            raise ProviderError(
                redact(_error_message(response), [self.api_key]),
                status=response.status_code,
                retryable=response.status_code in RETRY_STATUSES,
            )
        data = response.json()
        return [str(row.get("id")) for row in data.get("data", []) if row.get("id")]


def _unsupported_reasoning(response: httpx.Response) -> bool:
    text = response.text.lower()
    return "reasoning" in text and "not supported" in text


def _output_budget(response: httpx.Response) -> bool:
    text = response.text.lower()
    return "output token" in text or "exhausted its output" in text


def _quota_exhausted(exc: ProviderError) -> bool:
    text = str(exc).lower()
    return exc.status in {402, 429} and "quota" in text


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"Ошибка API ({response.status_code}): {error['message']}"
        if isinstance(error, str):
            return f"Ошибка API ({response.status_code}): {error}"
        if payload.get("message"):
            return f"Ошибка API ({response.status_code}): {payload['message']}"
    text = response.text.strip().replace("\n", " ")
    if len(text) > 500:
        text = text[:500] + "…"
    if response.status_code in {401, 403}:
        return f"Ключ отклонён ({response.status_code}). Проверьте API-ключ. {text}"
    return f"Ошибка API ({response.status_code}): {text or 'пустой ответ'}"
