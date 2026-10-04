import io

import pytest
from PIL import Image

import profile_card
import riot
from stats import best_partner, player_games


def test_riot_helpers():
    assert riot.parse_riot_id("Føxŷ#RU1") == ("Føxŷ", "RU1")
    assert riot.parse_riot_id("Name With Space#EUW") == ("Name With Space", "EUW")
    with pytest.raises(riot.RiotError):
        riot.parse_riot_id("безтега")
    entries = [{"queueType": "RANKED_FLEX_SR", "tier": "GOLD"},
               {"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD", "rank": "II", "leaguePoints": 45}]
    solo = riot.solo_entry(entries)
    assert riot.rank_text(solo) == "💚 Изумруд II, 45 LP"
    assert riot.rank_text({"tier": "MASTER", "rank": "I", "leaguePoints": 120}) == "🟣 Мастер, 120 LP"
    assert riot.rank_text(None) == "без ранга"
    assert riot.rank_elo(solo) == 1200 + 40 + 9
    # новичок — по рангу, ветеран — по Elo кастомок
    assert riot.blended_elo(1000, 0, solo) == pytest.approx(1249)
    assert riot.blended_elo(1000, 5, solo) == pytest.approx(1124.5)
    assert riot.blended_elo(1100, 10, solo) == 1100
    assert riot.blended_elo(1000, 0, None) == 1000


def test_partner_and_history():
    games = [
        {"id": 1, "winners": [1, 2], "losers": [3, 4], "elo": {"1": 20}},
        {"id": 2, "winners": [1, 2], "losers": [3, 4], "elo": {"1": 18}},
        {"id": 3, "winners": [3, 1], "losers": [2, 4], "elo": {"1": 15}},
        {"id": 4, "winners": [2, 4], "losers": [1, 3], "elo": {"1": -22}},
    ]
    assert best_partner(games, 1) == (2, 2, 2)
    mine = player_games(games, 1)
    assert [won for won, _ in mine] == [True, True, True, False]
    assert profile_card.elo_history(1031, [d for _, d in mine]) == [1000, 1020, 1038, 1053, 1031]


def test_card_renders():
    card = profile_card.CardData(
        name="Føxŷ (Дениска)", elo=1084, games=12, wins=8, losses=4, mvp=3, place=2, coins=1450,
        rank="💚 Изумруд II, 45 LP · Foxy#RU1", lanes="Top, Mid",
        elo_history=[1000, 1020, 1005, 1040, 1084], form=[True, False, True, True], partner="Havoc — 5 из 6",
    )
    data = profile_card.render(card)
    image = Image.open(io.BytesIO(data))
    assert image.size == (profile_card.WIDTH, profile_card.HEIGHT)
    assert profile_card.render(profile_card.CardData(name="Новичок", elo=1000, games=0, wins=0, losses=0))


def test_riot_without_key_explains(monkeypatch):
    import asyncio

    import config

    monkeypatch.setattr(config, "RIOT_API_KEY", "")
    client = riot.Riot.__new__(riot.Riot)
    assert not client.enabled
    with pytest.raises(riot.RiotError):
        asyncio.run(client.link(1, "Name#TAG"))
