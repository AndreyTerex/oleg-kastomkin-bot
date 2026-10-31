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
from contextlib import contextmanager
from pathlib import Path
from copy import deepcopy

import psycopg
from psycopg.types.json import Jsonb


class SharedStateError(RuntimeError):
    pass


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
        except psycopg.Error:
            raise SharedStateError("Shared database connection failed; local fallback is disabled") from None

    def _connect(self):
        return psycopg.connect(
            self._dsn, autocommit=True, connect_timeout=10,
            options="-c statement_timeout=10000 -c lock_timeout=5000",
            keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )

    def _abort(self, message):
        self.failed = True
        self.connection.close()
        if self.on_failure:
            self.on_failure()
        raise SharedStateError(message) from None

    def _disconnected(self, error):
        return (self.connection.closed or self.connection.broken or
                isinstance(error, psycopg.InterfaceError) or
                isinstance(error, psycopg.OperationalError) and
                (error.sqlstate is None or error.sqlstate.startswith("08") or
                 error.sqlstate in ("57P01", "57P02", "57P03", "57P05")))

    def _read_state(self):
        rows = dict(self.connection.execute(
            "SELECT name, data FROM oleg_state WHERE namespace=%s", (self.namespace,)).fetchall())
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
        self.connection.close()
        self.connection = self._connect()
        if self.owned:
            acquired = self.connection.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()[0]
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
            except psycopg.Error:
                self._abort("Shared database operation failed; outcome may require readback")

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
            return self.owned

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
        with self.connection.transaction():
            self._write_entries(entries)
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

    def _write_entries(self, entries):
        for name, data in entries.items():
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
