"""Локальная копия общей базы в data/*.json — запасная «встроенная база» бота.

Пока бот работает с общей базой (DATABASE_URL), каждая подтверждённая запись дублируется в обычные
файлы data/*.json, а в data/.shared-mirror.json запоминаются отпечатки (sha256) того, что лежит в базе.
Если при запуске база недоступна (VPN не подключился, нет интернета до облака), бот не ждёт её
бесконечно, а работает на этих файлах как без DATABASE_URL и отмечает время ухода в автономный режим.

Когда база снова доступна, при запуске сверяет:
- базу за это время никто не менял (отпечатки совпадают с запомненными) — переносит в неё всё,
  что было сделано без неё;
- база менялась (работал другой экземпляр бота) — база главнее: локальные изменения не теряются,
  а откладываются в data/backups-offline/<время>, чтобы их можно было разобрать вручную.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

MARKER = ".shared-mirror.json"
BACKUPS = "backups-offline"


def fingerprint(data: dict | None) -> str:
    """Отпечаток одного файла; отсутствующий и пустой — одно и то же."""
    from shared_state import digest
    return digest(data or {})


def fingerprints(entries: dict, names) -> dict[str, str]:
    return {name: fingerprint(entries.get(name)) for name in sorted(names)}


def _write_json(path: Path, data) -> None:
    from storage import _write_json as write_atomic
    write_atomic(path, data)


def read_marker(directory) -> dict | None:
    try:
        data = json.loads((Path(directory) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(directory, namespace: str, names, entries: dict, known: dict[str, str]) -> dict[str, str]:
    """Записывает файлы entries (каждый атомарно), затем отметку с отпечатками всех файлов базы.
    known — отпечатки с прошлой записи (чтобы не пересчитывать неизменённые файлы)."""
    directory = Path(directory)
    digests = dict(known)
    for name, data in entries.items():
        _write_json(directory / name, data)
        digests[name] = fingerprint(data)
    if set(digests) != set(names):
        # Первая запись после запуска: досчитываем по тому, что сейчас в файлах (= в базе).
        for name in names:
            if name not in digests:
                digests[name] = fingerprint(_read_file(directory / name))
    _write_json(directory / MARKER, {
        "namespace": namespace, "files": digests, "synced_at": int(time.time()), "offline_since": None,
    })
    return digests


def mark_offline(directory, namespace: str, now: float | None = None) -> dict:
    """Бот начинает работать без базы. Время ухода и отпечатки базы сохраняются до возвращения на неё
    (в том числе после перезапусков). Нет копии базы — files=None: перенести изменения назад не выйдет."""
    marker = read_marker(directory) or {}
    if marker.get("namespace") != namespace:
        marker = {"namespace": namespace, "files": None}
    if not marker.get("offline_since"):
        marker["offline_since"] = int(now or time.time())
        _write_json(Path(directory) / MARKER, marker)
    return marker


def _read_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект: {path.name}")
    return data


def read_local(directory, names) -> dict[str, dict]:
    from storage import recover_transactions
    directory = Path(directory)
    recover_transactions(directory)
    return {name: _read_file(directory / name) for name in names}


def backup(directory, names) -> Path | None:
    """Копия локальных файлов перед тем, как их заменит база."""
    directory = Path(directory)
    present = [name for name in names if (directory / name).is_file()]
    if not present:
        return None
    target = directory / BACKUPS / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target.mkdir(parents=True, exist_ok=True)
    for name in present:
        shutil.copy2(directory / name, target / name)
    return target


def reconcile(directory, namespace: str, names, database: dict, upload) -> str | None:
    """Сверка при запуске с доступной базой. upload(entries) записывает в базу. Файлы после этого
    перезаписываются копией базы (это делает вызывающий), поэтому всё ценное сохраняется здесь."""
    marker = read_marker(directory)
    try:
        local = read_local(directory, names)
    except (OSError, ValueError):
        saved = backup(directory, names)
        return f"локальные файлы не прочитать — беру данные из базы (файлы сохранены в {saved})"
    base_db = fingerprints(database, names)
    base_local = fingerprints(local, names)
    if base_db == base_local:
        return None
    same = marker is not None and marker.get("namespace") == namespace
    if not same:
        # Первый запуск с этой базой: в data/ старые файлы, база главнее.
        saved = backup(directory, names)
        return f"локальные файлы заменены данными из базы (прежние сохранены в {saved})" if saved else None
    if not marker.get("offline_since"):
        # Копия просто отстала (например, бот упал между записью файлов и отметки) — база главнее.
        backup(directory, names)
        return None
    base = marker.get("files")
    since = datetime.fromtimestamp(marker["offline_since"]).strftime("%d.%m %H:%M")
    if isinstance(base, dict):
        expected = {name: base.get(name, fingerprint({})) for name in sorted(names)}
        if expected == base_db:
            changed = {name: local[name] for name in sorted(names) if base_local[name] != base_db[name]}
            upload(changed)
            return f"перенёс в базу изменения, сделанные без неё с {since}: {', '.join(changed)}"
    saved = backup(directory, names)
    return (f"база менялась, пока бот работал без неё (с {since}), — беру данные из базы; изменения, "
            f"сделанные без неё, в базу не перенесены и сохранены в {saved}")
