"""Безотказность: VPN не подключился — напрямую; общая база недоступна — локальная копия, а не вечное ожидание."""
import asyncio
import json
import os
import socket
import uuid

import pytest

import config
import datasync
import local_mirror
import shared_state
import vpn
from stats import Stats
from storage import configure_shared

NAMES = datasync.SYNC_FILES


# --- сверка локальной копии с базой (без базы) ------------------------------------------


def _mirror(directory, database):
    local_mirror.write(directory, "ns", NAMES, {name: database.get(name, {}) for name in NAMES}, {})


def test_offline_changes_are_uploaded_when_database_was_untouched(tmp_path):
    database = {"stats.json": {"1": {"games": [1]}}, "wallet.json": {"7": 100}}
    _mirror(tmp_path, database)
    local_mirror.mark_offline(tmp_path, "ns")
    (tmp_path / "stats.json").write_text(json.dumps({"1": {"games": [1, 2]}}))
    uploaded = []
    message = local_mirror.reconcile(tmp_path, "ns", NAMES, database, uploaded.append)
    assert uploaded == [{"stats.json": {"1": {"games": [1, 2]}}}]
    assert "перенёс в базу" in message and "stats.json" in message


def test_database_changed_meanwhile_wins_and_offline_changes_are_kept_aside(tmp_path):
    _mirror(tmp_path, {"stats.json": {"1": {"games": [1]}}})
    local_mirror.mark_offline(tmp_path, "ns")
    (tmp_path / "stats.json").write_text(json.dumps({"1": {"games": [1, "offline"]}}))
    uploaded = []
    other_host = {"stats.json": {"1": {"games": [1, "other"]}}}
    message = local_mirror.reconcile(tmp_path, "ns", NAMES, other_host, uploaded.append)
    assert uploaded == [] and "база менялась" in message
    [kept] = list((tmp_path / local_mirror.BACKUPS).iterdir())
    assert json.loads((kept / "stats.json").read_text()) == {"1": {"games": [1, "offline"]}}


def test_nothing_done_offline_means_nothing_to_upload(tmp_path):
    database = {"stats.json": {"1": {}}}
    _mirror(tmp_path, database)
    local_mirror.mark_offline(tmp_path, "ns")
    uploaded = []
    assert local_mirror.reconcile(tmp_path, "ns", NAMES, database, uploaded.append) is None
    assert uploaded == []


def test_old_local_files_without_a_mirror_never_overwrite_the_database(tmp_path):
    (tmp_path / "stats.json").write_text('{"stale": {}}')
    local_mirror.mark_offline(tmp_path, "ns")  # копии базы ещё не было
    uploaded = []
    message = local_mirror.reconcile(tmp_path, "ns", NAMES, {"stats.json": {"fresh": {}}}, uploaded.append)
    assert uploaded == [] and "база менялась" in message
    assert list((tmp_path / local_mirror.BACKUPS).iterdir())


def test_offline_mark_survives_restarts(tmp_path):
    _mirror(tmp_path, {})
    first = local_mirror.mark_offline(tmp_path, "ns", now=100)
    again = local_mirror.mark_offline(tmp_path, "ns", now=200)
    assert first["offline_since"] == again["offline_since"] == 100
    assert again["files"] is not None


def test_offline_history_is_not_trimmed(tmp_path, monkeypatch):
    import stats as stats_module
    monkeypatch.setattr(stats_module, "HISTORY_LIMIT", 2)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u@db.example.com/x?sslmode=require")
    store = Stats(tmp_path / "stats.json")
    store.load()
    for _ in range(4):
        asyncio.run(store.record_game(1, [10], [20]))
    # История уйдёт обратно в базу, где она полная, — без базы её не обрезаем.
    assert len(store.store.data["1"]["games"]) == 4


# --- VPN ------------------------------------------------------------------------------


def test_dead_saved_server_switches_to_direct_at_start(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn.config, "VPN_URL", "trojan://p@s.example.com:443?sni=s.example.com#S")
    monkeypatch.setattr(vpn.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(vpn.shutil, "which", lambda name: "/usr/bin/xray")
    client = vpn.VPN()
    server = vpn.parse_subscription("trojan://p@s.example.com:443?sni=s.example.com#S")[0][0]
    client.state.load()
    client.state.select(vpn.ProbeResult(server.fingerprint, "S", True, 80.0, "DE"))
    asyncio.run(client.state.save())
    calls, delays = [], []

    async def refresh(force=False):
        client.servers = {server.fingerprint: server}

    async def activate(result):
        client.current = result
        calls.append("server")
        return True

    async def dead():
        return False

    async def direct(quiet=False, force=False):
        calls.append(("direct", force))
        client.current = None

    async def loop(first_delay):
        delays.append(first_delay)

    monkeypatch.setattr(client, "refresh_servers", refresh)
    monkeypatch.setattr(client, "activate", activate)
    monkeypatch.setattr(client, "_current_alive", dead)
    monkeypatch.setattr(client, "activate_direct", direct)
    monkeypatch.setattr(client, "_loop", loop)

    async def run():
        await client.start()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert calls[:2] == ["server", ("direct", True)]
    assert client.current is None
    assert delays == [0]  # поиск рабочего сервера — сразу, а не через полторы минуты


def test_direct_mode_backs_off_instead_of_endless_full_scans(monkeypatch):
    monkeypatch.setattr(vpn.config, "VPN_FULL_CHECK_HOURS", 3.0)
    client = vpn.VPN()
    hours = []
    for failures in range(6):
        client.direct_failures = failures
        hours.append(client.full_check_hours())
    assert hours == [0.5, 0.5, 1.0, 2.0, 3.0, 3.0]
    client.current = vpn.ProbeResult("fp", "Germany", True, 80.0, "DE")
    assert client.full_check_hours() == 3.0


def test_failed_full_check_counts_towards_backoff(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn.config, "VPN_CHECK_GEMINI", False)
    client = vpn.VPN()
    client.state = vpn.VpnState(tmp_path / "vpn.json")
    client.state.load()
    client.started = True

    async def nothing(*args, **kwargs):
        return {}

    async def direct(quiet=False, force=False):
        client.current = None

    monkeypatch.setattr(client, "refresh_servers", nothing)
    monkeypatch.setattr(client, "probe", nothing)
    monkeypatch.setattr(client, "activate_direct", direct)
    monkeypatch.setattr(type(client), "ready", property(lambda self: True))
    asyncio.run(client.recheck(full=True))
    asyncio.run(client.recheck(full=True))
    assert client.direct_failures == 2


# --- общая база (нужен TEST_POSTGRES_DSN) ----------------------------------------------


@pytest.fixture
def dsn():
    value = os.environ.get("TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("Set TEST_POSTGRES_DSN to a disposable PostgreSQL database")
    return value


@pytest.fixture
def namespace(dsn, tmp_path):
    name = "offline-" + uuid.uuid4().hex
    owner = shared_state.SharedState(dsn, name, tmp_path, NAMES)
    owner.ensure_schema()
    assert owner.acquire()
    owner.initialize({"stats.json": {}, "wallet.json": {}})
    owner.close()
    yield name
    configure_shared(None)
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as cleanup:
        cleanup.execute("DELETE FROM oleg_state WHERE namespace=%s", (name,))


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _owner(dsn, name, directory, **kwargs):
    backend = shared_state.SharedState(dsn, name, directory, NAMES, **kwargs)
    assert backend.acquire()
    backend.require_initialized()
    return backend


def test_direct_fallback_keeps_certificate_verification():
    state = shared_state.SharedState.__new__(shared_state.SharedState)
    state._dsn = "postgresql://u:p@db.example.com/x?sslmode=require"
    state._tunnel_port = 15432
    routes = state._routes()
    assert [("hostaddr" in route) for route in routes] == [True, False, True]
    # Сломанный туннель не понижает защиту до sslmode=require из DSN: и напрямую — только verify-full.
    assert all(route["sslmode"] == "verify-full" and route["sslrootcert"] for route in routes)
    state._tunnel_port = None
    assert state._routes() == [{}] * shared_state.CONNECT_ATTEMPTS  # VPN для базы выключен — как в DSN


def test_dead_vpn_tunnel_falls_back_to_direct_connection(dsn, namespace, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(shared_state, "CONNECT_RETRY_DELAY", 0)
    # Тестовая база без TLS: проверку сертификата на прямом пути проверяет тест выше.
    monkeypatch.setattr(shared_state.SharedState, "_fallback_params", lambda self: {})
    backend = shared_state.SharedState(dsn, namespace, tmp_path, NAMES, tunnel_port=_closed_port())
    try:
        assert not backend.via_tunnel and backend.acquire()
    finally:
        backend.close()
    assert "подключился напрямую" in caplog.text


def test_offline_round_trip_returns_changes_to_database(dsn, namespace, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    owner = _owner(dsn, namespace, tmp_path)
    assert owner.reconcile_offline() is None
    configure_shared(owner)
    stats = Stats(tmp_path / "stats.json")
    stats.load()
    asyncio.run(stats.record_game(1, [10], [20], event_key="online"))
    # Каждая запись в базу — и в локальную копию.
    assert len(json.loads((tmp_path / "stats.json").read_text())["1"]["games"]) == 1
    owner.close()
    configure_shared(None)

    local_mirror.mark_offline(tmp_path, namespace)  # при запуске база не ответила
    offline = Stats(tmp_path / "stats.json")
    offline.load()
    asyncio.run(offline.record_game(1, [20], [10], event_key="offline"))

    back = _owner(dsn, namespace, tmp_path)
    try:
        assert "перенёс в базу" in back.reconcile_offline()
        games = back.snapshot()["stats.json"]["1"]["games"]
        assert [game["event_key"] for game in games] == ["online", "offline"]
        assert local_mirror.read_marker(tmp_path)["offline_since"] is None
    finally:
        back.close()


def test_offline_changes_do_not_overwrite_newer_database(dsn, namespace, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    owner = _owner(dsn, namespace, tmp_path)
    owner.reconcile_offline()
    owner.close()
    local_mirror.mark_offline(tmp_path, namespace)
    offline = Stats(tmp_path / "stats.json")
    offline.load()
    asyncio.run(offline.record_game(1, [20], [10], event_key="offline"))
    # Тем временем с базой поработал другой компьютер.
    other = _owner(dsn, namespace, tmp_path / "other-host")
    other.save({"wallet.json": {"7": 500}})
    other.close()

    back = _owner(dsn, namespace, tmp_path)
    try:
        assert "база менялась" in back.reconcile_offline()
        assert back.snapshot().get("stats.json") == {}
        assert json.loads((tmp_path / "wallet.json").read_text()) == {"7": 500}
    finally:
        back.close()
    [kept] = list((tmp_path / local_mirror.BACKUPS).iterdir())
    assert json.loads((kept / "stats.json").read_text())["1"]["games"][0]["event_key"] == "offline"


def test_database_ready_probe(dsn, namespace, tmp_path, monkeypatch):
    monkeypatch.setattr(shared_state, "CONNECT_RETRY_DELAY", 0)
    assert shared_state.database_ready(dsn, namespace, tmp_path, NAMES)
    owner = _owner(dsn, namespace, tmp_path)
    try:
        # Базу держит другой экземпляр — тоже «вернуться»: этот уйдёт в резерв, работать будет тот.
        assert shared_state.database_ready(dsn, namespace, tmp_path, NAMES)
    finally:
        owner.close()
    assert not shared_state.database_ready("postgresql://postgres@127.0.0.1:1/postgres", namespace, tmp_path, NAMES)
    assert not shared_state.database_ready(dsn, "missing-" + uuid.uuid4().hex, tmp_path, NAMES)


def test_bot_starts_on_local_files_when_database_is_unreachable(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    import bot as bot_module
    from bot import ScrimBot, EXTENSIONS
    configure_shared(None)
    monkeypatch.setattr(shared_state, "CONNECT_RETRY_DELAY", 0)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://postgres@127.0.0.1:1/postgres")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "DATABASE_RETRY_MINUTES", 60)
    monkeypatch.setattr(config, "GUILD_ID", 0)
    monkeypatch.setattr(config, "WATCHDOG_DISCONNECT_MINUTES", 0)
    monkeypatch.setattr(bot_module, "SHARED_RETRY_SECONDS", 0.01)
    # Этот компьютер — последний владелец базы (есть её копия).
    local_mirror.write(tmp_path, config.SHARED_STATE_KEY, NAMES, {}, {})
    bot = ScrimBot()
    bot.tree.sync = AsyncMock(return_value=[])

    async def run():
        async with bot:
            await bot.setup_hook()
            assert set(bot.extensions) == set(EXTENSIONS)
            assert bot.shared_state is None and bot.offline_since
            assert not bot.database_watch.done()
        assert bot.database_watch.cancelled() or bot.database_watch.done()

    asyncio.run(run())
    assert local_mirror.read_marker(tmp_path)["offline_since"]


def test_bot_restarts_when_database_comes_back(tmp_path, monkeypatch):
    import bot as bot_module
    from bot import ScrimBot
    monkeypatch.setattr(config, "DATABASE_RETRY_MINUTES", 0.0001)
    checks = []

    def ready(*args):
        checks.append(args)
        return len(checks) >= 2

    monkeypatch.setattr(shared_state, "database_ready", ready)
    bot = ScrimBot()
    closed = []

    async def close():
        closed.append(True)

    bot.close = close
    asyncio.run(asyncio.wait_for(bot.watch_database(), 5))
    assert len(checks) == 2 and closed and bot.restart_requested
    assert bot_module.restart  # main() перезапускает процесс через os.execv


# --- быстрые ответы Discord и свои оборванные сессии -------------------------------------


def test_claim_is_one_round_trip_and_old_receipts_expire(dsn, namespace, tmp_path, monkeypatch):
    owner = _owner(dsn, namespace, tmp_path)
    try:
        statements = []
        original = owner.connection.execute
        monkeypatch.setattr(owner.connection, "execute", lambda *a, **k: statements.append(a[0]) or original(*a, **k))
        assert owner.claim_event("INTERACTION_CREATE:1")
        assert len(statements) == 1  # владение, квитанция и ревизия — одним запросом
        assert not owner.claim_event("INTERACTION_CREATE:1")
        monkeypatch.undo()
        # Квитанции не копятся вечно: старше RECEIPT_TTL выбрасываются при следующей записи.
        owner.connection.execute("""UPDATE oleg_state SET data = jsonb_set(data, '{receipts,old}', '1')
            WHERE namespace=%s AND name='_schema'""", (namespace,))
        assert owner.claim_event("INTERACTION_CREATE:2")
        receipts = owner.connection.execute(
            "SELECT data->'receipts' FROM oleg_state WHERE namespace=%s AND name='_schema'", (namespace,)).fetchone()[0]
        assert set(receipts) == {"INTERACTION_CREATE:1", "INTERACTION_CREATE:2"}
        # И не читаются вместе с данными.
        assert "receipts" not in owner._read_state()[1]
    finally:
        owner.close()


def test_restart_reclaims_own_dead_session_instead_of_waiting(dsn, namespace, tmp_path, monkeypatch):
    monkeypatch.setattr(shared_state, "CONNECT_RETRY_DELAY", 0)
    me = shared_state.local_instance_id(tmp_path)
    assert shared_state.local_instance_id(tmp_path) == me  # тот же ID после перезапуска
    zombie = shared_state.SharedState(dsn, namespace, tmp_path, NAMES, instance_id=me)
    assert zombie.acquire()  # процесс «упал», а сессия через VPN-сервер ещё держит замок
    try:
        # Чужой бот замок не отнимает.
        assert not shared_state.SharedState(dsn, namespace, tmp_path, NAMES).acquire()
        restarted = shared_state.SharedState(dsn, namespace, tmp_path, NAMES, instance_id=me)
        assert restarted.acquire()  # свой прошлый запуск — забираем сразу, без 3 минут ожидания
        restarted.close()
    finally:
        zombie.connection.close()
    # На другой машине (скопированная папка data/) ID другой.
    monkeypatch.setattr(shared_state.socket, "gethostname", lambda: "friend-pc")
    assert shared_state.local_instance_id(tmp_path) != me


def test_interaction_callback_skips_extra_owner_check():
    from types import SimpleNamespace as NS
    from shared_discord import OwnedWebhookAdapter, needs_owner_check
    callback = NS(method="POST", path="/interactions/{webhook_id}/{webhook_token}/callback")
    assert not needs_owner_check(callback)  # событие уже принято через claim_event, у Discord лимит 3 с
    assert needs_owner_check(NS(method="POST", path="/webhooks/{webhook_id}/{webhook_token}"))
    assert not needs_owner_check(NS(method="GET", path="/webhooks/{webhook_id}/{webhook_token}"))
    assert OwnedWebhookAdapter


def test_crash_restart_backs_off_and_resets_after_long_uptime(monkeypatch):
    import bot as bot_module
    monkeypatch.delenv(bot_module.RESTART_DELAY_ENV, raising=False)
    delays = []
    for _ in range(8):
        delay = bot_module.restart_delay(uptime=5)
        monkeypatch.setenv(bot_module.RESTART_DELAY_ENV, str(delay))
        delays.append(delay)
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    assert bot_module.restart_delay(uptime=3600) == 5  # долго проработал — снова быстро


def test_expired_interaction_is_not_an_error():
    import discord
    from types import SimpleNamespace as NS
    from utils import interaction_expired
    expired = discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10062, "message": "Unknown interaction"})
    acked = discord.HTTPException(NS(status=400, reason="Bad Request"),
                                  {"code": 40060, "message": "Interaction has already been acknowledged."})
    other = discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"})
    assert interaction_expired(expired) and interaction_expired(acked) and not interaction_expired(other)
    assert interaction_expired(discord.app_commands.CommandInvokeError(NS(name="x", qualified_name="x"), expired))


def test_only_last_owner_runs_without_database(tmp_path):
    allowed, reason = local_mirror.may_run_offline(tmp_path, "ns")
    assert not allowed and "ни разу" in reason  # копии базы нет — может работать другой экземпляр
    _mirror(tmp_path, {})
    assert local_mirror.may_run_offline(tmp_path, "ns")[0]
    # Этот бот стал резервным: базой владеет другой. Даже после перезапуска без базы он не запустится.
    local_mirror.note_other_owner(tmp_path, "ns")
    allowed, reason = local_mirror.may_run_offline(tmp_path, "ns")
    assert not allowed and "другой экземпляр" in reason
    # Снова стал владельцем — отметка сброшена.
    _mirror(tmp_path, {})
    assert local_mirror.may_run_offline(tmp_path, "ns")[0]


def test_standby_keeps_waiting_when_database_disappears(tmp_path, monkeypatch, caplog):
    import bot as bot_module
    from bot import ScrimBot
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://postgres@127.0.0.1:1/postgres")
    monkeypatch.setattr(config, "DATABASE_WAIT_SECONDS", 0)
    monkeypatch.setattr(bot_module, "SHARED_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(shared_state, "CONNECT_RETRY_DELAY", 0)
    _mirror(tmp_path, {})
    local_mirror.write(tmp_path, config.SHARED_STATE_KEY, NAMES, {}, {})
    local_mirror.note_other_owner(tmp_path, config.SHARED_STATE_KEY)
    bot = ScrimBot()

    async def run():
        from async_db import bounded_call
        waiting = asyncio.create_task(
            bot.acquire_shared(shared_state.SharedState, shared_state.SharedStateError, bounded_call))
        await asyncio.sleep(0.5)
        assert not waiting.done()  # резервный бот не запускается без базы: может работать владелец
        waiting.cancel()

    asyncio.run(run())
    assert "Без неё не запускаюсь" in caplog.text


def test_second_live_process_gets_no_persistent_id(tmp_path):
    import fcntl
    directory = tmp_path / "data"
    directory.mkdir()
    lock = os.open(directory / shared_state.INSTANCE_LOCK, os.O_RDWR | os.O_CREAT)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # «другой живой процесс бота» держит замок
    try:
        # Без постоянного ID его живую сессию в базе никто не примет за свою оборванную и не закроет.
        assert shared_state.local_instance_id(directory) is None
    finally:
        os.close(lock)
    assert shared_state.local_instance_id(directory) is not None
