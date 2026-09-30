#!/usr/bin/env python3
"""Create a clean VLESS + Reality inbound in 3X-UI and import it into VoltaVPN.

This intentionally creates a compatibility-first profile:
  - port 443
  - TCP + VLESS Reality
  - xtls-rprx-vision
  - www.cloudflare.com:443
  - no ML-DSA/pqv and no ML-KEM extensions

Credentials are read only from environment variables.

Required:
  XUI_URL=http://145.63.137.109:1972/YOUR_WEB_BASE_PATH
  XUI_API_TOKEN=...

Optional VoltaVPN import:
  VOLTA_URL=https://vpn.example.com
  VOLTA_ADMIN_USER=...
  VOLTA_ADMIN_PASSWORD=...
  VOLTA_COUNTRY_CODE=DE

Usage:
  python scripts/create_clean_reality.py
  python scripts/create_clean_reality.py --no-volta
"""

import argparse
import json
import os
import secrets
import sys
import uuid
from urllib.parse import quote, urlparse

import requests


def need(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


def url(base, path):
    return base.rstrip("/") + "/" + path.lstrip("/")


def check(response, what):
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"{what}: HTTP {response.status_code}: {body}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{what}: панель вернула не JSON: {response.text[:300]}") from exc
    if payload.get("success") is False:
        raise RuntimeError(f"{what}: {payload.get('msg') or payload}")
    return payload


def make_uri(host, port, client_id, public_key, short_id, spider_x="/"):
    values = {
        "type": "tcp",
        "encryption": "none",
        "security": "reality",
        "flow": "xtls-rprx-vision",
        "pbk": public_key,
        "sid": short_id,
        "fp": "chrome",
        "sni": "www.cloudflare.com",
        "spx": spider_x,
    }
    query = "&".join(
        f"{quote(str(key))}={quote(str(value), safe='-_/.')}"
        for key, value in values.items()
    )
    return f"vless://{client_id}@{host}:{port}?{query}#VoltaVPN-clean-reality"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-volta", action="store_true", help="не импортировать в VoltaVPN")
    parser.add_argument("--port", type=int, default=443, help="порт inbound (по умолчанию 443)")
    args = parser.parse_args()

    xui_url = need("XUI_URL")
    token = need("XUI_API_TOKEN")
    parsed = urlparse(xui_url)
    host = parsed.hostname
    if not host:
        raise SystemExit("В XUI_URL отсутствует hostname")

    session = requests.Session()
    session.verify = os.getenv("XUI_VERIFY_TLS", "0").lower() in {"1", "true", "yes"}
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    cert = check(
        session.get(url(xui_url, "/panel/api/server/getNewX25519Cert"), timeout=20),
        "генерация Reality-ключей",
    ).get("obj") or {}
    private_key = cert.get("privateKey")
    public_key = cert.get("publicKey")
    if not private_key or not public_key:
        raise RuntimeError(f"Панель не вернула X25519 keypair: {cert}")

    client_id = str(uuid.uuid4())
    short_id = secrets.token_hex(8)
    inbound_port = args.port
    settings = {
        "clients": [{
            "id": client_id,
            "flow": "xtls-rprx-vision",
            "email": "volta-clean-reality",
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,
            "enable": True,
            "tgId": "",
            "subId": "",
            "comment": "",
            "reset": 0,
        }],
        "decryption": "none",
        "fallbacks": [],
    }
    stream = {
        "network": "tcp",
        "security": "reality",
        "externalProxy": [],
        "realitySettings": {
            "show": False,
            "xver": 0,
            "dest": "www.cloudflare.com:443",
            "serverNames": ["www.cloudflare.com"],
            "privateKey": private_key,
            "minClient": "",
            "maxClient": "",
            "maxTimediff": 0,
            "shortIds": [short_id],
            "settings": {
                "publicKey": public_key,
                "fingerprint": "chrome",
                "serverName": "",
                "spiderX": "/",
            },
        },
        "tcpSettings": {
            "acceptProxyProtocol": False,
            "header": {"type": "none"},
        },
    }
    payload = {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": "VoltaVPN-clean-reality",
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": inbound_port,
        "protocol": "vless",
        "settings": json.dumps(settings, separators=(",", ":")),
        "streamSettings": json.dumps(stream, separators=(",", ":")),
        "sniffing": json.dumps({
            "enabled": True,
            "destOverride": ["http", "tls", "quic", "fakedns"],
            "metadataOnly": False,
            "routeOnly": False,
        }, separators=(",", ":")),
        "allocate": json.dumps({"strategy": "always", "refresh": 5, "concurrency": 3}),
    }
    check(
        session.post(url(xui_url, "/panel/api/inbounds/add"), json=payload, timeout=30),
        "создание inbound",
    )

    uri = make_uri(host, inbound_port, client_id, public_key, short_id)
    print("Inbound создан в 3X-UI.")
    print("Готовый URI (без pqv и ML-KEM):")
    print(uri)

    if args.no_volta:
        return
    volta_url = os.getenv("VOLTA_URL", "").strip()
    volta_user = os.getenv("VOLTA_ADMIN_USER", "").strip()
    volta_password = os.getenv("VOLTA_ADMIN_PASSWORD", "").strip()
    if not all((volta_url, volta_user, volta_password)):
        print("VoltaVPN не настроен: URI выведен, импорт пропущен.", file=sys.stderr)
        return

    volta = requests.Session()
    login = volta.post(url(volta_url, "/login"), data={
        "username": volta_user,
        "password": volta_password,
    }, timeout=20)
    login.raise_for_status()
    response = volta.post(url(volta_url, "/admin/configs/add"), data={
        "single_uri": uri,
        "country_code": os.getenv("VOLTA_COUNTRY_CODE", "DE"),
        "make_primary": "1",
    }, timeout=60)
    response.raise_for_status()
    print("Конфигурация импортирована в VoltaVPN и назначена основной.")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
