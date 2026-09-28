"""/oleg-vpn — состояние VPN бота (Discord и нейросети): какой сервер выбран и перевыбор по кнопке.

Только для администраторов сервера, и ответ виден лишь вызвавшему.
Адресов, ключей и ссылки подписки здесь нет — только название сервера, страна и задержка.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import NEUTRAL, WARNING
from vpn import VPN_CLIENT

log = logging.getLogger("scrimbot.vpn_admin")

RECHECK_TIMEOUT = 240


def status_embed(status: dict) -> discord.Embed:
    if not status["enabled"]:
        return discord.Embed(
            title="VPN выключен",
            description="В `.env` не задан `VPN_SUBSCRIPTION` или `VPN_URL`.",
            color=WARNING,
        )
    working = status["server"] and status["running"]
    embed = discord.Embed(title="VPN бота", color=NEUTRAL if working else WARNING)
    if status["server"]:
        latency = f"{status['latency_ms']:.0f} мс" if status["latency_ms"] is not None else "—"
        embed.add_field(name="Сервер", value=status["server"], inline=False)
        embed.add_field(name="Страна выхода", value=status["country"] or "—")
        embed.add_field(name="Задержка", value=latency)
        embed.add_field(name="Фрагментация", value="да" if status["fragment"] else "нет")
    else:
        embed.description = (
            "Рабочий сервер пока не найден — Discord и нейросети идут напрямую, поиск продолжается."
            if status.get("direct") else "Рабочий сервер пока не найден — идёт проверка или все серверы недоступны."
        )
    check = status["last_check"]
    if check:
        embed.add_field(
            name="Последняя проверка",
            value=f"<t:{int(check['at'])}:R> · рабочих {check['working']} из {check['checked']}",
            inline=False,
        )
    embed.set_footer(text=f"Серверов в подписке к проверке: {status['servers']}")
    return embed


class RecheckView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @discord.ui.button(label="Перевыбрать сейчас", emoji="🔄", style=discord.ButtonStyle.primary)
    async def recheck(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Команда только для администраторов сервера.", ephemeral=True)
            return
        if not VPN_CLIENT.started:
            await interaction.response.send_message("VPN не запущен — смотрите логи бота.", ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(content="Проверяю все серверы — это до пары минут…", view=self)
        try:
            status = await asyncio.wait_for(VPN_CLIENT.recheck(), RECHECK_TIMEOUT)
        except TimeoutError:
            status = VPN_CLIENT.status()
        except Exception:
            log.exception("Перевыбор VPN по кнопке не удался")
            status = VPN_CLIENT.status()
        button.disabled = False
        await interaction.edit_original_response(content=None, embed=status_embed(status or VPN_CLIENT.status()), view=self)


class VpnAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="oleg-vpn", description="Какой VPN-сервер сейчас у нейросетей Олега")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def vpn_status(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Команда только для администраторов сервера.", ephemeral=True)
            return
        status = VPN_CLIENT.status()
        view = RecheckView() if status["enabled"] else None
        await interaction.response.send_message(embed=status_embed(status), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VpnAdmin(bot))
