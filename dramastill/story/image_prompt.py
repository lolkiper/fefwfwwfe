from __future__ import annotations

from dramastill.config.defaults import ImageSettings, style_prompt


def build_image_prompt(story: dict, settings: ImageSettings) -> str:
    chapter = (story.get("chapters") or [{}])[0]
    return build_chapter_image_prompt(story, chapter if isinstance(chapter, dict) else {}, settings, 1, len(story.get("chapters") or []) or 1)


def build_chapter_image_prompt(story: dict, chapter: dict, settings: ImageSettings, number: int, total: int) -> str:
    """Отдельный кадр главы. Герои те же, сцена и ракурс свои."""
    lines = [
        style_prompt(settings.style),
        "Single photorealistic cinematic 16:9 film still of one continuous moment.",
        "One camera angle, one room or place, no collage, no split screen, no captions, no text, no watermark, no logo.",
        f"This is still {number} of {total}. It must show a different action, camera angle, and composition from the other stills.",
    ]
    if settings.consistency_lock:
        lines.append("The people below are the only character designs. Keep every listed trait and the same wardrobe visible and exact.")
    else:
        lines.append("Use the character descriptions as the appearance guide.")
    for person in story.get("characters") or []:
        name = person.get("name") or "Character"
        appearance = (person.get("appearance") or "").strip()
        wardrobe = (person.get("wardrobe") or "").strip()
        age = person.get("age")
        if not appearance:
            continue
        lines.append(f"{name}, age {age}: {appearance}. Wardrobe: {wardrobe}.")
    summary = str(chapter.get("summary_actual") or chapter.get("summary") or "").strip()
    narration = " ".join(str(chapter.get("narration") or "").split())
    if len(narration) > 700:
        narration = narration[:700].rsplit(" ", 1)[0]
    moment = summary or (story.get("dramatic_moment") or "").strip()
    lines.append(f"The moment in the frame: {moment}")
    if narration:
        lines.append(f"What just happened, shown in one frozen instant: {narration}")
    extra = (settings.style_extra or "").strip()
    if extra:
        lines.append(extra)
    lines.append("Emotion is readable on the faces. Hands look natural. The composition leaves a little air at the bottom of the frame.")
    return "\n".join(lines)
