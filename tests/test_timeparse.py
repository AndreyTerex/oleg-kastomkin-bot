from datetime import datetime
from zoneinfo import ZoneInfo

from timeparse import parse_when

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 28, 18, 0, tzinfo=MSK)  # понедельник


def at(*args):
    return datetime(*args, tzinfo=MSK)


def test_today_and_plain_time():
    assert parse_when("сегодня 21:00", NOW) == at(2026, 9, 28, 21, 0)
    assert parse_when("21:30", NOW) == at(2026, 9, 28, 21, 30)
    assert parse_when("в 21.00", NOW) == at(2026, 9, 28, 21, 0)


def test_plain_time_in_the_past_means_tomorrow():
    assert parse_when("10:00", NOW) == at(2026, 9, 29, 10, 0)


def test_relative_days_and_bare_hour():
    assert parse_when("завтра в 19", NOW) == at(2026, 9, 29, 19, 0)
    assert parse_when("послезавтра 20:15", NOW) == at(2026, 9, 30, 20, 15)


def test_weekdays():
    assert parse_when("пт 20:30", NOW) == at(2026, 10, 2, 20, 30)
    assert parse_when("в пятницу в 20", NOW) == at(2026, 10, 2, 20, 0)
    assert parse_when("пн 12:00", NOW) == at(2026, 10, 5, 12, 0)  # сегодняшний полдень уже прошёл


def test_explicit_dates():
    assert parse_when("29.09 21:00", NOW) == at(2026, 9, 29, 21, 0)
    assert parse_when("03.10 20.00", NOW) == at(2026, 10, 3, 20, 0)
    assert parse_when("05.01 18:00", NOW) == at(2027, 1, 5, 18, 0)


def test_no_time():
    assert parse_when("вечером, как соберёмся", NOW) is None
    assert parse_when("после работы", NOW) is None
