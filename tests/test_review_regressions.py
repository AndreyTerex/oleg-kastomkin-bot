"""Independent review regressions against an explicitly disposable database."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import psycopg

from test_shared_state import database
from bets import Wallets
from stats import Stats
from storage import JsonStore, async_transaction
from cogs.lobby import UndoResultView


def test_queued_plain_save_preserves_earlier_result_transaction(database):
    owner, _, directory, _, _ = database
    wallets = Wallets(directory / 'wallet.json')
    wallets.load()
    stats = Stats(directory / 'stats.json')
    stats.load()

    async def result():
        async with async_transaction(stats.store, wallets.store):
            await stats.record_game(1, [10], [20], event_key='result')
            wallets.pay_bounty(1, [10], 50)
            await wallets.save()

    async def daily():
        wallets.claim_daily(1, 10, today='2026-10-31')
        await wallets.save()

    async def run():
        # A gateway admission / owner check can hold the same lock during DB I/O.
        async with owner.async_lock:
            tx = asyncio.create_task(result())
            await asyncio.sleep(0)
            standalone = asyncio.create_task(daily())
            await asyncio.sleep(0)
        await asyncio.gather(tx, standalone)
    asyncio.run(run())
    saved = owner.snapshot()
    memory = wallets.balance(1, 10)
    durable = saved['wallet.json']['1']['users']['10']['coins']
    print(f'result transaction followed by queued daily: memory={memory}, durable={durable}')
    assert len(saved['stats.json']['1']['games']) == 1
    assert durable == memory, 'A later stale whole-store save erased the committed match bounty'


@pytest.mark.parametrize('daily_first', [False, True])
def test_lobby_transaction_and_plain_save_follow_memory_order(database, daily_first):
    owner, _, directory, _, _ = database
    store = JsonStore(directory / 'lobbies.json')
    store.load()
    store.data['100'] = {'in': [], 'closed': False}

    async def joined():
        async with async_transaction(store):
            store.data['100']['in'].append(10)
            await store.save()

    async def standalone():
        store.data['100']['closed'] = True
        await store.save()

    async def run():
        actions = (standalone, joined) if daily_first else (joined, standalone)
        async with owner.async_lock:
            first = asyncio.create_task(actions[0]())
            await asyncio.sleep(0)
            second = asyncio.create_task(actions[1]())
            await asyncio.sleep(0)
        await asyncio.gather(first, second)
    asyncio.run(run())
    assert owner.snapshot()['lobbies.json'] == store.data == {'100': {'in': [10], 'closed': True}}


def test_yield_inside_preparation_fences_partial_state(database):
    owner, reader, directory, _, _ = database
    store = JsonStore(directory / 'wallet.json')
    store.load()
    async def run():
        with pytest.raises(RuntimeError, match='transaction body yielded'):
            async with async_transaction(store):
                store.data['partial'] = True
                await asyncio.sleep(0)
        assert owner.failed and store.data == {}
        with pytest.raises(RuntimeError):
            await store.save()
    asyncio.run(run())
    assert reader().snapshot()['wallet.json'] == {}


def test_already_ready_task_cannot_save_partial_transaction(database):
    owner, reader, directory, _, _ = database
    store = JsonStore(directory / 'wallet.json')
    store.load()
    async def competitor():
        store.data['other'] = True
        with pytest.raises(RuntimeError, match='unfinished shared transaction'):
            await store.save()
    async def run():
        # This task is ready BEFORE the preparation guard is scheduled.
        pending = asyncio.create_task(competitor())
        with pytest.raises(RuntimeError, match='transaction body yielded'):
            async with async_transaction(store):
                store.data['partial'] = True
                await asyncio.sleep(0)
        await pending
        assert owner.failed
    asyncio.run(run())
    assert reader().snapshot()['wallet.json'] == {}


def test_failed_transaction_fences_later_standalone_snapshot(database, monkeypatch):
    owner, reader, directory, _, _ = database
    wallets = Wallets(directory / 'wallet.json')
    wallets.load()
    stats = Stats(directory / 'stats.json')
    stats.load()
    original = owner._write_entries
    def fail_after_write(entries):
        original(entries)
        raise psycopg.errors.CheckViolation('injected transaction failure')
    monkeypatch.setattr(owner, '_write_entries', fail_after_write)
    async def result():
        async with async_transaction(stats.store, wallets.store):
            await stats.record_game(1, [10], [20], event_key='failed')
            wallets.pay_bounty(1, [10], 50)
            await wallets.save()
    async def daily():
        wallets.claim_daily(1, 10, today='2026-10-31')
        await wallets.save()
    async def run():
        async with owner.async_lock:
            first = asyncio.create_task(result())
            await asyncio.sleep(0)
            second = asyncio.create_task(daily())
            await asyncio.sleep(0)
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
        assert owner.failed
    asyncio.run(run())
    saved = reader().snapshot()
    assert saved['stats.json'] == saved['wallet.json'] == {}


def test_cancelled_transaction_fences_later_standalone_snapshot(database):
    owner, reader, directory, _, _ = database
    store = JsonStore(directory / 'lobbies.json')
    store.load()
    async def transaction_save():
        async with async_transaction(store):
            store.data['cancelled'] = True
            await store.save()
    async def standalone():
        store.data['later'] = True
        await store.save()
    async def run():
        async with owner.async_lock:
            first = asyncio.create_task(transaction_save())
            await asyncio.sleep(0)
            second = asyncio.create_task(standalone())
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        with pytest.raises(RuntimeError):
            await second
        assert owner.failed
    asyncio.run(run())
    assert reader().snapshot()['lobbies.json'] == {}


def test_failed_standalone_save_fences_later_transaction(database, monkeypatch):
    owner, reader, directory, _, _ = database
    store = JsonStore(directory / 'wallet.json')
    store.load()
    def bad_encoding(entries, operation):
        raise TypeError('injected JSON encoding failure')
    monkeypatch.setattr(owner, '_commit', bad_encoding)
    async def standalone():
        store.data['first'] = True
        await store.save()
    async def transaction_save():
        async with async_transaction(store):
            store.data['later'] = True
            await store.save()
    async def run():
        async with owner.async_lock:
            first = asyncio.create_task(standalone())
            await asyncio.sleep(0)
            second = asyncio.create_task(transaction_save())
            await asyncio.sleep(0)
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], TypeError) and isinstance(results[1], RuntimeError)
        assert owner.failed
    asyncio.run(run())
    assert reader().snapshot()['wallet.json'] == {}


def test_report_result_and_undo_callbacks_with_shared_database(database, monkeypatch):
    # Reuse the full actual-callback scenario, including two concurrent reports,
    # bets, bounty, duels and repeated undo, against PostgreSQL instead of JSON.
    from test_bets import test_report_result_pays_bets_bounty_duels_and_undo
    test_report_result_pays_bets_bounty_duels_and_undo(database[2], monkeypatch)


def test_substitute_callback_with_shared_database(database, monkeypatch):
    from test_substitute import test_substitute_button_end_to_end
    test_substitute_button_end_to_end(database[2], monkeypatch)


def test_queued_join_buttons_keep_capacity_and_duplicate_rules(database):
    from cogs import lobby
    owner, _, directory, _, _ = database
    cog = lobby.Lobby.__new__(lobby.Lobby)
    cog.store = JsonStore(directory / 'lobbies.json')
    cog.store.load()
    record = {'kind': lobby.KIND_ARAM_PARTY, 'target': 5, 'stage': 'signup',
              'in': [1, 2, 3, 4], 'sub': [], 'out': [], 'waiting': []}
    cog.store.data['100'] = record
    cog.build_embed = lambda *args: None
    cog.announce_promoted = AsyncMock()
    guild = SimpleNamespace(id=1, get_member=lambda uid: SimpleNamespace(id=uid))
    def interaction(uid):
        return SimpleNamespace(client=SimpleNamespace(get_cog=lambda name: cog), guild=guild,
            message=SimpleNamespace(id=100), user=SimpleNamespace(id=uid),
            response=SimpleNamespace(edit_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    async def run():
        view = lobby.LobbyView(record)
        tasks = []
        async with owner.async_lock:
            for uid in (5, 5, 6):
                tasks.append(asyncio.create_task(view._set_status(interaction(uid), 'in')))
                await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    asyncio.run(run())
    saved = owner.snapshot()['lobbies.json']['100']
    assert saved == record
    assert saved['in'] == [1, 2, 3, 4, 5] and saved['sub'] == saved['waiting'] == [6]


def test_report_rechecks_roster_after_defer_and_queued_substitute(database):
    from cogs import lobby
    owner, _, directory, _, _ = database
    stats = Stats(directory / 'stats.json')
    stats.load()
    wallets = Wallets(directory / 'wallet.json')
    wallets.load()
    cog = lobby.Lobby.__new__(lobby.Lobby)
    cog.store = JsonStore(directory / 'lobbies.json')
    cog.store.load()
    record = {'kind': 'custom', 'stage': 'done', 'author_id': 10, 'target': 2,
              'in': [10, 20], 'sub': [30], 'out': [], 'teams': [[10], [20]], 'round': 1}
    cog.store.data['100'] = record
    cog.build_embed = lambda *args: None
    stats_cog = SimpleNamespace(stats=stats, wallets=wallets)
    client = SimpleNamespace(get_cog={'Lobby': cog, 'Stats': stats_cog}.get)
    members = {uid: SimpleNamespace(id=uid, bot=False, name=str(uid), nick=None,
               global_name=None, display_name=str(uid), mention=f'<@{uid}>') for uid in (10, 20, 30)}
    guild = SimpleNamespace(id=1, get_member=members.get)
    message = SimpleNamespace(id=100, guild=guild, edit=AsyncMock())
    user = SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_events=False))
    async def run():
        deferred, resume = asyncio.Event(), asyncio.Event()
        async def defer():
            deferred.set()
            await resume.wait()
        result_interaction = SimpleNamespace(client=client, guild=guild, message=message, user=user,
            response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=AsyncMock()))
        substitute_interaction = SimpleNamespace(client=client, guild=guild, user=user,
            response=SimpleNamespace(edit_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        view = lobby.SubstituteView(cog, record, message)
        view.leaving._values = ['10']
        view.coming._values = [members[30]]
        reporting = asyncio.create_task(lobby.report_result(result_interaction, 100, 0))
        await deferred.wait()
        async with owner.async_lock:
            replacing = asyncio.create_task(view.confirm.callback(substitute_interaction))
            await asyncio.sleep(0)
            resume.set()
            await asyncio.sleep(0)
        await asyncio.gather(reporting, replacing)
        assert result_interaction.followup.send.await_count == 1
    asyncio.run(run())
    saved = owner.snapshot()
    assert saved['stats.json'] == {}
    assert saved['lobbies.json']['100']['teams'] == [[30], [20]]


def test_two_queued_undo_clicks_deduct_bounty_only_once(database):
    owner, _, directory, _, _ = database
    wallets = Wallets(directory / 'wallet.json')
    wallets.load()
    stats = Stats(directory / 'stats.json')
    stats.load()
    lobby_store = JsonStore(directory / 'lobbies.json')
    lobby_store.load()

    async def run():
        game_id = await stats.record_game(1, [10], [20], event_key='result')
        wallets.pay_bounty(1, [10], 50)
        await wallets.save()
        before = wallets.balance(1, 10)
        record = {'in': [10,20], 'sub': [], 'out': [], 'round': 2, 'results': [{'round': 1, 'winner': 0, 'game': game_id}]}
        lobby_store.data['100'] = record
        await lobby_store.save()
        guild = SimpleNamespace(id=1)
        stats_cog = SimpleNamespace(stats=stats, wallets=wallets)
        cog = SimpleNamespace(store=lobby_store, save=lobby_store.save, build_embed=lambda *a: None)
        lobby_message = SimpleNamespace(id=100, guild=guild, edit=AsyncMock())
        announcement = SimpleNamespace(content='result', edit=AsyncMock())
        view = UndoResultView(cog, record, lobby_message, game_id, 1, announcement)
        view.bounty = ([10], 50)
        def interaction():
            return SimpleNamespace(client=SimpleNamespace(get_cog=lambda name: stats_cog),
                guild=guild, response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        async with owner.async_lock:
            first = asyncio.create_task(view.undo.callback(interaction()))
            await asyncio.sleep(0)
            second = asyncio.create_task(view.undo.callback(interaction()))
            await asyncio.sleep(0)
        await asyncio.gather(first, second)
        after = wallets.balance(1, 10)
        print(f'two queued undo callbacks: before={before}, after={after}, expected={before-50}')
        assert after == before - 50, 'Second undo ignored undo_game(False) and deducted bounty again'
    asyncio.run(run())
