import asyncio

import pytest

import bets
import config
from champions import Champion
from cogs import lobby


def make(tmp_path):
    wallets = bets.Wallets(tmp_path / "wallet.json")
    wallets.load()
    return wallets


def test_settle_pari_mutuel():
    pool = {"1": {"side": 0, "amount": 100}, "2": {"side": 0, "amount": 300}, "3": {"side": 1, "amount": 200}}
    assert bets.settle(pool, 0) == {"1": 150, "2": 450}
    assert bets.settle(pool, 1) == {"3": 600}
    assert bets.settle({"1": {"side": 0, "amount": 100}}, 0) == {"1": 110}  # соперников нет — бонус
    assert bets.settle({"1": {"side": 1, "amount": 100}}, 0) == {}


def test_bet_flow_settle_undo_refund(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BET_MINUTES", 10)
    w = make(tmp_path)
    teams = [[10, 11], [20, 21]]
    w.open_pool(1, 555, 1, now=0)
    assert w.place(1, 555, 1, 99, 0, 200, teams, now=1) == bets.START_COINS - 200
    w.place(1, 555, 1, 98, 1, 100, teams, now=1)
    # игрок ставит только на своих
    with pytest.raises(bets.BetError):
        w.place(1, 555, 1, 10, 1, 100, teams, now=1)
    w.place(1, 555, 1, 10, 0, 100, teams, now=1)
    # переставить можно, денег не теряется
    w.place(1, 555, 1, 99, 0, 300, teams, now=2)
    assert w.balance(1, 99) == bets.START_COINS - 300
    with pytest.raises(bets.BetError):
        w.place(1, 555, 1, 99, 0, 5000, teams, now=2)
    with pytest.raises(bets.BetError):
        w.place(1, 555, 1, 97, 0, 100, teams, now=10_000)  # закрыто по времени
    payouts = w.settle_pool(1, 555, 1, 0)
    assert payouts == {"99": 375, "10": 125}
    assert w.balance(1, 99) == bets.START_COINS + 75
    w.unsettle_pool(1, 555, 1)
    assert w.balance(1, 99) == bets.START_COINS - 300
    # кастомку отменили — ставки вернулись
    assert w.refund_lobby(1, 555) == 3
    assert w.balance(1, 99) == bets.START_COINS and w.balance(1, 98) == bets.START_COINS
    assert w.claim_daily(1, 99, "2026-10-04") == bets.DAILY_BONUS
    assert w.claim_daily(1, 99, "2026-10-04") == 0
    assert w.richest(1)[0] == (99, bets.START_COINS + bets.DAILY_BONUS)


def test_new_series_gets_fresh_pools(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BET_MINUTES", 10)
    w = make(tmp_path)
    record = {"round": 1}
    assert lobby.bet_round(record) == 1  # старые сборы — прежние ключи
    w.open_pool(1, 555, lobby.bet_round(record), now=0)
    w.place(1, 555, lobby.bet_round(record), 99, 0, 100, [[1], [2]], now=1)
    w.settle_pool(1, 555, lobby.bet_round(record), 0)
    record["series_no"] = 1  # пересобрали составы — катка 1 новой серии
    assert lobby.bet_round(record) == "1.1"
    pool = w.open_pool(1, 555, lobby.bet_round(record), now=2)
    assert pool.get("settled") is None and not pool["bets"]
    assert lobby.bet_round(record, 3) == "1.3"


def test_abandoned_pools_refund(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BET_MINUTES", 10)
    w = make(tmp_path)
    w.open_pool(1, 555, 1, now=0)
    w.place(1, 555, 1, 99, 0, 300, [[1], [2]], now=1)
    w.open_pool(1, 556, 1, now=0)
    w.settle_pool(1, 556, 1, 0)
    assert w.expire_pools(1, now=3600) == 0
    assert w.balance(1, 99) == bets.START_COINS - 300
    assert w.expire_pools(1, now=bets.STALE_POOL_SECONDS + 1000) == 1
    assert w.balance(1, 99) == bets.START_COINS
    assert w.pool(1, 555, 1) is None and w.pool(1, 556, 1) is not None
    w.expire_pools(1, now=bets.KEEP_SETTLED_SECONDS + 1000)
    assert w.pool(1, 556, 1) is None


def test_fearless_bans_previous_rounds_only(monkeypatch):
    monkeypatch.setattr(config, "FEARLESS_DRAFT", True)
    record = {"round": 1}
    champs = lambda *names: {1: [Champion(n, n, frozenset({"mid"})) for n in names]}
    lobby.remember_round_champions(record, champs("Ари", "Зед"))
    assert lobby.fearless_used(record) == set()  # катка ещё идёт
    lobby.remember_round_champions(record, champs("Люкс"))  # новая раздача той же катки — заменяет
    record["round"] = 2
    assert lobby.fearless_used(record) == {"Люкс"}
    lobby.remember_round_champions(record, champs("Синдра"))
    record["round"] = 3
    assert lobby.fearless_used(record) == {"Люкс", "Синдра"}
    lobby.reset_series(record)
    assert lobby.fearless_used(record) == set()


def test_deal_for_respects_banned(monkeypatch):
    from types import SimpleNamespace as NS

    pool = [Champion(f"c{i}", f"Ч{i}", frozenset({"mid"})) for i in range(80)]

    async def all_champions():
        return pool

    monkeypatch.setattr(lobby.POOL, "all", all_champions)
    cog = lobby.Lobby.__new__(lobby.Lobby)

    class History:
        data: dict = {}

        async def save(self):
            pass

    cog.champion_history = History()
    import modes
    guild = NS(id=1)
    teams = [[NS(id=i, guild=guild) for i in range(5)], [NS(id=i, guild=guild) for i in range(5, 10)]]
    banned = {f"Ч{i}" for i in range(70)}
    dealt, _ = asyncio.run(cog.deal_for(teams, {}, {"champs": modes.CHAMPS_RANDOM}, banned=banned))
    names = {champion.name for picks in dealt.values() for champion in picks}
    assert names and not names & banned


def test_duels_resolve_undo_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DUEL_HOURS", 24)
    w = make(tmp_path)
    start = bets.START_COINS
    w.start_duel(1, 10, 20, 300, now=0)
    assert w.balance(1, 10) == start - 300 and w.balance(1, 20) == start - 300
    with pytest.raises(bets.BetError):
        w.start_duel(1, 20, 10, 100, now=1)  # между ними уже дуэль
    with pytest.raises(bets.BetError):
        w.start_duel(1, 10, 30, 5000, now=1)  # не хватает
    # в одной команде — дуэль ждёт
    assert w.resolve_duels(1, 7, [10, 20], [30, 40]) == []
    resolved = w.resolve_duels(1, 8, [20, 30], [10, 40])
    assert [d["winner"] for d in resolved] == [20]
    assert w.balance(1, 20) == start + 300 and w.balance(1, 10) == start - 300
    assert w.resolve_duels(1, 9, [10], [20]) == []  # уже решена
    # отмена результата — дуэль снова ждёт
    assert w.unresolve_duels(1, 8) == 1
    assert w.balance(1, 20) == start - 300 and len(w.active_duels(1, 10)) == 1
    # не встретились — коины назад
    assert len(w.expire_duels(1, now=25 * 3600)) == 1
    assert w.balance(1, 10) == start and w.balance(1, 20) == start
    assert w.active_duels(1) == []


def test_prediction_lines():
    assert "монетка" in lobby.prediction_line(0.52)
    assert "синие — 70%" in lobby.prediction_line(0.7)
    assert "красные — 70%" in lobby.prediction_line(0.3)
    assert "Апсет" in lobby.result_comment(0.3)
    assert "предсказывала" in lobby.result_comment(0.7)
    assert lobby.result_comment(0.5) is None


def test_win_chance_by_elo(tmp_path):
    from stats import Stats

    stats = Stats(tmp_path / "stats.json")
    stats.load()
    assert lobby.win_chance(stats, 1, [[1, 2], [3, 4]]) == pytest.approx(0.5)
    asyncio.run(stats.record_game(1, [1, 2], [3, 4]))
    assert lobby.win_chance(stats, 1, [[1, 2], [3, 4]]) > 0.5


def test_report_result_pays_bets_bounty_duels_and_undo(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from cogs.stats import StatsCog
    from stats import Stats

    monkeypatch.setattr(config, "BET_MINUTES", 10)
    monkeypatch.setattr(config, "BOUNTY_COINS", 100)
    monkeypatch.setattr(config, "MVP_VOTE_MINUTES", 0)

    stats_cog = StatsCog.__new__(StatsCog)
    stats_cog.stats = Stats(tmp_path / "stats.json")
    stats_cog.stats.load()
    stats_cog.wallets = make(tmp_path)
    # лидер таблицы — 5 (обыграл всех трижды), остальные набрали по 3 катки
    for _ in range(3):
        asyncio.run(stats_cog.stats.record_game(1, [5, 6], [1, 2]))
        asyncio.run(stats_cog.stats.record_game(1, [3, 4], [7, 8]))
    lobby_cog = lobby.Lobby.__new__(lobby.Lobby)
    lobby_cog.store = NS(data={})

    async def save():
        pass

    lobby_cog.save = save
    lobby_cog.build_embed = lambda guild, record: None
    cogs = {"Lobby": lobby_cog, "Stats": stats_cog}
    bot = NS(get_cog=cogs.get, get_channel=lambda channel_id: None)
    lobby_cog.bot = bot

    record = {"author_id": 1, "teams": [[1, 2], [5, 6]], "round": 1, "results": [], "kind": "custom",
              "channel_id": 9, "series": 3, "stage": lobby.STAGE_DONE,
              lobby.STATUS_IN: [1, 2, 5, 6], lobby.STATUS_SUB: [], lobby.STATUS_OUT: []}
    lobby_cog.store.data["777"] = record
    guild = NS(id=1, get_member=lambda user_id: None)
    w = stats_cog.wallets
    w.open_pool(1, 777, lobby.bet_round(record))
    w.place(1, 777, 1, 99, 0, 200, record["teams"])
    w.start_duel(1, 2, 6, 150)

    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))
        return NS(content=args[0] if args else "", edit=edit)

    async def edit(**kwargs):
        pass

    async def edit_message(**kwargs):
        pass

    interaction = NS(
        client=bot, guild=guild, user=NS(id=1, guild_permissions=NS(manage_events=False)),
        message=NS(id=777, guild=guild, edit=edit), channel_id=9, channel=None,
        response=NS(edit_message=edit_message), followup=NS(send=followup_send),
    )

    async def scenario():
        await lobby.report_result(interaction, 777, 0)  # синие (1, 2) свалили лидера 5
        announce = sent[0][0][0]
        assert "Апсет" in announce and "Охота на голову" in announce and "Дуэль" in announce
        assert "Ставки сыграли" in announce
        assert w.balance(1, 1) == bets.START_COINS + 100
        assert w.balance(1, 2) == bets.START_COINS + 100 + 150
        assert w.balance(1, 99) == bets.START_COINS + 20  # соперников по ставке нет — +10%
        assert record["round"] == 2
        undo = sent[1][1]["view"]
        button = next(item for item in undo.children)
        await button.callback(NS(client=bot, guild=guild, response=NS(edit_message=edit_message)))
        assert record["round"] == 1 and record["results"] == []
        assert w.balance(1, 1) == bets.START_COINS and w.balance(1, 99) == bets.START_COINS - 200
        assert w.balance(1, 2) == bets.START_COINS - 150 and len(w.active_duels(1)) == 1

    asyncio.run(scenario())
