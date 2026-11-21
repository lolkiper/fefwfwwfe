import json
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from dramastill.cancel import CancelToken
from dramastill.config.defaults import AppSettings
from dramastill.core.database import Database
from dramastill.core.secrets import SecretStore
from dramastill.media.image_service import ensure_image
from dramastill.pipeline.runner import run_project
from dramastill.providers.base import LLMResult
from dramastill.providers.errors import ProviderError

NARRATION = " ".join(
    [
        "Марина услышала чужое имя в завещании и не стала молчать.",
        "Она достала ключ, который прятали в старом пальто.",
        "Сейф открылся тихо, и бумага оказалась теплее рук.",
        "Подпись на листе не совпала с почерком свекрови.",
        "Марина сверила дату и поняла, что документ новее.",
        "В кухне горела одна лампа, и спор уже нельзя было отложить.",
    ]
)


def _concept() -> dict:
    return {
        "title": "Чужое завещание",
        "theme": "наследство",
        "logline": "Невестка находит настоящее завещание.",
        "narrative_voice": "third person",
        "characters": [
            {
                "name": "Марина",
                "role": "невестка",
                "age": 34,
                "appearance": "woman of 34 with dark brown hair pulled back, oval face, tired brown eyes, light olive skin, slim build, a small scar on the left eyebrow",
                "wardrobe": "plain grey wool coat over a black dress",
                "personality": "сдержанная",
            }
        ],
        "chapters": [
            {"index": 1, "title": "Ключ", "summary": "Марина находит ключ от сейфа в кармане старого пальто.", "purpose": "hook"},
            {"index": 2, "title": "Подпись", "summary": "Она понимает, что подпись на документах принадлежит не свекрови.", "purpose": "ending"},
        ],
        "dramatic_moment": "In a dim kitchen at night, Marina holds an old will toward a stern older woman across the table, both faces tense and lit by one lamp.",
        "thumbnail_text": "ЧУЖОЕ ЗАВЕЩАНИЕ",
        "title_variants": ["Чужое завещание", "Ключ от сейфа", "Подпись матери"],
    }


class ScriptLLM:
    def __init__(self):
        self.tasks = []

    def complete(self, *, system, user, temperature, max_tokens, json_mode):
        task = user.splitlines()[0]
        self.tasks.append(task)
        if task == "TASK: concept":
            return LLMResult(json.dumps(_concept(), ensure_ascii=False))
        if task == "TASK: chapter":
            return LLMResult(json.dumps({"narration": NARRATION, "summary": "Событие главы произошло."}, ensure_ascii=False))
        if task == "TASK: consistency":
            return LLMResult(json.dumps({"changed": False, "narration": NARRATION, "summary": "Противоречий нет."}))
        if task == "TASK: metadata":
            return LLMResult(
                json.dumps(
                    {
                        "titles": ["Чужое завещание", "Ключ", "Подпись", "Ночь", "Дом"],
                        "description": "Оригинальная семейная драма.\n\nЭто художественный вымысел.",
                        "tags": ["драма", "семья", "наследство"],
                        "thumbnail_text": "ЧУЖОЕ ЗАВЕЩАНИЕ",
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(task)


class OneImage:
    def __init__(self):
        self.calls = 0

    def generate(self, *, prompt, dest, size, seed):
        self.calls += 1
        tone = 20 + (sum(prompt.encode("utf-8")) % 180)
        image = Image.new("RGB", (640, 360), (tone, 18, 20))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 36, 360), fill=(170, 40, 40))
        draw.rectangle((604, 0, 640, 360), fill=(30, 40, 170))
        draw.ellipse((230, 50, 410, 250), fill=(186, 142, 116))
        image.save(dest, "PNG")


class FlakyVoice:
    name = "test"

    def __init__(self):
        self.calls = []
        self.fail_second = True

    def synthesize(self, *, text, dest, voice, speed, emotion, language):
        self.calls.append(dest.name)
        if self.fail_second and dest.name.startswith("00001"):
            self.fail_second = False
            raise ProviderError("временный обрыв")
        _tone(dest, 0.8)

    def list_voices(self, language=None):
        return []


def _tone(path: Path, seconds: float) -> None:
    rate = 48000
    count = int(rate * seconds)
    time = np.arange(count) / rate
    pcm = (0.2 * np.sin(2 * np.pi * 180 * time) * 32767).astype(np.int16)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def _settings(tmp_path) -> AppSettings:
    settings = AppSettings()
    settings.story.duration_minutes = 1
    settings.story.chapters = 2
    settings.story.words_per_minute = 30
    settings.story.consistency_check = True
    settings.video.width = 320
    settings.video.height = 180
    settings.video.fps = 12
    settings.video.chunk_seconds = 30
    settings.video.video_bitrate = "800k"
    settings.video.encoder = "libx264"
    settings.video.x264_preset = "ultrafast"
    settings.video.spark_density = 0.2
    settings.video.visualizer_bars = 10
    settings.video.visualizer_height = 40
    settings.video.cleanup_temp = True
    settings.video.output_dir = str(tmp_path / "out")
    return settings


def test_pipeline_creates_one_image_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAMASTILL_DATA", str(tmp_path / "data"))
    db = Database(tmp_path / "app.sqlite3")
    project = db.create_project(_settings(tmp_path), "Тест")
    llm = ScriptLLM()
    image = OneImage()
    voice = FlakyVoice()
    from dramastill.providers.factory import Providers

    providers = Providers(llm=llm, image=image, tts=voice)
    try:
        run_project(db, project.id, SecretStore(tmp_path / "secrets"), CancelToken(), providers=providers)
    except ProviderError:
        pass
    else:
        raise AssertionError("first run should stop on the voice failure")
    failed = db.get(project.id)
    assert failed.status == "failed"
    assert image.calls == 0
    speech = Path(project.work_dir) / "audio" / "speech" / "00000.mp3"
    assert speech.exists()
    spoken_before = len(voice.calls)

    output = run_project(db, project.id, SecretStore(tmp_path / "secrets"), CancelToken(), providers=providers)
    done = db.get(project.id)
    assert done.status == "completed"
    assert done.started_at is None
    assert done.elapsed_seconds > 0
    assert image.calls == 2
    assert voice.calls[spoken_before:] == ["00001.partial.mp3"]
    assert output.exists()
    assert output.with_suffix(".jpg").exists()
    assert output.with_suffix(".youtube.json").exists()
    pngs = sorted(Path(project.work_dir).rglob("*.png"))
    assert [path.name for path in pngs] == ["chapter_01.png", "chapter_02.png"]
    assert not (Path(project.work_dir) / "chunks").exists()
    metadata = json.loads(output.with_suffix(".youtube.json").read_text(encoding="utf-8"))
    assert "ЧУЖОЕ ЗАВЕЩАНИЕ" == metadata["thumbnail_text"]


def test_ensure_image_never_calls_provider_twice(tmp_path):
    provider = OneImage()
    story = _concept()
    dest = tmp_path / "image.png"
    settings = AppSettings().image
    ensure_image(story=story, settings=settings, provider=provider, dest=dest, prompt_path=tmp_path / "prompt.txt", log=lambda *_: None)
    ensure_image(story=story, settings=settings, provider=provider, dest=dest, prompt_path=tmp_path / "prompt.txt", log=lambda *_: None)
    assert provider.calls == 1
    assert "one camera angle" in (tmp_path / "prompt.txt").read_text(encoding="utf-8").lower() or "Single photorealistic" in (tmp_path / "prompt.txt").read_text(encoding="utf-8")
