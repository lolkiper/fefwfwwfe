import math

import pytest

from dramastill.config.defaults import VideoSettings
from dramastill.media.motion import motion_expressions
from dramastill.media.particles import LOOP_SECONDS, Particle, position, presence, render_particle_frame
from dramastill.story.textutil import extract_json, pack_sentences, split_sentences


def test_extract_json_from_fence():
    data = extract_json('```json\n{"title": "Дом", "n": 2,}\n```')
    assert data["title"] == "Дом"
    assert data["n"] == 2


def test_sentences_pack_without_losing_words():
    text = "Первая фраза длинная. Вторая короче! Третья с вопросом?"
    sentences = split_sentences(text)
    assert len(sentences) == 3
    packed = pack_sentences(sentences, 40)
    assert "Первая" in " ".join(packed)
    assert "вопросом?" in " ".join(packed)


def test_motion_expressions_have_no_commas_and_continue():
    settings = VideoSettings()
    first = "".join(motion_expressions(settings, 0, 30))
    later = "".join(motion_expressions(settings, 240, 30))
    assert "," not in first
    assert "on+0" in first
    assert "on+240" in later


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
