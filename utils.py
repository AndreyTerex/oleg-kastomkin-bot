"""Общие помощники: ответы на взаимодействия, работа с составом и линиями."""
from __future__ import annotations

import random
from typing import Iterable, Sequence

import discord

import config

BLUE = discord.Color(0x3B82F6)
RED = discord.Color(0xEF4444)
NEUTRAL = discord.Color(0x5865F2)
WARNING = discord.Color(0xF59E0B)

BLUE_SIDE = "🔵 Синяя сторона"
RED_SIDE = "🔴 Красная сторона"


async def respond(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """Отвечает на взаимодействие независимо от того, был ли ответ уже отправлен."""
    payload: dict = {"ephemeral": ephemeral}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embed"] = embed
    if view is not None:
        payload["view"] = view

    if interaction.response.is_done():
        await interaction.followup.send(**payload)
    else:
        await interaction.response.send_message(**payload)


def member_lanes(member: discord.Member) -> list[config.Lane]:
    """Линии, отмеченные участником через панель ролей."""
    role_ids = {role.id for role in getattr(member, "roles", ())}
    return [lane for lane in config.LANES if lane.role_id in role_ids]


def lanes_badge(member: discord.Member, limit: int = 2) -> str:
    """Компактная отметка линий: не больше двух значков, остальные — числом.

    У флекс-игроков отмечено по четыре-пять линий, и полная гирлянда значков
    после каждого ника делает списки нечитаемыми.
    """
    lanes = member_lanes(member)
    if not lanes:
        return ""
    shown = "".join(lane.emoji for lane in lanes[:limit])
    rest = len(lanes) - limit
    return f"{shown}+{rest}" if rest > 0 else shown


def player_name(member: discord.Member) -> str:
    """Отображаемое имя без разметки — в составах читается спокойнее упоминания."""
    return discord.utils.escape_markdown(member.display_name)


def voice_members(channel: discord.VoiceChannel | discord.StageChannel) -> list[discord.Member]:
    """Живые участники голосового канала (боты не учитываются)."""
    return [member for member in channel.members if not member.bot]


def resolve_voice_channel(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel | None,
) -> discord.VoiceChannel | None:
    """Канал из аргумента команды либо тот, в котором сейчас находится автор."""
    if channel is not None:
        return channel
    voice_state = getattr(interaction.user, "voice", None)
    return voice_state.channel if voice_state else None


def format_players(
    members: Sequence[discord.Member],
    *,
    numbered: bool = False,
    show_lanes: bool = True,
    mention: bool = True,
    captain: discord.Member | None = None,
    empty: str = "—",
    start: int = 1,
) -> str:
    """Список участников для поля эмбеда.

    `mention=False` выводит имена текстом: в готовых составах ряд синих
    упоминаний выглядит рябью, а имена читаются как обычный список.
    Капитан помечается короной вместо номера.
    """
    if not members:
        return empty
    lines = []
    for index, member in enumerate(members, start=start):
        if captain is not None and member.id == captain.id:
            prefix = "👑 "
        elif numbered:
            prefix = f"`{index:>2}.` " if start + len(members) > 10 else f"`{index}.` "
        else:
            prefix = "• "
        name = member.mention if mention else f"**{player_name(member)}**"
        badge = f" {lanes_badge(member)}".rstrip() if show_lanes else ""
        lines.append(f"{prefix}{name}{badge}")
    return "\n".join(lines)


def format_roster(entries: Sequence[tuple[config.Lane, discord.Member]]) -> str:
    """Состав с распределением по линиям."""
    return "\n".join(f"{lane.emoji} **{lane.label}** — {player_name(member)}" for lane, member in entries)


def shuffled(items: Iterable) -> list:
    result = list(items)
    random.shuffle(result)
    return result


def split_by_lanes(
    players: Sequence[discord.Member],
) -> tuple[list[tuple[config.Lane, discord.Member]], list[tuple[config.Lane, discord.Member]]] | None:
    """Пытается собрать два состава так, чтобы в каждом были все пять линий.

    Учитываются роли, выданные через панель. Возвращает None, если игроков
    не ровно десять или подходящего распределения не существует.
    """
    if len(players) != config.TEAM_SIZE * 2:
        return None

    lanes = list(config.LANES)
    slots = [(lane, side) for lane in lanes for side in (0, 1)]
    candidates: dict[tuple[str, int], list[discord.Member]] = {}
    for lane, side in slots:
        candidates[(lane.key, side)] = [
            player for player in players if lane.role_id in {role.id for role in player.roles}
        ]

    # Сначала закрываем самые дефицитные слоты — так перебор почти всегда сходится сразу.
    slots.sort(key=lambda slot: len(candidates[(slot[0].key, slot[1])]))

    assigned: dict[tuple[str, int], discord.Member] = {}
    taken: set[int] = set()

    def backtrack(index: int) -> bool:
        if index == len(slots):
            return True
        lane, side = slots[index]
        options = [p for p in candidates[(lane.key, side)] if p.id not in taken]
        random.shuffle(options)
        for player in options:
            taken.add(player.id)
            assigned[(lane.key, side)] = player
            if backtrack(index + 1):
                return True
            taken.discard(player.id)
            assigned.pop((lane.key, side), None)
        return False

    if not backtrack(0):
        return None

    blue = [(lane, assigned[(lane.key, 0)]) for lane in lanes]
    red = [(lane, assigned[(lane.key, 1)]) for lane in lanes]
    return blue, red
