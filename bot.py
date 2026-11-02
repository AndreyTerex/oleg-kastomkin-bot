"""Точка входа: бот для организации скримов и каток по League of Legends."""
from __future__ import annotations

import logging
import asyncio
import os
import sys
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands

import champions
import config
import datasync
import local_mirror
import portraits
from sync import Sync
from storage import recover_transactions, configure_shared
from shared_state import IDLE_SESSION_TIMEOUT_MS, SharedStateDeadline
from vpn import VPN_CLIENT
from utils import WARNING, respond

log = logging.getLogger("scrimbot")

# Как часто резервный экземпляр проверяет, не освободилась ли общая база.
SHARED_RETRY_SECONDS = 15

EXTENSIONS = ("cogs.roles", "cogs.scrim", "cogs.stats", "cogs.lobby", "cogs.extras", "cogs.chat", "cogs.vpn_admin", "cogs.ops", "cogs.matchday", "cogs.sync", "cogs.profile", "cogs.quiz", "cogs.patchnotes")


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
        # Работа без общей базы (она недоступна): с какого момента. None — база подключена или не задана.
        self.offline_since: float | None = None
        # После закрытия запустить процесс заново (main): база вернулась или пропала посреди работы.
        self.restart_requested = False
        self.database_watch: asyncio.Task | None = None

    async def login(self, token: str) -> None:
        # VPN поднимаем до входа в Discord: через него ходят и Discord, и нейросети.
        await VPN_CLIENT.start()
        if config.VPN_FOR_DISCORD and VPN_CLIENT.ready:
            self.http.proxy = config.VPN_PROXY_URL
            log.info("Discord подключается через VPN")
        await super().login(token)

    async def acquire_shared(self, SharedState, SharedStateError, bounded_call):
        """Право записи в общую базу, или None — база недоступна, работаем на локальных файлах.

        Занято другим экземпляром — ждём в резерве (а не падаем: Docker иначе перезапускает бота по кругу),
        и стоит владельцу остановиться, резервный бот подхватит работу сам. А вот если базы просто не видно
        (VPN не подключился, нет связи с облаком) дольше DATABASE_WAIT_SECONDS или подключение зависло —
        бесконечно не ждём: бот запускается на своей локальной копии (local_mirror.py)."""
        waited = 0
        unreachable_since: float | None = None
        while True:
            try:
                tunnel = VPN_CLIENT.database_port()
                backend = await bounded_call(
                    lambda: SharedState(config.DATABASE_URL, config.SHARED_STATE_KEY, config.DATA_DIR,
                                        datasync.SYNC_FILES, tunnel_port=tunnel),
                    discard=lambda late: late.close(),
                )
            except SharedStateDeadline:
                # Зависший поток не прервать, а новые попытки копили бы их — дальше без базы.
                log.error("Подключение к общей базе зависло — работаю без неё")
                return None
            except SharedStateError as error:
                now = time.monotonic()
                unreachable_since = unreachable_since if unreachable_since is not None else now
                left = config.DATABASE_WAIT_SECONDS - (now - unreachable_since)
                if left <= 0:
                    log.warning("Общая база недоступна %d с (%s) — работаю без неё",
                                config.DATABASE_WAIT_SECONDS, error)
                    return None
                backend, message = None, (f"База недоступна ({error}). Пробую снова через {SHARED_RETRY_SECONDS} с; "
                                          f"не появится за {left:.0f} с — начну работать без неё.")
            else:
                unreachable_since = None
                try:
                    if await backend.arun("acquire"):
                        log.info("Общая база: подключение %s",
                                 "через VPN бота" if backend.via_tunnel else "напрямую")
                        if waited:
                            log.info("Общая база освободилась после %d с ожидания", waited)
                        return backend
                    holder = await backend.arun("lock_holder")
                except SharedStateDeadline:
                    await backend.aclose()
                    log.error("Общая база перестала отвечать посреди подключения — работаю без неё")
                    return None
                except SharedStateError as error:
                    holder = f"не удалось проверить ({error})"
                message = (
                    f"Общей базой владеет другой экземпляр бота: {holder}. Жду в резерве, проверяю раз в "
                    f"{SHARED_RETRY_SECONDS} с. Если второго бота нет — это оборванное подключение, "
                    f"база закроет его сама за {IDLE_SESSION_TIMEOUT_MS // 60000} мин."
                )
                # Новое подключение на каждую попытку: простаивающее сервер всё равно закроет.
                await backend.aclose()
            if waited == 0 or waited % 300 < SHARED_RETRY_SECONDS:
                log.warning("%s", message)
            await asyncio.sleep(SHARED_RETRY_SECONDS)
            waited += SHARED_RETRY_SECONDS

    async def connect_shared(self) -> bool:
        """Подключить общую базу: True — работаем с ней, False — она недоступна, бот на локальных файлах."""
        from shared_state import SharedState, SharedStateError
        from async_db import bounded_call, OPERATION_TIMEOUT
        backend = await self.acquire_shared(SharedState, SharedStateError, bounded_call)
        if backend is None:
            return False
        try:
            await backend.arun("require_initialized")
            # Перенос сделанного без базы и свежая локальная копия; может записывать в базу — даём запас.
            message = await bounded_call(backend.reconcile_offline, timeout=OPERATION_TIMEOUT * 3,
                                         invalidate=backend.invalidate)
        except SharedStateError as error:
            log.error("Общая база подключилась, но работать с ней не вышло (%s) — работаю без неё", error)
            await backend.aclose()
            return False
        except BaseException:
            await backend.aclose()
            raise
        if message:
            log.warning("Общая база: %s", message)
        loop = asyncio.get_running_loop()
        def failed():
            log.critical("Общая база пропала посреди работы — перезапускаюсь: вернусь на неё "
                         "или продолжу на локальной копии")
            self.restart_requested = True
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.close()))
        backend.on_failure = failed
        log.info("Общая база: экземпляр oleg:%s получил право записи", backend.instance_id)
        backend.runtime = True
        self.shared_state = backend
        configure_shared(backend)
        from shared_discord import install_discord_guard
        install_discord_guard(self, backend)
        return True

    async def go_offline(self) -> None:
        """База недоступна: работаем на локальной копии и ждём, когда база вернётся."""
        self.offline_since = time.time()
        try:
            marker = await asyncio.to_thread(local_mirror.mark_offline, config.DATA_DIR, config.SHARED_STATE_KEY)
            self.offline_since = marker.get("offline_since") or self.offline_since
            copy = "локальной копии базы" if marker.get("files") is not None else (
                "локальных файлах data/ (копии базы на этом компьютере ещё не было — данные могут быть старыми)")
        except OSError as error:
            copy = f"локальных файлах data/ (отметку записать не вышло: {type(error).__name__})"
        log.warning("Работаю без общей базы, на %s. Проверяю базу раз в %d мин; появится — перенесу "
                    "сделанное и перезапущусь на ней.", copy, config.DATABASE_RETRY_MINUTES)
        self.database_watch = asyncio.create_task(self.watch_database())

    async def watch_database(self) -> None:
        """Без базы: раз в DATABASE_RETRY_MINUTES проверяем её. Вернулась — перезапуск: при запуске бот
        перенесёт в базу сделанное без неё и продолжит работать с ней."""
        from shared_state import SharedStateError, database_ready
        from async_db import bounded_call
        pending: threading.Event | None = None
        while True:
            await asyncio.sleep(config.DATABASE_RETRY_MINUTES * 60)
            if pending is not None and not pending.is_set():
                continue  # прошлая проверка ещё висит в своём потоке — новую не добавляем
            done = pending = threading.Event()
            tunnel = VPN_CLIENT.database_port()
            def probe(done=done, tunnel=tunnel):
                try:
                    return database_ready(config.DATABASE_URL, config.SHARED_STATE_KEY, config.DATA_DIR,
                                          datasync.SYNC_FILES, tunnel)
                finally:
                    done.set()
            try:
                ready = await bounded_call(probe)
            except SharedStateError:
                ready = False
            except Exception:
                log.exception("Проверка общей базы не удалась")
                ready = False
            if ready:
                log.warning("Общая база снова доступна — перезапускаюсь, чтобы вернуться на неё")
                self.restart_requested = True
                await self.close()
                return

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_tree_error
        self.shared_state = None
        if config.DATABASE_URL and not await self.connect_shared():
            await self.go_offline()
        # До загрузки модулей: если бот уже работает на другом компьютере — ждём; свежие данные — из канала.
        if self.shared_state is None:
            recover_transactions(config.DATA_DIR)
        self.sync = Sync(self)
        await self.sync.prepare()

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
        sync = getattr(self, "sync", None)
        if sync is not None and not self.is_closed():
            try:
                await sync.push(force=True)
            except Exception:
                log.exception("Не удалось выложить снимок данных при выключении")
        if self.database_watch is not None and self.database_watch is not asyncio.current_task():
            self.database_watch.cancel()
        await champions.POOL.close()
        await portraits.close()
        await VPN_CLIENT.close()
        try:
            await super().close()
        finally:
            backend = getattr(self, "shared_state", None)
            if backend is not None:
                await backend.aclose()
                # Keep the failed backend installed: late tasks must never fall back to local files.

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


def restart() -> None:
    """Запустить бота заново в том же процессе (и в Docker, и без него): потоки, зависшие на сети,
    исчезают вместе со старым образом процесса."""
    log.warning("Перезапуск бота")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


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
        if bot.restart_requested:
            restart()
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
