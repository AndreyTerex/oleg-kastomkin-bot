"""/profile — карточка игрока картинкой."""
from __future__ import annotations

import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import profile_card
from stats import best_partner, player_games, rivals
from utils import member_lanes, player_name

log = logging.getLogger("scrimbot.profile")


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="Карточка игрока: Elo, график, форма, напарник")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, player: discord.Member | None = None) -> None:
        member = player or interaction.user
        await interaction.response.defer(thinking=True)
        stats_cog = self.bot.get_cog("Stats")
        guild = interaction.guild
        record = stats_cog.stats.player(guild.id, member.id)
        games = stats_cog.stats.games_since(guild.id, 0)
        mine = player_games(games, member.id)

        def name(user_id: int) -> str:
            other = guild.get_member(user_id)
            return player_name(other) if other else "кто-то ушедший"

        partner = best_partner(games, member.id)
        nemesis, _victim = rivals(games, member.id)
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            avatar = None
        card = profile_card.CardData(
            name=player_name(member),
            elo=record.elo, games=record.games, wins=record.wins, losses=record.losses, mvp=record.mvp,
            place=stats_cog.place_of(guild.id, member.id),
            coins=stats_cog.wallets.balance(guild.id, member.id),
            lanes=", ".join(lane.label for lane in member_lanes(member)),
            elo_history=profile_card.elo_history(record.elo, [delta for _won, delta in mine]),
            form=[won for won, _delta in mine],
            partner=f"{name(partner[0])} · {partner[1]}/{partner[2]}" if partner else "",
            nemesis=f"{name(nemesis.user_id)} · {nemesis.wins}:{nemesis.losses}" if nemesis else "",
            avatar=avatar,
        )
        started = time.monotonic()
        data = await self.bot.loop.run_in_executor(None, profile_card.render, card)
        log.info("Карточка %s нарисована за %.2f с", member, time.monotonic() - started)
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="profile.png"))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
