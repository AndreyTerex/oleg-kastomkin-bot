from collections import Counter

import modes
from champions import Champion
from cogs.scrim import draft_order
from utils import split_by_lanes

LANES = ("top", "jungle", "mid", "adc", "support")


def test_draft_order_is_snake():
    assert draft_order(8) == [0, 1, 1, 0, 0, 1, 1, 0]


def test_get_mode_falls_back_to_classic_and_returns_copy():
    record = {"mode": {"teams": "nonsense", "lanes": modes.LANES_RANDOM}}
    mode = modes.get_mode(record)
    assert mode == {"teams": modes.TEAMS_DRAFT, "lanes": modes.LANES_RANDOM, "champs": modes.CHAMPS_FREE}
    mode["teams"] = modes.TEAMS_RANDOM
    assert modes.get_mode(record)["teams"] == modes.TEAMS_DRAFT


def test_normalize_fixes_incompatible_settings():
    mode, notes = modes.normalize({"teams": modes.TEAMS_BALANCED, "lanes": modes.LANES_FREE, "champs": modes.CHAMPS_MIRROR})
    assert mode["lanes"] == modes.LANES_MAIN
    assert notes


def test_assign_lanes_main_respects_marked_lanes(make_member):
    team = [make_member(i, lane) for i, lane in enumerate(LANES)]
    lanes, ok = modes.assign_lanes(team, modes.LANES_MAIN)
    assert ok
    assert lanes == {i: lane for i, lane in enumerate(LANES)}


def test_assign_lanes_offrole_avoids_marked_lanes(make_member):
    team = [make_member(i, lane) for i, lane in enumerate(LANES)]
    lanes, ok = modes.assign_lanes(team, modes.LANES_OFFROLE)
    assert ok
    assert all(lanes[i] != lane for i, lane in enumerate(LANES))
    assert sorted(lanes.values()) == sorted(LANES)


def test_split_by_lanes_builds_full_rosters(make_member):
    players = [make_member(i, LANES[i % 5]) for i in range(10)]
    blue, red = split_by_lanes(players)
    for roster in (blue, red):
        assert [lane.key for lane, _ in roster] == list(LANES)
    assert len({member.id for _, member in blue + red}) == 10


def test_split_by_lanes_impossible_returns_none(make_member):
    players = [make_member(i, "mid") for i in range(10)]
    assert split_by_lanes(players) is None


def _pool():
    champions = []
    for lane in LANES:
        champions += [Champion(f"{lane}{n}", f"{lane.title()} {n}", frozenset({lane})) for n in range(10)]
    return champions


def test_deal_champions_no_repeats_and_fitting_lanes(make_member):
    teams = [[make_member(i) for i in range(5)], [make_member(i) for i in range(5, 10)]]
    lanes = {i: LANES[i % 5] for i in range(10)}
    dealt = modes.deal_champions(teams, lanes, modes.CHAMPS_CHOICE, _pool())
    names = [champion.id for picks in dealt.values() for champion in picks]
    assert len(names) == len(set(names)) == 30
    for user_id, picks in dealt.items():
        assert all(lanes[user_id] in champion.lanes for champion in picks)


def test_deal_champions_mirror_gives_same_champion_per_lane(make_member):
    teams = [[make_member(i) for i in range(5)], [make_member(i) for i in range(5, 10)]]
    lanes = {i: LANES[i % 5] for i in range(10)}
    dealt = modes.deal_champions(teams, lanes, modes.CHAMPS_MIRROR, _pool())
    for i in range(5):
        assert dealt[i] == dealt[i + 5]
    assert len(Counter(dealt[i][0].id for i in range(5))) == 5
