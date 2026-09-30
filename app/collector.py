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
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Splitted-By-Protocol/vless.txt",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Splitted-By-Protocol/ss.txt",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Splitted-By-Protocol/trojan.txt",
    "https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/Splitted-By-Protocol/hysteria2.txt",
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
MAX_WORKERS = 80
TCP_TIMEOUT = 2.0

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

DEFAULT_SEED_CONFIGS = []


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


def extract_sni_and_tls(uri, proto):
    """
    Extracts SNI/host and whether TLS/Reality handshake should be verified.
    """
    try:
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query)
        sni = qs.get('sni', [None])[0] or qs.get('host', [None])[0] or qs.get('serverName', [None])[0] or qs.get('peer', [None])[0]
        sec = (qs.get('security', [''])[0] or '').lower()
        is_tls = sec in ['tls', 'reality'] or proto in ['trojan', 'hysteria2', 'hy2']
        return sni, is_tls
    except Exception:
        return None, False


def test_tcp_connection(host, port, timeout=TCP_TIMEOUT, sni=None, is_tls=False):
    """
    Tests TCP connection and optionally TLS handshake to host:port.
    Measures latency in milliseconds.
    Filters out dead nodes, closed ports, and TLS handshake failures.
    """
    if not host or not port:
        return None

    clean_host = host.strip('[]')
    start_time = time.time()
    try:
        sock = socket.create_connection((clean_host, int(port)), timeout=timeout)
        if is_tls or (port in [443, 8443, 2053, 2083, 2087, 2096] and sni):
            server_name = sni or clean_host
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                tls_sock = ctx.wrap_socket(sock, server_hostname=server_name)
                tls_sock.close()
            except Exception:
                try:
                    sock.close()
                except Exception:
                    pass
                return None
        else:
            sock.close()

        elapsed = round((time.time() - start_time) * 1000, 2)
        if elapsed > 450.0:
            return None
        return elapsed
    except (socket.timeout, socket.error, OSError):
        return None


def rename_node_autoselect(content, protocol, latency=None, code=None):
    """
    Auto-generates the #1 Auto-Select node title for the fastest server in the feed:
        ⚡ VoltaVPN | 🚀 АВТОВЫБОР (Самый быстрый) · 🇩🇪 Германия · 24ms
    """
    flag = country_flag(code)
    cname = country_name(code)
    c_info = f" · {flag} {cname}" if cname else ""
    ping = f" · {int(latency)}ms" if latency is not None else ""
    title = f"⚡ {BRAND} | 🚀 АВТОВЫБОР (Самый быстрый){c_info}{ping}"

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
        return f"{base}#{title}"
    except Exception:
        return content


def rename_node(content, protocol, index, latency=None, code=None):
    """
    Auto-generates a branded node title for a config URI:
        🇩🇪 VoltaVPN | Германия #1 · VLESS Reality · 38ms
    """
    label = PROTOCOL_LABELS.get(protocol, protocol.upper())
    flag = country_flag(code)
    cname = country_name(code)
    ping = f" · {int(latency)}ms" if latency is not None else ""
    title = f"{flag} {BRAND} | {cname} #{index} · {label}{ping}"

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
        return f"{base}#{title}"
    except Exception:
        return content


def fetch_configs_from_source(url):
    try:
        headers = {'User-Agent': 'VOLTA-Collector/1.0'}
        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()
        return response.text.splitlines()
    except Exception as e:
        print(f"[Collector] Source fetch notice ({url}): {e}")
        return []


def _probe(entry):
    line_str, protocol, source_url = entry
    host, port = extract_host_port(line_str, protocol)
    sni, is_tls = extract_sni_and_tls(line_str, protocol)
    latency = test_tcp_connection(host, port, timeout=TCP_TIMEOUT, sni=sni, is_tls=is_tls)
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
    Seeds/updates the database with verified configs from open sources.
    """
    return collect_configs()


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
    sni, is_tls = extract_sni_and_tls(content, proto)
    latency = test_tcp_connection(host, port, timeout=TCP_TIMEOUT, sni=sni, is_tls=is_tls)
    
    # If is_working was not explicitly passed, determine from TCP handshake
    if is_working is None:
        effective_working = latency is not None
    else:
        effective_working = bool(is_working)

    code = country_code or resolve_country(host) or 'DE'
    cname = country_name(code)
    effective_latency = latency if latency is not None else (45.0 if effective_working else None)

    with current_app.app_context():
        existing = Config.query.filter_by(content=content).first()
        if existing:
            existing.protocol = proto
            existing.host = host
            existing.port = port
            existing.country_code = code
            existing.country = cname
            existing.is_working = effective_working
            if effective_latency is not None:
                existing.latency_ms = effective_latency
            existing.checked_at = datetime.utcnow()
            cfg = existing
        else:
            cfg = Config(
                protocol=proto,
                content=content,
                host=host,
                port=port,
                latency_ms=effective_latency,
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
    Strictly probes TCP + TLS connectivity so dead/unreachable nodes are not marked active.
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
            sni, is_tls = extract_sni_and_tls(line_str, proto)
            lat = test_tcp_connection(host, port, timeout=TCP_TIMEOUT, sni=sni, is_tls=is_tls) if test_connectivity else None
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


def probe_all_configs(delete_dead=True):
    """
    Re-tests TCP + TLS connectivity for all configs in DB and updates their status.
    Ensures non-responding nodes are deactivated or pruned immediately.
    """
    from flask import current_app
    with current_app.app_context():
        configs = Config.query.all()
        if not configs:
            return {'total': 0, 'working': 0, 'dead': 0}

        entries = []
        for c in configs:
            sni, is_tls = extract_sni_and_tls(c.content, c.protocol)
            entries.append((c.id, c.host, c.port, sni, is_tls))

        results = {}

        def _test_item(item):
            cid, host, port, sni, is_tls = item
            lat = test_tcp_connection(host, port, timeout=TCP_TIMEOUT, sni=sni, is_tls=is_tls)
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
            if lat is not None and lat < 450.0:
                c.latency_ms = lat
                c.is_working = True
                working_count += 1
            else:
                c.is_working = False
                c.latency_ms = None
                dead_count += 1
            c.checked_at = datetime.utcnow()

        if delete_dead and dead_count > 0:
            Config.query.filter(Config.is_working == False).delete(synchronize_session=False)

        db.session.commit()
        save_configs_to_repo()
        return {'total': len(configs), 'working': working_count, 'dead': dead_count}


test_all_configs = probe_all_configs


def delete_dead_configs():
    """
    Deletes all non-working configs from database.
    """
    from flask import current_app
    with current_app.app_context():
        deleted = Config.query.filter(Config.is_working == False).delete(synchronize_session=False)
        db.session.commit()
        save_configs_to_repo()
        return deleted


def fast_recheck_and_prune():
    """
    3-minute periodic worker:
    1. Re-tests TCP ping for every server in the pool.
    2. Immediately prunes dead / blocked configs.
    3. If working pool is low (< 10), triggers fresh collection from open sources.
    4. Regenerates subscription artifacts.
    """
    from flask import current_app
    with current_app.app_context():
        stats = probe_all_configs(delete_dead=True)
        working_count = stats.get('working', 0)
        print(f"[3-Min Recheck] Working: {working_count}, Pruned dead: {stats.get('dead', 0)}")
        if working_count < 10:
            collect_configs()
        else:
            save_configs_to_repo()


def collect_configs():
    """
    Collects candidates from remote sources, tests connectivity,
    and updates the database.
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

        # Re-check all active database configs to prune any dead servers
        probe_all_configs(delete_dead=True)

        current_working = Config.query.filter_by(is_working=True).count()
        print(f"[Collector] Finished: {new_count} new, {updated_count} updated, {current_working} working total.")
        save_configs_to_repo()
        return current_working


def get_working_configs(protocol=None, limit=25):
    """
    Returns the top verified, highest-quality working configs.
    Filters strictly by lowest latency (< 450ms), deduplicates by host/IP,
    and returns the top 15-25 best nodes for maximum reliability in Russia.
    """
    from flask import current_app
    with current_app.app_context():
        query = Config.query.filter(
            Config.is_working == True,
            Config.latency_ms.isnot(None),
            Config.latency_ms > 0,
            Config.latency_ms < 450.0,
        )
        if protocol:
            query = query.filter(Config.protocol == protocol)

        all_candidates = query.order_by(Config.latency_ms.asc(), Config.checked_at.desc()).limit(150).all()

        selected = []
        seen_hosts = {}
        country_counts = {}

        for c in all_candidates:
            host = (c.host or '').lower().strip()
            country = c.country_code or 'UN'

            # Max 2 configs per host to prevent 1 broken host spamming slots
            if seen_hosts.get(host, 0) >= 2:
                continue
            # Max 4 configs per country to maintain geographic diversity
            if country_counts.get(country, 0) >= 4:
                continue

            selected.append(c)
            seen_hosts[host] = seen_hosts.get(host, 0) + 1
            country_counts[country] = country_counts.get(country, 0) + 1

            if len(selected) >= limit:
                break

        return selected


def build_branded_lines(configs):
    """
    Auto-generate branded, renamed node lines from Config rows.
    The FIRST line in the subscription is always the Auto-Select (#1 Fastest server)
    followed by all individual nodes sorted strictly by best latency.
    """
    if not configs:
        return []

    lines = []

    # 1. First entry: Auto-Select fastest server
    best = configs[0]
    best_code = getattr(best, 'country_code', None)
    lines.append(rename_node_autoselect(best.content, best.protocol, best.latency_ms, best_code))

    # 2. Individual server lines sorted by latency
    counters = {}
    for c in configs:
        counters[c.protocol] = counters.get(c.protocol, 0) + 1
        code = getattr(c, 'country_code', None)
        lines.append(rename_node(c.content, c.protocol, counters[c.protocol], c.latency_ms, code))

    return lines


def generate_subscription_feed(is_base64=True, limit=25):
    """
    Generates dynamic, auto-branded subscription content for VPN clients
    (Happ, v2rayN, Karing, Streisand, NekoBox, Hiddify, Sing-box).
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

        active_protocols = set(c.protocol for c in working_configs)
        all_protocols = ['vless', 'vmess', 'trojan', 'ss', 'hysteria2', 'tuic']
        for proto in all_protocols:
            proto_file = os.path.join(configs_dir, f"working_{proto}.txt")
            if proto in active_protocols:
                proto_configs = [c for c in working_configs if c.protocol == proto]
                proto_lines = build_branded_lines(proto_configs)
                with open(proto_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(proto_lines))
            else:
                if os.path.exists(proto_file):
                    with open(proto_file, 'w', encoding='utf-8') as f:
                        f.write('')
    except Exception as e:
        print(f"[Collector] Save configs notice: {e}")
