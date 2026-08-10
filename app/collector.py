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
]

# Brand prefix used when auto-generating subscription node names
BRAND = "VOLTA"

PROTOCOL_PATTERNS = {
    'vless': re.compile(r'^vless://', re.IGNORECASE),
    'trojan': re.compile(r'^trojan://', re.IGNORECASE),
    'ss': re.compile(r'^ss://', re.IGNORECASE),
    'hysteria2': re.compile(r'^(hysteria2|hy2)://', re.IGNORECASE),
    'vmess': re.compile(r'^vmess://', re.IGNORECASE),
    'tuic': re.compile(r'^tuic://', re.IGNORECASE),
}

PROTOCOL_LABELS = {
    'vless': 'VLESS',
    'trojan': 'Trojan',
    'ss': 'Shadowsocks',
    'hysteria2': 'Hysteria2',
    'vmess': 'VMess',
    'tuic': 'Tuic',
}

# Max threads for concurrent TCP connectivity testing
MAX_WORKERS = 60
TCP_TIMEOUT = 2.5

# ISO country code -> (Russian name, flag emoji). Covers the common VPN locations.
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


def country_flag(code):
    if not code:
        return '🏳️'
    entry = COUNTRY_NAMES.get(code.upper())
    if entry:
        return entry[1]
    # Derive regional-indicator flag from any 2-letter code
    cc = code.upper()
    if len(cc) == 2 and cc.isalpha():
        return ''.join(chr(0x1F1E6 + ord(ch) - ord('A')) for ch in cc)
    return '🏳️'


def country_name(code):
    if not code:
        return 'Неизвестно'
    entry = COUNTRY_NAMES.get(code.upper())
    return entry[0] if entry else code.upper()


# In-process cache: host -> country_code, to avoid repeated GeoIP lookups
_GEO_CACHE = {}


def resolve_country(host):
    """
    Resolves an ISO country code for a host (IP or domain) using a free GeoIP
    endpoint, with per-host caching. Returns an uppercase 2-letter code or None.
    Network failures degrade gracefully to None.
    """
    if not host:
        return None
    key = host.strip('[]')
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    code = None
    try:
        # Resolve domain to IP first (ip-api accepts domains too, but IP is cheaper)
        ip = socket.gethostbyname(key)
        resp = requests.get(f"http://ip-api.com/json/{ip}?fields=status,countryCode", timeout=5)
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
    Extracts host (IP/domain) and port from a VPN URI config string.
    """
    try:
        line_str = line.strip()
        if protocol == 'vmess':
            raw_b64 = line_str[8:]
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
            decoded = base64.b64decode(raw_b64).decode('utf-8', errors='ignore')
            data = json.loads(decoded)
            host = data.get('add') or data.get('host')
            port = int(data.get('port', 443))
            return host, port

        parsed = urlparse(line_str)
        if parsed.netloc:
            netloc = parsed.netloc
            if '@' in netloc:
                server_part = netloc.split('@')[-1]
            else:
                server_part = netloc

            if server_part.startswith('['):
                host = server_part.split(']')[0] + ']'
                port_part = server_part.split(']:')[-1] if ']:' in server_part else '443'
                port = int(port_part) if port_part.isdigit() else 443
            else:
                if ':' in server_part:
                    host, port_str = server_part.split(':')[:2]
                    port = int(port_str) if port_str.isdigit() else 443
                else:
                    host = server_part
                    port = 443 if protocol in ['vless', 'trojan', 'hysteria2'] else 80
            return host, port
    except Exception:
        pass
    return None, None


def test_tcp_connection(host, port, timeout=TCP_TIMEOUT):
    """
    Tests TCP connection to host:port and measures latency in milliseconds.
    Returns latency_ms (float) if reachable, None if connection failed.
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
    Auto-generates a branded node title for a config URI.
    Rewrites the fragment (#name) to something like:
        🇩🇪 VOLTA | Германия · VLESS · 82ms
    This is what makes it an auto-GENERATED subscription rather than a raw copy.
    """
    label = PROTOCOL_LABELS.get(protocol, protocol.upper())
    flag = country_flag(code)
    cname = country_name(code)
    ping = f" · {int(latency)}ms" if latency is not None else ""
    title = f"{flag} {BRAND} | {cname} · {label}{ping}"
    encoded_title = quote(title)

    try:
        if protocol == 'vmess':
            # vmess stores name in the "ps" field of the base64 JSON payload
            raw_b64 = content.strip()[8:]
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
            data = json.loads(base64.b64decode(raw_b64).decode('utf-8', errors='ignore'))
            data['ps'] = title
            new_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            return 'vmess://' + base64.b64encode(new_json.encode('utf-8')).decode('utf-8')

        # For URI-style protocols the name is the #fragment
        base = content.split('#', 1)[0]
        return f"{base}#{encoded_title}"
    except Exception:
        return content


def fetch_configs_from_source(url):
    try:
        headers = {'User-Agent': 'VOLTA-Collector/1.0'}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text.splitlines()
    except Exception as e:
        print(f"[Collector] Error fetching {url}: {e}")
        return []


def _probe(entry):
    """Worker: probe a single (line, protocol, source_url) tuple concurrently."""
    line_str, protocol, source_url = entry
    host, port = extract_host_port(line_str, protocol)
    latency = test_tcp_connection(host, port)
    is_working = latency is not None
    # Only geo-locate reachable hosts (saves lookups on dead nodes)
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


def collect_configs():
    """
    Hourly scheduler task:
    1. Fetches configs from GitHub sources.
    2. Tests TCP connectivity CONCURRENTLY (fast).
    3. Saves/updates database records.
    4. Synchronizes branded subscription files into configs/ and attempts git push.
    """
    from flask import current_app

    with current_app.app_context():
        print(f"[Collector] Starting collection at {datetime.utcnow().isoformat()}...")

        # 1. Gather unique candidate lines across all sources
        candidates = {}
        for source_url in GITHUB_SOURCES:
            lines = fetch_configs_from_source(source_url)
            print(f"[Collector] {source_url} -> {len(lines)} lines")
            for line in lines:
                line_str = line.strip()
                if not line_str or len(line_str) < 10:
                    continue
                protocol = detect_protocol(line_str)
                if not protocol:
                    continue
                # keep first source that provided it
                if line_str not in candidates:
                    candidates[line_str] = (line_str, protocol, source_url)

        entries = list(candidates.values())
        print(f"[Collector] Probing {len(entries)} unique configs with {MAX_WORKERS} workers...")

        # 2. Concurrent connectivity testing
        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(_probe, e) for e in entries]
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception:
                    pass

        # 3. Persist results
        new_count = updated_count = working_count = 0
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
        print(f"[Collector] Done: {new_count} new, {updated_count} updated, {working_count} working.")

        save_configs_to_repo()
        return working_count


def get_working_configs(protocol=None, limit=200):
    """Returns active, tested configs sorted by country, then latency (fastest first)."""
    from flask import current_app
    with current_app.app_context():
        query = Config.query.filter_by(is_working=True)
        if protocol:
            query = query.filter_by(protocol=protocol)
        return query.order_by(
            Config.country.asc().nullslast(),
            Config.latency_ms.asc().nullslast(),
            Config.checked_at.desc(),
        ).limit(limit).all()


def build_branded_lines(configs):
    """
    Auto-generate branded, renamed node lines from Config rows,
    grouped by country and annotated with country flag + measured latency.
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
    (v2rayN, Karing, Streisand, NekoBox, Hiddify).
    Returns Base64 encoded string or raw line-separated text.
    """
    configs = get_working_configs(limit=limit)
    raw_content = "\n".join(build_branded_lines(configs))
    if is_base64:
        return base64.b64encode(raw_content.encode('utf-8')).decode('utf-8')
    return raw_content


def save_configs_to_repo():
    """
    Writes branded working configs grouped by protocol into `configs/`.
    Attempts git commit & push if a repository is initialized.
    """
    from flask import current_app
    configs_dir = os.path.abspath(os.path.join(current_app.root_path, '..', 'configs'))
    os.makedirs(configs_dir, exist_ok=True)

    working_configs = get_working_configs(limit=1000)

    # All working configs (branded + base64 subscription file)
    all_lines = build_branded_lines(working_configs)
    with open(os.path.join(configs_dir, "working_all.txt"), 'w', encoding='utf-8') as f:
        f.write('\n'.join(all_lines))
    with open(os.path.join(configs_dir, "subscription_all.txt"), 'w', encoding='utf-8') as f:
        f.write(base64.b64encode('\n'.join(all_lines).encode('utf-8')).decode('utf-8'))

    # Per-protocol files
    protocols = set(c.protocol for c in working_configs)
    for proto in protocols:
        proto_configs = [c for c in working_configs if c.protocol == proto]
        proto_lines = build_branded_lines(proto_configs)
        with open(os.path.join(configs_dir, f"working_{proto}.txt"), 'w', encoding='utf-8') as f:
            f.write('\n'.join(proto_lines))

    print(f"[Collector] Wrote {len(working_configs)} branded configs to {configs_dir}")

    try:
        subprocess.run(['git', 'add', '.'], cwd=configs_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ['git', 'commit', '-m', f'Auto-update VPN configs: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}'],
            cwd=configs_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(['git', 'push'], cwd=configs_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
