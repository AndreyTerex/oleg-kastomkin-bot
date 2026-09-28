"""Развлекательные рандомайзеры: чемпионы и линии."""
from __future__ import annotations

import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from champions import POOL, Champion, ChampionsUnavailable
from utils import NEUTRAL, WARNING, respond, resolve_voice_channel, shuffled, voice_members

log = logging.getLogger("scrimbot.extras")


class Extras(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _champions_or_error(self, interaction: discord.Interaction) -> list[Champion] | None:
        try:
            return await POOL.all()
        except ChampionsUnavailable:
            log.exception("Не удалось получить список чемпионов из Data Dragon")
            embed = discord.Embed(
                title="Список чемпионов недоступен",
                description="Не удалось связаться с сервисом Riot Data Dragon. Попробуйте ещё раз через минуту.",
                color=WARNING,
            )
            await respond(interaction, embed=embed)
            return None

    @app_commands.command(name="champion", description="Случайный чемпион")
    @app_commands.describe(count="Сколько чемпионов выдать (1–10, по умолчанию 1)")
    async def champion(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        await interaction.response.defer()
        champions = await self._champions_or_error(interaction)
        if champions is None:
            return

        picked = random.sample(champions, count)
        embed = discord.Embed(
            title="Случайный чемпион" if count == 1 else "Случайные чемпионы",
            description="\n".join(f"• **{champion.name}**" for champion in picked),
            color=NEUTRAL,
        )
        if count == 1:
            embed.set_thumbnail(url=POOL.portrait(picked[0]))
        embed.set_footer(text=f"Патч {POOL.version}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="champion-roll", description="Раздать всем в голосовом канале случайных чемпионов")
    @app_commands.describe(channel="Голосовой канал (по умолчанию — ваш текущий)")
    @app_commands.guild_only()
    async def champion_roll(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        voice = resolve_voice_channel(interaction, channel)
        if voice is None:
            await respond(interaction, "Зайдите в голосовой канал или укажите его параметром `channel`.")
            return

        players = voice_members(voice)
        if not players:
            await respond(interaction, f"В канале **{voice.name}** сейчас никого нет.")
            return

        await interaction.response.defer()
        champions = await self._champions_or_error(interaction)
        if champions is None:
            return

        picked = random.sample(champions, min(len(players), len(champions)))
        embed = discord.Embed(
            title="Рулетка чемпионов",
            description="\n".join(
                f"{player.mention} — **{champion.name}**" for player, champion in zip(players, picked)
            ),
            color=NEUTRAL,
        )
        embed.set_footer(text=f"Канал: {voice.name} · патч {POOL.version}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="lane-roll", description="Раздать линии случайным образом, не глядя на роли")
    @app_commands.describe(channel="Голосовой канал (по умолчанию — ваш текущий)")
    @app_commands.guild_only()
    async def lane_roll(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        voice = resolve_voice_channel(interaction, channel)
        if voice is None:
            await respond(interaction, "Зайдите в голосовой канал или укажите его параметром `channel`.")
            return

        players = shuffled(voice_members(voice))
        if not players:
            await respond(interaction, f"В канале **{voice.name}** сейчас никого нет.")
            return

        lines = []
        for index, player in enumerate(players):
            lane = config.LANES[index % len(config.LANES)]
            lines.append(f"{lane.emoji} **{lane.label}** — {player.mention}")

        embed = discord.Embed(title="Рулетка линий", description="\n".join(lines), color=NEUTRAL)
        embed.set_footer(text=f"Канал: {voice.name} · роли игроков не учитываются")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Extras(bot))
