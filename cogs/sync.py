"""Фоновая часть синхронизации (sync.py): раз в минуту обновляет замок, раз в SYNC_MINUTES — снимок данных."""
from __future__ import annotations

import logging

from discord.ext import commands, tasks

import config

log = logging.getLogger("scrimbot.sync")


class SyncCog(commands.Cog, name="Sync"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sync = getattr(bot, "sync", None)
        self.ticks = 0

    async def cog_load(self) -> None:
        if self.sync is not None and self.sync.enabled:
            self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    @tasks.loop(minutes=1)
    async def loop(self) -> None:
        try:
            await self.sync.heartbeat()
            if self.ticks % config.SYNC_MINUTES == 0:
                await self.sync.push()
        except Exception:
            log.exception("Ошибка синхронизации")
        self.ticks += 1


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SyncCog(bot))
