"""Честная жеребьёвка капитанов: случайно, но с памятью (data/captains.json).

Чистый random.sample не помнит прошлых сборов — одни и те же люди легко выпадают по нескольку раз подряд,
а кто-то неделями не капитанит. Поэтому:
- капитаны прошлой жеребьёвки на сервере в этот раз не выпадают (если есть из кого выбрать);
- чем чаще человек был капитаном за последние HISTORY жеребьёвок, тем ниже шанс (вес 1 / (1 + раз)²);
- перезапуск жеребьёвки в том же сборе заменяет его запись, а не добавляет новую.
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import config
from storage import JsonStore

HISTORY = 20


def weights(candidates: list[int], recent: list[dict]) -> list[float]:
    counts = {user_id: 0 for user_id in candidates}
    for entry in recent[-HISTORY:]:
        for user_id in entry["pair"]:
            if user_id in counts:
                counts[user_id] += 1
    return [1 / (1 + counts[user_id]) ** 2 for user_id in candidates]


def pick(
    player_ids: list[int], recent: list[dict], rng: random.Random | None = None, count: int = 2,
    avoid: list[int] = (),
) -> list[int]:
    """count капитанов из player_ids с учётом истории recent (старые записи первыми).

    avoid — пара, которую перебрасывают: она не выпадает, если есть из кого выбрать (важнее отдыха прошлой пары)."""
    rng = rng or random
    candidates = list(player_ids)
    last = recent[-1]["pair"] if recent else []
    for excluded in (set(avoid) | set(last), set(avoid)):
        rested = [user_id for user_id in candidates if user_id not in excluded]
        if excluded and len(rested) >= count:
            candidates = rested
            break
    chosen = []
    for _ in range(min(count, len(candidates))):
        user_id = rng.choices(candidates, weights=weights(candidates, recent))[0]
        chosen.append(user_id)
        candidates = [other for other in candidates if other != user_id]
    rng.shuffle(chosen)  # кому синие, кому красные — тоже случайно
    return chosen


class CaptainHistory:
    def __init__(self, path: Path | None = None) -> None:
        self.store = JsonStore(path or Path(config.DATA_DIR) / "captains.json")

    def load(self) -> None:
        self.store.load()

    def recent(self, guild_id: int) -> list[dict]:
        return self.store.data.setdefault(str(guild_id), [])

    async def draw(self, guild_id: int, lobby_id: int | str, player_ids: list[int], count: int = 2) -> list[int]:
        """Выбирает капитанов сбора и запоминает. Повторная жеребьёвка в сборе заменяет прошлую."""
        recent = self.recent(guild_id)
        previous = [entry for entry in recent if entry.get("lobby") == lobby_id]
        recent[:] = [entry for entry in recent if entry.get("lobby") != lobby_id]
        avoid = previous[-1]["pair"] if previous else []
        pair = pick(player_ids, recent, count=count, avoid=avoid)
        recent.append({"lobby": lobby_id, "pair": pair, "at": time.time()})
        del recent[:-HISTORY]
        await self.store.save()
        return pair
