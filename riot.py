"""Ранги League of Legends через Riot API: /link Ник#TAG привязывает аккаунт, ранг в соло-очереди
подтягивается и кэшируется (data/riot.json).

Нужен ключ RIOT_API_KEY (developer.riotgames.com). Ключ разработчика живёт сутки — для постоянной работы
оформите Personal API Key там же. Без ключа команды честно говорят, что ранги не настроены.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

import config
from storage import JsonStore

log = logging.getLogger("scrimbot.riot")

ACCOUNT_URL = "https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
LEAGUE_URL = "https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
RANK_TTL = 6 * 60 * 60

TIERS = {
    "IRON": ("Железо", 800), "BRONZE": ("Бронза", 880), "SILVER": ("Серебро", 960), "GOLD": ("Золото", 1040),
    "PLATINUM": ("Платина", 1120), "EMERALD": ("Изумруд", 1200), "DIAMOND": ("Алмаз", 1280),
    "MASTER": ("Мастер", 1360), "GRANDMASTER": ("Грандмастер", 1420), "CHALLENGER": ("Претендент", 1480),
}
DIVISIONS = {"IV": 0, "III": 20, "II": 40, "I": 60}
TIER_EMOJI = {
    "IRON": "⚙️", "BRONZE": "🥉", "SILVER": "🥈", "GOLD": "🥇", "PLATINUM": "💠", "EMERALD": "💚",
    "DIAMOND": "💎", "MASTER": "🟣", "GRANDMASTER": "🔴", "CHALLENGER": "👑",
}
# Сколько каток на кастомках нужно, чтобы Elo перестал опираться на ранг.
ELO_TRUST_GAMES = 10


class RiotError(Exception):
    """Ошибка для пользователя (текст можно показать)."""


def parse_riot_id(text: str) -> tuple[str, str]:
    name, sep, tag = (text or "").strip().rpartition("#")
    if not sep or not name.strip() or not tag.strip() or len(tag) > 5:
        raise RiotError("Пиши Riot ID целиком: «Ник#TAG», как в клиенте.")
    return name.strip(), tag.strip()


def solo_entry(entries) -> dict | None:
    if not isinstance(entries, list):
        return None
    return next((e for e in entries if isinstance(e, dict) and e.get("queueType") == "RANKED_SOLO_5x5"), None)


def rank_text(rank: dict | None, emoji: bool = True) -> str:
    if not rank:
        return "без ранга"
    tier = rank.get("tier", "")
    title, _ = TIERS.get(tier, (tier.title(), 0))
    division = "" if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {rank.get('rank', '')}"
    icon = TIER_EMOJI.get(tier, "") if emoji else ""
    return f"{icon} {title}{division}, {rank.get('leaguePoints', 0)} LP".strip()


def rank_elo(rank: dict | None) -> float | None:
    """Ранг в шкалу Elo кастомок (1000 — середина), чтобы новичка сразу ставить в честную команду."""
    if not rank or rank.get("tier") not in TIERS:
        return None
    base = TIERS[rank["tier"]][1]
    return base + DIVISIONS.get(rank.get("rank", ""), 0) + min(rank.get("leaguePoints", 0), 100) * 0.2


def blended_elo(elo: float, games: int, rank: dict | None) -> float:
    """Пока каток мало, Elo тянется к рангу; с ELO_TRUST_GAMES катками — только Elo кастомок."""
    seed = rank_elo(rank)
    if seed is None or games >= ELO_TRUST_GAMES:
        return elo
    share = games / ELO_TRUST_GAMES
    return elo * share + seed * (1 - share)


class Riot:
    def __init__(self, path: Path | None = None) -> None:
        self.store = JsonStore(path or Path(config.DATA_DIR) / "riot.json")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.RIOT_API_KEY)

    def load(self) -> None:
        self.store.load()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def linked(self, user_id: int) -> dict | None:
        return self.store.data.get(str(user_id))

    def rank(self, user_id: int) -> dict | None:
        entry = self.linked(user_id)
        return entry.get("rank") if entry else None

    async def _get(self, url: str):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        async with self._session.get(url, headers={"X-Riot-Token": config.RIOT_API_KEY}) as response:
            if response.status == 404:
                return None
            if response.status in (401, 403):
                raise RiotError("Riot отклонил ключ — похоже, RIOT_API_KEY истёк (ключ разработчика живёт сутки).")
            if response.status == 429:
                raise RiotError("Riot просит подождать — слишком много запросов, попробуй через минуту.")
            response.raise_for_status()
            return await response.json()

    async def link(self, user_id: int, riot_id: str) -> dict:
        if not self.enabled:
            raise RiotError("Ранги не настроены: владелец бота не задал RIOT_API_KEY.")
        name, tag = parse_riot_id(riot_id)
        try:
            account = await self._get(ACCOUNT_URL.format(
                region=config.RIOT_REGION, name=quote(name, safe=""), tag=quote(tag, safe=""),
            ))
        except aiohttp.ClientError as error:
            raise RiotError("Riot API сейчас не отвечает, попробуй позже.") from error
        if not account or not account.get("puuid"):
            raise RiotError(f"Не нашла аккаунт «{name}#{tag}». Проверь ник и тег.")
        entry = {"riot_id": f"{account.get('gameName', name)}#{account.get('tagLine', tag)}", "puuid": account["puuid"]}
        self.store.data[str(user_id)] = entry
        await self.refresh(user_id, force=True)
        return self.store.data[str(user_id)]

    async def unlink(self, user_id: int) -> bool:
        if self.store.data.pop(str(user_id), None) is None:
            return False
        await self.store.save()
        return True

    async def refresh(self, user_id: int, force: bool = False) -> dict | None:
        """Обновить ранг, если он старше RANK_TTL (или force). Ошибки сети — оставить старый."""
        entry = self.linked(user_id)
        if entry is None or not self.enabled:
            return None
        if not force and time.time() - entry.get("checked", 0) < RANK_TTL:
            return entry.get("rank")
        try:
            entries = await self._get(LEAGUE_URL.format(platform=config.RIOT_PLATFORM, puuid=entry["puuid"]))
        except (aiohttp.ClientError, TimeoutError) as error:
            log.info("Ранг %s не обновился: %s", entry.get("riot_id"), type(error).__name__)
            return entry.get("rank")
        solo = solo_entry(entries)
        entry["rank"] = (
            {key: solo.get(key) for key in ("tier", "rank", "leaguePoints", "wins", "losses")} if solo else None
        )
        entry["checked"] = time.time()
        await self.store.save()
        return entry["rank"]
