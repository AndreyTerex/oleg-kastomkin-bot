"""Викторина по League of Legends: вопросы из Riot Data Dragon и проверка ответов."""
from __future__ import annotations

import io
import random
import re
from dataclasses import dataclass

from PIL import Image

QUESTION_KINDS = ("title", "spell", "portrait", "lore")
SPELL_KEYS = ("Q", "W", "E", "R")


@dataclass
class Question:
    kind: str
    text: str
    answer: str
    champion_id: str
    hint: str = ""
    image: bytes | None = None


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", (text or "").casefold().replace("ё", "е"))


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def is_correct(guess: str, answer: str, aliases: tuple[str, ...] = ()) -> bool:
    """Верно ли: регистр, пробелы и знаки не важны, одна опечатка в длинном имени прощается."""
    attempt = normalize(guess)
    if not attempt:
        return False
    for option in (answer, *aliases):
        target = normalize(option)
        if attempt == target:
            return True
        if len(target) >= 6 and _distance(attempt, target) <= 1:
            return True
    return False


def hint(answer: str) -> str:
    """Подсказка: первая буква и длина, остальное — подчёркивания (пробелы и дефисы видны)."""
    letters = [char if not char.isalpha() else "_" for char in answer]
    if letters:
        letters[0] = answer[0]
    return " ".join(letters)


def mask_name(text: str, names: list[str]) -> str:
    """Убирает имя чемпиона из описания, чтобы не подсказывать ответ."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), "███", text, flags=re.IGNORECASE)
    return text


def crop_portrait(data: bytes, size: int = 44, scale: int = 5, rng: random.Random | None = None) -> bytes:
    """Случайный кусочек портрета, увеличенный, — угадать по нему чемпиона."""
    rng = rng or random
    image = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = image.size
    size = min(size, width, height)
    left, top = rng.randint(0, width - size), rng.randint(0, height - size)
    piece = image.crop((left, top, left + size, top + size)).resize((size * scale, size * scale), Image.NEAREST)
    buffer = io.BytesIO()
    piece.save(buffer, "PNG")
    return buffer.getvalue()


def build_question(kind: str, champion: dict, details: dict | None = None, portrait: bytes | None = None,
                   rng: random.Random | None = None) -> Question | None:
    """Вопрос из данных Data Dragon. champion — запись из champion.json, details — из champion/<id>.json."""
    rng = rng or random
    name, champion_id = champion["name"], champion["id"]
    if kind == "title" and champion.get("title"):
        return Question(kind, f"Чей это титул: **«{champion['title']}»**?", name, champion_id, hint(name))
    if kind == "lore" and champion.get("blurb"):
        blurb = mask_name(champion["blurb"], [name, champion_id])
        blurb = blurb[:350].rsplit(" ", 1)[0] + "…" if len(blurb) > 350 else blurb
        return Question(kind, f"Кто это?\n> {blurb}", name, champion_id, hint(name))
    if kind == "spell" and details:
        spells = details.get("spells") or []
        options = [(key, spell.get("name")) for key, spell in zip(SPELL_KEYS, spells) if spell.get("name")]
        passive = (details.get("passive") or {}).get("name")
        if passive:
            options.append(("пассивка", passive))
        if options:
            key, spell = rng.choice(options)
            return Question(kind, f"Чьё это умение ({key}): **«{spell}»**?", name, champion_id, hint(name))
    if kind == "portrait" and portrait:
        return Question(kind, "Чей это кусочек портрета? 🔍", name, champion_id, hint(name),
                        image=crop_portrait(portrait, rng=rng))
    return None
