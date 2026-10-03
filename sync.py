"""Синхронизация данных между установками бота через закрытый канал Discord (SYNC_CHANNEL_ID).

Зачем: бота можно запускать на разных компьютерах/хостингах, а статистика, память Олежки и сборы
остаются общими. Хранилище — сам Discord, ничего дополнительно поднимать не нужно.

Как устроено:
- В канале лежит «замок» — одно сообщение бота «🔒 oleg-sync lock · instance=… · alive=…». Работающий
  бот обновляет его раз в минуту. Запустившийся второй экземпляр видит свежий чужой замок и ждёт
  в режиме ожидания, не подключаясь к Discord (иначе отвечали бы два Олега). Если замок не обновлялся
  дольше SYNC_LOCK_STALE — прежний бот выключен, новый забирает замок.
- Перед стартом бот берёт последний снимок данных из канала и, если он новее локальных файлов,
  подставляет его — так новый компьютер продолжает с того же места.
- Работающий бот раз в SYNC_MINUTES выкладывает снимок, если данные изменились, и при выключении;
  хранятся последние SNAPSHOTS_KEEP снимков.
В снимке — то же, что в /oleg-export: без кэша VPN и ключей.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import uuid
from pathlib import Path

import discord

import config
import datasync

log = logging.getLogger("scrimbot.sync")

LOCK_PREFIX = "🔒 oleg-sync lock"
SNAPSHOT_PREFIX = "oleg-sync-"
SNAPSHOTS_KEEP = 3
SCAN_LIMIT = 100
STANDBY_POLL = 60
CLAIM_SETTLE = 5
_LOCK = re.compile(r"instance=([0-9a-f]+).*?alive=(\d+)")


def instance_id(data_dir: str | os.PathLike[str]) -> str:
    """Постоянный id этой установки: после перезапуска (обновление образа) бот узнаёт свой замок."""
    path = Path(data_dir) / "instance_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{12}", value):
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex[:12]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


def lock_text(instance: str, now: float) -> str:
    return f"{LOCK_PREFIX} · instance={instance} · alive={int(now)} · <t:{int(now)}:R>"


def parse_lock(content: str) -> tuple[str, float] | None:
    if not content.startswith(LOCK_PREFIX):
        return None
    match = _LOCK.search(content)
    return (match.group(1), float(match.group(2))) if match else None


def snapshot_time(filename: str) -> float | None:
    match = re.fullmatch(rf"{SNAPSHOT_PREFIX}(\d+)\.zip", filename)
    return float(match.group(1)) if match else None


def local_updated(data_dir: str | os.PathLike[str]) -> float:
    """Когда локальные данные менялись последний раз (0 — данных нет)."""
    times = [
        (Path(data_dir) / name).stat().st_mtime for name in datasync.SYNC_FILES if (Path(data_dir) / name).is_file()
    ]
    return max(times, default=0.0)


def data_digest(data_dir: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    for name in datasync.SYNC_FILES:
        path = Path(data_dir) / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def lock_is_foreign(lock: tuple[str, float] | None, me: str, now: float, stale: float) -> bool:
    """Чужой и живой замок — значит, бот уже работает на другом компьютере."""
    return lock is not None and lock[0] != me and now - lock[1] < stale


class Sync:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.channel_id = config.SYNC_CHANNEL_ID
        self.me = instance_id(config.DATA_DIR) if self.channel_id else ""
        self.lock_message: discord.Message | None = None
        self.pushed_digest = ""
        self.lost = False

    @property
    def enabled(self) -> bool:
        return bool(self.channel_id)

    def channel(self):
        return self.bot.get_partial_messageable(self.channel_id)

    async def scan(self) -> tuple[discord.Message | None, list[discord.Message]]:
        """Последний замок и снимки бота в канале (новые первыми)."""
        lock_message, snapshots = None, []
        async for message in self.channel().history(limit=SCAN_LIMIT):
            if message.author.id != self.bot.user.id:
                continue
            if lock_message is None and parse_lock(message.content):
                lock_message = message
            if any(snapshot_time(a.filename) for a in message.attachments):
                snapshots.append(message)
        return lock_message, snapshots

    async def prepare(self) -> None:
        """До подключения к Discord: дождаться своей очереди, забрать замок, подтянуть свежий снимок."""
        if not self.enabled:
            return
        announced = False
        while True:
            try:
                lock_message, snapshots = await self.scan()
            except discord.HTTPException as error:
                log.error("Канал синхронизации недоступен (%s) — работаю без синхронизации", error)
                self.channel_id = 0
                return
            lock = parse_lock(lock_message.content) if lock_message else None
            if lock_is_foreign(lock, self.me, time.time(), config.SYNC_LOCK_STALE):
                if not announced:
                    log.warning(
                        "Олег уже запущен на другом компьютере (замок обновлён <%.0f с назад) — жду, "
                        "пока он выключится, в Discord не подключаюсь", time.time() - lock[1],
                    )
                    announced = True
                self._touch_heartbeat()
                await asyncio.sleep(STANDBY_POLL)
                continue
            await self.write_lock(lock_message)
            await asyncio.sleep(CLAIM_SETTLE)
            latest, snapshots = await self.scan()
            latest_lock = parse_lock(latest.content) if latest else None
            if latest_lock and latest_lock[0] != self.me:
                log.warning("Замок одновременно забрал другой компьютер — уступаю")
                continue
            self.lock_message = latest
            break
        await self.pull(snapshots)
        # Первый снимок уходит сразу после старта: в канале всегда то, с чем работает основной бот.
        self.pushed_digest = ""
        log.info("Синхронизация: этот компьютер — основной (instance %s)", self.me)

    async def pull(self, snapshots: list[discord.Message]) -> None:
        for message in snapshots:
            attachment = next(a for a in message.attachments if snapshot_time(a.filename))
            taken = snapshot_time(attachment.filename) or 0.0
            if taken <= local_updated(config.DATA_DIR):
                log.info("Синхронизация: локальные данные не старше снимка в канале — беру их")
                return
            try:
                files, _meta = datasync.read_archive(await attachment.read())
            except (datasync.ImportError_, discord.HTTPException) as error:
                log.warning("Снимок %s не подошёл (%s), пробую предыдущий", attachment.filename, error)
                continue
            backup = await asyncio.to_thread(datasync.apply_archive, config.DATA_DIR, files)
            log.info("Синхронизация: данные взяты из снимка %s (прежние — в %s)", attachment.filename, backup)
            return

    async def write_lock(self, existing: discord.Message | None) -> None:
        text = lock_text(self.me, time.time())
        if existing is not None:
            try:
                self.lock_message = await existing.edit(content=text)
                return
            except discord.HTTPException:
                pass
        self.lock_message = await self.channel().send(text)

    async def heartbeat(self) -> None:
        """Раз в минуту: обновить замок. Если его забрал другой компьютер — уйти, чтобы не было двух Олегов."""
        if not self.enabled or self.lost:
            return
        try:
            if self.lock_message is not None:
                current = await self.channel().fetch_message(self.lock_message.id)
                lock = parse_lock(current.content)
                if lock and lock[0] != self.me:
                    self.lost = True
                    log.critical("Замок синхронизации забрал другой компьютер — выключаюсь, чтобы не было двух Олегов")
                    await self.bot.close()
                    return
            await self.write_lock(self.lock_message)
        except discord.NotFound:
            self.lock_message = None
            await self.write_lock(None)
        except discord.HTTPException as error:
            log.warning("Не удалось обновить замок синхронизации: %s", error)

    async def push(self, force: bool = False) -> None:
        """Выложить снимок, если данные изменились (или force), и подчистить старые."""
        if not self.enabled or self.lost:
            return
        digest = await asyncio.to_thread(data_digest, config.DATA_DIR)
        if digest == self.pushed_digest and not force:
            return
        payload, files = await asyncio.to_thread(
            datasync.build_archive, config.DATA_DIR, [guild.id for guild in self.bot.guilds],
        )
        now = time.time()
        try:
            await self.channel().send(
                f"📦 Снимок данных Олега <t:{int(now)}:f> · {', '.join(files)}",
                file=discord.File(io.BytesIO(payload), filename=f"{SNAPSHOT_PREFIX}{int(now)}.zip"),
            )
        except discord.HTTPException as error:
            log.warning("Не удалось выложить снимок данных: %s", error)
            return
        self.pushed_digest = digest
        try:
            _lock, snapshots = await self.scan()
            for old in snapshots[SNAPSHOTS_KEEP:]:
                await old.delete()
        except discord.HTTPException:
            pass

    @staticmethod
    def _touch_heartbeat() -> None:
        try:
            (Path(config.DATA_DIR) / "heartbeat").touch()
        except OSError:
            pass
