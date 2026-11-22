import base64
import io
import json
import time

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


@pytest.fixture(autouse=True)
def _reset_backup_image_gate():
    import dramastill.providers.image as image

    image._backup_not_before = 0.0
    image.bind_image_stop(None)
    yield
    image._backup_not_before = 0.0
    image.bind_image_stop(None)


def time_in_future() -> float:
    return time.monotonic() + 30


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


def test_pollinations_asks_for_short_reasoning_and_retries_a_spent_budget(monkeypatch):
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda *_args: None)
    OpenAICompatLLM._next_allowed = 0.0
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "The model exhausted its output token limit before producing an answer. Increase the token limit or reduce reasoning effort."
                    }
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}]},
        )

    notes = []
    provider = OpenAICompatLLM(
        base_url="https://text.pollinations.ai/openai",
        api_key="",
        model="openai-fast",
        transport=httpx.MockTransport(handler),
    )
    provider.on_wait = notes.append
    result = provider.complete(system="s", user="u", temperature=0.2, max_tokens=1000, json_mode=True)
    assert "ok" in result.text
    assert bodies[0]["reasoning_effort"] == "low"
    assert bodies[0]["max_tokens"] == 1000
    assert bodies[1]["reasoning_effort"] == "low"
    assert bodies[1]["max_tokens"] == 2000
    assert any("рассуждения" in note for note in notes)
    OpenAICompatLLM._next_allowed = 0.0


def test_daily_quota_switches_to_the_next_free_model(monkeypatch):
    waits = []
    notes = []
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda seconds: waits.append(seconds))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode())["model"]
        seen.append(model)
        if model == "DeepSeek-V4-Flash-0731":
            return httpx.Response(
                429,
                json={"error": {"message": "Daily token quota exceeded. Retry after 58 seconds."}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}]},
        )

    provider = OpenAICompatLLM(
        base_url="https://api.llm7.io/v1",
        api_key="",
        model="DeepSeek-V4-Flash-0731",
        transport=httpx.MockTransport(handler),
    )
    provider.model_chain = ("gemma4:31b",)
    provider.on_wait = notes.append
    result = provider.complete(system="s", user="u", temperature=0.2, max_tokens=100, json_mode=True)
    assert "ok" in result.text
    assert seen == ["DeepSeek-V4-Flash-0731", "gemma4:31b"]
    assert provider.model == "gemma4:31b"
    assert waits == []
    assert any("следующую" in note for note in notes)


def test_llm7_rate_limit_waits_the_requested_pause(monkeypatch):
    waits = []
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda seconds: waits.append(seconds))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "Busy. Retry after 12 seconds."}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    provider = OpenAICompatLLM(
        base_url="https://api.llm7.io/v1",
        api_key="",
        model="DeepSeek-V4-Flash-0731",
        transport=httpx.MockTransport(handler),
    )
    result = provider.complete(system="s", user="u", temperature=0.2, max_tokens=20, json_mode=False)
    assert result.text == "ok"
    assert calls["n"] == 2
    assert waits and waits[0] >= 12


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


def test_flux_null_error_is_a_filter_block_and_the_safe_prompt_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda *_args: None)
    payload = _png()
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content.decode())
            prompts.append(body["data"][0])
            return httpx.Response(200, json={"event_id": f"evt{len(prompts)}"})
        if str(request.url).endswith("/evt1"):
            return httpx.Response(200, text="event: error\ndata: null\n\n")
        if str(request.url).endswith("/evt2"):
            data = json.dumps([{"url": "https://flux.test/still.png"}])
            return httpx.Response(200, text=f"event: complete\ndata: {data}\n")
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    dest = tmp_path / "still.png"
    prefix = "photorealistic cinematic film still of one room and two clothed people. "
    FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler)).generate(
        prompt=prefix * 12 + "A man is shot. Blood and a gun fill the kitchen.",
        dest=dest,
        size="1280x720",
        seed=3,
    )
    assert len(prompts) == 2
    assert "uninjured" in prompts[1]
    assert "Blood" not in prompts[1]
    assert dest.read_bytes() == payload


def test_flux_uses_pollinations_after_the_space_hides_every_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("dramastill.providers.retry.time.sleep", lambda *_args: None)
    payload = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        if "pollinations.ai" in str(request.url):
            return httpx.Response(200, content=payload, headers={"content-type": "image/png"})
        if request.method == "POST":
            return httpx.Response(200, json={"event_id": "evt"})
        return httpx.Response(200, text="event: error\ndata: null\n\n")

    dest = tmp_path / "still.png"
    FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler)).generate(
        prompt="A dark family argument in a kitchen",
        dest=dest,
        size="1280x720",
        seed=-1,
    )
    assert dest.read_bytes() == payload


def test_backup_image_waits_out_the_free_limit(tmp_path, monkeypatch):
    import dramastill.providers.image as image

    image._backup_not_before = 0.0
    waits: list[float] = []
    monkeypatch.setattr(image.time, "sleep", lambda seconds: waits.append(seconds))
    payload = _png()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "pollinations.ai" in str(request.url):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(402, headers={"retry-after": "22"}, json={"error": {"code": "FREE_LIMIT"}})
            return httpx.Response(200, content=payload, headers={"content-type": "image/png"})
        if request.method == "POST":
            return httpx.Response(200, json={"event_id": "evt"})
        return httpx.Response(200, text="event: error\ndata: null\n\n")

    dest = tmp_path / "still.png"
    try:
        FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler)).generate(
            prompt="A quiet room",
            dest=dest,
            size="1280x720",
            seed=2,
        )
    finally:
        image._backup_not_before = 0.0
    assert dest.read_bytes() == payload
    assert sum(waits) >= 20


def test_backup_images_use_a_different_seed_for_each_chapter(tmp_path, monkeypatch):
    import dramastill.providers.image as image

    monkeypatch.setattr(image.time, "sleep", lambda *_args: None)
    payload = _png()
    seeds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "pollinations.ai" in str(request.url):
            seeds.append(str(request.url.params.get("seed")))
            return httpx.Response(200, content=payload, headers={"content-type": "image/png"})
        if request.method == "POST":
            return httpx.Response(200, json={"event_id": "evt"})
        return httpx.Response(200, text="event: error\ndata: null\n\n")

    provider = FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler))
    for prompt in ("A quiet kitchen at night, chapter one", "A stairwell at dawn, chapter two"):
        provider.generate(prompt=prompt, dest=tmp_path / f"{len(seeds)}.png", size="1280x720", seed=-1)
    assert len(seeds) == 2
    assert seeds[0] != seeds[1]
    assert seeds[0] not in {"", "0", "None"}


def test_local_stills_differ_when_the_seed_differs(tmp_path):
    from dramastill.providers.image import _local_still

    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    _local_still(first, 640, 360, 1)
    _local_still(second, 640, 360, 2)
    assert first.stat().st_size > 1000
    assert first.read_bytes() != second.read_bytes()


def test_backup_wait_stops_when_the_user_cancels(tmp_path, monkeypatch):
    import dramastill.providers.image as image
    from dramastill.cancel import PipelineCancelled

    image._backup_not_before = time_in_future()
    monkeypatch.setattr(image.time, "sleep", lambda *_args: None)
    image.bind_image_stop(lambda: (_ for _ in ()).throw(PipelineCancelled("stop")))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"event_id": "evt"})
        return httpx.Response(200, text="event: error\ndata: null\n\n")

    with pytest.raises(PipelineCancelled):
        FluxSchnellImageProvider(base_url="https://flux.test", transport=httpx.MockTransport(handler)).generate(
            prompt="A quiet room",
            dest=tmp_path / "still.png",
            size="1280x720",
            seed=4,
        )


def test_saved_pollinations_settings_move_off_that_service():
    from dramastill.config.defaults import app_settings_from_dict

    settings = app_settings_from_dict(
        {
            "llm": {"provider": "pollinations", "base_url": "https://text.pollinations.ai/openai", "model": "openai-fast"},
            "image": {"provider": "pollinations", "base_url": "https://image.pollinations.ai", "model": "sana"},
        }
    )
    assert settings.llm.provider == "pollinations"
    assert "pollinations.ai" in settings.llm.base_url
    assert settings.image.provider == "flux"
    assert "pollinations" not in settings.image.base_url


def test_gemini_without_key_uses_pollinations(tmp_path):
    secrets = SecretStore(tmp_path / "secrets")
    settings = AppSettings()
    provider = build_llm(settings, secrets)
    assert provider.model == "openai-fast"
    assert "pollinations.ai" in provider.base_url
    route = describe_route(settings, secrets)
    assert "Pollinations" in route
    assert "FLUX" in route
    assert "дневного лимита" in route
    secrets.set("llm", "AIza-free-gemini-key")
    paid_route = build_llm(settings, secrets)
    assert paid_route.model == "gemini-3.8-flash"
    assert paid_route.api_key == "AIza-free-gemini-key"


def test_grok_key_skips_the_slow_free_route(tmp_path):
    secrets = SecretStore(tmp_path / "secrets")
    secrets.set("llm", "xai-test-key")
    settings = AppSettings()
    provider = build_llm(settings, secrets)
    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.model == "grok-4.7"
    assert provider.retries_immediately() is True
    assert "Grok" in describe_route(settings, secrets)
    settings.llm.provider = "grok"
    settings.llm.model = "grok-4.7"
    secrets.set("llm", "")
    try:
        build_llm(settings, secrets)
    except ProviderError as exc:
        assert "console.x.ai" in str(exc)
    else:
        raise AssertionError("Grok без ключа не должен молча уходить на медленный маршрут")


def test_tokenlavka_uses_its_gateway(tmp_path):
    secrets = SecretStore(tmp_path / "secrets")
    settings = AppSettings()
    settings.llm.provider = "tokenlavka"
    settings.llm.base_url = "https://tokenlavka.com/v1"
    settings.llm.model = "claude-fable-5-1"
    try:
        build_llm(settings, secrets)
    except ProviderError as exc:
        assert "Tokenlavka" in str(exc)
    else:
        raise AssertionError("Tokenlavka без ключа не должна уходить на бесплатный маршрут")
    secrets.set("llm", "xai-test-key")
    provider = build_llm(settings, secrets)
    assert isinstance(provider, OpenAICompatLLM)
    assert provider.base_url == "https://tokenlavka.com/v1"
    assert provider.model == "claude-fable-5-1"
    assert provider.api_key == "xai-test-key"
    assert provider.retries_immediately() is True
    route = describe_route(settings, secrets)
    assert "Tokenlavka" in route
    assert "claude-fable-5-1" in route
    settings.llm.model = "deepseek-v4.1-flash"
    assert build_llm(settings, secrets).model == "deepseek-v4.1-flash"


def test_tokenlavka_balance_reads_the_cabinet_remainder():
    from dramastill.providers.tokenlavka import balance_view, format_balance

    label, tip = format_balance({"key": {"balance": 1234567, "reserved": 20}})
    assert label == "Баланс: 1 234 567"
    assert "20" in tip
    secret = "sk-testsecretvalue123456"
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        body = json.loads(request.content.decode())
        assert body["key"] == secret
        return httpx.Response(200, json={"key": {"balance": 42, "reserved": 0}})

    label, tip = balance_view(secret, transport=httpx.MockTransport(handler))
    assert seen["path"] == "/api/portal"
    assert label == "Баланс: 42"
    assert secret not in label
    assert secret not in tip

    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    label, _tip = balance_view(secret, transport=httpx.MockTransport(missing))
    assert label == "Баланс: ключ не найден"
    assert secret not in label


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
