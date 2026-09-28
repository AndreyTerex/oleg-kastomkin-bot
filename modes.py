"""Режимы кастомки: как делятся команды, раздаются линии и выдаются чемпионы.

Режим — три независимые настройки (команды, линии, чемпионы). Пресеты — готовые
сочетания этих настроек, их можно выбрать одним кликом и потом подправить по пунктам.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from typing import Callable, Sequence

import discord

import config
from champions import Champion
from utils import BLUE_SIDE, NEUTRAL, RED_SIDE, member_lanes, player_name, shuffled, split_by_lanes

TEAMS_DRAFT = "draft"
TEAMS_RANDOM = "random"
TEAMS_BALANCED = "balanced"
TEAMS_SKILL = "skill"

LANES_FREE = "free"
LANES_RANDOM = "random"
LANES_MAIN = "main"
LANES_OFFROLE = "offrole"

CHAMPS_FREE = "free"
CHAMPS_CHOICE = "choice"
CHAMPS_RANDOM = "random"
CHAMPS_MIRROR = "mirror"

CHOICE_SIZE = 3

LANE_KEYS = tuple(lane.key for lane in config.LANES)
LANE_ORDER = {key: index for index, key in enumerate(LANE_KEYS)}


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    emoji: str
    description: str


TEAM_OPTIONS = (
    Option(TEAMS_DRAFT, "Капитанский драфт", "👑", "Два случайных капитана по очереди разбирают игроков"),
    Option(TEAMS_RANDOM, "Случайные команды", "🎲", "Бот делит состав на две команды наугад"),
    Option(TEAMS_BALANCED, "По отмеченным линиям", "🧭", "Команды подбираются так, чтобы каждый встал на свою линию"),
    Option(TEAMS_SKILL, "По силе", "⚖️", "Команды уравниваются по статистике побед на сервере"),
)

LANE_OPTIONS = (
    Option(LANES_FREE, "Договариваетесь сами", "🗣️", "Бот линии не раздаёт"),
    Option(LANES_RANDOM, "Случайные линии", "🎲", "Каждому в команде достаётся случайная линия"),
    Option(LANES_MAIN, "Свои линии", "🧭", "Ставит на линии, отмеченные в панели ролей"),
    Option(LANES_OFFROLE, "Не своя роль", "🙃", "Ставит туда, где игрок обычно не играет"),
)

CHAMP_OPTIONS = (
    Option(CHAMPS_FREE, "Свободный выбор", "🆓", "Чемпионов выбираете сами"),
    Option(CHAMPS_CHOICE, "3 на выбор", "🃏", "3 чемпиона на линию; если соперники забанят все три — пик любого"),
    Option(CHAMPS_RANDOM, "Один случайный", "🎰", "Один чемпион на линию; если соперники его забанят — пик любого"),
    Option(CHAMPS_MIRROR, "Зеркало", "🪞", "Соперники по линии получают одного и того же чемпиона"),
)


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    emoji: str
    description: str
    teams: str
    lanes: str
    champs: str

    @property
    def mode(self) -> dict:
        return {"teams": self.teams, "lanes": self.lanes, "champs": self.champs}


PRESETS = (
    Preset("classic", "Классика", "👑", "Капитаны разбирают игроков, линии и чемпионов выбираете сами",
           TEAMS_DRAFT, LANES_FREE, CHAMPS_FREE),
    Preset("random", "Полный рандом", "🎲", "Команды и линии решает рандом, чемпионов берёте сами",
           TEAMS_RANDOM, LANES_RANDOM, CHAMPS_FREE),
    Preset("roulette", "Рулетка трёх", "🃏", "Рандом команд и линий, каждому 3 чемпиона на его линию",
           TEAMS_RANDOM, LANES_RANDOM, CHAMPS_CHOICE),
    Preset("mains", "По своим линиям", "🧭", "Каждый на отмеченной линии, команды подбираются под это",
           TEAMS_BALANCED, LANES_MAIN, CHAMPS_FREE),
    Preset("offrole", "Не своя роль", "🙃", "Каждого ставят туда, где он не играет, и дают 3 чемпиона",
           TEAMS_RANDOM, LANES_OFFROLE, CHAMPS_CHOICE),
    Preset("mirror", "Зеркало", "🪞", "Соперники по линии играют одним и тем же чемпионом",
           TEAMS_RANDOM, LANES_RANDOM, CHAMPS_MIRROR),
    Preset("chaos", "Хаос", "🌪️", "Всё решает рандом, каждому один чемпион без выбора",
           TEAMS_RANDOM, LANES_RANDOM, CHAMPS_RANDOM),
    Preset("fair", "Честный бой", "⚖️", "Команды уравнены по статистике побед, каждый на своей линии",
           TEAMS_SKILL, LANES_MAIN, CHAMPS_FREE),
)

PRESET_BY_KEY = {preset.key: preset for preset in PRESETS}
DEFAULT_PRESET = PRESETS[0]

_VALID = {
    "teams": {option.key for option in TEAM_OPTIONS},
    "lanes": {option.key for option in LANE_OPTIONS},
    "champs": {option.key for option in CHAMP_OPTIONS},
}


# --- настройки ----------------------------------------------------------------


def get_mode(record: dict) -> dict:
    """Режим сбора; у старых записей и при битых значениях — классика."""
    stored = record.get("mode") or {}
    mode = DEFAULT_PRESET.mode
    for key, allowed in _VALID.items():
        if stored.get(key) in allowed:
            mode[key] = stored[key]
    return mode


def find_preset(mode: dict) -> Preset | None:
    return next((preset for preset in PRESETS if preset.mode == mode), None)


def option_for(options: Sequence[Option], key: str) -> Option:
    return next(option for option in options if option.key == key)


def normalize(mode: dict) -> tuple[dict, list[str]]:
    """Убирает несовместимые сочетания и объясняет, что поменялось."""
    mode = dict(mode)
    notes: list[str] = []
    if mode["teams"] == TEAMS_BALANCED and mode["lanes"] != LANES_MAIN:
        mode["lanes"] = LANES_MAIN
        notes.append("Команды «по отмеченным линиям» сразу ставят каждого на свою линию — линии переключены на «Свои линии».")
    if mode["champs"] == CHAMPS_MIRROR and mode["lanes"] == LANES_FREE:
        mode["lanes"] = LANES_RANDOM
        notes.append("«Зеркалу» нужны линии, чтобы понимать, кто кому соперник, — включены случайные линии.")
    return mode, notes


def mode_title(mode: dict) -> str:
    preset = find_preset(mode)
    return f"{preset.emoji} {preset.title}" if preset else "⚙️ Свой режим"


def mode_summary(mode: dict) -> str:
    return " · ".join(
        option_for(options, mode[key]).label
        for key, options in (("teams", TEAM_OPTIONS), ("lanes", LANE_OPTIONS), ("champs", CHAMP_OPTIONS))
    )


# --- команды и линии ----------------------------------------------------------


def random_teams(players: Sequence[discord.Member]) -> list[list[discord.Member]]:
    mixed = shuffled(players)
    half = (len(mixed) + 1) // 2
    return [mixed[:half], mixed[half:]]


# До стольких игроков перебираем все деления; больше — жадно по рейтингу.
EXACT_SPLIT_LIMIT = 14
# Деления, которые хуже лучшего не больше чем на столько, считаются равными — из них выбираем случайно,
# чтобы одни и те же составы не повторялись из катки в катку.
SKILL_TOLERANCE = 0.03


def skill_teams(
    players: Sequence[discord.Member], rating: Callable[[discord.Member], float]
) -> list[list[discord.Member]]:
    """Две команды с максимально близкой суммой рейтингов."""
    players = list(players)
    if len(players) < 2:
        return [players, []]
    half = len(players) // 2
    ratings = {member.id: rating(member) for member in players}

    if len(players) <= EXACT_SPLIT_LIMIT:
        total = sum(ratings.values())
        splits = []
        # Первый игрок всегда в первой команде — так каждое деление встречается один раз.
        first, rest = players[0], players[1:]
        size = len(players) - half  # первая команда больше, если игроков нечётно
        for combo in itertools.combinations(rest, size - 1):
            team = [first, *combo]
            diff = abs(total - 2 * sum(ratings[member.id] for member in team))
            splits.append((diff, team))
        best = min(diff for diff, _ in splits)
        team = random.choice([team for diff, team in splits if diff <= best + SKILL_TOLERANCE])
        chosen = {member.id for member in team}
        blue, red = team, [member for member in players if member.id not in chosen]
    else:
        # Змейка по рейтингу: 1-2-2-1… даёт близкие суммы и на больших составах.
        ordered = sorted(players, key=lambda member: (-ratings[member.id], random.random()))
        blue, red = [], []
        for index, member in enumerate(ordered):
            (blue if index % 4 in (0, 3) else red).append(member)

    if random.random() < 0.5:
        blue, red = red, blue
    return [shuffled(blue), shuffled(red)]


def balanced_teams(players: Sequence[discord.Member]) -> tuple[list[list[discord.Member]], dict[int, str]] | None:
    """Две команды, в каждой все пять линий по отмеченным ролям."""
    rosters = split_by_lanes(players)
    if rosters is None:
        return None
    teams = [[member for _lane, member in roster] for roster in rosters]
    lanes = {member.id: lane.key for roster in rosters for lane, member in roster}
    return teams, lanes


def _match(candidates: dict[int, list[str]]) -> dict[int, str] | None:
    """Разные линии для всех игроков из допустимых вариантов, со случайным порядком."""
    order = sorted(candidates, key=lambda player_id: (len(candidates[player_id]), random.random()))
    assigned: dict[int, str] = {}
    taken: set[str] = set()

    def backtrack(index: int) -> bool:
        if index == len(order):
            return True
        player_id = order[index]
        options = [lane for lane in candidates[player_id] if lane not in taken]
        random.shuffle(options)
        for lane in options:
            assigned[player_id] = lane
            taken.add(lane)
            if backtrack(index + 1):
                return True
            taken.discard(lane)
            del assigned[player_id]
        return False

    return dict(assigned) if backtrack(0) else None


def assign_lanes(team: Sequence[discord.Member], how: str) -> tuple[dict[int, str], bool]:
    """Линии внутри одной команды. Второе значение — удалось ли выполнить условие режима."""
    if how == LANES_FREE or not team or len(team) > len(LANE_KEYS):
        return {}, how == LANES_FREE

    random_lanes = dict(zip((member.id for member in shuffled(team)), shuffled(LANE_KEYS)))
    if how == LANES_RANDOM:
        return random_lanes, True

    candidates: dict[int, list[str]] = {}
    for member in team:
        marked = [lane.key for lane in member_lanes(member)]
        if how == LANES_MAIN:
            # Кто линий не отмечал, встанет куда угодно.
            candidates[member.id] = marked or list(LANE_KEYS)
        else:
            # Флексу, отметившему все линии, «не своей» не найдётся — ставим куда угодно.
            candidates[member.id] = [key for key in LANE_KEYS if key not in marked] or list(LANE_KEYS)

    matched = _match(candidates)
    if matched is None:
        return random_lanes, False
    return matched, True


# --- чемпионы -----------------------------------------------------------------


# Какая доля чемпионов линии (самые давно не выпадавшие) участвует в случайном выборе.
FRESH_SHARE = 0.35


def deal_champions(
    teams: Sequence[Sequence[discord.Member]],
    lanes: dict[int, str],
    how: str,
    pool: Sequence[Champion],
    last_seen: dict[str, float] | None = None,
) -> dict[int, list[Champion]]:
    """Раздаёт чемпионов без повторов на всю кастомку.

    Игроку без линии достаются чемпионы из всего списка. Если на линии чемпионы
    кончились, недостающие добираются из всех остальных.
    last_seen — когда чемпион последний раз выпадал на сервере: случайный выбор идёт только
    среди давно не выпадавших (FRESH_SHARE списка линии), чтобы одни и те же не мелькали подряд.
    """
    if how == CHAMPS_FREE:
        return {}

    used: set[str] = set()

    def take(lane: str | None, count: int) -> list[Champion]:
        free = [champion for champion in pool if champion.id not in used]
        fitting = [champion for champion in free if lane is None or lane in champion.lanes]
        if len(fitting) < count:
            fitting += [champion for champion in free if champion not in fitting]
        if last_seen:
            # Сначала те, кого давно (или ни разу) не было; среди равных — случайный порядок.
            fitting.sort(key=lambda champion: (last_seen.get(champion.id, 0.0), random.random()))
            fitting = fitting[:max(count, math.ceil(len(fitting) * FRESH_SHARE))]
        picked = random.sample(fitting, min(count, len(fitting)))
        used.update(champion.id for champion in picked)
        return picked

    players = [member for team in teams for member in team]
    dealt: dict[int, list[Champion]] = {}

    if how == CHAMPS_MIRROR:
        by_lane: dict[str, list[int]] = {}
        for member in players:
            lane = lanes.get(member.id)
            if lane:
                by_lane.setdefault(lane, []).append(member.id)
            else:
                dealt[member.id] = take(None, 1)
        for lane, member_ids in by_lane.items():
            champion = take(lane, 1)
            for member_id in member_ids:
                dealt[member_id] = champion
        return dealt

    count = CHOICE_SIZE if how == CHAMPS_CHOICE else 1
    # Раздаём в случайном порядке, чтобы «остатки» линии не доставались всегда одним и тем же.
    for member in shuffled(players):
        dealt[member.id] = take(lanes.get(member.id), count)
    return dealt


# --- оформление ---------------------------------------------------------------


def champion_rules(how: str) -> list[str]:
    """Правила выбора чемпионов для раздачи: баны бот не видит, поэтому они прописаны текстом."""
    if how == CHAMPS_CHOICE:
        return [
            "Каждый берёт одного чемпиона из своей тройки. Лобби — «Турнирный драфт», баны обычные.",
            "Тройки видны всем. Если **соперники** забанили все три твоих чемпиона — бери кого угодно. "
            "Баны своей команды не в счёт.",
        ]
    if how == CHAMPS_RANDOM:
        return [
            "Каждый играет выпавшим чемпионом. Лобби — «Турнирный драфт», баны обычные.",
            "Если **соперники** забанили твоего чемпиона — бери кого угодно. Бан своей команды не в счёт.",
        ]
    if how == CHAMPS_MIRROR:
        return [
            "Соперники по линии играют одним чемпионом. Лобби — «Вслепую»: только в нём один чемпион "
            "может быть у обеих команд, банов в этом режиме нет.",
        ]
    return []


def order_by_lane(team: Sequence[discord.Member], lanes: dict[int, str]) -> list[discord.Member]:
    """Игроки команды по порядку линий (топ → саппорт), без линии — в конце. Порядок устойчивый."""
    return sorted(team, key=lambda member: LANE_ORDER.get(lanes.get(member.id), len(LANE_ORDER)))


def team_lines(
    team: Sequence[discord.Member],
    lanes: dict[int, str],
    *,
    champions: dict[int, list[Champion]] | None = None,
    captain: discord.Member | None = None,
) -> str:
    """Состав команды: по порядку линий, с капитаном и чемпионами, если они есть."""
    ordered = order_by_lane(team, lanes)
    lines = []
    for member in ordered:
        lane_key = lanes.get(member.id)
        lane = config.LANE_BY_KEY.get(lane_key) if lane_key else None
        crown = "👑 " if captain is not None and member.id == captain.id else ""
        line = f"{lane.emoji} {lane.label} · " if lane else ""
        line += f"{crown}**{player_name(member)}**"
        picks = (champions or {}).get(member.id)
        if picks:
            line += " — " + " / ".join(champion.name for champion in picks)
        lines.append(line)
    return "\n".join(lines) or "—"


def deal_embed(
    teams: Sequence[Sequence[discord.Member]],
    lanes: dict[int, str],
    champions: dict[int, list[Champion]],
    mode: dict,
    notes: Sequence[str],
    *,
    captains: Sequence[discord.Member] = (),
    patch: str | None = None,
    round_no: int = 1,
) -> discord.Embed:
    """Итог запуска: кто где играет и что ему выпало. round_no > 1 — переигранная раздача после катки."""
    description = [mode_summary(mode)]
    description += champion_rules(mode["champs"])

    embed = discord.Embed(
        title=f"Раздача · катка {round_no} · {mode_title(mode)}" if round_no > 1 else f"Раздача · {mode_title(mode)}",
        description="\n".join(description),
        color=NEUTRAL,
    )
    for side, (team, side_name) in enumerate(zip(teams, (BLUE_SIDE, RED_SIDE))):
        captain = captains[side] if len(captains) == 2 else None
        embed.add_field(
            name=f"{side_name} ({len(team)})",
            value=team_lines(team, lanes, champions=champions, captain=captain),
            inline=False,
        )
    if notes:
        embed.add_field(name="Обратите внимание", value="\n".join(f"• {note}" for note in notes), inline=False)

    footer = "Развести по голосовым — «Раскидать по каналам» · сыграли — отметьте победителя в посте сбора"
    if champions and patch:
        footer = f"Патч {patch} · чемпионы не повторяются · " + footer
    embed.set_footer(text=footer)
    return embed
