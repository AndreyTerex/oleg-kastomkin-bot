"""Список чемпионов из Riot Data Dragon и линии, на которых их играют."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("scrimbot.champions")

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPIONS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/ru_RU/champion.json"
PORTRAIT_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_id}.png"

CACHE_TTL = 12 * 60 * 60

# Где чемпиона обычно играют. Ключи — id из Data Dragon. Один чемпион может стоять
# на нескольких линиях. Незнакомые id (например, после переименования) просто пропускаются.
LANE_POOLS: dict[str, tuple[str, ...]] = {
    "top": (
        "Aatrox", "Ambessa", "Camille", "Chogath", "Darius", "DrMundo", "Fiora", "Gangplank",
        "Garen", "Gnar", "Gragas", "Gwen", "Illaoi", "Irelia", "Jax", "Jayce", "KSante", "Kayle",
        "Kennen", "Kled", "Malphite", "Mordekaiser", "Nasus", "Olaf", "Ornn", "Pantheon", "Poppy",
        "Quinn", "Renekton", "Riven", "Rumble", "Sett", "Shen", "Singed", "Sion", "Teemo",
        "Trundle", "Tryndamere", "Urgot", "Vladimir", "Volibear", "Warwick", "MonkeyKing",
        "Yasuo", "Yone", "Yorick", "Heimerdinger", "Akali",
    ),
    "jungle": (
        "Amumu", "Belveth", "Briar", "Diana", "Ekko", "Elise", "Evelynn", "Fiddlesticks",
        "Gragas", "Graves", "Hecarim", "Ivern", "JarvanIV", "Jax", "Karthus", "Kayn", "Khazix",
        "Kindred", "LeeSin", "Lillia", "MasterYi", "Nidalee", "Nocturne", "Nunu", "Poppy",
        "Rammus", "RekSai", "Rengar", "Sejuani", "Shaco", "Shyvana", "Skarner", "Taliyah",
        "Talon", "Trundle", "Udyr", "Vi", "Viego", "Volibear", "Warwick", "MonkeyKing",
        "XinZhao", "Zac", "Zyra",
    ),
    "mid": (
        "Ahri", "Akali", "Akshan", "Anivia", "Annie", "AurelionSol", "Aurora", "Azir",
        "Cassiopeia", "Corki", "Diana", "Ekko", "Fizz", "Galio", "Hwei", "Irelia", "Kassadin",
        "Katarina", "Leblanc", "Lissandra", "Lux", "Malzahar", "Mel", "Naafiri", "Neeko",
        "Orianna", "Qiyana", "Ryze", "Swain", "Sylas", "Syndra", "Taliyah", "Talon",
        "TwistedFate", "Veigar", "Velkoz", "Vex", "Viktor", "Vladimir", "Xerath", "Yasuo",
        "Yone", "Zed", "Ziggs", "Zoe",
    ),
    "adc": (
        "Aphelios", "Ashe", "Caitlyn", "Corki", "Draven", "Ezreal", "Jhin", "Jinx", "Kaisa",
        "Kalista", "KogMaw", "Lucian", "MissFortune", "Nilah", "Samira", "Sivir", "Smolder",
        "Tristana", "Twitch", "Varus", "Vayne", "Xayah", "Yunara", "Zeri", "Ziggs",
    ),
    "support": (
        "Alistar", "Bard", "Blitzcrank", "Brand", "Braum", "Janna", "Karma", "Leona", "Lulu",
        "Lux", "Maokai", "Milio", "Morgana", "Nami", "Nautilus", "Neeko", "Pyke", "Rakan", "Rell",
        "Renata", "Senna", "Seraphine", "Sona", "Soraka", "Swain", "TahmKench", "Taric",
        "Thresh", "Velkoz", "Xerath", "Yuumi", "Zilean", "Zyra",
    ),
}

# Чемпионы, которых нет в списках выше (вышли позже), раскладываются по основному тегу.
TAG_LANES = {
    "Marksman": "adc",
    "Support": "support",
    "Mage": "mid",
    "Assassin": "mid",
    "Fighter": "top",
    "Tank": "top",
}


@dataclass(frozen=True)
class Champion:
    id: str
    name: str
    lanes: frozenset[str]


class ChampionsUnavailable(Exception):
    """Data Dragon не ответил — чемпионов выдать не получится."""


class ChampionPool:
    """Кэш списка чемпионов на 12 часов, общий для всех модулей бота."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._champions: list[Champion] = []
        self._fetched_at = 0.0
        self.version: str | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def portrait(self, champion: Champion) -> str:
        return PORTRAIT_URL.format(version=self.version, champion_id=champion.id)

    async def all(self) -> list[Champion]:
        if self._champions and time.time() - self._fetched_at < CACHE_TTL:
            return self._champions
        try:
            await self._fetch()
        except (aiohttp.ClientError, KeyError, ValueError, TimeoutError) as error:
            if self._champions:
                # Устаревший список лучше, чем никакого.
                log.warning("Data Dragon недоступен, используется прошлый список: %s", error)
                return self._champions
            raise ChampionsUnavailable from error
        return self._champions

    async def for_lane(self, lane_key: str) -> list[Champion]:
        return [champion for champion in await self.all() if lane_key in champion.lanes]

    async def _fetch(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        async with self._session.get(VERSIONS_URL) as response:
            response.raise_for_status()
            version = (await response.json())[0]
        async with self._session.get(CHAMPIONS_URL.format(version=version)) as response:
            response.raise_for_status()
            payload = await response.json()

        lanes_by_id: dict[str, set[str]] = {}
        for lane, ids in LANE_POOLS.items():
            for champion_id in ids:
                lanes_by_id.setdefault(champion_id, set()).add(lane)

        champions = []
        for champion_id, data in payload["data"].items():
            lanes = lanes_by_id.get(champion_id)
            if not lanes:
                tags = data.get("tags") or []
                lane = next((TAG_LANES[tag] for tag in tags if tag in TAG_LANES), "mid")
                lanes = {lane}
            champions.append(Champion(champion_id, data["name"], frozenset(lanes)))

        self._champions = champions
        self.version = version
        self._fetched_at = time.time()
        log.info("Список чемпионов обновлён: патч %s, всего %d", version, len(champions))


POOL = ChampionPool()
