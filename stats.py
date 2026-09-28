"""Статистика каток: кто сколько раз играл и побеждал. Хранится в data/stats.json отдельно по серверам.

Результат записывает организатор кнопкой в посте сбора. По статистике бот умеет уравнивать команды,
а Олег шутит на реальных цифрах.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from storage import JsonStore

# Сколько последних каток хранить подробно (для отмены и истории). Счётчики игроков не обрезаются.
HISTORY_LIMIT = 500


@dataclass(frozen=True)
class Record:
    wins: int = 0
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def rating(self) -> float:
        """Сглаженный винрейт: у новичка 50%, одна победа не делает из игрока бога (правило Лапласа)."""
        return (self.wins + 1) / (self.games + 2)

    def describe(self) -> str:
        if not self.games:
            return "ещё не играл"
        return (
            f"{self.games} {plural(self.games, 'катка', 'катки', 'каток')}, "
            f"{self.wins} {plural(self.wins, 'победа', 'победы', 'побед')} ({self.winrate:.0%})"
        )


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class Stats:
    def __init__(self, path: Path) -> None:
        self.store = JsonStore(path)

    def load(self) -> None:
        self.store.load()

    def _guild(self, guild_id: int) -> dict:
        guild = self.store.data.setdefault(str(guild_id), {})
        guild.setdefault("players", {})
        guild.setdefault("games", [])
        guild.setdefault("next_id", 1)
        return guild

    def player(self, guild_id: int, user_id: int) -> Record:
        entry = self._guild(guild_id)["players"].get(str(user_id)) or {}
        return Record(entry.get("wins", 0), entry.get("losses", 0))

    def all_players(self, guild_id: int) -> dict[int, Record]:
        return {
            int(user_id): Record(entry.get("wins", 0), entry.get("losses", 0))
            for user_id, entry in self._guild(guild_id)["players"].items()
        }

    def games_count(self, guild_id: int) -> int:
        return len(self._guild(guild_id)["games"])

    async def record_game(
        self, guild_id: int, winners: list[int], losers: list[int], *, lobby_id: int | None = None, round_no: int = 1
    ) -> int:
        """Записывает катку и возвращает её номер — по нему катку можно отменить."""
        guild = self._guild(guild_id)
        game_id = guild["next_id"]
        guild["next_id"] += 1
        for user_id, key in [(u, "wins") for u in winners] + [(u, "losses") for u in losers]:
            entry = guild["players"].setdefault(str(user_id), {"wins": 0, "losses": 0})
            entry[key] = entry.get(key, 0) + 1
        guild["games"].append({
            "id": game_id, "at": time.time(), "winners": winners, "losers": losers,
            "lobby": lobby_id, "round": round_no,
        })
        del guild["games"][:-HISTORY_LIMIT]
        await self.store.save()
        return game_id

    async def undo_game(self, guild_id: int, game_id: int) -> bool:
        guild = self._guild(guild_id)
        game = next((g for g in guild["games"] if g["id"] == game_id), None)
        if game is None:
            return False
        for user_id, key in [(u, "wins") for u in game["winners"]] + [(u, "losses") for u in game["losers"]]:
            entry = guild["players"].get(str(user_id))
            if entry and entry.get(key, 0) > 0:
                entry[key] -= 1
        guild["games"].remove(game)
        await self.store.save()
        return True
