import asyncio
from types import SimpleNamespace as NS

import llm_actions as la
import persona
from cogs import chat


def member(user_id, display, name=None):
    return NS(id=user_id, display_name=display, name=name or display, global_name=None, bot=False)


ANORIA = member(1, "Anoria")
KATAZ = member(2, "kataz")
KEKS = member(3, "Кексик Шмексик (ИТАЛЯ)", "keks")


def test_parse_lobby_and_lane():
    action = la.parse({"variants": ["ок"], "action": {"type": "lobby", "who": ["анория"], "to": "sub"}})
    assert action == la.Action("lobby", ("анория",), to="sub")
    action = la.parse({"action": {"type": "lane", "who": "автор", "op": "give", "lanes": ["mid", "mid", "xxx"]}})
    assert action == la.Action("lane", ("автор",), op="give", lanes=("mid",))


def test_parse_rejects_unknown_and_broken():
    assert la.parse({"action": None}) is None
    assert la.parse({"action": {"type": "ban", "who": ["kataz"]}}) is None
    assert la.parse({"action": {"type": "lobby", "who": ["kataz"], "to": "admin"}}) is None
    assert la.parse({"action": {"type": "lane", "who": ["kataz"], "op": "give", "lanes": ["administrator"]}}) is None
    assert la.parse({"action": {"type": "lobby", "who": [], "to": "in"}}) is None
    assert la.parse(None) is None


def test_resolve_names():
    people = [ANORIA, KATAZ, KEKS]
    assert la.resolve(("анорию",), KATAZ, [], people) == [ANORIA]
    assert la.resolve(("автор", "Кексик"), KATAZ, [], people) == [KATAZ, KEKS]
    assert la.resolve(("Вася",), KATAZ, [], people) == []


def test_non_organizer_cannot_move_others_via_model():
    """Даже если модель предложила подвинуть другого, без прав организатора код откажет."""
    said, moved = [], []
    record = {"author_id": 99, "in": [1], "sub": [], "out": []}
    lobby_cog = NS(
        latest_open_record=lambda guild: ("555", record),
        members_from_ids=lambda guild, ids: [p for p in (ANORIA, KATAZ) if p.id in ids],
        is_organizer_member=lambda m, r: False,
        apply_move=lambda *args: moved.append(args) or ([], [], []),
    )
    guild = NS(members=[ANORIA, KATAZ], get_role=lambda role_id: None)
    cog = chat.Chat.__new__(chat.Chat)
    cog.bot = NS(user=NS(id=100), get_cog=lambda name: lobby_cog)

    async def say(message, text):
        said.append(text)

    cog.say = say
    message = NS(guild=guild, author=KATAZ, mentions=[])
    handled = asyncio.run(cog.run_action(message, la.Action("lobby", ("анория",), to="sub")))
    assert handled and said == [persona.LOBBY_FORBIDDEN] and not moved


def test_actions_prompt_only_for_direct_calls():
    assert "action" in persona.ACTIONS and "Права проверит" in persona.ACTIONS
