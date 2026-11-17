from __future__ import annotations

import httpx

from dramastill.core.redact import redact
from dramastill.providers.base import LLMResult
from dramastill.providers.errors import ProviderError
from dramastill.providers.retry import RETRY_STATUSES, with_retries


class OpenAICompatLLM:
    """Chat Completions для OpenAI, OpenRouter, Groq, Ollama и совместимых серверов."""

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

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult:
        def once(use_json: bool, token_field: str) -> LLMResult:
            payload: dict = {
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                token_field: max_tokens,
            }
            if use_json:
                payload["response_format"] = {"type": "json_object"}
            response = self._post("/chat/completions", payload)
            if response.status_code == 400 and use_json:
                body = response.text.lower()
                if "response_format" in body or "json" in body:
                    return once(False, token_field)
            if response.status_code == 400 and token_field == "max_tokens":
                body = response.text.lower()
                if "max_completion_tokens" in body or "max_tokens" in body:
                    return once(use_json, "max_completion_tokens")
            self._raise_for_status(response)
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                text = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            else:
                text = content or ""
            if not text.strip():
                raise ProviderError("Языковая модель вернула пустой ответ")
            return LLMResult(text=text, finish_reason=str(choice.get("finish_reason") or "stop"))

        return with_retries(lambda: once(json_mode, "max_tokens"))

    def _post(self, path: str, payload: dict) -> httpx.Response:
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
        raise ProviderError(
            redact(message, [self.api_key]),
            status=response.status_code,
            retryable=response.status_code in RETRY_STATUSES,
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
