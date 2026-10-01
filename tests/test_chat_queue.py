import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import discord

from cogs import chat as chat_module
from cogs.chat import (
    Chat, ChannelState, enqueue, is_bare_call, next_pending, pick_variant, question_lines, salvage_variants,
)

NOW = datetime.now(timezone.utc)


def msg(id, author, text="", age=0, reference=None):
    return NS(
        id=id, author=NS(id=author, display_name=f"P{author}"), content=text, clean_content=text,
        created_at=NOW - timedelta(seconds=age), reference=reference, embeds=[], attachments=[], stickers=[],
        channel=NS(id=1),
    )


def test_bare_call_detection():
    assert is_bare_call("Олег")
    assert is_bare_call("олежа, ")
    assert is_bare_call("<@123> эй")
    assert not is_bare_call("Олег?")
    assert not is_bare_call("Олег, как контрить Зеда")
    assert not is_bare_call("олег привет")


def test_queue_keeps_latest_per_person_and_skips_answered_and_old():
    pending = deque(maxlen=5)
    enqueue(pending, msg(1, 10, "олег а"))
    enqueue(pending, msg(2, 20, "олег б"))
    enqueue(pending, msg(3, 10, "олег а уточню"))
    assert [m.id for m in pending] == [2, 3]
    state = ChannelState()
    state.pending = pending
    state.answered.append(2)
    enqueue(state.pending, msg(4, 30, "старьё", age=9999))
    assert next_pending(state).id == 3
    assert next_pending(state) is None


def test_question_lines_join_consecutive_messages_and_quote():
    me = NS(id=99)
    quoted = msg(5, 40, "кто идёт на топ?", age=100)
    history = [msg(6, 30, "привет", age=60), msg(7, 10, "олег", age=20), msg(8, 10, "кого банить?", age=5)]
    question = msg(9, 10, "против Зеда", reference=NS(resolved=quoted))
    lines = question_lines(question, history + [question], me)
    assert lines[0] == "(в ответ на сообщение P40: «кто идёт на топ?»)"
    assert lines[1:] == ["P10: олег", "P10: кого банить?", "P10: против Зеда"]


def test_salvage_variants_from_cut_json():
    cut = '{"variants": ["Первый вариант", "Второй \\"в кавычках\\"", "Тре'
    assert salvage_variants(cut) == ["Первый вариант", 'Второй "в кавычках"']
    assert pick_variant(cut) == "Первый вариант"
    broken = '{"variants": ["Один", "Два"], "best": 1,,}'
    assert salvage_variants(broken) == ["Один", "Два"]
    assert pick_variant('{"oops": 1') is None


def test_calls_while_busy_are_all_answered(monkeypatch):
    cog = Chat.__new__(Chat)
    cog.channels = {}
    answered = []

    async def answer_once(message, *, called, queued):
        if message.id == 1:
            # пока думаем над первым, пришли ещё два зова от разных людей и уточнение от первого
            await cog.answer(msg(2, 20, "олег б"), called=True)
            await cog.answer(msg(3, 30, "олег в"), called=True)
            await cog.answer(msg(4, 20, "олег б, точнее так"), called=True)
        answered.append(message.id)

    async def wait_followup(message):
        return message

    cog.answer_once = answer_once
    cog.wait_followup = wait_followup
    asyncio.run(cog.answer(msg(1, 10, "олег а"), called=True))
    assert answered == [1, 3, 4]
    assert not cog.state(1).busy


def test_bare_call_waits_for_the_question():
    cog = Chat.__new__(Chat)
    followup = msg(2, 10, "как контрить Зеда")

    async def wait_for(event, check, timeout):
        assert event == "message" and check(followup)
        return followup

    cog.bot = NS(wait_for=wait_for)
    assert asyncio.run(cog.wait_followup(msg(1, 10, "Олег"))) is followup
    assert asyncio.run(cog.wait_followup(msg(3, 10, "Олег, как дела?"))).id == 3


def test_reply_falls_back_when_message_deleted():
    sent = []

    async def reply(*args, **kwargs):
        raise discord.NotFound(NS(status=404, reason="x"), "Unknown Message")

    async def send(text, **kwargs):
        sent.append(text)

    message = msg(1, 10, "олег")
    message.reply = reply
    message.channel = NS(id=1, send=send)
    cog = Chat.__new__(Chat)
    asyncio.run(cog.reply_or_send(message, "привет"))
    assert sent == ["P10, привет"]
    assert chat_module.RETRY_AFTER_FAILURE > 0


def test_stats_topic_catches_table_questions():
    from cogs.chat import STATS_TOPIC

    assert STATS_TOPIC.search("дай инфу по остальным трем отдельной табличкой")
    assert STATS_TOPIC.search("какое у меня место?")
    assert STATS_TOPIC.search("сколько у меня эло")
    assert not STATS_TOPIC.search("пойдём вместе на мид")


def test_leaderboard_summary_lists_everyone_including_newcomers():
    from stats import Record

    people = {i: NS(display_name=f"P{i}") for i in range(1, 15)}
    ranking = [(i, Record(3, 1, 1000 + i)) for i in range(1, 13)]
    newcomers = [(13, Record(1, 1)), (14, Record(0, 1))]
    stats_cog = NS(
        ranking=lambda guild_id: ranking, newcomers=lambda guild_id: newcomers,
        stats=NS(games_count=lambda guild_id: 20),
    )
    cog = Chat.__new__(Chat)
    cog.bot = NS(get_cog=lambda name: stats_cog)
    text = cog.leaderboard_summary(NS(id=1, get_member=lambda i: people.get(i)))
    assert "12. P12" in text
    assert "меньше 3 каток" in text and "P13" in text and "P14" in text
