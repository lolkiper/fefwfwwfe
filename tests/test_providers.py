import base64
import io
import json

import httpx
import pytest
from PIL import Image

from dramastill.config.defaults import AppSettings
from dramastill.core.secrets import SecretStore
from dramastill.providers.errors import ProviderError
from dramastill.providers.factory import build_llm, build_providers, describe_route
from dramastill.providers.image import FluxSchnellImageProvider, OpenAIImageProvider, StabilityImageProvider
from dramastill.providers.llm import AnthropicLLM, OpenAICompatLLM
from dramastill.providers.tts import edge_prosody


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (20, 30, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def test_openai_chat_and_redacted_error():
    secret = "sk-testsecretvalue123456"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            assert request.headers["authorization"] == f"Bearer {secret}"
            return httpx.Response(401, json={"error": {"message": f"rejected {secret}"}})
        return httpx.Response(404)

    provider = OpenAICompatLLM(
        base_url="https://example.test/v1",
        api_key=secret,
        model="gpt-4o-mini",
        transport=httpx.MockTransport(handler),
    )
    try:
        provider.complete(system="s", user="u", temperature=0.2, max_tokens=100, json_mode=True)
    except ProviderError as exc:
        assert secret not in str(exc)
        assert exc.status == 401
    else:
        raise AssertionError("expected error")


def test_openai_retries_then_reads_json():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"message": "busy"}})
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_object"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"ready\": true}"}, "finish_reason": "stop"}]},
        )

    provider = OpenAICompatLLM(
        base_url="https://example.test/v1",
        api_key="sk-test",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    result = provider.complete(system="s", user="u", temperature=0.4, max_tokens=200, json_mode=True)
    assert "ready" in result.text
    assert calls["n"] == 2


def test_anthropic_reads_text_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "anthropic-key"
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "{\"a\": 1}"}], "stop_reason": "end_turn"},
        )

    provider = AnthropicLLM(api_key="anthropic-key", model="claude", transport=httpx.MockTransport(handler))
    result = provider.complete(system="s", user="u", temperature=0.2, max_tokens=100, json_mode=True)
    assert result.text == '{"a": 1}'


def test_image_providers_write_bytes(tmp_path):
    payload = _png()

    def openai_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(payload).decode()}]})

    dest = tmp_path / "one.png"
    OpenAIImageProvider(
        base_url="https://example.test/v1",
        api_key="sk-image",
        model="dall-e-3",
        transport=httpx.MockTransport(openai_handler),
    ).generate(prompt="one still", dest=dest, size="1792x1024", seed=None)
    assert dest.read_bytes() == payload

    def stability_handler(request: httpx.Request) -> httpx.Response:
        assert b"16:9" in request.content
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    other = tmp_path / "stable.png"
    StabilityImageProvider(api_key="stability-key", model="core", transport=httpx.MockTransport(stability_handler)).generate(
        prompt="one still",
        dest=other,
        size="1792x1024",
        seed=7,
    )
    assert other.read_bytes() == payload


def test_edge_tts_speaks_a_real_phrase(tmp_path):
    from dramastill.providers.tts import EdgeTTS

    dest = tmp_path / "phrase.mp3"
    try:
        EdgeTTS().synthesize(
            text="Это короткая проверка голоса.",
            dest=dest,
            voice="ru-RU-SvetlanaNeural",
            speed=1.0,
            emotion="calm",
            language="ru",
        )
    except Exception as exc:
        pytest.skip(f"Edge TTS недоступен: {exc}")
    assert dest.stat().st_size > 1000


def test_pollinations_waits_out_the_free_limit(monkeypatch):
    waits = []
    notes = []
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda seconds: waits.append(seconds))
    OpenAICompatLLM._next_allowed = 0.0
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                402,
                json={"error": {"message": "Free limit reached: 1 reply every 45 seconds per IP. Retry after 27s"}},
                headers={"Retry-After": "27"},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}]},
        )

    provider = OpenAICompatLLM(
        base_url="https://text.pollinations.ai/openai",
        api_key="",
        model="openai-fast",
        transport=httpx.MockTransport(handler),
    )
    provider.on_wait = notes.append
    result = provider.complete(system="s", user="u", temperature=0.2, max_tokens=100, json_mode=True)
    assert "ok" in result.text
    assert calls["n"] == 2
    assert waits and waits[0] >= 26
    assert notes and "пауза" in notes[0]
    OpenAICompatLLM._next_allowed = 0.0


def test_flux_retries_then_saves_one_image(tmp_path, monkeypatch):
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda *_args: None)
    payload = _png()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.method == "POST":
            if calls["n"] == 1:
                return httpx.Response(503, json={"error": "busy"})
            body = json.loads(request.content.decode())
            assert body["data"][3] == 1280
            assert body["data"][4] == 720
            return httpx.Response(200, json={"event_id": "evt"})
        if str(request.url).endswith("/evt"):
            data = json.dumps([{"url": "https://flux.test/still.png"}])
            return httpx.Response(200, text=f"event: complete\ndata: {data}\n")
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    dest = tmp_path / "still.png"
    FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler)).generate(
        prompt="one cinematic still",
        dest=dest,
        size="1280x720",
        seed=3,
    )
    assert calls["n"] == 4
    assert dest.read_bytes() == payload


def test_saved_pollinations_settings_move_off_that_service():
    from dramastill.config.defaults import app_settings_from_dict

    settings = app_settings_from_dict(
        {
            "llm": {"provider": "pollinations", "base_url": "https://text.pollinations.ai/openai", "model": "openai-fast"},
            "image": {"provider": "pollinations", "base_url": "https://image.pollinations.ai", "model": "sana"},
        }
    )
    assert settings.llm.provider == "llm7"
    assert "llm7.io" in settings.llm.base_url
    assert settings.image.provider == "flux"
    assert "pollinations" not in settings.image.base_url


def test_gemini_without_key_uses_llm7(tmp_path):
    secrets = SecretStore(tmp_path / "secrets")
    settings = AppSettings()
    provider = build_llm(settings, secrets)
    assert provider.model == "DeepSeek-V4-Flash-0731"
    assert "llm7.io" in provider.base_url
    route = describe_route(settings, secrets)
    assert "DeepSeek" in route
    assert "Pollinations" not in route
    assert "FLUX" in route
    secrets.set("llm", "AIza-free-gemini-key")
    paid_route = build_llm(settings, secrets)
    assert paid_route.model == "gemini-3.8-flash"
    assert paid_route.api_key == "AIza-free-gemini-key"


def test_factory_uses_edge_without_key(tmp_path):
    settings = AppSettings()
    settings.voice.provider = "edge"
    settings.llm.provider = "ollama"
    settings.llm.base_url = "http://127.0.0.1:11434/v1"
    settings.image.provider = "openai_compatible"
    settings.image.base_url = "http://127.0.0.1:9/v1"
    providers = build_providers(settings, SecretStore(tmp_path / "secrets"))
    assert providers.tts.name == "edge"
    rate, pitch = edge_prosody(1.0, "sad")
    assert rate.startswith("-")
    assert pitch.startswith("-")
