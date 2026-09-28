"""Долгая память Олега: прозвища и факты о людях, которые его попросили запомнить.

Хранится на диске (data/memory.json) отдельно по серверам и переживает перезапуски.
Олег запоминает только по просьбе («Олег, запомни…»), сам ничего не собирает.
"""
from __future__ import annotations

import time
from pathlib import Path

from storage import JsonStore

MAX_PER_PERSON = 8
MAX_GENERAL = 12
FACT_LIMIT = 160


class Memory:
    def __init__(self, path: Path) -> None:
        self.store = JsonStore(path)

    def load(self) -> None:
        self.store.load()

    def _guild(self, guild_id: int) -> dict:
        guild = self.store.data.setdefault(str(guild_id), {})
        guild.setdefault("people", {})
        guild.setdefault("general", [])
        return guild

    def about(self, guild_id: int, user_id: int) -> list[str]:
        return [entry["fact"] for entry in self._guild(guild_id)["people"].get(str(user_id), [])]

    def general(self, guild_id: int) -> list[str]:
        return [entry["fact"] for entry in self._guild(guild_id)["general"]]

    def people(self, guild_id: int) -> dict[int, list[str]]:
        return {
            int(user_id): [entry["fact"] for entry in entries]
            for user_id, entries in self._guild(guild_id)["people"].items()
            if entries
        }

    async def remember(self, guild_id: int, user_id: int | None, fact: str, author_id: int) -> bool:
        """Запоминает факт о человеке (или о сервере, если user_id нет). False — такое уже помнит."""
        fact = " ".join(fact.split())[:FACT_LIMIT]
        guild = self._guild(guild_id)
        bucket = guild["people"].setdefault(str(user_id), []) if user_id else guild["general"]
        if any(entry["fact"].casefold() == fact.casefold() for entry in bucket):
            return False
        bucket.append({"fact": fact, "by": author_id, "at": time.time()})
        # Старые факты вытесняются новыми, чтобы подсказка для модели не разрасталась.
        limit = MAX_PER_PERSON if user_id else MAX_GENERAL
        del bucket[:-limit]
        await self.store.save()
        return True

    async def forget(self, guild_id: int, user_id: int | None) -> int:
        """Забывает всё о человеке (или общие факты о сервере). Возвращает, сколько забыл."""
        guild = self._guild(guild_id)
        if user_id:
            removed = guild["people"].pop(str(user_id), [])
        else:
            removed, guild["general"] = guild["general"], []
        await self.store.save()
        return len(removed)
