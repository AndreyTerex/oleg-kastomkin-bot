"""Панель выбора линий: кнопки, выдающие и снимающие роли LoL."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import NEUTRAL, member_lanes, respond

log = logging.getLogger("scrimbot.roles")

CUSTOM_ID_PREFIX = "lol:lane"


async def _toggle_lane(interaction: discord.Interaction, lane: config.Lane) -> None:
    """Выдаёт роль линии, а если она уже есть — снимает."""
    guild = interaction.guild
    if guild is None:
        await respond(interaction, "Эта кнопка работает только на сервере.")
        return

    role = guild.get_role(lane.role_id)
    if role is None:
        await respond(
            interaction,
            f"Роль для линии **{lane.label}** не найдена на сервере. "
            f"Проверьте переменную `ROLE_ID_{lane.key.upper()}` в настройках бота.",
        )
        return

    if role >= guild.me.top_role:
        await respond(
            interaction,
            f"Не могу управлять ролью {role.mention}: она находится выше роли бота. "
            "Поднимите роль бота в списке ролей сервера.",
        )
        return

    member = interaction.user
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Панель выбора линий")
            await respond(interaction, f"{lane.emoji} Роль **{lane.label}** снята.")
        else:
            await member.add_roles(role, reason="Панель выбора линий")
            await respond(interaction, f"{lane.emoji} Роль **{lane.label}** выдана.")
    except discord.Forbidden:
        await respond(
            interaction,
            "Боту не хватает права **«Управлять ролями»**. Выдайте его и попробуйте снова.",
        )


class LaneButton(discord.ui.Button):
    def __init__(self, lane: config.Lane) -> None:
        super().__init__(
            label=lane.label,
            emoji=lane.emoji,
            style=lane.style,
            custom_id=f"{CUSTOM_ID_PREFIX}:{lane.key}",
            row=0,
        )
        self.lane = lane

    async def callback(self, interaction: discord.Interaction) -> None:
        await _toggle_lane(interaction, self.lane)


class ClearLanesButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Снять все линии",
            emoji="🧹",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CUSTOM_ID_PREFIX}:clear",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        lanes = member_lanes(interaction.user)
        if not lanes:
            await respond(interaction, "У вас и так нет ролей линий.")
            return

        roles = [role for lane in lanes if (role := guild.get_role(lane.role_id)) is not None]
        try:
            await interaction.user.remove_roles(*roles, reason="Панель выбора линий: сброс")
        except discord.Forbidden:
            await respond(interaction, "Боту не хватает права **«Управлять ролями»**.")
            return
        await respond(interaction, f"Роли линий сняты: {', '.join(r.name for r in roles)}.")


class LaneRolesView(discord.ui.View):
    """Постоянная панель — работает и после перезапуска бота."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for lane in config.LANES:
            self.add_item(LaneButton(lane))
        self.add_item(ClearLanesButton())


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Ваши линии в League of Legends",
        description=(
            "Отметьте линии, на которых готовы играть. Нажатие выдаёт роль, "
            "повторное нажатие — снимает её. Линий можно выбрать сколько угодно.\n\n"
            "По этим ролям бот собирает составы для скримов и подбирает замены."
        ),
        color=NEUTRAL,
    )
    embed.add_field(
        name="Доступные линии",
        value="\n".join(
            f"{lane.emoji} **{lane.label}** ({lane.title}) — {lane.description}"
            for lane in config.LANES
        ),
        inline=False,
    )
    embed.set_footer(text="Ответы бота видны только вам")
    return embed


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Регистрируем панель как постоянную, чтобы кнопки жили после рестарта.
        self.bot.add_view(LaneRolesView())

    @app_commands.command(name="roles", description="Опубликовать панель выбора линий LoL")
    @app_commands.describe(channel="Канал для панели (по умолчанию — текущий)")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def roles_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        permissions = target.permissions_for(interaction.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await respond(
                interaction,
                f"Боту нужны права **«Отправлять сообщения»** и **«Встраивать ссылки»** в {target.mention}.",
            )
            return

        await target.send(embed=build_panel_embed(), view=LaneRolesView())
        await respond(interaction, f"Панель выбора линий опубликована в {target.mention}.")

    @app_commands.command(name="lanes", description="Показать, кто на какой линии играет")
    @app_commands.guild_only()
    async def lanes_overview(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        embed = discord.Embed(title="Игроки по линиям", color=NEUTRAL)
        for lane in config.LANES:
            role = guild.get_role(lane.role_id)
            members = sorted(role.members, key=lambda m: m.display_name.lower()) if role else []
            value = ", ".join(m.display_name for m in members) if members else "никого"
            if len(value) > 1000:
                value = value[:997] + "…"
            embed.add_field(
                name=f"{lane.emoji} {lane.label} — {len(members)}",
                value=value,
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
