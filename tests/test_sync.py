import asyncio
import json
from types import SimpleNamespace as NS

import config
import datasync
import sync


def test_lock_parsing_and_ownership():
    text = sync.lock_text("abcdef123456", 1000)
    assert sync.parse_lock(text) == ("abcdef123456", 1000.0)
    assert sync.parse_lock("привет") is None
    assert sync.lock_is_foreign(("other1234567", 990.0), "abcdef123456", 1000, 180)
    assert not sync.lock_is_foreign(("other1234567", 700.0), "abcdef123456", 1000, 180)  # протух
    assert not sync.lock_is_foreign(("abcdef123456", 999.0), "abcdef123456", 1000, 180)  # свой
    assert sync.snapshot_time("oleg-sync-1700000000.zip") == 1700000000.0
    assert sync.snapshot_time("other.zip") is None


def test_instance_id_is_stable(tmp_path):
    first = sync.instance_id(tmp_path)
    assert first == sync.instance_id(tmp_path) and len(first) == 12


class FakeMessage:
    def __init__(self, channel, content="", file=None, author=1):
        self.channel = channel
        self.id = len(channel.messages) + 1
        self.content = content
        self.author = NS(id=author)
        self.attachments = []
        if file is not None:
            data = file.fp.read()

            async def read():
                return data

            self.attachments = [NS(filename=file.filename, read=read)]

    async def edit(self, content):
        self.content = content
        return self

    async def delete(self):
        self.channel.messages.remove(self)


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def history(self, limit):
        for message in reversed(self.messages[-limit:]):
            yield message

    async def send(self, content="", file=None):
        message = FakeMessage(self, content, file)
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)


def make_sync(monkeypatch, tmp_path, channel):
    monkeypatch.setattr(config, "SYNC_CHANNEL_ID", 42)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "CLAIM_SETTLE", 0)
    bot = NS(user=NS(id=1), guilds=[NS(id=7)], get_partial_messageable=lambda channel_id: channel)
    return sync.Sync(bot)


def test_new_computer_takes_data_from_channel(monkeypatch, tmp_path):
    channel = FakeChannel()
    old_pc, new_pc = tmp_path / "old", tmp_path / "new"
    old_pc.mkdir()
    new_pc.mkdir()
    (old_pc / "stats.json").write_text(json.dumps({"7": {"players": {"10": {"wins": 5}}}}))

    async def scenario():
        first = make_sync(monkeypatch, old_pc, channel)
        await first.prepare()
        await first.push()
        assert any(sync.snapshot_time(a.filename) for m in channel.messages for a in m.attachments)
        # Новый компьютер стартует, пока старый жив: ждёт (замок свежий и чужой).
        second = make_sync(monkeypatch, new_pc, channel)
        monkeypatch.setattr(sync, "STANDBY_POLL", 0)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 1:
                # Старый «выключился»: замок протух.
                lock_message = next(m for m in channel.messages if sync.parse_lock(m.content))
                lock_message.content = sync.lock_text(first.me, 0)

        monkeypatch.setattr(sync.asyncio, "sleep", fake_sleep)
        await second.prepare()
        assert calls  # ждал
        stats = json.loads((new_pc / "stats.json").read_text())
        assert stats["7"]["players"]["10"]["wins"] == 5
        # Замок теперь у нового, старый при следующем пульсе уходит.
        closed = []

        async def close():
            closed.append(True)

        first.bot.close = close
        await first.heartbeat()
        assert first.lost and closed

    asyncio.run(scenario())


def test_push_only_on_change_and_keeps_few_snapshots(monkeypatch, tmp_path):
    channel = FakeChannel()
    (tmp_path / "stats.json").write_text("{}")

    async def scenario():
        node = make_sync(monkeypatch, tmp_path, channel)
        await node.prepare()
        await node.push()
        count = lambda: sum(1 for m in channel.messages if m.attachments)
        assert count() == 1
        await node.push()
        assert count() == 1  # ничего не поменялось
        for value in range(5):
            (tmp_path / "stats.json").write_text(json.dumps({"v": value}))
            await node.push()
        assert count() == sync.SNAPSHOTS_KEEP
        # В снимке нет кэша VPN.
        (tmp_path / "vpn.json").write_text("{}")
        await node.push(force=True)
        newest = [m for m in channel.messages if m.attachments][-1]
        files, _ = datasync.read_archive(await newest.attachments[0].read())
        assert "vpn.json" not in files

    asyncio.run(scenario())
