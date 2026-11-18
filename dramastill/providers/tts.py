from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from dramastill.core.redact import redact
from dramastill.providers.base import VoiceInfo
from dramastill.providers.errors import ProviderError
from dramastill.providers.llm import _error_message
from dramastill.providers.retry import RETRY_STATUSES, with_retries

EDGE_EMOTION = {
    "neutral": (0, 0),
    "calm": (-8, -2),
    "dramatic": (-6, 0),
    "sad": (-12, -4),
    "warm": (-4, 2),
    "tense": (4, 3),
}

OPENAI_EMOTION = {
    "neutral": "Narrate clearly and naturally, with even pacing.",
    "calm": "Narrate calmly, softly, and steadily, like a quiet conversation.",
    "dramatic": "Narrate as a cinematic storyteller: measured, serious, emotionally present, never shouting.",
    "sad": "Narrate with quiet sadness and compassion, without melodrama.",
    "warm": "Narrate warmly and humanly, with gentle emotion.",
    "tense": "Narrate with restrained tension and urgency, while staying intelligible.",
}

ELEVEN_EMOTION = {
    "neutral": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.2},
    "calm": {"stability": 0.72, "similarity_boost": 0.75, "style": 0.12},
    "dramatic": {"stability": 0.34, "similarity_boost": 0.8, "style": 0.55},
    "sad": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.35},
    "warm": {"stability": 0.5, "similarity_boost": 0.78, "style": 0.4},
    "tense": {"stability": 0.3, "similarity_boost": 0.8, "style": 0.62},
}

OPENAI_VOICES = [
    VoiceInfo("alloy", "Alloy", "", "multi"),
    VoiceInfo("ash", "Ash", "", "multi"),
    VoiceInfo("ballad", "Ballad", "", "multi"),
    VoiceInfo("coral", "Coral", "female", "multi"),
    VoiceInfo("echo", "Echo", "male", "multi"),
    VoiceInfo("fable", "Fable", "", "multi"),
    VoiceInfo("nova", "Nova", "female", "multi"),
    VoiceInfo("onyx", "Onyx", "male", "multi"),
    VoiceInfo("sage", "Sage", "", "multi"),
    VoiceInfo("shimmer", "Shimmer", "female", "multi"),
    VoiceInfo("verse", "Verse", "", "multi"),
]

DEFAULT_EDGE_VOICES = [
    VoiceInfo("ru-RU-SvetlanaNeural", "Светлана (ru, женский)", "female", "ru"),
    VoiceInfo("ru-RU-DmitryNeural", "Дмитрий (ru, мужской)", "male", "ru"),
    VoiceInfo("es-ES-ElviraNeural", "Elvira (es, женский)", "female", "es"),
    VoiceInfo("es-ES-AlvaroNeural", "Alvaro (es, мужской)", "male", "es"),
    VoiceInfo("es-MX-DaliaNeural", "Dalia (es-MX, женский)", "female", "es"),
    VoiceInfo("uz-UZ-MadinaNeural", "Madina (uz, женский)", "female", "uz"),
    VoiceInfo("uz-UZ-SardorNeural", "Sardor (uz, мужской)", "male", "uz"),
    VoiceInfo("en-US-JennyNeural", "Jenny (en, женский)", "female", "en"),
    VoiceInfo("en-US-GuyNeural", "Guy (en, мужской)", "male", "en"),
]


def edge_prosody(speed: float, emotion: str) -> tuple[str, str]:
    rate_delta, pitch = EDGE_EMOTION.get(emotion, (0, 0))
    combined = float(speed) * (1 + rate_delta / 100)
    percent = int(round((combined - 1) * 100))
    percent = max(-50, min(100, percent))
    pitch = max(-50, min(50, int(pitch)))
    sign = "+" if percent >= 0 else ""
    pitch_sign = "+" if pitch >= 0 else ""
    return f"{sign}{percent}%", f"{pitch_sign}{pitch}Hz"


class EdgeTTS:
    name = "edge"

    def synthesize(
        self,
        *,
        text: str,
        dest: Path,
        voice: str,
        speed: float,
        emotion: str,
        language: str,
    ) -> None:
        import edge_tts
        from edge_tts.exceptions import EdgeTTSException

        rate, pitch = edge_prosody(speed, emotion)

        async def _save() -> None:
            communicate = edge_tts.Communicate(
                text,
                voice or "ru-RU-SvetlanaNeural",
                rate=rate,
                pitch=pitch,
                connect_timeout=20,
                receive_timeout=180,
            )
            await communicate.save(str(dest))

        def once() -> None:
            try:
                asyncio.run(_save())
            except EdgeTTSException as exc:
                raise ProviderError(f"Edge TTS: {exc}", retryable=True) from exc
            except Exception as exc:
                raise ProviderError(f"Edge TTS: {exc}", retryable=True) from exc
            if not dest.exists() or dest.stat().st_size < 200:
                raise ProviderError("Edge TTS не вернул аудио", retryable=True)

        with_retries(once, attempts=5, base_delay=2.0)

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]:
        import edge_tts

        try:
            rows = asyncio.run(edge_tts.list_voices())
        except Exception as exc:
            raise ProviderError(f"Не удалось получить список голосов Edge: {exc}", retryable=True) from exc
        voices: list[VoiceInfo] = []
        prefix = _edge_prefix(language)
        for row in rows:
            locale = str(row.get("Locale") or "")
            if prefix and not locale.lower().startswith(prefix):
                continue
            short = str(row.get("ShortName") or "")
            if not short:
                continue
            gender = str(row.get("Gender") or "").lower()
            label = f"{row.get('FriendlyName') or short} ({locale})"
            voices.append(VoiceInfo(short, label, gender, locale.split("-")[0].lower()))
        return voices or list(DEFAULT_EDGE_VOICES)


class OpenAITTS:
    name = "openai"

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

    def synthesize(
        self,
        *,
        text: str,
        dest: Path,
        voice: str,
        speed: float,
        emotion: str,
        language: str,
    ) -> None:
        def once(with_instructions: bool) -> None:
            payload: dict = {
                "model": self.model,
                "voice": voice or "nova",
                "input": text,
                "speed": max(0.25, min(4.0, float(speed))),
                "response_format": "mp3",
            }
            if with_instructions:
                instruction = OPENAI_EMOTION.get(emotion, OPENAI_EMOTION["dramatic"])
                payload["instructions"] = instruction
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            with httpx.Client(timeout=httpx.Timeout(180, connect=30), transport=self._transport) as client:
                response = client.post(self.base_url + "/audio/speech", headers=headers, json=payload)
            if response.status_code == 400 and with_instructions and "instruction" in response.text.lower():
                return once(False)
            if response.status_code >= 400:
                raise ProviderError(
                    redact(_error_message(response), [self.api_key]),
                    status=response.status_code,
                    retryable=response.status_code in RETRY_STATUSES,
                )
            if not response.content:
                raise ProviderError("OpenAI TTS вернул пустой файл", retryable=True)
            dest.write_bytes(response.content)

        supports_instructions = "gpt-4o" in self.model or "mini-tts" in self.model
        with_retries(lambda: once(supports_instructions))

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]:
        return list(OPENAI_VOICES)


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(self, *, api_key: str, model: str, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.model = model or "eleven_multilingual_v2"
        self._transport = transport

    def synthesize(
        self,
        *,
        text: str,
        dest: Path,
        voice: str,
        speed: float,
        emotion: str,
        language: str,
    ) -> None:
        if not voice:
            raise ProviderError("Выберите голос ElevenLabs")
        settings = dict(ELEVEN_EMOTION.get(emotion, ELEVEN_EMOTION["dramatic"]))
        settings["speed"] = max(0.7, min(1.2, float(speed)))

        def once(with_speed: bool) -> None:
            voice_settings = dict(settings)
            if not with_speed:
                voice_settings.pop("speed", None)
            payload = {"text": text, "model_id": self.model, "voice_settings": voice_settings}
            headers = {"xi-api-key": self.api_key, "accept": "audio/mpeg", "content-type": "application/json"}
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
            with httpx.Client(timeout=httpx.Timeout(180, connect=30), transport=self._transport) as client:
                response = client.post(url, headers=headers, json=payload)
            if response.status_code == 400 and with_speed and "speed" in response.text.lower():
                return once(False)
            if response.status_code >= 400:
                raise ProviderError(
                    redact(_error_message(response), [self.api_key]),
                    status=response.status_code,
                    retryable=response.status_code in RETRY_STATUSES,
                )
            if not response.content:
                raise ProviderError("ElevenLabs вернул пустой файл", retryable=True)
            dest.write_bytes(response.content)

        with_retries(lambda: once(True))

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]:
        headers = {"xi-api-key": self.api_key}
        with httpx.Client(timeout=httpx.Timeout(60, connect=20), transport=self._transport) as client:
            response = client.get("https://api.elevenlabs.io/v1/voices", headers=headers)
        if response.status_code >= 400:
            raise ProviderError(
                redact(_error_message(response), [self.api_key]),
                status=response.status_code,
                retryable=response.status_code in RETRY_STATUSES,
            )
        voices = []
        for row in response.json().get("voices", []):
            labels = row.get("labels") or {}
            voices.append(
                VoiceInfo(
                    id=str(row.get("voice_id")),
                    label=str(row.get("name") or row.get("voice_id")),
                    gender=str(labels.get("gender") or ""),
                    language=str(labels.get("language") or ""),
                )
            )
        return voices


def _edge_prefix(language: str | None) -> str:
    if not language or language == "custom":
        return ""
    return {"ru": "ru", "es": "es", "uz": "uz", "en": "en"}.get(language, language)
