from datetime import datetime

import config
from cogs.matchday import invite_due
from screenshots import ShotPlayer, best_player, guess_winner, parse_scoreboard


def shot(name, champ, k, d, a, won):
    return ShotPlayer(name, champ, k, d, a, won)


def test_parse_scoreboard():
    text = '```json\n{"scoreboard": true, "players": [{"name": "Kekc", "champion": "Камилла", "kills": "5", ' \
           '"deaths": 2, "assists": null, "won": true}, "мусор"]}\n```'
    players = parse_scoreboard(text)
    assert players == [ShotPlayer("Kekc", "Камилла", 5, 2, 0, True)]
    assert parse_scoreboard('{"scoreboard": false, "players": []}') is None
    assert parse_scoreboard("не json") is None


def test_guess_winner_by_champions_and_names():
    teams = [[1, 2], [3, 4]]
    champions = {1: ["Камилла"], 2: ["Люкс"], 3: ["Зед"], 4: ["Джинкс"]}
    names = {1: ["Кекс"], 2: ["Anoria"], 3: ["Havoc"], 4: ["Kaban RS 5"]}
    players = [
        shot("x", "Камилла", 5, 1, 3, False), shot("y", "Люкс", 1, 5, 9, False),
        shot("z", "Зед", 9, 2, 1, True), shot("KabanRS5", "Варвик", 3, 3, 3, True),
    ]
    assert guess_winner(players, teams, champions, names) == 1
    # без совпадений — не угадываем
    assert guess_winner([shot("q", "Тимо", 1, 1, 1, True)], teams, champions, names) is None
    # противоречие — тоже None
    contradict = [shot("a", "Камилла", 0, 0, 0, True), shot("b", "Зед", 0, 0, 0, True)]
    assert guess_winner(contradict, teams, champions, names) is None
    assert best_player(players).champion == "Камилла"


def test_invite_schedule(monkeypatch):
    monkeypatch.setattr(config, "LOBBY_INVITE_HOUR", 17)
    evening = datetime(2026, 10, 1, 18, 0)
    assert invite_due(evening, None, False)
    assert not invite_due(evening, "2026-10-01", False)
    assert not invite_due(evening, None, True)
    assert not invite_due(datetime(2026, 10, 1, 12, 0), None, False)
    assert not invite_due(datetime(2026, 10, 1, 23, 30), None, False)
    monkeypatch.setattr(config, "LOBBY_INVITE_HOUR", -1)
    assert not invite_due(evening, None, False)


def test_image_message_format():
    from llm import image_message

    parts = image_message("что тут", b"\x89PNG", "image/png")
    assert parts[0] == {"type": "text", "text": "что тут"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
