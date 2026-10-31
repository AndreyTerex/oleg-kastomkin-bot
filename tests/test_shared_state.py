"""Real PostgreSQL integration; uses an explicitly supplied disposable test DSN."""
import asyncio
import json
import os
import uuid
from copy import deepcopy

import psycopg
from psycopg import sql
import pytest

import config
import datasync
import shared_admin
import shared_state
import storage
from cogs.ops import make_backup
from stats import Stats, DuplicateGameError
from storage import JsonStore, configure_shared, transaction


@pytest.fixture
def database(tmp_path):
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set TEST_POSTGRES_DSN to a disposable PostgreSQL database")
    namespace = "test-" + uuid.uuid4().hex
    nodes = []
    def connect(directory=tmp_path, **kwargs):
        backend = shared_state.SharedState(dsn, namespace, directory, datasync.SYNC_FILES, **kwargs)
        nodes.append(backend)
        return backend
    owner = connect()
    owner.ensure_schema()
    assert owner.acquire()
    owner.initialize({"stats.json": {}, "lobbies.json": {}, "wallet.json": {}})
    configure_shared(owner)
    yield owner, connect, tmp_path, dsn, namespace
    configure_shared(None)
    for node in nodes:
        node.close()
    with psycopg.connect(dsn, autocommit=True) as cleanup:
        cleanup.execute("DELETE FROM oleg_state WHERE namespace=%s", (namespace,))


def test_one_writer_and_host_switch_ignore_stale_local_files(database):
    owner, connect, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    game = asyncio.run(stats.record_game(1, [10], [20], event_key="one-match"))
    second = connect(directory / "other-host")
    assert not second.acquire()
    with pytest.raises(shared_state.SharedStateError, match="own"):
        second.save({"stats.json": {"wrong": {}}})
    owner.close()
    assert second.acquire()
    second.require_initialized()
    configure_shared(second)
    second.directory.mkdir()
    (second.directory / "stats.json").write_text('{"stale":true}')
    resumed = Stats(second.directory / "stats.json")
    resumed.load()
    assert resumed.game(1, game)["winners"] == [10]
    assert resumed.player(1, 10).elo == 1020
    before = deepcopy(resumed.store.data)
    with pytest.raises(DuplicateGameError):
        asyncio.run(resumed.record_game(1, [10], [20], event_key="one-match"))
    assert resumed.store.data == before
    assert asyncio.run(resumed.undo_game(1, game))
    assert resumed.player(1, 10).elo == 1000
    # A correction gets a new revision; an old request cannot resurrect the undone result.
    with pytest.raises(DuplicateGameError):
        asyncio.run(resumed.record_game(1, [10], [20], event_key="one-match"))
    asyncio.run(resumed.record_game(1, [20], [10], event_key="one-match:revision1"))


def test_atomic_result_and_database_error_roll_back_all_stores(database):
    owner, _, directory, dsn, namespace = database
    stores = [JsonStore(directory / n) for n in ("stats.json", "lobbies.json", "wallet.json")]
    for store in stores:
        store.load()
    constraint = "fail_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER TABLE oleg_state ADD CONSTRAINT {} CHECK "
                              "(namespace <> {} OR name <> 'wallet.json' OR data->>'fail' IS DISTINCT FROM 'yes')")
                      .format(sql.Identifier(constraint), sql.Literal(namespace)))
        async def run():
            with transaction(*stores):
                for store in stores:
                    store.data["fail"] = "yes"
                    await store.save()
        try:
            with pytest.raises(shared_state.SharedStateError):
                asyncio.run(run())
            assert all(s.data == {} for s in stores)
            rows = admin.execute("SELECT data FROM oleg_state WHERE namespace=%s AND name <> '_schema'",
                                 (namespace,)).fetchall()
            assert all(row[0] == {} for row in rows)
            assert owner.failed
        finally:
            admin.execute(sql.SQL("ALTER TABLE oleg_state DROP CONSTRAINT {}").format(sql.Identifier(constraint)))


def test_lost_session_cannot_overwrite_new_owner(database):
    owner, connect, _, dsn, _ = database
    pid = owner.connection.info.backend_pid
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    second = connect()
    assert second.acquire()
    second.save({"stats.json": {"new-owner": {}}})
    with pytest.raises(shared_state.SharedStateError):
        owner.save({"stats.json": {"stale-owner": {}}})
    assert owner.failed
    assert second.snapshot()["stats.json"] == {"new-owner": {}}
    with pytest.raises(shared_state.SharedStateError):
        owner.save({"stats.json": {"retry": {}}})


def test_exports_backups_and_local_caches_use_correct_sources(database):
    owner, _, directory, _, _ = database
    owner.save({"stats.json": {"1": {"server": True}}})
    (directory / "stats.json").write_text('{"stale":true}')
    local = JsonStore(directory / "llm_usage.json")
    local.data = {"local-only": True}
    asyncio.run(local.save())
    assert "llm_usage.json" not in owner.snapshot()
    payload, names = datasync.build_archive(directory, [1])
    files, _ = datasync.read_archive(payload)
    assert json.loads(files["stats.json"]) == {"1": {"server": True}}
    assert "llm_usage.json" not in names
    backup = make_backup(directory, "test-day", keep=7)
    assert json.loads((backup / "stats.json").read_text()) == {"1": {"server": True}}
    with pytest.raises(datasync.ImportError_):
        datasync.apply_archive(directory, {"stats.json": b"{}"})


def test_shared_mode_preserves_history_beyond_local_limit(database, monkeypatch):
    import stats as stats_module
    monkeypatch.setattr(stats_module, "HISTORY_LIMIT", 2)
    _, _, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    async def run():
        for i in range(4):
            await stats.record_game(1, [10], [20], event_key=f"game:{i}")
    asyncio.run(run())
    assert len(stats.store.data["1"]["games"]) == 4
    restored = Stats(stats.store.path)
    restored.load()
    assert restored.player(1, 10).wins == 4 and restored.games_count(1) == 4


def recovered_snapshot():
    # Synthetic schema fixture, NOT the user's production data.
    ids = list(range(1, 35)) + [39, 40, 41]
    return {"stats.json": {"1": {"players": {"10": {"wins": 37, "losses": 0, "elo": 1500.5}},
        "games": [{"id": i, "winners": [10], "losers": [20], "at": i, "future_field": {"preserve": True}}
                  for i in ids], "next_id": 42, "elo_version": 1, "recovery_note": "preserve"}},
            "lobbies.json": {"20": {"opaque": [1, 2, 3]}}}


def test_migration_preserves_37_games_unknown_fields_and_readback(tmp_path, database, monkeypatch):
    owner, _, _, dsn, namespace = database
    owner.close()
    configure_shared(None)
    # Use a fresh namespace for explicit initialization.
    namespace += "-migration"
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    monkeypatch.setattr(config, "SHARED_STATE_KEY", namespace)
    source = tmp_path / "source.zip"
    entries = recovered_snapshot()
    payload, _ = datasync.archive_entries(entries, [1])
    source.write_bytes(payload)
    backup = tmp_path / "pre-migration.zip"
    args = ["init", "--source", str(source), "--backup", str(backup), "--guild-id", "1",
            "--min-games", "37", "--min-next-id", "42", "--require-game-id", "39",
            "--require-game-id", "40", "--require-game-id", "41", "--apply"]
    try:
        shared_admin.main(args)
        reader = shared_state.SharedState(dsn, namespace, tmp_path, datasync.SYNC_FILES)
        try:
            assert reader.snapshot() == entries
            assert shared_admin.load_export(backup) == entries
            before = reader.snapshot()
            with pytest.raises(shared_state.SharedStateError, match="already contains"):
                shared_admin.main([*args[:], "--backup", str(tmp_path / "second.zip")])
            assert reader.snapshot() == before
        finally:
            reader.close()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute("DELETE FROM oleg_state WHERE namespace=%s", (namespace,))


def test_restore_requires_digest_and_keeps_backup(database, monkeypatch):
    owner, _, directory, dsn, namespace = database
    initial = recovered_snapshot()
    owner.save(initial)
    owner.close()
    configure_shared(None)
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    monkeypatch.setattr(config, "SHARED_STATE_KEY", namespace)
    source = directory / "new.zip"
    replacement = deepcopy(initial)
    replacement["stats.json"]["1"]["recovery_note"] = "verified correction"
    source.write_bytes(datasync.archive_entries(replacement, [1])[0])
    backup = directory / "rollback.zip"
    reader = shared_state.SharedState(dsn, namespace, directory, datasync.SYNC_FILES)
    try:
        current = reader.snapshot()
        args = ["restore", "--source", str(source), "--backup", str(backup), "--apply"]
        with pytest.raises(shared_state.SharedStateError, match="digest"):
            shared_admin.main(args + ["--expected-digest", "wrong"])
        assert not backup.exists()
        shared_admin.main(args + ["--expected-digest", shared_state.digest(current)])
        assert shared_admin.load_export(backup) == current
        assert reader.snapshot()["stats.json"] == replacement["stats.json"]
    finally:
        reader.close()


def test_dry_run_never_connects_and_missing_recovered_game_is_rejected(tmp_path, monkeypatch):
    entries = recovered_snapshot()
    source = tmp_path / "source.zip"
    source.write_bytes(datasync.archive_entries(entries, [1])[0])
    def unexpected(*args, **kwargs):
        raise AssertionError("Dry run connected to a database")
    monkeypatch.setattr(shared_admin, "SharedState", unexpected)
    shared_admin.main(["init", "--source", str(source), "--guild-id", "1", "--min-games", "37"])
    with pytest.raises(ValueError, match="required"):
        shared_admin.main(["init", "--source", str(source), "--guild-id", "1", "--require-game-id", "38"])


def test_neon_pooler_and_insecure_remote_urls_are_rejected(tmp_path):
    for dsn in ("postgresql://test@ep-example-pooler.eu.neon.tech/test?sslmode=require",
                "postgresql://test@example.invalid/test?sslmode=disable"):
        with pytest.raises(shared_state.SharedStateError):
            shared_state.SharedState(dsn, "test", tmp_path, datasync.SYNC_FILES)


def test_migration_rejects_newer_elo_before_writing():
    entries = recovered_snapshot()
    entries["stats.json"]["1"]["elo_version"] = 999
    with pytest.raises(ValueError, match="newer Elo"):
        shared_admin.stats_summary(entries)


def test_busy_database_waits_in_standby_before_extensions_or_discord_sync(database, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    import bot as bot_module
    from bot import ScrimBot
    owner, _, directory, dsn, namespace = database
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    monkeypatch.setattr(config, "SHARED_STATE_KEY", namespace)
    monkeypatch.setattr(config, "DATA_DIR", directory)
    monkeypatch.setattr(bot_module, "SHARED_RETRY_SECONDS", 0.05)
    bot = ScrimBot()
    bot.load_extension = AsyncMock()
    bot.tree.sync = AsyncMock()
    async def run():
        startup = asyncio.create_task(bot.setup_hook())
        await asyncio.sleep(0.5)
        # Владелец жив — резервный экземпляр ждёт и ничего не грузит, а не падает.
        assert not startup.done()
        bot.load_extension.assert_not_awaited()
        bot.tree.sync.assert_not_awaited()
        owner.close()
        await asyncio.wait_for(startup, 10)
        bot.load_extension.assert_awaited()
        await bot.shared_state.aclose()
    asyncio.run(run())
    assert "владеет другой экземпляр" in caplog.text and "oleg:" in caplog.text


def test_abandoned_session_releases_lock_after_idle_timeout(database, monkeypatch):
    _, connect, directory, _, _ = database
    monkeypatch.setattr(shared_state, "IDLE_SESSION_TIMEOUT_MS", 500)
    database[0].close()  # освобождаем замок владельца фикстуры
    zombie = connect(directory)
    assert zombie.acquire()
    # Клиент «пропал» (сон ноутбука, обрыв сети): соединение висит, запросов нет.
    assert not connect(directory / "other-host").acquire()
    import time
    time.sleep(1.5)
    # Резервный бот пробует каждый раз новым подключением (bot.acquire_shared) — и забирает замок.
    assert connect(directory / "other-host").acquire()


def disconnect_session(owner, dsn):
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (owner.connection.info.backend_pid,))


def test_idle_loss_reacquires_and_preserves_first_user_mutation(database):
    owner, _, directory, dsn, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    first = asyncio.run(stats.record_game(1, [10], [20], event_key="before-sleep"))
    pid = owner.connection.info.backend_pid
    disconnect_session(owner, dsn)
    second = asyncio.run(stats.record_game(1, [20], [10], event_key="after-sleep"))
    assert (first, second) == (1, 2)
    assert not owner.failed and owner.connection.info.backend_pid != pid
    assert stats.games_count(1) == 2
    restored = Stats(directory / "stats.json")
    restored.load()
    assert restored.store.data == stats.store.data


def test_idle_loss_read_reconnects_without_restart(database):
    owner, _, _, dsn, _ = database
    before = owner.snapshot()
    disconnect_session(owner, dsn)
    assert owner.snapshot() == before and not owner.failed
    disconnect_session(owner, dsn)
    owner.check_owner()
    assert not owner.failed


def test_real_idle_session_timeout_wakes_only_on_demand(database):
    import time
    owner, _, _, _, _ = database
    old_pid = owner.connection.info.backend_pid
    owner.connection.execute("SET idle_session_timeout = '100ms'")
    time.sleep(0.25)
    # No heartbeat/reconnect ran during idle. The first real action wakes it.
    assert owner.connection.info.backend_pid == old_pid
    owner.check_owner()
    assert owner.connection.info.backend_pid != old_pid and not owner.failed


def test_disconnect_mid_transaction_rolls_back_before_safe_retry(database, monkeypatch):
    owner, _, directory, dsn, _ = database
    stores = [JsonStore(directory / name) for name in ("stats.json", "wallet.json")]
    for store in stores:
        store.load()
    original = owner._commit
    calls = []
    def partial_then_disconnect(entries, operation):
        calls.append(operation)
        if len(calls) == 1:
            with owner.connection.transaction():
                owner._write_entries({"stats.json": entries["stats.json"]})
                disconnect_session(owner, dsn)
        else:
            original(entries, operation)
    monkeypatch.setattr(owner, "_commit", partial_then_disconnect)
    with transaction(*stores):
        stores[0].data["result"] = "once"
        stores[1].data["coins"] = 100
    saved = owner.snapshot()
    assert saved["stats.json"] == {"result": "once"}
    assert saved["wallet.json"] == {"coins": 100}
    assert len(calls) == 2 and len(set(calls)) == 1


def test_other_host_changes_then_disconnects_stale_mutation_rejected(database):
    owner, connect, directory, dsn, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    asyncio.run(stats.record_game(1, [10], [20], event_key="old"))
    disconnect_session(owner, dsn)
    second = connect()
    assert second.acquire()
    second.require_initialized()
    updated = deepcopy(second.snapshot()["stats.json"])
    updated["1"]["next_id"] = 42
    second.save({"stats.json": updated})
    second.close()
    with pytest.raises(shared_state.SharedStateError, match="changed on another host"):
        asyncio.run(stats.record_game(1, [10], [20], event_key="stale-action"))
    assert owner.failed
    fresh = connect()
    assert fresh.acquire()
    fresh.require_initialized()
    configure_shared(fresh)
    restored = Stats(directory / "stats.json")
    restored.load()
    assert restored.store.data["1"]["next_id"] == 42
    assert asyncio.run(restored.record_game(1, [10], [20], event_key="fresh-action")) == 42


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_readback_prevents_duplicate_mutation(database, monkeypatch, committed):
    owner, _, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    original = owner._commit
    calls = []
    def unreliable(entries, operation):
        calls.append(operation)
        if len(calls) == 1:
            if committed:
                original(entries, operation)
            owner.connection.close()
            raise psycopg.OperationalError("simulated lost connection/acknowledgement")
        return original(entries, operation)
    monkeypatch.setattr(owner, "_commit", unreliable)
    assert asyncio.run(stats.record_game(1, [10], [20], event_key="exactly-once")) == 1
    assert len(calls) == (1 if committed else 2)
    assert len(set(calls)) == 1  # retry retains the operation identity
    assert owner.snapshot()["stats.json"]["1"]["next_id"] == 2
    assert stats.player(1, 10).wins == 1 and stats.player(1, 10).elo == 1020
    assert not owner.failed
    with pytest.raises(DuplicateGameError):
        asyncio.run(stats.record_game(1, [10], [20], event_key="exactly-once"))


def test_uncertain_commit_followed_by_other_writer_is_not_replayed(database, monkeypatch):
    owner, connect, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    original = owner._commit
    calls = []
    def committed_then_taken_over(entries, operation):
        calls.append(operation)
        original(entries, operation)
        owner.connection.close()
        second = connect()
        assert second.acquire()
        second.require_initialized()
        second.save({"wallet.json": {"other-host": True}})
        second.close()
        raise psycopg.OperationalError("lost acknowledgement")
    monkeypatch.setattr(owner, "_commit", committed_then_taken_over)
    with pytest.raises(shared_state.SharedStateError, match="changed on another host"):
        asyncio.run(stats.record_game(1, [10], [20], event_key="committed-once"))
    assert len(calls) == 1
    reader = connect()
    saved = reader.snapshot()
    assert len(saved["stats.json"]["1"]["games"]) == 1
    assert saved["wallet.json"] == {"other-host": True}


def test_gateway_receipt_survives_wake_and_host_switch(database):
    owner, connect, _, dsn, _ = database
    key = "INTERACTION_CREATE:123456789"
    assert owner.claim_event(key)
    disconnect_session(owner, dsn)
    assert not owner.claim_event(key)
    assert owner.claim_event("INTERACTION_CREATE:123456790")
    owner.close()
    second = connect()
    assert second.acquire()
    second.require_initialized()
    assert not second.claim_event(key)
    assert second.claim_event("MESSAGE_CREATE:123456789")


def test_uncertain_event_admission_does_not_dispatch_twice(database, monkeypatch):
    owner, _, _, _, _ = database
    original = owner._commit
    calls = []
    def lose_ack(entries, operation, event_key=None):
        calls.append(event_key)
        original(entries, operation, event_key)
        owner.connection.close()
        raise psycopg.OperationalError("lost ack")
    monkeypatch.setattr(owner, "_commit", lose_ack)
    assert owner.claim_event("INTERACTION_CREATE:one")
    assert not owner.claim_event("INTERACTION_CREATE:one")
    assert calls == ["INTERACTION_CREATE:one"]


def test_same_token_gateway_and_outgoing_effects_are_fenced(database, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import Mock, AsyncMock
    from shared_discord import install_discord_guard, OwnedWebhookAdapter
    from discord.webhook.async_ import AsyncWebhookAdapter
    owner, connect, _, dsn, _ = database
    events = ("INTERACTION_CREATE", "MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE")
    parsers = {event: Mock() for event in events}
    send = AsyncMock()
    webhook_send = AsyncMock()
    monkeypatch.setattr(AsyncWebhookAdapter, "request", webhook_send)
    bot = NS(http=NS(request=send), _connection=NS(parsers=dict(parsers)))
    async def drain():
        if bot._shared_event_tasks:
            await asyncio.gather(*tuple(bot._shared_event_tasks))
    async def run():
        install_discord_guard(bot, owner)
        # Pure installation and bot-authored gateway events perform no DB operation.
        revision = owner._revision
        bot._connection.parsers["MESSAGE_CREATE"]({"id": "99", "author": {"bot": True}})
        assert owner._revision == revision and not bot._shared_event_tasks
        disconnect_session(owner, dsn)
        for _ in range(2):
            bot._connection.parsers["INTERACTION_CREATE"]({"id": "123", "type": 3})
        await drain()
        assert parsers["INTERACTION_CREATE"].call_count == 1
        await bot.http.request(NS(method="POST"))
        await OwnedWebhookAdapter(owner).request(NS(method="POST"), None)
        assert send.await_count == webhook_send.await_count == 1
        disconnect_session(owner, dsn)
        second = connect()
        assert second.acquire()
        second.require_initialized()
        # Same gateway delivery on new host is skipped by the durable receipt.
        assert not second.claim_event("INTERACTION_CREATE:123")
        bot._connection.parsers["INTERACTION_CREATE"]({"id": "124", "type": 3})
        await drain()
        assert parsers["INTERACTION_CREATE"].call_count == 1
        with pytest.raises(shared_state.SharedStateError):
            await bot.http.request(NS(method="PATCH"))
        with pytest.raises(shared_state.SharedStateError):
            await OwnedWebhookAdapter(owner).request(NS(method="POST"), None)
        assert send.await_count == webhook_send.await_count == 1
    asyncio.run(run())


def test_persistent_button_parser_is_guarded_before_view_dispatch(database, monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    import discord.state
    from bot import ScrimBot
    from shared_discord import install_discord_guard
    owner, connect, _, dsn, _ = database
    bot = ScrimBot()
    dispatch_view = Mock()
    monkeypatch.setattr(discord.state, "Interaction", lambda **kwargs: NS(id=kwargs["data"]["id"]))
    monkeypatch.setattr(bot._connection._view_store, "dispatch_view", dispatch_view)
    monkeypatch.setattr(bot._connection, "dispatch", Mock())
    async def run():
        install_discord_guard(bot, owner)
        payload = {"id": "123", "type": 3, "data": {"custom_id": "lol:lobby:in", "component_type": 2}}
        parse = bot._connection.parsers["INTERACTION_CREATE"]
        parse(payload)
        await asyncio.gather(*tuple(bot._shared_event_tasks))
        assert dispatch_view.call_count == 1
        disconnect_session(owner, dsn)
        second = connect()
        assert second.acquire()
        second.require_initialized()
        parse({**payload, "id": "124"})
        await asyncio.gather(*tuple(bot._shared_event_tasks))
        assert dispatch_view.call_count == 1
    asyncio.run(run())


def test_quiz_style_save_keeps_discord_heartbeat_running(database, monkeypatch):
    import threading
    import async_db
    owner, _, directory, _, _ = database
    store = JsonStore(directory / "quiz.json")
    store.load()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = owner._commit
    def stalled(entries, operation):
        entered.set()
        try:
            release.wait(2)  # simulate a socket read ignoring server statement_timeout
            original(entries, operation)
        finally:
            finished.set()
    monkeypatch.setattr(owner, "_commit", stalled)
    monkeypatch.setattr(async_db, "OPERATION_TIMEOUT", 0.08)
    async def run():
        store.data["1"] = {"last_at": 1}
        save = asyncio.create_task(store.save())
        ticks = 0
        try:
            while not save.done():
                await asyncio.sleep(0.005)
                ticks += 1
            with pytest.raises(shared_state.SharedStateError, match="deadline"):
                await save
            assert entered.is_set() and ticks >= 5
            assert owner.failed and owner._stop.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
    asyncio.run(run())
    # The timed-out worker may not resume and commit from old memory.
    reader = database[1]()
    assert "quiz.json" not in reader.snapshot()


def test_async_partial_transaction_timeout_rolls_back_all_state(database, monkeypatch):
    import threading
    import async_db
    owner, connect, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    before = deepcopy(stats.store.data)
    release, finished = threading.Event(), threading.Event()
    original_write, original_commit = owner._write_entries, owner._commit
    def slow_write(entries):
        original_write(entries)
        release.wait(2)
    def observed_commit(entries, operation):
        try:
            original_commit(entries, operation)
        finally:
            finished.set()
    monkeypatch.setattr(owner, "_write_entries", slow_write)
    monkeypatch.setattr(owner, "_commit", observed_commit)
    monkeypatch.setattr(async_db, "OPERATION_TIMEOUT", 0.05)
    async def run():
        try:
            with pytest.raises(shared_state.SharedStateError, match="deadline"):
                await stats.record_game(1, [10], [20], event_key="must-not-commit")
            assert stats.store.data == before
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
    asyncio.run(run())
    assert connect().snapshot()["stats.json"] == before


def test_async_match_transactions_serialize_concurrent_results(database):
    owner, _, directory, _, _ = database
    stats = Stats(directory / "stats.json")
    stats.load()
    async def run():
        return await asyncio.gather(*(stats.record_game(1, [10], [20], event_key=f"concurrent:{i}")
                                      for i in range(12)))
    assert sorted(asyncio.run(run())) == list(range(1, 13))
    saved = owner.snapshot()["stats.json"]["1"]
    assert len(saved["games"]) == 12 and saved["players"]["10"]["wins"] == 12


def test_runtime_load_uses_preloaded_snapshot_without_network(database, monkeypatch):
    owner, _, directory, _, _ = database
    owner.runtime = True
    owner._saved["quiz.json"] = {"1": {"score": 2}}
    def forbidden(*args):
        raise AssertionError("Runtime load used synchronous database I/O")
    monkeypatch.setattr(owner, "load", forbidden)
    store = JsonStore(directory / "quiz.json")
    assert store.load() == {"1": {"score": 2}}
    store.data["1"]["score"] = 3
    assert owner._saved["quiz.json"]["1"]["score"] == 2


def test_successful_startup_loads_all_cogs_without_live_discord(database, monkeypatch):
    import threading
    from unittest.mock import AsyncMock
    from bot import ScrimBot, EXTENSIONS
    owner, _, directory, dsn, namespace = database
    owner.close()
    configure_shared(None)
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    monkeypatch.setattr(config, "SHARED_STATE_KEY", namespace)
    monkeypatch.setattr(config, "DATA_DIR", directory)
    monkeypatch.setattr(config, "GUILD_ID", 0)
    monkeypatch.setattr(config, "WATCHDOG_DISCONNECT_MINUTES", 0)
    main_thread = threading.get_ident()
    for name in ("_connect", "acquire", "_read_state", "_commit"):
        original = getattr(shared_state.SharedState, name)
        def off_loop(self, *args, _original=original, **kwargs):
            assert threading.get_ident() != main_thread, "Synchronous DB work reached Discord's loop"
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(shared_state.SharedState, name, off_loop)
    bot = ScrimBot()
    bot.tree.sync = AsyncMock(return_value=[])
    async def run():
        async with bot:
            await bot.setup_hook()
            assert set(bot.extensions) == set(EXTENSIONS)
            assert bot.shared_state.runtime and not bot.shared_state.failed
            assert not bot.sync.enabled
            assert bot.tree.sync.await_count == 1
    asyncio.run(run())


def test_cancelled_queued_save_fences_later_mutations(database):
    owner, _, directory, _, _ = database
    store = JsonStore(directory / "quiz.json")
    async def run():
        async with owner.async_lock:
            store.data["cancelled"] = True
            save = asyncio.create_task(store.save())
            await asyncio.sleep(0)
            save.cancel()
            with pytest.raises(asyncio.CancelledError):
                await save
        assert owner.failed
        with pytest.raises(shared_state.SharedStateError):
            await store.save()
    asyncio.run(run())


def test_async_close_is_bounded_behind_stuck_worker(database, monkeypatch):
    import threading
    import time
    import async_db
    owner, _, _, _, _ = database
    held, release = threading.Event(), threading.Event()
    def occupy():
        with owner._mutex:
            held.set()
            release.wait(2)
    thread = threading.Thread(target=occupy, daemon=True)
    thread.start()
    assert held.wait(1)
    monkeypatch.setattr(async_db, "CLOSE_TIMEOUT", 0.03)
    async def run():
        started = time.monotonic()
        try:
            await owner.aclose()
            assert time.monotonic() - started < 0.3
            assert owner._stop.is_set()
        finally:
            release.set()
    asyncio.run(run())
    thread.join(1)


def test_database_session_has_correlatable_nonsecret_owner_label(database):
    owner, _, _, _, _ = database
    assert owner.connection.execute("SHOW application_name").fetchone()[0] == f"oleg:{owner.instance_id}"


def test_unreachable_database_retries_instead_of_crashing(database, monkeypatch, caplog):
    import bot as bot_module
    from bot import ScrimBot
    from async_db import bounded_call
    owner, _, directory, dsn, namespace = database
    owner.close()
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://postgres@127.0.0.1:1/postgres")
    monkeypatch.setattr(config, "SHARED_STATE_KEY", namespace)
    monkeypatch.setattr(config, "DATA_DIR", directory)
    monkeypatch.setattr(bot_module, "SHARED_RETRY_SECONDS", 0.05)
    bot = ScrimBot()
    async def run():
        waiting = asyncio.create_task(
            bot.acquire_shared(shared_state.SharedState, shared_state.SharedStateError, bounded_call))
        await asyncio.sleep(0.5)
        assert not waiting.done()
        config.DATABASE_URL = dsn  # сеть вернулась
        backend = await asyncio.wait_for(waiting, 10)
        assert backend.owned
        await backend.aclose()
    asyncio.run(run())
    assert "База недоступна" in caplog.text
