"""Просьбы про сбор в чате: «Олег, переведи катаза в основной состав», «запиши меня в запас».

Как и роли, это решает код, а не нейросеть: модель только шутит, а менять состав должен тот,
у кого есть на это право. Других двигает организатор сбора, себя — кто угодно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import discord

STATUS_IN = "in"
STATUS_SUB = "sub"
REMOVE = "remove"

VERB = re.compile(
    r"(?<!\w)(переведи|перекинь|переставь|перенеси|поставь|добавь|запиши|верни|отправь|засунь|кинь|"
    r"убери|выкинь|удали|вычеркни|выпиши)(?!\w)",
    re.IGNORECASE,
)
REMOVE_VERB = re.compile(r"(?<!\w)(убери|выкинь|удали|вычеркни|выпиши)(?!\w)", re.IGNORECASE)
# Без слова про сбор это не про состав: «Олег, добавь огонька» — просто болтовня.
CONTEXT = re.compile(r"состав|запас|сбор|кастомк|скрим|играющ|основн|старт|лобби|список", re.IGNORECASE)
# Куда: «в запас(ные)», «в (основной) состав», «в игру», «в старт».
TO_SUB = re.compile(r"(?<!\w)(в|во)\s+(\w+\s+)?запас", re.IGNORECASE)
# «в составе» — это вопрос «кто в составе?», а не просьба; поэтому «состав» без «е» на конце.
TO_IN = re.compile(r"(?<!\w)в\s+(основ|состав(?!е)|игр|старт|играющ)", re.IGNORECASE)
FROM_LOBBY = re.compile(r"(?<!\w)из\s+(сбора|кастомк\w*|скрима|списка|лобби|состава)", re.IGNORECASE)
SELF = re.compile(r"(?<!\w)(меня|себя|мне|себе)(?!\w)", re.IGNORECASE)
MENTION = re.compile(r"<@[!&]?\d+>")
WORD = re.compile(r"[\w.]+", re.IGNORECASE)

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
# Латинские буквы, которые по-русски пишут иначе: kataz → «катаз», x → «кс».
LATIN_SOUNDS = str.maketrans({"x": "ks", "w": "v", "q": "k", "j": "zh"})
# Падежные окончания: «катаза», «катазу», «катазом», «анорию» (у Anoria окончание съедает «а»).
MAX_ENDING = 3
VOWELS = "aeiouy"


@dataclass
class LobbyRequest:
    status: str  # STATUS_IN, STATUS_SUB или REMOVE
    targets: list[discord.Member] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    """Имя к латинице без знаков: «Катаз» и «kataz» дают одно и то же."""
    text = text.casefold().translate(TRANSLIT).translate(LATIN_SOUNDS)
    return re.sub(r"[^a-z0-9]", "", text)


def name_keys(member: discord.Member) -> set[str]:
    keys = set()
    for name in (member.display_name, member.name, getattr(member, "global_name", None) or ""):
        key = normalize(name)
        if len(key) >= 3:
            keys.add(key)
        # Длинные ники с пробелами («Кексик Шмексик (ИТАЛЯ)») зовут по первому слову.
        first = normalize((name.split() or [""])[0])
        if len(first) >= 3:
            keys.add(first)
    return keys


def matches(word: str, member: discord.Member) -> bool:
    key = normalize(word)
    if len(key) < 3:
        return False
    for name in name_keys(member):
        if key == name or (key.startswith(name) and len(key) - len(name) <= MAX_ENDING):
            return True
        # Ник на гласную склоняется со сменой окончания: Anoria → «Анорию», «Анории».
        stem = name.rstrip(VOWELS)
        if len(stem) >= 3 and stem != name and key.startswith(stem) and len(key) - len(stem) <= MAX_ENDING:
            return True
        # «кат» → «kataz»: короткое прозвище в начале ника тоже годится, если не меньше 4 букв.
        if len(key) >= 4 and name.startswith(key):
            return True
    return False


def parse(
    message: discord.Message, bot_user: discord.abc.User, candidates: list[discord.Member]
) -> LobbyRequest | None:
    """None — это не просьба про состав сбора."""
    text = MENTION.sub(" ", message.content)
    has_verb = bool(VERB.search(text))
    if not CONTEXT.search(text):
        return None
    # Без глагола («Олег, Anoria в запас») — только если сказано, куда, и это не вопрос.
    if not has_verb and ("?" in text or not (TO_SUB.search(text) or TO_IN.search(text))):
        return None

    # Сначала «куда»: «убери из состава в запас» — это перевод в запас, а не удаление.
    if TO_SUB.search(text):
        status = STATUS_SUB
    elif TO_IN.search(text):
        status = STATUS_IN
    elif REMOVE_VERB.search(text) or FROM_LOBBY.search(text):
        status = REMOVE
    else:
        return None
    request = LobbyRequest(status)

    request.targets = [m for m in message.mentions if m.id != bot_user.id and not m.bot]
    if not request.targets:
        found: list[discord.Member] = []
        for word in WORD.findall(text):
            if normalize(word) in ("oleg", "olezh", "olezha", "kastomkin"):
                continue
            for member in candidates:
                if member not in found and matches(word, member):
                    found.append(member)
        request.targets = found
    if not request.targets and SELF.search(text):
        request.targets = [message.author]
    if not request.targets and message.reference is not None:
        replied = getattr(message.reference.resolved, "author", None)
        if isinstance(replied, discord.Member) and not replied.bot:
            request.targets = [replied]
    # Без глагола и без понятно кого — скорее всего, это обычная реплика, пусть ответит Олег.
    if not has_verb and not request.targets:
        return None
    return request
