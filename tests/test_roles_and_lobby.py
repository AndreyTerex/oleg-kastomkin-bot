import asyncio
from types import SimpleNamespace

import config
import role_requests
from cogs import lobby
from tests.conftest import FakeMember, FakeRole


def test_lane_aliases():
    assert role_requests._lane_keys("Олег, дай мне роль мида") == ["mid"]
    assert role_requests._lane_keys("сними лес и сапа") == ["jungle"]
    assert role_requests._lane_keys("дай стрелка и саппорт") == ["adc", "support"]
    assert role_requests._lane_keys("я гулял в лесу") == []


def test_author_can_manage_only_below_top_role():
    guild = SimpleNamespace(owner_id=1)
    author = SimpleNamespace(id=2, guild=guild, top_role=FakeRole(10, position=5))
    assert role_requests.author_can_manage(FakeRole(11, position=4), author)
    assert not role_requests.author_can_manage(FakeRole(12, position=5), author)
    assert not role_requests.author_can_manage(FakeRole(13, position=9), author)
    owner = SimpleNamespace(id=1, guild=guild, top_role=FakeRole(10, position=1))
    assert role_requests.author_can_manage(FakeRole(13, position=9), owner)


def test_progress_bar():
    assert lobby.progress_bar(3, 5) == "▰▰▰▱▱"
    assert lobby.progress_bar(10, 20) == "▰" * 5 + "▱" * 5
    assert lobby.progress_bar(0, 0) == ""
    assert lobby.progress_bar(1, 40) == "▰" + "▱" * 9  # один записавшийся всё равно виден
    assert lobby.progress_bar(12, 10) == "▰" * 10


def test_fill_line_and_color():
    assert lobby.fill_line(6, 10, subs=2) == "`▰▰▰▰▰▰▱▱▱▱` **6/10** · не хватает 4 · в запасе 2"
    assert lobby.fill_line(10, 10) == "`▰▰▰▰▰▰▰▰▰▰` **10/10** · состав собран"
    assert lobby.fill_color(3, 10) == lobby.COLOR_FILLING
    assert lobby.fill_color(7, 10) == lobby.COLOR_ALMOST
    assert lobby.fill_color(10, 10) == lobby.COLOR_READY


def test_slots_show_free_places_in_two_columns():
    from types import SimpleNamespace as NS

    people = [NS(id=i, mention=f"<@{i}>", roles=[]) for i in range(7)]
    columns = lobby.slot_columns(people, 10, "✅ Основной состав")
    assert [name for name, _ in columns] == ["✅ Основной состав", "\u200b"]
    left, right = columns[0][1].splitlines(), columns[1][1].splitlines()
    assert len(left) == len(right) == 5
    assert left[0].startswith("` 1` <@0>") and right[1].startswith("` 7` <@6>")
    assert right[2] == "` 8` ◦ *свободно*" and right[-1] == "`10` ◦ *свободно*"
    assert len(lobby.slot_columns(people[:2], 5, "x")) == 1


def _record(**extra):
    record = {
        "kind": lobby.KIND_CUSTOM, "target": 3, "stage": lobby.STAGE_SIGNUP, "teams": None, "captains": None,
        "closed": False, lobby.STATUS_IN: [1, 2], lobby.STATUS_SUB: [9, 3], lobby.STATUS_OUT: [],
    }
    record.update(extra)
    return record


class FakeGuild:
    def __init__(self, ids):
        self.members = {i: FakeMember(i) for i in ids}

    def get_member(self, user_id):
        return self.members.get(user_id)


def test_promote_sub_skips_people_who_left():
    cog = lobby.Lobby.__new__(lobby.Lobby)
    record = _record(waiting=[9, 3])
    promoted = cog.promote_sub(FakeGuild([1, 2, 3]), record)  # 9 ушёл с сервера
    assert promoted.id == 3
    assert record[lobby.STATUS_IN] == [1, 2, 3]
    assert record[lobby.STATUS_SUB] == [9]
    assert record["waiting"] == [9]


def test_promote_sub_ignores_voluntary_subs():
    cog = lobby.Lobby.__new__(lobby.Lobby)
    # 9 и 3 сами выбрали «Запасной» — очереди нет, в состав их не тянем.
    record = _record()
    assert cog.promote_sub(FakeGuild([1, 2, 3, 9]), record) is None
    assert record[lobby.STATUS_SUB] == [9, 3]


def test_set_waiting_keeps_order_and_resets():
    record = {}
    lobby.set_waiting(record, 5, True)
    lobby.set_waiting(record, 6, True)
    lobby.set_waiting(record, 5, False)
    lobby.set_waiting(record, 7, True)
    assert record["waiting"] == [6, 7]


def test_promote_sub_not_after_teams_split():
    cog = lobby.Lobby.__new__(lobby.Lobby)
    record = _record(stage=lobby.STAGE_DONE, teams=[[1], [2]])
    assert cog.promote_sub(FakeGuild([1, 2, 3, 9]), record) is None
    assert record[lobby.STATUS_IN] == [1, 2]


def test_scrim_lobby_hides_custom_game_buttons():
    async def build(kind):
        view = lobby.LobbyView(_record(kind=kind))
        return {getattr(item, "custom_id", None) for item in view.children}

    scrim_ids = asyncio.run(build(lobby.KIND_SCRIM))
    custom_ids = asyncio.run(build(lobby.KIND_CUSTOM))
    assert not scrim_ids & lobby.CUSTOM_GAME_ONLY
    assert lobby.CUSTOM_ID_CAPTAINS in custom_ids and lobby.CUSTOM_ID_MODE in custom_ids


def test_team_size_config():
    assert config.TEAM_SIZE == 5


def _bare_lobby(records):
    cog = lobby.Lobby.__new__(lobby.Lobby)
    cog.store = SimpleNamespace(data=records)
    cog.drafts = {}
    sent, saved = [], []

    async def send_reminder(message_id, record):
        sent.append(message_id)

    async def save():
        saved.append(True)

    cog.send_reminder = send_reminder
    cog.save = save
    return cog, sent, saved


def test_remind_due_fires_once_in_window(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(lobby.time, "time", lambda: now)
    monkeypatch.setattr(config, "REMIND_MINUTES", 15)
    records = {
        "soon": {"start_at": now + 10 * 60, "created_at": now - 3600, "reminded": False},
        "later": {"start_at": now + 60 * 60, "created_at": now - 3600, "reminded": False},
        "missed": {"start_at": now - 60, "created_at": now - 3600, "reminded": False},
        "just_created": {"start_at": now + 5 * 60, "created_at": now - 60, "reminded": False},
        "closed": {"start_at": now + 5 * 60, "created_at": now - 3600, "reminded": False, "closed": True},
    }
    cog, sent, saved = _bare_lobby(records)
    asyncio.run(cog.remind_due())
    assert sent == ["soon"]
    assert records["soon"]["reminded"] and records["missed"]["reminded"] and records["just_created"]["reminded"]
    assert not records["later"]["reminded"] and not records["closed"]["reminded"]
    asyncio.run(cog.remind_due())
    assert sent == ["soon"]


def test_expire_drafts_picks_stale_and_closed(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(lobby.time, "time", lambda: now)
    records = {
        "1": {"stage": lobby.STAGE_DRAFT, "draft": {"expires_at": now - 1}},
        "2": {"stage": lobby.STAGE_DRAFT, "draft": {"expires_at": now + 100}},
        "3": {"stage": lobby.STAGE_DRAFT, "closed": True, "draft": {"expires_at": now + 100}},
        "4": {"stage": lobby.STAGE_DONE},
    }
    cog, _sent, _saved = _bare_lobby(records)
    expired = []

    async def expire_draft(message_id, record):
        expired.append(message_id)

    cog.expire_draft = expire_draft
    asyncio.run(cog.expire_drafts())
    assert expired == ["1", "3"]


def test_series_score_and_winner():
    record = _record(stage=lobby.STAGE_DONE, teams=[[1], [2]], series=3,
                     results=[{"winner": 0}, {"winner": 1}])
    assert lobby.Lobby.score_line(record) == "📊 **Серия Bo3:** 🔵 1 : 1 🔴"
    assert lobby.series_winner(record) is None
    record["results"].append({"winner": 0})
    assert lobby.series_winner(record) == 0
    assert lobby.series_winner(record, drop_last=True) is None
    assert lobby.Lobby.score_line(record).endswith("серия за 🔵 синими")
    assert lobby.series_winner({"series": 1, "results": [{"winner": 1}]}) == 1
    assert lobby.series_winner({"results": [{"winner": 0}]}) is None  # старые сборы — Bo3


def test_result_buttons_stay_open_between_games():
    async def buttons(rec):
        view = lobby.LobbyView(rec)
        return {getattr(item, "custom_id", None): item.disabled for item in view.children}

    record = _record(stage=lobby.STAGE_DONE, teams=[[1], [2]], round=2, results=[{"winner": 0}])
    states = asyncio.run(buttons(record))
    assert not states[lobby.CUSTOM_ID_WIN_BLUE] and not states[lobby.CUSTOM_ID_WIN_RED]
    # До деления на команды кнопок побед в посте нет вовсе.
    states = asyncio.run(buttons(_record()))
    assert lobby.CUSTOM_ID_WIN_BLUE not in states


def test_restart_skips_posts_that_already_look_right():
    import asyncio
    from types import SimpleNamespace as NS

    import discord

    async def build():
        record = {"kind": "custom", "target": 10, "time": "20:00", "captains": [], "teams": [],
                  lobby.STATUS_IN: [], lobby.STATUS_SUB: [], lobby.STATUS_OUT: []}
        cog = lobby.Lobby.__new__(lobby.Lobby)
        guild = NS(get_member=lambda i: None)
        embed, view = cog.build_embed(guild, record), lobby.LobbyView(record)
        # Так пост возвращается от Discord: эмбед и кнопки заново собраны из JSON.
        shown = NS(
            embeds=[discord.Embed.from_dict(embed.to_dict())],
            components=[discord.components.ActionRow(row) for row in view.to_components()],
        )
        assert lobby.same_render(shown, embed, view)
        record[lobby.STATUS_IN].append(1)
        changed = cog.build_embed(NS(get_member=lambda i: NS(id=1, mention="<@1>", roles=[])), record)
        assert not lobby.same_render(shown, changed, view)

    asyncio.run(build())


def test_split_takes_teams_from_lobby_when_newer():
    from types import SimpleNamespace as NS

    from cogs.scrim import Scrim

    people = {i: NS(id=i) for i in range(1, 5)}
    guild = NS(id=1, get_member=lambda i: people.get(i))
    record = {"teams": [[1, 2], [3, 4]], "teams_at": 200.0, "created_at": 100.0}
    lobby = NS(
        latest_open_record=lambda g: ("55", record),
        members_from_ids=lambda g, ids: [people[i] for i in ids],
    )
    cog = Scrim.__new__(Scrim)
    cog.bot = NS(get_cog=lambda name: lobby)
    cog.last_teams, cog.last_teams_at = {}, {}
    blue, red = cog.current_teams(guild)
    assert [m.id for m in blue] == [1, 2] and [m.id for m in red] == [3, 4]
    # /teams позже поста сбора — берём его
    cog.last_teams[1] = ([people[4]], [people[1]])
    cog.last_teams_at[1] = 300.0
    assert cog.current_teams(guild) == ([people[4]], [people[1]])
    # сбора нет — только /teams
    lobby.latest_open_record = lambda g: None
    assert cog.current_teams(guild) == ([people[4]], [people[1]])


def test_deal_is_posted_via_interaction_and_explains_missing_rights():
    import asyncio
    from types import SimpleNamespace as NS

    import discord

    from cogs.lobby import Lobby

    sent, warned = [], []

    async def followup_send(*args, **kwargs):
        if kwargs.get("ephemeral"):
            warned.append(args[0])
            return
        if fail:
            raise discord.Forbidden(NS(status=403, reason="x"), "Missing Permissions")
        sent.append(kwargs)

    async def channel_send(**kwargs):
        raise AssertionError("в канал напрямую писать не должны, раз есть нажатие")

    interaction = NS(followup=NS(send=followup_send), is_expired=lambda: False)
    cog = Lobby.__new__(Lobby)
    fail = False
    asyncio.run(cog.post_deal(NS(send=channel_send), {"embed": "e"}, interaction, "раздачу"))
    assert sent == [{"embed": "e"}] and not warned
    fail = True
    asyncio.run(cog.post_deal(NS(send=channel_send), {"embed": "e"}, interaction, "раздачу"))
    assert warned and "Прикреплять файлы" in warned[0]
