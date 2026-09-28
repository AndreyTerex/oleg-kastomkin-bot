from types import SimpleNamespace as NS

import lobby_requests as lr

BOT = NS(id=100)


def member(user_id, display, name=None):
    return NS(id=user_id, display_name=display, name=name or display, global_name=None, bot=False)


KATAZ = member(1, "kataz")
KEKS = member(2, "Кексик Шмексик (ИТАЛЯ)", "keks")
FOXY = member(3, "Føxý (Дениска)", "foxy")
PEOPLE = [KATAZ, KEKS, FOXY]


def message(text, author=KEKS, mentions=(), reference=None):
    return NS(content=text, author=author, mentions=list(mentions), reference=reference)


def parse(text, **kw):
    return lr.parse(message(text, **kw), BOT, PEOPLE)


def test_transliterated_name_with_case_ending():
    request = parse("Олежа, переведи катаза в основной состав кастомки")
    assert request.status == lr.STATUS_IN
    assert request.targets == [KATAZ]


def test_to_sub_and_self():
    request = parse("Олег, запиши меня в запасные")
    assert request.status == lr.STATUS_SUB and request.targets == [KEKS]
    request = parse("Олег, перекинь кексика в запас")
    assert request.status == lr.STATUS_SUB and request.targets == [KEKS]


def test_remove_and_move_from_roster_to_sub():
    assert parse("Олег, убери катаза из сбора").status == lr.REMOVE
    assert parse("Олег, убери катаза из состава в запас").status == lr.STATUS_SUB


def test_mentions_win_over_names():
    request = parse("Олег, переведи <@3> в состав", mentions=[FOXY])
    assert request.targets == [FOXY]


def test_not_about_lobby():
    assert parse("Олег, добавь огонька") is None
    assert parse("Олег, переведи это на английский") is None
    assert parse("Олег, кто в составе?") is None


def test_unknown_name_gives_no_targets_but_is_a_request():
    request = parse("Олег, переведи васю в основной состав")
    assert request is not None and request.targets == []
