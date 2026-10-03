"""Перенос данных бота между установками: /oleg-export собирает архив, /oleg-import его загружает.

В архив идут только данные сервера (статистика, память Олежки, сборы, настройки). Кэш VPN (там адреса
серверов из подписки) и счётчики нейросетей не попадают никогда — их новый бот соберёт сам.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

# Что переносится. Всё остальное в data/ (vpn.json, llm_usage.json, heartbeat, backups) — нет.
SYNC_FILES = (
    "stats.json", "memory.json", "oleg.json", "lobbies.json", "champion_history.json", "recap.json", "invites.json",
)
META_FILE = "oleg-export.json"
FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
IMPORT_BACKUPS = "backups-import"


class ImportError_(ValueError):
    """Архив не подходит — текст ошибки можно показать пользователю."""


def build_archive(data_dir: str | os.PathLike[str], guild_ids: list[int]) -> tuple[bytes, list[str]]:
    """ZIP с данными и описанием. Возвращает байты архива и список вошедших файлов."""
    data = Path(data_dir)
    buffer = io.BytesIO()
    included = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in SYNC_FILES:
            path = data / name
            if path.is_file():
                archive.write(path, name)
                included.append(name)
        archive.writestr(META_FILE, json.dumps({
            "format": FORMAT_VERSION,
            "exported_at": int(time.time()),
            "guilds": [str(guild_id) for guild_id in guild_ids],
            "files": included,
        }, ensure_ascii=False, indent=2))
    return buffer.getvalue(), included


def read_archive(payload: bytes) -> tuple[dict[str, bytes], dict]:
    """Проверяет архив: только известные файлы, каждый — корректный JSON. Возвращает файлы и описание."""
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ImportError_("Архив слишком большой.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ImportError_("Это не ZIP-архив от /oleg-export.") from error
    with archive:
        names = set(archive.namelist())
        if META_FILE not in names:
            raise ImportError_("В архиве нет описания — он сделан не командой /oleg-export.")
        try:
            meta = json.loads(archive.read(META_FILE))
        except ValueError as error:
            raise ImportError_("Описание архива повреждено.") from error
        if not isinstance(meta, dict) or meta.get("format") != FORMAT_VERSION:
            raise ImportError_("Архив от другой версии бота.")
        files: dict[str, bytes] = {}
        for name in SYNC_FILES:
            if name not in names:
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_FILE_BYTES:
                raise ImportError_(f"Файл {name} в архиве подозрительно большой.")
            content = archive.read(name)
            try:
                if not isinstance(json.loads(content), dict):
                    raise ValueError
            except ValueError as error:
                raise ImportError_(f"Файл {name} в архиве повреждён.") from error
            files[name] = content
    if not files:
        raise ImportError_("В архиве нет данных.")
    return files, meta


def apply_archive(data_dir: str | os.PathLike[str], files: dict[str, bytes]) -> Path:
    """Сохраняет копию текущих данных в data/backups-import/<время> и заменяет файлы из архива.

    Файлы, которых нет в архиве, не трогаются. Пишется синхронно и атомарно (через временный файл).
    """
    data = Path(data_dir)
    backup = data / IMPORT_BACKUPS / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup.mkdir(parents=True, exist_ok=True)
    for name in SYNC_FILES:
        if (data / name).is_file():
            shutil.copy2(data / name, backup / name)
    for name, content in files.items():
        tmp = data / f".{name}.import"
        tmp.write_bytes(content)
        tmp.replace(data / name)
    return backup


def describe_meta(meta: dict) -> str:
    when = meta.get("exported_at")
    files = ", ".join(meta.get("files") or []) or "—"
    stamp = f"<t:{int(when)}:f>" if isinstance(when, (int, float)) else "неизвестно когда"
    return f"выгружен {stamp}, файлы: {files}"
