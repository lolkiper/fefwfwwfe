from __future__ import annotations

from dataclasses import dataclass

from dramastill.config.defaults import FLUX_BASE, LLM7_BASE, LLM7_MODEL, AppSettings
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
    if settings.llm.provider in {"ollama", "llm7", "gemini"}:
        return False
    base = (settings.llm.base_url or "").lower()
    if "127.0.0.1" in base or "localhost" in base or "llm7.io" in base:
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


def gemini_falls_back(settings: AppSettings, secrets: SecretStore) -> bool:
    return settings.llm.provider == "gemini" and not secrets.get("llm")


def describe_route(settings: AppSettings, secrets: SecretStore) -> str:
    if gemini_falls_back(settings, secrets):
        story = "История: LLM7 DeepSeek V4 Flash, ключ Gemini не задан"
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
    return f"{story}; {picture}; {voice}. На проект по-прежнему одна картинка."


def build_llm(settings: AppSettings, secrets: SecretStore):
    key = secrets.get("llm")
    if gemini_falls_back(settings, secrets) or settings.llm.provider == "llm7":
        base = settings.llm.base_url if settings.llm.provider == "llm7" and settings.llm.base_url else LLM7_BASE
        model = settings.llm.model if settings.llm.provider == "llm7" and settings.llm.model else LLM7_MODEL
        return OpenAICompatLLM(base_url=base, api_key=key, model=model)
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
