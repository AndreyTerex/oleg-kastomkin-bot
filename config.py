"""Конфигурация бота: токен, идентификаторы ролей и описание линий LoL."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    for model in os.getenv("GROQ_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b").split(",")
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

# Токен Puter (puter.com): сильные модели разных компаний через OpenAI-совместимый API. Платит владелец токена:
# сначала тратится бесплатная месячная квота аккаунта, потом Puter просит пополнить баланс — бот тогда
# пропускает Puter и отвечает другими моделями. По умолчанию — Grok: сначала быстрый и недорогой 4.1 Fast
# (квоты хватает дольше), потом флагманский 4.6, в конце самая дешёвая GPT.
PUTER_AUTH_TOKEN: str = os.getenv("PUTER_AUTH_TOKEN", "").strip()
PUTER_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("PUTER_MODELS", "x-ai/grok-4.1-fast,x-ai/grok-4.6,gpt-5.4-nano").split(",")
    if model.strip()
]

# Ключ Z.ai (z.ai, регистрация по почте → профиль → API Keys): бесплатные модели GLM Flash, около запроса в секунду.
ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "").strip()
ZAI_MODELS: list[str] = [
    model.strip() for model in os.getenv("ZAI_MODELS", "glm-4.7-flash,glm-4.5-flash").split(",") if model.strip()
]

# Токен Hugging Face: сильные модели (по умолчанию DeepSeek V3.2), но всего $0.10 бесплатно в месяц —
# это около 170 ответов. Суффикс :cheapest выбирает самого дешёвого провайдера модели.
HF_TOKEN: str = os.getenv("HF_TOKEN", "").strip()
HF_MODELS: list[str] = [
    model.strip()
    for model in os.getenv("HF_MODELS", "deepseek-ai/DeepSeek-V3.2:cheapest").split(",")
    if model.strip()
]

# Ключ Google AI Studio (aistudio.google.com → Get API key): бесплатные Gemini Flash — лучшие из бесплатных
# по-русски. Модели по порядку: самая сильная Flash, при перегрузке — следующая.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODELS: list[str] = [
    model.strip()
    for model in os.getenv(
        "GEMINI_MODELS",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite",
    ).split(",")
    if model.strip()
]
# Модели с крошечным бесплатным лимитом (20 запросов в день): их берегут для прямых обращений к Олегу
# и не переспрашивают при перегрузке. Считаются все Gemini с этими словами в названии, кроме «-lite».
GEMINI_SCARCE: list[str] = [
    part.strip().lower() for part in os.getenv("GEMINI_SCARCE", "flash").split(",") if part.strip()
]

# VPN для нейросетей, закрытых из России (см. vpn.py): подписка и/или ключи vless://, trojan://, vmess://.
# Бот сам запускает Xray как локальный прокси и ходит через него к нейросетям (LLM_PROXY_FOR) и к Discord.
VPN_SUBSCRIPTION: str = os.getenv("VPN_SUBSCRIPTION", "").strip()
VPN_URL: str = os.getenv("VPN_URL", "").strip()
# Необязательно: брать только серверы, в названии которых есть это (регулярное выражение), например «DE|NL|Finland».
VPN_FILTER: str = os.getenv("VPN_FILTER", "").strip()
VPN_PORT: int = _env_int("VPN_PORT", 10809)
# Страны ВЫХОДА, через которые не ходим: там Gemini и другие нейросети не работают.
VPN_EXCLUDE_COUNTRIES: list[str] = [
    code.strip().upper()
    for code in os.getenv("VPN_EXCLUDE_COUNTRIES", "RU,BY,CN,HK,MO,IR,KP,CU,SY").split(",")
    if code.strip()
]
# Фрагментация TLS ClientHello против DPI: auto — пробовать без неё, а если сервер не отвечает — с ней.
VPN_FRAGMENT: str = (os.getenv("VPN_FRAGMENT", "auto").strip().lower() or "auto")
if VPN_FRAGMENT not in ("auto", "on", "off"):
    VPN_FRAGMENT = "auto"
# Как часто проверять текущий сервер (минуты; один лёгкий запрос) и как часто перепроверять ВСЕ серверы (часы).
# Полная проверка идёт и сразу, если текущий сервер перестал отвечать.
VPN_RECHECK_MINUTES: int = max(1, _env_int("VPN_RECHECK_MINUTES", 12))
try:
    VPN_FULL_CHECK_HOURS: float = max(0.25, float(os.getenv("VPN_FULL_CHECK_HOURS", "3")))
except ValueError:
    VPN_FULL_CHECK_HOURS = 3.0
# Насколько новый сервер должен быть быстрее текущего, чтобы переключиться (0.3 = на 30%).
try:
    VPN_SWITCH_THRESHOLD: float = min(0.9, max(0.0, float(os.getenv("VPN_SWITCH_THRESHOLD", "0.3"))))
except ValueError:
    VPN_SWITCH_THRESHOLD = 0.3
# …и минимум на столько миллисекунд: смена сервера на секунды рвёт соединение с Discord.
VPN_SWITCH_MIN_GAIN_MS: int = max(0, _env_int("VPN_SWITCH_MIN_GAIN_MS", 150))
# Сколько серверов проверять одновременно и сколько секунд ждать ответа от каждого.
VPN_PROBE_CONCURRENCY: int = max(1, _env_int("VPN_PROBE_CONCURRENCY", 8))
VPN_PROBE_TIMEOUT: int = max(3, _env_int("VPN_PROBE_TIMEOUT", 6))
XRAY_BIN: str = os.getenv("XRAY_BIN", "xray").strip() or "xray"

# Google не пускает к Gemini из России («User location is not supported»). Если есть HTTP-прокси за рубежом,
# укажите его: http://логин:пароль@адрес:порт. С VPN_SUBSCRIPTION / VPN_URL прокси подставляется сам.
# Через прокси ходят провайдеры из LLM_PROXY_FOR (по умолчанию all — все нейросети).
VPN_PROXY_URL: str = f"http://127.0.0.1:{VPN_PORT}" if (VPN_SUBSCRIPTION or VPN_URL) else ""
LLM_PROXY: str = os.getenv("LLM_PROXY", "").strip() or VPN_PROXY_URL
# Сколько раз за запрос переспрашивать перегруженную модель (HTTP 503) перед переходом к следующей.
LLM_OVERLOAD_RETRIES: int = max(0, _env_int("LLM_OVERLOAD_RETRIES", 3))
LLM_PROXY_FOR: list[str] = [
    name.strip().lower() for name in os.getenv("LLM_PROXY_FOR", "all").split(",") if name.strip()
]
# Нейросети, которые из России работают только через VPN: пока VPN сломан, их не спрашивают напрямую
# (иначе отказ по региону отключил бы их на 15 минут), а пропускают до починки VPN.
# Сколько секунд ждать, пока VPN сменит сбойный сервер, прежде чем отдать ответ модели без VPN (Z.ai, Groq).
LLM_VPN_WAIT: int = max(0, _env_int("LLM_VPN_WAIT", 25))
LLM_VPN_ONLY: list[str] = [
    name.strip().lower() for name in os.getenv("LLM_VPN_ONLY", "gemini").split(",") if name.strip()
]
# Выбирая VPN-сервер, проверять, что через него отвечает Gemini (а не «страна не поддерживается»).
VPN_CHECK_GEMINI: bool = bool(GEMINI_API_KEY) and ("all" in LLM_PROXY_FOR or "gemini" in LLM_PROXY_FOR) and (
    os.getenv("VPN_CHECK_GEMINI", "1").strip().lower() not in ("0", "false", "no", "off", "нет")
)
# Discord через VPN (VPN_FOR_DISCORD=1). По умолчанию напрямую: через нестабильный VPN-сервер соединение
# с Discord рвалось, и нажатия кнопок в это время пропадали («Приложение не ответило вовремя»).
VPN_FOR_DISCORD: bool = os.getenv("VPN_FOR_DISCORD", "0").strip().lower() not in ("0", "false", "no", "off", "нет")

# Порядок провайдеров: первым отвечает первый, после его лимита — следующий. Первым — Gemini (самые сильные
# бесплатные модели), за ним Puter и Z.ai (если заданы ключи), TokenHarbor и OpenRouter. Groq отвечает
# быстро, но слабее — он почти в самом конце, только когда остальные на лимите.
LLM_ORDER: list[str] = [
    name.strip().lower()
    for name in os.getenv("LLM_ORDER", "gemini,puter,openrouter,tokenharbor,zai,groq,huggingface").split(",")
    if name.strip()
]

# Каталог для сохранения состояния (в Docker смонтирован как volume).
DATA_DIR: str = os.getenv("DATA_DIR", "data")

# Канал для тревог (ошибки, VPN лежит, все нейросети на лимите). Пусто — тревоги не шлются, /oleg-status работает.
ALERT_CHANNEL_ID: int = _env_int("ALERT_CHANNEL_ID")
# Синхронизация данных между компьютерами через закрытый канал Discord (см. sync.py). Пусто — выключена.
SYNC_CHANNEL_ID: int = _env_int("SYNC_CHANNEL_ID")
# Как часто выкладывать снимок данных, если они изменились (минуты).
SYNC_MINUTES: int = max(1, _env_int("SYNC_MINUTES", 5))
# Через сколько секунд без обновления замка считается, что основной бот выключен.
SYNC_LOCK_STALE: int = max(90, _env_int("SYNC_LOCK_STALE", 180))
# Сколько ежедневных копий data/ хранить в data/backups.
BACKUP_KEEP: int = max(1, _env_int("BACKUP_KEEP", 7))
# Сторожевой таймер: бот перезапускается, если завис или не может подключиться к Discord столько минут.
WATCHDOG_DISCONNECT_MINUTES: int = max(0, _env_int("WATCHDOG_DISCONNECT_MINUTES", 15))

# Канал для объявлений бота: итоги недели и приглашения на кастомку. Пусто — канал, где отмечали последнюю катку.
ANNOUNCE_CHANNEL_ID: int = _env_int("ANNOUNCE_CHANNEL_ID")
# Итоги недели: день (0 — понедельник … 5 — суббота) и час по TIMEZONE. RECAP_HOUR=-1 — не публиковать.
RECAP_WEEKDAY: int = min(6, max(0, _env_int("RECAP_WEEKDAY", 5)))
RECAP_HOUR: int = min(23, _env_int("RECAP_HOUR", 11))
# С какого часа (по TIMEZONE) Олег зовёт на кастомку, если сегодня сбора нет. -1 — не звать.
LOBBY_INVITE_HOUR: int = min(22, _env_int("LOBBY_INVITE_HOUR", 17))
# Викторина в чате: канал (пусто — канал объявлений), как часто (минуты, 0 — только по /quiz), в какие часы,
# сколько секунд на ответ, сколько коинов за верный ответ и сколько минут канал считается «живым» после сообщения.
QUIZ_CHANNEL_ID: int = _env_int("QUIZ_CHANNEL_ID")
QUIZ_MINUTES: int = max(0, _env_int("QUIZ_MINUTES", 45))
QUIZ_HOURS: str = os.getenv("QUIZ_HOURS", "12-24").strip() or "12-24"
QUIZ_ANSWER_SECONDS: int = max(20, _env_int("QUIZ_ANSWER_SECONDS", 60))
QUIZ_COINS: int = max(0, _env_int("QUIZ_COINS", 50))
QUIZ_ACTIVE_MINUTES: int = max(5, _env_int("QUIZ_ACTIVE_MINUTES", 120))
# Патчноуты после обновлений бота: 1 — писать в канал объявлений, 0 — нет.
PATCH_NOTES: bool = os.getenv("PATCH_NOTES", "1").strip().lower() not in ("0", "false", "no", "off")
# Fearless-драфт: чемпионы прошлых каток серии не выпадают и недоступны никому. 0 — выключить.
FEARLESS_DRAFT: bool = os.getenv("FEARLESS_DRAFT", "1").strip().lower() not in ("0", "false", "no", "off")
# Сколько минут после раздачи принимаются ставки на катку.
BET_MINUTES: int = max(1, _env_int("BET_MINUTES", 10))
# Дуэли (/duel): сколько часов ждать катки, где дуэлянты окажутся в разных командах, потом коины возвращаются.
DUEL_HOURS: int = max(1, _env_int("DUEL_HOURS", 24))
# Охота на голову: столько коинов каждому, кто обыграл лидера таблицы по Elo. 0 — выключить.
BOUNTY_COINS: int = max(0, _env_int("BOUNTY_COINS", 100))
# Сколько минут идёт голосование за MVP после отмеченной катки (до 14 — дольше Discord не даёт
# обновить сообщение). 0 — не голосовать.
MVP_VOTE_MINUTES: int = min(14, max(0, _env_int("MVP_VOTE_MINUTES", 10)))

# Часовой пояс, в котором организаторы пишут время сбора («сегодня 21:00»). Игроки видят его в своём поясе.
try:
    TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow")
except (ZoneInfoNotFoundError, ValueError):
    TIMEZONE = ZoneInfo("Europe/Moscow")

# За сколько минут до начала сбора бот зовёт записавшихся. 0 — не напоминать.
REMIND_MINUTES: int = _env_int("REMIND_MINUTES", 15)

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
