from __future__ import annotations

import base64
from pathlib import Path

import httpx

from dramastill.core.redact import redact
from dramastill.providers.errors import ProviderError
from dramastill.providers.llm import _error_message
from dramastill.providers.retry import RETRY_STATUSES, with_retries

_NEGATIVE = (
    "text, letters, watermark, logo, caption, collage, split screen, comic panels, "
    "extra fingers, deformed face, cartoon, anime, low resolution"
)


class OpenAIImageProvider:
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

    def generate(self, *, prompt: str, dest: Path, size: str, seed: int | None) -> None:
        def once(include_format: bool) -> None:
            payload: dict = {"model": self.model, "prompt": prompt, "size": size, "n": 1}
            if include_format:
                payload["response_format"] = "b64_json"
            response = self._post("/images/generations", payload)
            if response.status_code == 400 and include_format and "response_format" in response.text.lower():
                return once(False)
            if response.status_code >= 400:
                raise ProviderError(
                    redact(_error_message(response), [self.api_key]),
                    status=response.status_code,
                    retryable=response.status_code in RETRY_STATUSES,
                )
            data = response.json()
            rows = data.get("data") or []
            if not rows:
                raise ProviderError("Сервис изображений не вернул файл")
            row = rows[0]
            if row.get("b64_json"):
                dest.write_bytes(_decode_b64(row["b64_json"]))
                return
            if row.get("url"):
                dest.write_bytes(self._download(row["url"]))
                return
            raise ProviderError("В ответе нет изображения")

        with_retries(lambda: once(True))

    def _post(self, path: str, payload: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=httpx.Timeout(240, connect=30), transport=self._transport) as client:
            return client.post(self.base_url + path, headers=headers, json=payload)

    def _download(self, url: str) -> bytes:
        with httpx.Client(timeout=httpx.Timeout(180, connect=30), transport=self._transport) as client:
            response = client.get(url)
        if response.status_code >= 400:
            raise ProviderError("Не удалось скачать сгенерированное изображение", status=response.status_code)
        return response.content


class StabilityImageProvider:
    def __init__(self, *, api_key: str, model: str = "core", transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.model = model or "core"
        self._transport = transport

    def generate(self, *, prompt: str, dest: Path, size: str, seed: int | None) -> None:
        model = self.model.strip("/") or "core"
        if model in {"stable-image-core", "stable-core"}:
            model = "core"
        url = f"https://api.stability.ai/v2beta/stable-image/generate/{model}"

        def once(with_negative: bool) -> None:
            fields = {
                "prompt": (None, prompt),
                "aspect_ratio": (None, "16:9"),
                "output_format": (None, "png"),
            }
            if with_negative:
                fields["negative_prompt"] = (None, _NEGATIVE)
            if seed is not None and seed >= 0:
                fields["seed"] = (None, str(seed))
            headers = {"authorization": f"Bearer {self.api_key}", "accept": "image/*"}
            with httpx.Client(timeout=httpx.Timeout(240, connect=30), transport=self._transport) as client:
                response = client.post(url, headers=headers, files=fields)
            if response.status_code == 400 and with_negative and "negative" in response.text.lower():
                return once(False)
            if response.status_code >= 400:
                raise ProviderError(
                    redact(_error_message(response), [self.api_key]),
                    status=response.status_code,
                    retryable=response.status_code in RETRY_STATUSES,
                )
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                raise ProviderError("Stability вернул JSON вместо изображения")
            if not response.content:
                raise ProviderError("Stability вернул пустой файл")
            dest.write_bytes(response.content)

        with_retries(lambda: once(True))


def _decode_b64(value: str) -> bytes:
    payload = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    return base64.b64decode(payload)
