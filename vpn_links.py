"""Разбор VPN-ключей и подписок в outbound'ы Xray. Без сети и без запуска Xray — чистые функции.

Поддерживается: VLESS, Trojan, VMess, Shadowsocks, Hysteria2 — ссылками, а также подписки base64-списком,
простым списком и JSON (массив конфигов Xray или конфиг sing-box).

Секреты (uuid, пароли, адреса серверов) живут только в outbound'ах: в repr, логи и data/vpn.json
они не попадают — наружу выходят лишь название сервера и отпечаток ключа (sha256).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED_PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks", "hysteria")

# Служебные записи подписок: «Осталось 12 дней», «Трафик 50 GB», «Сайт/поддержка», «Обновите подписку».
SERVICE_NAME = re.compile(
    r"осталось|остаток|истека|истёк|срок|expire|трафик|traffic|remaining|days?\s*left|\bдн(ей|я)\b|"
    r"сайт|website|поддержк|support|t\.me|телеграм|telegram|обнов|update|продл|renew|баланс|balance|"
    r"подписк|subscription|оплат|payment|тариф",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Server:
    """Сервер из подписки. outbound (с секретами) не показывается в repr."""

    name: str
    protocol: str
    fingerprint: str
    outbound: dict = field(repr=False, compare=False, hash=False)
    address: str = field(repr=False, default="")
    port: int = field(repr=False, default=0)


# --- помощники ------------------------------------------------------------------


def _b64decode(text: str) -> str:
    text = "".join(text.split())
    text += "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return base64.b64decode(text).decode("utf-8")


def _query(url: str) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def _fingerprint(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalize_link(link: str) -> str:
    """Ключ без названия (#…) и лишних пробелов: переименование сервера не меняет отпечаток."""
    return link.strip().split("#", 1)[0]


def _list(value: str) -> list[str]:
    return [item for item in (value or "").split(",") if item]


def _json_param(value: str):
    if not value:
        return None
    try:
        data = json.loads(unquote(value))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_bad_address(address: str, port: int) -> bool:
    """Заглушки из подписок: пустой адрес, localhost, приватные сети, порт 0/1."""
    if not address or port in (0, 1) or not 0 < port < 65536:
        return True
    host = address.strip("[]").lower()
    if host in ("localhost", "0.0.0.0", "::", "::1") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_reserved or ip.is_multicast


def is_service_entry(name: str) -> bool:
    return bool(SERVICE_NAME.search(name or ""))


# --- транспорт и шифрование ---------------------------------------------------------


def build_stream(params: dict[str, str], default_security: str = "none") -> dict:
    """streamSettings Xray из параметров ссылки (формат share-link Xray)."""
    network = (params.get("type") or params.get("net") or "tcp").lower()
    if network == "raw":
        network = "tcp"
    if network == "splithttp":
        network = "xhttp"
    if network == "http":  # старое «h2» в Xray заменено на xhttp
        network = "xhttp"
    security = (params.get("security") or default_security).lower()
    if security in ("", "false", "0"):
        security = "none"
    stream: dict = {"network": network, "security": security}

    sni = params.get("sni") or params.get("peer") or ""
    fingerprint = params.get("fp") or "chrome"
    if security == "tls":
        tls: dict = {"fingerprint": fingerprint}
        if sni or params.get("host"):
            tls["serverName"] = sni or params.get("host", "")
        if params.get("alpn"):
            tls["alpn"] = _list(params["alpn"])
        # allowInsecure в Xray 26 удалён — вместо него закрепление сертификата.
        if params.get("pcs"):
            tls["pinnedPeerCertSha256"] = params["pcs"]
        if params.get("vcn"):
            tls["verifyPeerCertByName"] = params["vcn"]
        if params.get("ech"):
            tls["echConfigList"] = params["ech"]
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality = {
            "serverName": sni,
            "fingerprint": fingerprint,
            "publicKey": params.get("pbk", ""),
            "shortId": params.get("sid", ""),
            "spiderX": params.get("spx", ""),
        }
        if params.get("pqv"):
            reality["mldsa65Verify"] = params["pqv"]
        stream["realitySettings"] = reality

    path = params.get("path") or "/"
    host = params.get("host", "")
    if network == "ws":
        stream["wsSettings"] = {"path": path, "host": host}
    elif network == "grpc":
        grpc = {"serviceName": params.get("serviceName") or params.get("path", ""), "multiMode": params.get("mode") == "multi"}
        if params.get("authority"):
            grpc["authority"] = params["authority"]
        stream["grpcSettings"] = grpc
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": host}
    elif network == "xhttp":
        xhttp: dict = {"path": path, "host": host, "mode": params.get("mode") or "auto"}
        extra = _json_param(params.get("extra", ""))
        if extra:
            xhttp["extra"] = extra
        stream["xhttpSettings"] = xhttp
    elif network == "kcp":
        kcp: dict = {}
        if params.get("seed"):
            kcp["seed"] = params["seed"]
        if params.get("headerType") and params["headerType"] != "none":
            kcp["header"] = {"type": params["headerType"]}
        stream["kcpSettings"] = kcp
    elif network == "tcp" and params.get("headerType") == "http":
        stream["tcpSettings"] = {"header": {"type": "http", "request": {
            "path": _list(path) or ["/"], "headers": {"Host": _list(host)} if host else {},
        }}}
    return stream


# --- протоколы ----------------------------------------------------------------------


def vless_outbound(address: str, port: int, uuid: str, params: dict[str, str]) -> dict:
    user: dict = {"id": uuid, "encryption": params.get("encryption") or "none"}
    if params.get("flow"):
        user["flow"] = params["flow"]
    stream = build_stream(params)
    if stream.get("security", "none") == "none" and user["encryption"] == "none":
        # Xray 26 не запускает такой ключ на публичном адресе: трафик шёл бы открытым текстом
        raise Unsupported("VLESS без шифрования")
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
        "streamSettings": stream,
    }


def trojan_outbound(address: str, port: int, password: str, params: dict[str, str]) -> dict:
    return {
        "protocol": "trojan",
        "settings": {"servers": [{"address": address, "port": port, "password": password}]},
        "streamSettings": build_stream(params, default_security="tls"),
    }


def vmess_outbound(address: str, port: int, uuid: str, alter_id: int, cipher: str, params: dict[str, str]) -> dict:
    return {
        "protocol": "vmess",
        "settings": {"vnext": [{
            "address": address, "port": port,
            "users": [{"id": uuid, "alterId": alter_id, "security": cipher or "auto"}],
        }]},
        "streamSettings": build_stream(params),
    }


def shadowsocks_outbound(address: str, port: int, method: str, password: str) -> dict:
    return {
        "protocol": "shadowsocks",
        "settings": {"servers": [{"address": address, "port": port, "method": method, "password": password}]},
        "streamSettings": {"network": "tcp"},
    }


def hysteria2_outbound(address: str, port: int, auth: str, params: dict[str, str]) -> dict:
    tls: dict = {"serverName": params.get("sni") or "", "alpn": _list(params.get("alpn", "")) or ["h3"]}
    if params.get("pinSHA256") or params.get("pcs"):
        tls["pinnedPeerCertSha256"] = (params.get("pinSHA256") or params.get("pcs", "")).replace(":", "")
    stream: dict = {
        "network": "hysteria",
        "security": "tls",
        "tlsSettings": tls,
        "hysteriaSettings": {"version": 2, "auth": auth},
    }
    if params.get("obfs") == "salamander" and params.get("obfs-password"):
        stream["finalmask"] = {"udp": [{"type": "salamander", "settings": {"password": params["obfs-password"]}}]}
    return {
        "protocol": "hysteria",
        "settings": {"version": 2, "address": address, "port": port},
        "streamSettings": stream,
    }


def endpoint(outbound: dict) -> tuple[str, int]:
    settings = outbound.get("settings") or {}
    for key in ("vnext", "servers"):
        items = settings.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return str(items[0].get("address") or ""), int(items[0].get("port") or 0)
    return str(settings.get("address") or ""), int(settings.get("port") or 0)


# --- ссылки -------------------------------------------------------------------------


class Unsupported(ValueError):
    """Ключ понятен, но не поддерживается (например, плагин Shadowsocks)."""


def _host_port(parts) -> tuple[str, int]:
    return parts.hostname or "", parts.port if parts.port is not None else 443


def _parse_ss(link: str) -> tuple[str, dict]:
    parts = urlsplit(link)
    params = _query(link)
    if params.get("plugin"):
        raise Unsupported("плагин Shadowsocks")
    name = unquote(parts.fragment)
    if parts.username and parts.hostname:
        userinfo = unquote(parts.username) if parts.password is None else f"{unquote(parts.username)}:{unquote(parts.password)}"
        if ":" not in userinfo:
            userinfo = _b64decode(userinfo)
        method, _, password = userinfo.partition(":")
        address, port = parts.hostname, parts.port or 0
    else:
        decoded = _b64decode(link[len("ss://"):].split("#", 1)[0].split("?", 1)[0])
        credentials, _, hostport = decoded.rpartition("@")
        method, _, password = credentials.partition(":")
        host, _, port_text = hostport.rpartition(":")
        address, port = host.strip("[]"), int(port_text)
    if not method or not password:
        raise ValueError("пустой метод или пароль")
    return name, shadowsocks_outbound(address, port, method, password)


def _parse_vmess(link: str) -> tuple[str, dict]:
    data = json.loads(_b64decode(link[len("vmess://"):].split("#", 1)[0]))
    tls_mode = str(data.get("tls") or "").lower()
    params = {
        "type": str(data.get("net") or "tcp"),
        "security": tls_mode if tls_mode in ("tls", "reality") else "none",
        "sni": str(data.get("sni") or ""),
        "host": str(data.get("host") or ""),
        "path": str(data.get("path") or ""),
        "fp": str(data.get("fp") or "chrome"),
        "alpn": str(data.get("alpn") or ""),
        "headerType": str(data.get("type") or ""),
        "serviceName": str(data.get("path") or ""),
        "authority": str(data.get("authority") or ""),
        "pbk": str(data.get("pbk") or ""),
        "sid": str(data.get("sid") or ""),
        "spx": str(data.get("spx") or ""),
    }
    outbound = vmess_outbound(
        str(data["add"]), int(data["port"]), str(data["id"]), int(data.get("aid") or 0), str(data.get("scy") or "auto"), params,
    )
    return str(data.get("ps") or ""), outbound


def _parse_url_style(link: str, scheme: str) -> tuple[str, dict]:
    parts = urlsplit(link)
    params = _query(link)
    address, port = _host_port(parts)
    secret = unquote(parts.username or "")
    if parts.password is not None:  # hysteria2://user:pass@… — auth целиком
        secret = f"{secret}:{unquote(parts.password)}"
    if not secret:
        raise ValueError("нет uuid/пароля")
    if scheme == "vless":
        outbound = vless_outbound(address, port, secret, params)
    elif scheme == "trojan":
        outbound = trojan_outbound(address, port, secret, params)
    else:
        outbound = hysteria2_outbound(address, port, secret, params)
    return unquote(parts.fragment), outbound


SCHEMES = {
    "vless://": "vless", "trojan://": "trojan", "vmess://": "vmess", "ss://": "ss",
    "hysteria2://": "hysteria2", "hy2://": "hysteria2",
}


def parse_link(link: str) -> Server:
    """Server из ссылки. ValueError — битая ссылка, Unsupported — формат, который не поддерживается."""
    link = link.strip()
    lowered = link.lower()
    scheme = next((kind for prefix, kind in SCHEMES.items() if lowered.startswith(prefix)), None)
    if scheme is None:
        raise Unsupported(lowered.split("://", 1)[0] + "://" if "://" in lowered else "неизвестный формат")
    try:
        if scheme == "vmess":
            name, outbound = _parse_vmess(link)
            material = json.dumps({k: v for k, v in json.loads(_b64decode(link[8:].split("#", 1)[0])).items() if k != "ps"},
                                  sort_keys=True)
        elif scheme == "ss":
            name, outbound = _parse_ss(link)
            material = _normalize_link(link)
        else:
            name, outbound = _parse_url_style(link, scheme)
            material = _normalize_link(link)
    except Unsupported:
        raise
    except (KeyError, TypeError, IndexError, json.JSONDecodeError, binascii.Error, UnicodeDecodeError) as error:
        raise ValueError(type(error).__name__) from None
    address, port = endpoint(outbound)
    return Server(name.strip(), outbound["protocol"], _fingerprint(material), outbound, address, port)


# --- JSON-подписки ------------------------------------------------------------------


def _from_xray_outbound(outbound: dict, name: str) -> Server | None:
    if outbound.get("protocol") not in SUPPORTED_PROTOCOLS:
        return None
    clean = {key: value for key, value in outbound.items() if key not in ("tag", "mux")}
    address, port = endpoint(clean)
    return Server(name or str(outbound.get("tag") or ""), clean["protocol"],
                  _fingerprint(json.dumps(clean, sort_keys=True)), clean, address, port)


def _singbox_params(item: dict) -> dict[str, str]:
    tls = item.get("tls") or {}
    transport = item.get("transport") or {}
    params: dict[str, str] = {}
    if tls.get("enabled"):
        reality = tls.get("reality") or {}
        params["security"] = "reality" if reality.get("enabled") else "tls"
        params["sni"] = str(tls.get("server_name") or "")
        params["fp"] = str((tls.get("utls") or {}).get("fingerprint") or "chrome")
        if tls.get("alpn"):
            params["alpn"] = ",".join(tls["alpn"])
        if reality.get("enabled"):
            params["pbk"] = str(reality.get("public_key") or "")
            params["sid"] = str(reality.get("short_id") or "")
    kind = str(transport.get("type") or "tcp")
    params["type"] = {"http": "xhttp"}.get(kind, kind)
    params["path"] = str(transport.get("path") or "")
    headers = transport.get("headers") or {}
    host = headers.get("Host") or headers.get("host") or transport.get("host") or ""
    params["host"] = host[0] if isinstance(host, list) and host else str(host or "")
    params["serviceName"] = str(transport.get("service_name") or "")
    if item.get("flow"):
        params["flow"] = str(item["flow"])
    return params


def _from_singbox(item: dict) -> Server | None:
    kind = item.get("type")
    address, port = str(item.get("server") or ""), int(item.get("server_port") or 0)
    params = _singbox_params(item)
    if kind == "vless":
        outbound = vless_outbound(address, port, str(item.get("uuid") or ""), params)
    elif kind == "trojan":
        outbound = trojan_outbound(address, port, str(item.get("password") or ""), params)
    elif kind == "vmess":
        outbound = vmess_outbound(address, port, str(item.get("uuid") or ""), int(item.get("alter_id") or 0),
                                  str(item.get("security") or "auto"), params)
    elif kind == "shadowsocks":
        outbound = shadowsocks_outbound(address, port, str(item.get("method") or ""), str(item.get("password") or ""))
    elif kind == "hysteria2":
        obfs = item.get("obfs") or {}
        params.update({"obfs": str(obfs.get("type") or ""), "obfs-password": str(obfs.get("password") or "")})
        outbound = hysteria2_outbound(address, port, str(item.get("password") or ""), params)
    else:
        return None
    return Server(str(item.get("tag") or ""), outbound["protocol"],
                  _fingerprint(json.dumps(item, sort_keys=True, default=str)), outbound, address, port)


def _parse_json(data, skipped: Counter) -> list[Server]:
    configs = data if isinstance(data, list) else [data]
    servers: list[Server] = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        outbounds = config.get("outbounds") or ([config] if "protocol" in config or "type" in config else [])
        remarks = str(config.get("remarks") or config.get("ps") or "")
        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            try:
                if "protocol" in outbound:
                    server = _from_xray_outbound(outbound, remarks)
                    if server is None and outbound.get("protocol") not in ("freedom", "blackhole", "dns", "loopback"):
                        skipped[f"протокол {outbound.get('protocol')}"] += 1
                else:
                    server = _from_singbox(outbound)
                    if server is None and outbound.get("type") not in ("direct", "block", "dns", "selector", "urltest"):
                        skipped[f"протокол {outbound.get('type')}"] += 1
            except (ValueError, TypeError, KeyError):
                skipped["битый JSON-сервер"] += 1
                continue
            if server is not None:
                servers.append(server)
                if remarks:  # у конфига Xray первый подходящий outbound и есть сервер
                    break
    return servers


# --- подписка целиком ---------------------------------------------------------------


def parse_subscription(text: str) -> tuple[list[Server], Counter]:
    """Серверы из подписки и счётчик пропущенного (причина → сколько), без секретов."""
    skipped: Counter = Counter()
    body = (text or "").strip().lstrip("﻿")
    if not body:
        return [], skipped
    if body[0] in "[{":
        try:
            return _parse_json(json.loads(body), skipped), skipped
        except ValueError:
            skipped["битый JSON"] += 1
            return [], skipped
    if re.search(r"^\s*proxies\s*:", body, re.MULTILINE):
        skipped["формат Clash (YAML) — нужна подписка base64 или JSON"] += 1
        return [], skipped
    if "://" not in body:
        try:
            body = _b64decode(body)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            skipped["не похоже на подписку"] += 1
            return [], skipped
        if body.lstrip()[:1] in "[{":
            return parse_subscription(body)
    servers: list[Server] = []
    for line in re.split(r"[\r\n]+|\s(?=[a-z0-9]+://)", body):
        line = line.strip()
        if "://" not in line:
            continue
        try:
            servers.append(parse_link(line))
        except Unsupported as reason:
            skipped[str(reason)] += 1
        except ValueError:
            skipped["битый ключ"] += 1
    return servers, skipped


def usable(servers: list[Server], name_filter: str = "") -> tuple[list[Server], Counter]:
    """Отсев мусора до проверки: служебные записи, заглушки-адреса, дубли, фильтр по названию."""
    pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
    kept: list[Server] = []
    seen: set[str] = set()
    dropped: Counter = Counter()
    for server in servers:
        if server.fingerprint in seen:
            dropped["дубль"] += 1
        elif is_bad_address(server.address, server.port):
            dropped["служебный адрес"] += 1
        elif is_service_entry(server.name):
            dropped["служебная запись"] += 1
        elif pattern is not None and not pattern.search(server.name):
            dropped["не подходит под VPN_FILTER"] += 1
        else:
            kept.append(server)
            seen.add(server.fingerprint)
            continue
        seen.add(server.fingerprint)
    return kept, dropped
