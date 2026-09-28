"""Бесплатные языковые модели для болтовни Олега: OpenRouter и Groq.

Провайдеры перебираются по порядку: если у одного кончился лимит или он молчит,
отвечает следующий. Ключи бессрочные, лимиты сбрасываются сами.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable

import aiohttp

import config

log = logging.getLogger("scrimbot.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HF_URL = "https://router.huggingface.co/v1/chat/completions"
TOKENHARBOR_URL = "https://tokenharbor.ai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

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
OVERLOAD_PAUSE = 60.0
# Провайдер не работает в стране, откуда идёт запрос (Gemini из России), — не дёргаем его часами.
REGION_PAUSE = 6 * 3600.0
VPN_REGION_PAUSE = 15 * 60.0
_REGION_RE = re.compile(
    r"location is not supported|not available in your (country|region)|unsupported_country|region is not supported",
    re.IGNORECASE,
)


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
            replace(provider, proxy=config.LLM_PROXY) if provider.name in config.LLM_PROXY_FOR else provider
            for provider in providers
        ]
    return order_providers([provider for provider in providers if provider.api_key], config.LLM_ORDER)


def order_providers(providers: list[Provider], order: list[str]) -> list[Provider]:
    """Сортирует по LLM_ORDER; порядок моделей внутри провайдера сохраняется, неназванные — в конце."""
    rank = {name: index for index, name in enumerate(order)}
    return sorted(providers, key=lambda provider: rank.get(provider.name, len(rank)))


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
    ) -> Reply:
        """Первый годный ответ по цепочке. `validate` бракует ответ — тогда спрашиваем следующую модель."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        last_error = "нет доступных моделей"
        for provider in self.providers:
            if self._blocked_until.get(provider.label, 0) > time.time():
                continue

            body = {
                "model": provider.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": temperature,
                provider.max_tokens_field: provider.max_tokens,
                **provider.options,
            }
            headers = {"Authorization": f"Bearer {provider.api_key}", "X-Title": "Oleg Kastomkin"}
            try:
                async with self._session.post(
                    provider.url, json=body, headers=headers, proxy=provider.proxy,
                    timeout=aiohttp.ClientTimeout(total=provider.timeout),
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
                        # Короткая пауза — это поминутный лимит, даже если в тексте есть слово «quota».
                        daily = daily and wait >= 3600
                        log.warning(
                            "Лимит %s (%s), пауза %.0f с",
                            provider.label, "суточный" if daily else "временный", wait,
                        )
                        self._blocked_until[provider.label] = time.time() + wait
                        self._blocked_daily[provider.label] = daily
                        continue
                    if response.status == 402:
                        self._blocked_until[provider.label] = time.time() + QUOTA_PAUSE
                        self._blocked_daily[provider.label] = True
                        last_error = f"{provider.label}: кончились бесплатные кредиты"
                        log.warning(
                            "%s: кончились бесплатные кредиты (HTTP 402), пропускаем его %.0f ч",
                            provider.label, QUOTA_PAUSE / 3600,
                        )
                        continue
                    if is_region_block(response.status, str(payload)):
                        # Все модели этого провайдера недоступны из этой страны — отключаем их разом.
                        # Через VPN пауза короткая: Xray может переключиться на сервер в другой стране.
                        pause = VPN_REGION_PAUSE if provider.proxy else REGION_PAUSE
                        until = time.time() + pause
                        for other in self.providers:
                            if other.name == provider.name:
                                self._blocked_until[other.label] = until
                                self._blocked_daily[other.label] = False
                        last_error = f"{provider.name}: недоступен из этой страны"
                        log.warning(
                            "%s не работает из вашей страны (%s). Отключаю его на %.2g ч — отвечают другие модели. "
                            "Чтобы он заработал, укажите в .env VPN_SUBSCRIPTION или LLM_PROXY.",
                            provider.name, str(payload)[:120], pause / 3600,
                        )
                        continue
                    if response.status in (500, 502, 503, 504):
                        # «Модель перегружена» — временно: пропускаем её минуту и спрашиваем следующую.
                        self._blocked_until[provider.label] = time.time() + OVERLOAD_PAUSE
                        self._blocked_daily[provider.label] = False
                        last_error = f"{provider.label}: HTTP {response.status}"
                        log.warning("%s перегружен (HTTP %s), пропускаем %.0f с", provider.label, response.status, OVERLOAD_PAUSE)
                        continue
                    if response.status >= 400:
                        last_error = f"{provider.label}: HTTP {response.status}"
                        log.warning("%s ответил ошибкой: %s %s", provider.label, response.status, str(payload)[:300])
                        continue
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                pause = TIMEOUT_PAUSE if isinstance(error, TimeoutError) else ERROR_PAUSE
                self._blocked_until[provider.label] = time.time() + pause
                self._blocked_daily[provider.label] = False
                last_error = f"{provider.label}: {error!r}"
                log.warning("Не удалось обратиться к %s: %r — пропускаем его %.0f с", provider.label, error, pause)
                continue

            choices = payload.get("choices") or []
            text = _message_text(choices[0].get("message") or {}) if choices else ""
            if not text:
                # Бывает у моделей с рассуждениями: всё ушло в мысли. Пробуем следующего.
                last_error = f"{provider.label}: пустой ответ"
                log.warning("%s вернул пустой ответ", provider.label)
                continue
            if validate is not None and not validate(text):
                last_error = f"{provider.label}: ответ забракован"
                log.warning("%s: ответ забракован проверкой: %.120s", provider.label, text)
                continue
            tokens = (payload.get("usage") or {}).get("total_tokens", 0)
            return Reply(text, provider.label, tokens)

        now = time.time()
        blocked = [p.label for p in self.providers if self._blocked_until.get(p.label, 0) > now]
        if blocked and len(blocked) == len(self.providers):
            raise LLMUnavailable(
                "все модели на лимите",
                retry_after=min(self._blocked_until[label] for label in blocked) - now,
                daily=all(self._blocked_daily.get(label) for label in blocked),
            )
        raise LLMUnavailable(last_error)
