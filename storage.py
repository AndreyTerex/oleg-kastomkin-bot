"""JSON-хранилище с восстановлением незавершённых записей результата матча."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

_TRANSACTION: ContextVar[tuple | None] = ContextVar("json_transaction", default=None)
JOURNAL = ".pending-transaction.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as tmp:
            name = tmp.name
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def recover_transactions(directory: str | Path) -> None:
    """Дописать все файлы из журнала до загрузки модулей или следующего сохранения."""
    directory = Path(directory)
    journal = directory / JOURNAL
    if not journal.exists():
        return
    entries = json.loads(journal.read_text(encoding="utf-8"))
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Повреждён журнал сохранения")
    for name, data in entries.items():
        if Path(name).name != name or not name.endswith(".json") or name == JOURNAL or not isinstance(data, dict):
            raise ValueError("Повреждён журнал сохранения")
    for name, data in entries.items():
        _write_json(directory / name, data)
    journal.unlink()


def _restore(target: dict, snapshot: dict) -> None:
    # Сохраняем ссылки на записи лобби, которыми пользуются открытые кнопки.
    for key in list(target):
        if key not in snapshot:
            del target[key]
    for key, value in snapshot.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _restore(target[key], value)
        elif isinstance(value, list) and isinstance(target.get(key), list):
            target[key][:] = deepcopy(value)
        else:
            target[key] = deepcopy(value)


@contextmanager
def transaction(*stores: "JsonStore"):
    """Одна операция над несколькими JSON-файлами.

    Внутри допустимы только изменения памяти и await store.save() (он не отдаёт
    управление). Discord и другие сетевые запросы выполняются снаружи.
    После подготовки журнала операция считается принятой: при сбое диска
    оставшиеся файлы допишутся при следующем сохранении или запуске.
    """
    if _TRANSACTION.get() is not None:
        raise RuntimeError("Вложенная транзакция")
    parents = {store.path.resolve().parent for store in stores}
    if len(parents) != 1:
        raise ValueError("Файлы транзакции должны лежать в одной папке")
    directory = parents.pop()
    recover_transactions(directory)
    snapshots = [deepcopy(store.data) for store in stores]
    token = _TRANSACTION.set(stores)
    prepared = False
    try:
        yield
        entries = {store.path.name: store.data for store in stores}
        _write_json(directory / JOURNAL, entries)
        prepared = True
        recover_transactions(directory)
    except BaseException:
        if not prepared:
            for store, snapshot in zip(stores, snapshots):
                _restore(store.data, snapshot)
        raise
    finally:
        _TRANSACTION.reset(token)


class JsonStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.data: dict = {}

    def load(self) -> dict:
        recover_transactions(self.path.parent)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        # Битый файл нельзя молча заменить пустым и затереть следующим save().
        if not isinstance(data, dict):
            raise ValueError(f"Ожидался JSON-объект: {self.path.name}")
        self.data = data
        return self.data

    async def save(self) -> None:
        active = _TRANSACTION.get()
        if active is not None:
            if self not in active:
                raise RuntimeError("Хранилище не включено в транзакцию")
            return
        # Небольшие локальные JSON пишутся без переключения задач: снимок не может
        # измениться во время записи, а транзакцию не обгонит фоновый поток save().
        recover_transactions(self.path.parent)
        self._write()

    def _write(self) -> None:
        _write_json(self.path, self.data)
