"""Единые правила записи в сбор, независимые от кнопок и Discord."""
from __future__ import annotations


class RosterError(ValueError):
    pass


def set_waiting(record: dict, user_id: int, waiting: bool) -> None:
    queue = record.setdefault("waiting", [])
    if waiting and user_id not in queue:
        queue.append(user_id)
    elif not waiting and user_id in queue:
        queue.remove(user_id)


def change_roster(record: dict, user_ids: list[int], status: str | None, *, default_target: int = 5):
    """Меняет статус одинаково для кнопки, правки организатора и просьбы в чате."""
    if record.get("closed"):
        raise RosterError("Этот сбор уже закрыт.")
    if status not in ("in", "sub", "out", None):
        raise ValueError("Неизвестный статус записи")
    # Готовые команды — снимок участников игры. Их нельзя незаметно рассинхронизировать.
    if record.get("stage", "signup") != "signup" or record.get("teams"):
        raise RosterError("Команды уже собираются или играют. Организатору: «Управление» → «Замена игрока» "
                          "(поменять одного) или «Пересобрать составы».")
    changed, overflow = [], []
    for user_id in dict.fromkeys(user_ids):
        # Повторное нажатие не переставляет человека в конец очереди.
        if status is not None and user_id in record.get(status, []):
            changed.append(user_id)
            if status == "sub":
                set_waiting(record, user_id, False)
            continue
        if status == "in" and user_id in record.get("waiting", []) and len(record.get("in", [])) >= record.get("target", default_target):
            overflow.append(user_id)
            continue
        for key in ("in", "sub", "out"):
            if user_id in record.setdefault(key, []):
                record[key].remove(user_id)
        full = status == "in" and len(record["in"]) >= record.get("target", default_target)
        set_waiting(record, user_id, full)
        if full:
            record["sub"].append(user_id)
            overflow.append(user_id)
        else:
            if status is not None:
                record[status].append(user_id)
            changed.append(user_id)
    return changed, overflow


def substitute(record: dict, old: int, new: int) -> int:
    """Замена в уже поделённых командах: new встаёт на место old (та же команда, линия, чемпионы, капитанство).

    old уходит в запас (без очереди), счёт серии и сыгранные катки не меняются. Возвращает сторону (0/1)."""
    if record.get("closed"):
        raise RosterError("Этот сбор уже закрыт.")
    teams = record.get("teams") or []
    if len(teams) != 2 or record.get("stage") != "done":
        raise RosterError("Замена — когда команды уже поделены. До этого правьте запись через «Правка состава».")
    if old == new:
        raise RosterError("Это один и тот же человек.")
    side = next((index for index, team in enumerate(teams) if old in team), None)
    if side is None:
        raise RosterError("Этого игрока нет в командах.")
    if any(new in team for team in teams):
        raise RosterError("Новый игрок уже в одной из команд — поменять местами можно через «Пересобрать составы».")
    team = teams[side]
    team[team.index(old)] = new

    deal = record.get("deal") or {}
    for key in ("lanes", "champions"):
        part = deal.get(key) or {}
        if str(old) in part:
            part[str(new)] = part.pop(str(old))
    captains = record.get("captains") or []
    if old in captains:
        captains[captains.index(old)] = new

    for key in ("in", "sub", "out"):
        if new in record.setdefault(key, []):
            record[key].remove(new)
    set_waiting(record, new, False)
    if old in record["in"]:
        record["in"][record["in"].index(old)] = new
    else:
        record["in"].append(new)
    if old not in record["sub"]:
        record["sub"].append(old)
    set_waiting(record, old, False)
    return side
