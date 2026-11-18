from __future__ import annotations

from dramastill.config.defaults import AppSettings
from dramastill.core.secrets import SecretStore
from dramastill.media.ffmpeg_util import FFmpegError, resolve_ffmpeg
from dramastill.providers.factory import image_needs_key, llm_needs_key, tts_needs_key


def preflight(settings: AppSettings, secrets: SecretStore) -> list[str]:
    problems: list[str] = []
    try:
        resolve_ffmpeg(settings.video)
    except FFmpegError as exc:
        problems.append(str(exc))
    if settings.story.language == "custom" and not settings.story.custom_language.strip():
        problems.append("Укажите название языка")
    if not settings.story.automatic and not settings.story.idea.strip():
        problems.append("Введите идею или включите полностью автоматический режим")
    if settings.story.duration_minutes < 1:
        problems.append("Длительность должна быть не меньше одной минуты")
    if settings.story.chapters < 1:
        problems.append("Нужна хотя бы одна глава")
    if settings.story.duration_minutes > 0 and settings.story.chapters > 0:
        per_chapter = settings.story.duration_minutes * settings.story.words_per_minute / settings.story.chapters
        if per_chapter > 1100:
            problems.append("Слишком мало глав для такой длины. Увеличьте число глав, чтобы сценарий не обрывался")
    if llm_needs_key(settings) and not secrets.get("llm"):
        problems.append("Не задан API-ключ языковой модели")
    if image_needs_key(settings) and not secrets.get("image"):
        problems.append("Не задан API-ключ генерации изображений")
    if tts_needs_key(settings) and not secrets.get("tts"):
        problems.append("Не задан API-ключ озвучки")
    if settings.video.width < 640 or settings.video.height < 360:
        problems.append("Разрешение видео слишком маленькое")
    if settings.video.fps < 8 or settings.video.fps > 60:
        problems.append("FPS должен быть от 8 до 60")
    if settings.voice.provider == "elevenlabs" and not settings.voice.voice.strip():
        problems.append("Выберите голос ElevenLabs")
    if settings.voice.provider != "elevenlabs" and not settings.voice.voice.strip():
        problems.append("Выберите голос")
    return problems
