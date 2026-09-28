"""Бесплатные языковые модели для болтовни Олега: OpenRouter и Groq.

Провайдеры перебираются по порядку: если у одного кончился лимит или он молчит,
отвечает следующий. Ключи бессрочные, лимиты сбрасываются сами.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import aiohttp

import config
from vpn import VPN_CLIENT

log = logging.getLogger("scrimbot.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HF_URL = "https://router.huggingface.co/v1/chat/completions"
TOKENHARBOR_URL = "https://tokenharbor.ai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
PUTER_URL = "https://api.puter.com/puterai/openai/v1/chat/completions"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"

# Если модель зависла или недоступна, какое-то время сразу идём к следующей,
# чтобы чат не ждал по минуте на каждом сообщении.
TIMEOUT_PAUSE = 180.0
ERROR_PAUSE = 60.0
# HTTP 402 — кончились бесплатные кредиты (Hugging Face даёт $0.10 в месяц). Проверяем раз в несколько часов.
QUOTA_PAUSE = 6 * 3600.0


def _message_text(message: dict) -> str:
    """Текст ответа. Некоторые модели с размышлениями отдают список кусков: мысли отдельно, текст отдельно."""
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            chunk.get("text", "") for chunk in content if isinstance(chunk, dict) and chunk.get("type") == "text"
        )
    return (content or "").strip()

# У моделей Groq рассуждения прячутся по-разному: у gpt-oss — include_reasoning,
# у qwen — reasoning_format. Олегу они не нужны, только сам ответ.
GROQ_OPTIONS = {
    "openai/": {"reasoning_effort": "low", "include_reasoning": False},
    "qwen/": {"reasoning_effort": "none", "reasoning_format": "hidden"},
}

_RETRY_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s")
# Google кладёт паузу в подробности ошибки: "retryDelay": "48s".
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]([\d.]+)s")
# Сбои на стороне провайдера («модель перегружена») — пропускаем его ненадолго.
OVERLOAD_PAUSE = 20.0
# Сколько ждать подключения (вместе с прокси и TLS), прежде чем считать сервер недоступным.
CONNECT_TIMEOUT = 8.0
# Gemini часто отвечает 503 «high demand», но через пару секунд обычно отвечает. Модель с большим лимитом
# (Flash Lite, Groq) переспрашиваем с нарастающей паузой (LLM_OVERLOAD_RETRIES раз), «редкую» — нет.
OVERLOAD_RETRY_DELAYS = (2.0, 4.0, 8.0)
_OVERLOADED = "перегружен"
# Провайдер не работает в стране, откуда идёт запрос (Gemini из России), — не дёргаем его часами.
REGION_PAUSE = 6 * 3600.0
VPN_REGION_PAUSE = 15 * 60.0
_REGION_RE = re.compile(
    r"location is not supported|not available in your (country|region)|unsupported_country|region is not supported",
    re.IGNORECASE,
)


def is_out_of_credits(text: str) -> bool:
    """Провайдер отказал из-за баланса: «insufficient funds», «not enough credits» и т. п."""
    return bool(re.search(
        r"insufficient[ _](funds|balance|credit)|not enough (funds|credits|balance)|out of credits"
        r"|exceeded your (monthly |free )?(usage|allowance)|upgrade (your plan|to continue)",
        text or "", re.IGNORECASE,
    ))


def is_region_block(status: int, text: str) -> bool:
    """Отказ по стране: Google отвечает 400 FAILED_PRECONDITION «User location is not supported»."""
    return status in (400, 403, 451) and bool(_REGION_RE.search(text))


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    api_key: str
    model: str
    options: dict = field(default_factory=dict)
    max_tokens_field: str = "max_tokens"
    max_tokens: int = 900
    timeout: float = 30.0
    proxy: str | None = None
    # Модель с крошечным суточным лимитом (Gemini Flash на бесплатном тарифе — 20 запросов в день):
    # не переспрашиваем её при перегрузке и не тратим на необязательные реплики.
    scarce: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    tokens: int


class LLMUnavailable(Exception):
    """Ни один провайдер не ответил: лимиты или сбой сети."""

    def __init__(self, message: str, *, retry_after: float = 0.0, daily: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


def is_scarce_gemini(model: str) -> bool:
    """У бесплатного Gemini «Flash» — 20 запросов в день на модель, у «Flash Lite» — сотни (см. GEMINI_SCARCE)."""
    return any(part in model for part in config.GEMINI_SCARCE) and "lite" not in model


def build_providers() -> list[Provider]:
    """Цепочка провайдеров в порядке LLM_ORDER (по умолчанию Groq → остальные)."""
    providers: list[Provider] = []
    for model in config.GEMINI_MODELS:
        providers.append(Provider(
            name="gemini",
            url=GEMINI_URL,
            api_key=config.GEMINI_API_KEY,
            model=model,
            # Немного размышлений улучшает шутки, но всё сверх этого — только задержка.
            options={"reasoning_effort": "low"},
            max_tokens=1500,  # вместе с размышлениями
            timeout=25.0,
            scarce=is_scarce_gemini(model),
        ))
    for model in config.PUTER_MODELS:
        providers.append(Provider(
            name="puter",
            url=PUTER_URL,
            api_key=config.PUTER_AUTH_TOKEN,
            model=model,
            max_tokens=900,
            timeout=25.0,
        ))
    for model in config.ZAI_MODELS:
        providers.append(Provider(
            name="zai",
            url=ZAI_URL,
            api_key=config.ZAI_API_KEY,
            model=model,
            # GLM по умолчанию «думает» вслух: для реплик в чате это лишняя задержка.
            options={"thinking": {"type": "disabled"}},
            max_tokens=900,
            timeout=25.0,
        ))
    for model in config.TOKENHARBOR_MODELS:
        providers.append(Provider(
            name="tokenharbor",
            url=TOKENHARBOR_URL,
            api_key=config.TOKENHARBOR_API_KEY,
            model=model,
            max_tokens=1500,
            timeout=25.0,
        ))
    for model in config.OPENROUTER_MODELS:
        providers.append(Provider(
            name="openrouter",
            url=OPENROUTER_URL,
            api_key=config.OPENROUTER_API_KEY,
            model=model,
            options={"reasoning": {"effort": "low", "exclude": True}},
            max_tokens=1500,  # вместе с рассуждениями
            timeout=25.0,
        ))
    for model in config.HF_MODELS:
        providers.append(Provider(
            name="huggingface",
            url=HF_URL,
            api_key=config.HF_TOKEN,
            model=model,
            max_tokens=700,
            timeout=25.0,
        ))
    for model in config.GROQ_MODELS:
        options = next((opts for prefix, opts in GROQ_OPTIONS.items() if model.startswith(prefix)), {})
        providers.append(Provider(
            name="groq",
            url=GROQ_URL,
            api_key=config.GROQ_API_KEY,
            model=model,
            options=options,
            max_tokens_field="max_completion_tokens",
        ))
    if config.LLM_PROXY:
        providers = [
            replace(provider, proxy=config.LLM_PROXY)
            if provider.name in config.LLM_PROXY_FOR or "all" in config.LLM_PROXY_FOR else provider
            for provider in providers
        ]
    return order_providers([provider for provider in providers if provider.api_key], config.LLM_ORDER)


def proxy_for(provider: Provider) -> str | None:
    """Прокси запроса. Встроенный VPN не запущен (нет Xray вне Docker) или только что уронил соединение —
    идём напрямую, а не ждём таймаутов через сломанный сервер."""
    if provider.proxy and provider.proxy == config.VPN_PROXY_URL and not VPN_CLIENT.healthy:
        return None
    return provider.proxy


def order_providers(providers: list[Provider], order: list[str]) -> list[Provider]:
    """Сортирует по LLM_ORDER; порядок моделей внутри провайдера сохраняется, неназванные — в конце."""
    rank = {name: index for index, name in enumerate(order)}
    return sorted(providers, key=lambda provider: rank.get(provider.name, len(rank)))


def seconds_until_google_reset(now: float | None = None) -> float:
    """Секунды до полуночи по тихоокеанскому времени — тогда Google обнуляет суточные лимиты Gemini."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/Los_Angeles")
    except Exception:  # нет базы часовых поясов — берём зимнее смещение, ошибка максимум на час
        zone = timezone(timedelta(hours=-8))
    current = datetime.fromtimestamp(time.time() if now is None else now, zone)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max((midnight - current).total_seconds(), 60.0)


def _rate_limit_wait(message: str, headers, daily: bool) -> float:
    retry = headers.get("retry-after")
    if retry:
        try:
            return float(retry)
        except ValueError:
            pass
    delay = _RETRY_DELAY_RE.search(message)
    if delay:
        return float(delay.group(1))
    match = _RETRY_RE.search(message)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    reset = headers.get("x-ratelimit-reset")  # OpenRouter: момент сброса в миллисекундах
    if reset:
        try:
            return max(float(reset) / 1000 - time.time(), 1.0)
        except ValueError:
            pass
    return 3600.0 if daily else 60.0


class LLMClient:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers
        self._session: aiohttp.ClientSession | None = None
        self._blocked_until: dict[str, float] = {}
        self._blocked_daily: dict[str, bool] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 1.0,
        validate: Callable[[str], bool] | None = None,
        economy: bool = False,
    ) -> Reply:
        """Первый годный ответ по цепочке. `validate` бракует ответ — тогда спрашиваем следующую модель.

        economy=True — необязательная реплика (Олег сам встрял, разбор «запомни»): модели с крошечным суточным
        лимитом пропускаем, чтобы они остались для прямых обращений и прожарок.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        errors: list[str] = []
        for provider in self.providers:
            if economy and provider.scarce:
                continue
            if self._blocked_until.get(provider.label, 0) > time.time():
                continue
            if provider.name in config.LLM_VPN_ONLY and provider.proxy == config.VPN_PROXY_URL and VPN_CLIENT.broken:
                # Напрямую из России не пустят, а отказ по региону отключил бы модель надолго — ждём VPN.
                continue
            # Перегруженную «редкую» модель не переспрашиваем: неудачный запрос тоже съедает её суточный лимит,
            # а следующая модель Gemini в цепочке — со своим лимитом, это и есть повтор.
            retries = 0 if provider.scarce else config.LLM_OVERLOAD_RETRIES
            reply = await self._ask_with_retries(provider, system, user, temperature, validate, retries, errors)
            if reply is not None:
                return reply
        last_error = errors[-1] if errors else "нет доступных моделей"
        now = time.time()
        blocked = [p.label for p in self.providers if self._blocked_until.get(p.label, 0) > now]
        if blocked and len(blocked) == len(self.providers):
            raise LLMUnavailable(
                "все модели на лимите",
                retry_after=min(self._blocked_until[label] for label in blocked) - now,
                daily=all(self._blocked_daily.get(label) for label in blocked),
            )
        raise LLMUnavailable(last_error)

    async def _ask_with_retries(self, provider, system, user, temperature, validate, retries, errors) -> Reply | None:
        """Одна модель: при перегрузке переспрашиваем с нарастающей паузой, потом она отдыхает OVERLOAD_PAUSE."""
        for attempt in range(retries + 1):
            result = await self._ask(provider, system, user, temperature, validate)
            if isinstance(result, Reply):
                return result
            if result != _OVERLOADED:
                errors.append(result)
                return None
            if attempt < retries:
                delay = OVERLOAD_RETRY_DELAYS[min(attempt, len(OVERLOAD_RETRY_DELAYS) - 1)]
                log.info("%s перегружен, переспрашиваю через %.0f с (%d/%d)", provider.label, delay, attempt + 1, retries)
                await asyncio.sleep(delay)
        self._blocked_until[provider.label] = time.time() + OVERLOAD_PAUSE
        self._blocked_daily[provider.label] = False
        errors.append(f"{provider.label}: перегружен")
        log.warning("%s перегружен, пропускаем %.0f с", provider.label, OVERLOAD_PAUSE)
        return None

    async def _ask(self, provider, system, user, temperature, validate) -> Reply | str:
        """Один запрос. Ответ — Reply, _OVERLOADED или текст ошибки (паузы провайдера уже выставлены)."""
        body = {
            "model": provider.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            provider.max_tokens_field: provider.max_tokens,
            **provider.options,
        }
        headers = {"Authorization": f"Bearer {provider.api_key}", "X-Title": "Oleg Kastomkin"}
        proxy = proxy_for(provider)
        try:
            async with self._session.post(
                provider.url, json=body, headers=headers, proxy=proxy,
                # Подключение (с прокси и TLS) — отдельно и коротко: сломанный VPN-сервер не должен
                # держать каждую модель по 25 секунд.
                timeout=aiohttp.ClientTimeout(total=provider.timeout, connect=CONNECT_TIMEOUT),
            ) as response:
                payload = await response.json(content_type=None)
                if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
                    # Google отдаёт ошибки списком из одного объекта.
                    payload = payload[0]
                if not isinstance(payload, dict):
                    # Прокси и балансировщики иногда отдают строку или список вместо объекта.
                    payload = {"error": {"message": str(payload)[:300]}}
                if response.status == 429:
                    # Обычно ошибка лежит в error, у некоторых провайдеров — прямо в корне ответа.
                    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
                    message = (
                        f"{error.get('message', '')} {(error.get('metadata') or {}).get('raw', '')} "
                        f"{error.get('details', '')}"
                    )
                    daily = bool(re.search(
                        r"per[ _-]?day|month|week|7-day|allowance|quota", message, re.IGNORECASE
                    ))
                    wait = _rate_limit_wait(message, response.headers, daily)
                    if provider.name == "gemini" and "PerDay" in message:
                        # Кончился суточный лимит модели (…RequestsPerDay…): Google сбрасывает его в полночь
                        # по тихоокеанскому времени, а retryDelay в ответе — секунды, из-за которых
                        # закончившуюся модель дёргали бы каждые полминуты.
                        wait = max(wait, seconds_until_google_reset())
                    # Короткая пауза — это поминутный лимит, даже если в тексте есть слово «quota».
                    daily = daily and wait >= 3600
                    log.warning(
                        "Лимит %s (%s), пауза %.0f с",
                        provider.label, "суточный" if daily else "временный", wait,
                    )
                    self._blocked_until[provider.label] = time.time() + wait
                    self._blocked_daily[provider.label] = daily
                    return f"{provider.label}: лимит"
                if response.status >= 400 and is_out_of_credits(str(payload)):
                    # Кончился баланс (у Puter — бесплатная месячная квота): не дёргаем провайдера часами.
                    for other in self.providers:
                        if other.name == provider.name:
                            self._blocked_until[other.label] = time.time() + QUOTA_PAUSE
                            self._blocked_daily[other.label] = True
                    log.warning(
                        "%s: кончился баланс или бесплатная квота (HTTP %s), пропускаем его %.0f ч",
                        provider.name, response.status, QUOTA_PAUSE / 3600,
                    )
                    return f"{provider.name}: кончился баланс"
                if response.status == 402:
                    self._blocked_until[provider.label] = time.time() + QUOTA_PAUSE
                    self._blocked_daily[provider.label] = True
                    error_text = f"{provider.label}: кончились бесплатные кредиты"
                    log.warning(
                        "%s: кончились бесплатные кредиты (HTTP 402), пропускаем его %.0f ч",
                        provider.label, QUOTA_PAUSE / 3600,
                    )
                    return error_text
                if is_region_block(response.status, str(payload)):
                    if proxy:
                        # Сервер VPN выходит в стране, куда нейросеть не пускает, — VPN выберет другой.
                        VPN_CLIENT.report_failure(region=True)
                    # Все модели этого провайдера недоступны из этой страны — отключаем их разом.
                    # Через VPN пауза короткая: Xray может переключиться на сервер в другой стране.
                    # Шли напрямую только потому, что VPN сломан, — тоже короткая пауза: VPN скоро починится.
                    pause = VPN_REGION_PAUSE if provider.proxy else REGION_PAUSE
                    until = time.time() + pause
                    for other in self.providers:
                        if other.name == provider.name:
                            self._blocked_until[other.label] = until
                            self._blocked_daily[other.label] = False
                    error_text = f"{provider.name}: недоступен из этой страны"
                    log.warning(
                        "%s не работает из вашей страны (%s). Отключаю его на %.2g ч — отвечают другие модели. "
                        "Чтобы он заработал, укажите в .env VPN_SUBSCRIPTION или LLM_PROXY.",
                        provider.name, str(payload)[:120], pause / 3600,
                    )
                    return error_text
                if response.status in (500, 502, 503, 504):
                    return _OVERLOADED  # паузы и повторы решает _ask_with_retries
                if response.status >= 400:
                    error_text = f"{provider.label}: HTTP {response.status}"
                    log.warning("%s ответил ошибкой: %s %s", provider.label, response.status, str(payload)[:300])
                    return error_text
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            if proxy == config.VPN_PROXY_URL and isinstance(error, (aiohttp.ClientConnectionError, TimeoutError)):
                # Прокси не довёз запрос — следующие модели пойдут напрямую, а VPN сразу перепроверит серверы.
                VPN_CLIENT.report_failure(connection=isinstance(error, aiohttp.ClientConnectionError))
            pause = TIMEOUT_PAUSE if isinstance(error, TimeoutError) else ERROR_PAUSE
            self._blocked_until[provider.label] = time.time() + pause
            self._blocked_daily[provider.label] = False
            error_text = f"{provider.label}: {error!r}"
            log.warning("Не удалось обратиться к %s: %r — пропускаем его %.0f с", provider.label, error, pause)
            return error_text

        choices = payload.get("choices") or []
        text = _message_text(choices[0].get("message") or {}) if choices else ""
        if not text:
            # Бывает у моделей с рассуждениями: всё ушло в мысли. Пробуем следующего.
            error_text = f"{provider.label}: пустой ответ"
            log.warning("%s вернул пустой ответ", provider.label)
            return error_text
        if validate is not None and not validate(text):
            error_text = f"{provider.label}: ответ забракован"
            log.warning("%s: ответ забракован проверкой: %.120s", provider.label, text)
            return error_text
        tokens = (payload.get("usage") or {}).get("total_tokens", 0)
        return Reply(text, provider.label, tokens)
