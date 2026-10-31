"""JSON-хранилище с восстановлением незавершённых записей результата матча."""
from __future__ import annotations

import json
import asyncio
import os
import tempfile
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

_TRANSACTION: ContextVar[tuple | None] = ContextVar("json_transaction", default=None)
JOURNAL = ".pending-transaction.json"
_SHARED = None


def configure_shared(backend) -> None:
    global _SHARED
    _SHARED = backend


def shared_backend(directory=None):
    if _SHARED is not None and (directory is None or Path(directory).resolve() == _SHARED.directory):
        return _SHARED
    return None


def _check_preparation(backend):
    # ContextVars are copied into child tasks. The explicit task identity also
    # blocks a save from an already-ready task before the no-yield guard runs.
    preparing = getattr(backend, "_preparing_task", None)
    if preparing is not None and preparing is not asyncio.current_task():
        backend.invalidate()
        raise RuntimeError("Another task entered an unfinished shared transaction")


@contextmanager
def atomic(*stores):
    """Join an existing result transaction or create one for a standalone operation."""
    active = _TRANSACTION.get()
    if active is not None:
        if any(store not in active for store in stores):
            raise RuntimeError("Store is not part of the active transaction")
        yield
    else:
        with transaction(*stores):
            yield


@asynccontextmanager
async def async_atomic(*stores):
    if _SHARED is not None:
        _check_preparation(_SHARED)
    active = _TRANSACTION.get()
    if active is not None:
        if any(store not in active for store in stores):
            raise RuntimeError("Store is not part of the active transaction")
        yield
    else:
        async with async_transaction(*stores):
            yield


@asynccontextmanager
async def async_transaction(*stores):
    """Prepare without yielding, then persist immutable snapshots in FIFO order.

    Entry MUST NOT wait for the database lock: standalone callers mutate memory
    synchronously before save(), too. Both paths capture/enqueue their snapshots
    before yielding, so their durable order matches their in-memory order.
    Inside the body only in-memory work and non-yielding nested saves are allowed.
    """
    shared = [_SHARED is not None and _SHARED.handles(store.path) for store in stores]
    if not any(shared):
        with transaction(*stores):
            yield
        return
    if not all(shared) or _TRANSACTION.get() is not None:
        raise RuntimeError("Invalid mixed or nested shared transaction")
    backend = _SHARED
    _check_preparation(backend)
    backend._check_stopped()
    snapshots = [deepcopy(store.data) for store in stores]
    token = _TRANSACTION.set(stores)
    backend._preparing_task = asyncio.current_task()
    # Fail closed if a future caller introduces network I/O or another yielding
    # await into the preparation phase. A competing save must never persist a
    # partially prepared transaction. Cancel this guard before awaiting SQL.
    yielded = False
    def unexpected_yield():
        nonlocal yielded
        yielded = True
        backend.invalidate()
    no_yield = asyncio.get_running_loop().call_soon(unexpected_yield)
    try:
        yield
        if yielded:
            raise RuntimeError("Shared transaction body yielded; only in-memory work and nested saves are allowed")
        backend._check_stopped()
        entries = {store.path.name: deepcopy(store.data) for store in stores}
    except BaseException:
        for store, snapshot in zip(stores, snapshots):
            _restore(store.data, snapshot)
        raise
    finally:
        no_yield.cancel()
        backend._preparing_task = None
        _TRANSACTION.reset(token)
    try:
        await backend.arun("save", entries)
    except BaseException:
        # Later snapshots may include these mutations. Stop the entire queue;
        # never persist them after this commit failed or was cancelled.
        backend.invalidate()
        for store, snapshot in zip(stores, snapshots):
            _restore(store.data, snapshot)
        raise


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
    shared = [_SHARED is not None and _SHARED.handles(store.path) for store in stores]
    if any(shared) and not all(shared):
        raise RuntimeError("Cannot mix local and shared stores in one transaction")
    if not all(shared):
        recover_transactions(directory)
    snapshots = [deepcopy(store.data) for store in stores]
    token = _TRANSACTION.set(stores)
    prepared = False
    try:
        yield
        entries = {store.path.name: store.data for store in stores}
        if all(shared):
            _SHARED.save(entries)
            return
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
        if _SHARED is not None and _SHARED.handles(self.path):
            # Runtime startup already fetched one complete snapshot off the event loop.
            self.data = (deepcopy(_SHARED._saved.get(self.path.name, {})) if _SHARED.runtime else
                         _SHARED.load(self.path.name))
            return self.data
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
        if _SHARED is not None and _SHARED.handles(self.path):
            _check_preparation(_SHARED)
        active = _TRANSACTION.get()
        if active is not None:
            if self not in active:
                raise RuntimeError("Хранилище не включено в транзакцию")
            return
        if _SHARED is not None and _SHARED.handles(self.path):
            # Capture before yielding: a queued save must not read later mutations.
            try:
                await _SHARED.arun("save", {self.path.name: deepcopy(self.data)})
            except BaseException:
                _SHARED.invalidate()
                raise
            return
        # Небольшие локальные JSON пишутся без переключения задач: снимок не может
        # измениться во время записи, а транзакцию не обгонит фоновый поток save().
        if _SHARED is None or not _SHARED.handles(self.path):
            recover_transactions(self.path.parent)
        self._write()

    def _write(self) -> None:
        if _SHARED is not None and _SHARED.handles(self.path):
            _SHARED.save({self.path.name: self.data})
            return
        _write_json(self.path, self.data)
