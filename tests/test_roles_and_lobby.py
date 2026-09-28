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
    assert lobby.progress_bar(3, 5) == "🟩🟩🟩⬜⬜"
    assert lobby.progress_bar(10, 20) == "🟩" * 5 + "⬜" * 5
    assert lobby.progress_bar(0, 0) == ""
    assert lobby.progress_bar(4, 10, subs=2) == "🟩" * 4 + "⬜" * 6 + "🟨" * 2
    assert lobby.progress_bar(5, 5, subs=9) == "🟩" * 5 + "🟨" * lobby.SUB_CELLS_LIMIT
    assert lobby.progress_bar(10, 20, subs=1) == "🟩" * 5 + "⬜" * 5 + "🟨"


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
