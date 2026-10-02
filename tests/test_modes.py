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


def _lane_pool(size=25):
    champions = []
    for lane in LANES:
        champions += [Champion(f"{lane}{n}", f"{lane.title()} {n}", frozenset({lane})) for n in range(size)]
    return champions


def test_recent_champions_do_not_come_back_in_the_next_deal(make_member):
    teams = [[make_member(i) for i in range(5)], [make_member(i) for i in range(5, 10)]]
    lanes = {i: LANES[i % 5] for i in range(10)}
    pool, history = _lane_pool(), {}
    previous: set[str] = set()
    for game in range(1, 30):
        dealt = modes.deal_champions(teams, lanes, modes.CHAMPS_CHOICE, pool, last_seen=history)
        names = {champion.id for picks in dealt.values() for champion in picks}
        assert not names & previous  # подряд одни и те же не выпадают
        for name in names:
            history[name] = float(game)
        previous = names


def test_lane_champions_do_not_repeat_for_several_games(make_member):
    teams = [[make_member(i) for i in range(5)], [make_member(i) for i in range(5, 10)]]
    lanes = {i: LANES[i % 5] for i in range(10)}
    pool, history = _lane_pool(), {}
    games: list[set[str]] = []
    for game in range(1, 41):
        dealt = modes.deal_champions(teams, lanes, modes.CHAMPS_RANDOM, pool, last_seen=history)
        names = {champion.id for picks in dealt.values() for champion in picks}
        for name in names:
            history[name] = float(game)
        games.append(names)
    # По 2 чемпиона линии за игру из 25: в любых 5 играх подряд — ни одного повтора.
    for start in range(len(games) - 4):
        window = [name for names in games[start:start + 5] for name in names]
        assert len(window) == len(set(window))


def test_skill_teams_balances_ratings(make_member):
    players = [make_member(i) for i in range(10)]
    # Равно делится, только если сильные (0.9) в разных командах: 0.9 + 4×0.5 = 2.9 с каждой стороны.
    ratings = {i: (0.9 if i < 2 else 0.5) for i in range(10)}
    for _ in range(50):
        teams = modes.skill_teams(players, lambda member: ratings[member.id])
        assert sorted(len(team) for team in teams) == [5, 5]
        sums = [sum(ratings[m.id] for m in team) for team in teams]
        assert abs(sums[0] - sums[1]) <= modes.SKILL_TOLERANCE + 1e-9
        assert all(sum(1 for m in team if m.id < 2) == 1 for team in teams)


def test_skill_teams_large_roster_uses_snake(make_member):
    players = [make_member(i) for i in range(16)]
    teams = modes.skill_teams(players, lambda member: member.id / 16)
    assert sorted(len(team) for team in teams) == [8, 8]


def test_order_by_lane(make_member):
    team = [make_member(1), make_member(2), make_member(3)]
    lanes = {1: "support", 3: "top"}
    assert [m.id for m in modes.order_by_lane(team, lanes)] == [3, 1, 2]


def test_draft_view_restores_turn(make_member):
    import asyncio
    from types import SimpleNamespace

    from cogs.scrim import DraftView

    captains = [make_member(1), make_member(2)]
    teams = [[captains[0], make_member(3)], [captains[1], make_member(4), make_member(5)]]
    pool = [make_member(6), make_member(7), make_member(8)]

    async def build():
        return DraftView(SimpleNamespace(last_teams={}), captains, pool, teams=teams, custom_id="x", timeout=None)

    view = asyncio.run(build())
    assert view.turn == 3
    # змейка 0,1,1,0,0,1: после трёх пиков ходит первый капитан
    assert view.current_captain.id == 1
    assert view.is_persistent()


def test_random_lanes_come_from_own_lanes(make_member):
    team = [make_member(1, "top"), make_member(2, "jungle"), make_member(3, "mid"),
            make_member(4, "adc"), make_member(5, "support")]
    for _ in range(30):
        lanes, _ok = modes.assign_lanes(team, modes.LANES_RANDOM)
        assert lanes == {1: "top", 2: "jungle", 3: "mid", 4: "adc", 5: "support"}


def test_same_single_lane_is_not_given_twice_in_a_team(make_member):
    team = [make_member(1, "mid"), make_member(2, "mid"), make_member(3, "top", "jungle")]
    for _ in range(30):
        lanes, _ok = modes.assign_lanes(team, modes.LANES_RANDOM)
        assert len(set(lanes.values())) == 3 and "mid" in (lanes[1], lanes[2])


def test_single_lane_does_not_repeat_in_the_next_game(make_member):
    team = [make_member(1, "mid"), make_member(2, "top", "mid", "adc")]
    for _ in range(30):
        lanes, _ok = modes.assign_lanes(team, modes.LANES_RANDOM, previous={1: "mid", 2: "top"})
        assert lanes[1] != "mid" and lanes[2] != "top"
        assert lanes[2] in ("mid", "adc")  # у второго ещё есть свои линии кроме прошлой


def test_deal_is_not_a_fixed_rotation(make_member):
    """После первого круга чемпионы не ходят одними и теми же группами."""
    teams = [[make_member(i) for i in range(5)], [make_member(i) for i in range(5, 10)]]
    lanes = {i: LANES[i % 5] for i in range(10)}
    pool = _lane_pool()
    runs = []
    for _ in range(2):
        history: dict[str, float] = {}
        deals = []
        for game in range(1, 16):
            dealt = modes.deal_champions(teams, lanes, modes.CHAMPS_RANDOM, pool, last_seen=history)
            names = frozenset(champion.id for picks in dealt.values() for champion in picks)
            for name in names:
                history[name] = float(game)
            deals.append(names)
        runs.append(deals)
    # Вторая половина (после того как все побывали) не повторяет первую.
    first, later = runs[0][:5], runs[0][10:15]
    assert first != later
    # Разные серии игр дают разные раздачи.
    assert runs[0] != runs[1]


def test_role_rates_add_lanes():
    from champions import parse_role_rates

    payload = {"data": {
        "266": {"TOP": {"playRate": 8.0}, "JUNGLE": {"playRate": 0.1}, "MIDDLE": {"playRate": 2.0}},
        "1": {"UTILITY": {"playRate": 0}},
        "bad": "x",
    }}
    assert parse_role_rates(payload) == {"266": {"top", "mid"}}
    assert parse_role_rates([]) == {} and parse_role_rates({"data": None}) == {}
