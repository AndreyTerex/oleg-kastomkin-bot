import asyncio
import json
import logging

from cogs.ops import ErrorBuffer, Outage, last_backup, make_backup, sanitize, uptime_text, usage_hint
from llm import LLMClient, Provider, usage_day
from storage import JsonStore


def test_sanitize_hides_addresses_links_and_keys():
    text = "fail 185.12.3.4:443 vless://abc@host?x=1 uuid 123e4567-e89b-12d3-a456-426614174000 key=SECRET 127.0.0.1"
    clean = sanitize(text)
    assert "185.12" not in clean and "vless" not in clean and "123e4567" not in clean and "SECRET" not in clean
    assert "127.0.0.1" in clean


def test_outage_alerts_once_after_threshold_and_on_recovery():
    outage = Outage(600)
    assert outage.update(True, 0) is None
    assert outage.update(True, 599) is None
    assert outage.update(True, 600) == "down"
    assert outage.update(True, 900) is None
    assert outage.update(False, 950) == "up"
    assert outage.update(False, 1000) is None
    # короткий сбой без тревоги — и без «починилось»
    assert outage.update(True, 2000) is None
    assert outage.update(False, 2100) is None


def test_backup_keeps_last_n(tmp_path):
    (tmp_path / "stats.json").write_text(json.dumps({"a": 1}))
    (tmp_path / "heartbeat").write_text("")
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        assert make_backup(tmp_path, day, keep=2) is not None
    assert make_backup(tmp_path, "2026-01-03", keep=2) is None
    days = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert days == ["2026-01-02", "2026-01-03"]
    assert (tmp_path / "backups" / "2026-01-03" / "stats.json").exists()
    assert not (tmp_path / "backups" / "2026-01-03" / "heartbeat").exists()
    assert last_backup(tmp_path)[0] == "2026-01-03"


def test_error_buffer_collects_sanitized_errors():
    buffer = ErrorBuffer()
    logger = logging.getLogger("scrimbot.test_ops")
    logger.addHandler(buffer)
    try:
        logger.error("сервер 10.1.2.3 упал")
        logger.warning("не ошибка")
    finally:
        logger.removeHandler(buffer)
    assert buffer.unreported == 1
    assert "10.1.2.3" not in buffer.recent[-1][1]


def test_uptime_and_usage_hint():
    assert uptime_text(90) == "1 мин"
    assert uptime_text(3 * 3600 + 120) == "3 ч 2 мин"
    assert uptime_text(2 * 86400 + 3600) == "2 д 1 ч"
    flash = Provider("gemini", "u", "k", "gemini-3.8-flash", scarce=True)
    lite = Provider("gemini", "u", "k", "gemini-3.5-flash-lite")
    assert usage_hint(flash, 3) == "3/20 сегодня"
    assert usage_hint(lite, 0) == "0/500 сегодня"
    assert usage_hint(Provider("groq", "u", "k", "m"), 0) == ""


def test_request_counter_persists_and_resets_daily(tmp_path):
    asyncio.run(_counter(tmp_path))


async def _counter(tmp_path):
    store = JsonStore(tmp_path / "llm_usage.json")
    client = LLMClient([Provider("gemini", "u", "k", "m")], store)
    await client.count_request("gemini:m")
    await client.count_request("gemini:m")
    assert client.requests_today("gemini:m") == 2
    assert json.loads((tmp_path / "llm_usage.json").read_text())["counts"]["gemini:m"] == 2
    store.data["day"] = "2000-01-01"
    assert client.requests_today("gemini:m") == 0
    await client.count_request("gemini:m")
    assert client.requests_today("gemini:m") == 1
    assert store.data["day"] == usage_day()


def test_recap_schedule(monkeypatch):
    from datetime import datetime

    import config
    from cogs.stats import recap_due, recap_fields, week_key

    monkeypatch.setattr(config, "RECAP_WEEKDAY", 5)
    monkeypatch.setattr(config, "RECAP_HOUR", 11)
    saturday = datetime(2026, 10, 3, 11, 5)
    assert recap_due(saturday, None)
    assert not recap_due(saturday, week_key(saturday))
    assert not recap_due(datetime(2026, 10, 3, 9), None)
    assert not recap_due(datetime(2026, 10, 2, 12), None)
    monkeypatch.setattr(config, "RECAP_HOUR", -1)
    assert not recap_due(saturday, None)

    from stats import week_highlights, week_lines

    top = week_highlights(week_lines([{"id": 1, "winners": [1], "losers": [2], "elo": {"1": 20, "2": -20}}]))
    fields = recap_fields(top, lambda user_id: f"P{user_id}")
    assert ("🎮 Больше всех каток", "**P1** — 1") in fields
    assert any("+20" in value for _title, value in fields)


def test_mvp_winners_ties():
    from cogs.lobby import mvp_winners

    assert mvp_winners({}) == []
    assert mvp_winners({1: 5, 2: 5, 3: 6}) == [5]
    assert mvp_winners({1: 5, 2: 6}) == [5, 6]
