import asyncio
import io

from PIL import Image

import portraits
from stats import ELO_START, Record, Stats, elo_changes, plural, week_highlights, week_lines


def test_record_rating_is_smoothed():
    assert Record().rating == 0.5
    assert Record(1, 0).rating == 2 / 3
    assert Record(7, 3).winrate == 0.7
    assert Record(7, 3).describe() == "10 каток, 7 побед (70%), Elo 1000"
    assert Record(7, 3, 1084.4, 2).describe() == "10 каток, 7 побед (70%), Elo 1084, MVP ×2"
    assert plural(21, "катка", "катки", "каток") == "катка"
    assert plural(3, "катка", "катки", "каток") == "катки"


def test_record_and_undo_game(tmp_path):
    stats = Stats(tmp_path / "stats.json")
    stats.load()

    async def scenario():
        game = await stats.record_game(1, [10, 11], [20, 21], lobby_id=5)
        await stats.record_game(1, [20], [10])
        assert stats.player(1, 10) == Record(1, 1)
        assert stats.player(1, 20) == Record(1, 1)
        assert await stats.undo_game(1, game)
        assert stats.player(1, 10) == Record(0, 1)
        assert stats.player(1, 11) == Record(0, 0)
        assert not await stats.undo_game(1, game)
        assert stats.games_count(1) == 1

    asyncio.run(scenario())
    reloaded = Stats(tmp_path / "stats.json")
    reloaded.load()
    assert reloaded.player(1, 20) == Record(1, 0)


def test_elo_changes_and_undo_restore(tmp_path):
    even = elo_changes({}, [1, 2], [3, 4])
    assert even == {1: 20.0, 2: 20.0, 3: -20.0, 4: -20.0}
    # фаворит за победу получает меньше, андердог — больше
    strong = {1: (1200.0, 30), 2: (800.0, 30)}
    assert elo_changes(strong, [1], [2])[1] < elo_changes(strong, [2], [1])[2]

    stats = Stats(tmp_path / "stats.json")
    stats.load()

    async def scenario():
        game = await stats.record_game(1, [10], [20], channel_id=77)
        assert stats.player(1, 10).elo == ELO_START + 20
        assert stats.player(1, 20).elo == ELO_START - 20
        assert stats.last_channel(1) == 77
        second = await stats.record_game(1, [10], [20])
        assert await stats.set_mvp(1, second, [10])
        assert not await stats.set_mvp(1, second, [20])
        assert stats.player(1, 10).mvp == 1
        assert await stats.undo_game(1, second)
        assert stats.player(1, 10).elo == ELO_START + 20 and stats.player(1, 10).mvp == 0
        assert await stats.undo_game(1, game)
        assert stats.player(1, 10).elo == ELO_START
        assert not await stats.set_mvp(1, game, [10])

    asyncio.run(scenario())


def test_elo_is_replayed_from_old_history(tmp_path):
    import json

    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"1": {
        "players": {"10": {"wins": 1, "losses": 0}, "20": {"wins": 0, "losses": 1}},
        "games": [{"id": 1, "at": 0, "winners": [10], "losers": [20], "lobby": None, "round": 1}],
        "next_id": 2,
    }}))
    stats = Stats(path)
    stats.load()
    assert stats.player(1, 10).elo == ELO_START + 20
    assert stats.player(1, 20).elo == ELO_START - 20
    assert stats.player(1, 10) == Record(1, 0)


def test_week_highlights():
    games = [
        {"id": i, "winners": [1, 2], "losers": [3, 4], "elo": {"1": 10, "2": 10, "3": -10, "4": -10}}
        for i in range(1, 4)
    ] + [{"id": 4, "winners": [3], "losers": [1], "elo": {"3": 15, "1": -15}, "mvp": [3]}]
    lines = week_lines(games)
    assert lines[1].games == 4 and lines[1].best_streak == 3 and lines[1].elo == 15
    top = week_highlights(lines)
    assert top["most_games"][0] == 1
    assert top["best_winrate"][0] == 2
    assert top["streak"][0] in (1, 2)
    assert top["climber"][0] == 2
    assert top["mvp"][0] == 3
    assert week_highlights({}) == {}


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 120), color).save(buffer, "PNG")
    return buffer.getvalue()


def test_render_sizes_follow_picks():
    three = Image.open(io.BytesIO(portraits.render([[[_png("red")] * 3] * 5, [[_png("blue")] * 3] * 5])))
    one = Image.open(io.BytesIO(portraits.render([[[_png("red")]] * 5, [[None]] * 5])))
    assert three.width > one.width
    assert three.height == one.height


def test_recent_games_and_rivals(tmp_path):
    from stats import main_rivalry, rivals

    stats = Stats(tmp_path / "stats.json")
    stats.load()

    async def scenario():
        await stats.record_game(1, [10], [20])
        await stats.record_game(1, [20], [10])
        await stats.record_game(1, [20], [10])
        await stats.record_game(1, [10], [30])
        await stats.record_game(1, [10], [30])

    asyncio.run(scenario())
    recent = stats.recent_games(1, 10, limit=3)
    assert [won for _game, won, _delta in recent] == [True, True, False]
    assert recent[0][2] > 0 and recent[2][2] < 0

    games = stats.games_since(1, 0)
    nemesis, victim = rivals(games, 10)
    assert (nemesis.user_id, nemesis.wins, nemesis.losses) == (20, 1, 2)
    assert (victim.user_id, victim.wins, victim.losses) == (30, 2, 0)
    assert rivals(games, 10, min_games=5) == (None, None)
    assert main_rivalry(games) == (10, 20, 1, 2)
    assert main_rivalry(games[:2]) is None


def test_stats_lines():
    from cogs.stats import recent_line, rival_line
    from stats import Rival

    assert recent_line(True, 18.4, 100) == "🟢 `+18` · <t:100:R>"
    assert recent_line(False, -12.0, 100) == "🔴 `−12` · <t:100:R>"
    name = lambda user_id: f"P{user_id}"
    assert "проиграл ему 2 из 3 (1:2)" in rival_line(Rival(5, 1, 2), name, True)
    assert "обыграл его 3 из 3 (3:0)" in rival_line(Rival(5, 3, 0), name, False)


def test_leaderboard_ranking_and_newcomers(tmp_path):
    from cogs.stats import StatsCog

    cog = StatsCog.__new__(StatsCog)
    cog.stats = Stats(tmp_path / "stats.json")
    cog.stats.load()

    async def scenario():
        for _ in range(3):
            await cog.stats.record_game(1, [10], [20])
        await cog.stats.record_game(1, [30], [40])

    asyncio.run(scenario())
    assert [user_id for user_id, _ in cog.ranking(1)] == [10, 20]
    assert [user_id for user_id, _ in cog.newcomers(1)] == [30, 40]
