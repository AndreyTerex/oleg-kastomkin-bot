import base64
import json

import vpn

UUID = "2b4e3a3c-1111-4222-8333-444455556666"
REALITY = (
    f"vless://{UUID}@de.example.com:443?type=tcp&security=reality&flow=xtls-rprx-vision"
    "&sni=www.microsoft.com&pbk=PUBKEY&sid=6ba8&fp=chrome#%F0%9F%87%A9%F0%9F%87%AA%20Germany"
)
WS_TLS = f"vless://{UUID}@fi.example.com:8443?type=ws&security=tls&sni=fi.example.com&host=fi.example.com&path=%2Fvl#Finland"
TROJAN = "trojan://secret@pl.example.com:443?sni=pl.example.com#Poland"


def vmess_link(name="VMess NL"):
    data = {"ps": name, "add": "nl.example.com", "port": "443", "id": UUID, "aid": "0", "net": "grpc",
            "path": "svc", "tls": "tls", "sni": "nl.example.com"}
    return "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()


def test_vless_reality():
    name, out = vpn.parse_link(REALITY)
    assert name == "🇩🇪 Germany"
    user = out["settings"]["vnext"][0]["users"][0]
    assert out["protocol"] == "vless" and user == {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}
    stream = out["streamSettings"]
    assert stream["security"] == "reality"
    assert stream["realitySettings"]["publicKey"] == "PUBKEY" and stream["realitySettings"]["shortId"] == "6ba8"


def test_vless_ws_tls_and_trojan_and_vmess():
    _, ws = vpn.parse_link(WS_TLS)
    assert ws["streamSettings"]["wsSettings"] == {"path": "/vl", "host": "fi.example.com"}
    assert ws["settings"]["vnext"][0]["port"] == 8443
    _, trojan = vpn.parse_link(TROJAN)
    assert trojan["protocol"] == "trojan" and trojan["streamSettings"]["security"] == "tls"
    name, vmess = vpn.parse_link(vmess_link())
    assert name == "VMess NL" and vmess["streamSettings"]["grpcSettings"]["serviceName"] == "svc"


def test_unsupported_and_broken_links():
    assert vpn.parse_link("ss://YWVzOnBhc3M@1.2.3.4:8388#SS") is None
    assert vpn.parse_link("vmess://not-base64!!") is None
    assert vpn.parse_link("vless://@:443") is None


def test_subscription_base64_and_plain():
    links = [REALITY, WS_TLS, TROJAN]
    encoded = base64.b64encode("\n".join(links).encode()).decode()
    assert vpn.split_links(encoded) == links
    assert vpn.split_links("\n".join(links)) == links


def test_build_config_balancer_and_filter():
    xray, names = vpn.build_config([REALITY, WS_TLS, TROJAN, vmess_link()], port=10809)
    assert names == ["🇩🇪 Germany", "Finland", "Poland", "VMess NL"]
    tags = [o["tag"] for o in xray["outbounds"]]
    assert tags == ["srv-0", "srv-1", "srv-2", "srv-3", "direct"]
    assert xray["inbounds"][0]["listen"] == "127.0.0.1" and xray["inbounds"][0]["port"] == 10809
    balancer = xray["routing"]["balancers"][0]
    assert balancer["strategy"]["type"] == "leastPing" and balancer["selector"] == ["srv-"]
    _, only = vpn.build_config([REALITY, WS_TLS, TROJAN], port=1, name_filter="germany|poland")
    assert only == ["🇩🇪 Germany", "Poland"]
    assert vpn.build_config(["ss://x"], port=1) is None
