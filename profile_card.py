"""Карточка игрока для /profile: картинка с аватаром, Elo, графиком, формой и напарником (Pillow)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1000, 560
BG = (24, 25, 33)
PANEL = (34, 36, 47)
TEXT = (235, 236, 242)
MUTED = (150, 154, 170)
ACCENT = (138, 115, 255)
WIN = (72, 199, 116)
LOSS = (232, 84, 84)
GOLD = (245, 196, 66)

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)
BOLD_PATHS = tuple(path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") for path in FONT_PATHS)


@dataclass
class CardData:
    name: str
    elo: float
    games: int
    wins: int
    losses: int
    mvp: int = 0
    place: int | None = None
    coins: int | None = None
    lanes: str = ""
    elo_history: list[float] = field(default_factory=list)
    form: list[bool] = field(default_factory=list)
    partner: str = ""
    nemesis: str = ""
    avatar: bytes | None = None


def _font(size: int, bold: bool = False):
    for path in (BOLD_PATHS if bold else FONT_PATHS):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def elo_history(current: float, deltas: list[float]) -> list[float]:
    """Точки графика: значения Elo до и после каждой катки (deltas — по порядку каток)."""
    points = [current]
    for delta in reversed(deltas):
        points.append(points[-1] - delta)
    return list(reversed(points))


def _avatar(data: bytes | None, size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), ACCENT + (255,))
    if data:
        try:
            image = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size))
        except Exception:
            pass
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    image.putalpha(mask)
    return image


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    while text and draw.textlength(text, font=font) > width:
        text = text[:-2] + "…"
    return text


def render(card: CardData) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    big, mid, small, tiny = _font(46, True), _font(28, True), _font(22), _font(18)

    # Шапка: аватар, имя и линии.
    image.paste(_avatar(card.avatar, 140), (40, 36), _avatar(card.avatar, 140))
    draw.text((205, 50), _fit(draw, card.name, big, 760), font=big, fill=TEXT)
    subtitle = card.lanes or "линии не отмечены"
    draw.text((207, 115), _fit(draw, subtitle, small, 760), font=small, fill=MUTED)
    if card.place:
        draw.text((207, 148), f"#{card.place} на сервере по Elo", font=small, fill=GOLD)

    # Плитки с цифрами.
    winrate = f"{card.wins / card.games:.0%}" if card.games else "—"
    tiles = [
        ("Elo", f"{card.elo:.0f}"), ("Каток", str(card.games)), ("Победы", f"{card.wins} · {winrate}"),
        ("MVP", f"★ {card.mvp}" if card.mvp else "0"), ("Коины", str(card.coins) if card.coins is not None else "—"),
    ]
    x = 40
    for title, value in tiles:
        draw.rounded_rectangle((x, 200, x + 172, 290), radius=14, fill=PANEL)
        draw.text((x + 16, 210), title, font=tiny, fill=MUTED)
        draw.text((x + 16, 238), _fit(draw, value, mid, 145), font=mid, fill=TEXT)
        x += 186

    # График Elo.
    chart = (40, 310, 620, 520)
    draw.rounded_rectangle(chart, radius=14, fill=PANEL)
    draw.text((chart[0] + 16, chart[1] + 10), "Elo по каткам", font=tiny, fill=MUTED)
    points = card.elo_history[-30:]
    if len(points) >= 2:
        low, high = min(points), max(points)
        span = max(high - low, 20)
        left, right, top, bottom = chart[0] + 20, chart[2] - 20, chart[1] + 45, chart[3] - 20
        coords = [
            (left + (right - left) * index / (len(points) - 1), bottom - (bottom - top) * (value - low) / span)
            for index, value in enumerate(points)
        ]
        draw.line(coords, fill=ACCENT, width=4, joint="curve")
        draw.ellipse((coords[-1][0] - 6, coords[-1][1] - 6, coords[-1][0] + 6, coords[-1][1] + 6), fill=ACCENT)
        draw.text((right - 120, chart[1] + 10), f"{low:.0f}–{high:.0f}", font=tiny, fill=MUTED)
    else:
        draw.text((chart[0] + 16, chart[1] + 90), "Сыграй пару каток — тут появится график", font=small, fill=MUTED)

    # Форма и люди.
    side = (640, 310, 960, 520)
    draw.rounded_rectangle(side, radius=14, fill=PANEL)
    draw.text((side[0] + 16, side[1] + 10), "Последние катки", font=tiny, fill=MUTED)
    for index, won in enumerate(card.form[-10:]):
        left = side[0] + 16 + index * 29
        draw.rounded_rectangle((left, side[1] + 40, left + 23, side[1] + 63), radius=5, fill=WIN if won else LOSS)
    if not card.form:
        draw.text((side[0] + 16, side[1] + 40), "пока пусто", font=small, fill=MUTED)
    draw.text((side[0] + 16, side[1] + 85), "Лучший напарник", font=tiny, fill=MUTED)
    draw.text((side[0] + 16, side[1] + 108), _fit(draw, card.partner or "—", small, 290), font=small, fill=TEXT)
    draw.text((side[0] + 16, side[1] + 145), "Неудобный соперник", font=tiny, fill=MUTED)
    draw.text((side[0] + 16, side[1] + 168), _fit(draw, card.nemesis or "—", small, 290), font=small, fill=TEXT)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()
