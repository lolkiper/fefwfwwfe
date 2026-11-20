from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


THEMES: list[tuple[str, str]] = [
    ("auto", "Автоматический выбор"),
    ("mother_in_law", "Конфликт свекрови и невестки"),
    ("family_secret", "Семейная тайна"),
    ("betrayal_revenge", "Предательство и месть"),
    ("poverty_wealth", "Бедность и богатство"),
    ("inheritance", "Неожиданное наследство"),
    ("tragedy", "Семейная трагедия"),
    ("love_infidelity", "Любовь, измена и примирение"),
    ("social_drama", "Социальная драма"),
]

LANGUAGES: list[tuple[str, str]] = [
    ("ru", "Русский"),
    ("es", "Испанский"),
    ("uz", "Узбекский"),
    ("en", "Английский"),
    ("custom", "Другой язык"),
]

LANGUAGE_GUIDE = {
    "ru": "Write every audience-facing sentence in Russian.",
    "es": "Write every audience-facing sentence in Spanish.",
    "uz": "Write every audience-facing sentence in Uzbek using the Latin alphabet.",
    "en": "Write every audience-facing sentence in English.",
}

IMAGE_STYLES: list[tuple[str, str, str]] = [
    (
        "cinematic_realistic",
        "Кинематографический реализм",
        "photorealistic cinematic film still, anamorphic lens, shallow depth of field, natural skin texture, dramatic motivated lighting, 35mm photography",
    ),
    (
        "intimate_drama",
        "Камерная семейная драма",
        "photorealistic intimate drama still, natural window light, realistic faces with visible emotion, detailed clothing and domestic interior, cinematic color",
    ),
    (
        "night_rain",
        "Ночь и дождь",
        "photorealistic night scene, wet surfaces, practical lamps, rain in the air, emotional faces, cinematic contrast",
    ),
    (
        "wealth_poverty",
        "Богатство и бедность",
        "photorealistic cinematic contrast between wealth and hardship, realistic faces, specific clothing and setting, dramatic but natural light",
    ),
]

LLM7_BASE = "https://api.llm7.io/v1"
LLM7_MODEL = "DeepSeek-V4-Flash-0731"
LLM7_FALLBACK = "gemma4:31b"
POLLINATIONS_BASE = "https://text.pollinations.ai/openai"
POLLINATIONS_MODEL = "openai-fast"
FLUX_BASE = "https://black-forest-labs-flux-1-schnell.hf.space"
FLUX_MODEL = "flux-1-schnell"

LLM_PRESETS: list[tuple[str, str, str, str]] = [
    ("tokenlavka", "Tokenlavka Claude Fable 5.1", "https://tokenlavka.com/v1", "claude-fable-5-1"),
    ("grok", "Grok 4.7 (ключ xAI, без паузы)", "https://api.x.ai/v1", "grok-4.7"),
    ("gemini", "Gemini 3.8 Flash (бесплатно)", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),
    ("pollinations", "Pollinations GPT-OSS (без ключа)", POLLINATIONS_BASE, POLLINATIONS_MODEL),
    ("llm7", "LLM7 DeepSeek (нужен бесплатный токен)", LLM7_BASE, LLM7_MODEL),
    ("groq", "Groq GPT-OSS 120B (бесплатно)", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("openrouter", "OpenRouter free", "https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free"),
    ("ollama", "Ollama (локально)", "http://127.0.0.1:11434/v1", "llama3.1"),
    ("openai", "OpenAI (платно)", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("anthropic", "Anthropic (платно)", "https://api.anthropic.com", "claude-sonnet-4-5"),
    ("custom", "Свой OpenAI-совместимый адрес", "http://127.0.0.1:8080/v1", "local-model"),
]

IMAGE_PRESETS: list[tuple[str, str, str, str]] = [
    ("flux", "FLUX.1 Schnell (без ключа)", FLUX_BASE, FLUX_MODEL),
    ("openai", "OpenAI Images (платно)", "https://api.openai.com/v1", "dall-e-3"),
    ("stability", "Stability AI (платно)", "https://api.stability.ai", "core"),
    ("openai_compatible", "Совместимый Images API", "https://api.openai.com/v1", "dall-e-3"),
]

TTS_PRESETS: list[tuple[str, str, str, str]] = [
    ("edge", "Edge Neural (без ключа)", "", "edge"),
    ("openai", "OpenAI TTS (платно)", "https://api.openai.com/v1", "gpt-4o-mini-tts"),
    ("elevenlabs", "ElevenLabs (платно)", "https://api.elevenlabs.io", "eleven_multilingual_v2"),
]

RESOLUTIONS: list[tuple[str, int, int]] = [
    ("1080p", 1920, 1080),
    ("1440p", 2560, 1440),
    ("4K", 3840, 2160),
]

STATUS_LABELS = {
    "queued": "В очереди",
    "running": "Создаётся",
    "paused": "Пауза",
    "failed": "Ошибка",
    "completed": "Готово",
    "cancelled": "Остановлено",
}

STAGE_LABELS = {
    "": "—",
    "story": "История",
    "image": "Изображение",
    "tts": "Озвучка",
    "analyze": "Анализ аудио",
    "particles": "Частицы",
    "render": "Сборка видео",
    "concat": "Склейка",
    "publish": "Описание и превью",
    "done": "Завершено",
}

EMOTIONS: list[tuple[str, str]] = [
    ("neutral", "Нейтральная"),
    ("calm", "Спокойная"),
    ("dramatic", "Драматическая"),
    ("sad", "Печальная"),
    ("warm", "Тёплая"),
    ("tense", "Напряжённая"),
]


def theme_label(theme_id: str) -> str:
    for key, label in THEMES:
        if key == theme_id:
            return label
    return theme_id


def language_instruction(language: str, custom: str) -> str:
    if language == "custom":
        name = custom.strip() or "Russian"
        return f"Write every audience-facing sentence in {name}."
    return LANGUAGE_GUIDE.get(language, LANGUAGE_GUIDE["ru"])


def language_name(language: str, custom: str) -> str:
    if language == "custom":
        return custom.strip() or "Другой язык"
    for key, label in LANGUAGES:
        if key == language:
            return label
    return language


@dataclass
class StorySettings:
    theme: str = "auto"
    language: str = "ru"
    custom_language: str = ""
    duration_minutes: int = 15
    chapters: int = 4
    words_per_minute: int = 135
    automatic: bool = True
    idea: str = ""
    consistency_check: bool = True


@dataclass
class LLMSettings:
    provider: str = "gemini"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    model: str = "gemini-3.8-flash"
    temperature: float = 0.9
    max_tokens: int = 8000


@dataclass
class ImageSettings:
    provider: str = "flux"
    base_url: str = FLUX_BASE
    model: str = FLUX_MODEL
    size: str = "1280x720"
    style: str = "cinematic_realistic"
    style_extra: str = ""
    consistency_lock: bool = True
    seed: int = -1


@dataclass
class VoiceSettings:
    provider: str = "edge"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini-tts"
    voice: str = "ru-RU-SvetlanaNeural"
    gender: str = "female"
    speed: float = 1.0
    emotion: str = "dramatic"
    sentence_pause_ms: int = 0
    chapter_pause_ms: int = 700
    chunk_chars: int = 2800
    parallelism: int = 2
    eleven_model: str = "eleven_multilingual_v2"


@dataclass
class VideoSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    video_bitrate: str = "8M"
    audio_bitrate: str = "192k"
    encoder: str = "auto"
    x264_preset: str = "veryfast"
    chunk_seconds: int = 120
    preset: str = "cinematic"
    zoom_base: float = 1.075
    zoom_amplitude: float = 0.02
    zoom_period: float = 120.0
    pan_x_px: float = 32.0
    pan_y_px: float = 18.0
    pan_period_x: float = 108.0
    pan_period_y: float = 146.0
    shake_px: float = 1.1
    shake_period: float = 8.5
    sparks: bool = True
    dust: bool = True
    lights: bool = True
    spark_density: float = 1.2
    spark_opacity: float = 0.88
    dust_opacity: float = 0.42
    light_opacity: float = 0.24
    intro_path: str = ""
    outro_path: str = ""
    vignette: float = 0.35
    grade_contrast: float = 1.04
    grade_saturation: float = 1.05
    visualizer: bool = True
    visualizer_bars: int = 48
    visualizer_color: str = "#E6C36A"
    visualizer_opacity: float = 0.72
    visualizer_height: int = 150
    visualizer_gap: float = 0.36
    visualizer_smooth: float = 0.78
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    cleanup_temp: bool = True
    max_parallel_projects: int = 2
    performance_mode: str = "balanced"
    cooling_mode: str = ""
    temp_warning_c: int = 72
    temp_limit_c: int = 82
    max_cpu_percent: int = 0
    max_gpu_percent: int = 0
    max_ram_mb: int = 0
    worker_urls: str = ""
    worker_token: str = ""
    render_mode: str = "fast"
    cpu_threads: int = 0
    memory_limit_percent: int = 90
    output_dir: str = ""


@dataclass
class AppSettings:
    story: StorySettings = field(default_factory=StorySettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    image: ImageSettings = field(default_factory=ImageSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    video: VideoSettings = field(default_factory=VideoSettings)

    def to_dict(self) -> dict:
        return asdict(self)


def _take(cls, raw: object):
    data = raw if isinstance(raw, dict) else {}
    known = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in known})


def app_settings_from_dict(raw: dict | None) -> AppSettings:
    data = raw or {}
    settings = AppSettings(
        story=_take(StorySettings, data.get("story")),
        llm=_take(LLMSettings, data.get("llm")),
        image=_take(ImageSettings, data.get("image")),
        voice=_take(VoiceSettings, data.get("voice")),
        video=_take(VideoSettings, data.get("video")),
    )
    _retire_pollinations(settings)
    return settings


def _retire_pollinations(settings: AppSettings) -> None:
    image_base = (settings.image.base_url or "").lower()
    if settings.image.provider == "pollinations" or "pollinations.ai" in image_base:
        settings.image.provider = "flux"
        settings.image.base_url = FLUX_BASE
        settings.image.model = FLUX_MODEL


def style_prompt(style_id: str) -> str:
    for key, _label, prompt in IMAGE_STYLES:
        if key == style_id:
            return prompt
    return IMAGE_STYLES[0][2]


EFFECT_PRESETS: dict[str, dict] = {
    "subtle": {
        "zoom_base": 1.06,
        "zoom_amplitude": 0.014,
        "zoom_period": 140.0,
        "pan_x_px": 22.0,
        "pan_y_px": 12.0,
        "pan_period_x": 130.0,
        "pan_period_y": 168.0,
        "shake_px": 0.6,
        "shake_period": 11.0,
        "spark_density": 0.7,
        "spark_opacity": 0.62,
        "dust_opacity": 0.28,
        "light_opacity": 0.14,
        "visualizer_opacity": 0.55,
        "visualizer_height": 120,
        "vignette": 0.25,
    },
    "cinematic": {
        "zoom_base": 1.075,
        "zoom_amplitude": 0.02,
        "zoom_period": 120.0,
        "pan_x_px": 32.0,
        "pan_y_px": 18.0,
        "pan_period_x": 108.0,
        "pan_period_y": 146.0,
        "shake_px": 1.1,
        "shake_period": 8.5,
        "spark_density": 1.2,
        "spark_opacity": 0.88,
        "dust_opacity": 0.42,
        "light_opacity": 0.24,
        "visualizer_opacity": 0.72,
        "visualizer_height": 150,
        "vignette": 0.35,
    },
    "dramatic": {
        "zoom_base": 1.09,
        "zoom_amplitude": 0.03,
        "zoom_period": 96.0,
        "pan_x_px": 42.0,
        "pan_y_px": 22.0,
        "pan_period_x": 84.0,
        "pan_period_y": 118.0,
        "shake_px": 1.8,
        "shake_period": 6.5,
        "spark_density": 1.65,
        "spark_opacity": 0.95,
        "dust_opacity": 0.5,
        "light_opacity": 0.32,
        "visualizer_opacity": 0.84,
        "visualizer_height": 170,
        "vignette": 0.42,
    },
    "warm_dust": {
        "zoom_base": 1.075,
        "zoom_amplitude": 0.022,
        "zoom_period": 86.0,
        "pan_x_px": 20.0,
        "pan_y_px": 12.0,
        "pan_period_x": 74.0,
        "pan_period_y": 90.0,
        "shake_px": 0.9,
        "shake_period": 9.5,
        "spark_density": 0.9,
        "spark_opacity": 0.7,
        "dust_opacity": 0.7,
        "light_opacity": 0.22,
        "visualizer_opacity": 0.62,
        "visualizer_height": 140,
        "vignette": 0.3,
    },
    "minimal": {
        "zoom_base": 1.06,
        "zoom_amplitude": 0.012,
        "zoom_period": 110.0,
        "pan_x_px": 10.0,
        "pan_y_px": 6.0,
        "pan_period_x": 150.0,
        "pan_period_y": 180.0,
        "shake_px": 0.4,
        "shake_period": 12.0,
        "spark_density": 0.2,
        "spark_opacity": 0.35,
        "dust_opacity": 0.18,
        "light_opacity": 0.08,
        "visualizer_opacity": 0.45,
        "visualizer_height": 100,
        "vignette": 0.18,
    },
}
