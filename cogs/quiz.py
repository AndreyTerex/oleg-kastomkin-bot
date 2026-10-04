"""Викторина в чате: раз в QUIZ_MINUTES, если в канале живое общение, Олежка загадывает чемпиона —
по титулу, умению, описанию или кусочку портрета. Кто первым ответил — очко и QUIZ_COINS коинов.
/quiz — вопрос прямо сейчас, /quiz-top — таблица знатоков."""
from __future__ import annotations

import asyncio
import io
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import portraits
import quiz
from champions import CHAMPIONS_URL, POOL, ChampionsUnavailable
from storage import JsonStore
from utils import NEUTRAL, player_name, respond

log = logging.getLogger("scrimbot.quiz")

DETAILS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/ru_RU/champion/{champion_id}.json"
CORRECT_LINES = (
    "Правильно, {who}! Это {answer} ✨ +1 очко и {coins} коинов",
    "{who} угадал(а)! {answer}, конечно~ +{coins} 💰",
    "Ну всё, {who} — знаток! Это {answer} (≧▽≦) +{coins} коинов",
)
TIMEOUT_LINES = (
    "Никто не угадал (｡•́︿•̀｡) Это был **{answer}**!",
    "Время вышло~ Правильный ответ — **{answer}**. Учим чемпионов, солнышки!",
)


@dataclass
class Active:
    question: quiz.Question
    started: float
    task: asyncio.Task | None = None


def in_hours(hour: int, spec: str) -> bool:
    """«12-24» → с 12:00 до полуночи; «20-3» — через полночь."""
    try:
        start, end = (int(part) for part in spec.split("-", 1))
    except ValueError:
        return True
    start, end = start % 24, end % 24 if end != 24 else 24
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class Quiz(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(Path(config.DATA_DIR) / "quiz.json")
        self.active: dict[int, Active] = {}
        self.activity: dict[int, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._catalog: tuple[str | None, dict] = (None, {})

    async def cog_load(self) -> None:
        self.store.load()
        if config.QUIZ_MINUTES:
            self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()
        for active in self.active.values():
            if active.task:
                active.task.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # --- данные ---------------------------------------------------------------

    async def _json(self, url: str):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        async with self._session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def catalog(self) -> dict:
        """champion.json (ru_RU) текущего патча: титулы и описания."""
        await POOL.all()
        version = POOL.version
        if self._catalog[0] != version:
            payload = await self._json(CHAMPIONS_URL.format(version=version))
            self._catalog = (version, payload["data"])
        return self._catalog[1]

    async def make_question(self) -> quiz.Question | None:
        catalog = await self.catalog()
        champions = {champion.id: champion for champion in await POOL.all()}
        for _ in range(6):
            champion_id = random.choice(list(catalog))
            entry = catalog[champion_id]
            kind = random.choice(quiz.QUESTION_KINDS)
            details = portrait = None
            if kind == "spell":
                data = await self._json(DETAILS_URL.format(version=POOL.version, champion_id=champion_id))
                details = (data.get("data") or {}).get(champion_id)
            elif kind == "portrait" and champion_id in champions:
                portrait = await portraits._portrait(champions[champion_id])
            question = quiz.build_question(kind, entry, details, portrait)
            if question:
                return question
        return None

    # --- ход викторины ----------------------------------------------------------

    def _guild(self, guild_id: int) -> dict:
        guild = self.store.data.setdefault(str(guild_id), {})
        guild.setdefault("scores", {})
        return guild

    def channel_for(self, guild: discord.Guild):
        if config.QUIZ_CHANNEL_ID:
            channel = self.bot.get_channel(config.QUIZ_CHANNEL_ID)
            if channel is not None and getattr(channel, "guild", None) == guild:
                return channel
        stats = self.bot.get_cog("Stats")
        return stats.announce_channel(guild.id) if stats is not None else None

    @tasks.loop(minutes=5)
    async def loop(self) -> None:
        now = time.time()
        hour = datetime.now(config.TIMEZONE).hour
        if not in_hours(hour, config.QUIZ_HOURS):
            return
        for guild in self.bot.guilds:
            data = self._guild(guild.id)
            if now - data.get("last_at", 0) < config.QUIZ_MINUTES * 60:
                continue
            channel = self.channel_for(guild)
            if channel is None or channel.id in self.active:
                continue
            # В пустой канал не пишем: викторина — для живой компании.
            if now - self.activity.get(channel.id, 0) > config.QUIZ_ACTIVE_MINUTES * 60:
                continue
            await self.ask(channel)

    @loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def ask(self, channel) -> bool:
        try:
            question = await self.make_question()
        except (aiohttp.ClientError, ChampionsUnavailable, TimeoutError, KeyError) as error:
            log.info("Вопрос викторины не собрался: %s", type(error).__name__)
            return False
        if question is None:
            return False
        self._guild(channel.guild.id)["last_at"] = time.time()
        await self.store.save()
        embed = discord.Embed(
            title="🧠 Викторина от Олежки",
            description=f"{question.text}\n-# Пиши ответ в чат — у вас {config.QUIZ_ANSWER_SECONDS} секунд!",
            color=NEUTRAL,
        )
        kwargs = {}
        if question.image:
            embed.set_image(url="attachment://quiz.png")
            kwargs["file"] = discord.File(io.BytesIO(question.image), filename="quiz.png")
        try:
            await channel.send(embed=embed, **kwargs)
        except discord.HTTPException:
            log.warning("Не удалось задать вопрос викторины в #%s", channel)
            return False
        active = Active(question, time.time())
        self.active[channel.id] = active
        active.task = asyncio.create_task(self._timer(channel, active))
        log.info("Викторина в #%s: %s → %s", channel, question.kind, question.answer)
        return True

    async def _timer(self, channel, active: Active) -> None:
        try:
            await asyncio.sleep(config.QUIZ_ANSWER_SECONDS / 2)
            if self.active.get(channel.id) is active:
                await channel.send(f"💡 Подсказка: `{active.question.hint}`")
            await asyncio.sleep(config.QUIZ_ANSWER_SECONDS / 2)
            if self.active.get(channel.id) is active:
                self.active.pop(channel.id, None)
                await channel.send(random.choice(TIMEOUT_LINES).format(answer=active.question.answer))
        except asyncio.CancelledError:
            pass
        except discord.HTTPException:
            self.active.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        self.activity[message.channel.id] = time.time()
        active = self.active.get(message.channel.id)
        if active is None or not quiz.is_correct(message.content, active.question.answer,
                                                 (active.question.champion_id,)):
            return
        self.active.pop(message.channel.id, None)
        if active.task:
            active.task.cancel()
        scores = self._guild(message.guild.id)["scores"]
        scores[str(message.author.id)] = scores.get(str(message.author.id), 0) + 1
        await self.store.save()
        stats = self.bot.get_cog("Stats")
        coins = config.QUIZ_COINS if stats is not None else 0
        if coins:
            stats.wallets.add(message.guild.id, message.author.id, coins)
            await stats.wallets.save()
        line = random.choice(CORRECT_LINES).format(
            who=player_name(message.author), answer=active.question.answer, coins=coins,
        )
        try:
            await message.reply(line, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # --- команды ------------------------------------------------------------------

    @app_commands.command(name="quiz", description="Викторина: Олежка загадает чемпиона прямо сейчас")
    @app_commands.guild_only()
    async def quiz_now(self, interaction: discord.Interaction) -> None:
        if interaction.channel_id in self.active:
            await respond(interaction, "Вопрос уже висит — отвечайте в чат!")
            return
        await interaction.response.send_message("Загадываю… 🤔", ephemeral=True)
        if not await self.ask(interaction.channel):
            await interaction.followup.send("Не получилось собрать вопрос — Data Dragon не отвечает.", ephemeral=True)

    @app_commands.command(name="quiz-top", description="Лучшие знатоки викторины")
    @app_commands.guild_only()
    async def quiz_top(self, interaction: discord.Interaction) -> None:
        scores = self._guild(interaction.guild.id)["scores"]
        if not scores:
            await respond(interaction, "Никто ещё ничего не угадал — /quiz, и вперёд!")
            return
        ranked = sorted(scores.items(), key=lambda item: -item[1])[:10]
        lines = []
        for index, (user_id, points) in enumerate(ranked, start=1):
            member = interaction.guild.get_member(int(user_id))
            lines.append(f"`{index}.` **{player_name(member) if member else f'<@{user_id}>'}** — {points}")
        embed = discord.Embed(title="🧠 Знатоки чемпионов", description="\n".join(lines), color=NEUTRAL)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quiz(bot))
