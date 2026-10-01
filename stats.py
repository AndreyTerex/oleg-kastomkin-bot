"""Статистика каток: кто сколько раз играл и побеждал, Elo и MVP. Хранится в data/stats.json по серверам.

Результат записывает организатор кнопкой в посте сбора. По Elo бот уравнивает команды и строит таблицу
лидеров, а Олег шутит на реальных цифрах.

Elo командный: ожидаемый результат считается по среднему Elo команд, каждый игрок получает изменение
со своим коэффициентом (у новичков больше — быстрее находят своё место). Изменения сохраняются в катке,
поэтому отмена результата возвращает Elo точно.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from storage import JsonStore

# Сколько последних каток хранить подробно (для отмены и истории). Счётчики игроков не обрезаются.
HISTORY_LIMIT = 500
ELO_START = 1000.0
ELO_K_NEW = 40.0
ELO_K = 24.0
# Столько каток игрок считается новичком (больший коэффициент).
ELO_NEW_GAMES = 10
ELO_VERSION = 1


@dataclass(frozen=True)
class Record:
    wins: int = 0
    losses: int = 0
    elo: float = field(default=ELO_START, compare=False)
    mvp: int = field(default=0, compare=False)

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
            f"{self.wins} {plural(self.wins, 'победа', 'победы', 'побед')} ({self.winrate:.0%}), "
            f"Elo {self.elo:.0f}" + (f", MVP ×{self.mvp}" if self.mvp else "")
        )


def expected_score(own: float, other: float) -> float:
    return 1 / (1 + 10 ** ((other - own) / 400))


def elo_changes(ratings: dict[int, tuple[float, int]], winners: list[int], losers: list[int]) -> dict[int, float]:
    """Изменение Elo каждого игрока. ratings: id → (Elo, сыграно каток до этой)."""
    def average(team: list[int]) -> float:
        return sum(ratings.get(u, (ELO_START, 0))[0] for u in team) / len(team) if team else ELO_START

    win_avg, lose_avg = average(winners), average(losers)
    expected_win = expected_score(win_avg, lose_avg)
    changes = {}
    for team, gain in ((winners, 1 - expected_win), (losers, -(1 - expected_win))):
        for user_id in team:
            games = ratings.get(user_id, (ELO_START, 0))[1]
            k = ELO_K_NEW if games < ELO_NEW_GAMES else ELO_K
            changes[user_id] = round(k * gain, 1)
    return changes


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
        if guild.get("elo_version") != ELO_VERSION:
            self._replay_elo(guild)
        return guild

    @staticmethod
    def _replay_elo(guild: dict) -> None:
        """Первый запуск с Elo: пересчитываем его по сохранённой истории каток."""
        for entry in guild["players"].values():
            entry["elo"] = ELO_START
        played: dict[str, int] = {}
        for game in sorted(guild["games"], key=lambda g: g["id"]):
            ratings = {
                u: (guild["players"].get(str(u), {}).get("elo", ELO_START), played.get(str(u), 0))
                for u in game["winners"] + game["losers"]
            }
            changes = elo_changes(ratings, game["winners"], game["losers"])
            for user_id, delta in changes.items():
                entry = guild["players"].setdefault(str(user_id), {"wins": 0, "losses": 0})
                entry["elo"] = round(entry.get("elo", ELO_START) + delta, 1)
                played[str(user_id)] = played.get(str(user_id), 0) + 1
            game["elo"] = {str(u): d for u, d in changes.items()}
        guild["elo_version"] = ELO_VERSION

    @staticmethod
    def _record(entry: dict) -> Record:
        return Record(entry.get("wins", 0), entry.get("losses", 0), entry.get("elo", ELO_START), entry.get("mvp", 0))

    def player(self, guild_id: int, user_id: int) -> Record:
        return self._record(self._guild(guild_id)["players"].get(str(user_id)) or {})

    def all_players(self, guild_id: int) -> dict[int, Record]:
        return {
            int(user_id): self._record(entry)
            for user_id, entry in self._guild(guild_id)["players"].items()
        }

    def games_count(self, guild_id: int) -> int:
        return len(self._guild(guild_id)["games"])

    async def record_game(
        self, guild_id: int, winners: list[int], losers: list[int], *, lobby_id: int | None = None, round_no: int = 1,
        channel_id: int | None = None,
    ) -> int:
        """Записывает катку и возвращает её номер — по нему катку можно отменить."""
        guild = self._guild(guild_id)
        game_id = guild["next_id"]
        guild["next_id"] += 1
        ratings = {u: (self.player(guild_id, u).elo, self.player(guild_id, u).games) for u in winners + losers}
        changes = elo_changes(ratings, winners, losers)
        for user_id, delta in changes.items():
            entry = guild["players"].setdefault(str(user_id), {"wins": 0, "losses": 0})
            entry["elo"] = round(entry.get("elo", ELO_START) + delta, 1)
        for user_id, key in [(u, "wins") for u in winners] + [(u, "losses") for u in losers]:
            entry = guild["players"].setdefault(str(user_id), {"wins": 0, "losses": 0})
            entry[key] = entry.get(key, 0) + 1
        guild["games"].append({
            "id": game_id, "at": time.time(), "winners": winners, "losers": losers,
            "lobby": lobby_id, "round": round_no, "elo": {str(u): d for u, d in changes.items()},
        })
        del guild["games"][:-HISTORY_LIMIT]
        if channel_id:
            guild["last_channel"] = channel_id
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
        for user_id, delta in (game.get("elo") or {}).items():
            entry = guild["players"].get(str(user_id))
            if entry:
                entry["elo"] = round(entry.get("elo", ELO_START) - delta, 1)
        for user_id in game.get("mvp") or []:
            entry = guild["players"].get(str(user_id))
            if entry and entry.get("mvp", 0) > 0:
                entry["mvp"] -= 1
        guild["games"].remove(game)
        await self.store.save()
        return True

    def game(self, guild_id: int, game_id: int) -> dict | None:
        return next((g for g in self._guild(guild_id)["games"] if g["id"] == game_id), None)

    async def set_mvp(self, guild_id: int, game_id: int, user_ids: list[int]) -> bool:
        """MVP катки по итогам голосования. False — катку уже отменили."""
        game = self.game(guild_id, game_id)
        if game is None or game.get("mvp"):
            return False
        guild = self._guild(guild_id)
        game["mvp"] = list(user_ids)
        for user_id in user_ids:
            entry = guild["players"].setdefault(str(user_id), {"wins": 0, "losses": 0})
            entry["mvp"] = entry.get("mvp", 0) + 1
        await self.store.save()
        return True

    def games_since(self, guild_id: int, since: float) -> list[dict]:
        return [g for g in self._guild(guild_id)["games"] if g.get("at", 0) >= since]

    def guild_ids(self) -> list[int]:
        return [int(key) for key in self.store.data if key.isdigit()]


    def last_channel(self, guild_id: int) -> int | None:
        return self._guild(guild_id).get("last_channel")


@dataclass
class WeekLine:
    games: int = 0
    wins: int = 0
    elo: float = 0.0
    mvp: int = 0
    best_streak: int = 0
    streak: int = 0


def week_lines(games: list[dict]) -> dict[int, WeekLine]:
    """Итоги игроков по списку каток (в порядке записи): катки, победы, прирост Elo, MVP, лучшая серия побед."""
    lines: dict[int, WeekLine] = {}
    for game in sorted(games, key=lambda g: g["id"]):
        for user_id, won in [(u, True) for u in game["winners"]] + [(u, False) for u in game["losers"]]:
            line = lines.setdefault(user_id, WeekLine())
            line.games += 1
            line.elo += (game.get("elo") or {}).get(str(user_id), 0.0)
            if won:
                line.wins += 1
                line.streak += 1
                line.best_streak = max(line.best_streak, line.streak)
            else:
                line.streak = 0
        for user_id in game.get("mvp") or []:
            lines.setdefault(user_id, WeekLine()).mvp += 1
    return lines


def week_highlights(lines: dict[int, WeekLine], min_games: int = 3) -> dict[str, tuple[int, WeekLine]]:
    """Номинации недели: больше всех каток, лучший винрейт (от min_games), серия, рост Elo, MVP."""
    result: dict[str, tuple[int, WeekLine]] = {}
    if not lines:
        return result
    items = list(lines.items())
    result["most_games"] = max(items, key=lambda i: (i[1].games, i[1].wins))
    regulars = [i for i in items if i[1].games >= min_games]
    if regulars:
        result["best_winrate"] = max(regulars, key=lambda i: (i[1].wins / i[1].games, i[1].games))
    streak = max(items, key=lambda i: (i[1].best_streak, i[1].games))
    if streak[1].best_streak >= 3:
        result["streak"] = streak
    climber = max(items, key=lambda i: i[1].elo)
    if climber[1].elo > 0:
        result["climber"] = climber
    mvp = max(items, key=lambda i: (i[1].mvp, i[1].wins))
    if mvp[1].mvp:
        result["mvp"] = mvp
    return result
