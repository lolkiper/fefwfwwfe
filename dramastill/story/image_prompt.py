from __future__ import annotations

from dramastill.config.defaults import ImageSettings, style_prompt


def build_image_prompt(story: dict, settings: ImageSettings) -> str:
    """Один промпт для единственного кадра проекта."""
    lines = [
        style_prompt(settings.style),
        "Single photorealistic cinematic 16:9 film still of one continuous moment.",
        "One camera angle, one room or place, no collage, no split screen, no captions, no text, no watermark, no logo.",
    ]
    if settings.consistency_lock:
        lines.append("The people below are the only character designs. Keep every listed trait visible and exact.")
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
    moment = (story.get("dramatic_moment") or "").strip()
    lines.append(f"The moment in the frame: {moment}")
    extra = (settings.style_extra or "").strip()
    if extra:
        lines.append(extra)
    lines.append("Emotion is readable on the faces. Hands look natural. The composition leaves a little air at the bottom of the frame.")
    return "\n".join(lines)
