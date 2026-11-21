from __future__ import annotations

from dataclasses import dataclass

from dramastill.config.defaults import (
    FLUX_BASE,
    LLM7_BASE,
    LLM7_FALLBACK,
    LLM7_MODEL,
    POLLINATIONS_BASE,
    POLLINATIONS_MODEL,
    AppSettings,
)
from dramastill.core.secrets import SecretStore
from dramastill.providers.base import ImageProvider, LLMProvider, TTSProvider
from dramastill.providers.errors import ProviderError
from dramastill.providers.image import FluxSchnellImageProvider, OpenAIImageProvider, StabilityImageProvider
from dramastill.providers.llm import AnthropicLLM, OpenAICompatLLM
from dramastill.providers.tts import EdgeTTS, ElevenLabsTTS, OpenAITTS


@dataclass
class Providers:
    llm: LLMProvider
    image: ImageProvider
    tts: TTSProvider


def llm_needs_key(settings: AppSettings) -> bool:
    if settings.llm.provider in {"ollama", "llm7", "gemini", "pollinations"}:
        return False
    base = (settings.llm.base_url or "").lower()
    if "127.0.0.1" in base or "localhost" in base or "llm7.io" in base or "pollinations.ai" in base:
        return False
    return True


def image_needs_key(settings: AppSettings) -> bool:
    if settings.image.provider == "flux":
        return False
    base = (settings.image.base_url or "").lower()
    if settings.image.provider != "stability" and ("127.0.0.1" in base or "localhost" in base or "flux-1-schnell" in base):
        return False
    return True


def tts_needs_key(settings: AppSettings) -> bool:
    return settings.voice.provider != "edge"


def build_providers(settings: AppSettings, secrets: SecretStore) -> Providers:
    return Providers(
        llm=build_llm(settings, secrets),
        image=build_image(settings, secrets),
        tts=build_tts(settings, secrets),
    )


def _uses_tokenlavka(settings: AppSettings) -> bool:
    if settings.llm.provider == "tokenlavka":
        return True
    return "tokenlavka.com" in (settings.llm.base_url or "").lower()


def _uses_grok(settings: AppSettings, secrets: SecretStore) -> bool:
    if _uses_tokenlavka(settings):
        return False
    key = secrets.get("llm") or ""
    if settings.llm.provider == "grok" or "api.x.ai" in (settings.llm.base_url or "").lower():
        return True
    if settings.llm.provider not in {"gemini", ""}:
        return False
    return key.startswith("xai-")


def uses_pollinations(settings: AppSettings, secrets: SecretStore) -> bool:
    if _uses_tokenlavka(settings) or _uses_grok(settings, secrets):
        return False
    if settings.llm.provider == "pollinations" or "pollinations.ai" in (settings.llm.base_url or "").lower():
        return not secrets.get("llm") or settings.llm.provider == "pollinations"
    if settings.llm.provider == "gemini" and not secrets.get("llm"):
        return True
    return settings.llm.provider == "llm7" and not secrets.get("llm")


def describe_route(settings: AppSettings, secrets: SecretStore) -> str:
    if uses_pollinations(settings, secrets):
        story = "История: Pollinations GPT-OSS 20B, без ключа и без дневного лимита"
    elif _uses_tokenlavka(settings):
        model = settings.llm.model or "claude-fable-5-1"
        story = f"История: Tokenlavka {model}, без паузы 46 секунд"
    elif _uses_grok(settings, secrets):
        model = settings.llm.model if "grok" in (settings.llm.model or "").lower() else "grok-4.7"
        story = f"История: Grok {model}, без паузы 46 секунд"
    elif settings.llm.provider == "llm7":
        story = f"История: LLM7 {settings.llm.model}"
    elif settings.llm.provider == "gemini":
        story = f"История: Gemini {settings.llm.model}"
    elif settings.llm.provider == "groq":
        story = f"История: Groq {settings.llm.model}"
    else:
        story = f"История: {settings.llm.provider} {settings.llm.model}"
    if settings.image.provider == "flux":
        picture = "картинка: FLUX.1 Schnell"
    else:
        picture = f"картинка: {settings.image.provider} {settings.image.model}"
    if settings.voice.provider == "edge":
        voice = "голос: Edge Neural"
    else:
        voice = f"голос: {settings.voice.provider}"
    return f"{story}; {picture}; {voice}. На каждую главу своя картинка."


def build_llm(settings: AppSettings, secrets: SecretStore):
    key = secrets.get("llm")
    if _uses_tokenlavka(settings):
        if not key:
            raise ProviderError("Для Tokenlavka нужен ключ. Его вставляют в поле ключа языковой модели.")
        base = settings.llm.base_url if "tokenlavka.com" in (settings.llm.base_url or "").lower() else "https://tokenlavka.com/v1"
        return OpenAICompatLLM(base_url=base, api_key=key, model=settings.llm.model or "claude-fable-5-1")
    if _uses_grok(settings, secrets):
        if not key:
            raise ProviderError("Для Grok нужен ключ с console.x.ai. Его вставляют в поле ключа языковой модели.")
        model = settings.llm.model if "grok" in (settings.llm.model or "").lower() else "grok-4.7"
        return OpenAICompatLLM(base_url="https://api.x.ai/v1", api_key=key, model=model)
    if uses_pollinations(settings, secrets):
        return OpenAICompatLLM(base_url=POLLINATIONS_BASE, api_key="", model=POLLINATIONS_MODEL)
    if settings.llm.provider == "llm7":
        provider = OpenAICompatLLM(base_url=settings.llm.base_url or LLM7_BASE, api_key=key, model=settings.llm.model or LLM7_MODEL)
        provider.model_chain = tuple(name for name in (LLM7_MODEL, LLM7_FALLBACK) if name != provider.model)
        return provider
    if llm_needs_key(settings) and not key:
        raise ProviderError("Не задан API-ключ языковой модели")
    if settings.llm.provider == "anthropic":
        return AnthropicLLM(api_key=key, model=settings.llm.model, base_url=settings.llm.base_url or "https://api.anthropic.com")
    return OpenAICompatLLM(base_url=settings.llm.base_url, api_key=key, model=settings.llm.model)


def build_image(settings: AppSettings, secrets: SecretStore):
    key = secrets.get("image")
    if image_needs_key(settings) and not key:
        raise ProviderError("Не задан API-ключ генерации изображений")
    if settings.image.provider == "flux" or "flux-1-schnell" in (settings.image.base_url or "").lower():
        return FluxSchnellImageProvider(base_url=settings.image.base_url or FLUX_BASE, model=settings.image.model or "flux-1-schnell")
    if settings.image.provider == "stability":
        return StabilityImageProvider(api_key=key, model=settings.image.model or "core")
    return OpenAIImageProvider(base_url=settings.image.base_url, api_key=key, model=settings.image.model)


def build_tts(settings: AppSettings, secrets: SecretStore):
    provider = settings.voice.provider
    if provider == "edge":
        return EdgeTTS()
    key = secrets.get("tts")
    if not key:
        raise ProviderError("Не задан API-ключ озвучки")
    if provider == "elevenlabs":
        return ElevenLabsTTS(api_key=key, model=settings.voice.eleven_model or settings.voice.model)
    return OpenAITTS(base_url=settings.voice.base_url, api_key=key, model=settings.voice.model)
