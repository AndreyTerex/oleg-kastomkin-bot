"""Картинка раздачи: портреты выпавших чемпионов, синие слева, красные справа.

Строки идут в том же порядке, что и игроки в эмбеде раздачи, — так картинку легко читать рядом с текстом.
Если Data Dragon не ответил или Pillow недоступен, раздача уходит без картинки.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Sequence

import aiohttp
import discord

from champions import POOL, Champion

log = logging.getLogger("scrimbot.portraits")

try:
    from PIL import Image, ImageDraw
except ImportError:  # без Pillow просто не рисуем
    Image = ImageDraw = None

SIZE = 56
GAP = 4
PAD = 10
COLUMN_GAP = 28
MAX_PICKS = 3
BACKGROUND = (43, 45, 49, 255)
SIDE_COLORS = ((59, 130, 246, 255), (239, 68, 68, 255))

_cache: dict[str, bytes] = {}
_session: aiohttp.ClientSession | None = None


async def close() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


async def _portrait(champion: Champion) -> bytes | None:
    global _session
    url = POOL.portrait(champion)
    if url in _cache:
        return _cache[url]
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        async with _session.get(url) as response:
            response.raise_for_status()
            data = await response.read()
    except (aiohttp.ClientError, TimeoutError):
        log.debug("Портрет %s не скачался", champion.id)
        return None
    _cache[url] = data
    return data


def render(rows: Sequence[Sequence[Sequence[bytes | None]]]) -> bytes:
    """rows[сторона][игрок] — портреты чемпионов игрока. Возвращает PNG."""
    height_rows = max((len(side) for side in rows), default=0)
    # Колонка шириной с самый длинный набор: при одном чемпионе на игрока картинка узкая.
    picks = min(MAX_PICKS, max((len(player) for side in rows for player in side), default=1)) or 1
    column = picks * SIZE + (picks - 1) * GAP
    width = PAD * 2 + column * 2 + COLUMN_GAP
    height = PAD * 2 + 6 + height_rows * SIZE + max(height_rows - 1, 0) * GAP
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    for side, players in enumerate(rows):
        left = PAD + side * (column + COLUMN_GAP)
        draw.rectangle((left, PAD, left + column - 1, PAD + 2), fill=SIDE_COLORS[side])
        for index, player_picks in enumerate(players):
            top = PAD + 6 + index * (SIZE + GAP)
            for pick_index, data in enumerate(player_picks[:MAX_PICKS]):
                x = left + pick_index * (SIZE + GAP)
                if data is None:
                    draw.rectangle((x, top, x + SIZE - 1, top + SIZE - 1), outline=(90, 90, 90, 255))
                    continue
                try:
                    portrait = Image.open(io.BytesIO(data)).convert("RGBA").resize((SIZE, SIZE))
                except OSError:
                    continue
                canvas.paste(portrait, (x, top), portrait)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def deal_image(
    teams: Sequence[Sequence[discord.Member]],
    champions: dict[int, list[Champion]],
    order,
) -> discord.File | None:
    """PNG с чемпионами раздачи. order(team) — игроки команды в порядке эмбеда."""
    if Image is None or not champions:
        return None
    ordered = [order(team) for team in teams]
    unique = {champion.id: champion for picks in champions.values() for champion in picks}
    fetched = await asyncio.gather(*(_portrait(champion) for champion in unique.values()))
    by_id = dict(zip(unique, fetched))
    if not any(fetched):
        return None
    rows = [
        [[by_id.get(champion.id) for champion in champions.get(member.id, [])] for member in team]
        for team in ordered
    ]
    try:
        png = await asyncio.to_thread(render, rows)
    except Exception:
        log.exception("Не удалось нарисовать раздачу")
        return None
    return discord.File(io.BytesIO(png), filename="deal.png")
