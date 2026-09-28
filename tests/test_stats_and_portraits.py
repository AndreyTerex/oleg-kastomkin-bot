import asyncio
import io

from PIL import Image

import portraits
from stats import Record, Stats, plural


def test_record_rating_is_smoothed():
    assert Record().rating == 0.5
    assert Record(1, 0).rating == 2 / 3
    assert Record(7, 3).winrate == 0.7
    assert Record(7, 3).describe() == "10 каток, 7 побед (70%)"
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


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 120), color).save(buffer, "PNG")
    return buffer.getvalue()


def test_render_sizes_follow_picks():
    three = Image.open(io.BytesIO(portraits.render([[[_png("red")] * 3] * 5, [[_png("blue")] * 3] * 5])))
    one = Image.open(io.BytesIO(portraits.render([[[_png("red")]] * 5, [[None]] * 5])))
    assert three.width > one.width
    assert three.height == one.height
