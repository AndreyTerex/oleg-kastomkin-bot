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
