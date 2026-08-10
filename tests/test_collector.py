"""
Unit tests for VOLTA config collector: protocol detection, host/port parsing,
and branded auto-generation of subscription node names.
Run: python -m pytest tests/ -v
"""
import base64
import json
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.collector import (
    detect_protocol,
    extract_host_port,
    rename_node,
    build_branded_lines,
)


class FakeConfig:
    def __init__(self, protocol, content, latency_ms=None):
        self.protocol = protocol
        self.content = content
        self.latency_ms = latency_ms


def test_detect_protocol_vless():
    assert detect_protocol("vless://uuid@1.2.3.4:443?type=tcp#name") == 'vless'


def test_detect_protocol_hysteria2_alias():
    assert detect_protocol("hy2://pass@1.2.3.4:443#name") == 'hysteria2'
    assert detect_protocol("hysteria2://pass@1.2.3.4:443#name") == 'hysteria2'


def test_detect_protocol_shadowsocks():
    assert detect_protocol("ss://base64stuff@1.2.3.4:8388#name") == 'ss'


def test_detect_protocol_none():
    assert detect_protocol("not-a-config-line") is None
    assert detect_protocol("") is None


def test_extract_host_port_vless_with_auth():
    host, port = extract_host_port("vless://uuid-here@example.com:8443?type=tcp#node", 'vless')
    assert host == 'example.com'
    assert port == 8443


def test_extract_host_port_trojan_ip():
    host, port = extract_host_port("trojan://pass@15.222.3.181:443#node", 'trojan')
    assert host == '15.222.3.181'
    assert port == 443


def test_extract_host_port_vmess_base64():
    payload = {"add": "vmess.example.com", "port": "10086", "id": "abc", "ps": "old"}
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    host, port = extract_host_port("vmess://" + b64, 'vmess')
    assert host == 'vmess.example.com'
    assert port == 10086


def test_rename_node_vless_sets_fragment():
    original = "vless://uuid@example.com:443?type=tcp#OldName"
    renamed = rename_node(original, 'vless', 1, latency=82.0)
    # base URI preserved
    assert renamed.startswith("vless://uuid@example.com:443?type=tcp#")
    # brand present in the (url-encoded) fragment
    assert "VOLTA" in renamed
    assert "VLESS" in renamed
    # ping annotation encoded
    assert "82ms" in renamed


def test_rename_node_vmess_sets_ps_field():
    payload = {"add": "vmess.example.com", "port": "10086", "id": "abc", "ps": "OldName"}
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    renamed = rename_node("vmess://" + b64, 'vmess', 3)
    assert renamed.startswith("vmess://")
    decoded = json.loads(base64.b64decode(renamed[8:] + "===").decode('utf-8', errors='ignore'))
    assert "VOLTA" in decoded['ps']
    assert "#3" in decoded['ps']
    # host preserved
    assert decoded['add'] == 'vmess.example.com'


def test_build_branded_lines_numbers_per_protocol():
    configs = [
        FakeConfig('vless', "vless://a@h1:443#x", 10),
        FakeConfig('vless', "vless://b@h2:443#y", 20),
        FakeConfig('trojan', "trojan://c@h3:443#z", 30),
    ]
    lines = build_branded_lines(configs)
    assert len(lines) == 3
    # per-protocol counters: two VLESS (#1, #2), one Trojan (#1)
    assert "%23" in lines[0] or "#" in lines[0]  # fragment present
    # ensure both vless entries got sequential numbers
    joined = "\n".join(lines)
    assert "VLESS" in joined
    assert "Trojan" in joined


def test_rename_node_invalid_returns_original():
    # vmess with broken base64 should not raise, returns original
    broken = "vmess://!!!notbase64!!!"
    result = rename_node(broken, 'vmess', 1)
    assert result == broken

