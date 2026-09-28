"""Конфигурация бота: токен, идентификаторы ролей и описание линий LoL."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import discord

try:
    from dotenv import load_dotenv
except ImportError:  # без python-dotenv переменные задаются окружением (так работает Docker)
    load_dotenv = None

# Файл .env рядом с bot.py подхватывается и при запуске без Docker. Уже заданные переменные
# окружения (например, из docker compose) важнее файла и не перезаписываются.
if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Токен берётся только из окружения — в коде и в репозитории его быть не должно.
TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()

# Если указать GUILD_ID, команды синхронизируются мгновенно на одном сервере.
# Без него — глобально, но обновление у клиентов занимает до часа.
GUILD_ID: int = _env_int("GUILD_ID")

# Бесплатный ключ Groq для болтовни Олега в чате. Без него Олег в чате молчит,
# остальной бот работает как обычно.
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
# Модели по порядку: если у первой кончился лимит, отвечает следующая.
GROQ_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("GROQ_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-120b").split(",")
    if model.strip()
]

# Бесплатный ключ OpenRouter: модели поумнее, но около 50 запросов в день.
# Отвечают первыми, после лимита Олег переключается на Groq.
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("OPENROUTER_MODELS", "nvidia/nemotron-3-ultra-550b-a55b:free").split(",")
    if model.strip()
]

# Ключ TokenHarbor: бесплатные модели DeepSeek с недельным лимитом, никогда не списывают деньги.
# В тестах самые смешные и быстрые — поэтому первые в цепочке.
TOKENHARBOR_API_KEY: str = os.getenv("TOKENHARBOR_API_KEY", "").strip()
TOKENHARBOR_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("TOKENHARBOR_MODELS", "deepseek-v4.1-flash:free,deepseek-v4-flash:free").split(",")
    if model.strip()
]

# Токен Hugging Face: сильные модели (по умолчанию DeepSeek V3.2), но всего $0.10 бесплатно в месяц —
# это около 170 ответов. Суффикс :cheapest выбирает самого дешёвого провайдера модели.
HF_TOKEN: str = os.getenv("HF_TOKEN", "").strip()
HF_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("HF_MODELS", "deepseek-ai/DeepSeek-V3.2:cheapest").split(",")
    if model.strip()
]

# Бесплатный ключ Mistral: все модели, около миллиарда токенов в месяц — почти безлимитный запас.
MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("MISTRAL_MODELS", "mistral-medium-latest").split(",")
    if model.strip()
]

# Каталог для сохранения состояния (в Docker смонтирован как volume).
DATA_DIR: str = os.getenv("DATA_DIR", "data")

TEAM_SIZE = 5


@dataclass(frozen=True)
class Lane:
    """Линия в League of Legends и связанная с ней роль Discord."""

    key: str
    label: str
    title: str
    emoji: str
    description: str
    style: discord.ButtonStyle
    role_id: int


LANES: tuple[Lane, ...] = (
    Lane(
        key="top",
        label="Top",
        title="Топ",
        emoji="🗡️",
        description="верхняя линия",
        style=discord.ButtonStyle.danger,
        role_id=_env_int("ROLE_ID_TOP", 1544004430049968148),
    ),
    Lane(
        key="jungle",
        label="Jungle",
        title="Лес",
        emoji="🌲",
        description="лесник, контроль объектов",
        style=discord.ButtonStyle.success,
        role_id=_env_int("ROLE_ID_JUNGLE", 1544004604893729010),
    ),
    Lane(
        key="mid",
        label="Mid",
        title="Мид",
        emoji="☄️",
        description="центральная линия",
        style=discord.ButtonStyle.primary,
        role_id=_env_int("ROLE_ID_MID", 1544004754315681924),
    ),
    Lane(
        key="adc",
        label="ADC",
        title="Стрелок",
        emoji="🏹",
        description="нижняя линия, основной урон",
        style=discord.ButtonStyle.secondary,
        role_id=_env_int("ROLE_ID_ADC", 1544004593967435887),
    ),
    Lane(
        key="support",
        label="Support",
        title="Саппорт",
        emoji="🛡️",
        description="поддержка на нижней линии",
        style=discord.ButtonStyle.secondary,
        role_id=_env_int("ROLE_ID_SUPPORT", 1544004811886829719),
    ),
)

LANE_BY_KEY: dict[str, Lane] = {lane.key: lane for lane in LANES}
