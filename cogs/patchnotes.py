"""Патчноут Олежки: после обновления бота — пост в канале объявлений, что нового (changelog.py)."""
from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands

import changelog
import config
from storage import JsonStore
from utils import NEUTRAL

log = logging.getLogger("scrimbot.patchnotes")


def build_embed(entries: list[tuple[int, str, list[str]]], comment: str | None) -> discord.Embed:
    version = entries[0][0]
    items = [item for _version, _date, entry_items in entries for item in entry_items]
    embed = discord.Embed(
        title=f"📝 Патчноут Олежки · v{version}",
        description=(comment + "\n\n" if comment else "") + "\n".join(f"• {item}" for item in items),
        color=NEUTRAL,
    )
    embed.set_footer(text="Если что-то сломалось — пишите админам, я всё передам ♡")
    return embed


class PatchNotes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(Path(config.DATA_DIR) / "patchnotes.json")
        self.done = False

    async def cog_load(self) -> None:
        self.store.load()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.done or not config.PATCH_NOTES:
            return
        self.done = True
        latest = changelog.latest_version()
        for guild in self.bot.guilds:
            seen = self.store.data.get(str(guild.id))
            # Новая установка — рассказываем только о последнем обновлении, не обо всей истории.
            entries = changelog.entries_after(seen if seen is not None else latest - 1)
            if not entries:
                continue
            self.store.data[str(guild.id)] = latest
            await self.store.save()
            try:
                await self.post(guild, entries)
            except Exception:
                log.exception("Не удалось опубликовать патчноут на %s", guild)

    async def post(self, guild: discord.Guild, entries) -> None:
        stats = self.bot.get_cog("Stats")
        channel = stats.announce_channel(guild.id) if stats is not None else None
        if channel is None:
            log.info("Патчноут на %s: некуда писать (ANNOUNCE_CHANNEL_ID)", guild)
            return
        comment = None
        chat = self.bot.get_cog("Chat")
        if chat is not None and hasattr(chat, "oleg_line"):
            items = "; ".join(item for _v, _d, entry_items in entries for item in entry_items)
            comment = await chat.oleg_line(
                "Тебя только что обновили. Похвастайся всем в канале, что у тебя нового — коротко, 1–2 "
                f"предложения, позови попробовать самое интересное. Что нового: {items}",
                limit=350,
            )
        await channel.send(embed=build_embed(entries, comment), allowed_mentions=discord.AllowedMentions.none())
        log.info("Патчноут v%d опубликован на %s", entries[0][0], guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PatchNotes(bot))
