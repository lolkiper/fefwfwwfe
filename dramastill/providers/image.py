from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from urllib.parse import quote

import httpx

from dramastill.config.defaults import FLUX_BASE
from dramastill.core.redact import redact
from dramastill.providers.errors import ProviderError
from dramastill.providers.llm import _error_message
from dramastill.providers.retry import RETRY_STATUSES, with_retries

_FLUX_LOCK = threading.Lock()
_IMAGE_NOTE = threading.local()


def take_image_note() -> str:
    message = str(getattr(_IMAGE_NOTE, "message", "") or "")
    _IMAGE_NOTE.message = ""
    return message


def _remember(message: str) -> None:
    _IMAGE_NOTE.message = message


_NEGATIVE = (
    "text, letters, watermark, logo, caption, collage, split screen, comic panels, "
    "extra fingers, deformed face, cartoon, anime, low resolution"
)


class FluxSchnellImageProvider:
    """Один кадр 16:9 через FLUX.1 Schnell. Ключ не нужен."""

    def __init__(self, *, base_url: str = FLUX_BASE, model: str = "flux-1-schnell", transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or FLUX_BASE).rstrip("/")
        self.model = model or "flux-1-schnell"
        self._transport = transport
        self._flux_blocked = False

    def generate(self, *, prompt: str, dest: Path, size: str, seed: int | None) -> None:
        width, height = _flux_size(size)
        text = _clip_prompt(prompt)
        chosen = 0 if seed is None or seed < 0 else int(seed) % 2147483647
        randomize = seed is None or seed < 0
        if self._flux_blocked:
            _from_pollinations(_safe_still_prompt(text), dest, width, height, self._transport)
            _remember("кадр взят с запасного адреса, FLUX недоступен")
            return
        try:
            with _FLUX_LOCK:
                self._draw(text, dest, width, height, chosen, randomize)
            return
        except ProviderError as exc:
            if not _flux_give_up(exc):
                raise
        try:
            with _FLUX_LOCK:
                self._draw(_generic_still(), dest, width, height, chosen, True)
            _remember("кадр без сюжета: фильтр FLUX отклонил описание главы")
            return
        except ProviderError:
            self._flux_blocked = True
        _from_pollinations(_safe_still_prompt(text), dest, width, height, self._transport)
        _remember("кадр взят с запасного адреса")

    def _draw(self, body: str, dest: Path, width: int, height: int, chosen: int, randomize: bool) -> None:
        def once() -> None:
            timeout = httpx.Timeout(180, connect=20)
            with httpx.Client(timeout=timeout, follow_redirects=True, transport=self._transport) as client:
                started = client.post(
                    f"{self.base_url}/gradio_api/call/infer",
                    json={"data": [body, chosen, randomize, width, height, 4]},
                )
                if started.status_code in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderError("FLUX временно занят, запрос повторяется", status=started.status_code, retryable=True)
                if started.status_code >= 400:
                    raise ProviderError(redact(started.text[:300], []), status=started.status_code, retryable=False)
                event_id = str((started.json() or {}).get("event_id") or "")
                if not event_id:
                    raise ProviderError("FLUX не принял запрос на изображение", retryable=True)
                result = client.get(f"{self.base_url}/gradio_api/call/infer/{event_id}")
                if result.status_code in {408, 409, 429, 500, 502, 503, 504}:
                    raise ProviderError("FLUX временно занят, запрос повторяется", status=result.status_code, retryable=True)
                if result.status_code >= 400:
                    raise ProviderError(redact(result.text[:300], []), status=result.status_code, retryable=False)
                file_url = _flux_file_url(result.text)
                image = client.get(file_url)
            if image.status_code >= 400 or not _looks_like_image(image.content):
                raise ProviderError("FLUX вернул не изображение", retryable=True)
            _write_png(image.content, dest)

        with_retries(once, attempts=2, base_delay=4)


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


def _flux_size(size: str) -> tuple[int, int]:
    width, height = _frame_size(size)
    width = min(1280, max(512, width - width % 16))
    height = min(768, max(288, height - height % 16))
    return width, height


def _clip_prompt(prompt: str) -> str:
    text = " ".join(prompt.split())
    if len(text) > 1400:
        text = text[:1400].rsplit(" ", 1)[0]
    return text


def _safe_still_prompt(prompt: str) -> str:
    """Короткий кадр без сюжета, из-за которого фильтр FLUX отвечает null."""
    head = prompt[:480].rsplit(" ", 1)[0] if len(prompt) > 480 else prompt
    return (
        f"{head}. Same people and wardrobe, a quiet tense moment in the same place. "
        "Everyone is clothed and uninjured. No blood, wounds, weapons, nudity, or written text."
    )


def _generic_still() -> str:
    return (
        "photorealistic cinematic 16:9 film still, two clothed adults in a dim room, "
        "serious faces, natural hands, everyone uninjured, no text, no blood, no weapons, no nudity"
    )


def _flux_give_up(exc: ProviderError) -> bool:
    text = str(exc).lower()
    return _filter_block(exc) or "не вернул" in text or "занят" in text or "скрыл" in text


def _from_pollinations(prompt: str, dest: Path, width: int, height: int, transport: httpx.BaseTransport | None) -> None:
    text = quote(prompt[:700], safe="")
    url = f"https://image.pollinations.ai/prompt/{text}"
    timeout = httpx.Timeout(90, connect=20)
    with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
        response = client.get(url, params={"width": width, "height": height, "nologo": "true", "model": "flux"})
    if response.status_code >= 400 or not _looks_like_image(response.content):
        raise ProviderError("Запасной адрес не вернул изображение", retryable=True)
    _write_png(response.content, dest)


def _filter_block(exc: ProviderError) -> bool:
    text = str(exc).lower()
    return "фильтром содержания" in text or "content filter" in text or "blocked" in text


def _write_png(content: bytes, dest: Path) -> None:
    if content.startswith(b"\x89PNG"):
        dest.write_bytes(content)
        return
    import io

    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        image.convert("RGB").save(dest, "PNG")


def _flux_file_url(body: str) -> str:
    event = ""
    complete = ""
    error = ""
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
            if event == "complete":
                complete = data
            elif event == "error":
                error = data
    if not complete or complete.lower() == "null":
        hidden = error.strip().lower() in {"", "null", "none"}
        if hidden or "content filter" in error.lower() or "blocked" in error.lower():
            raise ProviderError(
                "FLUX отклонил кадр фильтром содержания или скрыл ошибку очереди",
                retryable=False,
            )
        raise ProviderError(error or "FLUX не вернул изображение", retryable=True)
    try:
        payload = json.loads(complete)
    except json.JSONDecodeError as exc:
        raise ProviderError("FLUX вернул непонятный ответ", retryable=True) from exc
    item = payload[0] if isinstance(payload, list) and payload else payload
    url = item.get("url") if isinstance(item, dict) else ""
    if not url:
        raise ProviderError("FLUX не вернул файл изображения", retryable=True)
    return str(url)


def _frame_size(size: str) -> tuple[int, int]:
    try:
        width_text, height_text = size.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, ValueError):
        width, height = 1280, 720
    width = min(1920, max(512, width))
    height = min(1080, max(288, height))
    return width, height


def _looks_like_image(payload: bytes) -> bool:
    if len(payload) < 32:
        return False
    if payload.startswith(b"\xff\xd8") or payload.startswith(b"\x89PNG") or payload.startswith(b"GIF8"):
        return True
    if payload.startswith(b"RIFF") and b"WEBP" in payload[:16]:
        return True
    return False


def _decode_b64(value: str) -> bytes:
    payload = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    return base64.b64decode(payload)
