"""Точка входа: бот для организации скримов и каток по League of Legends."""
from __future__ import annotations

import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

import champions
import config
import portraits
from vpn import VPN_CLIENT
from utils import WARNING, respond

log = logging.getLogger("scrimbot")

EXTENSIONS = ("cogs.roles", "cogs.scrim", "cogs.stats", "cogs.lobby", "cogs.extras", "cogs.chat", "cogs.vpn_admin")


class ScrimBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Members — чтобы видеть роли участников, Voice States — чтобы читать состав каналов.
        intents.members = True
        intents.voice_states = True
        # Чтение чата нужно только болтовне Олега; без ключа Groq привилегированный интент не запрашиваем.
        intents.message_content = bool(
            config.GEMINI_API_KEY or config.GROQ_API_KEY or config.OPENROUTER_API_KEY or config.HF_TOKEN
            or config.TOKENHARBOR_API_KEY or config.PUTER_AUTH_TOKEN or config.ZAI_API_KEY
        )
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )

    async def login(self, token: str) -> None:
        # VPN поднимаем до входа в Discord: через него ходят и Discord, и нейросети.
        await VPN_CLIENT.start()
        if config.VPN_FOR_DISCORD and VPN_CLIENT.ready:
            self.http.proxy = config.VPN_PROXY_URL
            log.info("Discord подключается через VPN")
        await super().login(token)

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_tree_error

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Модуль загружен: %s", extension)

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            commands_synced = await self.tree.sync(guild=guild)
            log.info("Команд синхронизировано для сервера %s: %d", config.GUILD_ID, len(commands_synced))
            # Снимаем глобальные копии, иначе команды двоятся в списке у пользователей.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            commands_synced = await self.tree.sync()
            log.info(
                "Глобальных команд синхронизировано: %d (появятся у пользователей в течение часа)",
                len(commands_synced),
            )

    async def close(self) -> None:
        await champions.POOL.close()
        await portraits.close()
        await VPN_CLIENT.close()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        # Текстовых команд у бота нет — только слэш-команды. Без этого «@Олег привет»
        # разбиралось бы как команда и сыпало в логи ошибками «команда не найдена».
        return

    async def on_ready(self) -> None:
        log.info("Бот в сети: %s (серверов: %d)", self.user, len(self.guilds))
        await self.change_presence(activity=discord.Game("скримы LoL · /scrim"))

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await respond(interaction, f"⏳ Слишком часто. Повторите через {error.retry_after:.0f} с.")
            return
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            await respond(interaction, "⛔ У вас недостаточно прав для этой команды.")
            return
        if isinstance(error, app_commands.CommandInvokeError) and isinstance(
            error.original, discord.Forbidden
        ):
            await respond(interaction, "⛔ Боту не хватает прав на это действие. Проверьте настройки роли бота.")
            return

        log.exception("Ошибка при выполнении команды", exc_info=error)
        embed = discord.Embed(
            title="Не удалось выполнить команду",
            description="Произошла непредвиденная ошибка. Подробности записаны в логи бота.",
            color=WARNING,
        )
        await respond(interaction, embed=embed)


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO)

    if not config.TOKEN:
        log.critical(
            "Переменная окружения DISCORD_TOKEN не задана. "
            "Скопируйте .env.example в .env и укажите токен бота."
        )
        sys.exit(1)

    bot = ScrimBot()
    try:
        bot.run(config.TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.critical("Discord отклонил токен. Сбросьте его в Developer Portal и обновите .env.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "В Developer Portal → Bot включите привилегированные интенты Server Members Intent "
            "и Message Content Intent (второй нужен болтовне Олега, если задан GROQ_API_KEY)."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
