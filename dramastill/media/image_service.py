from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from dramastill.cancel import CancelToken, PipelineCancelled, PipelinePaused
from dramastill.config.defaults import ImageSettings
from dramastill.providers.base import ImageProvider
from dramastill.providers.errors import ProviderError
from dramastill.providers.image import bind_image_log, bind_image_stop, take_image_note
from dramastill.story.image_prompt import build_image_prompt
from dramastill.story.picture_copy import write_shot


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
    log("info", "Изображение сохранено")
    return dest


def ensure_chapter_images(
    *,
    story: dict,
    settings: ImageSettings,
    provider: ImageProvider,
    directory: Path,
    log,
    token: CancelToken | None = None,
    llm=None,
) -> list[Path]:
    """Ровно одно изображение на главу. Повтор при ошибке касается только этого кадра."""
    chapters = [chapter for chapter in (story.get("chapters") or []) if isinstance(chapter, dict)]
    if not chapters:
        raise ProviderError("В истории нет глав для изображений")
    directory.mkdir(parents=True, exist_ok=True)
    total = len(chapters)
    jobs = []
    for offset, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("index") or offset)
        dest = directory / f"chapter_{number:02d}.png"
        jobs.append((number, dest, chapter))
    if llm is not None and hasattr(llm, "stop_check") and token is not None:
        llm.stop_check = token.check
    writer = getattr(llm, "model", "") or ""
    if writer:
        log("info", f"Описание кадров пишет {writer}. Рисует их сервис картинок.")

    def produce(number: int, dest: Path, chapter: dict, suffix: str = "", force: bool = False) -> None:
        prompt_path = dest.with_suffix(".prompt.txt")
        stored = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        if not force and valid_image(dest) and stored.strip():
            log("info", f"Глава {number}: изображение уже есть и повторно не генерируется")
            return
        if force and stored.strip():
            prompt = stored + suffix
        else:
            prompt = write_shot(llm, story, chapter, settings, number, total, log) + suffix
        last: Exception | None = None
        bind_image_log(log)
        if token is not None:
            bind_image_stop(token.check)
        for attempt in range(1, 4):
            log("info", f"Глава {number}: генерируется своё изображение, попытка {attempt} из 3")
            partial = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
            if partial.exists():
                partial.unlink()
            try:
                provider.generate(
                    prompt=prompt,
                    dest=partial,
                    size=settings.size or "1792x1024",
                    seed=None if settings.seed < 0 else settings.seed + number,
                )
                if not valid_image(partial):
                    raise ProviderError(f"Глава {number}: сервис вернул повреждённое изображение")
                partial.replace(dest)
                prompt_path.write_text(prompt, encoding="utf-8")
                note = take_image_note()
                if note:
                    log("info", f"Глава {number}: {note}")
                log("info", f"Глава {number}: изображение сохранено, {dest.name}")
                return
            except (PipelineCancelled, PipelinePaused):
                if partial.exists():
                    partial.unlink()
                raise
            except Exception as exc:
                last = exc
                take_image_note()
                if partial.exists():
                    partial.unlink()
                reason = str(exc).strip()
                if reason.lower() in {"", "null", "none"}:
                    reason = "FLUX скрыл причину. Кадр отклонён фильтром или очередь не ответила"
                log("warning", f"Глава {number}: изображение не собралось, попытка {attempt} из 3: {reason}")
        raise ProviderError(str(last or f"Глава {number}: изображение не собралось"))

    workers = min(3, len(jobs))
    log("info", f"Изображения глав: {len(jobs)}, одновременно {workers}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(produce, number, dest, prompt) for number, dest, prompt in jobs]
        for future in as_completed(futures):
            future.result()
    _reject_duplicate_stills(jobs, produce, log)
    return [dest for _number, dest, _prompt in jobs]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _reject_duplicate_stills(jobs: list[tuple[int, Path, str]], produce, log) -> None:
    seen: dict[str, int] = {}
    for number, dest, prompt in jobs:
        if not valid_image(dest):
            raise ProviderError(f"Глава {number}: изображение отсутствует после генерации")
        digest = _file_hash(dest)
        previous = seen.get(digest)
        if previous is None:
            seen[digest] = number
            log("info", f"Глава {number}: кадр уникален")
            continue
        log("warning", f"Глава {number}: кадр совпал с главой {previous}, запрашиваю другой ракурс")
        prompt_file = dest.with_suffix(".prompt.txt")
        kept = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""
        produce(
            number,
            dest,
            prompt,
            "\nDifferent camera height, different gesture, different background detail. Do not repeat the earlier still.",
            True,
        )
        if kept.strip():
            prompt_file.write_text(kept, encoding="utf-8")
        again = _file_hash(dest) if valid_image(dest) else ""
        if again and again != digest and again not in seen:
            seen[again] = number
            log("info", f"Глава {number}: после повтора кадр отличается")
        else:
            log("warning", f"Глава {number}: сервис снова вернул тот же кадр, глава остаётся с ним")
