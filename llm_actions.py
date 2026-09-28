"""Действия, которые нейросеть может попросить выполнить, когда код сам не понял просьбу.

Модель только предлагает: {"type": "lobby", "who": ["анория"], "to": "sub"}. Выполняет код, и права
проверяет тоже он — по тем же правилам, что кнопки и команды. Поэтому уговорить модель «переведи всех
в запас» бесполезно: без прав организатора код подвинет только самого просящего.

Разрешено немногое и безопасное: состав ближайшего сбора и роли линий.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord

import lobby_requests

LOBBY = "lobby"
LANE = "lane"

LOBBY_TARGETS = {"in": lobby_requests.STATUS_IN, "sub": lobby_requests.STATUS_SUB, "remove": lobby_requests.REMOVE}
LANE_OPS = {"give", "take"}
LANE_KEYS = {"top", "jungle", "mid", "adc", "support"}
# Модель пишет «автор», «я», «меня» — это тот, кто обратился к Олегу.
SELF_WORDS = {"автор", "я", "меня", "мне", "себя", "author", "me", "self"}
MAX_TARGETS = 10


@dataclass(frozen=True)
class Action:
    type: str
    who: tuple[str, ...]
    to: str = ""  # для lobby: in / sub / remove
    op: str = ""  # для lane: give / take
    lanes: tuple[str, ...] = ()


def parse(data: dict | None) -> Action | None:
    """Проверенное действие из JSON-ответа модели или None, если его нет или оно кривое."""
    if not isinstance(data, dict):
        return None
    raw = data.get("action")
    if not isinstance(raw, dict):
        return None
    who = raw.get("who")
    if isinstance(who, str):
        who = [who]
    if not isinstance(who, list):
        return None
    names = tuple(str(name).strip()[:40] for name in who if isinstance(name, str) and name.strip())[:MAX_TARGETS]
    if not names:
        return None

    kind = raw.get("type")
    if kind == LOBBY:
        to = raw.get("to")
        return Action(LOBBY, names, to=to) if to in LOBBY_TARGETS else None
    if kind == LANE:
        op = raw.get("op")
        lanes = raw.get("lanes")
        if isinstance(lanes, str):
            lanes = [lanes]
        if op not in LANE_OPS or not isinstance(lanes, list):
            return None
        keys = tuple(dict.fromkeys(lane for lane in lanes if lane in LANE_KEYS))
        return Action(LANE, names, op=op, lanes=keys) if keys else None
    return None


def resolve(
    names: tuple[str, ...],
    author: discord.Member,
    mentions: list[discord.Member],
    candidates: list[discord.Member],
) -> list[discord.Member]:
    """Ники от модели → участники сервера. Кого не узнали, того пропускаем — лучше меньше, чем не тех."""
    found: list[discord.Member] = []

    def add(member: discord.Member) -> None:
        if member not in found:
            found.append(member)

    for name in names:
        key = name.casefold().strip(" @")
        if key in SELF_WORDS:
            add(author)
            continue
        pool = list(mentions) + [m for m in candidates if m not in mentions]
        exact = [m for m in pool if key in {m.display_name.casefold(), m.name.casefold()}]
        if exact:
            add(exact[0])
            continue
        fuzzy = [m for m in pool if lobby_requests.matches(name, m)]
        # Два и больше подходящих — неоднозначно, не угадываем.
        if len(fuzzy) == 1:
            add(fuzzy[0])
    return found
