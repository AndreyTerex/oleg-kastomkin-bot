"""VPN для нейросетей, которые не работают из России (Gemini): VLESS / Trojan / VMess через Xray.

В .env указывается подписка (VPN_SUBSCRIPTION) или отдельные ключи (VPN_URL). Бот скачивает список серверов,
собирает конфиг Xray и запускает его внутри контейнера как локальный HTTP-прокси. Xray сам пингует все серверы
и отправляет запросы через самый быстрый живой; упал сервер — переключается на следующий. Подписка
перечитывается раз в несколько часов.

Через этот прокси ходят только провайдеры из LLM_PROXY_FOR (по умолчанию Gemini); Discord и остальное — напрямую.
Нет Xray (запуск без Docker) или нет рабочих серверов — бот работает как обычно, просто без Gemini.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

import config

log = logging.getLogger("scrimbot.vpn")

SERVER_TAG = "srv-"
REFRESH_EVERY = 6 * 3600
# Так подписку отдают списком ключей: многие сервисы для «незнакомых» клиентов отвечают YAML для Clash.
SUBSCRIPTION_USER_AGENT = "v2rayN/7.0"
PROBE_URL = "https://www.gstatic.com/generate_204"


# --- разбор ключей ------------------------------------------------------------


def _query(url: str) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def _stream(params: dict[str, str], default_security: str = "none") -> dict:
    """streamSettings Xray из параметров ссылки: транспорт и шифрование."""
    network = (params.get("type") or "tcp").lower()
    if network == "raw":
        network = "tcp"
    security = (params.get("security") or default_security).lower()
    stream: dict = {"network": network, "security": security}

    sni = params.get("sni") or params.get("peer") or params.get("host") or ""
    fingerprint = params.get("fp") or "chrome"
    if security == "tls":
        tls: dict = {"serverName": sni, "fingerprint": fingerprint}
        if params.get("alpn"):
            tls["alpn"] = [item for item in params["alpn"].split(",") if item]
        if params.get("allowInsecure") in ("1", "true"):
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": sni,
            "fingerprint": fingerprint,
            "publicKey": params.get("pbk", ""),
            "shortId": params.get("sid", ""),
            "spiderX": params.get("spx", ""),
        }

    path = params.get("path") or "/"
    host = params.get("host", "")
    if network == "ws":
        stream["wsSettings"] = {"path": path, "host": host}
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": params.get("serviceName") or params.get("path", ""),
            "multiMode": params.get("mode") == "multi",
        }
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": host}
    elif network in ("xhttp", "splithttp"):
        stream["network"] = "xhttp"
        stream["xhttpSettings"] = {"path": path, "host": host, "mode": params.get("mode") or "auto"}
    elif network == "tcp" and params.get("headerType") == "http":
        stream["tcpSettings"] = {"header": {"type": "http", "request": {
            "path": [path], "headers": {"Host": [host]} if host else {},
        }}}
    return stream


def _vless(link: str) -> tuple[str, dict]:
    parts = urlsplit(link)
    params = _query(link)
    user: dict = {"id": unquote(parts.username or ""), "encryption": params.get("encryption") or "none"}
    if params.get("flow"):
        user["flow"] = params["flow"]
    outbound = {
        "protocol": "vless",
        "settings": {"vnext": [{"address": parts.hostname, "port": parts.port or 443, "users": [user]}]},
        "streamSettings": _stream(params),
    }
    return unquote(parts.fragment), outbound


def _trojan(link: str) -> tuple[str, dict]:
    parts = urlsplit(link)
    params = _query(link)
    outbound = {
        "protocol": "trojan",
        "settings": {"servers": [{
            "address": parts.hostname, "port": parts.port or 443, "password": unquote(parts.username or ""),
        }]},
        "streamSettings": _stream(params, default_security="tls"),
    }
    return unquote(parts.fragment), outbound


def _vmess(link: str) -> tuple[str, dict]:
    data = json.loads(_b64decode(link[len("vmess://"):]))
    params = {
        "type": data.get("net") or "tcp",
        "security": "tls" if str(data.get("tls", "")).lower() == "tls" else "none",
        "sni": data.get("sni") or data.get("host") or "",
        "host": data.get("host") or "",
        "path": data.get("path") or "",
        "fp": data.get("fp") or "chrome",
        "alpn": data.get("alpn") or "",
        "headerType": data.get("type") or "",
        "serviceName": data.get("path") or "",
    }
    outbound = {
        "protocol": "vmess",
        "settings": {"vnext": [{
            "address": data["add"], "port": int(data["port"]),
            "users": [{"id": data["id"], "alterId": int(data.get("aid") or 0), "security": data.get("scy") or "auto"}],
        }]},
        "streamSettings": _stream(params),
    }
    return str(data.get("ps") or ""), outbound


PARSERS = {"vless://": _vless, "trojan://": _trojan, "vmess://": _vmess}


def parse_link(link: str) -> tuple[str, dict] | None:
    """(название сервера, outbound для Xray) или None — формат не поддерживается или ссылка битая."""
    link = link.strip()
    for prefix, parser in PARSERS.items():
        if link.lower().startswith(prefix):
            try:
                name, outbound = parser(link)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return None
            servers = outbound["settings"].get("vnext") or outbound["settings"].get("servers")
            if not servers or not servers[0].get("address"):
                return None
            return name, outbound
    return None


def _b64decode(text: str) -> str:
    text = "".join(text.split())
    text += "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return base64.b64decode(text).decode("utf-8")


def split_links(text: str) -> list[str]:
    """Ссылки из подписки: обычно это base64 со списком ключей построчно, иногда — сразу список."""
    body = text.strip()
    if "://" not in body:
        try:
            body = _b64decode(body)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return []
    return [line.strip() for line in re.split(r"[\r\n]+|\s(?=\w+://)", body) if "://" in line]


# --- конфиг Xray --------------------------------------------------------------


def build_config(links: list[str], *, port: int, name_filter: str = "", limit: int = 30) -> tuple[dict, list[str]] | None:
    """Конфиг Xray: HTTP-прокси на 127.0.0.1:port и балансировщик по самому быстрому живому серверу."""
    pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
    outbounds: list[dict] = []
    names: list[str] = []
    for link in links:
        parsed = parse_link(link)
        if parsed is None:
            continue
        name, outbound = parsed
        if pattern is not None and not pattern.search(name):
            continue
        outbound["tag"] = f"{SERVER_TAG}{len(outbounds)}"
        outbounds.append(outbound)
        names.append(name or outbound["tag"])
        if len(outbounds) >= limit:
            break
    if not outbounds:
        return None
    xray = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "http-in", "listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {},
        }],
        "outbounds": outbounds + [{"tag": "direct", "protocol": "freedom"}],
        # Каждую минуту пингуем все серверы; мёртвые выпадают, запросы идут через самый быстрый.
        "observatory": {
            "subjectSelector": [SERVER_TAG],
            "probeURL": PROBE_URL,
            "probeInterval": "1m",
            "enableConcurrency": True,
        },
        "routing": {
            "balancers": [{
                "tag": "best",
                "selector": [SERVER_TAG],
                "strategy": {"type": "leastPing"},
                "fallbackTag": outbounds[0]["tag"],
            }],
            "rules": [{"type": "field", "inboundTag": ["http-in"], "balancerTag": "best"}],
        },
    }
    return xray, names


# --- запуск -------------------------------------------------------------------


class VPN:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.links_key = ""
        self.config_path = Path(tempfile.gettempdir()) / "oleg-xray.json"

    @property
    def enabled(self) -> bool:
        return bool(config.VPN_SUBSCRIPTION or config.VPN_URL)

    async def start(self) -> None:
        if not self.enabled:
            return
        if shutil.which(config.XRAY_BIN) is None:
            log.warning(
                "VPN указан в .env, но Xray не найден (%s) — он есть только в Docker-образе бота. "
                "Gemini будет недоступен из России.", config.XRAY_BIN,
            )
            return
        await self.refresh()
        self.task = asyncio.create_task(self._refresh_forever())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        await self._stop()

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_EVERY)
            try:
                await self.refresh()
            except Exception:
                log.exception("Не удалось обновить подписку VPN")

    async def load_links(self) -> list[str]:
        links = split_links(config.VPN_URL.replace(",", "\n")) if config.VPN_URL else []
        if config.VPN_SUBSCRIPTION:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                    async with session.get(
                        config.VPN_SUBSCRIPTION, headers={"User-Agent": SUBSCRIPTION_USER_AGENT}
                    ) as response:
                        response.raise_for_status()
                        links += split_links(await response.text())
            except (aiohttp.ClientError, TimeoutError) as error:
                log.warning("Подписка VPN не скачалась: %r", error)
        return links

    async def refresh(self) -> None:
        links = await self.load_links()
        key = "\n".join(links)
        if not links:
            log.warning("В подписке VPN не нашлось ключей — Gemini из России работать не будет")
            return
        if key == self.links_key and self.process is not None and self.process.returncode is None:
            return
        built = build_config(links, port=config.VPN_PORT, name_filter=config.VPN_FILTER)
        if built is None:
            log.warning(
                "Из %d ключей подписки ни один не подошёл (поддерживаются VLESS, Trojan, VMess%s)",
                len(links), f", фильтр «{config.VPN_FILTER}»" if config.VPN_FILTER else "",
            )
            return
        xray, names = built
        self.config_path.write_text(json.dumps(xray, ensure_ascii=False), encoding="utf-8")
        await self._stop()
        self.process = await asyncio.create_subprocess_exec(
            config.XRAY_BIN, "run", "-c", str(self.config_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._pipe_logs(self.process))
        self.links_key = key
        log.info("VPN запущен: серверов %d (%s…), Xray выбирает самый быстрый", len(names), ", ".join(names[:5]))

    async def _pipe_logs(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.debug("xray: %s", line)
        if process is self.process:
            log.warning("Xray остановился (код %s) — перезапущу при следующем обновлении подписки", process.returncode)

    async def _stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()


VPN_CLIENT = VPN()
