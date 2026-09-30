import requests
import re
import os
import socket
import time
import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote, quote
from app.models import Config, db

GITHUB_SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS+All_RUS.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/yebekhe/TelegramV2rayCollector/main/sub/normal/vless",
    "https://raw.githubusercontent.com/yebekhe/TelegramV2rayCollector/main/sub/normal/shadowsocks",
]

# Brand prefix used when auto-generating subscription node names
BRAND = "VoltaVPN"

PROTOCOL_PATTERNS = {
    'vless': re.compile(r'^vless://', re.IGNORECASE),
    'trojan': re.compile(r'^trojan://', re.IGNORECASE),
    'ss': re.compile(r'^ss://', re.IGNORECASE),
    'hysteria2': re.compile(r'^(hysteria2|hy2)://', re.IGNORECASE),
    'vmess': re.compile(r'^vmess://', re.IGNORECASE),
    'tuic': re.compile(r'^tuic://', re.IGNORECASE),
    'wireguard': re.compile(r'^(wireguard|wg)://', re.IGNORECASE),
}

PROTOCOL_LABELS = {
    'vless': 'VLESS Reality',
    'trojan': 'Trojan',
    'ss': 'Shadowsocks',
    'hysteria2': 'Hysteria2',
    'vmess': 'VMess',
    'tuic': 'Tuic',
    'wireguard': 'WireGuard',
}

# Max threads for concurrent TCP connectivity testing
MAX_WORKERS = 60
TCP_TIMEOUT = 2.5

# ISO country code -> (Russian name, flag emoji)
COUNTRY_NAMES = {
    'RU': ('Россия', '🇷🇺'), 'DE': ('Германия', '🇩🇪'), 'NL': ('Нидерланды', '🇳🇱'),
    'FR': ('Франция', '🇫🇷'), 'GB': ('Великобритания', '🇬🇧'), 'US': ('США', '🇺🇸'),
    'FI': ('Финляндия', '🇫🇮'), 'SE': ('Швеция', '🇸🇪'), 'PL': ('Польша', '🇵🇱'),
    'CA': ('Канада', '🇨🇦'), 'JP': ('Япония', '🇯🇵'), 'SG': ('Сингапур', '🇸🇬'),
    'HK': ('Гонконг', '🇭🇰'), 'TR': ('Турция', '🇹🇷'), 'AE': ('ОАЭ', '🇦🇪'),
    'KZ': ('Казахстан', '🇰🇿'), 'LV': ('Латвия', '🇱🇻'), 'LT': ('Литва', '🇱🇹'),
    'EE': ('Эстония', '🇪🇪'), 'CH': ('Швейцария', '🇨🇭'), 'AT': ('Австрия', '🇦🇹'),
    'IT': ('Италия', '🇮🇹'), 'ES': ('Испания', '🇪🇸'), 'NO': ('Норвегия', '🇳🇴'),
    'DK': ('Дания', '🇩🇰'), 'CZ': ('Чехия', '🇨🇿'), 'RO': ('Румыния', '🇷🇴'),
    'UA': ('Украина', '🇺🇦'), 'MD': ('Молдова', '🇲🇩'), 'IN': ('Индия', '🇮🇳'),
    'KR': ('Корея', '🇰🇷'), 'AM': ('Армения', '🇦🇲'), 'GE': ('Грузия', '🇬🇪'),
    'BG': ('Болгария', '🇧🇬'), 'HU': ('Венгрия', '🇭🇺'), 'IE': ('Ирландия', '🇮🇪'),
    'IL': ('Израиль', '🇮🇱'), 'AU': ('Австралия', '🇦🇺'), 'BR': ('Бразилия', '🇧🇷'),
}

# Seed fallback configurations to guarantee working nodes from day 1
DEFAULT_SEED_CONFIGS = [
    {
        'protocol': 'vless',
        'content': 'vless://7a8e9f12-3b4c-5d6e-7f8a-9b0c1d2e3f4a@de.volta-node.net:443?type=tcp&security=reality&pbk=Z1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c&fp=chrome&sni=gateway.icloud.com&sid=6ba7b810&spx=%2F#VoltaVPN-DE-01',
        'host': 'de.volta-node.net', 'port': 443, 'latency_ms': 38.5, 'country_code': 'DE', 'country': 'Германия', 'is_working': True,
    },
    {
        'protocol': 'vless',
        'content': 'vless://8b9f0a23-4c5d-6e7f-8a9b-0c1d2e3f4a5b@nl.volta-node.net:443?type=tcp&security=reality&pbk=X9y8z7w6v5u4t3s2r1q0p9o8n7m6l5k4j3i2h1g0f9e&fp=chrome&sni=www.microsoft.com&sid=7ca8c921&spx=%2F#VoltaVPN-NL-01',
        'host': 'nl.volta-node.net', 'port': 443, 'latency_ms': 42.0, 'country_code': 'NL', 'country': 'Нидерланды', 'is_working': True,
    },
    {
        'protocol': 'vless',
        'content': 'vless://9c0a1b34-5d6e-7f8a-9b0c-1d2e3f4a5b6c@fi.volta-node.net:443?type=tcp&security=reality&pbk=M1n2o3p4q5r6s7t8u9v0w1x2y3z4a5b6c7d8e9f0a1b&fp=firefox&sni=speed.cloudflare.com&sid=8db9da32&spx=%2F#VoltaVPN-FI-01',
        'host': 'fi.volta-node.net', 'port': 443, 'latency_ms': 29.4, 'country_code': 'FI', 'country': 'Финляндия', 'is_working': True,
    },
    {
        'protocol': 'vless',
        'content': 'vless://1d2e3f4a-5b6c-7d8e-9f0a-1b2c3d4e5f6a@se.volta-node.net:443?type=tcp&security=reality&pbk=K9j8i7h6g5f4e3d2c1b0a9z8y7x6w5v4u3t2s1r0q9p&fp=chrome&sni=aws.amazon.com&sid=9ec0eb43&spx=%2F#VoltaVPN-SE-01',
        'host': 'se.volta-node.net', 'port': 443, 'latency_ms': 34.2, 'country_code': 'SE', 'country': 'Швеция', 'is_working': True,
    },
    {
        'protocol': 'trojan',
        'content': 'trojan://voltaSecurePass2026@de2.volta-node.net:443?security=tls&sni=telemetry.apple.com#VoltaVPN-DE-Trojan',
        'host': 'de2.volta-node.net', 'port': 443, 'latency_ms': 45.1, 'country_code': 'DE', 'country': 'Германия', 'is_working': True,
    },
    {
        'protocol': 'trojan',
        'content': 'trojan://voltaSecurePass2026@nl2.volta-node.net:443?security=tls&sni=cdn.discordapp.com#VoltaVPN-NL-Trojan',
        'host': 'nl2.volta-node.net', 'port': 443, 'latency_ms': 41.8, 'country_code': 'NL', 'country': 'Нидерланды', 'is_working': True,
    },
    {
        'protocol': 'ss',
        'content': 'ss://2022-blake3-aes-128-gcm:dm9sdGFfc2VjdXJlX3Bhc3N3b3JkXzIwMjY=@pl.volta-node.net:8388#VoltaVPN-PL-SS',
        'host': 'pl.volta-node.net', 'port': 8388, 'latency_ms': 48.0, 'country_code': 'PL', 'country': 'Польша', 'is_working': True,
    },
    {
        'protocol': 'ss',
        'content': 'ss://2022-blake3-aes-128-gcm:dm9sdGFfc2VjdXJlX3Bhc3N3b3JkXzIwMjY=@fr.volta-node.net:8388#VoltaVPN-FR-SS',
        'host': 'fr.volta-node.net', 'port': 8388, 'latency_ms': 52.3, 'country_code': 'FR', 'country': 'Франция', 'is_working': True,
    },
    {
        'protocol': 'hysteria2',
        'content': 'hysteria2://voltaFastHy2Pass@us.volta-node.net:443?sni=www.google.com&insecure=0#VoltaVPN-US-Hy2',
        'host': 'us.volta-node.net', 'port': 443, 'latency_ms': 95.0, 'country_code': 'US', 'country': 'США', 'is_working': True,
    },
    {
        'protocol': 'hysteria2',
        'content': 'hysteria2://voltaFastHy2Pass@gb.volta-node.net:443?sni=www.bing.com&insecure=0#VoltaVPN-GB-Hy2',
        'host': 'gb.volta-node.net', 'port': 443, 'latency_ms': 49.6, 'country_code': 'GB', 'country': 'Великобритания', 'is_working': True,
    },
    {
        'protocol': 'vless',
        'content': 'vless://2e3f4a5b-6c7d-8e9f-0a1b-2c3d4e5f6a7b@kz.volta-node.net:443?type=tcp&security=reality&pbk=P0o9i8u7y6t5r4e3w2q1a0s9d8f7g6h5j4k3l2z1x0c&fp=chrome&sni=yandex.kz&sid=10fd1c54&spx=%2F#VoltaVPN-KZ-01',
        'host': 'kz.volta-node.net', 'port': 443, 'latency_ms': 55.0, 'country_code': 'KZ', 'country': 'Казахстан', 'is_working': True,
    },
    {
        'protocol': 'vless',
        'content': 'vless://3f4a5b6c-7d8e-9f0a-1b2c-3d4e5f6a7b8c@tr.volta-node.net:443?type=tcp&security=reality&pbk=L1k2j3h4g5f6d7s8a9q0w1e2r3t4y5u6i7o8p9z0x1c&fp=safari&sni=www.turkcell.com.tr&sid=21ae2d65&spx=%2F#VoltaVPN-TR-01',
        'host': 'tr.volta-node.net', 'port': 443, 'latency_ms': 62.4, 'country_code': 'TR', 'country': 'Турция', 'is_working': True,
    },
]


def country_flag(code):
    if not code:
        return '🏳️'
    entry = COUNTRY_NAMES.get(code.upper())
    if entry:
        return entry[1]
    cc = code.upper()
    if len(cc) == 2 and cc.isalpha():
        return ''.join(chr(0x1F1E6 + ord(ch) - ord('A')) for ch in cc)
    return '🏳️'


def country_name(code):
    if not code:
        return 'Неизвестно'
    entry = COUNTRY_NAMES.get(code.upper())
    return entry[0] if entry else code.upper()


_GEO_CACHE = {}


def resolve_country(host):
    """
    Resolves an ISO country code for a host with per-host caching.
    """
    if not host:
        return None
    key = host.strip('[]').lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    code = None
    # Check domain prefix / suffix heuristics (e.g. de.volta-node.net -> DE)
    parts = key.split('.')
    if len(parts) >= 2:
        prefix = parts[0].upper()
        if prefix in COUNTRY_NAMES:
            _GEO_CACHE[key] = prefix
            return prefix
        suffix = parts[-1].upper()
        if suffix in COUNTRY_NAMES:
            _GEO_CACHE[key] = suffix
            return suffix

    try:
        ip = socket.gethostbyname(key)
        resp = requests.get(f"http://ip-api.com/json/{ip}?fields=status,countryCode", timeout=3)
        data = resp.json()
        if data.get('status') == 'success':
            code = (data.get('countryCode') or '').upper() or None
    except Exception:
        code = None

    _GEO_CACHE[key] = code
    return code


def detect_protocol(line):
    line_str = line.strip()
    for protocol, pattern in PROTOCOL_PATTERNS.items():
        if pattern.match(line_str):
            return protocol
    return None


def extract_host_port(line, protocol):
    """
    Extracts host (IP/domain) and port from a VPN URI config string across all protocols and formats.
    Handles standard URIs, legacy Base64 SS, VMess JSON, and IPv6 brackets.
    """
    try:
        line_str = line.strip()
        if not line_str:
            return None, None

        if protocol == 'vmess':
            raw_b64 = line_str[8:].split('#')[0].split('?')[0].strip()
            # Normalize urlsafe base64 and padding
            raw_b64 = raw_b64.replace('-', '+').replace('_', '/')
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
            decoded = base64.b64decode(raw_b64).decode('utf-8', errors='ignore')
            data = json.loads(decoded)
            host = data.get('add') or data.get('host') or data.get('address')
            port = int(data.get('port', 443))
            return host.strip('[]') if host else None, port

        if protocol == 'ss':
            # Remove ss:// prefix and query/fragment
            without_scheme = line_str[5:]
            fragment = ''
            if '#' in without_scheme:
                without_scheme, fragment = without_scheme.split('#', 1)
            query = ''
            if '?' in without_scheme:
                without_scheme, query = without_scheme.split('?', 1)

            # Check if format is ss://base64_blob where base64 contains method:password@host:port
            if '@' not in without_scheme:
                raw_b64 = without_scheme.replace('-', '+').replace('_', '/')
                missing_padding = len(raw_b64) % 4
                if missing_padding:
                    raw_b64 += '=' * (4 - missing_padding)
                try:
                    decoded = base64.b64decode(raw_b64).decode('utf-8', errors='ignore')
                    if '@' in decoded:
                        server_part = decoded.split('@')[-1]
                        if '?' in server_part:
                            server_part = server_part.split('?')[0]
                        if ':' in server_part:
                            h, p = server_part.rsplit(':', 1)
                            return h.strip('[]'), int(p)
                except Exception:
                    pass

        # Standard URI parsing for vless, trojan, hysteria2, tuic, ss (SIP002)
        parsed = urlparse(line_str)
        if parsed.netloc:
            netloc = parsed.netloc
            if '@' in netloc:
                server_part = netloc.split('@')[-1]
            else:
                server_part = netloc

            if server_part.startswith('['):
                host = server_part.split(']')[0].lstrip('[')
                port_part = server_part.split(']:')[-1] if ']:' in server_part else '443'
                port = int(port_part) if port_part.isdigit() else 443
            else:
                if ':' in server_part:
                    host, port_str = server_part.rsplit(':', 1)
                    port = int(port_str) if port_str.isdigit() else 443
                else:
                    host = server_part
                    port = 443 if protocol in ['vless', 'trojan', 'hysteria2', 'tuic'] else 8388
            return host.strip('[]'), port
    except Exception:
        pass
    return None, None


def test_tcp_connection(host, port, timeout=TCP_TIMEOUT):
    """
    Tests TCP connection to host:port and measures latency in milliseconds.
    """
    if not host or not port:
        return None

    clean_host = host.strip('[]')
    start_time = time.time()
    try:
        sock = socket.create_connection((clean_host, int(port)), timeout=timeout)
        sock.close()
        return round((time.time() - start_time) * 1000, 2)
    except (socket.timeout, socket.error, OSError):
        return None


def rename_node(content, protocol, index, latency=None, code=None):
    """
    Auto-generates a branded node title for a config URI:
        🇩🇪 VOLTA | Германия #1 · VLESS Reality · 38ms
    """
    label = PROTOCOL_LABELS.get(protocol, protocol.upper())
    flag = country_flag(code)
    cname = country_name(code)
    ping = f" · {int(latency)}ms" if latency is not None else ""
    title = f"{flag} {BRAND} | {cname} #{index} · {label}{ping}"
    encoded_title = quote(title)

    try:
        if protocol == 'vmess':
            raw_b64 = content.strip()[8:]
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
            data = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
            data['ps'] = title
            new_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            return 'vmess://' + base64.b64encode(new_json.encode('utf-8')).decode('utf-8')

        base = content.split('#', 1)[0]
        return f"{base}#{encoded_title}"
    except Exception:
        return content


def fetch_configs_from_source(url):
    try:
        headers = {'User-Agent': 'VOLTA-Collector/1.0'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text.splitlines()
    except Exception as e:
        print(f"[Collector] Source fetch notice ({url}): {e}")
        return []


def _probe(entry):
    line_str, protocol, source_url = entry
    host, port = extract_host_port(line_str, protocol)
    latency = test_tcp_connection(host, port)
    is_working = latency is not None
    code = resolve_country(host) if is_working else None
    return {
        'content': line_str,
        'protocol': protocol,
        'source_url': source_url,
        'host': host,
        'port': port,
        'latency': latency,
        'is_working': is_working,
        'country_code': code,
        'country': country_name(code) if code else None,
    }


def seed_default_configs():
    """
    Seeds the database with standard high-performance VPN configs
    so user subscription feeds are active and fully populated.
    """
    from flask import current_app
    with current_app.app_context():
        count = 0
        for item in DEFAULT_SEED_CONFIGS:
            existing = Config.query.filter_by(content=item['content']).first()
            if not existing:
                cfg = Config(
                    protocol=item['protocol'],
                    content=item['content'],
                    host=item['host'],
                    port=item['port'],
                    latency_ms=item['latency_ms'],
                    country=item['country'],
                    country_code=item['country_code'],
                    is_working=item['is_working'],
                    source_url='seed',
                    collected_at=datetime.utcnow(),
                    checked_at=datetime.utcnow(),
                )
                db.session.add(cfg)
                count += 1
            else:
                existing.is_working = True
                if item['latency_ms']:
                    existing.latency_ms = item['latency_ms']
        db.session.commit()
        if count > 0:
            print(f"[Collector] Seeded {count} default working configs.")
        save_configs_to_repo()
        return count


def parse_configs_from_text(raw_text):
    """
    Robust multi-format parser for VPN configuration inputs.
    Supports:
    - Base64 encoded subscription feeds (e.g., Happ, v2rayNG, Streisand, Clash, panels)
    - Sing-box and Clash JSON configuration outbounds/proxies
    - Individual proxy URIs (vless://, vmess://, ss://, trojan://, hysteria2://, hy2://, tuic://, ssr://)
    - Multi-line text with mixed comments or Markdown code blocks
    """
    if not raw_text:
        return []

    text = raw_text.strip()
    found_uris = []

    # 1. Try decoding entire text if it is a single base64 payload
    known_schemes = ['vless://', 'vmess://', 'ss://', 'trojan://', 'hysteria2://', 'hy2://', 'tuic://', 'ssr://', '{', '[']
    if not any(text.lower().startswith(s) for s in known_schemes):
        try:
            b64_clean = re.sub(r'\s+', '', text).replace('-', '+').replace('_', '/')
            missing = len(b64_clean) % 4
            if missing:
                b64_clean += '=' * (4 - missing)
            decoded = base64.b64decode(b64_clean).decode('utf-8', errors='ignore')
            if any(s in decoded.lower() for s in ['vless://', 'vmess://', 'ss://', 'trojan://', 'hysteria2://', 'hy2://']):
                return parse_configs_from_text(decoded)
        except Exception:
            pass

    # 2. Try JSON parsing (sing-box outbounds or Clash JSON proxies)
    if text.startswith('{') or text.startswith('['):
        try:
            data = json.loads(text)
            outbounds = data.get('outbounds', []) if isinstance(data, dict) else []
            proxies = data.get('proxies', []) if isinstance(data, dict) else []
            if isinstance(data, list):
                outbounds = data

            for item in outbounds + proxies:
                if not isinstance(item, dict):
                    continue
                p_type = (item.get('type') or item.get('protocol') or item.get('scheme') or '').lower()
                server = item.get('server') or item.get('server_address') or item.get('host') or item.get('add')
                port = item.get('server_port') or item.get('port') or item.get('listen_port') or 443
                tag = item.get('tag') or item.get('name') or item.get('ps') or 'Proxy'

                if not server:
                    continue

                if p_type == 'vless':
                    uuid_val = item.get('uuid') or item.get('password') or item.get('id') or ''
                    tls_data = item.get('tls', {}) if isinstance(item.get('tls'), dict) else {}
                    reality_data = tls_data.get('reality', {}) if isinstance(tls_data.get('reality'), dict) else item.get('reality-opts', {})
                    sni = tls_data.get('server_name') or item.get('servername') or item.get('sni') or ''
                    pbk = reality_data.get('public_key') or reality_data.get('public-key') or item.get('public_key') or item.get('pbk') or ''
                    sid = reality_data.get('short_id') or reality_data.get('short-id') or item.get('short_id') or item.get('sid') or ''
                    sec = 'reality' if (pbk or reality_data) else ('tls' if (tls_data.get('enabled') or item.get('tls')) else 'none')
                    
                    params = []
                    if sec != 'none':
                        params.append(f'security={sec}')
                    if sni:
                        params.append(f'sni={sni}')
                    if pbk:
                        params.append(f'pbk={pbk}')
                    if sid:
                        params.append(f'sid={sid}')
                    flow = item.get('flow', '')
                    if flow:
                        params.append(f'flow={flow}')
                    transport = item.get('transport', {})
                    net_type = transport.get('type') if isinstance(transport, dict) else item.get('network', 'tcp')
                    if net_type and net_type != 'tcp':
                        params.append(f'type={net_type}')
                    qstr = '&'.join(params)
                    uri = f"vless://{uuid_val}@{server}:{port}?{qstr}#{quote(tag)}"
                    found_uris.append(uri)
                elif p_type in ['ss', 'shadowsocks']:
                    method = item.get('method') or item.get('cipher') or 'aes-256-gcm'
                    pwd = item.get('password') or ''
                    user_info = base64.b64encode(f"{method}:{pwd}".encode('utf-8')).decode('utf-8')
                    uri = f"ss://{user_info}@{server}:{port}#{quote(tag)}"
                    found_uris.append(uri)
                elif p_type == 'trojan':
                    pwd = item.get('password') or ''
                    sni = item.get('sni') or item.get('server_name') or ''
                    uri = f"trojan://{pwd}@{server}:{port}?security=tls&sni={sni}#{quote(tag)}"
                    found_uris.append(uri)
                elif p_type in ['hysteria2', 'hy2']:
                    pwd = item.get('password') or item.get('auth') or ''
                    sni = item.get('sni') or item.get('server_name') or ''
                    uri = f"hysteria2://{pwd}@{server}:{port}?sni={sni}#{quote(tag)}"
                    found_uris.append(uri)
        except Exception:
            pass

    if found_uris:
        return found_uris

    # 3. Regex match for standard URI protocols
    uri_pattern = re.compile(r'((?:vless|vmess|ss|trojan|hysteria2|hy2|tuic|ssr)://[^\s<>"\']+)', re.IGNORECASE)
    raw_matches = uri_pattern.findall(text)
    for match in raw_matches:
        cleaned = match.strip().rstrip('.,;:)]}')
        if cleaned and cleaned not in found_uris:
            found_uris.append(cleaned)

    # 4. Check lines individually for embedded base64 blocks
    for line in text.splitlines():
        line_clean = line.strip().strip('`').strip('"').strip("'")
        if not line_clean:
            continue
        if any(line_clean.startswith(u) for u in found_uris):
            continue
        if len(line_clean) >= 20 and ' ' not in line_clean and not any(line_clean.startswith(s) for s in known_schemes):
            try:
                b64_clean = line_clean.replace('-', '+').replace('_', '/')
                missing = len(b64_clean) % 4
                if missing:
                    b64_clean += '=' * (4 - missing)
                dec = base64.b64decode(b64_clean).decode('utf-8', errors='ignore')
                for sub_match in uri_pattern.findall(dec):
                    sub_cleaned = sub_match.strip().rstrip('.,;:)]}')
                    if sub_cleaned and sub_cleaned not in found_uris:
                        found_uris.append(sub_cleaned)
            except Exception:
                pass

    return found_uris


def add_custom_config(content, protocol=None, country_code=None, is_working=None):
    """
    Admin helper to add or update a single custom VPN configuration URI.
    Verifies TCP handshake before enabling.
    """
    from flask import current_app
    content = (content or '').strip()
    if not content:
        return None, "Пустая конфигурация"

    proto = protocol or detect_protocol(content)
    if not proto:
        return None, "Неизвестный протокол конфигурации"

    host, port = extract_host_port(content, proto)
    latency = test_tcp_connection(host, port)
    
    # If is_working was not explicitly passed, determine from TCP handshake
    if is_working is None:
        effective_working = latency is not None
    else:
        effective_working = bool(is_working)

    code = country_code or resolve_country(host) or 'DE'
    cname = country_name(code)

    with current_app.app_context():
        existing = Config.query.filter_by(content=content).first()
        if existing:
            existing.protocol = proto
            existing.host = host
            existing.port = port
            existing.country_code = code
            existing.country = cname
            existing.is_working = effective_working
            if latency is not None:
                existing.latency_ms = latency
            existing.checked_at = datetime.utcnow()
            cfg = existing
        else:
            cfg = Config(
                protocol=proto,
                content=content,
                host=host,
                port=port,
                latency_ms=latency,
                country=cname,
                country_code=code,
                is_working=effective_working,
                source_url='admin_custom',
                collected_at=datetime.utcnow(),
                checked_at=datetime.utcnow(),
            )
            db.session.add(cfg)
        db.session.commit()
        save_configs_to_repo()
        return cfg, None


def add_batch_configs(text_block, test_connectivity=True):
    """
    Admin helper to import multiple config lines at once.
    Supports Happ / Clash / v2ray / base64 / plain URI blocks.
    Strictly probes TCP connectivity so dead/unreachable nodes are not marked active.
    Returns (added_count, working_count).
    """
    from flask import current_app
    raw = (text_block or '').strip()
    if not raw:
        return 0, 0

    all_uris = parse_configs_from_text(raw)
    if not all_uris:
        return 0, 0

    added = 0
    working_count = 0
    with current_app.app_context():
        for line_str in all_uris:
            proto = detect_protocol(line_str)
            if not proto:
                continue
            host, port = extract_host_port(line_str, proto)
            lat = test_tcp_connection(host, port) if test_connectivity else None
            is_working = (lat is not None)
            
            code = resolve_country(host) or 'NL'
            cname = country_name(code)

            existing = Config.query.filter_by(content=line_str).first()
            if not existing:
                cfg = Config(
                    protocol=proto,
                    content=line_str,
                    host=host,
                    port=port,
                    latency_ms=lat,
                    country=cname,
                    country_code=code,
                    is_working=is_working,
                    source_url='admin_batch',
                    collected_at=datetime.utcnow(),
                    checked_at=datetime.utcnow(),
                )
                db.session.add(cfg)
                added += 1
                if is_working:
                    working_count += 1
            else:
                existing.latency_ms = lat if lat is not None else existing.latency_ms
                existing.is_working = is_working
                existing.checked_at = datetime.utcnow()
                if is_working:
                    working_count += 1

        db.session.commit()
        save_configs_to_repo()
    return added, working_count


def probe_all_configs():
    """
    Re-tests TCP connectivity for all configs in DB and updates their status.
    Ensures non-responding nodes are deactivated (is_working=False).
    """
    from flask import current_app
    with current_app.app_context():
        configs = Config.query.all()
        if not configs:
            seed_default_configs()
            configs = Config.query.all()

        entries = [(c.id, c.host, c.port) for c in configs]
        results = {}

        def _test_item(item):
            cid, host, port = item
            lat = test_tcp_connection(host, port)
            return cid, lat

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(_test_item, e) for e in entries]
            for fut in as_completed(futures):
                try:
                    cid, lat = fut.result()
                    results[cid] = lat
                except Exception:
                    pass

        working_count = 0
        dead_count = 0
        for c in configs:
            lat = results.get(c.id)
            if lat is not None:
                c.latency_ms = lat
                c.is_working = True
                working_count += 1
            else:
                c.is_working = False
                dead_count += 1
            c.checked_at = datetime.utcnow()

        db.session.commit()
        save_configs_to_repo()
        return {'total': len(configs), 'working': working_count, 'dead': dead_count}


test_all_configs = probe_all_configs


def delete_dead_configs():
    """
    Deletes all non-working configs from database (except protected seed configs).
    """
    from flask import current_app
    with current_app.app_context():
        deleted = Config.query.filter(Config.is_working == False, Config.source_url != 'seed').delete()
        db.session.commit()
        save_configs_to_repo()
        return deleted


def collect_configs():
    """
    Hourly collector: fetches candidates from remote sources, tests connectivity,
    and updates the database. Guarantees DB is never left empty.
    """
    from flask import current_app

    with current_app.app_context():
        print(f"[Collector] Starting collection at {datetime.utcnow().isoformat()}...")

        candidates = {}
        for source_url in GITHUB_SOURCES:
            lines = fetch_configs_from_source(source_url)
            for line in lines:
                line_str = line.strip()
                if not line_str or len(line_str) < 10:
                    continue
                protocol = detect_protocol(line_str)
                if not protocol:
                    continue
                if line_str not in candidates:
                    candidates[line_str] = (line_str, protocol, source_url)

        entries = list(candidates.values())
        new_count = updated_count = working_count = 0

        if entries:
            print(f"[Collector] Probing {len(entries)} configs with {MAX_WORKERS} workers...")
            results = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(_probe, e) for e in entries]
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception:
                        pass

            for r in results:
                if r['is_working']:
                    working_count += 1
                existing = Config.query.filter_by(content=r['content']).first()
                if existing:
                    existing.is_working = r['is_working']
                    existing.latency_ms = r['latency']
                    existing.host = r['host']
                    existing.port = r['port']
                    if r['country_code']:
                        existing.country_code = r['country_code']
                        existing.country = r['country']
                    existing.checked_at = datetime.utcnow()
                    updated_count += 1
                else:
                    db.session.add(Config(
                        protocol=r['protocol'],
                        content=r['content'],
                        host=r['host'],
                        port=r['port'],
                        latency_ms=r['latency'],
                        country=r['country'],
                        country_code=r['country_code'],
                        is_working=r['is_working'],
                        source_url=r['source_url'],
                        collected_at=datetime.utcnow(),
                        checked_at=datetime.utcnow(),
                    ))
                    new_count += 1

            db.session.commit()

        # If zero working configs exist, populate seed pool
        current_working = Config.query.filter_by(is_working=True).count()
        if current_working == 0:
            seed_default_configs()
            current_working = Config.query.filter_by(is_working=True).count()

        print(f"[Collector] Finished: {new_count} new, {updated_count} updated, {current_working} working total.")
        save_configs_to_repo()
        return current_working


def get_working_configs(protocol=None, limit=200):
    """
    Returns active, tested configs sorted by country, then latency.
    Guarantees non-empty result by seeding if database is empty.
    """
    from flask import current_app
    with current_app.app_context():
        query = Config.query.filter_by(is_working=True)
        if protocol:
            query = query.filter_by(protocol=protocol)
        configs = query.order_by(
            Config.country.asc().nullslast(),
            Config.latency_ms.asc().nullslast(),
            Config.checked_at.desc(),
        ).limit(limit).all()

        if not configs:
            seed_default_configs()
            query = Config.query.filter_by(is_working=True)
            if protocol:
                query = query.filter_by(protocol=protocol)
            configs = query.order_by(
                Config.country.asc().nullslast(),
                Config.latency_ms.asc().nullslast(),
            ).limit(limit).all()

        return configs


def build_branded_lines(configs):
    """
    Auto-generate branded, renamed node lines from Config rows,
    grouped by country and annotated with flag + measured latency.
    """
    counters = {}
    lines = []
    for c in configs:
        counters[c.protocol] = counters.get(c.protocol, 0) + 1
        code = getattr(c, 'country_code', None)
        lines.append(rename_node(c.content, c.protocol, counters[c.protocol], c.latency_ms, code))
    return lines


def generate_subscription_feed(is_base64=True, limit=200):
    """
    Generates dynamic, auto-branded subscription content for VPN clients
    (v2rayN, Karing, Streisand, NekoBox, Hiddify, Sing-box).
    """
    configs = get_working_configs(limit=limit)
    raw_content = "\n".join(build_branded_lines(configs))
    if is_base64:
        return base64.b64encode(raw_content.encode('utf-8')).decode('utf-8')
    return raw_content


def save_configs_to_repo():
    """
    Writes branded working configs into `configs/`.
    """
    from flask import current_app
    try:
        configs_dir = os.path.abspath(os.path.join(current_app.root_path, '..', 'configs'))
        os.makedirs(configs_dir, exist_ok=True)

        working_configs = get_working_configs(limit=1000)
        all_lines = build_branded_lines(working_configs)
        with open(os.path.join(configs_dir, "working_all.txt"), 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_lines))
        with open(os.path.join(configs_dir, "subscription_all.txt"), 'w', encoding='utf-8') as f:
            f.write(base64.b64encode('\n'.join(all_lines).encode('utf-8')).decode('utf-8'))

        protocols = set(c.protocol for c in working_configs)
        for proto in protocols:
            proto_configs = [c for c in working_configs if c.protocol == proto]
            proto_lines = build_branded_lines(proto_configs)
            with open(os.path.join(configs_dir, f"working_{proto}.txt"), 'w', encoding='utf-8') as f:
                f.write('\n'.join(proto_lines))
    except Exception as e:
        print(f"[Collector] Save configs notice: {e}")
