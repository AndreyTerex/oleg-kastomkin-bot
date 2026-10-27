import asyncio
import random
from collections import Counter

import captains


def test_last_pair_rests_and_frequent_captains_drop():
    rng = random.Random(1)
    players = list(range(1, 11))
    recent = [{"lobby": 1, "pair": [1, 2]}]
    for _ in range(200):
        assert not {1, 2} & set(captains.pick(players, recent, rng))
    # 3 и 4 капитанили много раз — выпадают заметно реже остальных
    recent = [{"lobby": n, "pair": [3, 4]} for n in range(5)] + [{"lobby": 9, "pair": [9, 10]}]
    counts = Counter(user for _ in range(2000) for user in captains.pick(players, recent, rng))
    assert counts[3] * 5 < counts[5]
    assert counts[9] == counts[10] == 0


def test_small_roster_still_picks_two():
    rng = random.Random(2)
    pair = captains.pick([1, 2, 3], [{"lobby": 1, "pair": [1, 2]}], rng)
    assert len(set(pair)) == 2


def test_redraw_in_same_lobby_replaces_entry(tmp_path):
    history = captains.CaptainHistory(tmp_path / "captains.json")
    history.load()
    players = list(range(1, 11))
    first = asyncio.run(history.draw(1, 555, players))
    second = asyncio.run(history.draw(1, 555, players))
    assert not set(first) & set(second)  # перебросили — другая пара
    assert [entry["lobby"] for entry in history.recent(1)] == [555]
    for lobby in range(30):
        asyncio.run(history.draw(1, lobby, players))
    assert len(history.recent(1)) == captains.HISTORY


def test_reroll_avoids_own_pair_even_after_newer_draw(tmp_path):
    history = captains.CaptainHistory(tmp_path / "captains.json")
    history.load()
    history.recent(1)[:] = [{"lobby": 555, "pair": [1, 2]}, {"lobby": 777, "pair": [3, 4]}]
    for _ in range(20):
        pair = asyncio.run(history.draw(1, 555, [1, 2, 3, 4]))
        assert set(pair) != {1, 2} and len(set(pair)) == 2
        history.recent(1)[:] = [{"lobby": 555, "pair": [1, 2]}, {"lobby": 777, "pair": [3, 4]}]
