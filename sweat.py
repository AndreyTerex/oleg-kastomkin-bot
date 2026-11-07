"""Потные и заечки: как Олежка относится к винрейту в кастомках.

Кастомки — чтобы играть по кайфу: LP тут нет, на мать никто не играет. Кто держит высокий винрейт
(от config.SWEAT_WINRATE при config.SWEAT_MIN_GAMES катках и больше), тот «потный» — Олежка его презирает.
Кто играет по кайфу (винрейт не выше config.CHILL_WINRATE) — молодец и заечка. Тексты — в persona.py.
"""
from __future__ import annotations

import random
from typing import Callable, Iterable

import config
import persona
from stats import Record, player_games

# Каждые столько процентов винрейта сверх порога — следующий уровень потности.
LEVEL_STEP = 0.05
# Реплика о серии потного — на 3, 6, 9… побед подряд, чтобы не на каждой катке.
STREAK_EVERY = 3
# Больше стольких реплик о потных под одной каткой не пишем.
MAX_JABS = 2


def sweat_level(record: Record) -> int:
    """0 — не потный; 1…len(SWEAT_TITLES) — насколько."""
    threshold = config.SWEAT_WINRATE
    if threshold <= 0 or record.games < config.SWEAT_MIN_GAMES or record.winrate < threshold:
        return 0
    steps = int(round((record.winrate - threshold) / LEVEL_STEP, 6))
    return min(len(persona.SWEAT_TITLES), 1 + steps)


def title(record: Record) -> str | None:
    level = sweat_level(record)
    return persona.SWEAT_TITLES[level - 1] if level else None


def is_chill(record: Record) -> bool:
    """Играет по кайфу: каток достаточно, а винрейт не выше CHILL_WINRATE."""
    return (
        config.SWEAT_WINRATE > 0 and record.games >= config.SWEAT_MIN_GAMES
        and record.winrate <= config.CHILL_WINRATE
    )


def context_note(record: Record) -> str:
    """Пометка для нейросети рядом со статистикой игрока."""
    found = title(record)
    if found:
        return f"; ПОТНЫЙ («{found}», винрейт {record.winrate:.0%} в кастомках) — презирай и жёстко унижай за потение"
    if is_chill(record):
        return "; играет по кайфу — молодец и заечка, хвали"
    return ""


def chief(players: dict[int, Record], skip: Iterable[int] = ()) -> int | None:
    """Главный потный сервера: самый высокий винрейт среди потных."""
    skip = set(skip)
    sweaty = [(uid, r) for uid, r in players.items() if uid not in skip and sweat_level(r)]
    if not sweaty:
        return None
    return max(sweaty, key=lambda item: (item[1].winrate, item[1].games))[0]


def streaks(games: list[dict], user_id: int) -> tuple[int, int]:
    """(серия побед подряд сейчас, серия побед перед последней каткой)."""
    results = [won for won, _delta in player_games(games, user_id)]

    def tail(items: list[bool]) -> int:
        count = 0
        for won in reversed(items):
            if not won:
                break
            count += 1
        return count

    return tail(results), tail(results[:-1])


def game_jabs(
    games: list[dict], winners: list[int], losers: list[int], players: dict[int, Record],
    name: Callable[[int], str], skip: Iterable[int] = (),
) -> list[str]:
    """Реплики Олежки под результатом катки: серия потного, конец серии, проигрыш главного потного.

    games — история уже с этой каткой, players — статистика уже с ней же.
    """
    skip = set(skip)
    boss = chief(players, skip)
    lines = []
    for user_id in winners:
        record = players.get(user_id, Record())
        current, _before = streaks(games, user_id)
        if user_id in skip or not sweat_level(record):
            continue
        if current >= STREAK_EVERY and current % STREAK_EVERY == 0:
            lines.append(random.choice(persona.SWEAT_WIN_STREAK).format(
                name=name(user_id), streak=current, winrate=f"{record.winrate:.0%}",
            ))
    for user_id in losers:
        record = players.get(user_id, Record())
        if user_id in skip or not sweat_level(record):
            continue
        _current, before = streaks(games, user_id)
        if before >= STREAK_EVERY:
            lines.append(random.choice(persona.SWEAT_STREAK_BROKEN).format(name=name(user_id), streak=before))
        elif user_id == boss:
            lines.append(random.choice(persona.SWEAT_CHIEF_LOST).format(name=name(user_id)))
    return lines[:MAX_JABS]
