from __future__ import annotations

import json
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
from dramastill.story.textutil import extract_json, prepare_narration
from dramastill.util import count_words

Log = Callable[[str, str], None]
Progress = Callable[[float, str], None]


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
) -> dict:
    _bind_llm(llm, token, log)
    story = load_story(path)
    target_words = max(80, int(settings.duration_minutes * settings.words_per_minute))
    if not _concept_ready(story):
        token.check()
        log("info", "Модель придумывает оригинальную историю и один драматический момент")
        story = _generate_concept(llm, settings, target_words, temperature, max_tokens, log)
        save_story(path, story)
    progress(0.18, "story")

    outlines = story.get("chapters") or []
    per_chapter = max(80, target_words // max(1, len(outlines)))
    for index, chapter in enumerate(outlines):
        token.check()
        if (chapter.get("narration") or "").strip():
            continue
        log("info", f"Пишется глава {chapter.get('index', index + 1)} из {len(outlines)}")
        previous = outlines[:index]
        narration, summary = _write_chapter(
            llm, settings, story, chapter, previous, per_chapter, temperature, max_tokens, log
        )
        chapter["narration"] = narration
        if summary:
            chapter["summary_actual"] = summary
        save_story(path, story)
        progress(0.18 + 0.62 * ((index + 1) / len(outlines)), "story")

    if settings.consistency_check and not story.get("consistency_done"):
        language_line = language_instruction(settings.language, settings.custom_language)
        for index, chapter in enumerate(outlines):
            token.check()
            if chapter.get("consistency_done"):
                continue
            log("info", f"Проверка последовательности, глава {chapter.get('index', index + 1)}")
            checked = _complete_json(
                llm,
                system=SYSTEM,
                user=consistency_prompt(story, chapter, language_line),
                temperature=0.2,
                max_tokens=max_tokens,
                log=log,
            )
            if checked.get("changed") and (checked.get("narration") or "").strip():
                chapter["narration"] = prepare_narration(str(checked["narration"]))
                if checked.get("summary"):
                    chapter["summary_actual"] = str(checked["summary"]).strip()
                log("info", f"Глава {chapter.get('index', index + 1)} поправлена после проверки")
            chapter["consistency_done"] = True
            save_story(path, story)
            progress(0.82 + 0.18 * ((index + 1) / len(outlines)), "story")
        story["consistency_done"] = True
        save_story(path, story)

    narration = "\n\n".join(prepare_narration(chapter.get("narration") or "") for chapter in outlines)
    narration = narration.strip()
    if count_words(narration) < 40:
        raise ProviderError("Сценарий получился пустым")
    narration_path.write_text(narration, encoding="utf-8")
    story["narration"] = narration
    save_story(path, story)
    progress(1.0, "story")
    return story


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
        summary = str(chapter.get("summary") or "").strip()
        if len(summary) < 20:
            continue
        chapters.append(
            {
                "index": index,
                "title": str(chapter.get("title") or f"Глава {index}").strip(),
                "summary": summary,
                "purpose": str(chapter.get("purpose") or "").strip(),
            }
        )
    moment = str(data.get("dramatic_moment") or "").strip()
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


def _concept_ready(story: dict) -> bool:
    return bool(story.get("chapters") and story.get("dramatic_moment") and story.get("characters"))


def _write_chapter(llm, settings, story, chapter, previous, target_words, temperature, max_tokens, log: Log) -> tuple[str, str]:
    language_line = language_instruction(settings.language, settings.custom_language)
    data = _complete_json(
        llm,
        system=SYSTEM,
        user=chapter_prompt(settings, story, chapter, previous, target_words),
        temperature=temperature,
        max_tokens=max_tokens,
        log=log,
    )
    narration = prepare_narration(str(data.get("narration") or ""))
    summary = str(data.get("summary") or "").strip()
    if count_words(narration) < int(target_words * 0.55):
        log("info", f"Глава {chapter.get('index')} короче плана, модель дополняет её")
        expanded = _complete_json(
            llm,
            system=SYSTEM,
            user=continue_prompt(chapter, narration, target_words, language_line),
            temperature=temperature,
            max_tokens=max_tokens,
            log=log,
        )
        longer = prepare_narration(str(expanded.get("narration") or ""))
        if count_words(longer) > count_words(narration):
            narration = longer
            summary = str(expanded.get("summary") or summary).strip()
    if count_words(narration) < 30:
        raise ProviderError(f"Глава {chapter.get('index')} не получила текст")
    return narration, summary


def _complete_json(llm: LLMProvider, *, system: str, user: str, temperature: float, max_tokens: int, log: Log) -> dict:
    result = llm.complete(
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=True,
    )
    try:
        return extract_json(result.text)
    except (ValueError, json.JSONDecodeError):
        log("warning", "Ответ модели был не JSON, повторяю запрос")
        repaired = llm.complete(
            system=system,
            user="TASK: json_repair\nReturn one valid JSON object only, no markdown.\n" + result.text[:12000],
            temperature=0.1,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            return extract_json(repaired.text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("Модель не вернула пригодный JSON") from exc
