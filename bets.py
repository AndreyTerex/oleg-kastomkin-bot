"""Ставки на катки «Олежкиными коинами» (data/wallet.json).

- У каждого кошелёк: старт START_COINS, раз в сутки бонус DAILY_BONUS (по /coins).
- На каждую катку сбора открывается пул: ставить можно BET_MINUTES после раздачи. Одна ставка на человека,
  до закрытия её можно поменять. Игроки катки ставят только на свою команду — сливать нельзя.
- Расчёт тотализатором: победители забирают свою ставку и делят проигравший банк пропорционально ставкам.
  Если на проигравших никто не ставил — ставка возвращается с бонусом NO_RIVAL_BONUS.
- Отмена результата откатывает выплаты, а отмена или пересбор кастомки возвращают нерассчитанные ставки.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import config
from storage import JsonStore

START_COINS = 1000
DAILY_BONUS = 100
NO_RIVAL_BONUS = 0.1
BET_AMOUNTS = (50, 100, 250, 500, 1000)


class BetError(ValueError):
    """Ставку принять нельзя — текст для пользователя."""


def settle(bets: dict[str, dict], winner: int) -> dict[str, int]:
    """Сколько каждому вернуть (ставка уже списана). Пусто для проигравших."""
    win = {uid: bet["amount"] for uid, bet in bets.items() if bet["side"] == winner}
    lose_pool = sum(bet["amount"] for bet in bets.values() if bet["side"] != winner)
    win_pool = sum(win.values())
    if not win:
        return {}
    if lose_pool == 0:
        return {uid: amount + max(1, round(amount * NO_RIVAL_BONUS)) for uid, amount in win.items()}
    return {uid: amount + int(lose_pool * amount / win_pool) for uid, amount in win.items()}


class Wallets:
    def __init__(self, path: Path | None = None) -> None:
        self.store = JsonStore(path or Path(config.DATA_DIR) / "wallet.json")

    def load(self) -> None:
        self.store.load()

    async def save(self) -> None:
        await self.store.save()

    def _guild(self, guild_id: int) -> dict:
        guild = self.store.data.setdefault(str(guild_id), {})
        guild.setdefault("users", {})
        guild.setdefault("pools", {})
        return guild

    def _user(self, guild_id: int, user_id: int) -> dict:
        return self._guild(guild_id)["users"].setdefault(str(user_id), {"coins": START_COINS})

    def balance(self, guild_id: int, user_id: int) -> int:
        return self._user(guild_id, user_id)["coins"]

    def claim_daily(self, guild_id: int, user_id: int, today: str | None = None) -> int:
        """Ежедневный бонус: сколько начислено (0 — сегодня уже брал)."""
        user = self._user(guild_id, user_id)
        today = today or datetime.now(config.TIMEZONE).strftime("%Y-%m-%d")
        if user.get("daily") == today:
            return 0
        user["daily"] = today
        user["coins"] += DAILY_BONUS
        return DAILY_BONUS

    def richest(self, guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
        users = self._guild(guild_id)["users"]
        ranked = sorted(((int(uid), u["coins"]) for uid, u in users.items()), key=lambda item: -item[1])
        return ranked[:limit]

    # --- пулы ставок -----------------------------------------------------------

    @staticmethod
    def pool_key(lobby_id: int, round_no: int) -> str:
        return f"{lobby_id}:{round_no}"

    def open_pool(self, guild_id: int, lobby_id: int, round_no: int, now: float | None = None) -> dict:
        """Открывает приём ставок на катку (если пул уже есть — не трогает)."""
        pools = self._guild(guild_id)["pools"]
        key = self.pool_key(lobby_id, round_no)
        if key not in pools:
            pools[key] = {"open_until": (time.time() if now is None else now) + config.BET_MINUTES * 60, "bets": {}}
        return pools[key]

    def pool(self, guild_id: int, lobby_id: int, round_no: int) -> dict | None:
        return self._guild(guild_id)["pools"].get(self.pool_key(lobby_id, round_no))

    def place(
        self, guild_id: int, lobby_id: int, round_no: int, user_id: int, side: int, amount: int,
        teams: list[list[int]], now: float | None = None,
    ) -> int:
        """Ставит (или меняет ставку). Возвращает новый баланс. Ошибки — BetError с текстом."""
        pool = self.pool(guild_id, lobby_id, round_no)
        now = time.time() if now is None else now
        if pool is None or pool.get("settled") is not None or now > pool["open_until"]:
            raise BetError("Ставки на эту катку уже не принимаются.")
        for index, team in enumerate(teams):
            if user_id in team and index != side:
                raise BetError("Ты играешь за другую команду — ставить можно только на своих.")
        if amount <= 0:
            raise BetError("Ставка должна быть больше нуля.")
        user = self._user(guild_id, user_id)
        previous = pool["bets"].get(str(user_id))
        available = user["coins"] + (previous["amount"] if previous else 0)
        if amount > available:
            raise BetError(f"Не хватает коинов: у тебя {available}.")
        user["coins"] = available - amount
        pool["bets"][str(user_id)] = {"side": side, "amount": amount}
        return user["coins"]

    def settle_pool(self, guild_id: int, lobby_id: int, round_no: int, winner: int) -> dict[str, int]:
        """Расчёт пула: начисляет выигрыши, запоминает их для отмены. Возвращает выплаты."""
        pool = self.pool(guild_id, lobby_id, round_no)
        if pool is None or pool.get("settled") is not None:
            return {}
        payouts = settle(pool["bets"], winner)
        for uid, amount in payouts.items():
            self._guild(guild_id)["users"][uid]["coins"] += amount
        pool["settled"] = {"winner": winner, "payouts": payouts}
        return payouts

    def unsettle_pool(self, guild_id: int, lobby_id: int, round_no: int) -> None:
        """Отмена результата: забрать выплаты, пул снова ждёт результата (ставки в нём остаются)."""
        pool = self.pool(guild_id, lobby_id, round_no)
        if pool is None or pool.get("settled") is None:
            return
        for uid, amount in pool["settled"]["payouts"].items():
            user = self._guild(guild_id)["users"].get(uid)
            if user:
                user["coins"] -= amount
        pool["settled"] = None

    def refund_lobby(self, guild_id: int, lobby_id: int) -> int:
        """Вернуть все нерассчитанные ставки сбора (кастомку отменили или пересобрали). Сколько ставок вернули."""
        pools = self._guild(guild_id)["pools"]
        refunded = 0
        for key in [k for k in pools if k.startswith(f"{lobby_id}:")]:
            pool = pools[key]
            if pool.get("settled") is None:
                for uid, bet in pool["bets"].items():
                    self._user(guild_id, int(uid))["coins"] += bet["amount"]
                    refunded += 1
                del pools[key]
        return refunded

    def totals(self, pool: dict) -> tuple[int, int]:
        blue = sum(b["amount"] for b in pool["bets"].values() if b["side"] == 0)
        red = sum(b["amount"] for b in pool["bets"].values() if b["side"] == 1)
        return blue, red
