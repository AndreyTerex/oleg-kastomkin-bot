"""Сборы: скрим против соперника и внутренняя кастомка 5×5.

Лобби ведёт сбор по стадиям: запись → капитаны → драфт → развод по каналам.
Состояние пишется на диск, поэтому переживает перезапуск контейнера.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
import modes
import portraits
from champions import POOL, ChampionsUnavailable
from cogs.scrim import DraftView
from storage import JsonStore
from timeparse import parse_when
from utils import BLUE_SIDE, NEUTRAL, RED_SIDE, format_players, lanes_badge, player_name, respond, shuffled

log = logging.getLogger("scrimbot.lobby")

STATUS_IN = "in"
STATUS_SUB = "sub"
STATUS_OUT = "out"

STATUS_TITLES = {
    STATUS_IN: "✅ Основной состав",
    STATUS_SUB: "🕐 Запасные",
    STATUS_OUT: "❌ Не смогут",
}

KIND_SCRIM = "scrim"
KIND_CUSTOM = "custom"

KIND_TITLES = {
    KIND_SCRIM: "⚔️ Скрим",
    KIND_CUSTOM: "🎮 Кастомка 5×5",
}

# Стадии сбора: по ним включаются кнопки.
STAGE_SIGNUP = "signup"
STAGE_CAPTAINS = "captains"
STAGE_DRAFT = "draft"
STAGE_DONE = "done"

CUSTOM_ID_CAPTAINS = "lol:lobby:captains"
CUSTOM_ID_DRAFT = "lol:lobby:draft"
CUSTOM_ID_MOVE = "lol:lobby:move"
CUSTOM_ID_REROLL = "lol:lobby:reroll"
CUSTOM_ID_EDIT = "lol:lobby:edit"
CUSTOM_ID_MODE = "lol:lobby:mode"
CUSTOM_ID_CANCEL = "lol:lobby:close"
CUSTOM_ID_WIN_BLUE = "lol:lobby:win:blue"
CUSTOM_ID_WIN_RED = "lol:lobby:win:red"

# Кнопки, которые нужны только внутренней кастомке.
CUSTOM_GAME_ONLY = frozenset({
    CUSTOM_ID_MODE, CUSTOM_ID_CAPTAINS, CUSTOM_ID_DRAFT, CUSTOM_ID_REROLL, CUSTOM_ID_MOVE,
    CUSTOM_ID_WIN_BLUE, CUSTOM_ID_WIN_RED,
})

CANCEL_LABELS = {
    KIND_SCRIM: "Отменить скрим",
    KIND_CUSTOM: "Отменить кастомку",
}

PING_EVERYONE = "everyone"
PING_HERE = "here"
PING_NONE = "none"

PING_TEXT = {PING_EVERYONE: "@everyone", PING_HERE: "@here"}

# Кого зовёт /ping
PING_WHO_ROSTER = "roster"
PING_WHO_ALL = "all"
PING_WHO_SUBS = "subs"

PING_CHOICES = [
    app_commands.Choice(name="Всех (@everyone)", value=PING_EVERYONE),
    app_commands.Choice(name="Только тех, кто онлайн (@here)", value=PING_HERE),
    app_commands.Choice(name="Никого не упоминать", value=PING_NONE),
]

# Записи старше двух недель на старте выбрасываются, чтобы файл не рос бесконечно.
RECORD_TTL = 14 * 24 * 60 * 60
# Сбор старше полутора суток уже не «ближайший»: /ping, /roster и Олег его не предлагают.
# Считается от времени начала, если оно известно, иначе от создания поста.
LOBBY_STALE_AFTER = 36 * 60 * 60
# Драфт закрывается, если капитаны столько секунд никого не выбирали.
DRAFT_TIMEOUT = 15 * 60
# Как часто фоновая проверка смотрит напоминания и драфты.
TICK = 30


# Формат серии по умолчанию: обычно играем до двух побед.
DEFAULT_SERIES = 3
SERIES_CHOICES = [
    app_commands.Choice(name="Bo3 — до двух побед", value=3),
    app_commands.Choice(name="Bo1 — одна катка", value=1),
    app_commands.Choice(name="Bo5 — до трёх побед", value=5),
]
SERIES_WINNERS = ("🔵 синими", "🔴 красными")
# Две отметки подряд быстрее этого — почти наверняка двойной клик.
REPORT_COOLDOWN = 20


def series_score(record: dict, *, drop_last: bool = False) -> tuple[int, int]:
    results = list(record.get("results") or [])
    if drop_last:
        results = results[:-1]
    blue = sum(1 for result in results if result.get("winner") == 0)
    return blue, len(results) - blue


def series_winner(record: dict, *, drop_last: bool = False) -> int | None:
    """0 — синие, 1 — красные, None — серия ещё идёт. Считается первая серия сбора."""
    need = record.get("series", DEFAULT_SERIES) // 2 + 1
    blue, red = series_score(record, drop_last=drop_last)
    if blue >= need and blue > red:
        return 0
    if red >= need and red > blue:
        return 1
    return None


def set_waiting(record: dict, user_id: int, waiting: bool) -> None:
    """Очередь в состав: кто хотел играть, но не влез. Порядок — кто раньше нажал «Играю»."""
    queue = record.setdefault("waiting", [])
    if user_id in queue:
        queue.remove(user_id)
    if waiting:
        queue.append(user_id)


# Цвет карточки по заполнению: идёт набор → почти собрались → состав полный.
COLOR_FILLING = NEUTRAL
COLOR_ALMOST = discord.Color(0xF59E0B)
COLOR_READY = discord.Color(0x22C55E)
# С какой доли состава сбор считается «почти собранным».
ALMOST_SHARE = 0.7
# Больше стольких игроков — список делится на две колонки.
SPLIT_AFTER = 5


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """Тонкая полоска набора «▰▰▰▰▰▰▱▱▱▱». При большом сборе клетка — несколько игроков."""
    if total <= 0:
        return ""
    cells = min(total, width)
    filled = min(cells, round(done * cells / total))
    if done and not filled:
        filled = 1  # хоть кто-то записался — это видно
    return "▰" * filled + "▱" * (cells - filled)


def fill_line(done: int, total: int, subs: int = 0) -> str:
    """Строка под шапкой: полоска, счёт и сколько не хватает / сколько в запасе."""
    parts = [f"`{progress_bar(done, total)}` **{done}/{total}**"]
    if done >= total:
        parts.append("состав собран")
    else:
        parts.append(f"не хватает {total - done}")
    if subs:
        parts.append(f"в запасе {subs}")
    return " · ".join(parts)


def fill_color(done: int, total: int) -> discord.Color:
    if total > 0 and done >= total:
        return COLOR_READY
    if total > 0 and done >= total * ALMOST_SHARE:
        return COLOR_ALMOST
    return COLOR_FILLING


def slot_columns(members, target: int, heading: str) -> list[tuple[str, str]]:
    """Слоты состава, как в лобби игры: занятые — с игроками, свободные — «свободно».

    До SPLIT_AFTER слотов — одна колонка, больше — две (5 × 2 для кастомки) со сквозной нумерацией.
    """
    total = max(target, len(members))
    lines = []
    for index in range(total):
        number = f"`{index + 1:>2}`" if total >= 10 else f"`{index + 1}`"
        if index < len(members):
            member = members[index]
            badge = f" {lanes_badge(member)}".rstrip()
            lines.append(f"{number} {member.mention}{badge}")
        else:
            lines.append(f"{number} ◦ *свободно*")
    if total <= SPLIT_AFTER:
        return [(heading, "\n".join(lines) or "—")]
    half = (total + 1) // 2
    return [(heading, "\n".join(lines[:half])), ("\u200b", "\n".join(lines[half:]))]


def embed_key(embed: discord.Embed) -> tuple:
    """То, что видно в карточке, — для сравнения показанного поста с новой отрисовкой."""
    return (
        embed.title or None,
        embed.description or None,
        embed.color.value if embed.color else None,
        tuple((field.name, field.value, bool(field.inline)) for field in embed.fields),
        embed.footer.text or None,
        int(embed.timestamp.timestamp()) if embed.timestamp else None,
    )


def buttons_key(items) -> list[tuple]:
    return sorted(
        (item.custom_id or "", item.label or "", bool(item.disabled), str(item.emoji or ""))
        for item in items
        if isinstance(item, (discord.ui.Button, discord.components.Button))
    )


def same_render(message: discord.Message, embed: discord.Embed, view: discord.ui.View) -> bool:
    if len(message.embeds) != 1 or embed_key(message.embeds[0]) != embed_key(embed):
        return False
    shown = [child for row in message.components for child in getattr(row, "children", [])]
    return buttons_key(shown) == buttons_key(view.children)


class LobbyView(discord.ui.View):
    """Постоянная панель сбора.

    Без записи (`record=None`) создаётся только для регистрации custom_id при старте бота.
    Для конкретного сообщения панель собирается заново, чтобы кнопки соответствовали
    текущей стадии сбора.
    """

    def __init__(self, record: dict | None = None) -> None:
        super().__init__(timeout=None)
        if record is not None:
            self.sync_state(record)

    def sync_state(self, record: dict) -> None:
        closed = record.get("closed", False)
        stage = record.get("stage", STAGE_SIGNUP)
        kind = record.get("kind", KIND_SCRIM)
        going = len(record[STATUS_IN])
        target = record.get("target", config.TEAM_SIZE)
        has_captains = len(record.get("captains") or []) == 2
        has_teams = bool(record.get("teams"))
        mode = modes.get_mode(record)
        is_draft = mode["teams"] == modes.TEAMS_DRAFT
        preset = modes.find_preset(mode)

        for child in list(self.children):
            custom_id = getattr(child, "custom_id", None)

            # Скрим — это одна команда против соперника: делить её на две и раздавать чемпионов незачем.
            if kind == KIND_SCRIM and custom_id in CUSTOM_GAME_ONLY:
                self.remove_item(child)
                continue

            # Подписи зависят только от стадии — их видно и на закрытом сборе.
            if custom_id == CUSTOM_ID_CANCEL:
                child.label = CANCEL_LABELS.get(kind, CANCEL_LABELS[KIND_SCRIM])
                child.disabled = False
            elif custom_id == CUSTOM_ID_MODE:
                child.label = f"Режим: {preset.title if preset else 'свой'}"
                child.emoji = preset.emoji if preset else "⚙️"
                # Посреди драфта режим не меняем — иначе итог драфта разойдётся с настройками.
                child.disabled = stage == STAGE_DRAFT
            elif custom_id == CUSTOM_ID_CAPTAINS:
                if is_draft:
                    child.label = "Перезаролить капитанов" if has_captains else "Заролить капитанов"
                    child.emoji = "🎲"
                else:
                    child.label = "Перезапустить" if has_teams else "Запустить"
                    child.emoji = "▶️"
                # Запускать можно по собранному составу и не посреди драфта.
                child.disabled = going < target or stage == STAGE_DRAFT
            elif custom_id == CUSTOM_ID_DRAFT:
                if not is_draft:
                    # В режимах без капитанов кнопка драфта не нужна.
                    self.remove_item(child)
                    continue
                child.label = "Перезапустить драфт" if stage in (STAGE_DRAFT, STAGE_DONE) else "Начать драфт"
                child.disabled = not has_captains
            elif custom_id == CUSTOM_ID_REROLL:
                if mode["lanes"] == modes.LANES_FREE and mode["champs"] == modes.CHAMPS_FREE:
                    # В классике бот линии и чемпионов не раздаёт — переигрывать нечего.
                    self.remove_item(child)
                    continue
                child.disabled = not has_teams
            elif custom_id == CUSTOM_ID_MOVE:
                child.disabled = not has_teams
            elif custom_id in (CUSTOM_ID_WIN_BLUE, CUSTOM_ID_WIN_RED):
                # До деления на команды отмечать нечего — не занимаем место в посте.
                if not has_teams:
                    self.remove_item(child)
                    continue
                # Отмечается каждая катка серии: после отметки сразу начинается следующая.
                child.disabled = False
            else:
                child.disabled = False

            if closed:
                child.disabled = True

    # --- запись на сбор -------------------------------------------------

    async def _set_status(self, interaction: discord.Interaction, status: str | None) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)

        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается — создайте новый через `/scrim` или `/custom`.")
            return
        if record.get("closed"):
            await respond(interaction, "Сбор отменён.")
            return

        user_id = interaction.user.id
        target = record.get("target", config.TEAM_SIZE)
        note: str | None = None

        # Состав не может быть больше нужного: опоздавшие уходят в запас.
        if (
            status == STATUS_IN
            and user_id not in record[STATUS_IN]
            and len(record[STATUS_IN]) >= target
        ):
            status = STATUS_SUB
            note = (
                f"Состав уже собран ({target}/{target}) — записал вас запасным. "
                "Если кто-то отпишется до деления на команды, вы встанете в состав автоматически."
            )

        left_roster = user_id in record[STATUS_IN] and status != STATUS_IN
        for key in (STATUS_IN, STATUS_SUB, STATUS_OUT):
            if user_id in record[key]:
                record[key].remove(user_id)
        if status is not None:
            record[status].append(user_id)
        # Своё решение человека важнее очереди: сам выбрал «Запасной» — в состав его не тянем.
        set_waiting(record, user_id, note is not None)

        promoted = cog.promote_sub(interaction.guild, record) if left_roster else None

        # Сначала ответ Discord (на него 3 секунды), потом запись на диск.
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )
        await cog.save()
        if note:
            await interaction.followup.send(note, ephemeral=True)
        if promoted is not None:
            await cog.announce_promoted(interaction.followup, [promoted])

    @discord.ui.button(label="Играю", emoji="✅", style=discord.ButtonStyle.success, custom_id="lol:lobby:in")
    async def sign_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_status(interaction, STATUS_IN)

    @discord.ui.button(label="Запасной", emoji="🕐", style=discord.ButtonStyle.secondary, custom_id="lol:lobby:sub")
    async def sign_sub(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_status(interaction, STATUS_SUB)

    @discord.ui.button(label="Не смогу", emoji="❌", style=discord.ButtonStyle.danger, custom_id="lol:lobby:out")
    async def sign_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_status(interaction, STATUS_OUT)

    @discord.ui.button(
        label="Убрать мой голос", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="lol:lobby:reset", row=1
    )
    async def sign_reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_status(interaction, None)

    @discord.ui.button(
        label="Правка состава", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_EDIT, row=1
    )
    async def edit_roster(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Править состав может только автор сбора или организатор ивентов.")
            return
        if record.get("closed"):
            await respond(interaction, "Сбор отменён — править нечего.")
            return

        view = RosterEditView(cog, record, interaction.message)
        if not view.has_players:
            await respond(interaction, "В сборе пока никого нет.")
            return
        await interaction.response.send_message(
            "Выберите игроков в списке и нажмите нужное действие.", view=view, ephemeral=True
        )

    @discord.ui.button(
        label="Отменить сбор", emoji="🚫", style=discord.ButtonStyle.danger, custom_id=CUSTOM_ID_CANCEL, row=1
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Отменить сбор может только его автор или организатор ивентов.")
            return

        record["closed"] = True
        await cog.save()
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )

    # --- результат катки ------------------------------------------------

    @discord.ui.button(
        label="Победили синие", emoji="🔵", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID_WIN_BLUE, row=3
    )
    async def win_blue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._report(interaction, 0)

    @discord.ui.button(
        label="Победили красные", emoji="🔴", style=discord.ButtonStyle.danger, custom_id=CUSTOM_ID_WIN_RED, row=3
    )
    async def win_red(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._report(interaction, 1)

    async def _report(self, interaction: discord.Interaction, side: int) -> None:
        await report_result(interaction, interaction.message.id, side)

    # --- режим, запуск, драфт, развод по каналам -----------------------

    @discord.ui.button(
        label="Режим: Классика", emoji="👑", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_MODE, row=2
    )
    async def choose_mode(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Режим выбирает автор сбора или организатор ивентов.")
            return

        view = ModeView(cog, record, interaction.message)
        await interaction.response.send_message(view.describe(), view=view, ephemeral=True)

    @discord.ui.button(
        label="Заролить капитанов", emoji="🎲", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID_CAPTAINS, row=2
    )
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Запускать игру может только автор сбора или организатор ивентов.")
            return

        target = record.get("target", config.TEAM_SIZE)
        players = cog.signed_players(interaction.guild, record)
        if len(players) < target:
            await respond(
                interaction,
                f"Состав ещё не собран: записалось {len(players)} из {target}. "
                f"Не хватает {target - len(players)}.",
            )
            return

        mode = modes.get_mode(record)
        if mode["teams"] == modes.TEAMS_DRAFT:
            captains = random.sample(players[:target], 2)
            record["captains"] = [captain.id for captain in captains]
            record["teams"] = None
            record["deal"] = None
            record["stage"] = STAGE_CAPTAINS
            await cog.save()
            await interaction.response.edit_message(
                embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
            )
            return

        # Раздача может сходить за списком чемпионов — подтверждаем нажатие заранее.
        await interaction.response.defer()
        teams, lanes, champions, notes = await cog.run_mode(players[:target], mode)

        record["captains"] = None
        record["teams"] = [[member.id for member in team] for team in teams]
        record["deal"] = cog.pack_deal(lanes, champions)
        record["round"] = 1
        record["results"] = []
        record["stage"] = STAGE_DONE
        await cog.save()

        await interaction.message.edit(embed=cog.build_embed(interaction.guild, record), view=LobbyView(record))
        await interaction.followup.send(
            **await cog.deal_message(teams, lanes, champions, mode, notes)
        )

    @discord.ui.button(
        label="Начать драфт", emoji="📋", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID_DRAFT, row=2
    )
    async def start_draft(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Запускать драфт может только автор сбора или организатор ивентов.")
            return

        captains = cog.members_from_ids(interaction.guild, record.get("captains"))
        if len(captains) != 2:
            await respond(interaction, "Сначала заролльте капитанов — кто-то из них покинул сервер.")
            return

        if interaction.client.get_cog("Scrim") is None:
            await respond(interaction, "Модуль драфта не загружен — посмотрите логи бота.")
            return

        target = record.get("target", config.TEAM_SIZE)
        roster = cog.signed_players(interaction.guild, record)[:target]
        pool = [player for player in roster if player not in captains]
        if not pool:
            await respond(interaction, "Кроме капитанов в составе никого нет.")
            return

        # Прошлый драфт этого сбора (если перезапускают) больше не принимает пики.
        old = cog.drafts.pop(interaction.message.id, None)
        if old is not None:
            old.stop()

        # Номер драфта: если его перезапустили, старый драфт уже не должен трогать сбор.
        record["draft_no"] = record.get("draft_no", 0) + 1
        record["stage"] = STAGE_DRAFT
        record["teams"] = None
        record["deal"] = None
        record.pop("draft", None)

        draft = cog.draft_view(interaction.message, record, captains, shuffled(pool))
        cog.store_draft(record, draft)
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )
        draft.message = await interaction.followup.send(embed=draft.build_embed(), view=draft, wait=True)
        record["draft"]["message_id"] = draft.message.id
        await cog.save()

    @discord.ui.button(
        label="Новая раздача", emoji="🔁", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID_REROLL, row=2
    )
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not cog.is_organizer(interaction, record):
            await respond(interaction, "Переиграть раздачу может автор сбора или организатор ивентов.")
            return
        if not record.get("teams"):
            await respond(interaction, "Сначала запустите игру — переигрывать пока нечего.")
            return

        if interaction.message.id in cog._rerolling:
            await respond(interaction, "Уже раздаю — секунду.")
            return

        # Раздача может сходить за списком чемпионов — подтверждаем нажатие заранее.
        await interaction.response.defer()
        cog._rerolling.add(interaction.message.id)
        try:
            await cog.reroll_deal(interaction.message, record)
        finally:
            cog._rerolling.discard(interaction.message.id)

    @discord.ui.button(
        label="Раскидать по каналам", emoji="🚚", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_MOVE, row=2
    )
    async def move_players(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Lobby" = interaction.client.get_cog("Lobby")
        record = cog.get_record(interaction.message.id)
        if record is None:
            await respond(interaction, "Этот сбор больше не отслеживается.")
            return
        if not record.get("teams"):
            await respond(interaction, "Составы ещё не определены — сначала запустите игру.")
            return
        if not interaction.guild.me.guild_permissions.move_members:
            await respond(interaction, "Боту нужно право **«Перемещать участников»**.")
            return

        await interaction.response.send_message(
            "Выберите, куда развести составы, и нажмите «Развести».",
            view=MoveView(cog, record),
            ephemeral=True,
        )


async def report_result(interaction: discord.Interaction, lobby_id: int, side: int) -> None:
    """Записать победу стороны в сборе lobby_id: кнопкой в посте сбора или подтверждением по скриншоту."""
    cog: "Lobby" = interaction.client.get_cog("Lobby")
    record = cog.get_record(lobby_id)
    if record is None:
        await respond(interaction, "Этот сбор больше не отслеживается.")
        return
    if not cog.is_organizer(interaction, record):
        await respond(interaction, "Результат отмечает автор сбора или организатор ивентов.")
        return
    if record.get("closed") or len(record.get("teams") or []) != 2:
        await respond(interaction, "Составов нет — отмечать нечего.")
        return
    round_no = record.get("round", 1)
    if time.time() - record.get("last_report_at", 0) < REPORT_COOLDOWN:
        # Защита от двойного клика: катки не длятся секунды.
        await respond(interaction, f"Катку {round_no - 1} только что записала. Если это следующая — нажмите через пару секунд.")
        return
    stats_cog = interaction.client.get_cog("Stats")
    if stats_cog is None:
        await respond(interaction, "Модуль статистики не загружен — посмотрите логи бота.")
        return

    winners, losers = record["teams"][side], record["teams"][1 - side]
    # Отметка раньше записи: второй быстрый клик уже увидит, что катка записана.
    record["last_report_at"] = time.time()
    record["round"] = round_no + 1
    game_id = await stats_cog.stats.record_game(
        interaction.guild.id, list(winners), list(losers), lobby_id=lobby_id, round_no=round_no,
        channel_id=interaction.channel_id,
    )
    record.setdefault("results", []).append({"round": round_no, "winner": side, "game": game_id})
    await cog.save()
    side_name = (BLUE_SIDE, RED_SIDE)[side]
    if interaction.message is not None and interaction.message.id == lobby_id:
        lobby_message = interaction.message
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )
    else:
        channel = interaction.client.get_channel(record["channel_id"]) or interaction.channel
        lobby_message = channel.get_partial_message(lobby_id)
        await interaction.response.edit_message(content=f"✅ Записала: победа — **{side_name}**.", view=None)
        try:
            await lobby_message.edit(embed=cog.build_embed(interaction.guild, record), view=LobbyView(record))
        except discord.HTTPException:
            log.warning("Не удалось обновить пост сбора после записи результата по скриншоту")

    names = ", ".join(player_name(m) for m in cog.members_from_ids(interaction.guild, winners))
    lines = [f"🏆 Катка {round_no}: победа — **{side_name}**! {names}", cog.score_line(record)]
    if series_winner(record) is not None and series_winner(record, drop_last=True) is None:
        lines.append(f"🎉 **Серия Bo{record.get('series', DEFAULT_SERIES)} за {SERIES_WINNERS[side]}!**")
    announce = await interaction.followup.send("\n".join(lines), wait=True)
    await interaction.followup.send(
        "Записала в статистику. Ошиблись кнопкой — отмените:",
        view=UndoResultView(cog, record, lobby_message, game_id, round_no, announce),
        ephemeral=True,
    )
    if config.MVP_VOTE_MINUTES:
        players = cog.members_from_ids(interaction.guild, list(winners) + list(losers))
        if len(players) >= 2:
            view = MvpView(stats_cog.stats, interaction.guild.id, game_id, round_no, players, set(winners))
            view.message = await interaction.followup.send(view.text(), view=view, wait=True)


class UndoResultView(discord.ui.View):
    """Отмена ошибочно отмеченного результата. Видит только тот, кто отмечал."""

    def __init__(
        self, cog: "Lobby", record: dict, lobby_message: discord.Message, game_id: int, round_no: int,
        announce: discord.Message,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.record = record
        self.lobby_message = lobby_message
        self.game_id = game_id
        self.round_no = round_no
        self.announce = announce

    @discord.ui.button(label="Отменить результат", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        stats_cog = interaction.client.get_cog("Stats")
        if stats_cog is not None:
            await stats_cog.stats.undo_game(interaction.guild.id, self.game_id)
        self.record["results"] = [r for r in self.record.get("results", []) if r.get("game") != self.game_id]
        # Номер катки откатываем, только если после этой отметки новых не было.
        if self.record.get("round") == self.round_no + 1:
            self.record["round"] = self.round_no
        self.record.pop("last_report_at", None)
        await self.cog.save()
        try:
            await self.lobby_message.edit(
                embed=self.cog.build_embed(self.lobby_message.guild, self.record), view=LobbyView(self.record)
            )
            await self.announce.edit(content=f"~~{self.announce.content}~~\n↩️ Результат отменён.")
        except discord.HTTPException:
            log.exception("Не удалось обновить сообщения после отмены результата")
        button.disabled = True
        await interaction.response.edit_message(content="Результат отменён, статистика исправлена.", view=self)
        self.stop()


def mvp_winners(votes: dict[int, int]) -> list[int]:
    """Кто набрал больше всех голосов (при равенстве — все лидеры)."""
    if not votes:
        return []
    counts: dict[int, int] = {}
    for candidate in votes.values():
        counts[candidate] = counts.get(candidate, 0) + 1
    best = max(counts.values())
    return sorted(user_id for user_id, count in counts.items() if count == best)


class MvpView(discord.ui.View):
    """Голосование за MVP катки: голосуют только её участники, за себя нельзя."""

    def __init__(
        self, stats, guild_id: int, game_id: int, round_no: int, players: list[discord.Member], winners: set[int],
    ) -> None:
        super().__init__(timeout=config.MVP_VOTE_MINUTES * 60)
        self.stats = stats
        self.guild_id = guild_id
        self.game_id = game_id
        self.round_no = round_no
        self.players = {member.id: member for member in players}
        self.votes: dict[int, int] = {}
        self.message: discord.Message | None = None
        self.finished = False
        self.ends = int(time.time() + config.MVP_VOTE_MINUTES * 60)
        select = discord.ui.Select(
            placeholder="Кто MVP этой катки?",
            options=[
                discord.SelectOption(
                    label=player_name(member)[:100], value=str(member.id),
                    emoji="🏆" if member.id in winners else "💀",
                )
                for member in players[:25]
            ],
        )
        select.callback = self.vote
        self.add_item(select)

    def text(self) -> str:
        line = (
            f"⭐ **Кто MVP катки {self.round_no}?** Голосуют только игравшие, за себя нельзя. "
            f"Итоги <t:{self.ends}:R>."
        )
        return line + f"\n-# Проголосовало {len(self.votes)} из {len(self.players)}"

    async def vote(self, interaction: discord.Interaction) -> None:
        voter = interaction.user.id
        if voter not in self.players:
            await interaction.response.send_message("Голосуют только те, кто играл эту катку.", ephemeral=True)
            return
        candidate = int(interaction.data["values"][0])
        if candidate == voter:
            await interaction.response.send_message("За себя нельзя, хитрюшка 😼", ephemeral=True)
            return
        changed = voter in self.votes
        self.votes[voter] = candidate
        if len(self.votes) >= len(self.players):
            await interaction.response.defer()
            await self.finish()
            return
        await interaction.response.edit_message(content=self.text(), view=self)
        await interaction.followup.send(
            ("Голос изменён" if changed else "Голос принят") + f": {player_name(self.players[candidate])}.",
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        await self.finish()

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.stop()
        winners = mvp_winners(self.votes)
        if not winners:
            content = f"⭐ MVP катки {self.round_no}: никто не проголосовал."
        elif not await self.stats.set_mvp(self.guild_id, self.game_id, winners):
            content = f"⭐ Голосование за MVP катки {self.round_no} закрыто: результат катки отменили."
        else:
            names = ", ".join(f"**{player_name(self.players[u])}**" for u in winners if u in self.players)
            count = sum(1 for c in self.votes.values() if c == winners[0])
            content = f"⭐ MVP катки {self.round_no} — {names} ({count} из {len(self.votes)} голосов)!"
        if self.message is not None:
            try:
                await self.message.edit(content=content, view=None)
            except discord.HTTPException:
                log.warning("Не удалось подвести итоги голосования за MVP")


class ModeView(discord.ui.View):
    """Выбор режима организатором: готовый пресет или настройка по пунктам."""

    def __init__(self, cog: "Lobby", record: dict, lobby_message: discord.Message) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.record = record
        self.lobby_message = lobby_message
        self.mode = modes.get_mode(record)

        self.preset_select = discord.ui.Select(placeholder="Готовый режим", row=0)
        self.teams_select = discord.ui.Select(placeholder="Команды", row=1)
        self.lanes_select = discord.ui.Select(placeholder="Линии", row=2)
        self.champs_select = discord.ui.Select(placeholder="Чемпионы", row=3)

        self.preset_select.callback = self._on_preset
        self.teams_select.callback = self._setting("teams", self.teams_select)
        self.lanes_select.callback = self._setting("lanes", self.lanes_select)
        self.champs_select.callback = self._setting("champs", self.champs_select)
        for select in (self.preset_select, self.teams_select, self.lanes_select, self.champs_select):
            self.add_item(select)
        self._refresh()

    def _refresh(self) -> None:
        current = modes.find_preset(self.mode)
        self.preset_select.options = [
            discord.SelectOption(
                label=preset.title,
                value=preset.key,
                emoji=preset.emoji,
                description=preset.description[:100],
                default=current is not None and preset.key == current.key,
            )
            for preset in modes.PRESETS
        ]
        for select, key, options in (
            (self.teams_select, "teams", modes.TEAM_OPTIONS),
            (self.lanes_select, "lanes", modes.LANE_OPTIONS),
            (self.champs_select, "champs", modes.CHAMP_OPTIONS),
        ):
            select.options = [
                discord.SelectOption(
                    label=option.label,
                    value=option.key,
                    emoji=option.emoji,
                    description=option.description[:100],
                    default=option.key == self.mode[key],
                )
                for option in options
            ]

    def describe(self) -> str:
        preset = modes.find_preset(self.mode)
        lines = [f"**Режим: {modes.mode_title(self.mode)}**"]
        if preset:
            lines.append(preset.description)
        for key, title, options in (
            ("teams", "Команды", modes.TEAM_OPTIONS),
            ("lanes", "Линии", modes.LANE_OPTIONS),
            ("champs", "Чемпионы", modes.CHAMP_OPTIONS),
        ):
            option = modes.option_for(options, self.mode[key])
            lines.append(f"{option.emoji} {title}: **{option.label}** — {option.description.lower()}")
        lines.append("")
        lines.append("Выберите готовый режим или настройте по пунктам, затем нажмите «Сохранить».")
        return "\n".join(lines)

    async def _on_preset(self, interaction: discord.Interaction) -> None:
        self.mode = modes.PRESET_BY_KEY[self.preset_select.values[0]].mode
        self._refresh()
        await interaction.response.edit_message(content=self.describe(), view=self)

    def _setting(self, key: str, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            self.mode[key] = select.values[0]
            self._refresh()
            await interaction.response.edit_message(content=self.describe(), view=self)

        return callback

    @discord.ui.button(label="Сохранить", emoji="💾", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.record.get("stage") == STAGE_DRAFT:
            await respond(interaction, "Сейчас идёт драфт — режим можно поменять после него.")
            return

        mode, notes = modes.normalize(self.mode)
        self.mode = mode
        self.record["mode"] = mode
        # Капитаны, заролленные под драфт, в других режимах не нужны.
        if mode["teams"] != modes.TEAMS_DRAFT and self.record.get("stage") == STAGE_CAPTAINS:
            self.record["captains"] = None
            self.record["stage"] = STAGE_SIGNUP
        await self.cog.save()
        await self.lobby_message.edit(
            embed=self.cog.build_embed(self.lobby_message.guild, self.record), view=LobbyView(self.record)
        )

        lines = [f"Режим сохранён: **{modes.mode_title(mode)}** — {modes.mode_summary(mode)}."]
        lines += [f"• {note}" for note in notes]
        if self.record.get("teams"):
            lines.append("Составы уже сформированы — нажмите «Перезапустить», чтобы разыграть их по новому режиму.")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="\n".join(lines), view=self)
        self.stop()


class RosterEditView(discord.ui.View):
    """Правка состава организатором. Видит только тот, кто нажал кнопку."""

    STATUS_HINTS = {STATUS_IN: "в составе", STATUS_SUB: "в запасе", STATUS_OUT: "не сможет"}

    def __init__(self, cog: "Lobby", record: dict, lobby_message: discord.Message) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.record = record
        self.lobby_message = lobby_message

        self.select = discord.ui.Select(placeholder="Кого правим", min_values=1, max_values=1, row=0)
        self.select.callback = self._remember
        self.add_item(self.select)
        self.refresh_options()

    @property
    def has_players(self) -> bool:
        return bool(self.select.options)

    def refresh_options(self) -> None:
        guild = self.lobby_message.guild
        options: list[discord.SelectOption] = []
        for status, hint in self.STATUS_HINTS.items():
            for user_id in self.record[status]:
                member = guild.get_member(user_id)
                if member is None:
                    continue
                options.append(
                    discord.SelectOption(
                        label=member.display_name[:100], value=str(user_id), description=hint
                    )
                )
        options = options[:25]
        self.select.options = options
        self.select.max_values = max(1, len(options))
        self.select.disabled = not options

    async def _remember(self, interaction: discord.Interaction) -> None:
        # Выбор хранится в самом компоненте, здесь только подтверждаем нажатие.
        await interaction.response.defer()

    def _names(self, guild: discord.Guild, ids: list[int]) -> str:
        return ", ".join(
            player_name(member) for user_id in ids if (member := guild.get_member(user_id))
        )

    async def _apply(self, interaction: discord.Interaction, target_status: str | None, verb: str) -> None:
        if not self.select.values:
            await respond(interaction, "Сначала выберите игроков в списке.")
            return

        guild = self.lobby_message.guild
        limit = self.record.get("target", config.TEAM_SIZE)
        changed, overflow, promoted = self.cog.apply_move(
            guild, self.record, [int(value) for value in self.select.values], target_status
        )

        await self.cog.save()
        self.refresh_options()

        lines = []
        if changed:
            lines.append(f"{verb}: {self._names(guild, changed)}.")
        if promoted:
            lines.append(f"Из очереди в состав встали: {', '.join(player_name(m) for m in promoted)}.")
        if overflow:
            lines.append(
                f"В состав не поместились (уже {limit}/{limit}), оставлены в запасе: "
                f"{self._names(guild, overflow)}."
            )
        lines.append("Можно править дальше — список обновлён.")
        # Сначала отвечаем Discord (на это 3 секунды), потом правим пост: правка может ждать в очереди лимитов.
        await interaction.response.edit_message(content="\n".join(lines), view=self)
        try:
            await self.lobby_message.edit(
                embed=self.cog.build_embed(guild, self.record), view=LobbyView(self.record)
            )
        except discord.HTTPException as error:
            log.warning("Не удалось обновить пост сбора %s после правки состава: %s", self.lobby_message.id, error)
            await interaction.followup.send(
                "Состав сохранён, но Discord пока не даёт обновить пост сбора (лимит правок старых сообщений). "
                "Пост обновится при следующем нажатии кнопки в нём.",
                ephemeral=True,
            )
        if promoted:
            await self.cog.announce_promoted(self.lobby_message.channel, promoted)

    @discord.ui.button(label="Убрать из сбора", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove_players(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply(interaction, None, "Убраны из сбора")

    @discord.ui.button(label="В запас", emoji="🕐", style=discord.ButtonStyle.secondary, row=1)
    async def to_sub(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply(interaction, STATUS_SUB, "Переведены в запас")

    @discord.ui.button(label="В состав", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def to_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply(interaction, STATUS_IN, "Переведены в состав")


class MoveView(discord.ui.View):
    """Выбор голосовых каналов для двух составов. Видит только тот, кто нажал кнопку."""

    def __init__(self, cog: "Lobby", record: dict) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.record = record

        self.blue_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.voice],
            placeholder="Канал для синей стороны",
            row=0,
        )
        self.red_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.voice],
            placeholder="Канал для красной стороны",
            row=1,
        )
        for select in (self.blue_select, self.red_select):
            select.callback = self._remember
            self.add_item(select)

    async def _remember(self, interaction: discord.Interaction) -> None:
        # Выбор хранится в самом компоненте, здесь только подтверждаем нажатие.
        await interaction.response.defer()

    @staticmethod
    def _resolve(guild: discord.Guild, choice) -> discord.VoiceChannel | None:
        return choice.resolve() or guild.get_channel(choice.id)

    @discord.ui.button(label="Развести", emoji="🚚", style=discord.ButtonStyle.primary, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.blue_select.values or not self.red_select.values:
            await respond(interaction, "Выберите оба канала.")
            return

        guild = interaction.guild
        blue_channel = self._resolve(guild, self.blue_select.values[0])
        red_channel = self._resolve(guild, self.red_select.values[0])
        if blue_channel is None or red_channel is None:
            await respond(interaction, "Один из каналов недоступен. Выберите другой.")
            return

        await interaction.response.defer()
        moved, skipped = await self.cog.move_teams(guild, self.record, blue_channel, red_channel)

        lines = [f"Перемещено игроков: **{moved}** → {blue_channel.mention} и {red_channel.mention}."]
        if skipped:
            lines.append("Не в голосовом канале: " + ", ".join(skipped))
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content="\n".join(lines), view=self)
        self.stop()


class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JsonStore(Path(config.DATA_DIR) / "lobbies.json")
        # Когда какой чемпион выпадал на сервере: id сервера → {id чемпиона: время}.
        self.champion_history = JsonStore(Path(config.DATA_DIR) / "champion_history.json")
        self._refresh_task: asyncio.Task | None = None
        # Сборы, где прямо сейчас идёт новая раздача: двойной клик не должен раздать дважды.
        self._rerolling: set[int] = set()
        # Живые драфты сборов: id поста сбора → меню драфта.
        self.drafts: dict[int, DraftView] = {}

    async def cog_load(self) -> None:
        self.champion_history.load()
        data = self.store.load()
        now = time.time()
        fresh = {
            key: value
            for key, value in data.items()
            if now - value.get("created_at", now) < RECORD_TTL
        }
        trimmed = [
            key for key, record in fresh.items() if not record.get("closed") and self._trim_roster(record)
        ]
        # Сборы, созданные до появления дат: время разбираем относительно момента создания поста.
        dated = 0
        for record in fresh.values():
            if "start_at" not in record:
                created = datetime.fromtimestamp(record.get("created_at", now), config.TIMEZONE)
                start = parse_when(record.get("time", ""), created)
                record["start_at"] = start.timestamp() if start else None
                record["reminded"] = bool(start) and start.timestamp() <= now
                dated += 1
        if trimmed:
            log.info("Состав подрезан до нормы у сборов: %s", ", ".join(trimmed))
        if len(fresh) != len(data) or trimmed or dated:
            self.store.data = fresh
            await self.store.save()
        self.bot.add_view(LobbyView())
        log.info("Активных сборов восстановлено: %d", len(fresh))
        self._refresh_task = asyncio.create_task(self._background())

    async def cog_unload(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()

    async def _background(self) -> None:
        """После старта перерисовывает посты и восстанавливает драфты, дальше раз в TICK секунд
        напоминает о сборах и закрывает драфты, где капитаны давно не выбирали."""
        await self._refresh_messages()
        while True:
            try:
                await self.remind_due()
                await self.expire_drafts()
            except Exception:
                log.exception("Ошибка в фоновой проверке сборов")
            await asyncio.sleep(TICK)

    async def _refresh_messages(self) -> None:
        """Перерисовывает сохранённые сообщения сборов после перезапуска.

        Состав берётся из файла и не меняется — обновляется только вид сообщения,
        чтобы кнопки соответствовали текущим правилам.
        """
        await self.bot.wait_until_ready()
        closed_missing = 0
        redrawn = 0
        for message_id, record in list(self.store.data.items()):
            if record.get("closed"):
                continue  # отменённый сбор уже нарисован закрытым — лишняя правка только тратит лимит
            channel = self.bot.get_channel(record["channel_id"])
            if channel is None:
                log.warning("Канал %s для сбора %s недоступен", record["channel_id"], message_id)
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                if record.get("stage") == STAGE_DRAFT and not record.get("closed"):
                    await self.restore_draft(message, record)
                embed, view = self.build_embed(channel.guild, record), LobbyView(record)
                # Правим только то, что изменилось: у постов старше часа Discord ограничивает число правок,
                # и перерисовка всех сборов при каждом перезапуске бота съедала этот лимит.
                if not same_render(message, embed, view):
                    await message.edit(embed=embed, view=view)
                    redrawn += 1
            except discord.NotFound:
                # Пост удалили — значит, сбор отменили. Запись остаётся в файле, но закрытой.
                if not record.get("closed"):
                    record["closed"] = True
                    closed_missing += 1
                log.warning("Сообщение сбора %s не найдено — сбор помечен закрытым", message_id)
            except discord.HTTPException:
                log.exception("Не удалось обновить сообщение сбора %s", message_id)
        if closed_missing:
            await self.save()
        log.info("Сообщения сборов проверены, перерисовано: %d", redrawn)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Удалили пост сбора — сбор больше не открыт, иначе /ping и Олег продолжат на него ссылаться."""
        record = self.store.data.get(str(payload.message_id))
        if record is not None and not record.get("closed"):
            record["closed"] = True
            await self.save()
            log.info("Пост сбора %s удалён — сбор закрыт", payload.message_id)

    # --- драфт, который переживает перезапуск ------------------------------

    def draft_view(
        self,
        lobby_message: discord.Message,
        record: dict,
        captains: list[discord.Member],
        pool: list[discord.Member],
        teams: list[list[discord.Member]] | None = None,
    ) -> DraftView:
        """Меню драфта сбора. Постоянное: после рестарта бот находит его по custom_id."""
        draft_no = record["draft_no"]
        key = lobby_message.id

        def is_current() -> bool:
            return record.get("draft_no") == draft_no and not record.get("closed")

        async def on_pick(view: DraftView) -> None:
            if is_current():
                self.store_draft(record, view)
                await self.save()

        async def on_finish(teams: list[list[discord.Member]]) -> None:
            if not is_current():
                return
            record.pop("draft", None)
            self.drafts.pop(key, None)
            await self.finish_draft(lobby_message, record, teams)

        view = DraftView(
            self.bot.get_cog("Scrim"), captains, pool,
            on_finish=on_finish, on_pick=on_pick, teams=teams,
            expired_hint="Время на драфт вышло — нажмите «Перезапустить драфт» в посте сбора",
            custom_id=f"lol:draft:{key}:{draft_no}", timeout=None,
        )
        self.drafts[key] = view
        return view

    @staticmethod
    def store_draft(record: dict, view: DraftView) -> None:
        """Ход драфта на диск. Каждый пик продлевает срок: капитаны думают, а не ушли."""
        state = record.setdefault("draft", {})
        state["captains"] = [captain.id for captain in view.captains]
        state["pool"] = [member.id for member in view.pool]
        state["teams"] = [[member.id for member in team] for team in view.teams]
        state["expires_at"] = time.time() + DRAFT_TIMEOUT

    async def restore_draft(self, lobby_message: discord.Message, record: dict) -> None:
        """Возвращает к жизни меню драфта после перезапуска бота."""
        state = record.get("draft") or {}
        guild = lobby_message.guild
        captains = self.members_from_ids(guild, state.get("captains"))
        teams = [self.members_from_ids(guild, ids) for ids in state.get("teams") or []]
        pool = self.members_from_ids(guild, state.get("pool"))
        broken = len(captains) != 2 or len(teams) != 2 or any(not team for team in teams)
        if broken or not state.get("message_id") or state.get("expires_at", 0) < time.time():
            await self.expire_draft(str(lobby_message.id), record)
            return
        if self.bot.get_cog("Scrim") is None:
            return
        view = self.draft_view(lobby_message, record, captains, pool, teams)
        view.message = lobby_message.channel.get_partial_message(state["message_id"])
        self.bot.add_view(view, message_id=state["message_id"])
        log.info("Драфт сбора %s восстановлен: осталось игроков %d", lobby_message.id, len(pool))

    async def expire_drafts(self) -> None:
        now = time.time()
        for message_id, record in list(self.store.data.items()):
            state = record.get("draft")
            if not state:
                continue
            if record.get("closed") or record.get("stage") != STAGE_DRAFT or state.get("expires_at", 0) < now:
                await self.expire_draft(message_id, record)

    async def expire_draft(self, message_id: str, record: dict) -> None:
        """Снимает стадию драфта: меню сереет, а в посте сбора снова можно запустить драфт."""
        state = record.pop("draft", None) or {}
        view = self.drafts.pop(int(message_id), None)
        if view is not None:
            view.stop()
            view.select.disabled = True
        if record.get("stage") == STAGE_DRAFT:
            record["stage"] = STAGE_CAPTAINS
        await self.save()

        channel = self.bot.get_channel(record["channel_id"])
        if channel is None:
            return
        if state.get("message_id"):
            draft_message = channel.get_partial_message(state["message_id"])
            try:
                if view is not None:
                    await draft_message.edit(embed=view.expired_embed(), view=view)
                else:
                    await draft_message.edit(view=None)
            except discord.HTTPException:
                log.debug("Не удалось погасить меню драфта %s", state["message_id"])
        if not record.get("closed"):
            try:
                await channel.get_partial_message(int(message_id)).edit(
                    embed=self.build_embed(channel.guild, record), view=LobbyView(record)
                )
            except discord.HTTPException:
                log.exception("Не удалось обновить сбор %s после таймаута драфта", message_id)
        log.info("Драфт сбора %s закрыт по таймауту", message_id)

    # --- напоминания --------------------------------------------------------

    async def remind_due(self) -> None:
        """За REMIND_MINUTES до начала зовёт записавшихся — один раз на сбор."""
        if config.REMIND_MINUTES <= 0:
            return
        now = time.time()
        window = config.REMIND_MINUTES * 60
        changed = False
        for message_id, record in list(self.store.data.items()):
            start_at = record.get("start_at")
            if not start_at or record.get("reminded") or record.get("closed"):
                continue
            if start_at - now > window:
                continue
            record["reminded"] = True
            changed = True
            # Бот был выключен и проспал начало или сбор создан прямо перед игрой — звать поздно или незачем.
            if now >= start_at or record.get("created_at", 0) > start_at - window:
                continue
            await self.send_reminder(message_id, record)
        if changed:
            await self.save()

    async def send_reminder(self, message_id: str, record: dict) -> None:
        channel = self.bot.get_channel(record["channel_id"])
        if channel is None:
            return
        members = self.members_from_ids(channel.guild, record[STATUS_IN])
        if not members:
            return
        title = KIND_TITLES.get(record.get("kind", KIND_SCRIM), KIND_TITLES[KIND_SCRIM])
        jump_url = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{message_id}"
        lines = [
            f"⏰ **{title}** начинается <t:{int(record['start_at'])}:R>!",
            " ".join(member.mention for member in members),
        ]
        subs = self.members_from_ids(channel.guild, record[STATUS_SUB])
        if subs:
            lines.append("Запасные, будьте рядом: " + ", ".join(player_name(member) for member in subs))
        lines.append(f"[Пост сбора]({jump_url})")
        try:
            await channel.send(
                "\n".join(lines),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=members),
            )
            log.info("Напоминание о сборе %s отправлено", message_id)
        except discord.HTTPException:
            log.exception("Не удалось напомнить о сборе %s", message_id)

    # --- работа с записями ----------------------------------------------

    @staticmethod
    def _trim_roster(record: dict) -> bool:
        """Переносит лишних из состава в запас: записи могли остаться без ограничения."""
        target = record.get("target", config.TEAM_SIZE)
        overflow = record[STATUS_IN][target:]
        if not overflow:
            return False
        record[STATUS_IN] = record[STATUS_IN][:target]
        record[STATUS_SUB] = [
            user_id for user_id in record[STATUS_SUB] if user_id not in overflow
        ] + overflow
        for user_id in overflow:
            set_waiting(record, user_id, True)
        return True

    def promote_sub(self, guild: discord.Guild, record: dict) -> discord.Member | None:
        """На освободившееся место встаёт первый из очереди — пока составы ещё не поделены.

        В очереди только те, кто нажал «Играю», когда состав был полон. Кто сам выбрал «Запасной»,
        остаётся в запасе. После капитанов и драфта состав уже распределён, там решает организатор.
        """
        if record.get("stage", STAGE_SIGNUP) != STAGE_SIGNUP or record.get("teams"):
            return None
        if len(record[STATUS_IN]) >= record.get("target", config.TEAM_SIZE):
            return None
        for user_id in list(record.get("waiting") or []):
            member = guild.get_member(user_id)
            if member is None or user_id not in record[STATUS_SUB]:
                continue
            record[STATUS_SUB].remove(user_id)
            record[STATUS_IN].append(user_id)
            set_waiting(record, user_id, False)
            return member
        return None

    @staticmethod
    async def announce_promoted(destination, members: list[discord.Member]) -> None:
        mentions = ", ".join(member.mention for member in members)
        try:
            await destination.send(
                f"🔼 {mentions}, освободилось место — вы теперь в основном составе!",
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=members),
            )
        except discord.HTTPException:
            log.exception("Не удалось позвать поднятых из очереди")

    @staticmethod
    def score_line(record: dict) -> str:
        blue, red = series_score(record)
        series = record.get("series", DEFAULT_SERIES)
        line = f"📊 **Серия Bo{series}:** 🔵 {blue} : {red} 🔴"
        winner = series_winner(record)
        if winner is not None:
            line += f" — серия за {SERIES_WINNERS[winner]}"
        return line

    def get_record(self, message_id: int) -> dict | None:
        return self.store.data.get(str(message_id))

    async def save(self) -> None:
        await self.store.save()

    def is_organizer(self, interaction: discord.Interaction, record: dict) -> bool:
        """Управлять ходом сбора может автор объявления или организатор ивентов."""
        return self.is_organizer_member(interaction.user, record)

    @staticmethod
    def is_organizer_member(member: discord.Member, record: dict) -> bool:
        return member.id == record["author_id"] or member.guild_permissions.manage_events

    def apply_move(
        self, guild: discord.Guild, record: dict, user_ids: list[int], target_status: str | None
    ) -> tuple[list[int], list[int], list[discord.Member]]:
        """Переносит игроков (None — убрать из сбора). Возвращает перенесённых, не влезших в состав
        (они в запасе и в очереди) и поднятых из очереди на освободившиеся места."""
        limit = record.get("target", config.TEAM_SIZE)
        changed: list[int] = []
        overflow: list[int] = []
        for user_id in user_ids:
            for key in (STATUS_IN, STATUS_SUB, STATUS_OUT):
                if user_id in record[key]:
                    record[key].remove(user_id)
            # В состав больше нужного не пускаем даже организатора — иначе счётчик врёт.
            overflowed = target_status == STATUS_IN and len(record[STATUS_IN]) >= limit
            set_waiting(record, user_id, overflowed)
            if overflowed:
                record[STATUS_SUB].append(user_id)
                overflow.append(user_id)
                continue
            if target_status is not None:
                record[target_status].append(user_id)
            changed.append(user_id)
        promoted = []
        while (member := self.promote_sub(guild, record)) is not None:
            promoted.append(member)
        return changed, overflow, promoted

    async def refresh_post(self, message_id: int | str, record: dict) -> None:
        """Перерисовывает пост сбора, когда состав поменяли не кнопкой под ним."""
        channel = self.bot.get_channel(record["channel_id"])
        if channel is None:
            return
        try:
            await channel.get_partial_message(int(message_id)).edit(
                embed=self.build_embed(channel.guild, record), view=LobbyView(record)
            )
        except discord.HTTPException:
            log.exception("Не удалось обновить пост сбора %s", message_id)

    def members_from_ids(self, guild: discord.Guild, ids) -> list[discord.Member]:
        return [member for user_id in (ids or []) if (member := guild.get_member(user_id))]

    def signed_players(self, guild: discord.Guild, record: dict) -> list[discord.Member]:
        """Записавшиеся «Играю» в порядке записи, без тех, кто ушёл с сервера."""
        return self.members_from_ids(guild, record[STATUS_IN])

    async def finish_draft(
        self, message: discord.Message, record: dict, teams: list[list[discord.Member]]
    ) -> None:
        """Сохраняет итог драфта, раздаёт линии и чемпионов по режиму и открывает развод по каналам."""
        mode = modes.get_mode(record)
        lanes, notes = self.assign_team_lanes(teams, mode["lanes"])
        champions, champion_notes = await self.deal_for(teams, lanes, mode)

        record["teams"] = [[member.id for member in team] for team in teams]
        record["deal"] = self.pack_deal(lanes, champions)
        record["round"] = 1
        record["results"] = []
        record["stage"] = STAGE_DONE
        await self.save()
        try:
            await message.edit(embed=self.build_embed(message.guild, record), view=LobbyView(record))
        except discord.HTTPException:
            log.exception("Не удалось обновить сообщение сбора после драфта")

        # В классике раздавать нечего — составы и так видны в посте сбора.
        if mode["lanes"] == modes.LANES_FREE and mode["champs"] == modes.CHAMPS_FREE:
            return
        captains = [team[0] for team in teams]  # драфт начинается с капитанов
        try:
            await message.channel.send(
                **await self.deal_message(teams, lanes, champions, mode, notes + champion_notes, captains=captains)
            )
        except discord.HTTPException:
            log.exception("Не удалось опубликовать раздачу после драфта")

    async def deal_message(
        self,
        teams: list[list[discord.Member]],
        lanes: dict[int, str],
        champions: dict,
        mode: dict,
        notes: list[str],
        **options,
    ) -> dict:
        """Эмбед раздачи и, если есть чемпионы, картинка с их портретами."""
        embed = modes.deal_embed(teams, lanes, champions, mode, notes, patch=POOL.version, **options)
        image = await portraits.deal_image(teams, champions, lambda team: modes.order_by_lane(team, lanes))
        if image is None:
            return {"embed": embed}
        embed.set_image(url=f"attachment://{image.filename}")
        return {"embed": embed, "file": image}

    async def reroll_deal(self, message: discord.Message, record: dict) -> None:
        """Новая раздача после катки: составы те же, линии и чемпионы заново, без чемпионов прошлой катки."""
        guild = message.guild
        mode = modes.get_mode(record)
        teams = [self.members_from_ids(guild, ids) for ids in record["teams"]]
        previous = {
            name for names in ((record.get("deal") or {}).get("champions") or {}).values() for name in names
        }
        lanes, notes = self.assign_team_lanes(teams, mode["lanes"], previous=self.deal_lanes(record))
        champions, champion_notes = await self.deal_for(teams, lanes, mode, exclude=previous)

        # Номер катки двигает отметка победителя, а не раздача: переразадать можно и посреди катки.
        record["deal"] = self.pack_deal(lanes, champions)
        await self.save()
        try:
            await message.edit(embed=self.build_embed(guild, record), view=LobbyView(record))
        except discord.HTTPException:
            log.exception("Не удалось обновить сообщение сбора после новой раздачи")

        captains = self.members_from_ids(guild, record.get("captains"))
        try:
            await message.channel.send(
                **await self.deal_message(
                    teams, lanes, champions, mode, notes + champion_notes,
                    captains=captains, round_no=record["round"],
                )
            )
        except discord.HTTPException:
            log.exception("Не удалось опубликовать новую раздачу")
        log.info("Новая раздача в сборе %s: катка %d", message.id, record["round"])

    # --- режимы -----------------------------------------------------------

    async def run_mode(
        self, players: list[discord.Member], mode: dict
    ) -> tuple[list[list[discord.Member]], dict[int, str], dict, list[str]]:
        """Команды, линии и чемпионы по режиму без капитанов."""
        notes: list[str] = []
        if mode["teams"] == modes.TEAMS_BALANCED:
            balanced = modes.balanced_teams(players)
            if balanced is not None:
                teams, lanes = balanced
            else:
                teams = modes.random_teams(players)
                lanes, _ = self.assign_team_lanes(teams, modes.LANES_RANDOM)
                notes.append(
                    "Собрать команды по отмеченным линиям не вышло: нужно ровно 10 игроков "
                    "и подходящий набор ролей. Команды поделены случайно, линии розданы случайно."
                )
        elif mode["teams"] == modes.TEAMS_SKILL:
            stats_cog = self.bot.get_cog("Stats")
            guild_id = players[0].guild.id

            def rating(member: discord.Member) -> float:
                return stats_cog.stats.player(guild_id, member.id).elo if stats_cog else 1000.0

            teams = modes.skill_teams(players, rating)
            lanes, lane_notes = self.assign_team_lanes(teams, mode["lanes"])
            notes += lane_notes
            if stats_cog is None or not any(stats_cog.stats.player(guild_id, m.id).games for m in players):
                notes.append("Статистики пока нет — команды поделены случайно. Отмечайте победы кнопками в посте сбора.")
            else:
                strength = [sum(rating(m) for m in team) / len(team) for team in teams if team]
                notes.append(
                    "Команды уравнены по Elo: в среднем "
                    + " против ".join(f"{value:.0f}" for value in strength) + "."
                )
        else:
            teams = modes.random_teams(players)
            lanes, lane_notes = self.assign_team_lanes(teams, mode["lanes"])
            notes += lane_notes

        champions, champion_notes = await self.deal_for(teams, lanes, mode)
        return teams, lanes, champions, notes + champion_notes

    def assign_team_lanes(
        self, teams: list[list[discord.Member]], how: str, previous: dict[int, str] | None = None,
    ) -> tuple[dict[int, str], list[str]]:
        lanes: dict[int, str] = {}
        notes: list[str] = []
        for team, side_name in zip(teams, (BLUE_SIDE, RED_SIDE)):
            if how != modes.LANES_FREE and len(team) > len(config.LANES):
                notes.append(f"{side_name}: игроков больше пяти — линии не раздавались.")
                continue
            team_lanes, satisfied = modes.assign_lanes(team, how, previous)
            lanes.update(team_lanes)
            if not satisfied:
                wanted = "на отмеченные линии" if how == modes.LANES_MAIN else "не на свои линии"
                notes.append(f"{side_name}: поставить всех {wanted} не получилось — линии розданы случайно.")
        return lanes, notes

    async def deal_for(
        self,
        teams: list[list[discord.Member]],
        lanes: dict[int, str],
        mode: dict,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[dict, list[str]]:
        """Чемпионы по режиму. exclude — имена, которые не выдавать (чемпионы прошлой катки)."""
        if mode["champs"] == modes.CHAMPS_FREE:
            return {}, []
        try:
            pool = await POOL.all()
        except ChampionsUnavailable:
            log.exception("Не удалось получить список чемпионов для раздачи")
            return {}, ["Список чемпионов сейчас недоступен (Riot Data Dragon не отвечает) — выберите чемпионов сами."]
        if exclude:
            fresh = [champion for champion in pool if champion.name not in exclude]
            # Если без прошлых чемпионов останется совсем мало — раздаём из всех.
            if len(fresh) >= 60:
                pool = fresh
        guild_id = next((str(member.guild.id) for team in teams for member in team), None)
        history = self.champion_history.data.setdefault(guild_id, {}) if guild_id else {}
        dealt = modes.deal_champions(teams, lanes, mode["champs"], pool, last_seen=history)
        if guild_id:
            now = time.time()
            for champions in dealt.values():
                for champion in champions:
                    history[champion.id] = now
            await self.champion_history.save()
        return dealt, []

    @staticmethod
    def pack_deal(lanes: dict[int, str], champions: dict) -> dict:
        """Раздача в виде, пригодном для JSON: ключи — строки, чемпионы — имена."""
        return {
            "lanes": {str(user_id): lane for user_id, lane in lanes.items()},
            "champions": {
                str(user_id): [champion.name for champion in picks] for user_id, picks in champions.items()
            },
        }

    @staticmethod
    def deal_lanes(record: dict) -> dict[int, str]:
        deal = record.get("deal") or {}
        return {int(user_id): lane for user_id, lane in (deal.get("lanes") or {}).items()}

    async def move_teams(
        self,
        guild: discord.Guild,
        record: dict,
        blue_channel: discord.VoiceChannel,
        red_channel: discord.VoiceChannel,
    ) -> tuple[int, list[str]]:
        moved = 0
        skipped: list[str] = []
        for ids, target in zip(record.get("teams") or [], (blue_channel, red_channel)):
            for user_id in ids:
                member = guild.get_member(user_id)
                if member is None or member.voice is None:
                    skipped.append(member.mention if member else f"<@{user_id}>")
                    continue
                try:
                    await member.move_to(target, reason="Развод составов из лобби")
                    moved += 1
                except discord.HTTPException:
                    skipped.append(member.mention)
        return moved, skipped

    # --- оформление -------------------------------------------------------

    def build_embed(self, guild: discord.Guild, record: dict) -> discord.Embed:
        closed = record.get("closed", False)
        kind = record.get("kind", KIND_SCRIM)
        stage = record.get("stage", STAGE_SIGNUP)
        target = record.get("target", config.TEAM_SIZE)

        mode = modes.get_mode(record)
        series = record.get("series", DEFAULT_SERIES)
        title = KIND_TITLES.get(kind, KIND_TITLES[KIND_SCRIM])
        if kind != KIND_SCRIM:
            title += f" · Bo{series}"
        if closed:
            title += " · отменён"

        # Крупно — когда; мелким серым — подробности: так карточка читается с одного взгляда.
        start_at = record.get("start_at")
        if start_at:
            # Discord сам покажет время в часовом поясе каждого, а «через 2 часа» будет тикать.
            lines = [f"### 🕒 <t:{int(start_at)}:f> · <t:{int(start_at)}:R>"]
        else:
            lines = [f"### 🕒 {record['time']}"]
        details = []
        if kind != KIND_SCRIM:
            mode_text = modes.mode_title(mode)
            if modes.find_preset(mode) is None:
                mode_text += f" — {modes.mode_summary(mode)}"
            details.append(f"Режим {mode_text}")
        if record.get("opponent"):
            details.append(f"⚔️ Соперник {record['opponent']}")
        if details:
            lines.append("-# " + " · ".join(details))
        if record.get("note"):
            lines.append(f"> {record['note']}")
        if record.get("results"):
            lines.append(self.score_line(record))

        going = len(self.signed_players(guild, record))
        members_in = self.members_from_ids(guild, record[STATUS_IN])
        subs = len(record[STATUS_SUB])
        captains = self.members_from_ids(guild, record.get("captains"))
        teams = [self.members_from_ids(guild, ids) for ids in (record.get("teams") or [])]

        if len(teams) != 2 and not closed:
            lines.append("")
            lines.append(fill_line(len(members_in), target, subs))
        if closed:
            color = discord.Color.dark_grey()
        elif len(teams) == 2:
            color = COLOR_READY
        else:
            color = fill_color(len(members_in), target)

        embed = discord.Embed(title=title, description="\n".join(lines), color=color)

        lanes = self.deal_lanes(record)
        if len(teams) == 2:
            for side, (team, side_name) in enumerate(zip(teams, (BLUE_SIDE, RED_SIDE))):
                captain = captains[side] if len(captains) == 2 else None
                if lanes:
                    value = modes.team_lines(team, lanes, captain=captain)
                else:
                    value = format_players(team, numbered=True, show_lanes=False, mention=False, captain=captain)
                embed.add_field(name=f"{side_name} ({len(team)})", value=value, inline=True)
        else:
            for name, value in slot_columns(members_in, target, STATUS_TITLES[STATUS_IN]):
                embed.add_field(name=name, value=value, inline=True)
            if len(captains) == 2:
                embed.add_field(
                    name="👑 Капитаны",
                    value=f"{BLUE_SIDE} — {captains[0].mention}\n{RED_SIDE} — {captains[1].mention}",
                    inline=False,
                )
        # Запасные и отказавшиеся — компактно, одной строкой каждые, под основным составом.
        extra = []
        for status in (STATUS_SUB, STATUS_OUT):
            members = self.members_from_ids(guild, record[status])
            if members:
                extra.append(f"{STATUS_TITLES[status]} · {len(members)}: " + ", ".join(m.mention for m in members))
        if extra:
            embed.add_field(name="\u200b", value="\n".join(extra), inline=False)
        if start_at and not closed:
            embed.timestamp = datetime.fromtimestamp(start_at, tz=timezone.utc)

        if closed:
            footer = "Сбор отменён"
        elif len(teams) == 2:
            footer = "Составы готовы — можно раскидать по каналам"
            footer += f" · катка {record.get('round', 1)}: после игры отметьте победителя"
            if mode["lanes"] != modes.LANES_FREE or mode["champs"] != modes.CHAMPS_FREE:
                footer += ", новые чемпионы — «Новая раздача»"
        elif stage == STAGE_DRAFT:
            footer = "Идёт драфт"
        elif len(captains) == 2:
            footer = "Капитаны выбраны — можно начинать драфт"
        elif going >= target:
            if kind == KIND_SCRIM:
                footer = "Состав собран — ждём соперника"
            elif mode["teams"] == modes.TEAMS_DRAFT:
                footer = "Состав собран — можно ролить капитанов"
            else:
                footer = "Состав собран — можно запускать"
        else:
            footer = "Жми «Играю», чтобы попасть в состав, или «Запасной», если не уверен"
        embed.set_footer(text=footer)
        return embed

    # --- команды ----------------------------------------------------------

    async def _open_lobby(
        self,
        interaction: discord.Interaction,
        *,
        kind: str,
        target: int,
        when: str,
        opponent: str | None,
        note: str | None,
        mention: discord.Role | None,
        ping: str,
        series: int = DEFAULT_SERIES,
    ) -> None:
        warning: str | None = None
        # Массовый пинг доступен только тем, кто и сам может упоминать @everyone,
        # иначе через бота это мог бы сделать любой участник сервера.
        if ping != PING_NONE and not interaction.user.guild_permissions.mention_everyone:
            ping = PING_NONE
            warning = (
                "Объявление опубликовано без пинга: у вас нет права «Упоминать @everyone» на сервере."
            )
        elif ping != PING_NONE and not interaction.app_permissions.mention_everyone:
            ping = PING_NONE
            warning = (
                "Объявление опубликовано без пинга: у бота нет права «Упоминать @everyone» в этом канале."
            )
        # Неупоминаемую роль бот с правом «Упоминать @everyone» пинганул бы за любого участника.
        if (
            mention is not None
            and not mention.mentionable
            and not interaction.user.guild_permissions.mention_everyone
        ):
            warning = " ".join(
                filter(None, [warning, f"Роль {mention.name} не упомянута: её могут пинговать только модераторы."])
            )
            mention = None

        start = parse_when(when, datetime.now(config.TIMEZONE))
        record = {
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel_id,
            "author_id": interaction.user.id,
            "kind": kind,
            "target": target,
            "time": when,
            "opponent": opponent,
            "note": note,
            "created_at": time.time(),
            "start_at": start.timestamp() if start else None,
            "reminded": False,
            "closed": False,
            "stage": STAGE_SIGNUP,
            "captains": None,
            "teams": None,
            "mode": modes.DEFAULT_PRESET.mode,
            "series": series,
            "deal": None,
            STATUS_IN: [],
            STATUS_SUB: [],
            STATUS_OUT: [],
        }

        parts = [PING_TEXT[ping]] if ping in PING_TEXT else []
        if mention is not None:
            parts.append(mention.mention)

        await interaction.response.send_message(
            content=" ".join(parts) or None,
            embed=self.build_embed(interaction.guild, record),
            view=LobbyView(record),
            allowed_mentions=discord.AllowedMentions(
                everyone=ping != PING_NONE,
                roles=[mention] if mention else False,
            ),
        )
        message = await interaction.original_response()

        self.store.data[str(message.id)] = record
        await self.save()

        if warning:
            await interaction.followup.send(warning, ephemeral=True)

    @app_commands.command(name="scrim", description="Объявить сбор на скрим против другой команды")
    @app_commands.describe(
        when="Когда играем, например «сегодня 21:00»",
        opponent="Соперник, если уже известен",
        note="Дополнение: формат, патч, условия",
        ping="Кого позвать (по умолчанию @everyone)",
        mention="Дополнительно упомянуть конкретную роль",
        slots="Сколько игроков нужно (по умолчанию 5)",
    )
    @app_commands.choices(ping=PING_CHOICES)
    @app_commands.guild_only()
    async def scrim(
        self,
        interaction: discord.Interaction,
        when: str,
        opponent: str | None = None,
        note: str | None = None,
        ping: app_commands.Choice[str] | None = None,
        mention: discord.Role | None = None,
        slots: app_commands.Range[int, 2, 20] = config.TEAM_SIZE,
    ) -> None:
        await self._open_lobby(
            interaction,
            kind=KIND_SCRIM,
            target=slots,
            when=when,
            opponent=opponent,
            note=note,
            mention=mention,
            ping=ping.value if ping else PING_EVERYONE,
        )

    @app_commands.command(name="custom", description="Объявить сбор на кастомку 5×5 — нужно 10 человек")
    @app_commands.describe(
        when="Когда играем, например «сегодня 21:00»",
        note="Дополнение: режим, правила, условия",
        ping="Кого позвать (по умолчанию @everyone)",
        mention="Дополнительно упомянуть конкретную роль",
        slots="Сколько игроков нужно (по умолчанию 10)",
        series="Формат серии: после каждой катки отмечаете победителя (по умолчанию Bo3)",
    )
    @app_commands.choices(ping=PING_CHOICES, series=SERIES_CHOICES)
    @app_commands.guild_only()
    async def custom(
        self,
        interaction: discord.Interaction,
        when: str,
        note: str | None = None,
        ping: app_commands.Choice[str] | None = None,
        mention: discord.Role | None = None,
        slots: app_commands.Range[int, 2, 20] = config.TEAM_SIZE * 2,
        series: app_commands.Choice[int] | None = None,
    ) -> None:
        await self._open_lobby(
            interaction,
            kind=KIND_CUSTOM,
            target=slots,
            when=when,
            opponent=None,
            note=note,
            mention=mention,
            ping=ping.value if ping else PING_EVERYONE,
            series=series.value if series else DEFAULT_SERIES,
        )

    def latest_open_record(self, guild: discord.Guild) -> tuple[str, dict] | None:
        """Последний непогашенный сбор сервера — к нему привязаны /roster и /ping."""
        now = time.time()
        records = [
            (message_id, record)
            for message_id, record in self.store.data.items()
            if record["guild_id"] == guild.id
            and not record.get("closed")
            and now - max(record.get("created_at", now), record.get("start_at") or 0) < LOBBY_STALE_AFTER
        ]
        if not records:
            return None
        return max(records, key=lambda item: item[1].get("created_at", 0))

    @app_commands.command(name="ping", description="Позвать тех, кто записался на последний сбор")
    @app_commands.describe(
        text="Что написать, например «заходим в голосовой»",
        who="Кого звать (по умолчанию — основной состав)",
    )
    @app_commands.choices(
        who=[
            app_commands.Choice(name="Состав", value=PING_WHO_ROSTER),
            app_commands.Choice(name="Состав и запасных", value=PING_WHO_ALL),
            app_commands.Choice(name="Только запасных", value=PING_WHO_SUBS),
        ]
    )
    @app_commands.guild_only()
    async def ping(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        who: app_commands.Choice[str] | None = None,
    ) -> None:
        found = self.latest_open_record(interaction.guild)
        if found is None:
            await respond(interaction, "Активных сборов нет. Создайте новый через `/custom` или `/scrim`.")
            return
        message_id, record = found

        # Звать людей может организатор или тот, кто сам записан на этот сбор.
        participants = set(record[STATUS_IN]) | set(record[STATUS_SUB]) | set(record[STATUS_OUT])
        if not (self.is_organizer(interaction, record) or interaction.user.id in participants):
            await respond(interaction, "Позвать состав может организатор сбора или тот, кто в нём записан.")
            return

        target = who.value if who else PING_WHO_ROSTER
        statuses = {
            PING_WHO_ROSTER: (STATUS_IN,),
            PING_WHO_ALL: (STATUS_IN, STATUS_SUB),
            PING_WHO_SUBS: (STATUS_SUB,),
        }[target]

        members: list[discord.Member] = []
        for status in statuses:
            for member in self.members_from_ids(interaction.guild, record[status]):
                if member not in members:
                    members.append(member)

        if not members:
            await respond(interaction, "Звать некого — в этом списке пока никто не записан.")
            return

        kind = record.get("kind", KIND_SCRIM)
        title = KIND_TITLES.get(kind, KIND_TITLES[KIND_SCRIM])
        jump_url = f"https://discord.com/channels/{interaction.guild.id}/{record['channel_id']}/{message_id}"

        lines = [f"🔔 **{title}** · {record['time']}"]
        if text:
            lines.append(text)
        lines.append(" ".join(member.mention for member in members))
        lines.append(f"[Пост сбора]({jump_url})")

        await interaction.response.send_message(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=members),
        )

    @app_commands.command(name="roster", description="Показать состав последнего открытого сбора")
    @app_commands.guild_only()
    async def roster(self, interaction: discord.Interaction) -> None:
        found = self.latest_open_record(interaction.guild)
        if found is None:
            await respond(interaction, "Открытых сборов нет. Создайте новый через `/scrim` или `/custom`.")
            return

        _message_id, record = found
        await respond(interaction, embed=self.build_embed(interaction.guild, record))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Lobby(bot))
