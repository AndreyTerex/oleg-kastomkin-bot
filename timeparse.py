"""Время сбора из свободного текста: «сегодня 21:00», «завтра в 19», «пт 20:30», «29.09 21:00».

Организатор пишет как привык, а бот превращает это в настоящую дату: Discord показывает её каждому
в его часовом поясе, а за несколько минут до начала бот зовёт записавшихся.
Не разобрали — не страшно: сбор работает как раньше, просто без даты и напоминания.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "пн": 0, "понедельник": 0, "вт": 1, "вторник": 1, "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3, "пт": 4, "пятница": 4, "пятницу": 4, "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}
RELATIVE = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

COLON_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:ч]([0-5]\d)(?!\d)")
DOTTED = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d.])")
BARE_HOUR = re.compile(r"(?<!\w)(?:в|к)\s+([01]?\d|2[0-3])(?![\d:.])")
WORD = re.compile(r"[а-яё]+", re.IGNORECASE)


def parse_when(text: str, now: datetime) -> datetime | None:
    """Дата и время начала в часовом поясе `now` или None, если время в тексте не нашлось."""
    lowered = text.casefold()
    day: date | None = None
    clock: time | None = None

    colon = COLON_TIME.search(lowered)
    if colon:
        clock = time(int(colon.group(1)), int(colon.group(2)))

    for match in DOTTED.finditer(lowered):
        first, second, year = match.groups()
        as_date = _date(int(first), int(second), year, now)
        # «21.00» — это время, а «29.09» — дата; «12.10» без другого времени считаем временем.
        if as_date is not None and (clock is not None or _another_dotted(lowered, match)):
            day = day or as_date
        elif clock is None and int(first) < 24 and int(second) < 60 and not year:
            clock = time(int(first), int(second))

    if clock is None and (bare := BARE_HOUR.search(lowered)):
        clock = time(int(bare.group(1)), 0)
    if clock is None:
        return None

    explicit_day = day is not None
    if day is None:
        for word in WORD.findall(lowered):
            if word in RELATIVE:
                day, explicit_day = now.date() + timedelta(days=RELATIVE[word]), True
                break
            if word in WEEKDAYS:
                ahead = (WEEKDAYS[word] - now.weekday()) % 7
                day, explicit_day = now.date() + timedelta(days=ahead), True
                if ahead == 0 and datetime.combine(day, clock, now.tzinfo) < now:
                    day += timedelta(days=7)
                break
    if day is None:
        day = now.date()

    result = datetime.combine(day, clock, tzinfo=now.tzinfo)
    # «21:00», написанное в 23:00, — это завтра, а не прошлое.
    if not explicit_day and result < now:
        result += timedelta(days=1)
    return result


def _date(day: int, month: int, year: str | None, now: datetime) -> date | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    full_year = now.year if not year else int(year) + (2000 if len(year) == 2 else 0)
    try:
        result = date(full_year, month, day)
    except ValueError:
        return None
    # «05.01», написанное в декабре, — это январь следующего года.
    if not year and result < now.date() - timedelta(days=1):
        try:
            result = date(full_year + 1, month, day)
        except ValueError:
            return None
    return result


def _another_dotted(text: str, match: re.Match[str]) -> bool:
    """Есть ли в тексте ещё одно «чч.мм» — тогда первое из них дата."""
    return any(other.start() != match.start() for other in DOTTED.finditer(text))
