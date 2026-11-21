from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from dramastill.cancel import CancelToken
from dramastill.config.defaults import StorySettings, language_instruction
from dramastill.providers.base import LLMProvider
from dramastill.providers.errors import ProviderError
from dramastill.story.prompts import (
    SYSTEM,
    chapter_prompt,
    concept_prompt,
    consistency_prompt,
    continue_prompt,
    metadata_prompt,
)
from dramastill.story.textutil import drop_repeated_sentences, extract_json, prepare_narration, salvage_narration
from dramastill.util import count_words

Log = Callable[[str, str], None]
Progress = Callable[[float, str], None]
FANOUT = 4


class _Pulse:
    """Пишет в лог, пока долгий запрос ещё не вернулся."""

    def __init__(self, log: Log, message: str, every: float = 25.0):
        self._log = log
        self._message = message
        self._every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drama-pulse", daemon=True)

    def __enter__(self) -> _Pulse:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._stop.set()
        self._thread.join(timeout=1)
        return False

    def _run(self) -> None:
        waited = 0.0
        while not self._stop.wait(self._every):
            waited += self._every
            self._log("info", f"{self._message}, прошло {int(waited)} с")


def load_story(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_story(path: Path, story: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_story(
    *,
    settings: StorySettings,
    llm: LLMProvider,
    path: Path,
    narration_path: Path,
    token: CancelToken,
    log: Log,
    progress: Progress,
    temperature: float,
    max_tokens: int,
    on_chapter_final: Callable[[dict], None] | None = None,
) -> dict:
    _bind_llm(llm, token, log)
    story = load_story(path)
    target_words = max(80, int(settings.duration_minutes * settings.words_per_minute))
    if not _concept_ready(story):
        token.check()
        log("info", "Модель придумывает оригинальную историю и один драматический момент")
        story = _generate_concept(llm, settings, target_words, temperature, max_tokens, log)
        save_story(path, story)
    outlines = story.get("chapters") or []
    total = max(1, len(outlines))
    per_chapter = max(40, target_words // total)
    lock = threading.Lock()

    def _note() -> None:
        written = sum(1 for chapter in outlines if (chapter.get("narration") or "").strip())
        checked = sum(1 for chapter in outlines if chapter.get("consistency_done"))
        if settings.consistency_check:
            fraction = 0.18 + 0.62 * (written / total) + 0.20 * (checked / total)
        else:
            fraction = 0.18 + 0.82 * (written / total)
        progress(min(0.99, fraction), "story")

    _note()
    pending = [chapter for chapter in outlines if not (chapter.get("narration") or "").strip()]
    if pending:
        workers = min(FANOUT, len(pending))
        log("info", f"Главы пишутся одновременно: {workers}. Ошибка одной главы не стирает остальные")

        def write(chapter: dict) -> None:
            token.check()
            index = outlines.index(chapter)
            number = chapter.get("index", index + 1)
            log("info", f"Пишется глава {number} из {len(outlines)}")
            previous = [_chapter_context(item) for item in outlines[:index]]
            narration = ""
            summary = ""
            last_error: Exception | None = None
            for attempt in range(1, 4):
                token.check()
                try:
                    narration, summary = _write_chapter(
                        llm, settings, story, chapter, previous, per_chapter, temperature, max_tokens, log
                    )
                    last_error = None
                    break
                except ProviderError as exc:
                    last_error = exc
                    log("warning", f"Глава {number}, попытка {attempt} из 3: {exc}")
            if last_error is not None or not narration.strip():
                raise ProviderError(str(last_error or f"Глава {number} не получила текст"))
            with lock:
                chapter["narration"] = narration
                if summary:
                    chapter["summary_actual"] = summary
                save_story(path, story)
                _note()
            if not settings.consistency_check:
                _publish_ready(on_chapter_final, outlines, settings)

        _parallel(token, workers, pending, write)

    if settings.consistency_check and not story.get("consistency_done"):
        language_line = language_instruction(settings.language, settings.custom_language)
        pending_checks = [chapter for chapter in outlines if not chapter.get("consistency_done")]
        if pending_checks:
            workers = min(FANOUT, len(pending_checks))
            log("info", f"Проверка последовательности одновременно: {workers}")

            def check(chapter: dict) -> None:
                token.check()
                index = outlines.index(chapter)
                number = chapter.get("index", index + 1)
                log("info", f"Проверка последовательности, глава {number}: жду ответ модели")
                try:
                    with _Pulse(log, f"Глава {number}: модель ещё отвечает"):
                        checked = _complete_json(
                            llm,
                            system=SYSTEM,
                            user=consistency_prompt(story, chapter, language_line),
                            temperature=0.2,
                            max_tokens=max_tokens,
                            log=log,
                            allow_plain=True,
                        )
                except ProviderError as exc:
                    log("warning", f"Глава {number} оставлена как есть: {exc}")
                    checked = {}
                changed = bool(checked.get("changed") and (checked.get("narration") or "").strip())
                with lock:
                    if changed:
                        chapter["narration"] = prepare_narration(str(checked["narration"]))
                        if checked.get("summary"):
                            chapter["summary_actual"] = str(checked["summary"]).strip()
                    chapter["consistency_done"] = True
                    save_story(path, story)
                    _note()
                if changed:
                    log("info", f"Глава {number} поправлена после проверки")
                else:
                    log("info", f"Глава {number} проверена, текст оставлен")
                _publish_ready(on_chapter_final, outlines, settings)

            _parallel(token, workers, pending_checks, check)
            log("info", f"Проверка последовательности закончена: {len(outlines)} глав")
        story["consistency_done"] = True
        save_story(path, story)

    narration = "\n\n".join(prepare_narration(chapter.get("narration") or "") for chapter in outlines)
    narration = narration.strip()
    if count_words(narration) < 40:
        raise ProviderError("Сценарий получился пустым")
    narration_path.write_text(narration, encoding="utf-8")
    story["narration"] = narration
    save_story(path, story)
    log("info", f"Текст всех глав собран: {count_words(narration)} слов")
    progress(1.0, "story")
    return story


def _publish_ready(callback: Callable[[dict], None] | None, outlines: list, settings: StorySettings) -> None:
    if callback is None:
        return
    ready = []
    for chapter in outlines:
        if not (chapter.get("narration") or "").strip():
            break
        if settings.consistency_check and not chapter.get("consistency_done"):
            break
        ready.append(dict(chapter))
    if ready:
        callback({"chapters": ready})


def _chapter_context(chapter: dict) -> dict:
    return {
        "index": chapter.get("index"),
        "summary": chapter.get("summary_actual") or chapter.get("summary") or "",
        "narration": chapter.get("narration") or "",
    }


def _parallel(token: CancelToken, workers: int, items: list, job: Callable) -> None:
    if workers <= 1 or len(items) <= 1:
        for item in items:
            token.check()
            job(item)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, item) for item in items]
        for future in as_completed(futures):
            token.check()
            future.result()


def ensure_metadata(
    *,
    story: dict,
    llm: LLMProvider,
    path: Path,
    settings: StorySettings,
    temperature: float,
    max_tokens: int,
    token: CancelToken,
    log: Log,
) -> dict:
    _bind_llm(llm, token, log)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if existing.get("titles") and existing.get("description") and existing.get("thumbnail_text"):
            return existing
    token.check()
    log("info", "Модель пишет название, описание и теги")
    data = _complete_json(
        llm,
        system=SYSTEM,
        user=metadata_prompt(
            story,
            story.get("narration") or "",
            language_instruction(settings.language, settings.custom_language),
        ),
        temperature=min(0.8, temperature),
        max_tokens=min(2500, max_tokens),
        log=log,
    )
    titles = [str(item).strip() for item in data.get("titles") or [] if str(item).strip()]
    if not titles and story.get("title"):
        titles = [str(story["title"])]
    metadata = {
        "titles": titles[:5],
        "description": str(data.get("description") or story.get("logline") or "").strip(),
        "tags": [str(item).strip().lstrip("#") for item in data.get("tags") or [] if str(item).strip()],
        "thumbnail_text": str(data.get("thumbnail_text") or story.get("thumbnail_text") or story.get("title") or "").strip(),
    }
    if not metadata["titles"] or not metadata["description"] or not metadata["thumbnail_text"]:
        raise ProviderError("Модель не собрала материалы для YouTube")
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _bind_llm(llm, token: CancelToken, log: Log) -> None:
    if hasattr(llm, "on_wait"):
        llm.on_wait = lambda message: log("info", message)
    if hasattr(llm, "stop_check"):
        llm.stop_check = token.check


def _generate_concept(llm, settings, target_words, temperature, max_tokens, log: Log) -> dict:
    last_error = "Модель не собрала концепцию истории"
    for _attempt in range(3):
        data = _complete_json(
            llm,
            system=SYSTEM,
            user=concept_prompt(settings, target_words),
            temperature=temperature,
            max_tokens=min(max_tokens, 6000),
            log=log,
        )
        try:
            return _normalize_concept(data)
        except ValueError as exc:
            last_error = str(exc)
            log("warning", last_error)
    raise ProviderError(last_error)


def _normalize_concept(data: dict) -> dict:
    characters = []
    for person in data.get("characters") or []:
        if not isinstance(person, dict):
            continue
        appearance = str(person.get("appearance") or "").strip()
        name = str(person.get("name") or "").strip()
        role = str(person.get("role") or "").strip()
        wardrobe = str(person.get("wardrobe") or "").strip()
        appearance = " ".join(part for part in (appearance, wardrobe, role) if part)
        appearance = " ".join(appearance.split())
        if name and len(appearance) < 40:
            appearance = f"{appearance} Adult with a realistic face, natural skin, and ordinary clothes.".strip()
        if not name or len(appearance) < 40:
            continue
        characters.append(
            {
                "name": name,
                "role": str(person.get("role") or "").strip(),
                "age": person.get("age") or "",
                "appearance": appearance,
                "wardrobe": str(person.get("wardrobe") or "").strip(),
                "personality": str(person.get("personality") or "").strip(),
            }
        )
    chapters = []
    for index, chapter in enumerate(data.get("chapters") or [], start=1):
        if not isinstance(chapter, dict):
            continue
        summary = str(chapter.get("summary") or chapter.get("title") or "").strip()
        if len(summary) < 8:
            continue
        chapters.append(
            {
                "index": index,
                "title": str(chapter.get("title") or f"Глава {index}").strip(),
                "summary": summary,
                "purpose": str(chapter.get("purpose") or "").strip(),
            }
        )
    moment = _dramatic_moment(data, characters, chapters)
    title = str(data.get("title") or "").strip()
    if len(characters) < 1:
        raise ValueError("В концепции нет устойчивого описания персонажа")
    if len(chapters) < 1:
        raise ValueError("В концепции нет глав")
    if len(moment) < 40:
        raise ValueError("Не выбран один драматический момент для изображения")
    if not title:
        raise ValueError("Нет заголовка истории")
    variants = [str(item).strip() for item in data.get("title_variants") or [] if str(item).strip()]
    return {
        "title": title,
        "theme": str(data.get("theme") or "").strip(),
        "logline": str(data.get("logline") or "").strip(),
        "narrative_voice": str(data.get("narrative_voice") or "third person").strip(),
        "characters": characters[:4],
        "chapters": chapters,
        "dramatic_moment": moment,
        "thumbnail_text": str(data.get("thumbnail_text") or title).strip(),
        "title_variants": variants[:3],
    }


def _dramatic_moment(data: dict, characters: list[dict], chapters: list[dict]) -> str:
    moment = str(data.get("dramatic_moment") or data.get("moment") or data.get("scene") or "").strip()
    if len(moment) >= 40:
        return " ".join(moment.split())
    names = ", ".join(str(person.get("name") or "").strip() for person in characters[:3] if person.get("name"))
    pieces = [
        moment,
        f"One cinematic 16:9 photograph of {names}." if names else "",
        str(data.get("logline") or "").strip(),
        chapters[0]["summary"] if chapters else "",
    ]
    built = " ".join(part for part in pieces if part)
    return " ".join(built.split())


def _concept_ready(story: dict) -> bool:
    return bool(story.get("chapters") and story.get("dramatic_moment") and story.get("characters"))


def _write_chapter(llm, settings, story, chapter, previous, target_words, temperature, max_tokens, log: Log) -> tuple[str, str]:
    language_line = language_instruction(settings.language, settings.custom_language)
    number = chapter.get("index")
    floor = max(30, int(target_words * 0.85))
    narration = ""
    summary = ""
    prompt = chapter_prompt(settings, story, chapter, previous, target_words)
    for attempt in range(3):
        data = _complete_json(
            llm,
            system=SYSTEM,
            user=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            log=log,
            allow_plain=True,
            required=("narration",),
        )
        candidate = drop_repeated_sentences(prepare_narration(str(data.get("narration") or "")))
        grew = count_words(candidate) > count_words(narration)
        if grew:
            narration = candidate
            summary = str(data.get("summary") or summary).strip()
        words = count_words(narration)
        log("info", f"Глава {number}: {words} слов при плане около {target_words}")
        if words >= floor:
            return narration, summary
        if attempt > 0 and not grew:
            break
        log("info", f"Глава {number} короче плана, модель дополняет её")
        prompt = continue_prompt(chapter, narration, target_words, language_line)
    if count_words(narration) < 30:
        raise ProviderError(f"Глава {number} не получила текст")
    log("warning", f"Глава {number} осталась короче плана: {count_words(narration)} слов из {target_words}")
    return narration, summary


def _complete_json(
    llm: LLMProvider,
    *,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    log: Log,
    allow_plain: bool = False,
    required: tuple[str, ...] = (),
) -> dict:
    immediate = bool(getattr(llm, "retries_immediately", lambda: True)())
    attempts = 3 if immediate else 1
    prompt = user
    last_error = "ответ не является JSON-объектом"
    for attempt in range(1, attempts + 1):
        result = llm.complete(
            system=system,
            user=prompt,
            temperature=0.1 if attempt > 1 else temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed, error = _parse_model_json(result.text, allow_plain=allow_plain, required=required)
        if parsed is not None:
            if error == "salvaged":
                log("info", "Кривой JSON починен локально, повторный запрос не нужен")
            elif attempt > 1:
                log("info", f"JSON принят с попытки {attempt}. Ошибка прошлого ответа: {error or last_error}")
            return parsed
        last_error = error or last_error
        log("warning", f"Ответ модели был не JSON, попытка {attempt} из {attempts}: {last_error}")
        if attempt == attempts:
            break
        if not immediate:
            break
        prompt = (
            "TASK: json_repair\n"
            "Return one valid JSON object only, no markdown.\n"
            f"Parser error: {last_error}\n"
            "Required fields: "
            + ", ".join(required or ("a JSON object",))
            + "\n"
            + result.text[:12000]
        )
    if not immediate:
        raise ProviderError(
            "Модель не вернула пригодный JSON. Повтор сразу пропущен, чтобы не ждать 46 секунд. "
            "Для истории без этой паузы выберите Grok и вставьте ключ с console.x.ai."
        )
    raise ProviderError(f"Модель не вернула пригодный JSON: {last_error}")


def _parse_model_json(
    text: str,
    *,
    allow_plain: bool,
    required: tuple[str, ...] = (),
) -> tuple[dict | None, str]:
    try:
        data = extract_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        data = None
        error = str(exc)
    else:
        error = ""
    if data is None:
        salvaged = salvage_narration(text)
        if salvaged is not None and (not required or str(salvaged.get("narration") or "").strip()):
            return salvaged, "salvaged"
    if data is None and allow_plain:
        prose = prepare_narration(text)
        if count_words(prose) >= 30 and "{" not in prose:
            data = {"narration": prose, "summary": "", "changed": True}
            error = ""
    if data is None:
        return None, error or "ответ не является JSON-объектом"
    missing = [key for key in required if not str(data.get(key) or "").strip()]
    if missing:
        return None, "нет полей: " + ", ".join(missing)
    return data, error
