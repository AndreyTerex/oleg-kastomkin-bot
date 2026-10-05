import io

from PIL import Image

import profile_card
from stats import best_partner, player_games


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
        lanes="Top, Mid",
        elo_history=[1000, 1020, 1005, 1040, 1084], form=[True, False, True, True], partner="Havoc — 5 из 6",
    )
    data = profile_card.render(card)
    image = Image.open(io.BytesIO(data))
    assert image.size == (profile_card.WIDTH, profile_card.HEIGHT)
    assert profile_card.render(profile_card.CardData(name="Новичок", elo=1000, games=0, wins=0, losses=0))
