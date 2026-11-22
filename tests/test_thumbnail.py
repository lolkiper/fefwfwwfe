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


def test_metadata_prompt_asks_for_the_poster():
    text = metadata_prompt({"title": "Дом", "logline": "Продажа"}, "Невестка узнала.", "Language: Russian.")
    assert "thumbnail_lines" in text
    assert "70 to 100" in text
