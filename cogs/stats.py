"""Статистика каток: /stats и /leaderboard. Результаты отмечаются кнопками в посте сбора."""
from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from stats import Stats, plural
from utils import NEUTRAL, player_name, respond

log = logging.getLogger("scrimbot.stats")

# В таблицу лидеров попадают только те, кто сыграл хотя бы столько каток — иначе 1/1 = 100% всех обгонит.
LEADERBOARD_MIN_GAMES = 3
LEADERBOARD_SIZE = 10
MEDALS = ("🥇", "🥈", "🥉")


class StatsCog(commands.Cog, name="Stats"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.stats = Stats(Path(config.DATA_DIR) / "stats.json")

    async def cog_load(self) -> None:
        self.stats.load()

    @app_commands.command(name="stats", description="Статистика каток игрока на этом сервере")
    @app_commands.describe(player="Чья статистика (по умолчанию — ваша)")
    @app_commands.guild_only()
    async def show_stats(self, interaction: discord.Interaction, player: discord.Member | None = None) -> None:
        member = player or interaction.user
        record = self.stats.player(interaction.guild.id, member.id)
        embed = discord.Embed(title=f"📊 {player_name(member)}", color=NEUTRAL)
        if not record.games:
            embed.description = "Ещё ни одной отмеченной катки. Результат отмечает организатор кнопками в посте сбора."
        else:
            embed.add_field(name="Каток", value=str(record.games))
            embed.add_field(name="Победы", value=f"{record.wins} ({record.winrate:.0%})")
            embed.add_field(name="Поражения", value=str(record.losses))
            place = self.place_of(interaction.guild.id, member.id)
            if place:
                embed.set_footer(text=f"{place} место на сервере по рейтингу")
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    def ranking(self, guild_id: int) -> list[tuple[int, object]]:
        players = [
            (user_id, record)
            for user_id, record in self.stats.all_players(guild_id).items()
            if record.games >= LEADERBOARD_MIN_GAMES
        ]
        return sorted(players, key=lambda item: (-item[1].rating, -item[1].games))

    def place_of(self, guild_id: int, user_id: int) -> int | None:
        for index, (other_id, _record) in enumerate(self.ranking(guild_id), start=1):
            if other_id == user_id:
                return index
        return None

    @app_commands.command(name="leaderboard", description="Лучшие игроки сервера по статистике каток")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        ranking = self.ranking(interaction.guild.id)
        if not ranking:
            await respond(
                interaction,
                f"Таблица пуста: нужны игроки хотя бы с {LEADERBOARD_MIN_GAMES} "
                "отмеченными катками. Победы отмечаются кнопками в посте сбора.",
            )
            return
        lines = []
        for index, (user_id, record) in enumerate(ranking[:LEADERBOARD_SIZE], start=1):
            member = interaction.guild.get_member(user_id)
            name = player_name(member) if member else f"<@{user_id}>"
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`{index}.`"
            lines.append(f"{prefix} **{name}** — {record.wins}/{record.games} ({record.winrate:.0%})")
        games = self.stats.games_count(interaction.guild.id)
        embed = discord.Embed(title="🏆 Таблица лидеров", description="\n".join(lines), color=NEUTRAL)
        embed.set_footer(
            text=f"Сыграно {games} {plural(games, 'катка', 'катки', 'каток')} · "
            f"в таблице — от {LEADERBOARD_MIN_GAMES} каток · порядок по сглаженному винрейту"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
