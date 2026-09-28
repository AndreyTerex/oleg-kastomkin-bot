import asyncio
import base64
import json

import vpn
import vpn_links as links

UUID = "2b4e3a3c-1111-4222-8333-444455556666"
REALITY = (
    f"vless://{UUID}@de.example.com:443?type=tcp&security=reality&flow=xtls-rprx-vision&encryption=none"
    "&sni=www.microsoft.com&pbk=PUBKEY&sid=6ba8&spx=%2Fx&pqv=VERIFY&fp=chrome#%F0%9F%87%A9%F0%9F%87%AA%20Germany"
)
XHTTP = (
    f"vless://{UUID}@fi.example.com:443?type=xhttp&security=tls&sni=fi.example.com&alpn=h2,http%2F1.1"
    "&path=%2Fxh&mode=stream-one&encryption=mlkem768x25519plus.native.0rtt.KEY"
    "&extra=%7B%22xPaddingBytes%22%3A%22100-1000%22%7D#Finland%20XHTTP"
)
GRPC = f"vless://{UUID}@se.example.com:443?type=grpc&security=tls&serviceName=svc&authority=cdn.example.com&mode=multi#Sweden"
TROJAN = "trojan://secret@pl.example.com:443?sni=pl.example.com&type=ws&path=%2Ftr&host=pl.example.com#Poland"
SS = "ss://" + base64.urlsafe_b64encode(b"2022-blake3-aes-128-gcm:c2VjcmV0").decode().rstrip("=") + "@nl.example.com:8388#Netherlands%20SS"
SS_LEGACY = "ss://" + base64.b64encode(b"aes-256-gcm:pass@lv.example.com:8388").decode() + "#Latvia"
HY2 = "hysteria2://authpass@us.example.com:443?sni=us.example.com&obfs=salamander&obfs-password=obfspw#USA%20HY2"


def vmess_link(name="VMess NL", **extra):
    data = {"v": "2", "ps": name, "add": "nl.example.com", "port": "443", "id": UUID, "aid": "0", "net": "ws",
            "path": "/ws", "host": "nl.example.com", "tls": "tls", "sni": "nl.example.com", **extra}
    return "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()


# --- разбор ключей -------------------------------------------------------------------


def test_vless_reality_all_params():
    server = links.parse_link(REALITY)
    assert server.name == "🇩🇪 Germany" and server.protocol == "vless"
    user = server.outbound["settings"]["vnext"][0]["users"][0]
    assert user == {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}
    reality = server.outbound["streamSettings"]["realitySettings"]
    assert reality == {"serverName": "www.microsoft.com", "fingerprint": "chrome", "publicKey": "PUBKEY",
                       "shortId": "6ba8", "spiderX": "/x", "mldsa65Verify": "VERIFY"}


def test_vless_xhttp_extra_encryption_alpn_and_grpc_authority():
    xhttp = links.parse_link(XHTTP).outbound
    assert xhttp["settings"]["vnext"][0]["users"][0]["encryption"].startswith("mlkem768x25519plus")
    stream = xhttp["streamSettings"]
    assert stream["xhttpSettings"] == {"path": "/xh", "host": "", "mode": "stream-one",
                                       "extra": {"xPaddingBytes": "100-1000"}}
    assert stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
    grpc = links.parse_link(GRPC).outbound["streamSettings"]["grpcSettings"]
    assert grpc == {"serviceName": "svc", "multiMode": True, "authority": "cdn.example.com"}


def test_trojan_vmess_shadowsocks_hysteria2():
    trojan = links.parse_link(TROJAN).outbound
    assert trojan["protocol"] == "trojan" and trojan["streamSettings"]["security"] == "tls"
    assert trojan["streamSettings"]["wsSettings"] == {"path": "/tr", "host": "pl.example.com"}
    vmess = links.parse_link(vmess_link())
    assert vmess.name == "VMess NL" and vmess.outbound["streamSettings"]["tlsSettings"]["serverName"] == "nl.example.com"
    ss = links.parse_link(SS).outbound["settings"]["servers"][0]
    assert ss["method"] == "2022-blake3-aes-128-gcm" and ss["port"] == 8388
    legacy = links.parse_link(SS_LEGACY)
    assert legacy.outbound["settings"]["servers"][0]["method"] == "aes-256-gcm" and legacy.name == "Latvia"
    hy2 = links.parse_link(HY2).outbound
    assert hy2["protocol"] == "hysteria" and hy2["settings"]["version"] == 2
    assert hy2["streamSettings"]["hysteriaSettings"] == {"version": 2, "auth": "authpass"}
    assert hy2["streamSettings"]["finalmask"]["udp"][0]["type"] == "salamander"


def test_no_allow_insecure_it_breaks_xray_26():
    server = links.parse_link(f"vless://{UUID}@a.example.com:443?security=tls&allowInsecure=1#A")
    assert "allowInsecure" not in json.dumps(server.outbound)


def test_fingerprint_ignores_name_and_hides_secrets():
    renamed = REALITY.split("#")[0] + "#Другое название"
    assert links.parse_link(REALITY).fingerprint == links.parse_link(renamed).fingerprint
    server = links.parse_link(REALITY)
    assert UUID not in repr(server) and "de.example.com" not in repr(server)


def test_broken_and_unsupported():
    for bad in ("vmess://not-base64!!", "vless://@:443", "trojan://@host"):
        try:
            links.parse_link(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    try:
        links.parse_link("ss://YWVzOnBhc3M@1.2.3.4:8388?plugin=obfs-local#x")
    except links.Unsupported:
        pass
    else:
        raise AssertionError("plugin")


# --- подписки ------------------------------------------------------------------------


def test_subscription_base64_plain_and_counts_unsupported():
    body = "\n".join([REALITY, TROJAN, "tuic://x@y:1#t", "garbage"])
    plain, skipped = links.parse_subscription(body)
    encoded, _ = links.parse_subscription(base64.b64encode(body.encode()).decode())
    assert [s.name for s in plain] == [s.name for s in encoded] == ["🇩🇪 Germany", "Poland"]
    assert skipped["tuic://"] == 1


def test_subscription_xray_json_array():
    configs = [
        {"remarks": "🇩🇪 Germany JSON", "outbounds": [
            {"tag": "proxy", "protocol": "vless", "settings": {"vnext": [{"address": "de.example.com", "port": 443,
             "users": [{"id": UUID, "encryption": "none"}]}]}, "streamSettings": {"network": "tcp"}},
            {"tag": "direct", "protocol": "freedom"},
        ]},
        {"remarks": "Wireguard", "outbounds": [{"tag": "proxy", "protocol": "wireguard", "settings": {}}]},
    ]
    servers, skipped = links.parse_subscription(json.dumps(configs))
    assert [s.name for s in servers] == ["🇩🇪 Germany JSON"] and servers[0].address == "de.example.com"
    assert skipped["протокол wireguard"] == 1


def test_subscription_singbox_json():
    config = {"outbounds": [
        {"type": "vless", "tag": "SB Reality", "server": "sb.example.com", "server_port": 443, "uuid": UUID,
         "flow": "xtls-rprx-vision", "tls": {"enabled": True, "server_name": "www.apple.com",
                                             "utls": {"fingerprint": "firefox"},
                                             "reality": {"enabled": True, "public_key": "PK", "short_id": "ab"}}},
        {"type": "hysteria2", "tag": "SB HY2", "server": "hy.example.com", "server_port": 8443, "password": "pw",
         "tls": {"enabled": True, "server_name": "hy.example.com"}},
        {"type": "direct", "tag": "direct"},
    ]}
    servers, _ = links.parse_subscription(json.dumps(config))
    assert [s.name for s in servers] == ["SB Reality", "SB HY2"]
    reality = servers[0].outbound["streamSettings"]["realitySettings"]
    assert reality["publicKey"] == "PK" and reality["fingerprint"] == "firefox"


def test_clash_yaml_is_reported():
    servers, skipped = links.parse_subscription("proxies:\n  - name: x\n")
    assert servers == [] and any("Clash" in reason for reason in skipped)


def test_service_entries_and_bad_addresses_are_dropped_before_probing():
    service = [
        f"vless://{UUID}@0.0.0.0:1?security=tls#Осталось 12 дней",
        f"vless://{UUID}@127.0.0.1:443?security=tls#Трафик: 50 GB",
        f"vless://{UUID}@example.com:443?security=tls#Сайт: vpn.example / поддержка",
        f"vless://{UUID}@192.168.1.10:443?security=tls#LAN",
        f"vless://{UUID}@good.example.com:0?security=tls#Порт ноль",
    ]
    servers, _ = links.parse_subscription("\n".join(service + [REALITY, REALITY, TROJAN]))
    kept, dropped = links.usable(servers)
    assert [s.name for s in kept] == ["🇩🇪 Germany", "Poland"]
    assert dropped["дубль"] == 1 and dropped["служебная запись"] >= 1 and dropped["служебный адрес"] >= 3
    only, _ = links.usable(servers, "poland")
    _, skipped = links.parse_subscription(f"vless://{UUID}@example.com:443?security=none#open")
    assert skipped == {"VLESS без шифрования": 1}
    assert [s.name for s in only] == ["Poland"]


# --- выбор сервера -------------------------------------------------------------------


EXCLUDED = {"RU", "BY", "CN"}


def result(fp, latency, country="DE", ok=True):
    return vpn.ProbeResult(fp, fp, ok, latency, country)


def test_parse_trace():
    assert vpn.parse_trace("fl=1\nip=1.2.3.4\nloc=de\ntls=TLSv1.3\n") == "DE"
    assert vpn.parse_trace("nothing") is None


def test_exit_country_decides_not_name():
    results = {
        "ru-exit": result("ru-exit", 20, "RU"),       # быстрый, но выход в России
        "via-ru": result("via-ru", 80, "DE"),         # «Россия → Германия»: выход в DE — годится
        "dead": result("dead", None, None, ok=False),
        "unknown": result("unknown", 10, None),       # страну не узнали — не рискуем
    }
    assert vpn.choose(None, results, EXCLUDED, 0.3).fingerprint == "via-ru"


def test_switch_only_when_notably_faster():
    base = {"cur": result("cur", 100), "new": result("new", 80)}
    assert vpn.choose("cur", base, EXCLUDED, 0.3).fingerprint == "cur"      # быстрее всего на 20% — держимся
    faster = {"cur": result("cur", 100), "new": result("new", 65)}
    assert vpn.choose("cur", faster, EXCLUDED, 0.3).fingerprint == "new"    # на 35% — переключаемся
    broken = {"cur": result("cur", None, None, ok=False), "new": result("new", 300)}
    assert vpn.choose("cur", broken, EXCLUDED, 0.3).fingerprint == "new"    # текущий упал — уходим
    gone = {"new": result("new", 300)}
    assert vpn.choose("cur", gone, EXCLUDED, 0.3).fingerprint == "new"      # текущего нет в подписке
    assert vpn.choose("cur", {"x": result("x", 10, "RU")}, EXCLUDED, 0.3) is None
    # 35% быстрее, но всего на 35 мс — ради этого Discord не переподключаем.
    assert vpn.choose("cur", faster, EXCLUDED, 0.3, min_gain_ms=150).fingerprint == "cur"
    far = {"cur": result("cur", 400), "new": result("new", 120)}
    assert vpn.choose("cur", far, EXCLUDED, 0.3, min_gain_ms=150).fingerprint == "new"
    assert vpn.choose("cur", broken, EXCLUDED, 0.3, min_gain_ms=150).fingerprint == "new"


# --- data/vpn.json -------------------------------------------------------------------


def test_state_roundtrip_has_no_secrets(tmp_path):
    server = links.parse_link(REALITY)
    state = vpn.VpnState(tmp_path / "vpn.json")
    state.load()
    chosen = vpn.ProbeResult(server.fingerprint, server.name, True, 84.6, "DE", fragment=True)
    state.select(chosen)
    state.record({server.fingerprint: chosen, "deadbeef": result("deadbeef", None, None, ok=False)}, now=1000.0)
    asyncio.run(state.save())

    text = (tmp_path / "vpn.json").read_text(encoding="utf-8")
    for secret in (UUID, "de.example.com", "PUBKEY", "vless://"):
        assert secret not in text

    loaded = vpn.VpnState(tmp_path / "vpn.json")
    loaded.load()
    assert loaded.selected["fingerprint"] == server.fingerprint and loaded.selected["country"] == "DE"
    assert loaded.selected["latency_ms"] == 85 and loaded.selected["fragment"] is True
    assert loaded.blacklisted(now=1000.0 + 60) == {"deadbeef"}
    assert loaded.blacklisted(now=1000.0 + vpn.BLACKLIST_TTL + 1) == set()
    assert loaded.data["fragment"][server.fingerprint] is True


# --- конфиги Xray --------------------------------------------------------------------


def test_main_config_single_server_with_fragment():
    server = links.parse_link(REALITY)
    config = vpn.main_config(server, fragment=True, port=10809)
    tags = [o["tag"] for o in config["outbounds"]]
    assert tags == ["proxy", "direct", "fragment"]
    proxy = config["outbounds"][0]
    assert proxy["streamSettings"]["sockopt"]["dialerProxy"] == "fragment"
    fragment = config["outbounds"][2]
    assert fragment["protocol"] == "freedom" and fragment["settings"]["fragment"]["packets"] == "tlshello"
    assert config["inbounds"][0]["listen"] == "127.0.0.1" and config["inbounds"][0]["port"] == 10809
    assert config["routing"]["rules"] == [{"type": "field", "inboundTag": ["http-in"], "outboundTag": "proxy"}]
    # Исходный outbound сервера не портится.
    assert "sockopt" not in server.outbound["streamSettings"]
    plain = vpn.main_config(server, fragment=False, port=10809)
    assert [o["tag"] for o in plain["outbounds"]] == ["proxy", "direct"]


def test_probe_config_port_per_server():
    servers = [links.parse_link(REALITY), links.parse_link(TROJAN)]
    config = vpn.probe_config([(servers[0], False), (servers[1], True)], base_port=20000)
    assert [i["port"] for i in config["inbounds"]] == [20000, 20001]
    rules = config["routing"]["rules"]
    assert rules[1] == {"type": "field", "inboundTag": ["in-1"], "outboundTag": "out-1"}
    outbounds = {o["tag"]: o for o in config["outbounds"]}
    assert "fragment" in outbounds and outbounds["out-1"]["streamSettings"]["sockopt"]["dialerProxy"] == "fragment"
    assert "sockopt" not in outbounds["out-0"]["streamSettings"]


def test_direct_config_keeps_the_same_port_without_servers():
    config = vpn.direct_config(10809)
    assert config["inbounds"][0]["port"] == 10809
    assert config["outbounds"] == [{"tag": "proxy", "protocol": "freedom"}]


def test_failures_in_direct_mode_do_not_trigger_full_scans():
    client = vpn.VPN()
    client.started = True
    client.report_failure()
    assert not client.wakeup.is_set()


def test_sanitize_hides_the_whole_ip_with_port():
    clean = vpn.sanitize("proxy/http: failed to read response from 198.51.100.7:21810 > io: closed pipe")
    assert "198" not in clean and "100.7" not in clean and "closed pipe" in clean


def test_sanitize_drops_xray_timestamp():
    assert vpn.sanitize("2026/09/28 13:33:49.326003 [Warning] core: oops") == "[Warning] core: oops"


def test_light_recheck_does_not_probe_all_servers(monkeypatch, tmp_path):
    client = vpn.VPN()
    client.state = vpn.VpnState(tmp_path / "vpn.json")
    client.state.load()
    client.started = True
    probed = []

    async def alive():
        return True

    async def probe(servers):
        probed.append(servers)
        return {}

    monkeypatch.setattr(client, "_current_alive", alive)
    monkeypatch.setattr(client, "probe", probe)
    asyncio.run(client.recheck(full=False))
    assert probed == []


def test_full_check_is_due_by_last_full_check(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn.config, "VPN_FULL_CHECK_HOURS", 3.0)
    client = vpn.VPN()
    client.state = vpn.VpnState(tmp_path / "vpn.json")
    client.state.load()
    assert client._full_check_due()
    client.state.data["last_check"] = {"at": vpn.time.time() - 3600, "checked": 5, "working": 2}
    assert client._full_check_due()  # сервера нет, ходим напрямую — ищем раз в полчаса
    client.current = vpn.ProbeResult("fp", "Germany", True, 80.0, "DE")
    assert not client._full_check_due()  # сервер есть — все перебираем раз в 3 часа
    client.state.data["last_check"]["at"] = vpn.time.time() - 4 * 3600
    assert client._full_check_due()


def test_sanitize_hides_addresses_and_ids():
    line = f"[Warning] failed to dial tcp:203.0.113.5:443 via de.example.com user {UUID} [2001:db8::1]:8443"
    clean = vpn.sanitize(line)
    for secret in ("203.0.113", "113.5", "de.example.com", UUID, "2001:db8", "db8::1"):
        assert secret not in clean


def test_discord_goes_through_vpn_only_when_xray_is_up(monkeypatch):
    import bot as bot_module
    from discord.ext import commands

    async def fake_login(self, token):
        return None

    async def fake_start():
        return None

    monkeypatch.setattr(commands.Bot, "login", fake_login)
    monkeypatch.setattr(bot_module.VPN_CLIENT, "start", fake_start)
    monkeypatch.setattr(bot_module.config, "VPN_FOR_DISCORD", True)
    monkeypatch.setattr(bot_module.config, "VPN_PROXY_URL", "http://127.0.0.1:10809")

    async def run(ready):
        monkeypatch.setattr(type(bot_module.VPN_CLIENT), "ready", property(lambda self: ready))
        client = bot_module.ScrimBot()
        await client.login("token")
        proxy = client.http.proxy
        await client.http.close()
        return proxy

    assert asyncio.run(run(True)) == "http://127.0.0.1:10809"
    assert asyncio.run(run(False)) is None  # Xray нет (запуск без Docker) — Discord напрямую


def test_discord_goes_direct_by_default():
    import config

    assert config.VPN_FOR_DISCORD is False


def test_connection_failure_marks_vpn_broken_until_a_good_check(monkeypatch, tmp_path):
    client = vpn.VPN()
    client.state = vpn.VpnState(tmp_path / "vpn.json")
    client.state.load()
    client.started = True
    client.current = vpn.ProbeResult("fp", "Germany", True, 80.0, "DE")
    monkeypatch.setattr(type(client), "ready", property(lambda self: True))
    assert client.healthy
    client.report_failure(connection=True)
    assert not client.healthy and client.wakeup.is_set()
    client.broken_until = 0.0  # так делает удачная проверка или смена сервера
    assert client.healthy
