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

# Свежая статистика, на каких линиях реально играют чемпионов (Meraki Analytics, по данным Riot).
# Дополняет списки ниже: новые чемпионы и смены меты подхватываются сами. Недоступна — хватает списков.
ROLE_RATES_URL = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/championrates.json"
POSITION_LANES = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}
# Линия засчитывается, если на ней чемпиона играют хотя бы в такой доле его игр.
ROLE_MIN_SHARE = 0.12

# Где чемпиона обычно играют. Ключи — id из Data Dragon. Один чемпион может стоять
# на нескольких линиях. Незнакомые id (например, после переименования) просто пропускаются.
LANE_POOLS: dict[str, tuple[str, ...]] = {
    "top": (
        "Aatrox", "Akali", "Ambessa", "Camille", "Cassiopeia", "Chogath", "Darius", "DrMundo", "Fiora",
        "Gangplank", "Garen", "Gnar", "Gragas", "Gwen", "Heimerdinger", "Illaoi", "Irelia", "Jax", "Jayce",
        "KSante", "Kayle", "Kennen", "Kled", "Malphite", "Mordekaiser", "Nasus", "Olaf", "Ornn", "Pantheon",
        "Poppy", "Quinn", "Renekton", "Riven", "Rumble", "Ryze", "Sett", "Shen", "Singed", "Sion", "Sylas",
        "TahmKench", "Teemo", "Trundle", "Tryndamere", "Udyr", "Urgot", "Vayne", "Vladimir",
        "Volibear", "Warwick", "MonkeyKing", "Yasuo", "Yone", "Yorick", "Zac", "Zaahen", "Smolder",
    ),
    "jungle": (
        "Amumu", "Belveth", "Brand", "Briar", "Diana", "Ekko", "Elise", "Evelynn", "Fiddlesticks", "Gragas",
        "Graves", "Gwen", "Hecarim", "Ivern", "JarvanIV", "Jax", "Karthus", "Kayn", "Khazix", "Kindred",
        "LeeSin", "Lillia", "MasterYi", "Morgana", "Naafiri", "Nidalee", "Nocturne", "Nunu", "Pantheon",
        "Poppy", "Qiyana", "Rammus", "RekSai", "Rengar", "Sejuani", "Shaco", "Shyvana", "Skarner", "Sylas",
        "Taliyah", "Talon", "Trundle", "Udyr", "Vi", "Viego", "Volibear", "Warwick", "MonkeyKing", "XinZhao",
        "Zac", "Zed", "Zyra", "Zaahen",
    ),
    "mid": (
        "Ahri", "Akali", "Akshan", "Anivia", "Annie", "AurelionSol", "Aurora", "Azir", "Cassiopeia", "Corki",
        "Diana", "Ekko", "Fizz", "Galio", "Hwei", "Irelia", "Jayce", "Kassadin", "Katarina", "Kennen",
        "Leblanc", "Lissandra", "Lux", "Malphite", "Malzahar", "Mel", "Naafiri", "Neeko", "Orianna",
        "Pantheon", "Qiyana", "Ryze", "Smolder", "Swain", "Sylas", "Syndra", "Taliyah", "Talon",
        "Tristana", "TwistedFate", "Veigar", "Velkoz", "Vex", "Viktor", "Vladimir", "Xerath", "Yasuo",
        "Yone", "Zed", "Ziggs", "Zoe",
    ),
    "adc": (
        "Aphelios", "Ashe", "Caitlyn", "Corki", "Draven", "Ezreal", "Hwei", "Jhin", "Jinx", "Kaisa",
        "Kalista", "KogMaw", "Lucian", "MissFortune", "Nilah", "Samira", "Senna", "Seraphine", "Sivir",
        "Smolder", "Swain", "Tristana", "Twitch", "Varus", "Vayne", "Xayah", "Yasuo", "Yunara", "Zeri",
        "Ziggs",
    ),
    "support": (
        "Alistar", "Ashe", "Bard", "Blitzcrank", "Brand", "Braum", "Camille", "Elise", "Galio", "Hwei",
        "Janna", "Karma", "Leona", "Lulu", "Lux", "Maokai", "Mel", "Milio", "Morgana", "Nami", "Nautilus",
        "Neeko", "Pantheon", "Poppy", "Pyke", "Rakan", "Rell", "Renata", "Senna", "Seraphine", "Shaco",
        "Sona", "Soraka", "Swain", "TahmKench", "Taric", "Thresh", "Velkoz", "Xerath", "Yuumi", "Zilean",
        "Zyra",
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
        rates = await self._role_rates()

        champions = []
        for champion_id, data in payload["data"].items():
            lanes = set(lanes_by_id.get(champion_id, ())) | rates.get(str(data.get("key")), set())
            if not lanes:
                tags = data.get("tags") or []
                lane = next((TAG_LANES[tag] for tag in tags if tag in TAG_LANES), "mid")
                lanes = {lane}
            champions.append(Champion(champion_id, data["name"], frozenset(lanes)))

        self._champions = champions
        self.version = version
        if rates:
            log.info("Линии чемпионов дополнены свежей статистикой (%d чемпионов)", len(rates))
        self._fetched_at = time.time()
        log.info("Список чемпионов обновлён: патч %s, всего %d", version, len(champions))

    async def _role_rates(self) -> dict[str, set[str]]:
        """Ключ чемпиона (число из Data Dragon) → линии, где его заметно играют. Ошибка — пустой словарь."""
        try:
            async with self._session.get(ROLE_RATES_URL) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            log.info("Статистика линий недоступна, беру встроенные списки: %s", type(error).__name__)
            return {}
        return parse_role_rates(payload)


def parse_role_rates(payload) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return result
    for key, positions in data.items():
        if not isinstance(positions, dict):
            continue
        rates = {}
        for position, lane in POSITION_LANES.items():
            entry = positions.get(position)
            rate = entry.get("playRate") if isinstance(entry, dict) else None
            if isinstance(rate, (int, float)) and rate > 0:
                rates[lane] = float(rate)
        total = sum(rates.values())
        if total:
            lanes = {lane for lane, rate in rates.items() if rate / total >= ROLE_MIN_SHARE}
            if lanes:
                result[str(key)] = lanes
    return result


POOL = ChampionPool()
