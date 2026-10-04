import io
import random

from PIL import Image

import changelog
import quiz
from cogs.patchnotes import build_embed
from cogs.quiz import in_hours


def test_answers_are_forgiving():
    assert quiz.is_correct("ари", "Ари")
    assert quiz.is_correct("  Кай'Са!! ", "Кай'Са")
    assert quiz.is_correct("каиса", "Кай'Са") is False  # слишком коротко для опечатки
    assert quiz.is_correct("Аурелион сол", "Аурелион Сол")
    assert quiz.is_correct("Аурелеон Сол", "Аурелион Сол")  # одна опечатка
    assert quiz.is_correct("AurelionSol", "Аурелион Сол", ("AurelionSol",))
    assert not quiz.is_correct("Зед", "Ари")
    assert not quiz.is_correct("", "Ари")


def test_questions_and_hints():
    champion = {"id": "Ahri", "name": "Ари", "title": "девятихвостая лиса",
                "blurb": "Ари — вастайя, которая... Ahri любит охоту."}
    title = quiz.build_question("title", champion)
    assert "девятихвостая лиса" in title.text and title.answer == "Ари"
    lore = quiz.build_question("lore", champion)
    assert "Ари" not in lore.text and "Ahri" not in lore.text
    details = {"spells": [{"name": "Сфера обмана"}, {"name": "Лисий огонь"}], "passive": {"name": "Похищение сути"}}
    spell = quiz.build_question("spell", champion, details, rng=random.Random(1))
    assert spell and "«" in spell.text
    assert quiz.hint("Аурелион Сол") == "А _ _ _ _ _ _ _   _ _ _"
    buffer = io.BytesIO()
    Image.new("RGB", (120, 120), (200, 10, 10)).save(buffer, "PNG")
    portrait = quiz.build_question("portrait", champion, portrait=buffer.getvalue(), rng=random.Random(2))
    assert Image.open(io.BytesIO(portrait.image)).size == (220, 220)
    assert quiz.build_question("spell", champion) is None


def test_quiz_hours():
    assert in_hours(12, "12-24") and in_hours(23, "12-24") and not in_hours(3, "12-24")
    assert in_hours(1, "20-3") and in_hours(21, "20-3") and not in_hours(10, "20-3")
    assert in_hours(5, "кривое")


def test_changelog_and_embed():
    latest = changelog.latest_version()
    assert latest >= 1 and changelog.entries_after(latest) == []
    entries = changelog.entries_after(latest - 1)
    embed = build_embed(entries, "Я обновилась~")
    assert f"v{latest}" in embed.title and "Я обновилась~" in embed.description and "•" in embed.description
    versions = [version for version, _date, _items in changelog.CHANGELOG]
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions)
