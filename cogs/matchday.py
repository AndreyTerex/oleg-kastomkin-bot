"""Игровой день: ежедневное приглашение на кастомку и результат катки по скриншоту.

- Каждый день в LOBBY_INVITE_HOUR, если на сервере нет открытого сбора и сегодня его не создавали, Олег
  зовёт народ в ANNOUNCE_CHANNEL_ID (или туда, где отмечали последнюю катку) с кнопкой «Я бы сыграл»
  и ссылкой на /custom.
- Скриншот итогов катки в канале сбора (от игрока или организатора) Олег разбирает через Gemini: угадывает
  победившую сторону, хвалит лучший KDA и предлагает кнопки записать результат. Пишет в статистику
  только организатор — тем же путём, что и кнопки в посте сбора.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands, tasks

import config
import screenshots
from cogs.lobby import LOBBY_STALE_AFTER, report_result
from llm import LLMUnavailable, image_message
from storage import JsonStore
from utils import BLUE_SIDE, RED_SIDE, player_name

log = logging.getLogger("scrimbot.matchday")

SHOT_COOLDOWN = 60
INVITE_TIMEOUT = 6 * 60 * 60
INVITE_LAST_HOUR = 23
INVITE_FULL = 10


def invite_due(now: datetime, posted: str | None, has_lobby_today: bool) -> bool:
    if config.LOBBY_INVITE_HOUR < 0 or has_lobby_today:
        return False
    return config.LOBBY_INVITE_HOUR <= now.hour < INVITE_LAST_HOUR and posted != now.strftime("%Y-%m-%d")


class InviteView(discord.ui.View):
    """«Я бы сыграл»: видно, сколько желающих набирается, до сбора через /custom."""

    def __init__(self, custom: str) -> None:
        super().__init__(timeout=INVITE_TIMEOUT)
        self.custom = custom
        self.base = ""
        self.people: dict[int, str] = {}
        self.full_announced = False

    def text(self) -> str:
        if not self.people:
            return self.base
        names = ", ".join(self.people.values())
        return f"{self.base}\n\n🙋 Хотят играть ({len(self.people)}): {names}"

    @discord.ui.button(label="Я бы сыграл", emoji="🙋", style=discord.ButtonStyle.success)
    async def want(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id in self.people:
            self.people.pop(interaction.user.id)
        else:
            self.people[interaction.user.id] = player_name(interaction.user)
        await interaction.response.edit_message(content=self.text(), view=self)
        if len(self.people) >= INVITE_FULL and not self.full_announced:
            self.full_announced = True
            mentions = " ".join(f"<@{user_id}>" for user_id in self.people)
            await interaction.followup.send(
                f"🎉 Набралось {len(self.people)} желающих! Кто-нибудь, запустите {self.custom} — {mentions}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )


class ResultView(discord.ui.View):
    """Подтверждение результата по скриншоту. Права проверяет report_result (автор сбора/организатор)."""

    def __init__(self, lobby_id: int, guess: int | None) -> None:
        super().__init__(timeout=30 * 60)
        self.lobby_id = lobby_id
        blue = discord.ui.Button(
            label="Победили синие", emoji="🔵",
            style=discord.ButtonStyle.primary if guess == 0 else discord.ButtonStyle.secondary,
        )
        red = discord.ui.Button(
            label="Победили красные", emoji="🔴",
            style=discord.ButtonStyle.danger if guess == 1 else discord.ButtonStyle.secondary,
        )
        skip = discord.ui.Button(label="Не записывать", style=discord.ButtonStyle.secondary)
        blue.callback = lambda interaction: report_result(interaction, self.lobby_id, 0)
        red.callback = lambda interaction: report_result(interaction, self.lobby_id, 1)
        skip.callback = self.skip
        for button in (blue, red, skip):
            self.add_item(button)

    async def skip(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Lobby")
        record = cog.get_record(self.lobby_id) if cog else None
        if record is not None and not cog.is_organizer(interaction, record):
            await interaction.response.send_message("Это решает автор сбора или организатор.", ephemeral=True)
            return
        await interaction.response.edit_message(view=None)
        self.stop()


class Matchday(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.invites = JsonStore(Path(config.DATA_DIR) / "invites.json")
        self.shot_at: dict[str, float] = {}

    async def cog_load(self) -> None:
        self.invites.load()
        self.invite_loop.start()

    async def cog_unload(self) -> None:
        self.invite_loop.cancel()

    # --- приглашение ----------------------------------------------------------

    def lobbies(self, guild_id: int) -> list[tuple[str, dict]]:
        cog = self.bot.get_cog("Lobby")
        if cog is None:
            return []
        return [(key, record) for key, record in cog.store.data.items() if record.get("guild_id") == guild_id]

    def lobby_today(self, guild_id: int, now: datetime) -> bool:
        today = now.date()
        for _key, record in self.lobbies(guild_id):
            created = datetime.fromtimestamp(record.get("created_at", 0), config.TIMEZONE).date()
            start = record.get("start_at")
            starts_today = start and datetime.fromtimestamp(start, config.TIMEZONE).date() == today
            if created == today or starts_today:
                return True
            if not record.get("closed") and time.time() - record.get("created_at", 0) < LOBBY_STALE_AFTER:
                return True
        return False

    def announce_channel(self, guild: discord.Guild):
        stats = self.bot.get_cog("Stats")
        if stats is not None:
            return stats.announce_channel(guild.id)
        channel = self.bot.get_channel(config.ANNOUNCE_CHANNEL_ID) if config.ANNOUNCE_CHANNEL_ID else None
        return channel if channel is not None and channel.guild.id == guild.id else None

    async def custom_mention(self, guild: discord.Guild) -> str:
        try:
            commands_list = await self.bot.tree.fetch_commands(guild=guild if config.GUILD_ID else None)
        except discord.HTTPException:
            return "`/custom`"
        command = next((c for c in commands_list if c.name == "custom"), None)
        return command.mention if command else "`/custom`"

    @tasks.loop(minutes=10)
    async def invite_loop(self) -> None:
        now = datetime.now(config.TIMEZONE)
        for guild in self.bot.guilds:
            posted = self.invites.data.get(str(guild.id))
            if not invite_due(now, posted, self.lobby_today(guild.id, now)):
                continue
            self.invites.data[str(guild.id)] = now.strftime("%Y-%m-%d")
            await self.invites.save()
            try:
                await self.post_invite(guild)
            except Exception:
                log.exception("Не удалось позвать на кастомку на %s", guild)

    @invite_loop.before_loop
    async def _before_invite(self) -> None:
        await self.bot.wait_until_ready()

    async def post_invite(self, guild: discord.Guild) -> None:
        channel = self.announce_channel(guild)
        if channel is None:
            log.info("Приглашение на кастомку на %s: некуда писать (задайте ANNOUNCE_CHANNEL_ID)", guild)
            return
        custom = await self.custom_mention(guild)
        text = None
        chat = self.bot.get_cog("Chat")
        if chat is not None and hasattr(chat, "oleg_line"):
            text = await chat.oleg_line(
                "Сегодня на сервере ещё никто не собрал кастомку по League of Legends. Позови всех в канале "
                "собраться сегодня вечером — мило, заманчиво, 1–2 предложения. Не называй время и людей.",
                limit=300,
            )
        text = text or "Сегодня ещё никто не собрал кастомку 🥺 Может, сыграем вечером?"
        view = InviteView(custom)
        view.base = f"{text}\n-# Жмите «Я бы сыграл», а кто готов организовать — {custom}"
        await channel.send(view.text(), view=view, allowed_mentions=discord.AllowedMentions.none())
        log.info("Олег позвал на кастомку на %s", guild)

    # --- скриншот итогов ------------------------------------------------------

    def lobby_in_channel(self, message: discord.Message) -> tuple[int, dict] | None:
        now = time.time()
        found = [
            (int(key), record) for key, record in self.lobbies(message.guild.id)
            if record.get("channel_id") == message.channel.id and not record.get("closed")
            and len(record.get("teams") or []) == 2
            and now - record.get("created_at", now) < LOBBY_STALE_AFTER
        ]
        return max(found, key=lambda item: item[1].get("created_at", 0)) if found else None

    @staticmethod
    def screenshot(message: discord.Message) -> discord.Attachment | None:
        for attachment in message.attachments:
            if (attachment.content_type or "").split(";")[0] in screenshots.IMAGE_TYPES \
                    and attachment.size <= screenshots.MAX_IMAGE_BYTES:
                return attachment
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.attachments:
            return
        attachment = self.screenshot(message)
        if attachment is None:
            return
        found = self.lobby_in_channel(message)
        if found is None:
            return
        lobby_id, record = found
        lobby_cog = self.bot.get_cog("Lobby")
        players = [user_id for team in record["teams"] for user_id in team]
        if message.author.id not in players and not lobby_cog.is_organizer_member(message.author, record):
            return
        chat = self.bot.get_cog("Chat")
        client = getattr(chat, "client", None)
        if client is None or not any(p.name == "gemini" for p in client.providers):
            return
        if time.time() - self.shot_at.get(str(lobby_id), 0) < SHOT_COOLDOWN:
            return
        self.shot_at[str(lobby_id)] = time.time()
        await self.read_screenshot(message, attachment, lobby_id, record, client)

    async def read_screenshot(self, message, attachment, lobby_id: int, record: dict, client) -> None:
        try:
            await message.add_reaction("👀")
        except discord.HTTPException:
            pass
        try:
            image = await attachment.read()
            reply = await client.complete(
                screenshots.VISION_SYSTEM,
                image_message(screenshots.VISION_TASK, image, (attachment.content_type or "image/png").split(";")[0]),
                temperature=0.1, only={"gemini"}, validate=lambda text: "{" in text,
            )
        except (LLMUnavailable, discord.HTTPException) as error:
            log.info("Скриншот в #%s не разобрал: %s", message.channel, error)
            await self._unreact(message)
            return
        players = screenshots.parse_scoreboard(reply.text)
        await self._unreact(message)
        if not players:
            log.info("Картинка в #%s — не таблица итогов (%s)", message.channel, reply.model)
            return

        guild = message.guild
        deal = (record.get("deal") or {}).get("champions") or {}
        champions = {int(user_id): names for user_id, names in deal.items()}
        names: dict[int, list[str]] = {}
        for team in record["teams"]:
            for user_id in team:
                member = guild.get_member(user_id)
                if member is not None:
                    names[user_id] = [n for n in (member.display_name, member.name, member.global_name) if n]
        guess = screenshots.guess_winner(players, record["teams"], champions, names)
        best = screenshots.best_player(players)

        chat = self.bot.get_cog("Chat")
        lines = [f"📸 Вижу итоги катки {record.get('round', 1)}!"]
        if guess is not None:
            lines.append(f"Похоже, победили **{(BLUE_SIDE, RED_SIDE)[guess]}**.")
        else:
            lines.append("Не понял, какая это сторона в сборе, — отметьте кнопкой.")
        if best is not None:
            lines.append(f"Лучший KDA — **{best.name}** на {best.champion}: {best.kills}/{best.deaths}/{best.assists}.")
        if chat is not None and hasattr(chat, "oleg_line"):
            worst = max(players, key=lambda p: (p.deaths, -p.kda))
            comment = await chat.oleg_line(
                "По скриншоту итогов катки на кастомке: " + "; ".join(p.line() for p in players[:10])
                + f". Лучший KDA — {best.line() if best else '—'}, больше всех умирал — {worst.line()}. "
                "Одной-двумя фразами похвали лучшего и по-доброму подколи того, кто больше всех умирал. "
                "Цифры не выдумывай.",
                economy=True, limit=300,
            )
            if comment:
                lines.append(comment)
        lines.append("-# Записывает результат автор сбора или организатор.")
        await message.reply(
            "\n".join(lines), view=ResultView(lobby_id, guess),
            mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info("Скриншот итогов в #%s разобран (%s), победа: %s", message.channel, reply.model, guess)

    async def _unreact(self, message: discord.Message) -> None:
        try:
            await message.remove_reaction("👀", self.bot.user)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Matchday(bot))
