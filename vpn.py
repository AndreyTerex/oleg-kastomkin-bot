"""VPN бота: сам находит рабочий зарубежный сервер из подписки для нейросетей (Gemini закрыт из России) и Discord.

Как работает:
1. Скачивает подписку (VPN_SUBSCRIPTION) и/или ключи (VPN_URL), разбирает их (vpn_links.py) и сразу
   отсеивает мусор: служебные записи («Осталось 12 дней», «Сайт»), адреса-заглушки, дубли.
2. Проверяет ВСЕ серверы настоящим запросом: временный Xray с отдельным портом на каждый сервер, через него —
   https://www.gstatic.com/generate_204 (задержка) и https://www.cloudflare.com/cdn-cgi/trace (страна ВЫХОДА).
   Годится сервер, который ответил и выходит не в исключённой стране (VPN_EXCLUDE_COUNTRIES).
   Провайдер режет по DPI — повторяет проверку с фрагментацией TLS ClientHello и запоминает, кому она нужна.
3. Держится одного выбранного сервера: основной Xray слушает 127.0.0.1:VPN_PORT, через него идут нейросети
   из LLM_PROXY_FOR (по умолчанию все) и Discord (VPN_FOR_DISCORD). Рабочих серверов нет — тот же порт
   пускает трафик напрямую, чтобы бот не отваливался.
   Меняет сервер, только если текущий перестал работать или новый быстрее
   на VPN_SWITCH_THRESHOLD (по умолчанию 30%) — без прыжков туда-сюда.
4. Бережёт ресурсы машины: раз в VPN_RECHECK_MINUTES проверяет одним запросом только текущий сервер,
   а все серверы — раз в VPN_FULL_CHECK_HOURS или сразу, если текущий перестал отвечать (в том числе когда
   запрос к нейросети через прокси упал). Проверочный Xray работает на одном ядре с низким приоритетом,
   упавшие серверы 2 часа не перепроверяются.
5. Помнит выбор в data/vpn.json — только отпечаток ключа (sha256), название, страну, задержку, время.
   При старте сразу поднимает сохранённый сервер и не ждёт полной проверки.

В логи и в data/vpn.json никогда не попадают ссылка подписки, uuid, пароли и адреса серверов.
Скачивание подписки и картинки чемпионов идут напрямую.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

import config
from storage import JsonStore
from vpn_links import Server, parse_subscription, usable

log = logging.getLogger("scrimbot.vpn")

PROBE_URL = "https://www.gstatic.com/generate_204"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
# Так подписку отдают списком ключей: многие сервисы для «незнакомых» клиентов отвечают YAML для Clash.
SUBSCRIPTION_USER_AGENT = "v2rayN/7.0"
SUBSCRIPTION_REFRESH = 6 * 3600
# Упавший сервер не перепроверяем столько секунд (если есть другие рабочие).
BLACKLIST_TTL = 2 * 3600
# Сколько серверов проверяет один временный Xray (по порту на сервер).
PROBE_BATCH = 32
# Xray бота работает на одном ядре: трафика у него немного, а машину он не нагружает.
XRAY_ENV = {**os.environ, "GOMAXPROCS": "1"}
# Сообщения «прокси упал» от нейросети чаще этого не запускают новую проверку.
FAILURE_DEBOUNCE = 60
# Без рабочего сервера (режим «напрямую») все серверы перепроверяются так часто.
DIRECT_RETRY_HOURS = 0.5
FRAGMENT_TAG = "fragment"
PROXY_TAG = "proxy"

FRAGMENT_OUTBOUND = {
    "tag": FRAGMENT_TAG,
    "protocol": "freedom",
    "settings": {"fragment": {"packets": "tlshello", "length": "100-200", "interval": "10-20"}},
    "streamSettings": {"sockopt": {"tcpNoDelay": True}},
}

# Адреса, uuid и host:port в строках Xray — вырезаем перед выводом в лог.
_SECRET_PATTERNS = (
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"),
    re.compile(r"\[?\b(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}\]?(:\d+)?", re.IGNORECASE),
    re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(:\d+)?\b", re.IGNORECASE),
)


_TIMESTAMP = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d(\.\d+)?\s*")


def sanitize(line: str) -> str:
    line = _TIMESTAMP.sub("", line)  # время у логгера бота своё
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub("‹скрыто›", line)
    return line


# --- результаты проверки и выбор ----------------------------------------------------


@dataclass
class ProbeResult:
    fingerprint: str
    name: str
    ok: bool
    latency_ms: float | None = None
    country: str | None = None
    fragment: bool = False

    def usable(self, excluded: set[str]) -> bool:
        return self.ok and bool(self.country) and self.country not in excluded


def parse_trace(text: str) -> str | None:
    """Страна выхода из ответа Cloudflare trace (строка loc=DE)."""
    match = re.search(r"^loc=([A-Za-z]{2})\s*$", text or "", re.MULTILINE)
    return match.group(1).upper() if match else None


def choose(
    current: str | None, results: dict[str, ProbeResult], excluded: set[str], threshold: float,
    min_gain_ms: float = 0.0,
) -> ProbeResult | None:
    """Какой сервер держать. Текущий оставляем, пока он работает и новый не быстрее на threshold (0.3 = 30%)
    и хотя бы на min_gain_ms: смена сервера перезапускает Xray и на секунды рвёт соединение с Discord."""
    candidates = [result for result in results.values() if result.usable(excluded)]
    if not candidates:
        return None
    best = min(candidates, key=lambda result: result.latency_ms or float("inf"))
    kept = results.get(current) if current else None
    if kept is not None and kept.usable(excluded):
        new, old = best.latency_ms or 0, kept.latency_ms or 0
        if best.fingerprint != kept.fingerprint and new <= old * (1 - threshold) and old - new >= min_gain_ms:
            return best
        return kept
    return best


# --- состояние в data/vpn.json ------------------------------------------------------


class VpnState:
    """Только отпечатки ключей, названия, страны, задержки и время — без секретов."""

    def __init__(self, path: Path) -> None:
        self.store = JsonStore(path)

    def load(self) -> None:
        data = self.store.load()
        data.setdefault("selected", None)
        data.setdefault("blacklist", {})
        data.setdefault("fragment", {})
        data.setdefault("last_check", None)

    @property
    def data(self) -> dict:
        return self.store.data

    @property
    def selected(self) -> dict | None:
        return self.store.data.get("selected")

    def select(self, result: ProbeResult) -> None:
        self.store.data["selected"] = {
            "fingerprint": result.fingerprint,
            "name": result.name,
            "country": result.country,
            "latency_ms": round(result.latency_ms) if result.latency_ms is not None else None,
            "fragment": result.fragment,
            "checked_at": time.time(),
        }

    def blacklisted(self, now: float) -> set[str]:
        return {fp for fp, at in self.store.data["blacklist"].items() if now - at < BLACKLIST_TTL}

    def record(self, results: dict[str, ProbeResult], now: float) -> None:
        blacklist = self.store.data["blacklist"]
        for fp, result in results.items():
            if result.ok:
                blacklist.pop(fp, None)
                self.store.data["fragment"][fp] = result.fragment
            else:
                blacklist[fp] = now
        # Старые записи не копим.
        for fp in [fp for fp, at in blacklist.items() if now - at > 24 * 3600]:
            del blacklist[fp]

    async def save(self) -> None:
        await self.store.save()


# --- конфиги Xray -------------------------------------------------------------------


def _outbound(server: Server, tag: str, fragment: bool) -> dict:
    outbound = copy.deepcopy(server.outbound)
    outbound["tag"] = tag
    if fragment:
        stream = outbound.setdefault("streamSettings", {})
        stream.setdefault("sockopt", {})["dialerProxy"] = FRAGMENT_TAG
    return outbound


def main_config(server: Server, fragment: bool, port: int) -> dict:
    """Основной Xray: 127.0.0.1:port → один выбранный сервер (при необходимости через фрагментацию)."""
    outbounds = [_outbound(server, PROXY_TAG, fragment), {"tag": "direct", "protocol": "freedom"}]
    if fragment:
        outbounds.append(copy.deepcopy(FRAGMENT_OUTBOUND))
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "http-in", "listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}],
        "outbounds": outbounds,
        "routing": {"rules": [{"type": "field", "inboundTag": ["http-in"], "outboundTag": PROXY_TAG}]},
    }


def direct_config(port: int) -> dict:
    """Запасной режим: рабочих серверов нет — тот же порт, но трафик идёт напрямую (Discord не отваливается)."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "http-in", "listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}],
        "outbounds": [{"tag": PROXY_TAG, "protocol": "freedom"}],
    }


def probe_config(entries: list[tuple[Server, bool]], base_port: int) -> dict:
    """Временный Xray для проверки: порт base_port+i ведёт строго в сервер i."""
    inbounds, outbounds, rules = [], [], []
    for index, (server, fragment) in enumerate(entries):
        inbounds.append({
            "tag": f"in-{index}", "listen": "127.0.0.1", "port": base_port + index, "protocol": "http", "settings": {},
        })
        outbounds.append(_outbound(server, f"out-{index}", fragment))
        rules.append({"type": "field", "inboundTag": [f"in-{index}"], "outboundTag": f"out-{index}"})
    outbounds.append({"tag": "direct", "protocol": "freedom"})
    if any(fragment for _server, fragment in entries):
        outbounds.append(copy.deepcopy(FRAGMENT_OUTBOUND))
    return {"log": {"loglevel": "none"}, "inbounds": inbounds, "outbounds": outbounds, "routing": {"rules": rules}}


# --- работа с процессом Xray --------------------------------------------------------


def _write_config(data: dict, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    with handle:
        json.dump(data, handle, ensure_ascii=False)
    return Path(handle.name)


async def xray_accepts(data: dict) -> bool:
    path = _write_config(data, ".test.json")
    try:
        process = await asyncio.create_subprocess_exec(
            config.XRAY_BIN, "run", "-test", "-c", str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=XRAY_ENV, preexec_fn=_lower_priority,
        )
        return await asyncio.wait_for(process.wait(), 30) == 0
    except (OSError, TimeoutError):
        return False
    finally:
        path.unlink(missing_ok=True)


def _lower_priority() -> None:
    """Проверки идут с низким приоритетом, чтобы не мешать боту и другим программам на машине."""
    try:
        os.nice(10)
    except OSError:
        pass


async def _ports_open(ports: list[int], timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for port in (ports[0], ports[-1]):
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


async def _stop(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()


# --- сам VPN ------------------------------------------------------------------------


class VPN:
    def __init__(self) -> None:
        self.state = VpnState(Path(config.DATA_DIR) / "vpn.json")
        self.servers: dict[str, Server] = {}
        self.fetched_at = 0.0
        self.process: asyncio.subprocess.Process | None = None
        self.config_path: Path | None = None
        self.current: ProbeResult | None = None
        self.last_summary: dict | None = None
        self.invalid: set[str] = set()
        self.task: asyncio.Task | None = None
        self.wakeup = asyncio.Event()
        self.lock = asyncio.Lock()
        self.last_failure_report = 0.0
        self.need_full = False
        self.started = False

    # --- настройки

    @property
    def enabled(self) -> bool:
        return bool(config.VPN_SUBSCRIPTION or config.VPN_URL)

    @property
    def excluded(self) -> set[str]:
        return set(config.VPN_EXCLUDE_COUNTRIES)

    # --- запуск и остановка

    async def start(self) -> None:
        if not self.enabled or self.started:
            return
        if shutil.which(config.XRAY_BIN) is None:
            log.warning(
                "VPN указан в .env, но Xray не найден — он есть только в Docker-образе бота. "
                "Нейросети из LLM_PROXY_FOR будут недоступны из России."
            )
            return
        self.state.load()
        self.started = True
        await self.refresh_servers(force=True)
        saved = self.state.selected
        if saved and saved.get("fingerprint") in self.servers:
            # Сразу поднимаем прошлый выбор — полная проверка пойдёт в фоне.
            result = ProbeResult(
                saved["fingerprint"], saved.get("name") or "", True, saved.get("latency_ms"),
                saved.get("country"), bool(saved.get("fragment")),
            )
            if await self.activate(result):
                log.info(
                    "VPN: поднят сохранённый сервер «%s» (%s, %s мс), проверяю остальные в фоне",
                    result.name, result.country or "??", result.latency_ms or "?",
                )
        if self.current is None:
            # Порт нужен сразу (через него ходят Discord и нейросети), сервер подберётся в фоне.
            await self.activate_direct(quiet=True)
        self.task = asyncio.create_task(self._loop(first_delay=90 if self.current else 0))

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        process, self.process = self.process, None
        await _stop(process)

    # --- подписка

    async def refresh_servers(self, force: bool = False) -> None:
        if not force and time.time() - self.fetched_at < SUBSCRIPTION_REFRESH and self.servers:
            return
        texts = []
        if config.VPN_URL:
            texts.append(config.VPN_URL.replace(",", "\n"))
        if config.VPN_SUBSCRIPTION:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                    async with session.get(
                        config.VPN_SUBSCRIPTION, headers={"User-Agent": SUBSCRIPTION_USER_AGENT}
                    ) as response:
                        if response.status >= 400:
                            log.warning("VPN: подписка не скачалась — HTTP %s", response.status)
                        else:
                            texts.append(await response.text())
            except (aiohttp.ClientError, TimeoutError, OSError) as error:
                # Ссылку не выводим: в ней обычно секретный токен.
                log.warning("VPN: подписка не скачалась (%s)", type(error).__name__)
        servers, skipped = [], {}
        for text in texts:
            found, reasons = parse_subscription(text)
            servers += found
            for reason, count in reasons.items():
                skipped[reason] = skipped.get(reason, 0) + count
        if not servers and self.servers:
            log.warning("VPN: подписка пустая или не скачалась — остаюсь на прежнем списке серверов")
            return
        kept, dropped = usable(servers, config.VPN_FILTER)
        for reason, count in dropped.items():
            skipped[reason] = skipped.get(reason, 0) + count
        self.servers = {server.fingerprint: server for server in kept}
        self.fetched_at = time.time()
        details = ", ".join(f"{reason}: {count}" for reason, count in skipped.items())
        log.info("VPN: в подписке серверов %d, к проверке %d%s", len(servers), len(kept),
                 f" (пропущено — {details})" if details else "")

    # --- проверка серверов

    async def _probe_one(
        self, session: aiohttp.ClientSession, port: int, server: Server, fragment: bool,
        semaphore: asyncio.Semaphore | None = None,
    ) -> ProbeResult:
        proxy = f"http://127.0.0.1:{port}"
        result = ProbeResult(server.fingerprint, server.name, False, fragment=fragment)
        async with semaphore or contextlib.nullcontext():
            try:
                started = time.monotonic()
                async with session.get(PROBE_URL, proxy=proxy) as response:
                    await response.read()
                    if response.status not in (200, 204):
                        return result
                result.latency_ms = (time.monotonic() - started) * 1000
                async with session.get(TRACE_URL, proxy=proxy) as response:
                    result.country = parse_trace(await response.text())
                result.ok = True
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                pass
        return result

    async def _accepted(self, entries: list[tuple[Server, bool]]) -> list[tuple[Server, bool]]:
        """Отбрасывает ключи, которые Xray не принимает (один кривой ключ ломает весь конфиг)."""
        if await xray_accepts(probe_config(entries, config.VPN_PORT + 1)):
            return entries
        if len(entries) == 1:
            server = entries[0][0]
            self.invalid.add(server.fingerprint)
            log.warning("VPN: Xray не принимает ключ «%s» (%s) — пропускаю", server.name, server.protocol)
            return []
        middle = len(entries) // 2
        return await self._accepted(entries[:middle]) + await self._accepted(entries[middle:])

    async def _probe_batch(self, entries: list[tuple[Server, bool]]) -> dict[str, ProbeResult]:
        entries = await self._accepted(entries)
        if not entries:
            return {}
        base_port = config.VPN_PORT + 1
        path = _write_config(probe_config(entries, base_port), ".probe.json")
        process = await asyncio.create_subprocess_exec(
            config.XRAY_BIN, "run", "-c", str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=XRAY_ENV, preexec_fn=_lower_priority,
        )
        try:
            ports = [base_port + index for index in range(len(entries))]
            if not await _ports_open(ports):
                log.warning("VPN: проверочный Xray не запустился")
                return {}
            semaphore = asyncio.Semaphore(max(1, config.VPN_PROBE_CONCURRENCY))
            timeout = aiohttp.ClientTimeout(total=config.VPN_PROBE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(force_close=True)) as session:
                results = await asyncio.gather(*(
                    self._probe_one(session, port, server, fragment, semaphore)
                    for port, (server, fragment) in zip(ports, entries)
                ))
            return {result.fingerprint: result for result in results}
        finally:
            await _stop(process)
            path.unlink(missing_ok=True)

    async def _probe_pass(self, entries: list[tuple[Server, bool]]) -> dict[str, ProbeResult]:
        results: dict[str, ProbeResult] = {}
        for start in range(0, len(entries), PROBE_BATCH):
            results.update(await self._probe_batch(entries[start:start + PROBE_BATCH]))
        return results

    async def probe(self, servers: list[Server]) -> dict[str, ProbeResult]:
        """Проверка серверов; в режиме auto упавшие перепроверяются с фрагментацией (или без неё)."""
        mode = config.VPN_FRAGMENT
        known = self.state.data["fragment"]
        first = [(server, mode == "on" or (mode == "auto" and known.get(server.fingerprint) is True)) for server in servers]
        results = await self._probe_pass(first)
        if mode == "auto":
            retry = [
                (server, not fragment) for server, fragment in first
                if server.fingerprint in results and not results[server.fingerprint].ok
            ]
            if retry:
                for fp, result in (await self._probe_pass(retry)).items():
                    if result.ok:
                        results[fp] = result
        return results

    # --- переключение

    async def activate(self, result: ProbeResult) -> bool:
        """Поднимает основной Xray на выбранном сервере (ключ сначала проверяется, кривой не роняет рабочий)."""
        server = self.servers.get(result.fingerprint)
        if server is None:
            return False
        if not await self._run_main(main_config(server, result.fragment, config.VPN_PORT)):
            log.warning("VPN: Xray не принимает ключ «%s» — пропускаю", server.name)
            self.invalid.add(server.fingerprint)
            return False
        self.current = result
        self.state.select(result)
        await self.state.save()
        return True

    async def activate_direct(self, quiet: bool = False) -> None:
        """Рабочих серверов нет: порт остаётся, но трафик идёт напрямую, пока не найдётся сервер."""
        if self.current is None and self.ready:
            return
        if not quiet:
            log.warning("VPN: рабочих серверов нет — пока хожу напрямую, ищу сервер дальше")
        await self._run_main(direct_config(config.VPN_PORT))
        self.current = None
        self.state.data["selected"] = None
        await self.state.save()

    @property
    def ready(self) -> bool:
        """Локальный прокси запущен (через сервер или напрямую)."""
        return self.process is not None and self.process.returncode is None

    async def _run_main(self, data: dict) -> bool:
        if not await xray_accepts(data):
            return False
        # Сначала отвязываем старый процесс, иначе его читатель логов примет остановку за падение.
        previous, self.process = self.process, None
        await _stop(previous)
        if self.config_path is not None:
            self.config_path.unlink(missing_ok=True)
        self.config_path = _write_config(data, ".main.json")
        self.process = await asyncio.create_subprocess_exec(
            config.XRAY_BIN, "run", "-c", str(self.config_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=XRAY_ENV,
        )
        asyncio.create_task(self._pipe_logs(self.process))
        if not await _ports_open([config.VPN_PORT]):
            log.warning("VPN: основной Xray не открыл порт — попробую другой сервер при следующей проверке")
        return True

    async def _pipe_logs(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line or "deprecated" in line.lower() or "started" in line.lower():
                continue
            if "[Warning]" in line or "[Error]" in line or "failed" in line.lower():
                log.warning("xray: %s", sanitize(line))
            else:
                log.debug("xray: %s", sanitize(line))
        if process is self.process:
            log.warning("VPN: Xray остановился (код %s) — поднимаю заново", await process.wait())
            self.process = None
            self.wakeup.set()

    # --- перепроверка

    def report_failure(self, *, region: bool = False) -> None:
        """Нейросеть не достучалась через прокси: сразу перепроверить (не чаще раза в минуту)."""
        if not self.started:
            return
        now = time.time()
        if region and self.current is not None:
            # Выход сервера в стране, куда нейросеть не пускает, — этот сервер больше не выбираем.
            self.state.data["blacklist"][self.current.fingerprint] = now
            self.need_full = True
        if self.current is None and not region:
            # Уже напрямую: новый поиск сервера пойдёт по расписанию, не гоняем проверки на каждый запрос.
            return
        if now - self.last_failure_report < FAILURE_DEBOUNCE:
            return
        self.last_failure_report = now
        self.wakeup.set()

    async def _loop(self, first_delay: float) -> None:
        delay = first_delay
        while True:
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
            except TimeoutError:
                pass
            self.wakeup.clear()
            try:
                await self.recheck(full=self.need_full or self._full_check_due())
            except Exception:
                log.exception("VPN: ошибка при перепроверке серверов")
            delay = max(60, config.VPN_RECHECK_MINUTES * 60)

    def _full_check_due(self) -> bool:
        last = (self.state.data.get("last_check") or {}).get("at") or 0
        hours = config.VPN_FULL_CHECK_HOURS if self.current else min(config.VPN_FULL_CHECK_HOURS, DIRECT_RETRY_HOURS)
        return time.time() - last >= hours * 3600

    async def _current_alive(self) -> bool:
        """Лёгкая проверка: один запрос через уже запущенный Xray, без перебора всех серверов."""
        current = self.current
        server = self.servers.get(current.fingerprint) if current else None
        if server is None or self.process is None or self.process.returncode is not None:
            return False
        timeout = aiohttp.ClientTimeout(total=config.VPN_PROBE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(force_close=True)) as session:
            result = await self._probe_one(session, config.VPN_PORT, server, current.fragment)
        if not result.usable(self.excluded):
            log.info("VPN: текущий сервер «%s» перестал подходить — проверяю остальные", current.name)
            return False
        self.current = result
        self.state.select(result)
        await self.state.save()
        log.debug("VPN: «%s» в порядке (%s, %.0f мс)", result.name, result.country, result.latency_ms or 0)
        return True

    async def recheck(self, full: bool = True) -> dict | None:
        """full=False — сначала лёгкая проверка текущего сервера; полная — только если он не отвечает."""
        async with self.lock:
            if self.started and not self.ready:
                # Xray упал: сразу поднимаем его заново, чтобы Discord и нейросети не ждали полной проверки.
                if not (self.current and await self.activate(self.current)):
                    await self.activate_direct()
            if not full and self.current is None and self.ready:
                return self.status()  # напрямую: все серверы перебираем только по расписанию (_full_check_due)
            if not full and await self._current_alive():
                return self.status()
            self.need_full = False
            await self.refresh_servers()
            now = time.time()
            blocked = self.state.blacklisted(now)
            current = self.current.fingerprint if self.current else None
            candidates = [
                server for fp, server in self.servers.items()
                if fp not in self.invalid and fp not in blocked
            ]
            results = await self.probe(candidates)
            if not any(result.usable(self.excluded) for result in results.values()):
                # Все проверенные мимо — даём шанс недавно упавшим.
                retry = [self.servers[fp] for fp in blocked if fp in self.servers and fp not in results]
                if retry:
                    results.update(await self.probe(retry))
            self.state.record(results, now)
            choice = choose(
                current, results, self.excluded, config.VPN_SWITCH_THRESHOLD, config.VPN_SWITCH_MIN_GAIN_MS
            )
            working = sum(1 for result in results.values() if result.usable(self.excluded))
            wrong_country = sum(
                1 for result in results.values() if result.ok and result.country in self.excluded
            )
            if choice is None:
                if current in results and results[current].ok and self.ready:
                    self.current = results[current]  # выход неудачный, но связь есть — лучше, чем ничего
                else:
                    await self.activate_direct(quiet=True)
                log.warning(
                    "VPN: проверено %d, рабочих 0%s — %s",
                    len(results), f" (выход в исключённых странах: {wrong_country})" if wrong_country else "",
                    f"остаюсь на «{self.current.name}»" if self.current else "пока хожу напрямую",
                )
            elif choice.fingerprint != current or self.process is None:
                previous = self.current.name if self.current else None
                if await self.activate(choice):
                    log.info(
                        "VPN: проверено %d, рабочих %d, выбран «%s» (%s, %.0f мс)%s%s",
                        len(results), working, choice.name, choice.country, choice.latency_ms or 0,
                        ", с фрагментацией" if choice.fragment else "",
                        f", вместо «{previous}»" if previous and previous != choice.name else "",
                    )
            else:
                self.current = choice
                self.state.select(choice)
                log.info(
                    "VPN: проверено %d, рабочих %d, остаюсь на «%s» (%s, %.0f мс)",
                    len(results), working, choice.name, choice.country, choice.latency_ms or 0,
                )
            self.last_summary = {"at": now, "checked": len(results), "working": working}
            self.state.data["last_check"] = self.last_summary
            await self.state.save()
            return self.status()

    # --- статус для /oleg-vpn

    def status(self) -> dict:
        selected = self.state.selected if self.started else None
        return {
            "enabled": self.enabled,
            "running": self.started and self.ready,
            "direct": self.started and self.ready and self.current is None,
            "server": (self.current.name if self.current else (selected or {}).get("name")),
            "country": (self.current.country if self.current else (selected or {}).get("country")),
            "latency_ms": (self.current.latency_ms if self.current else (selected or {}).get("latency_ms")),
            "fragment": bool(self.current.fragment) if self.current else False,
            "servers": len(self.servers),
            "last_check": self.last_summary or (self.state.data.get("last_check") if self.started else None),
        }


VPN_CLIENT = VPN()
