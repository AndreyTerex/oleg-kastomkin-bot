"""Новые форматы проходят тот же жизненный цикл сбора без подключения к Discord."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

import config
import modes
from cogs import lobby
from cogs.chat import Chat, LOBBY_TOPIC
from lobby_state import RosterError
from storage import JsonStore
from tests.conftest import FakeMember


FORMATS = [
    ("aram", None, lobby.KIND_ARAM_PARTY, 5),
    ("aram", lobby.ARAM_FORMAT_CHOICES[1], lobby.KIND_ARAM_CUSTOM, 10),
    ("flex", None, lobby.KIND_FLEX, 5),
]


@pytest.fixture
def setup_lobby(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    members = {i: FakeMember(i, f"player{i}") for i in range(1, 15)}
    guild = NS(id=123, get_member=members.get)
    bot = NS(get_cog=lambda name: None, add_view=Mock())
    cog = lobby.Lobby(bot)
    cog.preferences.data[str(guild.id)] = modes.PRESET_BY_KEY["chaos"].mode
    bot.get_cog = lambda name: cog if name == "Lobby" else None
    message = NS(id=456, guild=guild, edit=AsyncMock())
    interaction = NS(
        client=bot, guild=guild, channel_id=789, message=message,
        user=NS(id=1, guild_permissions=NS(mention_everyone=False, manage_events=False)),
        app_permissions=NS(mention_everyone=True),
        response=NS(send_message=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: False),
        followup=NS(send=AsyncMock()), original_response=AsyncMock(return_value=message),
    )
    return cog, interaction


async def create(cog, interaction, command, format):
    options = {"format": format} if command == "aram" else {}
    await getattr(lobby.Lobby, command).callback(cog, interaction, when="завтра 21:00", **options)
    return cog.get_record(interaction.message.id)


@pytest.mark.parametrize("command,format,kind,target", FORMATS)
def test_create_persist_and_restore_formats(setup_lobby, command, format, kind, target):
    cog, interaction = setup_lobby

    async def run():
        record = await create(cog, interaction, command, format)
        assert record["kind"] == kind and record["target"] == target
        assert record["start_at"] is not None and not record["reminded"]
        assert record["stage"] == lobby.STAGE_SIGNUP and not record["teams"]
        assert "mode" not in record and "series" not in record
        assert cog.preferences.data[str(interaction.guild.id)] == modes.PRESET_BY_KEY["chaos"].mode
        payload = interaction.response.send_message.call_args.kwargs
        assert payload["content"] is None  # прежняя проверка права пинговать @everyone
        assert not payload["allowed_mentions"].everyone
        assert lobby.KIND_TITLES[kind] == payload["embed"].title
        assert f"**0/{target}**" in payload["embed"].description
        cog.apply_move(interaction.guild, record, list(range(1, target + 2)), "in")
        await cog.save()

        # Перезапуск через настоящий cog_load, но без фоновых сетевых запросов.
        restored = lobby.Lobby(interaction.client)
        restored._background = AsyncMock()
        await restored.cog_load()
        await restored._refresh_task
        assert restored.get_record(456) == record
        view = lobby.LobbyView(restored.get_record(456))
        assert view.is_persistent()
        ids = {item.custom_id for item in view.children}
        assert not ids & lobby.CUSTOM_GAME_ONLY
        assert {"lol:lobby:in", "lol:lobby:sub", "lol:lobby:out", lobby.CUSTOM_ID_MANAGE} <= ids
        assert restored.latest_open_record(interaction.guild)[0] == "456"
        await restored.cog_unload()

    asyncio.run(run())


@pytest.mark.parametrize("command,format,kind,target", FORMATS)
def test_buttons_capacity_duplicates_queue_leave_and_close(setup_lobby, command, format, kind, target):
    cog, interaction = setup_lobby

    async def press(user_id, status):
        click = NS(**vars(interaction))
        click.user = NS(**(vars(interaction.user) | {"id": user_id}))
        await lobby.LobbyView()._set_status(click, status)

    async def run():
        record = await create(cog, interaction, command, format)
        # Одновременные клики на последнее место не переполняют состав.
        await asyncio.gather(*(press(user_id, "in") for user_id in range(1, target + 3)))
        assert record["in"] == list(range(1, target + 1))
        assert record["waiting"] == record["sub"] == [target + 1, target + 2]
        await press(1, "in")
        await press(target + 1, "in")
        assert len(record["in"]) == target and record["waiting"] == [target + 1, target + 2]
        full = cog.build_embed(interaction.guild, record)
        assert f"**{target}/{target}**" in full.description
        assert "состав собран" in full.description and "клиенте LoL" in full.footer.text
        assert "Bo" not in full.title and "Классика" not in full.description
        await press(1, "out")
        assert target + 1 in record["in"] and record["waiting"] == [target + 2]
        assert record["out"] == [1]
        await press(target + 2, "sub")  # добровольный запас выходит из очереди
        await press(2, None)
        assert len(record["in"]) == target - 1 and record["waiting"] == []
        assert record["sub"] == [target + 2]
        assert f"**{target - 1}/{target}**" in cog.build_embed(interaction.guild, record).description
        assert JsonStore(cog.store.path).load()["456"] == record

        interaction.user.id = 1
        manage = lobby.ManageView(cog, interaction.message)
        labels = {item.label for item in manage.children}
        assert labels == {"Завершить сбор", "Отмена", "Правка состава"}
        interaction.user.id = 2
        assert not await manage.interaction_check(interaction)
        before = deepcopy(record)
        await manage.close_lobby.callback(interaction)
        assert record == before  # проверяются права и в самом callback
        interaction.user.id = 1
        await manage.close_lobby.callback(interaction)
        assert record["closed"] and record["finished"]
        assert all(item.disabled for item in lobby.LobbyView(record).children)
        assert cog.build_embed(interaction.guild, record).footer.text == "Сбор завершён"
        with pytest.raises(RosterError):
            cog.apply_move(interaction.guild, record, [3], "in")
        assert cog.latest_open_record(interaction.guild) is None
        assert JsonStore(cog.store.path).load()["456"]["closed"]

    asyncio.run(run())


@pytest.mark.parametrize("command,format,kind,target", FORMATS)
def test_existing_roster_ping_reminder_and_chat_use_new_format(setup_lobby, command, format, kind, target):
    cog, interaction = setup_lobby

    async def run():
        record = await create(cog, interaction, command, format)
        cog.apply_move(interaction.guild, record, [1, 2], "in")
        channel = NS(id=789, guild=interaction.guild, send=AsyncMock())
        interaction.client.get_channel = lambda channel_id: channel
        await cog.send_reminder("456", record)
        assert lobby.KIND_TITLES[kind] in channel.send.call_args.args[0]
        assert [m.id for m in channel.send.call_args.kwargs["allowed_mentions"].users] == [1, 2]
        await lobby.Lobby.ping.callback(cog, interaction)
        assert lobby.KIND_TITLES[kind] in interaction.response.send_message.call_args.args[0]
        await lobby.Lobby.roster.callback(cog, interaction)
        assert interaction.response.send_message.call_args.kwargs["embed"].title == lobby.KIND_TITLES[kind]
        chat = Chat.__new__(Chat)
        chat.bot = interaction.client
        summary = chat.lobby_summary(interaction.guild)
        assert lobby.KIND_TITLES[kind] in summary and f"2 из {target}" in summary
        assert LOBBY_TOPIC.search("ARAM Хаос") and LOBBY_TOPIC.search("флекс")

    asyncio.run(run())


@pytest.mark.parametrize("command,format,kind,target", FORMATS)
def test_new_formats_cannot_start_custom_draft_or_change_preferences(setup_lobby, command, format, kind, target):
    cog, interaction = setup_lobby

    async def run():
        record = await create(cog, interaction, command, format)
        cog.apply_move(interaction.guild, record, list(range(1, target + 1)), "in")
        before, preferences = deepcopy(record), deepcopy(cog.preferences.data)
        view = lobby.LobbyView()  # старый custom_id всё ещё зарегистрирован
        await view.launch.callback(interaction)
        await view.choose_mode.callback(interaction)
        await lobby.ModeView(cog, record, interaction.message).save.callback(interaction)
        await lobby.FinishView(cog, interaction.message).rebuild.callback(interaction)
        assert record == before and cog.preferences.data == preferences

    asyncio.run(run())


def test_legacy_custom_and_scrim_keep_sizes_modes_and_controls(setup_lobby):
    cog, interaction = setup_lobby

    async def run():
        for command, kind, size in (("custom", lobby.KIND_CUSTOM, 10), ("scrim", lobby.KIND_SCRIM, 5)):
            await getattr(lobby.Lobby, command).callback(cog, interaction, when="завтра 20:00")
            record = cog.get_record(456)
            assert record["target"] == size and record["kind"] == kind
            assert record["mode"] == modes.PRESET_BY_KEY["chaos"].mode
            view = lobby.LobbyView(record)
            ids = {item.custom_id for item in view.children}
            assert (lobby.CUSTOM_ID_MODE in ids) == (kind == lobby.KIND_CUSTOM)
            await getattr(lobby.Lobby, command).callback(cog, interaction, when="завтра 20:00", slots=8)
            assert cog.get_record(456)["target"] == 8

    asyncio.run(run())


def test_old_records_without_mode_or_kind_still_restore(setup_lobby):
    cog, interaction = setup_lobby

    async def run():
        await lobby.Lobby.custom.callback(cog, interaction, when="завтра 20:00")
        custom = cog.get_record(456)
        custom.pop("mode")
        custom.pop("series")
        custom["in"] = list(range(1, 11))
        scrim = deepcopy(custom)
        scrim.pop("kind")
        scrim["target"] = 5
        scrim["in"] = [1, 2, 3]
        cog.store.data["457"] = scrim
        await cog.save()
        before = deepcopy(cog.store.data)
        restored = lobby.Lobby(interaction.client)
        restored._background = AsyncMock()
        await restored.cog_load()
        await restored._refresh_task
        assert restored.store.data == before
        assert modes.get_mode(restored.get_record(456)) == modes.DEFAULT_PRESET.mode
        for key, is_custom in ((456, True), (457, False)):
            record = restored.get_record(key)
            embed = restored.build_embed(interaction.guild, record)
            ids = {item.custom_id for item in lobby.LobbyView(record).children}
            assert (lobby.CUSTOM_ID_CAPTAINS in ids) == is_custom
            assert ("Bo3" in embed.title) == is_custom
        await restored.cog_unload()

    asyncio.run(run())
