"""Скриншот итогов катки → кто победил в сборе.

Скриншот таблицы результатов League of Legends разбирает Gemini (он видит картинки), а код сопоставляет
чемпионов и ники со скриншота с составами сбора: чьи чемпионы из раздачи и чьи ники у победителей,
та сторона и выиграла. Записывает результат всё равно организатор — кнопкой подтверждения.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

VISION_SYSTEM = (
    "Ты распознаёшь скриншоты League of Legends. Смотри только на картинку, ничего не выдумывай. "
    "Ответ — строго один JSON-объект без пояснений."
)
VISION_TASK = (
    "Это скриншот из League of Legends? Если это таблица итогов матча (после игры: ПОБЕДА/ПОРАЖЕНИЕ, "
    "VICTORY/DEFEAT, счёт K/D/A игроков), разбери её. Ответь JSON: "
    '{"scoreboard": true|false, "players": [{"name": "ник как на экране", "champion": "чемпион по-русски, '
    'как в русском клиенте", "kills": 0, "deaths": 0, "assists": 0, "won": true|false|null}]}. '
    "won — победила ли команда этого игрока (у команды-победителя надпись «Победа»/«Victory»; "
    "если не видно — null). Если это не таблица итогов — {\"scoreboard\": false, \"players\": []}."
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


@dataclass(frozen=True)
class ShotPlayer:
    name: str
    champion: str
    kills: int
    deaths: int
    assists: int
    won: bool | None

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)

    def line(self) -> str:
        return f"{self.name} ({self.champion}) {self.kills}/{self.deaths}/{self.assists}"


def parse_scoreboard(text: str) -> list[ShotPlayer] | None:
    """Игроки со скриншота; None — это не таблица итогов или ответ не разобрать."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("scoreboard") or not isinstance(data.get("players"), list):
        return None
    players = []
    for item in data["players"]:
        if not isinstance(item, dict):
            continue

        def number(key: str) -> int:
            try:
                return max(0, int(item.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        won = item.get("won")
        players.append(ShotPlayer(
            name=str(item.get("name") or "").strip()[:40],
            champion=str(item.get("champion") or "").strip()[:30],
            kills=number("kills"), deaths=number("deaths"), assists=number("assists"),
            won=won if isinstance(won, bool) else None,
        ))
    return players or None


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", (text or "").casefold().replace("ё", "е"))


def _same(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if len(a) < 3 or len(b) < 3:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def guess_winner(
    players: list[ShotPlayer], teams: list[list[int]], champions: dict[int, list[str]], names: dict[int, list[str]],
) -> int | None:
    """Какая сторона сбора (0 — синие, 1 — красные) победила по скриншоту; None — не понять.

    teams — составы сбора, champions — раздача (id → чемпионы), names — id → ники игрока (Discord и др.).
    Каждый игрок со скриншота с известным исходом голосует за сторону, если его чемпион или ник
    совпал с кем-то из сбора.
    """
    votes = [0, 0]
    for shot in players:
        if shot.won is None:
            continue
        for side, team in enumerate(teams):
            matched = any(
                any(_same(shot.champion, c) for c in champions.get(user_id, []))
                or any(_same(shot.name, n) for n in names.get(user_id, []))
                for user_id in team
            )
            if matched:
                winner_side = side if shot.won else 1 - side
                votes[winner_side] += 1
                break
    if votes[0] == votes[1] or max(votes) < 2:
        return None
    return 0 if votes[0] > votes[1] else 1


def best_player(players: list[ShotPlayer]) -> ShotPlayer | None:
    """Лучший по KDA (при равенстве — по убийствам)."""
    return max(players, key=lambda p: (p.kda, p.kills), default=None)
