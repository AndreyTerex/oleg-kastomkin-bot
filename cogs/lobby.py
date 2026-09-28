"""Сборы: скрим против соперника и внутренняя кастомка 5×5.

Лобби ведёт сбор по стадиям: запись → капитаны → драфт → развод по каналам.
Состояние пишется на диск, поэтому переживает перезапуск контейнера.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
import modes
from champions import POOL, ChampionsUnavailable
from cogs.scrim import DraftView
from storage import JsonStore
from utils import BLUE_SIDE, NEUTRAL, RED_SIDE, format_players, player_name, respond, shuffled

log = logging.getLogger("scrimbot.lobby")

STATUS_IN = "in"
STATUS_SUB = "sub"
STATUS_OUT = "out"

STATUS_TITLES = {
    STATUS_IN: "✅ Играют",
    STATUS_SUB: "🕐 Запасные",
    STATUS_OUT: "❌ Не смогут",
}

KIND_SCRIM = "scrim"
KIND_CUSTOM = "custom"

KIND_TITLES = {
    KIND_SCRIM: "Сбор на скрим",
    KIND_CUSTOM: "Сбор на кастомку 5×5",
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
LOBBY_STALE_AFTER = 36 * 60 * 60


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
                "Если кто-то отпишется, организатор переведёт вас в состав."
            )

        for key in (STATUS_IN, STATUS_SUB, STATUS_OUT):
            if user_id in record[key]:
                record[key].remove(user_id)
        if status is not None:
            record[status].append(user_id)

        await cog.save()
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )
        if note:
            await interaction.followup.send(note, ephemeral=True)

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
        record["stage"] = STAGE_DONE
        await cog.save()

        await interaction.message.edit(embed=cog.build_embed(interaction.guild, record), view=LobbyView(record))
        await interaction.followup.send(
            embed=modes.deal_embed(teams, lanes, champions, mode, notes, patch=POOL.version)
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

        scrim_cog = interaction.client.get_cog("Scrim")
        if scrim_cog is None:
            await respond(interaction, "Модуль драфта не загружен — посмотрите логи бота.")
            return

        target = record.get("target", config.TEAM_SIZE)
        roster = cog.signed_players(interaction.guild, record)[:target]
        pool = [player for player in roster if player not in captains]
        if not pool:
            await respond(interaction, "Кроме капитанов в составе никого нет.")
            return

        record["stage"] = STAGE_DRAFT
        record["teams"] = None
        record["deal"] = None
        await cog.save()

        lobby_message = interaction.message

        async def on_finish(teams: list[list[discord.Member]]) -> None:
            await cog.finish_draft(lobby_message, record, teams)

        draft = DraftView(scrim_cog, captains, shuffled(pool), on_finish=on_finish)
        await interaction.response.edit_message(
            embed=cog.build_embed(interaction.guild, record), view=LobbyView(record)
        )
        draft.message = await interaction.followup.send(embed=draft.build_embed(), view=draft, wait=True)

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
        changed: list[int] = []
        overflow: list[int] = []

        for user_id in (int(value) for value in self.select.values):
            for key in (STATUS_IN, STATUS_SUB, STATUS_OUT):
                if user_id in self.record[key]:
                    self.record[key].remove(user_id)

            # В состав больше нужного не пускаем даже организатора — иначе счётчик врёт.
            if target_status == STATUS_IN and len(self.record[STATUS_IN]) >= limit:
                self.record[STATUS_SUB].append(user_id)
                overflow.append(user_id)
                continue

            if target_status is not None:
                self.record[target_status].append(user_id)
            changed.append(user_id)

        await self.cog.save()
        await self.lobby_message.edit(
            embed=self.cog.build_embed(guild, self.record), view=LobbyView(self.record)
        )
        self.refresh_options()

        lines = []
        if changed:
            lines.append(f"{verb}: {self._names(guild, changed)}.")
        if overflow:
            lines.append(
                f"В состав не поместились (уже {limit}/{limit}), оставлены в запасе: "
                f"{self._names(guild, overflow)}."
            )
        lines.append("Можно править дальше — список обновлён.")
        await interaction.response.edit_message(content="\n".join(lines), view=self)

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
        self._refresh_task: asyncio.Task | None = None
        # Сборы, где прямо сейчас идёт новая раздача: двойной клик не должен раздать дважды.
        self._rerolling: set[int] = set()

    async def cog_load(self) -> None:
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
        if trimmed:
            log.info("Состав подрезан до нормы у сборов: %s", ", ".join(trimmed))
        if len(fresh) != len(data) or trimmed:
            self.store.data = fresh
            await self.store.save()
        self.bot.add_view(LobbyView())
        log.info("Активных сборов восстановлено: %d", len(fresh))
        self._refresh_task = asyncio.create_task(self._refresh_messages())

    async def cog_unload(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()

    async def _refresh_messages(self) -> None:
        """Перерисовывает сохранённые сообщения сборов после перезапуска.

        Состав берётся из файла и не меняется — обновляется только вид сообщения,
        чтобы кнопки соответствовали текущим правилам.
        """
        await self.bot.wait_until_ready()
        closed_missing = 0
        for message_id, record in list(self.store.data.items()):
            channel = self.bot.get_channel(record["channel_id"])
            if channel is None:
                log.warning("Канал %s для сбора %s недоступен", record["channel_id"], message_id)
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(
                    embed=self.build_embed(channel.guild, record), view=LobbyView(record)
                )
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
        log.info("Сообщения сборов перерисованы")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Удалили пост сбора — сбор больше не открыт, иначе /ping и Олег продолжат на него ссылаться."""
        record = self.store.data.get(str(payload.message_id))
        if record is not None and not record.get("closed"):
            record["closed"] = True
            await self.save()
            log.info("Пост сбора %s удалён — сбор закрыт", payload.message_id)

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
        return True

    def get_record(self, message_id: int) -> dict | None:
        return self.store.data.get(str(message_id))

    async def save(self) -> None:
        await self.store.save()

    def is_organizer(self, interaction: discord.Interaction, record: dict) -> bool:
        """Управлять ходом сбора может автор объявления или организатор ивентов."""
        return (
            interaction.user.id == record["author_id"]
            or interaction.user.guild_permissions.manage_events
        )

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
                embed=modes.deal_embed(
                    teams, lanes, champions, mode, notes + champion_notes, captains=captains, patch=POOL.version
                )
            )
        except discord.HTTPException:
            log.exception("Не удалось опубликовать раздачу после драфта")

    async def reroll_deal(self, message: discord.Message, record: dict) -> None:
        """Новая раздача после катки: составы те же, линии и чемпионы заново, без чемпионов прошлой катки."""
        guild = message.guild
        mode = modes.get_mode(record)
        teams = [self.members_from_ids(guild, ids) for ids in record["teams"]]
        previous = {
            name for names in ((record.get("deal") or {}).get("champions") or {}).values() for name in names
        }
        lanes, notes = self.assign_team_lanes(teams, mode["lanes"])
        champions, champion_notes = await self.deal_for(teams, lanes, mode, exclude=previous)

        record["round"] = record.get("round", 1) + 1
        record["deal"] = self.pack_deal(lanes, champions)
        await self.save()
        try:
            await message.edit(embed=self.build_embed(guild, record), view=LobbyView(record))
        except discord.HTTPException:
            log.exception("Не удалось обновить сообщение сбора после новой раздачи")

        captains = self.members_from_ids(guild, record.get("captains"))
        try:
            await message.channel.send(
                embed=modes.deal_embed(
                    teams, lanes, champions, mode, notes + champion_notes,
                    captains=captains, patch=POOL.version, round_no=record["round"],
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
        else:
            teams = modes.random_teams(players)
            lanes, lane_notes = self.assign_team_lanes(teams, mode["lanes"])
            notes += lane_notes

        champions, champion_notes = await self.deal_for(teams, lanes, mode)
        return teams, lanes, champions, notes + champion_notes

    def assign_team_lanes(
        self, teams: list[list[discord.Member]], how: str
    ) -> tuple[dict[int, str], list[str]]:
        lanes: dict[int, str] = {}
        notes: list[str] = []
        for team, side_name in zip(teams, (BLUE_SIDE, RED_SIDE)):
            if how != modes.LANES_FREE and len(team) > len(config.LANES):
                notes.append(f"{side_name}: игроков больше пяти — линии не раздавались.")
                continue
            team_lanes, satisfied = modes.assign_lanes(team, how)
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
        return modes.deal_champions(teams, lanes, mode["champs"], pool), []

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

        title = KIND_TITLES.get(kind, KIND_TITLES[KIND_SCRIM])
        if closed:
            title += " (отменён)"

        lines = [f"🕒 **Когда:** {record['time']}"]
        if record.get("opponent"):
            lines.append(f"⚔️ **Соперник:** {record['opponent']}")
        if record.get("note"):
            lines.append(f"📝 {record['note']}")
        mode = modes.get_mode(record)
        mode_line = f"🎮 **Режим:** {modes.mode_title(mode)}"
        if modes.find_preset(mode) is None:
            mode_line += f" — {modes.mode_summary(mode)}"
        lines.append(mode_line)

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.dark_grey() if closed else NEUTRAL,
        )

        captains = self.members_from_ids(guild, record.get("captains"))
        teams = [self.members_from_ids(guild, ids) for ids in (record.get("teams") or [])]

        lanes = self.deal_lanes(record)
        if len(teams) == 2:
            for side, (team, side_name) in enumerate(zip(teams, (BLUE_SIDE, RED_SIDE))):
                captain = captains[side] if len(captains) == 2 else None
                if lanes:
                    value = modes.team_lines(team, lanes, captain=captain)
                else:
                    value = format_players(team, numbered=True, show_lanes=False, mention=False, captain=captain)
                embed.add_field(name=f"{side_name} ({len(team)})", value=value, inline=True)
            for status in (STATUS_SUB, STATUS_OUT):
                members = self.members_from_ids(guild, record[status])
                if members:
                    embed.add_field(name=STATUS_TITLES[status], value=format_players(members), inline=False)
        else:
            for status, heading in STATUS_TITLES.items():
                members = self.members_from_ids(guild, record[status])
                name = f"{heading} {len(members)}/{target}" if status == STATUS_IN else heading
                embed.add_field(name=name, value=format_players(members), inline=False)
            if len(captains) == 2:
                embed.add_field(
                    name="👑 Капитаны",
                    value=f"{BLUE_SIDE} — {captains[0].mention}\n{RED_SIDE} — {captains[1].mention}",
                    inline=False,
                )

        going = len(self.signed_players(guild, record))
        if closed:
            footer = "Сбор отменён"
        elif len(teams) == 2:
            footer = "Составы готовы — можно раскидать по каналам"
            if mode["lanes"] != modes.LANES_FREE or mode["champs"] != modes.CHAMPS_FREE:
                footer += f" · катка {record.get('round', 1)}, после неё — «Новая раздача»"
        elif stage == STAGE_DRAFT:
            footer = "Идёт драфт"
        elif len(captains) == 2:
            footer = "Капитаны выбраны — можно начинать драфт"
        elif going >= target:
            footer = (
                "Состав собран — можно ролить капитанов"
                if mode["teams"] == modes.TEAMS_DRAFT
                else "Состав собран — можно запускать"
            )
            waiting = len(record[STATUS_SUB])
            if waiting:
                footer += f" · в запасе {waiting}"
        else:
            footer = f"Не хватает игроков: {target - going}"
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
            "closed": False,
            "stage": STAGE_SIGNUP,
            "captains": None,
            "teams": None,
            "mode": modes.DEFAULT_PRESET.mode,
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
    )
    @app_commands.choices(ping=PING_CHOICES)
    @app_commands.guild_only()
    async def custom(
        self,
        interaction: discord.Interaction,
        when: str,
        note: str | None = None,
        ping: app_commands.Choice[str] | None = None,
        mention: discord.Role | None = None,
        slots: app_commands.Range[int, 2, 20] = config.TEAM_SIZE * 2,
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
        )

    def latest_open_record(self, guild: discord.Guild) -> tuple[str, dict] | None:
        """Последний непогашенный сбор сервера — к нему привязаны /roster и /ping."""
        now = time.time()
        records = [
            (message_id, record)
            for message_id, record in self.store.data.items()
            if record["guild_id"] == guild.id
            and not record.get("closed")
            and now - record.get("created_at", now) < LOBBY_STALE_AFTER
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
