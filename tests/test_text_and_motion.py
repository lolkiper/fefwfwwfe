import math

import pytest

from dramastill.config.defaults import VideoSettings
from dramastill.media.motion import motion_expressions
from dramastill.media.particles import LOOP_SECONDS, Particle, build_particles, position, presence, render_particle_frame
from dramastill.story.textutil import extract_json, pack_sentences, split_sentences


def test_extract_json_from_fence():
    data = extract_json('```json\n{"title": "Дом", "n": 2,}\n```')
    assert data["title"] == "Дом"
    assert data["n"] == 2


def test_extract_json_keeps_an_inner_quote():
    raw = '{"narration": "Он сказал "нет" и вышел из дома, а потом долго молчал у окна.", "summary": "спор"}'
    data = extract_json(raw)
    assert "нет" in data["narration"]
    assert data["summary"] == "спор"


def test_extract_json_repairs_a_line_break_inside_the_text():
    data = extract_json('Пояснение\n{"narration": "первая\nвторая",}')
    assert data["narration"] == "первая\nвторая"


def test_short_concept_still_gets_one_picture_moment():
    from dramastill.story.service import _normalize_concept

    story = _normalize_concept(
        {
            "title": "Чужое имя",
            "logline": "Марина узнаёт, что завещание подписано чужой рукой.",
            "characters": [{"name": "Марина", "role": "невестка", "age": 34, "appearance": "тёмные волосы"}],
            "chapters": [{"title": "Ключ", "summary": "Она находит ключ от сейфа."}],
        }
    )
    assert len(story["dramatic_moment"]) >= 40
    assert "Марина" in story["dramatic_moment"]
    assert story["characters"][0]["name"] == "Марина"


def test_paced_model_does_not_spend_another_request_on_broken_json():
    from dramastill.providers.base import LLMResult
    from dramastill.providers.errors import ProviderError
    from dramastill.story.service import _complete_json

    class Once:
        def __init__(self):
            self.calls = 0

        def retries_immediately(self) -> bool:
            return False

        def complete(self, **_kwargs):
            self.calls += 1
            return LLMResult("это не объект")

    llm = Once()
    try:
        _complete_json(llm, system="s", user="u", temperature=0.2, max_tokens=100, log=lambda *_args: None)
    except ProviderError as exc:
        assert "46" in str(exc)
    else:
        raise AssertionError("сломанный ответ должен остановить запрос")
    assert llm.calls == 1


def test_plain_chapter_text_is_kept_without_a_second_request():
    from dramastill.providers.base import LLMResult
    from dramastill.story.service import _complete_json

    class Once:
        def __init__(self):
            self.calls = 0

        def retries_immediately(self) -> bool:
            return True

        def complete(self, **_kwargs):
            self.calls += 1
            return LLMResult(" ".join(["Марина открыла сейф и увидела чужое имя."] * 8))

    llm = Once()
    data = _complete_json(
        llm, system="s", user="u", temperature=0.2, max_tokens=100, log=lambda *_args: None, allow_plain=True
    )
    assert llm.calls == 1
    assert "Марина" in data["narration"]


def test_chapters_are_written_ten_wide_and_a_bad_check_keeps_the_text(tmp_path):
    from dramastill.cancel import CancelToken
    from dramastill.config.defaults import StorySettings
    from dramastill.providers.base import LLMResult
    from dramastill.story.service import ensure_story

    narration = " ".join(
        [
            "Марина открыла сейф и увидела чужое имя.",
            "Бумага была сложена вдвое и пахла пылью.",
            "Она узнала дату, которой не было в рассказах семьи.",
            "Ключ остался в ладони, пока лампа качалась над столом.",
            "Она не отдала бумагу, пока не сверила каждое слово.",
            "За окном была ночь, и дом слушал их молча.",
        ]
    )
    current = {"open": 0, "most": 0}
    gate = __import__("threading").Lock()

    class LLM:
        def complete(self, *, system, user, temperature, max_tokens, json_mode):
            task = user.splitlines()[0]
            if task == "TASK: concept":
                raise AssertionError("concept already exists")
            if task == "TASK: consistency":
                if "CHAPTER_INDEX: 2" in user:
                    return LLMResult("не json")
                return LLMResult('{"changed": false, "narration": "%s", "summary": "ок"}' % narration)
            with gate:
                current["open"] += 1
                current["most"] = max(current["most"], current["open"])
            try:
                __import__("time").sleep(0.1)
                return LLMResult('{"narration": "%s", "summary": "глава готова"}' % narration)
            finally:
                with gate:
                    current["open"] -= 1

    story = {
        "title": "Сейф",
        "dramatic_moment": "Marina stands at an open safe in a dark kitchen and holds a will.",
        "characters": [{"name": "Марина", "appearance": "woman of 34 with dark hair and a grey coat, tired eyes"}],
        "chapters": [
            {"index": index, "title": f"Глава {index}", "summary": "Марина делает следующий шаг в споре из-за завещания."}
            for index in range(1, 5)
        ],
    }
    path = tmp_path / "story.json"
    path.write_text(__import__("json").dumps(story, ensure_ascii=False), encoding="utf-8")
    settings = StorySettings()
    settings.consistency_check = True
    settings.duration_minutes = 1
    settings.words_per_minute = 20
    settings.chapters = 4
    result = ensure_story(
        settings=settings,
        llm=LLM(),
        path=path,
        narration_path=tmp_path / "narration.txt",
        token=CancelToken(),
        log=lambda *_args: None,
        progress=lambda *_args: None,
        temperature=0.4,
        max_tokens=2000,
    )
    assert current["most"] >= 2
    assert all((chapter.get("narration") or "").strip() for chapter in result["chapters"])
    assert result["chapters"][1]["consistency_done"] is True
    assert "Марина" in result["narration"]


def test_repeated_sentences_are_removed_once():
    from dramastill.story.textutil import drop_repeated_sentences

    text = "Марина открыла сейф. Марина открыла сейф. Она прочитала имя."
    cleaned = drop_repeated_sentences(text)
    assert cleaned.count("сейф") == 1
    assert "имя" in cleaned


def test_chapter_slices_do_not_share_one_frame_range():
    from pathlib import Path

    from dramastill.media.composer import _chapter_slices

    first = Path("a.png")
    second = Path("b.png")
    slices = _chapter_slices(100, 10, 3, [(0.0, 4.0, first), (4.0, 10.0, second)], first)
    assert slices[0][2] == first
    assert slices[-1][2] == second
    covered = sum(count for _start, count, _path in slices)
    assert covered == 100
    assert slices[0][0] == 0


def test_sentences_pack_without_losing_words():
    text = "Первая фраза длинная. Вторая короче! Третья с вопросом?"
    sentences = split_sentences(text)
    assert len(sentences) == 3
    packed = pack_sentences(sentences, 40)
    assert "Первая" in " ".join(packed)
    assert "вопросом?" in " ".join(packed)


def test_motion_rate_repeats_frames_only_at_video_rates():
    from dramastill.media.motion import motion_rate

    assert motion_rate(30) == 8
    assert motion_rate(24) == 8
    assert motion_rate(20) == 20
    assert motion_rate(12) == 12


def test_motion_expressions_have_no_commas_and_continue():
    settings = VideoSettings()
    first = "".join(motion_expressions(settings, 0, 30))
    later = "".join(motion_expressions(settings, 240, 30))
    assert "," not in first
    assert first.count("sin(") >= 8
    assert "n+0" in first
    assert "n+240" in later
    assert "zoompan" not in first


def test_particles_loop_and_avoid_the_center():
    spark = Particle(0.22, 0.7, 0.04, 0.03, 2, 1, 0.4, 1.2, 1, 0.3, 2.0, (1, 0.8, 0.4), 0.8, "spark")
    assert position(spark, 0, LOOP_SECONDS) == pytest.approx(position(spark, LOOP_SECONDS, LOOP_SECONDS))
    center = Particle(0.5, 0.4, 0, 0, 1, 1, 0, 0, 1, math.pi / 2, 2, (1, 1, 1), 1, "spark")
    edge = Particle(0.05, 0.05, 0, 0, 1, 1, 0, 0, 1, math.pi / 2, 2, (1, 1, 1), 1, "spark")
    assert presence(edge, 0, LOOP_SECONDS, 0.05, 0.05) > presence(center, 0, LOOP_SECONDS, 0.5, 0.4) * 3
    frame_start = render_particle_frame([spark], 80, 48, 0, LOOP_SECONDS, {})
    frame_end = render_particle_frame([spark], 80, 48, LOOP_SECONDS, LOOP_SECONDS, {})
    assert frame_start.shape == frame_end.shape
    assert int(abs(frame_start.astype(int) - frame_end.astype(int)).mean()) == 0


def test_flashes_are_brief_and_the_layer_loops():
    settings = VideoSettings()
    particles = build_particles(settings, "уникальный сюжет")
    sparks = [item for item in particles if item.kind == "spark"]
    flashes = [item for item in particles if item.kind == "flash"]
    assert len(sparks) >= 100
    assert len(flashes) >= 8
    flash = flashes[0]
    assert position(flash, 0, LOOP_SECONDS) == pytest.approx(position(flash, LOOP_SECONDS, LOOP_SECONDS))
    peak = Particle(0.08, 0.08, 0, 0, 1, 1, 0, 0, 1, math.pi / 2, 20, (1, 1, 1), 1, "flash")
    bright = presence(peak, 0, LOOP_SECONDS, 0.08, 0.08)
    quiet = presence(peak, 2, LOOP_SECONDS, 0.08, 0.08)
    assert bright > quiet * 4
    frame_start = render_particle_frame([peak], 80, 48, 0, LOOP_SECONDS, {})
    frame_end = render_particle_frame([peak], 80, 48, LOOP_SECONDS, LOOP_SECONDS, {})
    assert int(abs(frame_start.astype(int) - frame_end.astype(int)).mean()) == 0
