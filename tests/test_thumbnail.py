import numpy as np
from PIL import Image

from dramastill.media.thumbnail import compose_thumbnail, poster_lines
from dramastill.story.prompts import metadata_prompt
from dramastill.story.service import _metadata_from


def test_poster_lines_paint_the_hook_yellow_and_the_blow_red():
    rows = poster_lines(
        "",
        [
            {"text": "Свекровь", "tone": "yellow"},
            {"text": "продала дом", "tone": "white"},
            {"text": "невестки", "tone": "white"},
            {"text": "тайком!", "tone": "red"},
        ],
    )
    assert [tone for _text, tone in rows] == ["yellow", "white", "white", "red"]


def test_a_plain_phrase_becomes_four_poster_lines():
    rows = poster_lines("Свекровь продала дом невестки тайком от всех")
    assert len(rows) == 4
    assert rows[0][1] == "yellow"
    assert rows[-1][1] == "red"
    assert all("," not in tone for _text, tone in rows)


def test_metadata_keeps_a_long_title_and_the_poster_lines():
    data = _metadata_from(
        {"title": "Коротко", "logline": "запас"},
        {
            "titles": ["Свекровь тайком продала дом невестки, и та узнала об этом в ту же ночь!"],
            "description": "Вымысел.",
            "thumbnail_lines": [
                {"text": "СВЕКРОВЬ", "tone": "yellow"},
                {"text": "ПРОДАЛА ДОМ", "tone": "white"},
                {"text": "НЕВЕСТКИ", "tone": "white"},
                {"text": "ТАЙКОМ!", "tone": "red"},
            ],
        },
    )
    assert data["titles"][0].endswith("!")
    assert len(data["titles"][0]) <= 100
    assert data["thumbnail_lines"][-1] == {"text": "ТАЙКОМ!", "tone": "red"}


def test_thumbnail_image_carries_yellow_and_red(tmp_path):
    source = tmp_path / "still.png"
    Image.new("RGB", (640, 360), (28, 32, 40)).save(source)
    dest = tmp_path / "thumb.jpg"
    compose_thumbnail(
        source,
        "СВЕКРОВЬ ПРОДАЛА ДОМ",
        dest,
        lines=[
            {"text": "СВЕКРОВЬ", "tone": "yellow"},
            {"text": "ПРОДАЛА ДОМ", "tone": "white"},
            {"text": "НЕВЕСТКИ", "tone": "white"},
            {"text": "ТАЙКОМ!", "tone": "red"},
        ],
    )
    image = Image.open(dest).convert("RGB")
    assert image.size == (1280, 720)
    pixels = np.asarray(image)
    reds_channel, greens, blues = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    yellow = int(((reds_channel > 200) & (greens > 150) & (blues < 90)).sum())
    reds = int(((reds_channel > 180) & (greens < 90) & (blues < 90)).sum())
    assert yellow > 400
    assert reds > 200
    top = image.crop((0, 0, 1280, 160))
    assert sum(top.resize((1, 1)).getpixel((0, 0))) > 40


def test_tokenlavka_writes_the_shot_and_a_failure_stays_local():
    from dramastill.providers.base import LLMResult
    from dramastill.story.picture_copy import write_shot

    class Writer:
        model = "economy"

        def complete(self, *, system, user, temperature, max_tokens, json_mode):
            return LLMResult('{"prompt": "A clothed woman of 34 in a grey coat holds a will in a dim kitchen, one camera, no text."}')

    story = {"characters": [{"name": "Марина", "age": 34, "appearance": "dark hair, oval face, tired eyes, light olive skin, slim build", "wardrobe": "grey coat"}], "dramatic_moment": "kitchen"}
    chapter = {"summary": "Она находит завещание."}
    settings = __import__("dramastill.config.defaults", fromlist=["ImageSettings"]).ImageSettings()
    notes = []
    prompt = write_shot(Writer(), story, chapter, settings, 1, 2, lambda *_args: notes.append(_args))
    assert "grey coat" in prompt
    assert "no text" in prompt.lower()
    assert notes and notes[0][0] == "info"

    class Broken:
        model = "economy"

        def complete(self, *, system, user, temperature, max_tokens, json_mode):
            return LLMResult("не json")

    fallback = write_shot(Broken(), story, chapter, settings, 2, 2, lambda *_args: notes.append(_args))
    assert "Single photorealistic" in fallback
    assert notes[-1][0] == "warning"


def test_picture_model_follows_the_story_until_it_is_changed():
    from dramastill.config.defaults import AppSettings
    from dramastill.providers.factory import picture_model

    settings = AppSettings()
    settings.llm.provider = "tokenlavka"
    settings.llm.model = "claude-fable-5-1"
    assert picture_model(settings) == "claude-fable-5-1"
    settings.image.writer_model = "economy"
    assert picture_model(settings) == "economy"


def test_metadata_prompt_asks_for_the_poster():
    text = metadata_prompt({"title": "Дом", "logline": "Продажа"}, "Невестка узнала.", "Language: Russian.")
    assert "thumbnail_lines" in text
    assert "70 to 100" in text
