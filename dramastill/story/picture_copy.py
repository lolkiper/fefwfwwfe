from __future__ import annotations

from dramastill.config.defaults import ImageSettings
from dramastill.providers.errors import ProviderError
from dramastill.story.image_prompt import build_chapter_image_prompt
from dramastill.story.textutil import extract_json


_SYSTEM = """You write a single image prompt for an original fictional drama.
Every person is an adult. No sexual content, no gore, no readable text in the picture.
Return only the requested JSON.
"""


def write_shot(llm, story: dict, chapter: dict, settings: ImageSettings, number: int, total: int, log) -> str:
    """Промпт кадра. Если модель не ответила, остаётся местное описание."""
    fallback = build_chapter_image_prompt(story, chapter, settings, number, total)
    if llm is None:
        return fallback
    model = getattr(llm, "model", "") or "модель"
    try:
        result = llm.complete(
            system=_SYSTEM,
            user=_shot_request(story, chapter, number, total),
            temperature=0.7,
            max_tokens=700,
            json_mode=True,
        )
        data = extract_json(result.text)
        prompt = " ".join(str(data.get("prompt") or "").split())
        if len(prompt) < 40:
            raise ProviderError("ответ слишком короткий")
    except (ProviderError, TypeError, ValueError, KeyError) as exc:
        log("warning", f"Глава {number}: описание кадра собрано на месте: {exc}")
        return fallback
    if "no text" not in prompt.lower():
        prompt += " No text, letters, watermark, blood, weapons, or nudity. Every adult is clothed."
    log("info", f"Глава {number}: описание кадра написала {model}")
    return prompt


def _shot_request(story: dict, chapter: dict, number: int, total: int) -> str:
    people = []
    for person in story.get("characters") or []:
        if not isinstance(person, dict):
            continue
        appearance = str(person.get("appearance") or "").strip()
        if not appearance:
            continue
        people.append(
            f"- {person.get('name')}, age {person.get('age')}: {appearance}. Wardrobe: {person.get('wardrobe') or ''}."
        )
    moment = str(chapter.get("summary_actual") or chapter.get("summary") or story.get("dramatic_moment") or "").strip()
    if len(moment) > 400:
        moment = moment[:400].rsplit(" ", 1)[0]
    return f"""TASK: still
Write one English prompt for a single photorealistic cinematic 16:9 film still.
This is still {number} of {total}. The action, camera angle, and composition must differ from the other stills.
People, keep every trait and the wardrobe:
{chr(10).join(people) if people else "Two clothed adults."}
Moment, shown through faces and the room, without graphic detail: {moment or "a tense family conversation"}
Rules: one camera, one place, no collage, no captions, no written words. Leave a little empty space along the bottom.
Return JSON: {{"prompt": "the prompt"}}
"""
