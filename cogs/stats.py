"""Статистика каток: /stats, /leaderboard и итоги недели по субботам. Результаты отмечаются кнопками в посте сбора."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from bets import Wallets
from stats import Stats, main_rivalry, plural, rivals, week_highlights, week_lines
from storage import JsonStore
from utils import NEUTRAL, player_name, respond

log = logging.getLogger("scrimbot.stats")

# В таблицу лидеров попадают только те, кто сыграл хотя бы столько каток — иначе 1/1 = 100% всех обгонит.
LEADERBOARD_MIN_GAMES = 3
# Сколько мест показывать: все, кто набрал минимум каток, но не больше этого (ограничение Discord на длину).
LEADERBOARD_SIZE = 40
MEDALS = ("🥇", "🥈", "🥉")
RECAP_MIN_GAMES = 3
RECENT_GAMES = 5
WEEK = 7 * 86400


def recent_line(won: bool, delta: float, at: float) -> str:
    sign = "+" if delta >= 0 else "−"
    return f"{'🟢' if won else '🔴'} `{sign}{abs(delta):.0f}` · <t:{int(at)}:R>"


def rival_line(rival, name, nemesis: bool) -> str:
    score = f"{rival.wins}:{rival.losses}"
    if nemesis:
        return f"😈 Неудобный соперник: **{name(rival.user_id)}** — проиграл ему {rival.losses} из {rival.games} ({score})"
    return f"🎯 Любимая жертва: **{name(rival.user_id)}** — обыграл его {rival.wins} из {rival.games} ({score})"


def week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def recap_due(now: datetime, posted: str | None) -> bool:
    """Пора ли публиковать итоги: нужный день, не раньше нужного часа, на этой неделе ещё не публиковали."""
    if config.RECAP_HOUR < 0:
        return False
    return now.weekday() == config.RECAP_WEEKDAY and now.hour >= config.RECAP_HOUR and posted != week_key(now)


def recap_fields(highlights: dict, name) -> list[tuple[str, str]]:
    """Номинации недели строками для embed; name(user_id) → имя."""
    fields = []
    if "most_games" in highlights:
        user_id, line = highlights["most_games"]
        fields.append(("🎮 Больше всех каток", f"**{name(user_id)}** — {line.games}"))
    if "best_winrate" in highlights:
        user_id, line = highlights["best_winrate"]
        fields.append(("📈 Лучший винрейт", f"**{name(user_id)}** — {line.wins}/{line.games} ({line.wins / line.games:.0%})"))
    if "streak" in highlights:
        user_id, line = highlights["streak"]
        fields.append(("🔥 Серия побед", f"**{name(user_id)}** — {line.best_streak} подряд"))
    if "climber" in highlights:
        user_id, line = highlights["climber"]
        fields.append(("🚀 Больше всех поднял Elo", f"**{name(user_id)}** — +{line.elo:.0f}"))
    if "mvp" in highlights:
        user_id, line = highlights["mvp"]
        fields.append(("⭐ Чаще всех MVP", f"**{name(user_id)}** — {line.mvp}"))
    return fields


class StatsCog(commands.Cog, name="Stats"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.stats = Stats(Path(config.DATA_DIR) / "stats.json")
        self.recaps = JsonStore(Path(config.DATA_DIR) / "recap.json")
        self.wallets = Wallets()

    async def cog_load(self) -> None:
        self.stats.load()
        self.recaps.load()
        self.wallets.load()
        self.recap_loop.start()

    async def cog_unload(self) -> None:
        self.recap_loop.cancel()

    # --- итоги недели --------------------------------------------------------

    def announce_channel(self, guild_id: int):
        channel_id = config.ANNOUNCE_CHANNEL_ID or self.stats.last_channel(guild_id)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is not None and getattr(channel, "guild", None) and channel.guild.id == guild_id:
            return channel
        return None

    @tasks.loop(minutes=10)
    async def recap_loop(self) -> None:
        now = datetime.now(config.TIMEZONE)
        for guild in self.bot.guilds:
            posted = self.recaps.data.get(str(guild.id))
            if not recap_due(now, posted):
                continue
            self.recaps.data[str(guild.id)] = week_key(now)
            await self.recaps.save()
            try:
                await self.post_recap(guild)
            except Exception:
                log.exception("Не удалось опубликовать итоги недели на %s", guild)

    @recap_loop.before_loop
    async def _before_recap(self) -> None:
        await self.bot.wait_until_ready()

    async def post_recap(self, guild: discord.Guild) -> None:
        games = self.stats.games_since(guild.id, time.time() - 7 * 86400)
        if not games:
            log.info("Итоги недели на %s: каток не было — молчу", guild)
            return
        channel = self.announce_channel(guild.id)
        if channel is None:
            log.info("Итоги недели на %s: некуда писать (задайте ANNOUNCE_CHANNEL_ID)", guild)
            return

        def name(user_id: int) -> str:
            member = guild.get_member(user_id)
            return player_name(member) if member else "кто-то ушедший"

        highlights = week_highlights(week_lines(games), RECAP_MIN_GAMES)
        fields = recap_fields(highlights, name)
        rivalry = main_rivalry(games)
        if rivalry:
            a, b, wins_a, wins_b = rivalry
            fields.append(("⚔️ Главное противостояние", f"**{name(a)}** {wins_a}:{wins_b} **{name(b)}**"))
        count = f"{len(games)} {plural(len(games), 'катка', 'катки', 'каток')}"
        embed = discord.Embed(title="🗓 Итоги недели на кастомках", color=NEUTRAL)
        for title, value in fields:
            embed.add_field(name=title, value=value, inline=False)
        embed.set_footer(text=f"За неделю сыграно {count} · полная таблица — /leaderboard")

        comment = None
        chat = self.bot.get_cog("Chat")
        if chat is not None and hasattr(chat, "oleg_line"):
            facts = "\n".join(f"- {title}: {value.replace('**', '')}" for title, value in fields)
            comment = await chat.oleg_line(
                "Подведи итоги недели на кастомках сервера для всех в канале: за неделю " + count + ". "
                "Номинации:\n" + facts + "\nПоздравь победителей номинаций по именам, подколи по-доброму, "
                "позови на кастомки на выходных. 2–4 предложения, цифры не выдумывай — только эти."
            )
        embed.description = comment or f"Неделя пролетела: {count}. Вот кто отличился 👇"
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        log.info("Итоги недели опубликованы на %s (%s)", guild, count)

    @app_commands.command(name="stats", description="Статистика каток игрока на этом сервере")
    @app_commands.describe(player="Чья статистика (по умолчанию — ваша)")
    @app_commands.guild_only()
    async def show_stats(self, interaction: discord.Interaction, player: discord.Member | None = None) -> None:
        member = player or interaction.user
        record = self.stats.player(interaction.guild.id, member.id)
        embed = discord.Embed(title=f"📊 {player_name(member)}", color=NEUTRAL)
        if not record.games:
            embed.description = "Ещё ни одной отмеченной катки. Результат отмечает организатор кнопками в посте сбора."
        else:
            embed.add_field(name="Каток", value=str(record.games))
            embed.add_field(name="Победы", value=f"{record.wins} ({record.winrate:.0%})")
            embed.add_field(name="Поражения", value=str(record.losses))
            embed.add_field(name="Elo", value=f"{record.elo:.0f}")
            profile = self.bot.get_cog("Profile")
            rank_line = profile.rank_line(member.id) if profile is not None else ""
            if rank_line:
                embed.add_field(name="Ранг", value=rank_line)
            if record.mvp:
                embed.add_field(name="MVP", value=f"⭐ ×{record.mvp}")
            recent = self.stats.recent_games(interaction.guild.id, member.id, RECENT_GAMES)
            if recent:
                embed.add_field(
                    name="Последние катки (изменение Elo)",
                    value="\n".join(recent_line(won, delta, game.get("at", 0)) for game, won, delta in recent),
                    inline=False,
                )
            rival_text = self.rivals_text(interaction.guild, member.id)
            if rival_text:
                embed.add_field(name=rival_text[0], value=rival_text[1], inline=False)
            place = self.place_of(interaction.guild.id, member.id)
            if place:
                embed.set_footer(text=f"{place} место на сервере по рейтингу")
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    def rivals_text(self, guild: discord.Guild, user_id: int) -> tuple[str, str] | None:
        """Соперники за неделю; если за неделю мало каток — за всё время."""
        def name(other: int) -> str:
            member = guild.get_member(other)
            return player_name(member) if member else "кто-то ушедший"

        for title, games in (
            ("Соперники недели", self.stats.games_since(guild.id, time.time() - WEEK)),
            ("Соперники за всё время", self.stats.games_since(guild.id, 0)),
        ):
            nemesis, victim = rivals(games, user_id)
            lines = [rival_line(r, name, r is nemesis) for r in (nemesis, victim) if r is not None]
            if lines:
                return title, "\n".join(lines)
        return None

    def ranking(self, guild_id: int) -> list[tuple[int, object]]:
        players = [
            (user_id, record)
            for user_id, record in self.stats.all_players(guild_id).items()
            if record.games >= LEADERBOARD_MIN_GAMES
        ]
        return sorted(players, key=lambda item: (-item[1].elo, -item[1].games))

    def newcomers(self, guild_id: int) -> list[tuple[int, object]]:
        """Кто уже играл, но меньше LEADERBOARD_MIN_GAMES каток: больше каток — выше."""
        players = [
            (user_id, record)
            for user_id, record in self.stats.all_players(guild_id).items()
            if 0 < record.games < LEADERBOARD_MIN_GAMES
        ]
        return sorted(players, key=lambda item: (-item[1].games, -item[1].elo))

    def place_of(self, guild_id: int, user_id: int) -> int | None:
        for index, (other_id, _record) in enumerate(self.ranking(guild_id), start=1):
            if other_id == user_id:
                return index
        return None

    @app_commands.command(name="coins", description="Твои Олежкины коины (раз в сутки — бонус)")
    @app_commands.guild_only()
    async def coins(self, interaction: discord.Interaction) -> None:
        bonus = self.wallets.claim_daily(interaction.guild.id, interaction.user.id)
        await self.wallets.save()
        balance = self.wallets.balance(interaction.guild.id, interaction.user.id)
        text = f"💰 У тебя **{balance}** Олежкиных коинов."
        if bonus:
            text += f" Сегодняшний бонус +{bonus} уже на счету ♡"
        else:
            text += " Бонус сегодня уже был — приходи завтра~"
        text += "\n-# Ставки — кнопкой «💰 Ставка» в посте кастомки, после раздачи."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="richest", description="Самые богатые по Олежкиным коинам")
    @app_commands.guild_only()
    async def richest(self, interaction: discord.Interaction) -> None:
        rows = self.wallets.richest(interaction.guild.id)
        if not rows:
            await respond(interaction, "Пока никто не ставил — кошельки пусты.")
            return
        lines = []
        for index, (user_id, coins) in enumerate(rows, start=1):
            member = interaction.guild.get_member(user_id)
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`{index}.`"
            lines.append(f"{prefix} **{player_name(member) if member else f'<@{user_id}>'}** — {coins} 💰")
        embed = discord.Embed(title="💰 Богачи сервера", description="\n".join(lines), color=NEUTRAL)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Таблица лидеров: все игроки по Elo, и кто сыграл меньше 3 каток")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        ranking = self.ranking(interaction.guild.id)
        newcomers = self.newcomers(interaction.guild.id)
        if not ranking and not newcomers:
            await respond(
                interaction,
                f"Таблица пуста: нужны игроки хотя бы с {LEADERBOARD_MIN_GAMES} "
                "отмеченными катками. Победы отмечаются кнопками в посте сбора.",
            )
            return
        lines = []
        for index, (user_id, record) in enumerate(ranking[:LEADERBOARD_SIZE], start=1):
            member = interaction.guild.get_member(user_id)
            name = player_name(member) if member else f"<@{user_id}>"
            prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`{index}.`"
            mvp = f" · ⭐{record.mvp}" if record.mvp else ""
            lines.append(
                f"{prefix} **{name}** — **{record.elo:.0f}** Elo · {record.wins}/{record.games} ({record.winrate:.0%}){mvp}"
            )
        if len(ranking) > LEADERBOARD_SIZE:
            lines.append(f"…и ещё {len(ranking) - LEADERBOARD_SIZE} — их место видно в `/stats`")
        if not lines:
            lines.append(f"В таблице пока никого: нужно от {LEADERBOARD_MIN_GAMES} каток.")
        games = self.stats.games_count(interaction.guild.id)
        embed = discord.Embed(title="🏆 Таблица лидеров", description="\n".join(lines)[:4000], color=NEUTRAL)
        if newcomers:
            names = []
            for user_id, record in newcomers:
                member = interaction.guild.get_member(user_id)
                names.append(f"{player_name(member) if member else f'<@{user_id}>'} ({record.wins}/{record.games})")
            embed.add_field(
                name=f"Ещё играли — меньше {LEADERBOARD_MIN_GAMES} каток",
                value=", ".join(names)[:1024], inline=False,
            )
        embed.set_footer(
            text=f"Сыграно {games} {plural(games, 'катка', 'катки', 'каток')} · "
            f"в таблице — от {LEADERBOARD_MIN_GAMES} каток · порядок по Elo"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
