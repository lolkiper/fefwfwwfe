from __future__ import annotations

from pathlib import Path

from PIL import Image

from dramastill.config.defaults import ImageSettings
from dramastill.providers.base import ImageProvider
from dramastill.providers.errors import ProviderError
from dramastill.story.image_prompt import build_image_prompt


def valid_image(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1000:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            return image.width >= 32 and image.height >= 32
    except Exception:
        return False


def ensure_image(
    *,
    story: dict,
    settings: ImageSettings,
    provider: ImageProvider,
    dest: Path,
    prompt_path: Path,
    log,
) -> Path:
    """Генерирует ровно одно изображение. Повторный вызов не создаёт второе."""
    prompt = build_image_prompt(story, settings)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    if valid_image(dest):
        log("info", "Изображение проекта уже есть и повторно не генерируется")
        return dest
    log("info", "Генерируется одно изображение на всю историю")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
    if partial.exists():
        partial.unlink()
    seed = settings.seed if settings.seed >= 0 else None
    try:
        provider.generate(prompt=prompt, dest=partial, size=settings.size or "1792x1024", seed=seed)
        if not valid_image(partial):
            raise ProviderError("Сервис вернул повреждённое изображение")
        partial.replace(dest)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    log("info", "Изображение сохранено. Другие кадры создаваться не будут")
    return dest
