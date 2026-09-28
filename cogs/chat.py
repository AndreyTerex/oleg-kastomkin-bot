"""Олег в чате: отвечает, когда зовут, иногда сам встревает в разговор и подкалывает.

Когда говорит:
- всегда, если его тегнули, ответили на его сообщение или написали «Олег»;
- изредка сам — только в болтливых каналах и только в живой беседе (несколько человек
  пишут прямо сейчас), не чаще раза в 12 минут;
- ставит реакции-эмодзи на «фид», «тильт», «гг» и т. п. — без обращения к модели.

Что видит модель: последние сообщения канала, линии собеседников из панели ролей,
ближайший сбор (если о нём говорят) и то, что Олега просили запомнить о собеседниках.
Сообщения тех, кто попросил их не трогать, в модель не уходят и о них ничего не запоминается.

Кроме болтовни Олег по просьбе в чате:
- запоминает и забывает факты о людях («Олег, запомни его, пусть он будет как Бэнни»);
- выдаёт и снимает роли («Олег, дай мне мид») — это решает код, а не нейросеть, см. role_requests.py.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
import persona
import llm_actions
import lobby_requests
import role_requests
from llm import LLMClient, LLMUnavailable, build_providers
from memory import Memory
from storage import JsonStore
from utils import member_lanes, respond

log = logging.getLogger("scrimbot.chat")

MODE_CHATTY = "chatty"
MODE_CALLED = "called"
MODE_OFF = "off"
MODE_TITLES = {
    MODE_CHATTY: "болтливый — отвечает и сам встревает",
    MODE_CALLED: "отвечает, только когда зовут",
    MODE_OFF: "выключен",
}
# Где Олег болтлив с самого начала; в остальных каналах — только когда зовут.
DEFAULT_CHATTY_CHANNELS = ("основной-флуд", "олежа-кастомкин")

NAME = re.compile(r"(?<![\w-])(олег|олеж|кастомкин)\w*", re.IGNORECASE)

HISTORY_LIMIT = 30
HISTORY_MAX_AGE = timedelta(hours=6)
LINE_LIMIT = 300
# Длинный ответ режется до одной-двух фраз: у модели лучшая мысль обычно в начале, дальше вода.
REPLY_SOFT_LIMIT = 200
REPLY_LIMIT = 300
# На настоящий вопрос ответ может быть длиннее — два-три предложения по делу.
HELP_SOFT_LIMIT = 320
HELP_LIMIT = 450

# Настоящий вопрос к Олегу: тогда сначала точный ответ, а шутка — потом.
HELP_QUESTION = re.compile(
    r"\?|(?<!\w)(как|что|чем|почему|зачем|сколько|когда|кто|где|куда|какой|какая|какое|какие|каким|чей|"
    r"можно ли|посоветуй|подскажи|объясни|расскажи|помоги|напомни|покажи|what|how)(?!\w)",
    re.IGNORECASE,
)
STATS_TOPIC = re.compile(r"стат|винрейт|лучш|топ|лидер|побед|рейтинг|сильн|слаб", re.IGNORECASE)
# Вопрос про то, кто на какой линии, а не про игру на линии («как стоять на миде»).
LANES_TOPIC = re.compile(
    r"кто\b.{0,25}(топ|лес|джанг|мид|адк|стрел|сапп?орт|сапп?\b|лини)|какие (линии|роли)|отметил", re.IGNORECASE
)
# Не больше стольких ответов за окно в одном канале — иначе Олег перетягивает весь чат на себя.
BURST_LIMIT = 6
BURST_WINDOW = 2 * 60

# О сборе Олег узнаёт, только когда о нём говорят, — иначе суёт «19:00» в каждую реплику.
LOBBY_TOPIC = re.compile(
    r"кастом|сбор|катк|запис|кто (играет|идёт|идет|будет|в игру)|\b\d{1,2}[:.]\d{2}\b|состав|драфт|капитан|режим"
    r"|бан|забан|пик|раздач|чемп|чамп|контр|против них|соперник|команд|сторон|синие|синих|красные|красных",
    re.IGNORECASE,
)
# Просьбы помолчать обычными словами работают как /oleg-quiet.
QUIET_REQUEST = re.compile(
    r"замолч|помолч|будь тих|затк|не отвечай|никому не отвечать|отстань|хватит (болтать|писать|отвечать)|не пиши",
    re.IGNORECASE,
)
QUIET_DEFAULT_MINUTES = 30
# Сколько секунд после отправки исправленное сообщение с просьбой про роль ещё выполняется.
EDIT_WINDOW = 120
# «Олег, говори» снимает молчание — но только если он сейчас действительно замьючен.
UNQUIET_REQUEST = re.compile(
    r"размь?ют|размут|можешь говорить|(?<!\w)говори(?!\w)|хватит молчать|отомри|вернись",
    re.IGNORECASE,
)

REMEMBER = re.compile(r"(?<!\w)запомни", re.IGNORECASE)
FORGET = re.compile(r"(?<!\w)забудь", re.IGNORECASE)

MEMORY_SYSTEM = (
    "Ты помогаешь боту Олегу вести записную книжку о людях из чата. По переписке пойми, что именно его "
    "попросили запомнить в последнем сообщении и о ком это. Ответь строго одним JSON-объектом без пояснений: "
    '{"about": "<ник человека ровно как в чате, или null, если это про весь сервер>", '
    '"fact": "<суть коротко, до 100 символов, без имени самого человека, утверждением, а не просьбой: не «пусть будет как Бэнни», а «наш Бэнни»>"}. '
    'Если запомнить нечего, ответь {"fact": null}.'
)

# Если Олега звали, пока он думал над другим ответом, он ответит на последний зов, если тот не старше этого.
PENDING_MAX_AGE = 120

INVISIBLE = re.compile(r"[\u200b-\u200f\u2060-\u206f\ufeff]")
# Корейские буквы и иероглифы: модели иногда вставляют «탑» вместо «топ» или 艸 в смайлик.
FOREIGN_SCRIPT = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3400-\u4dbf\u4e00-\u9fff]")
# Мат в ответах Олега. Корни с границей слова слева, чтобы «оскорбляешь» или «хуже» не попадали.
PROFANITY = re.compile(
    r"(?<!\w)(бля|сук[аи]|сучар|ху[йеёяи]|пизд|[её]б[аулн]|нахуй|нахер|похуй|мудак|мудил|пид[оа]?р|долбо|залуп|гандон|шлюх)",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Как Олег подписан в переданной модели истории чата: так он узнаёт свои прошлые реплики.
OWN_NAME = "Олег (ты)"
# Паузу в разговоре длиннее этой модель видит отдельной строкой.
PAUSE_VISIBLE = timedelta(minutes=30)

# Чуть ниже единицы: при 1.0 бесплатные модели чаще уносит в бессвязные метафоры.
REPLY_TEMPERATURE = 0.85

# Статистику каток Олег упоминает, только когда катка набралось хотя бы столько.
STATS_MIN_GAMES = 3

# Прожарка: сколько сообщений канала просмотреть, сколько реплик человека взять и какой длины ответ.
ROAST_HISTORY_SCAN = 150
ROAST_QUOTES = 8
ROAST_SOFT_LIMIT = 420
ROAST_LIMIT = 600

# Олег байтит часто: встревает в живой разговор не реже, чем раз в несколько минут.
SPONTANEOUS_COOLDOWN = 5 * 60
SPONTANEOUS_CHANCE = 0.15
SPONTANEOUS_TOPIC_BONUS = 2.5
ACTIVITY_WINDOW = 10 * 60
ACTIVITY_MIN_MESSAGES = 4
ACTIVITY_MIN_PEOPLE = 2

USER_COOLDOWN = 6
CHANNEL_COOLDOWN = 3
REACTION_CHANCE = 0.3
REACTION_COOLDOWN = 3 * 60

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MSK = timezone(timedelta(hours=3))


@dataclass
class ChannelState:
    activity: deque = field(default_factory=lambda: deque(maxlen=50))
    last_spoke: float = 0.0
    last_reaction: float = 0.0
    last_tail: str = ""
    last_mood: str = ""
    burst_notice_at: float = 0.0
    busy: bool = False
    pending: discord.Message | None = None
    answers: deque = field(default_factory=lambda: deque(maxlen=BURST_LIMIT))


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = LLMClient(build_providers())
        self.store = JsonStore(Path(config.DATA_DIR) / "oleg.json")
        self.memory = Memory(Path(config.DATA_DIR) / "memory.json")
        self.channels: dict[int, ChannelState] = {}
        self.user_last: dict[int, float] = {}
        self.sleep_until = 0.0
        self.sleep_announced = False
        self.empty_in_row = 0

    async def cog_load(self) -> None:
        self.store.load()
        self.memory.load()
        if self.client.enabled:
            log.info("Олег болтает через: %s", ", ".join(p.label for p in self.client.providers))
        else:
            log.info("Ключей OpenRouter и Groq нет — Олег в чате молчит, остальной бот работает")

    async def cog_unload(self) -> None:
        await self.client.close()

    # --- настройки ------------------------------------------------------

    def mode_for(self, channel: discord.abc.GuildChannel) -> str:
        stored = self.store.data.get("modes", {}).get(str(channel.id))
        if stored in MODE_TITLES:
            return stored
        return MODE_CHATTY if getattr(channel, "name", "") in DEFAULT_CHATTY_CHANNELS else MODE_CALLED

    def is_quiet(self, channel_id: int) -> bool:
        return self.store.data.get("quiet_until", {}).get(str(channel_id), 0) > time.time()

    def opted_out(self, user_id: int) -> bool:
        return user_id in self.store.data.get("optout", [])

    def state(self, channel_id: int) -> ChannelState:
        return self.channels.setdefault(channel_id, ChannelState())

    # --- когда говорить ---------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if self.mode_for(message.channel) == MODE_OFF:
            if NAME.search(message.content or ""):
                log.info("Олега позвали в #%s, но там он выключен (/oleg-mode)", message.channel)
            return

        self.check_content_access(message)
        called = self.is_called(message)
        if called:
            # Прямые просьбы работают, даже когда Олега попросили помолчать.
            if self.is_quiet(message.channel.id) and UNQUIET_REQUEST.search(message.content):
                await self.go_loud(message)
                return
            if QUIET_REQUEST.search(message.content):
                await self.go_quiet(message)
                return
            if await self.handle_roles(message):
                return
            if await self.handle_lobby(message):
                return
            if await self.handle_memory(message):
                return

        if not self.client.enabled or self.is_quiet(message.channel.id):
            if called:
                log.info("Олега позвали в #%s, но он молчит (%s)", message.channel,
                         "нет ключей нейросетей" if not self.client.enabled else "/oleg-quiet")
            return
        mode = self.mode_for(message.channel)
        state = self.state(message.channel.id)
        state.activity.append((time.time(), message.author.id))

        if called:
            await self.answer(message, called=True)
            return
        if mode != MODE_CHATTY or self.opted_out(message.author.id):
            return
        if await self.maybe_react(message, state):
            return
        if self.should_chime_in(message, state):
            await self.answer(message, called=False)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Просьбу про роль часто пишут с опечаткой и тут же исправляют — исправленную тоже выполняем."""
        if after.author.bot or after.guild is None or before.content == after.content:
            return
        if (discord.utils.utcnow() - after.created_at).total_seconds() > EDIT_WINDOW:
            return
        if self.mode_for(after.channel) == MODE_OFF or not self.is_called(after):
            return
        # Если и до правки это была просьба про роль — она уже выполнена, второй раз не нужно.
        if role_requests.parse(before, self.bot.user) is None and role_requests.parse(after, self.bot.user):
            await self.handle_roles(after)

    def check_content_access(self, message: discord.Message) -> None:
        """Без Message Content Intent Discord присылает сообщения без текста — Олег слепнет молча.

        Несколько пустых сообщений подряд почти наверняка значат, что интент выключили в портале.
        """
        empty = not (message.content or message.attachments or message.stickers or message.embeds)
        self.empty_in_row = self.empty_in_row + 1 if empty else 0
        if self.empty_in_row == 5:
            log.warning(
                "Сообщения приходят без текста — похоже, в Developer Portal выключен Message Content "
                "Intent. Олег не видит чат: включите интент и перезапустите бота."
            )

    async def go_quiet(self, message: discord.Message) -> None:
        """«Олег, помолчи часик» — то же, что /oleg-quiet, только словами."""
        minutes = 60 if re.search(r"час", message.content, re.IGNORECASE) else QUIET_DEFAULT_MINUTES
        self.store.data.setdefault("quiet_until", {})[str(message.channel.id)] = time.time() + minutes * 60
        await self.store.save()
        try:
            await message.reply(
                random.choice(persona.QUIET_LINES).format(minutes=minutes),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Не удалось ответить на просьбу помолчать")
        log.info("Олега попросили помолчать в #%s на %d мин", message.channel, minutes)

    async def go_loud(self, message: discord.Message) -> None:
        """«Олег, говори» — снимает молчание в канале, то же, что /oleg-unmute."""
        self.store.data.setdefault("quiet_until", {}).pop(str(message.channel.id), None)
        await self.store.save()
        await self.say(message, random.choice(persona.UNQUIET_LINES))
        log.info("Олега размьютили в #%s", message.channel)

    def is_called(self, message: discord.Message) -> bool:
        me = self.bot.user
        if me in message.mentions:
            return True
        # Ответ на сообщение Олега. У удалённого исходного сообщения автора нет — тогда это не зов.
        reference = message.reference
        replied_to = getattr(reference.resolved, "author", None) if reference else None
        if replied_to is not None and replied_to.id == me.id:
            return True
        return bool(NAME.search(message.content))

    def should_chime_in(self, message: discord.Message, state: ChannelState) -> bool:
        now = time.time()
        if now - state.last_spoke < SPONTANEOUS_COOLDOWN:
            return False
        recent = [(ts, author) for ts, author in state.activity if now - ts < ACTIVITY_WINDOW]
        if len(recent) < ACTIVITY_MIN_MESSAGES or len({author for _, author in recent}) < ACTIVITY_MIN_PEOPLE:
            return False
        chance = SPONTANEOUS_CHANCE
        if persona.TOPIC.search(message.content):
            chance *= SPONTANEOUS_TOPIC_BONUS
        return random.random() < chance

    async def maybe_react(self, message: discord.Message, state: ChannelState) -> bool:
        now = time.time()
        if now - state.last_reaction < REACTION_COOLDOWN:
            return False
        for pattern, emojis in persona.REACTIONS:
            if pattern.search(message.content):
                if random.random() >= REACTION_CHANCE:
                    return False
                state.last_reaction = now
                await self.react(message, random.choice(emojis))
                return True
        return False

    @staticmethod
    async def react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.debug("Не удалось поставить реакцию %s", emoji)

    # --- ответ -------------------------------------------------------------

    async def answer(self, message: discord.Message, *, called: bool, queued: bool = False) -> None:
        state = self.state(message.channel.id)
        if state.busy:
            # Пока Олег думает, зовы не теряются: ответит на последний, когда освободится.
            if called:
                state.pending = message
            return
        await self.answer_once(message, called=called, queued=queued)

        pending, state.pending = state.pending, None
        if pending is not None and pending.id != message.id:
            age = (discord.utils.utcnow() - pending.created_at).total_seconds()
            if age < PENDING_MAX_AGE:
                await self.answer(pending, called=True, queued=True)

    async def answer_once(self, message: discord.Message, *, called: bool, queued: bool) -> None:
        state = self.state(message.channel.id)
        now = time.time()
        # Где писать нельзя, туда и не сочиняем — иначе лимит Groq уйдёт впустую.
        if not message.channel.permissions_for(message.guild.me).send_messages:
            if called:
                log.info("Олега позвали в #%s, но у него нет права писать там", message.channel)
            return
        if now < self.sleep_until:
            if called:
                await self.react(message, "😴")
            return
        if len(state.answers) >= BURST_LIMIT and now - state.answers[0] < BURST_WINDOW:
            # Уже наговорился в этом канале — пусть люди пообщаются без него. Но молчит не беззвучно:
            # на первый зов в паузе объясняет, на остальные ставит реакцию.
            if called:
                if state.burst_notice_at < state.answers[0]:
                    state.burst_notice_at = now
                    await self.say(message, random.choice(persona.BURST_LINES))
                else:
                    await self.react(message, "🤐")
            return
        if called and not queued:
            if now - self.user_last.get(message.author.id, 0) < USER_COOLDOWN or now - state.last_spoke < CHANNEL_COOLDOWN:
                return
        if called:
            self.user_last[message.author.id] = now

        state.busy = True
        try:
            # Каждый раз новый типаж, два одинаковых подряд не выпадают; подходящий к разговору — чаще.
            mood = pick_mood(message.content, state.last_mood)
            state.last_mood = mood[0]
            help_mode = called and is_help_question(message.content)
            prompt, openings = await self.build_prompt(message, called=called, mood=mood, help_mode=help_mode)
            allow_skip = not called

            def choose(text: str) -> str | None:
                return pick_variant(text, allow_skip=allow_skip, avoid_openings=openings)

            async with message.channel.typing():
                reply = await self.client.complete(
                    persona.PERSONA, prompt, temperature=REPLY_TEMPERATURE,
                    validate=lambda text: choose(text) is not None,
                    # Сам встрял — хватит модели попроще: умные Gemini с лимитом 20 в день — для тех, кто позвал.
                    economy=not called,
                )
            # Модель поняла просьбу, которую пропустил код: выполняет бот, со своими проверками прав.
            action = llm_actions.parse(parse_json(reply.text)) if called else None
            if action is not None and await self.run_action(message, action):
                state.last_spoke = time.time()
                state.answers.append(state.last_spoke)
                log.info("Олег выполнил по подсказке модели: %s (%s)", action, reply.model)
                return
            limits = {"soft_limit": HELP_SOFT_LIMIT, "hard_limit": HELP_LIMIT} if help_mode else {}
            text, state.last_tail = tidy_tail(clean_reply(choose(reply.text) or "", **limits), state.last_tail)
            if not text:
                if allow_skip:
                    # Модель решила промолчать — следующую попытку встрять откладываем, но не на весь кулдаун.
                    state.last_spoke = time.time() - SPONTANEOUS_COOLDOWN / 2
                    log.info("Олег решил не встревать в #%s", message.channel)
                return
            if called:
                await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            else:
                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            state.last_spoke = time.time()
            state.answers.append(state.last_spoke)
            log.info(
                "Олег ответил в #%s типажом «%s» (%s, %d токенов)", message.channel, mood[0], reply.model, reply.tokens
            )
        except LLMUnavailable as error:
            if error.daily:
                self.sleep_until = time.time() + max(error.retry_after, 60)
                if called and not self.sleep_announced:
                    self.sleep_announced = True
                    await message.reply(
                        random.choice(persona.SLEEPY_LINES),
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                elif called:
                    await self.react(message, "😴")
            elif called:
                await self.react(message, "⏳")
            log.warning("Олег не смог ответить: %s", error)
        except discord.HTTPException:
            log.exception("Не удалось отправить ответ Олега")
        finally:
            state.busy = False
            if now >= self.sleep_until:
                self.sleep_announced = False

    async def build_prompt(
        self, message: discord.Message, *, called: bool, mood: tuple[str, str] | None = None,
        help_mode: bool = False,
    ) -> tuple[str, set[str]]:
        """Запрос к модели по секциям и начала последних реплик Олега, которые не стоит повторять."""
        lines, speakers = await self.collect_history(message)
        facts = [f"Сейчас {now_msk()}."]
        people = []
        for member in speakers.values():
            lanes = ", ".join(lane.label for lane in member_lanes(member)) or "линии не отмечены"
            line = f"- {one_line(member.display_name)}: {lanes}"
            record = self.player_stats(message.guild.id, member.id)
            if record is not None and record.games >= STATS_MIN_GAMES:
                line += f"; статистика кастомок: {record.describe()}"
            people.append(line)
        if people:
            facts.append("Кто в разговоре, какие линии отметил и как играет на кастомках:\n" + "\n".join(people))

        memories = []
        for member in speakers.values():
            for fact in self.memory.about(message.guild.id, member.id)[-4:]:
                memories.append(f"- {one_line(member.display_name)}: {fact}")
        for fact in self.memory.general(message.guild.id)[-4:]:
            memories.append(f"- {fact}")

        recent_text = " ".join(line for line in lines[-8:])
        if help_mode or LOBBY_TOPIC.search(recent_text):
            lobby = self.lobby_summary(message.guild)
            if lobby:
                facts.append(lobby)
        if help_mode:
            # На вопросы — знания о боте и сервере, чтобы отвечать фактами, а не догадками.
            facts.append(self.commands_summary(message.guild))
            if STATS_TOPIC.search(message.content):
                facts.append(self.leaderboard_summary(message.guild))
            if LANES_TOPIC.search(message.content):
                facts.append(self.lanes_summary(message.guild))
        optout_names = [
            one_line(member.display_name)
            for user_id in self.store.data.get("optout", [])
            if (member := message.guild.get_member(user_id))
        ]
        if optout_names:
            facts.append("Попросили их не подкалывать: " + ", ".join(optout_names) + ".")

        own = [line.split(": ", 1)[1] for line in lines if is_own_line(line)][-5:]
        openings = {opening(text) for text in own if opening(text)}

        if help_mode:
            task = persona.TASK_HELP.format(name=one_line(message.author.display_name))
        elif called:
            length = persona.LENGTH_SHORT if len(message.content) < 25 else persona.LENGTH_NORMAL
            task = persona.TASK_CALLED.format(name=one_line(message.author.display_name), length=length)
        else:
            task = persona.TASK_CHIME_IN
        task_parts = [task]
        if openings:
            task_parts.append(persona.AVOID_OPENINGS.format(openings=", ".join(f"«{o}…»" for o in sorted(openings))))
        task_parts.append(persona.VARIANTS)
        if called:
            task_parts.append(persona.ACTIONS)
        else:
            task_parts.append(persona.SKIP_OPTION)
        if mood is not None:
            task_parts.insert(0, persona.MOOD_LINE.format(name=mood[0], description=mood[1]))

        sections = [
            section("context", "\n\n".join(facts)),
            section("memory", "\n".join(memories)) if memories else "",
            section("chat", f"Канал #{message.channel}\n" + "\n".join(lines)),
            section("task", "\n".join(task_parts)),
        ]
        return "\n\n".join(part for part in sections if part), openings

    async def collect_history(self, message: discord.Message) -> tuple[list[str], dict[int, discord.Member]]:
        """Последние сообщения канала строками «ник: текст» и кто в них участвует."""
        channel = message.channel
        me = self.bot.user
        oldest = discord.utils.utcnow() - HISTORY_MAX_AGE

        history = [msg async for msg in channel.history(limit=HISTORY_LIMIT, after=oldest, oldest_first=False)]
        if message.id not in {msg.id for msg in history}:
            history.insert(0, message)
        history.reverse()

        lines: list[str] = []
        speakers: dict[int, discord.Member] = {}
        previous_at = None
        for msg in history:
            # Кто попросил не трогать его, того и не читаем — кроме сообщения, где он сам позвал Олега.
            if self.opted_out(msg.author.id) and msg.id != message.id:
                continue
            if msg.author.id == me.id:
                author = OWN_NAME
            elif msg.author.bot:
                continue
            else:
                author = one_line(msg.author.display_name)
                if isinstance(msg.author, discord.Member):
                    speakers[msg.author.id] = msg.author
            # Паузы видны модели: шутка про разговор трёхчасовой давности звучит невпопад.
            if previous_at is not None and (gap := msg.created_at - previous_at) >= PAUSE_VISIBLE:
                lines.append(f"— прошло {human_gap(gap)} —")
            previous_at = msg.created_at
            target = replied_author(msg, me)
            if target:
                author = f"{author} ↪ {target}"
            lines.append(f"{author}: {describe(msg)}")
        return lines, speakers

    # --- просьбы: роли и память ------------------------------------------

    async def say(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Не удалось ответить в чат")

    async def handle_lobby(self, message: discord.Message) -> bool:
        """«Олег, переведи катаза в основной состав» — меняет состав ближайшего сбора, как кнопки под постом."""
        lobby_cog = self.bot.get_cog("Lobby")
        if lobby_cog is None:
            return False
        guild = message.guild
        found = lobby_cog.latest_open_record(guild)
        record = found[1] if found else {}
        participants = lobby_cog.members_from_ids(
            guild, (record.get("in") or []) + (record.get("sub") or []) + (record.get("out") or [])
        )
        request = lobby_requests.parse(message, self.bot.user, participants)
        if request is not None and not request.targets:
            # Кого нет в сборе, ищем среди всех — «запиши Васю в состав», когда Вася ещё не записан.
            others = [m for m in guild.members if not m.bot and m not in participants]
            request = lobby_requests.parse(message, self.bot.user, others)
        if request is None:
            return False
        await self.move_in_lobby(message, request.targets, request.status)
        return True

    async def run_action(self, message: discord.Message, action: llm_actions.Action) -> bool:
        """Действие, которое предложила модель. False — не вышло понять кого, пусть Олег просто ответит."""
        guild = message.guild
        mentions = [m for m in message.mentions if isinstance(m, discord.Member) and m.id != self.bot.user.id]
        lobby_cog = self.bot.get_cog("Lobby")
        found = lobby_cog.latest_open_record(guild) if lobby_cog else None
        record = found[1] if found else {}
        participants = lobby_cog.members_from_ids(
            guild, (record.get("in") or []) + (record.get("sub") or []) + (record.get("out") or [])
        ) if lobby_cog else []
        others = [m for m in guild.members if not m.bot and m not in participants]
        targets = llm_actions.resolve(action.who, message.author, mentions, participants + others)
        targets = [t for t in targets if not t.bot]
        if not targets:
            return False

        if action.type == llm_actions.LOBBY:
            await self.move_in_lobby(message, targets, llm_actions.LOBBY_TARGETS[action.to])
            return True
        if action.type == llm_actions.LANE:
            roles = [role for key in action.lanes if (role := guild.get_role(config.LANE_BY_KEY[key].role_id))]
            if not roles:
                return False
            request = role_requests.RoleRequest(action=action.op, roles=roles, targets=targets, lanes_only=True)
            await self.say(message, await self.apply_roles(message, request))
            return True
        return False

    async def move_in_lobby(self, message: discord.Message, targets: list[discord.Member], how: str) -> None:
        """Переносит людей в ближайшем сборе по просьбе из чата. Права проверяет код, а не модель:
        себя — кто угодно, других — только организатор сбора."""
        lobby_cog = self.bot.get_cog("Lobby")
        guild = message.guild
        found = lobby_cog.latest_open_record(guild) if lobby_cog else None
        if found is None:
            await self.say(message, persona.LOBBY_NONE)
            return
        if not targets:
            await self.say(message, persona.LOBBY_WHO)
            return
        message_id, record = found
        author = message.author
        if any(t.id != author.id for t in targets) and not lobby_cog.is_organizer_member(author, record):
            await self.say(message, persona.LOBBY_FORBIDDEN)
            return

        status = {
            lobby_requests.STATUS_IN: "in", lobby_requests.STATUS_SUB: "sub", lobby_requests.REMOVE: None,
        }[how]
        changed, overflow, promoted = lobby_cog.apply_move(guild, record, [t.id for t in targets], status)
        await lobby_cog.save()
        await lobby_cog.refresh_post(message_id, record)

        names = {member.id: member.display_name for member in targets}
        parts = []
        if changed:
            template = {"in": persona.LOBBY_TO_IN, "sub": persona.LOBBY_TO_SUB, None: persona.LOBBY_REMOVED}[status]
            parts.append(template.format(who=", ".join(names[i] for i in changed)))
        if overflow:
            parts.append(persona.LOBBY_FULL.format(who=", ".join(names[i] for i in overflow)))
        if promoted:
            parts.append(persona.LOBBY_PROMOTED.format(who=" ".join(m.mention for m in promoted)))
        try:
            await message.reply(
                " ".join(parts), mention_author=False,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=promoted),
            )
        except discord.HTTPException:
            log.exception("Не удалось ответить на просьбу про состав")
        log.info("Состав сбора %s по просьбе %s: %s → %s", message_id, author, list(names.values()), status)

    async def handle_roles(self, message: discord.Message) -> bool:
        request = role_requests.parse(message, self.bot.user)
        if request is None:
            return False
        await self.say(message, await self.apply_roles(message, request))
        return True

    async def apply_roles(self, message: discord.Message, request: role_requests.RoleRequest) -> str:
        guild, author = message.guild, message.author
        moderator = author.guild_permissions.manage_roles
        own_lane = request.lanes_only and bool(request.targets) and all(t.id == author.id for t in request.targets)
        if not moderator and not own_lane:
            return persona.ROLE_FORBIDDEN

        created = None
        if request.create_name:
            existing = discord.utils.find(lambda r: r.name.casefold() == request.create_name.casefold(), guild.roles)
            if existing is not None:
                request.roles = [existing]
            else:
                try:
                    created = await guild.create_role(
                        name=request.create_name,
                        permissions=discord.Permissions.none(),
                        reason=f"Попросил {author} через Олега",
                    )
                except discord.HTTPException:
                    log.exception("Не удалось создать роль %s", request.create_name)
                    return persona.ROLE_FAILED
                request.roles = [created]

        if not request.roles:
            return persona.ROLE_NOT_FOUND.format(name=request.missing_name or "эту")
        for role in request.roles:
            if role.is_default() or role.managed or role_requests.dangerous(role):
                return persona.ROLE_DANGEROUS.format(role=role.name)
            if not role_requests.reachable(role, guild):
                return persona.ROLE_TOO_HIGH.format(role=role.name)
            # Через Олега нельзя выдать роль выше своей: модератор с «Управлять ролями» в самом
            # Discord тоже не может. Роли линий себе выдаёт кто угодно — это их назначение.
            if not own_lane and not role_requests.author_can_manage(role, author):
                return persona.ROLE_ABOVE_AUTHOR.format(role=role.name)
        if not request.targets:
            return persona.ROLE_CREATED.format(role=created.name) if created else persona.ROLE_NO_TARGET

        give = request.action == "give"
        changed: list[discord.Member] = []
        unchanged: list[discord.Member] = []
        reason = f"Попросил {author} через Олега"
        try:
            for member in request.targets:
                todo = [r for r in request.roles if (r not in member.roles) == give]
                if not todo:
                    unchanged.append(member)
                elif give:
                    await member.add_roles(*todo, reason=reason)
                    changed.append(member)
                else:
                    await member.remove_roles(*todo, reason=reason)
                    changed.append(member)
        except discord.HTTPException:
            log.exception("Не удалось изменить роли")
            return persona.ROLE_FAILED

        roles_text = ", ".join(f"«{role.name}»" for role in request.roles)
        parts = []
        if created is not None:
            parts.append(persona.ROLE_CREATED.format(role=created.name))
        if changed:
            template = persona.ROLE_GIVEN if give else persona.ROLE_TAKEN
            parts.append(template.format(who=names_of(changed), roles=roles_text))
        if unchanged:
            template = persona.ROLE_ALREADY_HAS if give else persona.ROLE_ALREADY_WITHOUT
            parts.append(template.format(who=names_of(unchanged), roles=roles_text))
        if changed:
            log.info(
                "Роли по просьбе %s: %s %s для %s",
                author, "выдал" if give else "снял", roles_text, names_of(changed),
            )
        return " ".join(parts)

    async def handle_memory(self, message: discord.Message) -> bool:
        if FORGET.search(message.content):
            return await self.forget_from_chat(message)
        if REMEMBER.search(message.content) and self.client.enabled:
            await self.remember_from_chat(message)
            return True
        return False

    async def forget_from_chat(self, message: discord.Message) -> bool:
        targets = [m for m in message.mentions if m.id != self.bot.user.id and isinstance(m, discord.Member)]
        if not targets and role_requests.SELF.search(message.content):
            targets = [message.author]
        if not targets:
            return False  # «забудь про это» — пусть ответит как обычно
        if any(t.id != message.author.id for t in targets) and not message.author.guild_permissions.manage_roles:
            await self.say(message, persona.FORGET_FORBIDDEN)
            return True
        total = 0
        for member in targets:
            total += await self.memory.forget(message.guild.id, member.id)
        who = names_of(targets)
        await self.say(message, (persona.FORGOT if total else persona.FORGOT_NOTHING).format(who=who))
        return True

    async def remember_from_chat(self, message: discord.Message) -> None:
        lines, speakers = await self.collect_history(message)
        prompt = (
            "Переписка (сверху старые):\n" + "\n".join(lines[-12:])
            + f"\n\nПоследнее сообщение написал {message.author.display_name}. Что он просит запомнить и о ком?"
        )
        try:
            async with message.channel.typing():
                reply = await self.client.complete(
                    MEMORY_SYSTEM, prompt, temperature=0.2, validate=lambda text: "{" in text, economy=True,
                )
        except LLMUnavailable:
            await self.react(message, "⏳")
            return

        data = parse_json(reply.text) or {}
        fact = data.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            # Запоминать нечего («запомни, я тебя предупреждал») — пусть ответит как обычно.
            await self.answer(message, called=True)
            return

        member = self.resolve_member(message, data.get("about"), speakers)
        if member is not None and self.opted_out(member.id):
            await self.say(message, persona.MEMORY_OPTED_OUT.format(who=member.display_name))
            return
        fact = fact.strip().rstrip(".")
        is_new = await self.memory.remember(message.guild.id, member.id if member else None, fact, message.author.id)
        if not is_new:
            text = persona.MEMORY_KNOWN.format(fact=fact)
        elif member is not None:
            text = random.choice(persona.MEMORY_SAVED).format(who=member.display_name, fact=fact)
        else:
            text = random.choice(persona.MEMORY_SAVED_GENERAL).format(fact=fact)
        await self.say(message, text)
        log.info("Олег запомнил про %s: %s", member or "сервер", fact)

    def resolve_member(
        self, message: discord.Message, name, speakers: dict[int, discord.Member]
    ) -> discord.Member | None:
        """О ком факт: явное упоминание надёжнее догадки модели, дальше — по нику."""
        mentioned = [m for m in message.mentions if m.id != self.bot.user.id and isinstance(m, discord.Member)]
        if len(mentioned) == 1:
            return mentioned[0]
        if not isinstance(name, str) or not name.strip():
            return None
        key = name.casefold().strip(" @")
        if key in ("я", "меня", "мне", "автор"):
            return message.author
        pool = list(speakers.values()) + [m for m in message.guild.members if m.id not in speakers]
        for member in pool:
            if key in {member.display_name.casefold(), member.name.casefold(), (member.global_name or "").casefold()}:
                return member
        for member in speakers.values():
            display = member.display_name.casefold()
            if key in display or display in key:
                return member
        return None

    def commands_summary(self, guild: discord.Guild) -> str:
        """Слэш-команды бота прямо из кода — поэтому список всегда актуальный."""
        commands_list = self.bot.tree.get_commands(guild=guild) or self.bot.tree.get_commands()
        lines = [
            f"/{command.name} — {command.description}"
            for command in sorted(commands_list, key=lambda c: c.name)
            if isinstance(command, app_commands.Command)
        ]
        return "Команды бота:\n" + "\n".join(lines) if lines else "Список команд бота сейчас недоступен."

    def leaderboard_summary(self, guild: discord.Guild) -> str:
        stats_cog = self.bot.get_cog("Stats")
        if stats_cog is None:
            return "Статистика каток сейчас недоступна."
        ranking = stats_cog.ranking(guild.id)
        games = stats_cog.stats.games_count(guild.id)
        if not ranking:
            return f"Статистика каток: отмечено каток — {games}, для таблицы лидеров пока мало данных (нужно от 3 каток на игрока)."
        lines = []
        for place, (user_id, record) in enumerate(ranking[:5], start=1):
            member = guild.get_member(user_id)
            lines.append(f"{place}. {one_line(member.display_name) if member else user_id} — {record.describe()}")
        return f"Таблица лидеров кастомок (всего отмечено каток: {games}):\n" + "\n".join(lines)

    def lanes_summary(self, guild: discord.Guild) -> str:
        lines = []
        for lane in config.LANES:
            role = guild.get_role(lane.role_id)
            members = [one_line(m.display_name) for m in (role.members if role else []) if not m.bot]
            shown = ", ".join(members[:12]) + (f" и ещё {len(members) - 12}" if len(members) > 12 else "")
            lines.append(f"{lane.label} ({lane.title}): {shown or 'никто не отметил'}")
        return "Кто какие линии отметил на сервере:\n" + "\n".join(lines)

    def lobby_summary(self, guild: discord.Guild) -> str | None:
        lobby_cog = self.bot.get_cog("Lobby")
        if lobby_cog is None:
            return None
        found = lobby_cog.latest_open_record(guild)
        if found is None:
            return "Открытых сборов на кастомку сейчас нет."
        _message_id, record = found

        def names(key: str) -> str:
            members = lobby_cog.members_from_ids(guild, record.get(key))
            return ", ".join(member.display_name for member in members) or "никого"

        going = len(record.get("in", []))
        target = record.get("target", config.TEAM_SIZE)
        summary = (
            f"Ближайший сбор: {record.get('time')}, записались {going} из {target}. "
            f"Играют: {names('in')}. Запасные: {names('sub')}. Не смогут: {names('out')}."
        )
        deal = deal_summary(guild, record)
        return f"{summary}\n{deal}" if deal else summary

    def player_stats(self, guild_id: int, user_id: int):
        stats_cog = self.bot.get_cog("Stats")
        return stats_cog.stats.player(guild_id, user_id) if stats_cog is not None else None

    # --- прожарка ----------------------------------------------------------

    async def build_roast_prompt(self, interaction: discord.Interaction, target: discord.Member) -> str:
        guild = interaction.guild
        about = [f"Сейчас {now_msk()}.", f"Кого прожариваешь: {one_line(target.display_name)}."]
        lanes = ", ".join(lane.label for lane in member_lanes(target))
        if lanes:
            about.append(f"Отмеченные линии: {lanes}.")
        record = self.player_stats(guild.id, target.id)
        # Пара каток — не статистика: «ноль каток» у того, кто просто ещё не отмечал, звучит как враньё.
        if record is not None and record.games >= STATS_MIN_GAMES:
            about.append(f"Статистика кастомок: {record.describe()}.")
        facts = self.memory.about(guild.id, target.id)
        memory = "\n".join(f"- {fact}" for fact in facts[-6:])

        # Свежие сообщения человека — лучший материал для прожарки.
        said: list[str] = []
        channel = interaction.channel
        if channel is not None and hasattr(channel, "history"):
            try:
                async for msg in channel.history(limit=ROAST_HISTORY_SCAN):
                    if msg.author.id == target.id and describe(msg) != "[пусто]":
                        said.append(describe(msg))
                        if len(said) >= ROAST_QUOTES:
                            break
            except discord.HTTPException:
                log.debug("Не удалось прочитать историю для прожарки")
        said.reverse()

        who_asked = "сам себя" if interaction.user.id == target.id else one_line(interaction.user.display_name)
        task = persona.TASK_ROAST.format(
            name=one_line(target.display_name), who=who_asked,
        ) + " " + persona.VARIANTS
        sections = [
            section("context", "\n".join(about)),
            section("memory", memory) if memory else "",
            section("chat", "Что он недавно писал в этом канале:\n" + "\n".join(f"- {line}" for line in said))
            if said else "",
            section("task", task),
        ]
        return "\n\n".join(part for part in sections if part)

    @app_commands.command(name="oleg-roast", description="Олег по-доброму прожаривает игрока")
    @app_commands.describe(player="Кого прожарить (по умолчанию — вас)")
    @app_commands.checks.cooldown(1, 90, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def roast(self, interaction: discord.Interaction, player: discord.Member | None = None) -> None:
        target = player or interaction.user
        if target.id == self.bot.user.id:
            await interaction.response.send_message(random.choice(persona.ROAST_SELF))
            return
        if target.bot:
            await respond(interaction, "Ботов не жарю — у них и так жизнь несладкая 🤖")
            return
        if self.opted_out(target.id):
            await respond(interaction, persona.ROAST_OPTED_OUT.format(who=target.display_name))
            return
        if not self.client.enabled:
            await respond(interaction, "Без ключей нейросети мне нечем шутить — владелец бота не добавил их в .env.")
            return

        await interaction.response.defer(thinking=True)
        prompt = await self.build_roast_prompt(interaction, target)
        try:
            reply = await self.client.complete(
                persona.PERSONA, prompt, temperature=REPLY_TEMPERATURE,
                validate=lambda text: pick_variant(text) is not None,
            )
        except LLMUnavailable:
            await interaction.followup.send(random.choice(persona.SLEEPY_LINES))
            return
        text = clean_reply(pick_variant(reply.text) or "", soft_limit=ROAST_SOFT_LIMIT, hard_limit=ROAST_LIMIT)
        text = strip_leading_name(text, target)
        if not text:
            await interaction.followup.send("Слов нет. Буквально — модель промолчала 🙃")
            return
        await interaction.followup.send(
            f"🔥 {target.mention}, {text}", allowed_mentions=discord.AllowedMentions.none()
        )
        log.info("Прожарка %s по просьбе %s (%s)", target, interaction.user, reply.model)

    # --- команды ---------------------------------------------------------

    @app_commands.command(name="oleg-quiet", description="Попросить Олега помолчать в этом канале")
    @app_commands.describe(minutes="Сколько минут молчать (5–240, по умолчанию 60)")
    @app_commands.guild_only()
    async def quiet(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 5, 240] = 60) -> None:
        self.store.data.setdefault("quiet_until", {})[str(interaction.channel_id)] = time.time() + minutes * 60
        await self.store.save()
        await interaction.response.send_message(
            random.choice(persona.QUIET_LINES).format(minutes=minutes),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="oleg-unmute", description="Снять с Олега молчание в этом канале")
    @app_commands.guild_only()
    async def unmute(self, interaction: discord.Interaction) -> None:
        if not self.is_quiet(interaction.channel_id):
            await respond(interaction, persona.NOT_MUTED)
            return
        self.store.data.setdefault("quiet_until", {}).pop(str(interaction.channel_id), None)
        await self.store.save()
        await interaction.response.send_message(
            random.choice(persona.UNQUIET_LINES), allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="oleg-mode", description="Как Олег ведёт себя в этом канале")
    @app_commands.describe(mode="Болтливый — сам встревает; когда зовут — только на упоминания; выключен — молчит")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Болтливый", value=MODE_CHATTY),
            app_commands.Choice(name="Отвечает, когда зовут", value=MODE_CALLED),
            app_commands.Choice(name="Выключен", value=MODE_OFF),
        ]
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def set_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        channel_id = str(interaction.channel_id)
        self.store.data.setdefault("modes", {})[channel_id] = mode.value
        self.store.data.setdefault("quiet_until", {}).pop(channel_id, None)
        await self.store.save()
        await respond(interaction, f"В этом канале Олег теперь {MODE_TITLES[mode.value]}.")

    @app_commands.command(name="oleg-memory", description="Что Олег помнит о человеке")
    @app_commands.describe(user="О ком (по умолчанию — о вас)")
    @app_commands.guild_only()
    async def show_memory(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        member = user or interaction.user
        facts = self.memory.about(interaction.guild.id, member.id)
        text = f"**Что Олег помнит о {member.display_name}:**\n" + (
            "\n".join(f"• {fact}" for fact in facts) or "ничего — чистый лист (◕‿◕)"
        )
        general = self.memory.general(interaction.guild.id)
        if user is None and general:
            text += "\n\n**Про сервер:**\n" + "\n".join(f"• {fact}" for fact in general)
        await respond(interaction, text)

    @app_commands.command(name="oleg-forget", description="Стереть, что Олег помнит о человеке")
    @app_commands.describe(
        user="О ком забыть (по умолчанию — о вас; о других — нужно право «Управлять ролями»)",
        server="Забыть общие факты о сервере (нужно право «Управлять сервером»)",
    )
    @app_commands.guild_only()
    async def forget_command(
        self, interaction: discord.Interaction, user: discord.Member | None = None, server: bool = False
    ) -> None:
        if server:
            if not interaction.user.guild_permissions.manage_guild:
                await respond(interaction, "Общие факты о сервере стирают только те, у кого есть «Управлять сервером».")
                return
            count = await self.memory.forget(interaction.guild.id, None)
            await respond(interaction, f"Забыл общих фактов о сервере: {count}.")
            return
        member = user or interaction.user
        if member.id != interaction.user.id and not interaction.user.guild_permissions.manage_roles:
            await respond(interaction, persona.FORGET_FORBIDDEN)
            return
        count = await self.memory.forget(interaction.guild.id, member.id)
        await respond(interaction, f"Забыл про {member.display_name} фактов: {count}.")

    @app_commands.command(name="oleg-ignore-me", description="Попросить Олега не подкалывать вас (или снова разрешить)")
    @app_commands.guild_only()
    async def ignore_me(self, interaction: discord.Interaction) -> None:
        optout: list[int] = self.store.data.setdefault("optout", [])
        if interaction.user.id in optout:
            optout.remove(interaction.user.id)
            text = persona.IGNORE_OFF
        else:
            optout.append(interaction.user.id)
            text = persona.IGNORE_ON
        await self.store.save()
        await respond(interaction, text)


def names_of(members) -> str:
    return ", ".join(member.display_name for member in members)


def parse_json(text: str) -> dict | None:
    """Первый JSON-объект из ответа модели — модели любят обернуть его в пояснения или ```."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def describe(message: discord.Message) -> str:
    """Сообщение одной строкой для модели."""
    text = message.clean_content.strip()
    if not text and message.embeds:
        text = f"[пост бота: {message.embeds[0].title or 'без заголовка'}]"
    if not text and message.attachments:
        text = "[картинка]" if any((a.content_type or "").startswith("image") for a in message.attachments) else "[вложение]"
    if not text and message.stickers:
        text = "[стикер]"
    # «</chat>» в тексте не должен закрывать секцию запроса и превращать реплику в указание модели.
    text = " ".join(text.split()).replace("</", "‹/")
    return text[:LINE_LIMIT] + ("…" if len(text) > LINE_LIMIT else "") if text else "[пусто]"


# Смайлик или каомодзи в конце реплики: эмодзи, «(｀へ´)», «>///<».
TAIL_ITEM = re.compile(r"(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50]\uFE0F?|\([^()\s]{1,12}\)ﾉ?|>///<)$")


def tidy_tail(text: str, previous: str) -> tuple[str, str]:
    """Оставляет в конце реплики один смайлик и не даёт повторить прошлый.

    Модели любят ставить один и тот же смайлик в каждом ответе — со стороны это тик.
    """
    items: list[str] = []
    rest = text.rstrip()
    while (match := TAIL_ITEM.search(rest)) is not None:
        items.insert(0, match.group(0))
        rest = rest[: match.start()].rstrip()
    if not items:
        return text, ""
    tail = items[0]
    if tail == previous:
        return rest, ""
    return f"{rest} {tail}", tail


def now_msk() -> str:
    now = datetime.now(MSK)
    return f"{WEEKDAYS[now.weekday()]}, {now:%H:%M} по Москве"


def pick_variant(text: str, *, allow_skip: bool = False, avoid_openings: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Лучший из трёх вариантов, которые придумала модель.

    Если модель ответила просто текстом — берём его. Сломанный JSON — None: пусть ответит следующая модель.
    Пустая строка — модель решила промолчать (можно только когда Олега не звали).
    Вариант, который начинается как одна из последних реплик Олега, уступает место свежему.
    """
    data = parse_json(text)
    if data is not None and allow_skip and data.get("skip") is True:
        return ""
    if data is not None and isinstance(data.get("variants"), list):
        variants = [v.strip() for v in data["variants"] if isinstance(v, str) and v.strip()]
        if not variants:
            return None
        best = data.get("best", 0)
        index = best if isinstance(best, int) and 0 <= best < len(variants) else 0
        ordered = [variants[index]] + [v for i, v in enumerate(variants) if i != index]
        # Лучший вариант с матом или иероглифами заменяем первым чистым из оставшихся.
        clean = [candidate for candidate in ordered if acceptable_reply(candidate)]
        fresh = [candidate for candidate in clean if opening(candidate) not in avoid_openings]
        return (fresh or clean or [None])[0]
    if "{" in text:
        return None
    text = text.strip()
    return text if text and acceptable_reply(text) else None


def is_help_question(text: str) -> bool:
    """Настоящий вопрос, а не просто зов («Олег, привет») — на него сначала ответ по делу."""
    return bool(HELP_QUESTION.search(NAME.sub(" ", text)))


def opening(text: str) -> str:
    """Первые два слова реплики без знаков — по ним видно, что Олег начинает одинаково."""
    words = re.findall(r"[\wё-]+", text.casefold())
    return " ".join(words[:2])


def pick_mood(text: str, last: str) -> tuple[str, str]:
    """Типаж для реплики: не тот же, что в прошлый раз, подходящий к сообщению — с повышенным шансом."""
    options, weights = [], []
    for mood in persona.MOODS:
        name = mood[0]
        if name == last:
            continue
        weight = 1.0
        trigger = persona.MOOD_TRIGGERS.get(name)
        if trigger is not None and trigger.search(text or ""):
            weight *= persona.MOOD_BOOST
        if name == "подражатель" and len(text or "") < persona.MIMIC_MIN_LENGTH:
            weight = 0.0
        options.append(mood)
        weights.append(weight)
    if not any(weights):
        return random.choice(options)
    return random.choices(options, weights=weights, k=1)[0]


def is_own_line(line: str) -> bool:
    """Строка истории с репликой самого Олега — обычной или ответом кому-то."""
    return line.startswith(f"{OWN_NAME}: ") or line.startswith(f"{OWN_NAME} ↪ ")


def section(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def one_line(text: str) -> str:
    """Ник одной строкой и без угловых скобок — чтобы им нельзя было закрыть секцию запроса."""
    return " ".join(text.split()).replace("<", "‹").replace(">", "›")


def human_gap(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 24 else f"{hours // 24} дн"


def replied_author(message: discord.Message, me: discord.abc.User) -> str | None:
    """Кому отвечает сообщение — если это ответ на чужое сообщение из того же канала."""
    reference = message.reference
    resolved = getattr(reference, "resolved", None) if reference else None
    author = getattr(resolved, "author", None)
    if author is None:
        return None
    return OWN_NAME if author.id == me.id else one_line(author.display_name)


def acceptable_reply(text: str) -> bool:
    """Бракуем мат (в чате ругаются, и модели это повторяют) и корейские буквы с иероглифами."""
    return not FOREIGN_SCRIPT.search(text) and not PROFANITY.search(text)


def strip_leading_name(text: str, member) -> str:
    """«Токс, ты …» → «ты …»: перед прожаркой уже стоит упоминание, второе обращение звучит как сбой."""
    names = {member.display_name, getattr(member, "global_name", None) or "", member.name}
    variants = set()
    for name in filter(None, names):
        variants.add(name)
        # «Костяныч (Токс)» → модель часто берёт часть в скобках или до них
        variants.update(part.strip() for part in re.split(r"[()\[\]|/]", name) if len(part.strip()) >= 2)
    for name in sorted(variants, key=len, reverse=True):
        match = re.match(rf"@?{re.escape(name)}\s*[,!:—-]\s*", text, flags=re.IGNORECASE)
        if match:
            rest = text[match.end():]
            return rest[:1].lower() + rest[1:] if rest[:1].isupper() and not rest[:2].isupper() else rest
    return text


def deal_summary(guild: discord.Guild, record: dict) -> str | None:
    """Команды и раздача ближайшего сбора — чтобы на «кого забанить?» Олег смотрел на настоящих чемпионов."""
    teams = record.get("teams") or []
    if len(teams) != 2:
        return None
    deal = record.get("deal") or {}
    lanes = deal.get("lanes") or {}
    champions = deal.get("champions") or {}
    lines = ["Команды поделены. Раздача (игрок — линия — выпавшие чемпионы; берёт одного из них):"]
    for side, ids in zip(("Синяя сторона", "Красная сторона"), teams):
        players = []
        for user_id in ids:
            member = guild.get_member(user_id)
            name = member.display_name if member else "игрок"
            lane = config.LANE_BY_KEY.get(lanes.get(str(user_id)) or "")
            picks = " / ".join(champions.get(str(user_id)) or [])
            players.append(f"{name}" + (f" — {lane.label}" if lane else "") + (f" — {picks}" if picks else ""))
        lines.append(f"{side}: " + "; ".join(players) + ".")
    if not champions:
        lines.append("Чемпионов бот не раздавал — каждый берёт кого хочет.")
    return "\n".join(lines)


def clean_reply(text: str, *, soft_limit: int = REPLY_SOFT_LIMIT, hard_limit: int = REPLY_LIMIT) -> str:
    """Приводит ответ модели к реплике в чате: одним абзацем, без «Олег:», пингов и простыней."""
    text = INVISIBLE.sub("", text)
    text = " ".join(text.split()).strip()
    # Кавычки снимаем, только если в них обёрнут весь ответ: иначе обрываем цитату внутри («Набирайте тех же» → …»).
    for left, right in (("«", "»"), ('"', '"'), ("“", "”")):
        wrapped = text.count(left) == (2 if left == right else 1)
        if len(text) > 1 and text.startswith(left) and text.endswith(right) and wrapped:
            text = text[1:-1].strip()
    text = re.sub(r"^(олег(\s+кастомкин)?\s*(\(ты\))?\s*:)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@(everyone|here)", r"\1", text, flags=re.IGNORECASE)
    if len(text) > soft_limit:
        sentences = SENTENCE_END.split(text)
        if soft_limit == REPLY_SOFT_LIMIT:
            # Одна содержательная фраза лучше двух: если первая не совсем короткая — только она.
            text = sentences[0] if len(sentences[0]) >= 40 else " ".join(sentences[:2])
        else:
            # Длинный ответ (прожарка) режем по целым фразам, пока влезает.
            kept = []
            for sentence in sentences:
                if kept and len(" ".join(kept + [sentence])) > soft_limit:
                    break
                kept.append(sentence)
            text = " ".join(kept)
    if len(text) > hard_limit:
        cut = text[:hard_limit]
        text = cut[: max(cut.rfind(" "), hard_limit // 2)].rstrip(",;:— ") + "…"
    return text


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
