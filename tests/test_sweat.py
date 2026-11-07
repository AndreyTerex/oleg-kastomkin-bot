import config
import persona
import sweat
from stats import Record


def _game(game_id, winners, losers):
    return {"id": game_id, "winners": winners, "losers": losers, "elo": {}}


def test_sweat_level_needs_games_and_winrate(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 10)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    assert sweat.sweat_level(Record(5, 0)) == 0  # мало каток — просто повезло
    assert sweat.sweat_level(Record(5, 5)) == 0
    assert sweat.sweat_level(Record(6, 4)) == 1
    assert sweat.sweat_level(Record(13, 7)) == 2  # 65%
    assert sweat.sweat_level(Record(7, 3)) == 3  # 70%
    assert sweat.sweat_level(Record(20, 0)) == len(persona.SWEAT_TITLES)
    assert sweat.title(Record(6, 4)) == persona.SWEAT_TITLES[0]
    assert sweat.title(Record(5, 5)) is None


def test_sweat_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0)
    assert sweat.sweat_level(Record(20, 0)) == 0
    assert sweat.context_note(Record(20, 0)) == ""


def test_context_note_marks_only_sweaty(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 10)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    note = sweat.context_note(Record(7, 3))
    assert "ПОТНЫЙ" in note and "70%" in note
    assert sweat.context_note(Record(11, 9)) == ""  # 55% — ни потный, ни заечка


def test_chill_players_are_praised(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 10)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    monkeypatch.setattr(config, "CHILL_WINRATE", 0.5)
    assert sweat.is_chill(Record(5, 5))
    assert sweat.is_chill(Record(2, 8))
    assert not sweat.is_chill(Record(2, 3))  # мало каток — ещё не понятно
    assert not sweat.is_chill(Record(6, 4))
    assert "заечка" in sweat.context_note(Record(4, 6)).lower()


def test_chief_sweat_skips_opted_out(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 10)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    players = {1: Record(7, 3), 2: Record(9, 1), 3: Record(5, 5), 4: Record(3, 0)}
    assert sweat.chief(players) == 2
    assert sweat.chief(players, skip={2}) == 1
    assert sweat.chief({3: Record(5, 5)}) is None


def test_streaks():
    games = [_game(1, [1], [2]), _game(2, [2], [1]), _game(3, [1], [2]), _game(4, [1], [2]), _game(5, [1], [2])]
    assert sweat.streaks(games, 1) == (3, 2)
    assert sweat.streaks(games, 2) == (0, 0)
    games.append(_game(6, [2], [1]))
    assert sweat.streaks(games, 1) == (0, 3)
    assert sweat.streaks([], 1) == (0, 0)


def test_game_jabs(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 3)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    name = lambda user_id: f"P{user_id}"
    games = [_game(i, [1], [2]) for i in range(1, 4)]
    players = {1: Record(3, 0), 2: Record(0, 3)}
    lines = sweat.game_jabs(games, [1], [2], players, name)
    assert len(lines) == 1 and "P1" in lines[0] and "3" in lines[0]
    # Отказавшихся от подколов не трогаем.
    assert sweat.game_jabs(games, [1], [2], players, name, skip={1}) == []
    # Четвёртая победа подряд — без реплики, чтобы не надоесть.
    games.append(_game(4, [1], [2]))
    assert sweat.game_jabs(games, [1], [2], {1: Record(4, 0), 2: Record(0, 4)}, name) == []
    # Серия потного прервалась — злорадствуем.
    games.append(_game(5, [2], [1]))
    lines = sweat.game_jabs(games, [2], [1], {1: Record(4, 1), 2: Record(1, 4)}, name)
    assert len(lines) == 1 and "P1" in lines[0]


def test_game_jabs_chief_lost(monkeypatch):
    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 3)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    name = lambda user_id: f"P{user_id}"
    games = [_game(1, [1], [2]), _game(2, [2], [1]), _game(3, [1], [2]), _game(4, [1], [2]), _game(5, [2], [1])]
    players = {1: Record(3, 2), 2: Record(2, 3)}
    lines = sweat.game_jabs(games, [2], [1], players, name)
    assert len(lines) == 1 and "P1" in lines[0]


def test_stats_cog_wiring(monkeypatch, tmp_path):
    import asyncio

    from cogs.stats import SWEATY_OF_WEEK, StatsCog, recap_fields
    from stats import Stats

    monkeypatch.setattr(config, "SWEAT_MIN_GAMES", 3)
    monkeypatch.setattr(config, "SWEAT_WINRATE", 0.6)
    monkeypatch.setattr(config, "CHILL_WINRATE", 0.5)
    cog = StatsCog.__new__(StatsCog)
    cog.stats = Stats(tmp_path / "stats.json")
    cog.stats.load()

    async def scenario():
        for _ in range(3):
            await cog.stats.record_game(1, [10], [20])

    asyncio.run(scenario())
    sweaty, chill = cog.stats.player(1, 10), cog.stats.player(1, 20)
    assert cog.sweat_line(10, sweaty) and cog.sweat_line(20, chill) in persona.CHILL_STATS
    assert "🥵" in cog.sweat_badge(10, sweaty, set())
    assert cog.sweat_badge(10, sweaty, {10}) == ""
    assert cog.sweat_badge(20, chill, set()) == " · 🐰"

    class Guild:
        id = 1

        @staticmethod
        def get_member(_user_id):
            return None

    jabs = cog.sweat_jabs(Guild(), [10], [20])
    assert len(jabs) == 1 and "кто-то" in jabs[0]

    name = lambda user_id: f"P{user_id}"
    line = type("Line", (), {"wins": 3, "games": 4})()
    assert recap_fields({"best_winrate": (10, line)}, name)[0][0] == SWEATY_OF_WEEK
    assert recap_fields({"best_winrate": (10, line)}, name, {10})[0][0] == "📈 Лучший винрейт"
