"""Эксплуатация бота: /oleg-status, тревоги в админ-канал, ежедневные бэкапы data/ и сторожевой таймер.

- /oleg-status (только админы, ответ виден лишь вызвавшему): нейросети и их лимиты, VPN, аптайм,
  последние ошибки и последний бэкап.
- Тревоги в ALERT_CHANNEL_ID: ошибки в логах (не чаще раза в 10 минут), VPN не работает дольше 10 минут,
  все нейросети на лимите — и сообщение, когда всё починилось.
- Раз в сутки копия data/*.json в data/backups/ГГГГ-ММ-ДД, хранятся последние BACKUP_KEEP.
- Сторожевой таймер: если цикл событий завис на 3 минуты или Discord недоступен дольше
  WATCHDOG_DISCONNECT_MINUTES, процесс завершается, и Docker (restart: unless-stopped) поднимает его заново.
  Для Docker HEALTHCHECK бот раз в 20 секунд обновляет файл data/heartbeat.

Секретов здесь нет и быть не должно: из текста ошибок вырезаются IP-адреса, ссылки и uuid.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import datasync
from utils import NEUTRAL, WARNING
from vpn import VPN_CLIENT

log = logging.getLogger("scrimbot.ops")

STARTED_AT = time.time()
HEARTBEAT_FILE = "heartbeat"
LOOP_STALL_SECONDS = 180
ERROR_ALERT_EVERY = 600
VPN_DOWN_ALERT_AFTER = 600
LLM_DOWN_ALERT_AFTER = 300
# Примерные бесплатные суточные лимиты Gemini — только для подсказки в /oleg-status.
GEMINI_SCARCE_LIMIT = 20
GEMINI_LITE_LIMIT = 500

_IP = re.compile(r"\b(?!127\.0\.0\.1\b)\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_KEYISH = re.compile(r"\b(key|token|password|pass|secret|auth)=\S+", re.I)


def sanitize(text: str, limit: int = 220) -> str:
    """Текст ошибки для Discord: без адресов, ссылок, uuid и ключей, одной строкой."""
    text = _URL.sub("<ссылка>", text)
    text = _UUID.sub("<uuid>", text)
    text = _IP.sub("<адрес>", text)
    text = _KEYISH.sub(lambda m: f"{m.group(1)}=<скрыто>", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ErrorBuffer(logging.Handler):
    """Запоминает последние ошибки из логов — для /oleg-status и тревог."""

    def __init__(self, size: int = 20) -> None:
        super().__init__(level=logging.ERROR)
        self.recent: deque[tuple[float, str]] = deque(maxlen=size)
        self.unreported = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == log.name:
            return  # не тревожим о сбоях самих тревог
        try:
            text = record.getMessage().splitlines()[0] if record.getMessage() else ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text = f"{text} ({type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''})"
            self.recent.append((record.created, f"{record.name}: {sanitize(text)}"))
            self.unreported += 1
        except Exception:
            pass


class Outage:
    """Что-то сломано дольше порога → «down» один раз; починилось после тревоги → «up» один раз."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.since: float | None = None
        self.alerted = False

    def update(self, broken: bool, now: float) -> str | None:
        if broken:
            if self.since is None:
                self.since = now
            if not self.alerted and now - self.since >= self.threshold:
                self.alerted = True
                return "down"
            return None
        self.since = None
        if self.alerted:
            self.alerted = False
            return "up"
        return None


def backups_dir(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / "backups"


def make_backup(data_dir: str | os.PathLike[str], day: str, keep: int) -> Path | None:
    """Копирует data/*.json в data/backups/<day>; удаляет старые копии сверх keep. None — если копия уже есть."""
    target = backups_dir(data_dir) / day
    if target.exists():
        return None
    files = sorted(Path(data_dir).glob("*.json"))
    tmp = target.with_name(f".{day}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, tmp / path.name)
    tmp.replace(target)
    prune_backups(data_dir, keep)
    return target


def prune_backups(data_dir: str | os.PathLike[str], keep: int) -> None:
    days = sorted(p for p in backups_dir(data_dir).iterdir() if p.is_dir() and not p.name.startswith("."))
    for old in days[: max(0, len(days) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def last_backup(data_dir: str | os.PathLike[str]) -> tuple[str, float] | None:
    root = backups_dir(data_dir)
    if not root.exists():
        return None
    days = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not days:
        return None
    return days[-1].name, days[-1].stat().st_mtime


def usage_hint(provider, used: int) -> str:
    if provider.name != "gemini":
        return f"{used} сегодня" if used else ""
    limit = GEMINI_LITE_LIMIT if "lite" in provider.model else GEMINI_SCARCE_LIMIT
    return f"{used}/{limit} сегодня"


def uptime_text(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


class Ops(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.errors = ErrorBuffer()
        self.vpn_outage = Outage(VPN_DOWN_ALERT_AFTER)
        self.llm_outage = Outage(LLM_DOWN_ALERT_AFTER)
        self.last_error_alert = 0.0
        self.beat = time.monotonic()
        self.disconnected_since: float | None = time.monotonic()
        self._stop = threading.Event()

    async def cog_load(self) -> None:
        logging.getLogger().addHandler(self.errors)
        self.heartbeat.start()
        self.checks.start()
        if config.WATCHDOG_DISCONNECT_MINUTES:
            threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()

    async def cog_unload(self) -> None:
        logging.getLogger().removeHandler(self.errors)
        self.heartbeat.cancel()
        self.checks.cancel()
        self._stop.set()

    # --- сторожевой таймер ------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.disconnected_since = None

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        self.disconnected_since = None

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        if self.disconnected_since is None:
            self.disconnected_since = time.monotonic()

    @tasks.loop(seconds=20)
    async def heartbeat(self) -> None:
        self.beat = time.monotonic()
        try:
            (Path(config.DATA_DIR) / HEARTBEAT_FILE).touch()
        except OSError:
            pass

    def _watchdog(self) -> None:
        limit = config.WATCHDOG_DISCONNECT_MINUTES * 60
        while not self._stop.wait(15):
            now = time.monotonic()
            if now - self.beat > LOOP_STALL_SECONDS:
                reason = f"бот завис: цикл событий не отвечает {now - self.beat:.0f} с"
            elif self.disconnected_since is not None and now - self.disconnected_since > limit:
                reason = f"нет связи с Discord {(now - self.disconnected_since) / 60:.0f} мин"
            else:
                continue
            log.critical("Сторожевой таймер: %s — перезапускаюсь", reason)
            logging.shutdown()
            os._exit(1)

    # --- тревоги и бэкапы ---------------------------------------------------

    def chat_client(self):
        chat = self.bot.get_cog("Chat")
        return getattr(chat, "client", None)

    def vpn_broken(self) -> bool:
        status = VPN_CLIENT.status()
        return bool(status["enabled"]) and not (status["running"] and VPN_CLIENT.current is not None)

    @tasks.loop(seconds=60)
    async def checks(self) -> None:
        await self.backup_if_due()
        if not config.ALERT_CHANNEL_ID or not self.bot.is_ready():
            return
        now = time.time()
        messages: list[str] = []

        change = self.vpn_outage.update(self.vpn_broken(), now)
        if change == "down":
            messages.append("🛑 **VPN не работает больше 10 минут** — Gemini недоступен, Олег отвечает слабыми моделями.")
        elif change == "up":
            status = VPN_CLIENT.status()
            messages.append(f"✅ VPN снова работает: {status['server']} ({status['country'] or '—'}).")

        client = self.chat_client()
        if client is not None and client.enabled:
            change = self.llm_outage.update(client.all_blocked(), now)
            if change == "down":
                messages.append("🛑 **Все нейросети на лимите** — Олег молчит, пока не отпустит.")
            elif change == "up":
                messages.append("✅ Нейросети снова отвечают.")

        if self.errors.unreported and now - self.last_error_alert >= ERROR_ALERT_EVERY:
            count = self.errors.unreported
            latest = list(self.errors.recent)[-min(3, count):]
            lines = "\n".join(f"- <t:{int(at)}:T> `{text}`" for at, text in latest)
            messages.append(f"⚠️ Ошибок в логах: **{count}** (последние ниже, подробности — в `docker logs`)\n{lines}")
            self.errors.unreported = 0
            self.last_error_alert = now

        if messages:
            await self.alert("\n\n".join(messages))

    @checks.before_loop
    async def _before_checks(self) -> None:
        await self.bot.wait_until_ready()

    async def alert(self, text: str) -> None:
        channel = self.bot.get_channel(config.ALERT_CHANNEL_ID)
        if channel is None:
            log.warning("ALERT_CHANNEL_ID указывает на канал, которого бот не видит")
            return
        try:
            await channel.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Не удалось отправить тревогу: %s", exc)

    async def backup_if_due(self) -> None:
        day = datetime.now(config.TIMEZONE).strftime("%Y-%m-%d")
        try:
            made = await asyncio.to_thread(make_backup, config.DATA_DIR, day, config.BACKUP_KEEP)
        except OSError:
            log.exception("Не удалось сделать резервную копию data/")
            return
        if made is not None:
            log.info("Резервная копия data/ сохранена: backups/%s", day)

    # --- /oleg-status --------------------------------------------------------

    def status_embed(self) -> discord.Embed:
        latency = self.bot.latency * 1000 if self.bot.latency == self.bot.latency else None
        embed = discord.Embed(title="Состояние Олега", color=NEUTRAL)
        embed.add_field(name="Работает", value=f"{uptime_text(time.time() - STARTED_AT)} (с <t:{int(STARTED_AT)}:f>)")
        embed.add_field(name="Discord", value=f"{latency:.0f} мс" if latency is not None else "—")

        client = self.chat_client()
        if client is None or not client.enabled:
            models = "Ключей нейросетей нет — Олег молчит."
        else:
            lines = []
            for provider, state in client.provider_states():
                hint = usage_hint(provider, client.requests_today(provider.label))
                mark = "🟢" if state == "готова" else "🟡"
                lines.append(f"{mark} `{provider.label}` — {state}" + (f" · {hint}" if hint else ""))
            models = "\n".join(lines)
            if client.all_blocked():
                embed.color = WARNING
        embed.add_field(name="Нейросети (по порядку)", value=models[:1024], inline=False)

        status = VPN_CLIENT.status()
        if not status["enabled"]:
            vpn = "выключен"
        elif self.vpn_broken():
            vpn = "⚠️ рабочего сервера нет — нейросети идут напрямую"
            embed.color = WARNING
        else:
            ms = f", {status['latency_ms']:.0f} мс" if status["latency_ms"] is not None else ""
            vpn = f"{status['server']} ({status['country'] or '—'}{ms})"
        embed.add_field(name="VPN", value=vpn, inline=False)

        backup = last_backup(config.DATA_DIR)
        embed.add_field(
            name="Последний бэкап",
            value=f"{backup[0]} (<t:{int(backup[1])}:R>)" if backup else "ещё не было",
            inline=False,
        )
        recent = list(self.errors.recent)[-5:]
        errors = "\n".join(f"<t:{int(at)}:R> `{text}`" for at, text in reversed(recent)) or "нет 🎉"
        embed.add_field(name="Последние ошибки", value=errors[:1024], inline=False)
        if not config.ALERT_CHANNEL_ID:
            embed.set_footer(text="Тревоги выключены: задайте ALERT_CHANNEL_ID в .env")
        return embed

    @app_commands.command(name="oleg-status", description="Здоровье Олега: нейросети, лимиты, VPN, ошибки, бэкапы")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Команда только для администраторов сервера.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.status_embed(), ephemeral=True)

    # --- перенос данных между установками ---------------------------------------

    @app_commands.command(name="oleg-export", description="Выгрузить статистику, память и настройки Олега архивом")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def export_data(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Команда только для администраторов сервера.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload, files = await asyncio.to_thread(
            datasync.build_archive, config.DATA_DIR, [guild.id for guild in self.bot.guilds],
        )
        name = f"oleg-data-{datetime.now(config.TIMEZONE).strftime('%Y-%m-%d_%H-%M')}.zip"
        await interaction.followup.send(
            f"Данные Олега: {', '.join(files) or 'пусто'}.\n"
            "Чтобы перенести их в другого бота, там выполните `/oleg-import` и приложите этот файл. "
            "Кэша VPN и ключей в архиве нет.",
            file=discord.File(io.BytesIO(payload), filename=name),
            ephemeral=True,
        )
        log.info("Данные выгружены по просьбе %s (%s)", interaction.user, ", ".join(files))

    @app_commands.command(name="oleg-import", description="Загрузить данные из архива /oleg-export (заменит текущие)")
    @app_commands.describe(archive="ZIP-файл из /oleg-export")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def import_data(self, interaction: discord.Interaction, archive: discord.Attachment) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Команда только для администраторов сервера.", ephemeral=True)
            return
        if archive.size > datasync.MAX_ARCHIVE_BYTES:
            await interaction.response.send_message("Архив слишком большой.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            files, meta = datasync.read_archive(await archive.read())
        except datasync.ImportError_ as error:
            await interaction.followup.send(f"Не получилось: {error}", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("Не удалось скачать файл из Discord, попробуйте ещё раз.", ephemeral=True)
            return
        warning = ""
        if str(interaction.guild.id) not in (meta.get("guilds") or []):
            warning = (
                "\n⚠️ Архив выгружен с другого Discord-сервера: статистика привязана к серверу, "
                "поэтому здесь её видно не будет."
            )
        view = ImportConfirm(self, files, interaction.user.id)
        await interaction.followup.send(
            f"Архив {datasync.describe_meta(meta)}.{warning}\n"
            "Текущие данные будут **заменены** (копия сохранится в `data/backups-import`), "
            "а бот перезапустится. Продолжить?",
            view=view, ephemeral=True,
        )

    def apply_import(self, files: dict[str, bytes]) -> None:
        """Записывает данные и сразу перезапускает процесс: модули держат старые данные в памяти
        и иначе перезаписали бы новые. Docker (restart: unless-stopped) поднимает бота заново."""
        backup = datasync.apply_archive(config.DATA_DIR, files)
        log.warning("Данные заменены из архива (%s), копия прежних — %s. Перезапускаюсь", ", ".join(files), backup)
        logging.shutdown()
        os._exit(0)


class ImportConfirm(discord.ui.View):
    def __init__(self, cog: Ops, files: dict[str, bytes], user_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.files = files
        self.user_id = user_id

    @discord.ui.button(label="Заменить данные и перезапустить", emoji="📥", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Подтверждает тот, кто загрузил архив.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="📥 Загружаю данные и перезапускаюсь — через минуту Олег вернётся с новыми данными.", view=None,
        )
        self.stop()
        # Ответ должен уйти до выхода процесса.
        await asyncio.sleep(1)
        self.cog.apply_import(self.files)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Импорт отменён, данные не тронуты.", view=None)
        self.stop()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ops(bot))
