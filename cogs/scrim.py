"""Инструменты для катки: капитаны, деление на команды, драфт, стороны."""
from __future__ import annotations

import logging
import random
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from utils import (
    BLUE,
    BLUE_SIDE,
    NEUTRAL,
    RED,
    RED_SIDE,
    format_players,
    format_roster,
    member_lanes,
    respond,
    resolve_voice_channel,
    shuffled,
    split_by_lanes,
    voice_members,
)

log = logging.getLogger("scrimbot.scrim")

SIDE_NAMES = (BLUE_SIDE, RED_SIDE)
SIDE_COLORS = (BLUE, RED)


def draft_order(picks: int) -> list[int]:
    """Змейка 1-2-2-1-1-2-2-1: первым выбирает первый капитан, дальше по два пика."""
    return [((index + 1) // 2) % 2 for index in range(picks)]


class CaptainsView(discord.ui.View):
    """Кнопки под результатом жеребьёвки капитанов."""

    def __init__(self, cog: "Scrim", author: discord.Member, players: list[discord.Member]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author = author
        self.players = players
        self.captains: list[discord.Member] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await respond(interaction, "Этими кнопками управляет тот, кто вызвал команду.")
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Перебросить", emoji="🎲", style=discord.ButtonStyle.secondary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.captains = random.sample(self.players, len(self.captains))
        await interaction.response.edit_message(
            embed=self.cog.captains_embed(self.captains, self.players), view=self
        )

    @discord.ui.button(label="Начать драфт", emoji="📋", style=discord.ButtonStyle.primary)
    async def start_draft(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if len(self.captains) != 2:
            await respond(interaction, "Для драфта нужно ровно два капитана.")
            return

        pool = [player for player in self.players if player not in self.captains]
        if not pool:
            await respond(interaction, "Кроме капитанов в канале никого нет.")
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        view = DraftView(self.cog, list(self.captains), shuffled(pool))
        view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
        self.stop()


class PlayerSelect(discord.ui.Select):
    """Выпадающий список свободных игроков."""

    def __init__(self, draft: "DraftView") -> None:
        super().__init__(placeholder="Выберите игрока в свою команду", min_values=1, max_values=1)
        self.draft = draft
        self.refresh()

    def refresh(self) -> None:
        options = []
        for player in self.draft.pool[:25]:
            lanes = member_lanes(player)
            options.append(
                discord.SelectOption(
                    label=player.display_name[:100],
                    value=str(player.id),
                    description=", ".join(lane.label for lane in lanes)[:100] or None,
                )
            )
        self.options = options
        self.disabled = not options

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.draft.handle_pick(interaction, int(self.values[0]))


class DraftView(discord.ui.View):
    """Поочерёдный набор игроков капитанами."""

    def __init__(
        self,
        cog: "Scrim",
        captains: list[discord.Member],
        pool: list[discord.Member],
        on_finish: Callable[[list[list[discord.Member]]], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.captains = captains
        self.pool = list(pool)
        # Вызывается, когда драфт закончен: так лобби узнаёт итоговые составы.
        self.on_finish = on_finish
        self.teams: list[list[discord.Member]] = [[captains[0]], [captains[1]]]
        self.order = draft_order(len(self.pool))
        self.turn = 0
        self.message: discord.Message | None = None
        self.select = PlayerSelect(self)
        self.add_item(self.select)

    @property
    def current_captain(self) -> discord.Member | None:
        if self.turn >= len(self.order):
            return None
        return self.captains[self.order[self.turn]]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Разбирать игроков могут только капитаны — остальным меню не отвечает."""
        if interaction.user.id in {captain.id for captain in self.captains}:
            return True
        names = " и ".join(captain.mention for captain in self.captains)
        await respond(interaction, f"Игроков разбирают только капитаны: {names}.")
        return False

    def build_embed(self) -> discord.Embed:
        finished = not self.pool
        embed = discord.Embed(
            title="Драфт составов",
            description=(
                "Драфт завершён. Хорошей игры!"
                if finished
                else "Игроков разбирают только капитаны, по очереди 1-2-2-1."
            ),
            color=discord.Color.green() if finished else NEUTRAL,
        )
        for side, team in enumerate(self.teams):
            embed.add_field(
                name=f"{SIDE_NAMES[side]} ({len(team)})",
                value=format_players(
                    team, numbered=True, show_lanes=False, mention=False, captain=self.captains[side]
                ),
                inline=True,
            )

        if finished:
            embed.set_footer(text="Развести по каналам: /split")
        else:
            embed.add_field(
                name="Свободные игроки",
                value=format_players(self.pool, mention=False),
                inline=False,
            )
            captain = self.current_captain
            embed.add_field(
                name="Сейчас выбирает",
                value=captain.mention if captain else "—",
                inline=False,
            )
        return embed

    async def handle_pick(self, interaction: discord.Interaction, player_id: int) -> None:
        captain = self.current_captain
        if captain is None:
            await respond(interaction, "Драфт уже завершён.")
            return
        if interaction.user.id != captain.id:
            await respond(interaction, f"Сейчас ход капитана {captain.mention}.")
            return

        player = discord.utils.get(self.pool, id=player_id)
        if player is None:
            await respond(interaction, "Этого игрока уже забрали — выберите другого.")
            return

        self.pool.remove(player)
        self.teams[self.order[self.turn]].append(player)
        self.turn += 1

        finished = not self.pool
        if finished:
            self.cog.last_teams[interaction.guild.id] = (list(self.teams[0]), list(self.teams[1]))
            self.select.disabled = True
            self.stop()
        else:
            self.select.refresh()

        await interaction.response.edit_message(embed=self.build_embed(), view=self)

        if finished and self.on_finish is not None:
            await self.on_finish(self.teams)

    async def on_timeout(self) -> None:
        self.select.disabled = True
        if self.message is None:
            return
        embed = self.build_embed()
        embed.color = discord.Color.dark_grey()
        embed.set_footer(text="Время на драфт вышло — запустите /draft заново")
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("Не удалось обновить сообщение драфта после таймаута")


class Scrim(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Последние составы по серверам — их использует /split.
        self.last_teams: dict[int, tuple[list[discord.Member], list[discord.Member]]] = {}

    async def _players_from_voice(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None,
        minimum: int,
    ) -> tuple[discord.VoiceChannel, list[discord.Member]] | None:
        """Канал и его состав, либо понятное объяснение, чего не хватает."""
        voice = resolve_voice_channel(interaction, channel)
        if voice is None:
            await respond(interaction, "Зайдите в голосовой канал или укажите его параметром `channel`.")
            return None

        players = voice_members(voice)
        if len(players) < minimum:
            await respond(
                interaction,
                f"В канале **{voice.name}** сейчас {len(players)} чел., а нужно минимум {minimum}.",
            )
            return None
        return voice, players

    def captains_embed(self, captains: list[discord.Member], players: list[discord.Member]) -> discord.Embed:
        embed = discord.Embed(
            title="Жеребьёвка капитанов",
            description="Капитаны выбраны случайно среди участников голосового канала.",
            color=NEUTRAL,
        )
        for index, captain in enumerate(captains):
            name = SIDE_NAMES[index] if index < 2 else f"Капитан {index + 1}"
            embed.add_field(name=name, value=f"👑 {captain.mention}", inline=True)
        embed.set_footer(text=f"Участвовали в жеребьёвке: {len(players)} чел.")
        return embed

    @app_commands.command(name="captains", description="Случайно выбрать капитанов из голосового канала")
    @app_commands.describe(
        channel="Голосовой канал (по умолчанию — ваш текущий)",
        count="Сколько капитанов выбрать (1–4, по умолчанию 2)",
    )
    @app_commands.guild_only()
    async def captains(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        count: app_commands.Range[int, 1, 4] = 2,
    ) -> None:
        result = await self._players_from_voice(interaction, channel, minimum=count)
        if result is None:
            return
        _voice, players = result

        view = CaptainsView(self, interaction.user, players)
        view.captains = random.sample(players, count)
        await interaction.response.send_message(
            embed=self.captains_embed(view.captains, players), view=view
        )

    @app_commands.command(name="teams", description="Разделить голосовой канал на две команды")
    @app_commands.describe(
        channel="Голосовой канал (по умолчанию — ваш текущий)",
        by_lanes="Собрать составы по линиям, опираясь на роли игроков",
    )
    @app_commands.guild_only()
    async def teams(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        by_lanes: bool = False,
    ) -> None:
        result = await self._players_from_voice(interaction, channel, minimum=2)
        if result is None:
            return
        voice, players = result

        rosters = split_by_lanes(players) if by_lanes else None
        note = None
        if by_lanes and rosters is None:
            note = (
                "Разложить по линиям не вышло: для этого нужны ровно 10 игроков "
                "и подходящий набор ролей. Составы поделены случайно."
            )

        embed = discord.Embed(title="Составы на катку", color=NEUTRAL)
        if rosters is not None:
            blue_roster, red_roster = rosters
            blue = [member for _lane, member in blue_roster]
            red = [member for _lane, member in red_roster]
            embed.add_field(name=BLUE_SIDE, value=format_roster(blue_roster), inline=True)
            embed.add_field(name=RED_SIDE, value=format_roster(red_roster), inline=True)
        else:
            mixed = shuffled(players)
            half = (len(mixed) + 1) // 2
            blue, red = mixed[:half], mixed[half:]
            embed.add_field(
                name=f"{BLUE_SIDE} ({len(blue)})",
                value=format_players(blue, numbered=True, mention=False),
                inline=True,
            )
            embed.add_field(
                name=f"{RED_SIDE} ({len(red)})",
                value=format_players(red, numbered=True, mention=False),
                inline=True,
            )

        if note:
            embed.add_field(name="Обратите внимание", value=note, inline=False)
        embed.set_footer(text=f"Канал: {voice.name} · развести по каналам — /split")

        self.last_teams[interaction.guild.id] = (blue, red)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="draft", description="Драфт: капитаны по очереди набирают команды")
    @app_commands.describe(
        channel="Голосовой канал с игроками (по умолчанию — ваш текущий)",
        captain_1="Первый капитан (по умолчанию — случайный)",
        captain_2="Второй капитан (по умолчанию — случайный)",
    )
    @app_commands.guild_only()
    async def draft(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        captain_1: discord.Member | None = None,
        captain_2: discord.Member | None = None,
    ) -> None:
        result = await self._players_from_voice(interaction, channel, minimum=3)
        if result is None:
            return
        _voice, players = result

        chosen = [captain for captain in (captain_1, captain_2) if captain is not None]
        if len(chosen) == 2 and chosen[0].id == chosen[1].id:
            await respond(interaction, "Капитаны должны быть разными людьми.")
            return
        for captain in chosen:
            if captain not in players:
                await respond(interaction, f"{captain.mention} сейчас не в голосовом канале.")
                return

        rest = [player for player in players if player not in chosen]
        while len(chosen) < 2:
            chosen.append(rest.pop(random.randrange(len(rest))))

        view = DraftView(self, chosen, shuffled(rest))
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="split", description="Развести составы по голосовым каналам")
    @app_commands.describe(blue="Канал для синей стороны", red="Канал для красной стороны")
    @app_commands.default_permissions(move_members=True)
    @app_commands.guild_only()
    async def split(
        self,
        interaction: discord.Interaction,
        blue: discord.VoiceChannel,
        red: discord.VoiceChannel,
    ) -> None:
        teams = self.last_teams.get(interaction.guild.id)
        if not teams:
            await respond(interaction, "Составы ещё не сформированы — сначала выполните `/teams` или `/draft`.")
            return
        if not interaction.guild.me.guild_permissions.move_members:
            await respond(interaction, "Боту нужно право **«Перемещать участников»**.")
            return

        await interaction.response.defer()
        moved: int = 0
        skipped: list[discord.Member] = []
        for team, target in zip(teams, (blue, red)):
            for member in team:
                if member.voice is None:
                    skipped.append(member)
                    continue
                try:
                    await member.move_to(target, reason="Разведение составов перед каткой")
                    moved += 1
                except discord.HTTPException:
                    skipped.append(member)

        lines = [f"Перемещено игроков: **{moved}** → {blue.mention} и {red.mention}."]
        if skipped:
            lines.append(
                "Остались на месте (нет в голосовом канале): "
                + ", ".join(member.mention for member in skipped)
            )
        await interaction.followup.send("\n".join(lines))

    @app_commands.command(name="side", description="Разыграть стороны: синие или красные")
    @app_commands.describe(team_1="Название первой команды", team_2="Название второй команды")
    @app_commands.guild_only()
    async def side(
        self,
        interaction: discord.Interaction,
        team_1: str | None = None,
        team_2: str | None = None,
    ) -> None:
        if team_1 and team_2:
            first, second = shuffled((team_1, team_2))
            description = f"{BLUE_SIDE} — **{first}**\n{RED_SIDE} — **{second}**"
        else:
            description = f"Выпало: **{random.choice(SIDE_NAMES)}**"

        embed = discord.Embed(
            title="Розыгрыш стороны", description=description, color=random.choice(SIDE_COLORS)
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pick-player", description="Случайно выбрать игрока из голосового канала")
    @app_commands.describe(
        channel="Голосовой канал (по умолчанию — ваш текущий)",
        count="Сколько человек выбрать (1–10, по умолчанию 1)",
        reason="Для чего выбираем, например «первый бан»",
    )
    @app_commands.guild_only()
    async def pick_player(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
        count: app_commands.Range[int, 1, 10] = 1,
        reason: str | None = None,
    ) -> None:
        result = await self._players_from_voice(interaction, channel, minimum=count)
        if result is None:
            return
        voice, players = result

        picked = random.sample(players, count)
        embed = discord.Embed(
            title="Случайный выбор",
            description=format_players(picked, numbered=count > 1),
            color=NEUTRAL,
        )
        if reason:
            embed.add_field(name="Повод", value=reason, inline=False)
        embed.set_footer(text=f"Канал: {voice.name} · участников: {len(players)}")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scrim(bot))
