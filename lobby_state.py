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
        raise RosterError("Команды уже собираются или играют. Организатору: «Управление» → «Пересобрать составы», затем поправьте запись.")
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
