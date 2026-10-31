"""Optional PostgreSQL JSON state; one session owns the bot, no offline fallback.

Use a direct/session connection, never a transaction pooler. The same connection
holds the advisory lock and performs every write; a lost session cannot reconnect
and overwrite a newer owner's state. Reconnection rechecks ownership, revision
and the complete durable snapshot before retrying. Local-only caches stay local.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
import asyncio
from contextlib import contextmanager
from pathlib import Path
from copy import deepcopy

import psycopg
from psycopg.types.json import Jsonb


# Через столько миллисекунд простоя сервер закрывает сессию и отпускает замок владельца. Закрывает сервер,
# а не клиент: у пропавшего клиента (сон ноутбука, обрыв сети, убитый процесс) некому разорвать соединение.
# Живой бот после простоя просто переподключается при следующем событии.
IDLE_SESSION_TIMEOUT_MS = 3 * 60 * 1000
# Клиент пропал посреди транзакции — сервер откатывает её и закрывает сессию.
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30 * 1000


class SharedStateError(RuntimeError):
    pass


class SharedStateDeadline(SharedStateError):
    """Операция не уложилась в срок: её поток брошен (libpq/DNS не прервать) и может висеть дальше."""


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class SharedState:
    def __init__(self, dsn: str, namespace: str, directory: str | Path, names, on_failure=None):
        if not namespace or len(namespace) > 100:
            raise ValueError("SHARED_STATE_KEY must contain 1–100 characters")
        self.namespace = namespace
        self.directory = Path(directory).resolve()
        self.names = frozenset(names)
        self.on_failure = on_failure
        self.owned = False
        self.failed = False
        self._closed = False
        self._ready = False
        self._revision = None
        self._dsn = dsn
        self.instance_id = uuid.uuid4().hex[:12]
        self._stop = threading.Event()
        self._failure_notified = threading.Event()
        self.async_lock = asyncio.Lock()
        self.runtime = False
        self._mutex = threading.RLock()
        self._saved = {}
        self.key = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True)
        try:
            parameters = psycopg.conninfo.conninfo_to_dict(dsn)
            host = parameters.get("host", "")
            if "-pooler" in host:
                raise SharedStateError("Use Neon's direct connection, not its transaction pooler")
            if host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("/"):
                if parameters.get("sslmode") not in ("require", "verify-ca", "verify-full"):
                    raise SharedStateError("Remote PostgreSQL requires TLS: sslmode=require or verify-full")
            self.connection = self._connect()
        except psycopg.Error as error:
            raise SharedStateError(f"Shared database connection failed ({type(error).__name__}, "
                                   f"SQLSTATE {error.sqlstate or 'unavailable'}); local fallback is disabled") from None

    def _connect(self):
        self._check_stopped()
        connection = psycopg.connect(
            self._dsn, autocommit=True, connect_timeout=10,
            # idle_session_timeout: сессия, чей клиент пропал без закрытия соединения (сон ноутбука, обрыв
            # сети), иначе часами держит advisory-замок, и ни один бот не может запуститься. Живой владелец
            # после простоя просто переподключается и заново берёт замок (_reconnect, SQLSTATE 57P05).
            options=(f"-c statement_timeout=10000 -c lock_timeout=5000"
                     f" -c idle_session_timeout={IDLE_SESSION_TIMEOUT_MS}"
                     f" -c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}"),
            keepalives_idle=10, keepalives_interval=2, keepalives_count=3, tcp_user_timeout=15000,
            application_name=f"oleg:{self.instance_id}",
        )
        if self._stop.is_set():
            connection.close()
            self._check_stopped()
        return connection

    def _check_stopped(self):
        if self._stop.is_set():
            raise SharedStateError("Database worker invalidated; restart and reload required")

    def invalidate(self, notify=True):
        """Lock-free: the event loop must not wait behind a stuck libpq/RLock call."""
        self._stop.set()
        self.failed = True
        if notify and not self._failure_notified.is_set():
            self._failure_notified.set()
            if self.on_failure:
                self.on_failure()

    async def arun(self, method, *args):
        from async_db import bounded_call, OPERATION_TIMEOUT
        async def execute():
            self._check_stopped()
            return await bounded_call(lambda: getattr(self, method)(*args), timeout=OPERATION_TIMEOUT,
                                      invalidate=self.invalidate)
        try:
            async with self.async_lock:
                return await execute()
        except asyncio.CancelledError:
            # A caller may already have changed its dictionary before waiting for
            # the serialization lock. Do not let later saves persist cancelled work.
            self.invalidate()
            raise

    async def aclose(self):
        from async_db import bounded_call, CLOSE_TIMEOUT
        self.invalidate(notify=False)
        try:
            await bounded_call(self.close, timeout=CLOSE_TIMEOUT)
        except SharedStateError:
            pass  # process exit closes its sockets; never wait indefinitely for a worker

    def _abort(self, message):
        self.invalidate()
        self.connection.close()
        raise SharedStateError(message) from None

    def _disconnected(self, error):
        return (self.connection.closed or self.connection.broken or
                isinstance(error, psycopg.InterfaceError) or
                isinstance(error, psycopg.OperationalError) and
                (error.sqlstate is None or error.sqlstate.startswith("08") or
                 error.sqlstate in ("57P01", "57P02", "57P03", "57P05")))

    def _read_state(self):
        self._check_stopped()
        rows = dict(self.connection.execute(
            "SELECT name, data FROM oleg_state WHERE namespace=%s", (self.namespace,)).fetchall())
        self._check_stopped()
        schema = rows.get("_schema", {})
        if schema.get("version") != 2 or type(schema.get("revision")) is not int:
            self._abort("Shared namespace requires schema 2; initialize from a verified export")
        return {name: data for name, data in rows.items() if name in self.names}, schema

    def _reconnect(self, pending=None):
        """One on-demand reconnect. Never replay a mutation against a different snapshot.

        pending is (operation UUID, complete expected post-commit state). A matching
        receipt plus exact readback resolves a lost COMMIT acknowledgement without
        executing the mutation again. The same revision and old snapshot prove that
        it was not committed and that one retry is safe.
        """
        self._check_stopped()
        self.connection.close()
        self.connection = self._connect()
        if self.owned:
            self._check_stopped()
            acquired = self.connection.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()[0]
            self._check_stopped()
            if not acquired:
                self._abort("Another bot owns the database after reconnect; this instance is stopping")
            current, schema = self._read_state()
            if self._ready:
                if pending is not None:
                    operation, expected = pending
                    if (schema["revision"] == self._revision + 1 and
                            schema.get("operation") == operation and current == expected):
                        self._saved = deepcopy(current)
                        self._revision = schema["revision"]
                        return True  # committed, acknowledgement was lost
                if schema["revision"] != self._revision or current != self._saved:
                    self._abort("Shared state changed on another host; stale action rejected, restart and reload required")
            self._saved = deepcopy(current)
            self._revision = schema["revision"]
            self._ready = True
        return False

    def _read(self, operation):
        with self._access():
            try:
                return operation()
            except psycopg.Error as error:
                if not self._disconnected(error):
                    raise
                self._reconnect()
                return operation()

    @contextmanager
    def _access(self):
        with self._mutex:
            if self.failed or self._closed:
                raise SharedStateError("Shared database session is unavailable; restart and reload required")
            try:
                yield self.connection
            except psycopg.Error as error:
                self._abort(f"Shared database operation failed ({type(error).__name__}, "
                            f"SQLSTATE {error.sqlstate or 'unavailable'}); outcome may require readback")
            except SharedStateError:
                if self._stop.is_set():
                    self.connection.close()
                raise

    def ensure_schema(self):
        """Explicit administrative initialization, not an automatic production migration."""
        with self._access() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS oleg_state (
                namespace text NOT NULL, name text NOT NULL,
                data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
                PRIMARY KEY (namespace, name))""")

    def acquire(self) -> bool:
        with self._access() as conn:
            if not self.owned:
                self.owned = conn.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()[0]
                self._check_stopped()
            return self.owned

    def lock_holder(self) -> str:
        """Кто держит замок (для лога ожидающего экземпляра): имя приложения и сколько сессия простаивает.
        Адреса не выводим."""
        key = self.key & 0xFFFFFFFFFFFFFFFF
        with self._access() as conn:
            row = conn.execute("""SELECT a.application_name, a.state,
                    EXTRACT(EPOCH FROM now() - a.backend_start)::int, EXTRACT(EPOCH FROM now() - a.state_change)::int
                FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.locktype = 'advisory' AND l.granted AND l.classid = %s AND l.objid = %s AND l.objsubid = 1""",
                (key >> 32, key & 0xFFFFFFFF)).fetchone()
        if row is None:
            return "никто (замок только что освободился)"
        name, state, age, idle = row
        if name is None:
            return "сессия другого пользователя базы"
        return f"{name}, подключён {age} с назад, состояние {state}, без активности {idle} с"

    def require_initialized(self):
        self.snapshot()

    def handles(self, path: Path) -> bool:
        return path.resolve().parent == self.directory and path.name in self.names

    def snapshot(self) -> dict:
        with self._mutex:
            current, schema = self._read(self._read_state)
            if self.owned and self._ready and (schema["revision"] != self._revision or current != self._saved):
                self._abort("Shared state changed outside the writer; restart and reload required")
            self._saved = deepcopy(current)
            self._revision = schema["revision"]
            self._ready = True
            return current

    def load(self, name: str) -> dict:
        return self.snapshot().get(name, {})

    def check_owner(self):
        """On actual work only, never a timer: fence Discord effects as well as writes."""
        with self._mutex:
            if not self.owned:
                raise SharedStateError("This process does not own the shared-state lock")
            if not self._ready:
                self.require_initialized()
            row = self._read(lambda: self.connection.execute(
                "SELECT data->>'version', data->>'revision' FROM oleg_state WHERE namespace=%s AND name='_schema'",
                (self.namespace,)).fetchone())
            if row != ("2", str(self._revision)):
                self._abort("Shared revision changed; this instance must reload before doing work")

    def claim_event(self, event_key: str) -> bool:
        """Durable admission before Discord callbacks; duplicate gateway delivery is skipped."""
        with self._mutex:
            self.check_owner()
            row = self._read(lambda: self.connection.execute(
                "SELECT (data->'receipts') ? %s FROM oleg_state WHERE namespace=%s AND name='_schema'",
                (event_key, self.namespace)).fetchone())
            if row and row[0]:
                return False
            self._save({}, event_key=event_key)
            return True

    def save(self, entries: dict):
        with self._mutex:
            self._save(entries)

    def _save(self, entries: dict, event_key=None):
        if not self.owned:
            raise SharedStateError("This process does not own the shared-state lock")
        if self.failed or self._closed:
            raise SharedStateError("Shared database session is unavailable; restart and reload required")
        if not set(entries) <= self.names or not all(isinstance(data, dict) for data in entries.values()):
            raise ValueError("Invalid shared-state entries")
        if not self._ready:
            self.require_initialized()
        entries = deepcopy({name: data for name, data in entries.items() if data != self._saved.get(name)})
        if not entries and event_key is None:
            return
        expected = deepcopy(self._saved)
        expected.update(entries)
        operation = uuid.uuid4().hex
        with self._access():
            for attempt in range(2):
                try:
                    if event_key is None:
                        self._commit(entries, operation)
                    else:
                        self._commit(entries, operation, event_key)
                except psycopg.Error as error:
                    if not self._disconnected(error):
                        raise
                    if self._reconnect((operation, expected)):
                        return  # exact intended state already committed, no second mutation
                    if attempt:
                        self._abort("Database disconnected twice; operation was not committed, retry after restart")
                except SharedStateError:
                    self._abort("Shared revision changed; stale transaction rejected, restart and reload required")
                else:
                    self._saved = expected
                    self._revision += 1
                    return

    def _commit(self, entries, operation, event_key=None):
        self._check_stopped()
        with self.connection.transaction():
            self._write_entries(entries)
            self._check_stopped()
            row = self.connection.execute("""UPDATE oleg_state SET data = data ||
                jsonb_build_object('version', 2, 'revision', %s, 'operation', %s::text) ||
                CASE WHEN %s::text IS NULL THEN '{}'::jsonb ELSE
                  jsonb_build_object('receipts', COALESCE(data->'receipts', '{}'::jsonb) ||
                    jsonb_build_object(%s::text, %s::text)) END
                WHERE namespace=%s AND name='_schema' AND (data->>'revision')::bigint=%s
                RETURNING name""", (self._revision + 1, operation, event_key, event_key, operation,
                                     self.namespace, self._revision)).fetchone()
            if row is None:
                raise SharedStateError("Shared revision changed; stale transaction rejected")
            self._check_stopped()

    def _write_entries(self, entries):
        for name, data in entries.items():
            self._check_stopped()
            self.connection.execute("""INSERT INTO oleg_state (namespace, name, data) VALUES (%s, %s, %s)
                ON CONFLICT (namespace, name) DO UPDATE SET data=EXCLUDED.data""",
                         (self.namespace, name, Jsonb(data)))

    def initialize(self, entries: dict):
        if not self.owned:
            raise SharedStateError("Stop the active bot before initialization")
        if not set(entries) <= self.names or not all(isinstance(data, dict) for data in entries.values()):
            raise ValueError("Invalid shared-state entries")
        # Administrative initialization is deliberately not auto-retried.
        with self._access() as conn, conn.transaction():
            if conn.execute("SELECT 1 FROM oleg_state WHERE namespace=%s LIMIT 1", (self.namespace,)).fetchone():
                raise SharedStateError("Namespace already contains data; initialization refuses to overwrite it")
            self._write_entries(entries)
            conn.execute("INSERT INTO oleg_state VALUES (%s, '_schema', %s)",
                         (self.namespace, Jsonb({"version": 2, "revision": 0, "operation": None})))
        self._saved = deepcopy(entries)
        self._revision = 0
        self._ready = True

    def close(self):
        with self._mutex:
            self.connection.close()
            self.owned = False
            self._closed = True
