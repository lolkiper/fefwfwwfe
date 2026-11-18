from __future__ import annotations

from dramastill.config.defaults import StorySettings, language_instruction, theme_label


SYSTEM = """You write original long-form fiction for a spoken YouTube drama.
Never copy an existing video, film, book, article, or real private person.
Every character is an adult.
Do not write sexual content, graphic gore, or instructions for real crime.
The result is narration that a voice actor can read aloud.
Return only the requested format. Do not add a slideshow, scene list, or extra images.
Put that format in the reply itself. Do not spend the reply on analysis.
"""


def concept_prompt(settings: StorySettings, words: int) -> str:
    idea = settings.idea.strip()
    idea_line = idea or "Invent the idea yourself. Do not ask for one."
    theme = theme_label(settings.theme) if settings.theme != "auto" else "Choose the strongest theme yourself from family conflict, secrets, betrayal, poverty and wealth, inheritance, tragedy, love and reconciliation, or social drama."
    return f"""TASK: concept
{language_instruction(settings.language, settings.custom_language)}
Theme: {theme}
Target spoken length: about {words} words, {settings.chapters} chapters.
User idea (optional): {idea_line}

Invent an original story. Return one JSON object with this shape:
{{
  "title": "working title in the audience language, under 80 characters",
  "theme": "short theme name in the audience language",
  "logline": "one sentence in the audience language",
  "narrative_voice": "first or third, and stay with it",
  "characters": [
    {{
      "name": "name in the audience language",
      "role": "role in the audience language",
      "age": 35,
      "appearance": "detailed stable physical description IN ENGLISH: face, age cues, hair, eyes, skin, build, distinguishing features",
      "wardrobe": "specific clothing IN ENGLISH",
      "personality": "short, audience language"
    }}
  ],
  "chapters": [
    {{
      "index": 1,
      "title": "chapter title in the audience language",
      "summary": "what happens, 3-5 sentences, audience language",
      "purpose": "hook, escalation, turn, or ending"
    }}
  ],
  "dramatic_moment": "ONE visual moment IN ENGLISH that can be a single 16:9 photograph. Name who is in the frame and what just happened. No collage.",
  "thumbnail_text": "3 to 6 emotional words in the audience language",
  "title_variants": ["three different YouTube titles in the audience language"]
}}

Rules:
- Include 2 to 4 adult characters. Give each a unique, concrete appearance.
- The first chapter summary must open on conflict or a secret, not on weather or a slow biography.
- The last chapter must contain a turn and a resolution.
- Do not propose more than one photograph.
- Keep names, ages, and relationships consistent.
"""


def chapter_prompt(settings: StorySettings, story: dict, chapter: dict, previous: list[dict], target_words: int) -> str:
    bible = []
    for person in story.get("characters") or []:
        bible.append(
            f"- {person.get('name')} ({person.get('role')}, age {person.get('age')}): {person.get('personality')}"
        )
    prior = []
    for item in previous:
        prior.append(f"Chapter {item.get('index')}: {item.get('summary')}")
    tail = ""
    if previous:
        narration = previous[-1].get("narration") or ""
        words = narration.split()
        tail = " ".join(words[-180:])
    return f"""TASK: chapter
CHAPTER_INDEX: {chapter.get("index")}
{language_instruction(settings.language, settings.custom_language)}
Write only the spoken narration for this chapter. Target length: {target_words} words.
Narrative voice: {story.get("narrative_voice")}
Title: {story.get("title")}
Characters:
{chr(10).join(bible)}
Previous chapters:
{chr(10).join(prior) if prior else "This is the opening chapter. Start with a hook."}
Last lines already spoken:
{tail or "None."}
This chapter: {chapter.get("title")}
What must happen: {chapter.get("summary")}
Purpose: {chapter.get("purpose")}

Return JSON:
{{"narration": "pure spoken prose", "summary": "what actually happened, 2-4 sentences"}}

The narration is read aloud. No headings, no speaker labels, no markdown, no stage directions, no camera words.
Write numbers as words. Do not mention images, chapters, or the viewer subscription.
Do not contradict earlier facts.
"""


def continue_prompt(chapter: dict, narration: str, target_words: int, language_line: str) -> str:
    return f"""TASK: chapter_continue
CHAPTER_INDEX: {chapter.get("index")}
{language_line}
The narration below is unfinished. Rewrite it as the FULL chapter, about {target_words} words, keeping the events already told and continuing until the chapter summary is complete.
Chapter purpose: {chapter.get("summary")}
Current text:
{narration}

Return JSON: {{"narration": "full chapter", "summary": "2-4 sentences"}}
"""


def consistency_prompt(story: dict, chapter: dict, language_line: str) -> str:
    facts = {
        "title": story.get("title"),
        "voice": story.get("narrative_voice"),
        "characters": [
            {
                "name": person.get("name"),
                "age": person.get("age"),
                "role": person.get("role"),
            }
            for person in story.get("characters") or []
        ],
        "outline": [
            {"index": item.get("index"), "summary": item.get("summary")}
            for item in story.get("chapters") or []
        ],
    }
    return f"""TASK: consistency
CHAPTER_INDEX: {chapter.get("index")}
{language_line}
Check this chapter against the fact sheet. If names, ages, motives, timeline, or objects contradict the sheet or earlier summaries, return the corrected full narration. If it is consistent, return it unchanged.
Fact sheet:
{facts}
Narration:
{chapter.get("narration")}

Return JSON: {{"changed": false, "narration": "...", "summary": "..."}}
Set changed to true only when you edit the narration.
"""


def metadata_prompt(story: dict, narration: str, language_line: str) -> str:
    excerpt = narration[:6000]
    return f"""TASK: metadata
{language_line}
Create YouTube metadata for this original fictional drama.
Working title: {story.get("title")}
Logline: {story.get("logline")}
Narration excerpt:
{excerpt}

Return JSON:
{{
  "titles": ["five different titles under 70 characters"],
  "description": "two short paragraphs, compelling and honest, plus one line that the story is original fiction",
  "tags": ["12 to 20 tags without hash marks"],
  "thumbnail_text": "3 to 6 emotional words"
}}
Do not claim the story is a true documentary.
"""
