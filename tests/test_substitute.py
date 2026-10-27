import pytest

import bets
import config
from lobby_state import RosterError, substitute


def game_record():
    return {
        "stage": "done", "target": 4, "in": [1, 2, 3, 4], "sub": [5], "out": [6], "waiting": [5],
        "teams": [[1, 2], [3, 4]], "captains": [1, 3],
        "deal": {"lanes": {"1": "top", "2": "mid", "3": "top", "4": "mid"},
                 "champions": {"1": ["Garen"], "3": ["Darius"]}},
        "results": [{"round": 1, "winner": 0, "game": 7}], "round": 2,
    }


def test_substitute_takes_place_lane_champions_and_captaincy():
    record = game_record()
    assert substitute(record, 1, 5) == 0
    assert record["teams"] == [[5, 2], [3, 4]]
    assert record["deal"]["lanes"]["5"] == "top" and "1" not in record["deal"]["lanes"]
    assert record["deal"]["champions"]["5"] == ["Garen"]
    assert record["captains"] == [5, 3]
    assert record["in"] == [5, 2, 3, 4] and record["sub"] == [1] and record["waiting"] == []
    # серия и катки не тронуты
    assert record["round"] == 2 and record["results"] == [{"round": 1, "winner": 0, "game": 7}]
    # тот, кто «не сможет», тоже может зайти
    substitute(record, 4, 6)
    assert record["teams"][1] == [3, 6] and 6 not in record["out"] and 4 in record["sub"]


def test_substitute_replaces_team_lists_for_in_flight_result():
    record = game_record()
    winners, losers = record["teams"]
    substitute(record, 1, 5)
    # запись результата, начатая до замены, видит, что составы уже другие
    assert record["teams"] != [winners, losers] and winners == [1, 2]


def test_captain_history_follows_substitute(tmp_path):
    import captains

    history = captains.CaptainHistory(tmp_path / "captains.json")
    history.load()
    history.recent(1)[:] = [{"lobby": 555, "pair": [1, 3]}, {"lobby": 777, "pair": [2, 4]}]
    assert history.replace(1, 555, 1, 5)
    assert history.recent(1)[0]["pair"] == [5, 3] and history.recent(1)[1]["pair"] == [2, 4]
    assert not history.replace(1, 555, 2, 6)  # 2 не капитанил в этом сборе


def test_substitute_rejects_bad_cases():
    record = game_record()
    with pytest.raises(RosterError):
        substitute(record, 1, 3)  # уже играет за другую команду
    with pytest.raises(RosterError):
        substitute(record, 9, 5)  # такого в командах нет
    with pytest.raises(RosterError):
        substitute(record, 1, 1)
    for stage in ("signup", "draft"):
        with pytest.raises(RosterError):
            substitute({**game_record(), "stage": stage}, 1, 5)
    with pytest.raises(RosterError):
        substitute({**game_record(), "closed": True}, 1, 5)
    assert record == game_record()


def test_newcomer_bet_on_rivals_is_refunded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BET_MINUTES", 10)
    w = bets.Wallets(tmp_path / "wallet.json")
    w.load()
    w.open_pool(1, 555, 2)
    w.place(1, 555, 2, 5, 1, 300, [[1, 2], [3, 4]])  # пока был зрителем — ставил на красных
    w.place(1, 555, 2, 6, 0, 100, [[1, 2], [3, 4]])
    assert w.refund_wrong_side(1, 555, 2, 5, 0) == 300  # зашёл за синих
    assert w.balance(1, 5) == bets.START_COINS and "5" not in w.pool(1, 555, 2)["bets"]
    assert w.refund_wrong_side(1, 555, 2, 6, 0) == 0  # ставка на своих остаётся
    assert w.refund_wrong_side(1, 555, 9, 6, 1) == 0  # пула нет


def test_substitute_button_end_to_end(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS

    from cogs import lobby
    from cogs.stats import StatsCog
    from storage import JsonStore

    monkeypatch.setattr(config, "BET_MINUTES", 10)
    record = {**game_record(), "author_id": 1, "kind": "custom", "channel_id": 9, "closed": False}
    cog = lobby.Lobby.__new__(lobby.Lobby)
    cog.store = JsonStore(tmp_path / "lobbies.json")
    cog.store.load()
    cog.store.data["555"] = record

    async def save():
        await cog.store.save()

    cog.save = save
    cog.build_embed = lambda guild, rec: None
    import captains

    cog.captain_history = captains.CaptainHistory(tmp_path / "captains.json")
    cog.captain_history.load()
    cog.captain_history.recent(1).append({"lobby": 555, "pair": [1, 3]})
    stats_cog = StatsCog.__new__(StatsCog)
    stats_cog.wallets = bets.Wallets(tmp_path / "wallet.json")
    stats_cog.wallets.load()
    stats_cog.wallets.open_pool(1, 555, lobby.bet_round(record))
    stats_cog.wallets.place(1, 555, lobby.bet_round(record), 5, 1, 200, record["teams"])

    members = {i: NS(id=i, bot=False, display_name=f"p{i}", name=f"p{i}", global_name=None, nick=None,
                     mention=f"<@{i}>") for i in range(1, 7)}
    guild = NS(id=1, get_member=members.get)
    sent, edits = [], []

    async def edit(**kwargs):
        edits.append(kwargs)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    lobby_message = NS(id=555, guild=guild, edit=edit)
    client = NS(get_cog={"Stats": stats_cog, "Lobby": cog}.get)
    interaction = NS(client=client, guild=guild, user=NS(id=1, guild_permissions=NS(manage_events=False)),
                     response=NS(edit_message=edit), followup=NS(send=followup_send))

    async def scenario():
        view = lobby.SubstituteView(cog, record, lobby_message)
        view.leaving._values = ["1"]
        view.coming._values = [members[5]]
        await view.confirm.callback(interaction)

    asyncio.run(scenario())
    assert record["teams"] == [[5, 2], [3, 4]] and record["captains"] == [5, 3]
    assert "Замена" in sent[0] and "Garen" in sent[0] and "возвращена" in sent[0]
    assert stats_cog.wallets.balance(1, 5) == bets.START_COINS
    assert JsonStore(tmp_path / "captains.json").load()["1"][0]["pair"] == [5, 3]
    assert JsonStore(tmp_path / "lobbies.json").load()["555"]["teams"] == [[5, 2], [3, 4]]  # сохранено на диск
