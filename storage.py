"""Простое JSON-хранилище: переживает перезапуск контейнера."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("scrimbot.storage")


class JsonStore:
    """Словарь, синхронизируемый с файлом на диске."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.data: dict = {}
        self._lock = asyncio.Lock()

    def load(self) -> dict:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except (json.JSONDecodeError, OSError):
            log.exception("Не удалось прочитать %s, файл будет создан заново", self.path)
            self.data = {}
        return self.data

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        # Пишем через временный файл, чтобы не потерять данные при падении контейнера.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)
