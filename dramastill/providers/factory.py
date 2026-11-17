from __future__ import annotations

from dataclasses import dataclass

from dramastill.config.defaults import AppSettings
from dramastill.core.secrets import SecretStore
from dramastill.providers.base import ImageProvider, LLMProvider, TTSProvider
from dramastill.providers.errors import ProviderError
from dramastill.providers.image import OpenAIImageProvider, PollinationsImageProvider, StabilityImageProvider
from dramastill.providers.llm import AnthropicLLM, OpenAICompatLLM
from dramastill.providers.tts import EdgeTTS, ElevenLabsTTS, OpenAITTS


@dataclass
class Providers:
    llm: LLMProvider
    image: ImageProvider
    tts: TTSProvider


POLLINATIONS_TEXT = "https://text.pollinations.ai/openai"
POLLINATIONS_TEXT_MODEL = "openai-fast"


def llm_needs_key(settings: AppSettings) -> bool:
    if settings.llm.provider in {"ollama", "pollinations", "gemini"}:
        return False
    base = (settings.llm.base_url or "").lower()
    if "127.0.0.1" in base or "localhost" in base or "pollinations.ai" in base:
        return False
    return True


def image_needs_key(settings: AppSettings) -> bool:
    if settings.image.provider == "pollinations":
        return False
    base = (settings.image.base_url or "").lower()
    if settings.image.provider != "stability" and ("127.0.0.1" in base or "localhost" in base or "pollinations.ai" in base):
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
        story = "История: Pollinations GPT-OSS 20B, ключ Gemini не задан"
    elif settings.llm.provider == "pollinations":
        story = "История: Pollinations GPT-OSS 20B"
    elif settings.llm.provider == "gemini":
        story = f"История: Gemini {settings.llm.model}"
    elif settings.llm.provider == "groq":
        story = f"История: Groq {settings.llm.model}"
    else:
        story = f"История: {settings.llm.provider} {settings.llm.model}"
    if settings.image.provider == "pollinations":
        picture = "картинка: Pollinations Sana"
    else:
        picture = f"картинка: {settings.image.provider} {settings.image.model}"
    if settings.voice.provider == "edge":
        voice = "голос: Edge Neural"
    else:
        voice = f"голос: {settings.voice.provider}"
    return f"{story}; {picture}; {voice}. На проект по-прежнему одна картинка."


def build_llm(settings: AppSettings, secrets: SecretStore):
    key = secrets.get("llm")
    if gemini_falls_back(settings, secrets):
        return OpenAICompatLLM(base_url=POLLINATIONS_TEXT, api_key="", model=POLLINATIONS_TEXT_MODEL)
    if llm_needs_key(settings) and not key:
        raise ProviderError("Не задан API-ключ языковой модели")
    if settings.llm.provider == "anthropic":
        return AnthropicLLM(api_key=key, model=settings.llm.model, base_url=settings.llm.base_url or "https://api.anthropic.com")
    return OpenAICompatLLM(base_url=settings.llm.base_url, api_key=key, model=settings.llm.model)


def build_image(settings: AppSettings, secrets: SecretStore):
    key = secrets.get("image")
    if image_needs_key(settings) and not key:
        raise ProviderError("Не задан API-ключ генерации изображений")
    if settings.image.provider == "pollinations" or "pollinations.ai" in (settings.image.base_url or "").lower():
        return PollinationsImageProvider(base_url=settings.image.base_url or "https://image.pollinations.ai", model=settings.image.model or "sana")
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
