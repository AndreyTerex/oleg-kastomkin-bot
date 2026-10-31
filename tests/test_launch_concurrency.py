"""Real callbacks and PostgreSQL coverage for the launch/draft transition."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_shared_state import database
from storage import JsonStore
from captains import CaptainHistory
from cogs import lobby


def launch_setup(database):
    owner, _, directory, _, _ = database
    cog = lobby.Lobby.__new__(lobby.Lobby)
    cog.store = JsonStore(directory / 'lobbies.json')
    cog.store.load()
    cog.captain_history = CaptainHistory(directory / 'captains.json')
    cog.captain_history.load()
    cog.drafts = {}
    players = [NS(id=i) for i in (10, 20, 30, 40)]
    record = {'kind': lobby.KIND_CUSTOM, 'stage': lobby.STAGE_SIGNUP, 'target': 4,
              'in': [p.id for p in players], 'sub': [], 'out': [], 'author_id': 10}
    cog.store.data['100'] = record
    cog.is_organizer = lambda *a: True
    cog.signed_players = lambda *a: players
    cog.build_embed = lambda *a: None
    cog.bot = NS(get_cog={'Lobby': cog, 'Scrim': NS()}.get)
    guild = NS(id=1, get_member={p.id: p for p in players}.get)
    created = []

    def draft_view(message, record, captains, pool):
        view = NS(captains=captains, pool=pool,
                  teams=[[captains[0]], [captains[1]]],
                  stop=Mock(), build_embed=lambda: None, message=None)
        cog.drafts[message.id] = view
        created.append(view)
        return view

    cog.draft_view = draft_view

    def interaction():
        return NS(client=cog.bot, guild=guild, user=players[0], message=NS(id=100),
                  response=NS(edit_message=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
                  followup=NS(send=AsyncMock(return_value=NS(id=999, edit=AsyncMock()))))
    return owner, cog, record, created, interaction


def test_concurrent_launch_does_not_restart_started_draft(database):
    owner, cog, record, created, interaction = launch_setup(database)
    async def run():
        view = lobby.LobbyView(record)
        async with owner.async_lock:
            first = asyncio.create_task(view.launch.callback(interaction()))
            await asyncio.sleep(0)
            second = asyncio.create_task(view.launch.callback(interaction()))
            await asyncio.sleep(0)
        await asyncio.gather(first, second)
    asyncio.run(run())
    print(f'drafts created={len(created)}, draft_no={record.get("draft_no")}, '
          f'first stopped={created[0].stop.call_count}')
    assert len(created) == 1, 'Second launch passed the old stage check before captain history save yielded'
    assert record['draft_no'] == 1 and created[0].stop.call_count == 0


@pytest.mark.parametrize('phase', ['captains', 'draft'])
def test_close_during_database_wait_cannot_resume_launch(database, phase):
    owner, cog, record, created, interaction = launch_setup(database)
    if phase == 'draft':
        record.update(stage=lobby.STAGE_CAPTAINS, captains=[10, 20])
    async def run():
        view = lobby.LobbyView(record)
        starting = interaction()
        callback = view.launch.callback if phase == 'captains' else view.start_draft.callback
        async with owner.async_lock:
            start = asyncio.create_task(callback(starting))
            await asyncio.sleep(0)
            close = asyncio.create_task(view.cancel.callback(interaction()))
            await asyncio.sleep(0)
            assert record['closed']
        await asyncio.gather(start, close)
        assert starting.followup.send.await_count == 0
        assert record['closed'] and not cog.drafts
        if phase == 'captains':
            assert not created and 'captains' not in record and 'draft_no' not in record
        else:
            assert len(created) == 1 and created[0].stop.call_count == 1
    asyncio.run(run())
    assert owner.snapshot()['lobbies.json']['100']['closed']


@pytest.mark.parametrize('phase', ['captains', 'draft'])
def test_cancelled_database_wait_cannot_leave_live_draft(database, phase):
    owner, cog, record, created, interaction = launch_setup(database)
    if phase == 'draft':
        record.update(stage=lobby.STAGE_CAPTAINS, captains=[10, 20])
    async def run():
        view = lobby.LobbyView(record)
        starting = interaction()
        callback = view.launch.callback if phase == 'captains' else view.start_draft.callback
        async with owner.async_lock:
            start = asyncio.create_task(callback(starting))
            await asyncio.sleep(0)
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
        assert owner.failed and not cog.drafts
        assert starting.followup.send.await_count == 0
        assert all(draft.stop.call_count == 1 for draft in created)
    asyncio.run(run())
    saved = database[1]().snapshot()
    assert saved['lobbies.json'] == {} and saved.get('captains.json', {}) == {}


def test_old_launch_cannot_take_over_after_reset_and_new_launch(database):
    owner, cog, record, created, interaction = launch_setup(database)
    async def run():
        view = lobby.LobbyView(record)
        async with owner.async_lock:
            first = asyncio.create_task(view.launch.callback(interaction()))
            await asyncio.sleep(0)
            lobby.reset_series(record)
            second = asyncio.create_task(view.launch.callback(interaction()))
            await asyncio.sleep(0)
        await asyncio.gather(first, second)
    asyncio.run(run())
    assert len(created) == 1 and created[0].stop.call_count == 0
    # reset_series itself increments draft_no to invalidate old draft views.
    assert record['launch_no'] == 2 and record['draft_no'] == 2
    saved = owner.snapshot()
    assert saved['lobbies.json']['100']['captains'] == saved['captains.json']['1'][0]['pair']


@pytest.mark.parametrize('phase', ['edit', 'send'])
def test_close_during_discord_wait_does_not_attach_stale_draft(database, phase):
    owner, cog, record, created, interaction = launch_setup(database)
    record.update(stage=lobby.STAGE_CAPTAINS, captains=[10, 20])
    async def run():
        starting = interaction()
        posted = NS(id=999, edit=AsyncMock())
        async def close_during_io(*args, **kwargs):
            record['closed'] = True
            await cog.save()
            return posted
        if phase == 'edit':
            starting.response.edit_message = AsyncMock(side_effect=close_during_io)
        else:
            starting.followup.send = AsyncMock(side_effect=close_during_io)
        await lobby.LobbyView(record).start_draft.callback(starting)
        assert record['closed'] and not cog.drafts and created[0].stop.call_count == 1
        assert 'message_id' not in record['draft']
        if phase == 'edit':
            assert starting.followup.send.await_count == 0
        else:
            posted.edit.assert_awaited_once_with(view=None)
    asyncio.run(run())
    saved = owner.snapshot()['lobbies.json']['100']
    assert saved['closed'] and 'message_id' not in saved['draft']
