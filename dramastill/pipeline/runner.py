from __future__ import annotations

import json
import shutil
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from dramastill.cancel import CancelToken, PipelineCancelled, PipelinePaused
from dramastill.config.defaults import AppSettings
from dramastill.core.database import Database
from dramastill.core.redact import redact
from dramastill.core.secrets import SecretStore
from dramastill.media.audio_analysis import ensure_bands
from dramastill.media.bookends import deliver_movie
from dramastill.media.composer import render_movie
from dramastill.media.ffmpeg_util import FFmpegError, probe, resolve_ffmpeg
from dramastill.media.resources import chunk_workers, cooling_decision, project_slots
from dramastill.media.image_service import ensure_chapter_images
from dramastill.media.particles import ensure_particles
from dramastill.media.thumbnail import compose_thumbnail
from dramastill.media.tts_service import ChapterAudioPrefetch, chapter_time_spans, ensure_narration_audio
from dramastill.providers.factory import Providers, build_providers, describe_route
from dramastill.story.service import ensure_metadata, ensure_story
from dramastill.util import format_clock, now_iso, safe_filename


@dataclass
class Callbacks:
    log: Callable[[str, str], None]
    progress: Callable[[float, str], None]
    title: Callable[[str], None]


def run_project(
    db: Database,
    project_id: str,
    secrets: SecretStore,
    token: CancelToken,
    *,
    providers: Providers | None = None,
    callbacks: Callbacks | None = None,
) -> Path:
    project = db.get(project_id)
    if project is None:
        raise RuntimeError("Проект не найден")
    settings = project.settings
    work = Path(project.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    db.update(project_id, status="running", started_at=project.started_at or started, error=None)

    def log(level: str, message: str) -> None:
        safe = redact(message, secrets.values())
        db.add_log(project_id, level, safe)
        if callbacks is not None:
            callbacks.log(level, safe)

    def progress(fraction: float, stage: str) -> None:
        bounded = max(0.0, min(1.0, float(fraction)))
        db.update(project_id, progress=bounded, stage=stage)
        if callbacks is not None:
            callbacks.progress(bounded, stage)

    try:
        ffmpeg, ffprobe = resolve_ffmpeg(settings.video)
        active = providers or build_providers(settings, secrets)
        log("info", describe_route(settings, secrets))
        cooling = cooling_decision(settings.video)
        log("info", cooling.summary)
        log(
            "info",
            "Скорость: "
            f"{_mode_label(settings.video.performance_mode)}, монтаж {_render_label(settings.video.render_mode)}. "
            f"Проектов сразу до {project_slots(settings.video)}, фрагментов видео сразу до {chunk_workers(settings.video)}. "
            "Главы и проверка идут до 4 одновременно, озвучка до 6. "
            "На каждую главу своя картинка. Движение через scale и crop, на видеокарте если она принимает CUDA, без zoompan. "
            "Готовые фрагменты склеиваются без повторного кодирования видео.",
        )
        story_path = work / "story.json"
        narration_path = work / "narration.txt"
        progress(0.02, "story")
        prefetch = ChapterAudioPrefetch(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            provider=active.tts,
            settings=settings.voice,
            work_dir=work,
            token=token,
            log=log,
        )
        story_error: Exception | None = None
        try:
            story = ensure_story(
                settings=settings.story,
                llm=active.llm,
                path=story_path,
                narration_path=narration_path,
                token=token,
                log=log,
                progress=lambda fraction, stage: progress(0.02 + 0.20 * fraction, stage),
                temperature=settings.llm.temperature,
                max_tokens=settings.llm.max_tokens,
                on_chapter_final=prefetch.submit,
            )
        except Exception as exc:
            story_error = exc
            story = None
        prefetch_error: Exception | None = None
        try:
            prefetch.wait()
        except Exception as exc:
            prefetch_error = exc
        if story_error is not None:
            raise story_error
        if prefetch_error is not None:
            raise prefetch_error
        assert story is not None
        log("info", "История и озвучка готовы, дальше кадры и монтаж")
        title = str(story.get("title") or project.title)
        db.update(project_id, title=title)
        if callbacks is not None:
            callbacks.title(title)

        progress(0.24, "prepare")
        images, audio, metadata = _prepare_media(
            settings=settings,
            story=story,
            active=active,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            work=work,
            token=token,
            log=log,
            progress=progress,
        )
        audio_info = probe(ffprobe, audio)
        log("info", f"Длительность озвучки: {format_clock(audio_info.duration)}")

        final = work / "final.mp4"
        if not _final_ready(ffprobe, final, audio_info.duration):
            progress(0.59, "analyze")
            total_frames = max(1, int(round(audio_info.duration * settings.video.fps)))
            bands = None
            if settings.video.visualizer:
                log("info", "Считается реальная громкость голоса для визуализатора")
                bands = ensure_bands(
                    ffmpeg=ffmpeg,
                    audio=audio,
                    dest=work / "audio" / "bands.npy",
                    meta_path=work / "audio" / "bands.json",
                    fps=settings.video.fps,
                    n_bars=max(8, int(settings.video.visualizer_bars)),
                    smooth=settings.video.visualizer_smooth,
                    n_frames=total_frames,
                )
            progress(0.63, "particles")
            particles = ensure_particles(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                dest=work / "particles.mp4",
                settings=settings.video,
                seed_text=project_id,
                log=log,
            )
            progress(0.66, "render")
            final = render_movie(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                image=images[0],
                audio=audio,
                particles=particles,
                bands=bands,
                work_dir=work,
                settings=settings.video,
                token=token,
                log=log,
                progress=lambda fraction: progress(0.66 + 0.28 * fraction, "render"),
                chapter_spans=_chapter_spans(work, ffprobe, story, images, audio_info.duration, log),
            )
        else:
            log("info", "Готовое видео уже собрано, повторная сборка не нужна")

        delivered = deliver_movie(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            body=final,
            work_dir=work,
            settings=settings.video,
            log=log,
        )
        progress(0.95, "publish")
        show_title = str((metadata.get("titles") or [title])[0] or title)
        thumb_text = metadata.get("thumbnail_text") or story.get("thumbnail_text") or show_title
        still, chapter_no = _poster_still(story, images)
        log("info", f"Превью собрано из кадра главы {chapter_no}: {show_title}")
        thumbnail = compose_thumbnail(still, thumb_text, work / "thumbnail.jpg", lines=metadata.get("thumbnail_lines"))
        output = _publish(settings, show_title, project_id, delivered, thumbnail, work / "youtube.json")
        if settings.video.cleanup_temp:
            _cleanup(work, log)
        _accumulate_elapsed(db, project_id)
        db.update(
            project_id,
            status="completed",
            stage="done",
            progress=1,
            output_path=str(output),
            error=None,
            title=show_title,
        )
        if callbacks is not None:
            callbacks.title(show_title)
        log("info", f"Видео сохранено: {output}")
        progress(1, "done")
        return output
    except PipelinePaused:
        _accumulate_elapsed(db, project_id)
        db.update(project_id, status="paused")
        log("warning", "Сборка на паузе. Уже готовые файлы сохранены")
        raise
    except PipelineCancelled:
        _accumulate_elapsed(db, project_id)
        db.update(project_id, status="cancelled", error="Остановлено пользователем")
        log("warning", "Сборка остановлена")
        raise
    except Exception as exc:
        _accumulate_elapsed(db, project_id)
        message = redact(str(exc), secrets.values()) or "Неизвестная ошибка"
        db.update(project_id, status="failed", error=message)
        log("error", message)
        detail = redact(traceback.format_exc(), secrets.values())
        (work / "error.log").write_text(detail, encoding="utf-8")
        raise


def _prepare_media(*, settings, story, active, ffmpeg, ffprobe, work, token, log, progress):
    parallel = settings.video.performance_mode != "low" and settings.video.render_mode != "low_resource"

    def do_image():
        token.check()
        progress(0.26, "image")
        return ensure_chapter_images(
            story=story,
            settings=settings.image,
            provider=active.image,
            directory=work / "images",
            log=log,
            token=token,
        )

    def do_audio():
        token.check()
        progress(0.32, "tts")
        return ensure_narration_audio(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            story=story,
            settings=settings.voice,
            provider=active.tts,
            work_dir=work,
            token=token,
            log=log,
            progress=lambda fraction: progress(0.32 + 0.22 * fraction, "tts"),
        )

    def do_meta():
        token.check()
        return ensure_metadata(
            story=story,
            llm=active.llm,
            path=work / "youtube.json",
            settings=settings.story,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            token=token,
            log=log,
        )

    if not parallel:
        return do_image(), do_audio(), do_meta()
    log("info", "Картинка, озвучка и описание собираются одновременно")
    tasks = {"image": do_image, "audio": do_audio, "meta": do_meta}
    results: dict = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_map = {pool.submit(fn): name for name, fn in tasks.items()}
        try:
            for future in as_completed(future_map):
                results[future_map[future]] = future.result()
        except (PipelineCancelled, PipelinePaused):
            raise
        except Exception:
            token.request("cancel")
            raise
    return results["image"], results["audio"], results["meta"]


def _chapter_spans(work: Path, ffprobe: str, story: dict, images: list[Path], duration: float, log) -> list[tuple[float, float, Path]]:
    chapters = [chapter for chapter in (story.get("chapters") or []) if isinstance(chapter, dict)]
    by_number: dict[int, Path] = {}
    for offset, (chapter, path) in enumerate(zip(chapters, images), start=1):
        by_number[int(chapter.get("index") or offset)] = path
    plan_path = work / "audio" / "plan.json"
    spans: list[tuple[float, float, Path]] = []
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            plan = []
        if isinstance(plan, list):
            for start, end, number in chapter_time_spans(plan, work / "audio" / "speech", ffprobe):
                spans.append((start, end, by_number.get(number) or images[0]))
    if not spans:
        step = duration / max(1, len(images))
        spans = [(index * step, (index + 1) * step, path) for index, path in enumerate(images)]
    log("info", f"Видео получает {len(spans)} разных кадров, по одному на отрезок истории")
    return spans


def _poster_still(story: dict, images: list[Path]) -> tuple[Path, int]:
    chapters = [chapter for chapter in (story.get("chapters") or []) if isinstance(chapter, dict)]
    best_score = 0
    best_offset = 0
    for offset, chapter in enumerate(chapters[: len(images)]):
        purpose = str(chapter.get("purpose") or "").lower()
        score = 1
        if "turn" in purpose or "поворот" in purpose:
            score = 4
        elif "end" in purpose or "финал" in purpose:
            score = 3
        elif "escalat" in purpose or "кульмин" in purpose:
            score = 2
        if score > best_score:
            best_score = score
            best_offset = offset
    if best_score <= 1 and len(images) > 1:
        best_offset = min(len(images) - 1, max(1, len(images) // 2))
    return images[best_offset], best_offset + 1


def _mode_label(mode: str) -> str:
    return {"low": "низкая нагрузка", "maximum": "максимальная скорость"}.get(mode, "баланс")


def _render_label(mode: str) -> str:
    return "тихий" if mode == "low_resource" else "быстрый"


def _final_ready(ffprobe: str, path: Path, audio_duration: float) -> bool:
    if not path.exists():
        return False
    try:
        info = probe(ffprobe, path)
    except FFmpegError:
        return False
    return info.has_video and info.has_audio and info.duration >= max(0.3, audio_duration - 0.8)


def _publish(settings: AppSettings, title: str, project_id: str, video: Path, thumbnail: Path, metadata: Path) -> Path:
    from dramastill.paths import default_output_dir

    output_dir = Path(settings.video.output_dir) if settings.video.output_dir else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(title, "")
    destination = output_dir / f"{stem}.mp4"
    if destination.exists():
        destination = output_dir / f"{stem}-{project_id}.mp4"
    shutil.copy2(video, destination)
    shutil.copy2(thumbnail, destination.with_suffix(".jpg"))
    shutil.copy2(metadata, destination.with_suffix(".youtube.json"))
    return destination


def _cleanup(work: Path, log) -> None:
    for relative in ("chunks", "audio/slices", "audio/wav", "audio/speech"):
        target = work / relative
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for relative in ("particles.mp4", "particles.json", "audio/bands.npy", "audio/bands.json"):
        (work / relative).unlink(missing_ok=True)
    for path in work.rglob("*.partial*"):
        path.unlink(missing_ok=True)
    log("info", "Временные фрагменты удалены. История, изображение, озвучка и MP4 сохранены")


def _accumulate_elapsed(db: Database, project_id: str) -> None:
    project = db.get(project_id)
    if project is None or not project.started_at:
        return
    from datetime import datetime, timezone

    try:
        started = datetime.fromisoformat(project.started_at)
    except ValueError:
        return
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = project.elapsed_seconds + max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    db.update(project_id, elapsed_seconds=elapsed, started_at=None)
