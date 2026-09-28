"""Выдача и снятие ролей по просьбе в чате: «Олег, дай мне мид», «сними с @Вася роль «Фидер»».

Решает всё код, а не нейросеть: иначе её можно было бы уговорить выдать кому-нибудь админку.
Правила:
- любой может попросить себе или снять с себя роль линии;
- чужие роли и создание новых — только для тех, у кого есть право «Управлять ролями»;
- роли с правами модератора, управляемые (боты, бусты) и стоящие выше роли Олега не трогаются никогда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import discord

import config

LANE_ALIASES: dict[str, tuple[str, ...]] = {
    "top": ("топ", "top", "верх"),
    "jungle": ("лес", "лесник", "джангл", "jungle", "jg"),
    "mid": ("мид", "mid"),
    "adc": ("адк", "adc", "адц", "стрелок"),
    "support": ("сапп", "саппорт", "сапорт", "support", "supp"),
}

GIVE = re.compile(r"(?<!\w)(выдай|дай|добавь|назначь|повесь|верни)(?!\w)", re.IGNORECASE)
TAKE = re.compile(r"(?<!\w)(забери|сними|убери|отними|удали)(?!\w)", re.IGNORECASE)
# «Удали у меня все роли» — только роли линий: остальные роли сервера так не трогаем.
ALL_LANES = re.compile(r"(?<!\w)вс[еёю]\s+(рол|лини)", re.IGNORECASE)
CREATE = re.compile(r"(?<!\w)созда(й|ть)(?!\w)", re.IGNORECASE)
ROLE_WORD = re.compile(r"(?<!\w)рол[ьиея]\w*", re.IGNORECASE)
SELF = re.compile(r"(?<!\w)(мне|меня|себе|себя|мою|мой)(?!\w)", re.IGNORECASE)
QUOTED = re.compile(r"[«\"“„](.+?)[»\"”“]")
MENTION = re.compile(r"<@[!&]?\d+>")
AFTER_ROLE = re.compile(r"(?<!\w)рол[ьиея]\w*\s+(.+)", re.IGNORECASE)
NAME_END = re.compile(r"\s+(и|для|с|у|на)\s+|[,.!?]")

# Роли с такими правами Олег не выдаёт и не снимает ни при каких условиях.
DANGEROUS_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
    "manage_messages", "mention_everyone", "moderate_members", "manage_webhooks", "manage_nicknames",
)


@dataclass
class RoleRequest:
    action: str  # "give" или "take"
    roles: list[discord.Role] = field(default_factory=list)
    targets: list[discord.Member] = field(default_factory=list)
    create_name: str | None = None
    missing_name: str | None = None
    lanes_only: bool = False


# «Дай роль мида / лесника / саппа» — родительный падеж. Кроме «лес»: «в лесу», «из леса» — это просто речь.
GENITIVE_OK = {"мид", "топ", "лесник", "джангл", "сапп", "саппорт", "сапорт", "адк", "стрелок"}


def _lane_keys(text: str) -> list[str]:
    found = []
    for key, aliases in LANE_ALIASES.items():
        for alias in aliases:
            ending = "а?" if alias in GENITIVE_OK else ""
            if alias == "стрелок":
                pattern = r"(?<!\w)стрел(ок|ка)(?!\w)"
            else:
                pattern = rf"(?<!\w){re.escape(alias)}{ending}(?!\w)"
            if re.search(pattern, text, re.IGNORECASE):
                found.append(key)
                break
    return found


def _find_role(guild: discord.Guild, text: str, quoted: str | None) -> discord.Role | None:
    candidates = [role for role in guild.roles if not role.is_default() and not role.managed]
    if quoted:
        for role in candidates:
            if role.name.casefold() == quoted.casefold():
                return role
    lowered = text.casefold()
    matches = [role for role in candidates if len(role.name) >= 3 and role.name.casefold() in lowered]
    return max(matches, key=lambda role: len(role.name)) if matches else None


def parse(message: discord.Message, bot_user: discord.abc.User) -> RoleRequest | None:
    """Разбирает просьбу про роли. None — это не про роли (например, «Олег, дай совет»)."""
    text = MENTION.sub(" ", message.content)
    give, take, create = GIVE.search(text), TAKE.search(text), CREATE.search(text)
    if not (give or take or create):
        return None

    lanes = _lane_keys(text)
    if ALL_LANES.search(text) and not message.role_mentions and not QUOTED.search(text):
        lanes = list(LANE_ALIASES)
    role_word = ROLE_WORD.search(text)
    if not (message.role_mentions or lanes or role_word):
        return None

    # Какой глагол встретился раньше, тот и главный: «сними лес» / «дай мид».
    verbs = [(match.start(), action) for match, action in ((give, "give"), (take, "take"), (create, "give")) if match]
    request = RoleRequest(action=min(verbs)[1])

    quoted_match = QUOTED.search(text)
    quoted = quoted_match.group(1).strip() if quoted_match else None

    guild = message.guild
    if message.role_mentions:
        request.roles = list(message.role_mentions)
    elif lanes and not quoted and not create:
        request.roles = [role for key in lanes if (role := guild.get_role(config.LANE_BY_KEY[key].role_id))]
        request.lanes_only = bool(request.roles)
    else:
        role = _find_role(guild, text, quoted)
        if role is not None:
            request.roles = [role]
        else:
            name = quoted
            if name is None and (after := AFTER_ROLE.search(text)):
                name = NAME_END.split(after.group(1), maxsplit=1)[0].strip()
            name = (name or "").strip(" «»\"'")[:40] or None
            if create and name:
                request.create_name = name
            else:
                request.missing_name = name

    request.targets = [member for member in message.mentions if member.id != bot_user.id and not member.bot]
    if not request.targets and SELF.search(text):
        request.targets = [message.author]
    if not request.targets and message.reference is not None:
        replied = getattr(message.reference.resolved, "author", None)
        if isinstance(replied, discord.Member) and not replied.bot:
            request.targets = [replied]
    # «Олег, убери роль мид» без «мне» — люди почти всегда имеют в виду себя.
    if not request.targets and not request.create_name:
        request.targets = [message.author]
    return request


def dangerous(role: discord.Role) -> bool:
    return any(getattr(role.permissions, name, False) for name in DANGEROUS_PERMISSIONS)


def reachable(role: discord.Role, guild: discord.Guild) -> bool:
    return role < guild.me.top_role


def author_can_manage(role: discord.Role, author: discord.Member) -> bool:
    """Роль ниже самой высокой роли автора просьбы — как в самом Discord. Владельцу сервера можно всё."""
    return author.id == author.guild.owner_id or role < author.top_role
