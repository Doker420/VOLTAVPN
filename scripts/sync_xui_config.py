#!/usr/bin/env python3
"""Get a VLESS Reality client from 3X-UI and import it into VoltaVPN.

The script deliberately reads credentials from environment variables so panel
passwords and VoltaVPN admin passwords are not stored in the repository.

Required:
  pip install requests
  XUI_URL=https://145.63.137.109:1972/KM81VuwVs0vaA39LCJ
  XUI_USER=...
  XUI_PASSWORD=...
  VOLTA_URL=https://vpn.example.com
  VOLTA_ADMIN_USER=...
  VOLTA_ADMIN_PASSWORD=...

Optional:
  XUI_VERIFY_TLS=1       Verify the panel certificate (default: 0)
  XUI_INBOUND_ID=12      Select a particular inbound; otherwise first VLESS
  VOLTA_COUNTRY_CODE=DE

Usage:
  python scripts/sync_xui_config.py
  python scripts/sync_xui_config.py --print-only
"""

import argparse
import json
import os
import sys
from urllib.parse import quote, urlparse

import requests


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


def as_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def api_url(base, path):
    return base.rstrip("/") + "/" + path.lstrip("/")


def login_xui(session, base, username, password):
    """Login and transparently handle panels configured as plain HTTP."""
    try:
        response = session.post(
            api_url(base, "/login"),
            data={"username": username, "password": password},
            timeout=20,
        )
    except requests.exceptions.SSLError as exc:
        if not base.lower().startswith("https://"):
            raise
        # WRONG_VERSION_NUMBER means the endpoint is speaking HTTP, despite
        # the panel's displayed Access URL saying HTTPS. Retry only with the
        # same host/path over HTTP; never silently ignore certificate errors.
        if "wrong_version_number" not in str(exc).lower():
            raise
        base = "http://" + base.split("://", 1)[1]
        print("Предупреждение: панель отвечает обычным HTTP, переключаюсь на http://", file=sys.stderr)
        response = session.post(
            api_url(base, "/login"),
            data={"username": username, "password": password},
            timeout=20,
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success", True):
        raise RuntimeError(payload.get("msg") or "Не удалось войти в 3X-UI")
    return base


def get_inbounds(session, base):
    response = session.get(api_url(base, "/panel/api/inbounds/list"), timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success", True):
        raise RuntimeError(payload.get("msg") or "3X-UI не вернула список подключений")
    return payload.get("obj") or []


def choose_inbound(inbounds, wanted_id=None):
    if wanted_id:
        for inbound in inbounds:
            if str(inbound.get("id")) == str(wanted_id):
                return inbound
        raise RuntimeError(f"Подключение с ID {wanted_id} не найдено")
    for inbound in inbounds:
        if str(inbound.get("protocol", "")).lower() == "vless":
            return inbound
    raise RuntimeError("В 3X-UI не найдено подключение VLESS")


def make_vless_uri(inbound, panel_host):
    settings = as_json(inbound.get("settings"))
    stream = as_json(inbound.get("streamSettings"))
    clients = settings.get("clients") or []
    if not clients:
        raise RuntimeError("В выбранном подключении нет клиентов VLESS")
    client = clients[0]
    client_id = client.get("id") or client.get("uuid")
    if not client_id:
        raise RuntimeError("У клиента VLESS отсутствует UUID")

    port = int(inbound.get("port"))
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    params = {"type": network, "encryption": "none"}
    if client.get("flow"):
        params["flow"] = client["flow"]

    if security == "reality":
        reality = as_json(stream.get("realitySettings"))
        reality_settings = as_json(reality.get("settings"))
        public_key = reality_settings.get("publicKey") or reality.get("publicKey")
        short_ids = reality.get("shortIds") or reality_settings.get("shortIds") or []
        server_names = reality.get("serverNames") or reality_settings.get("serverNames") or []
        if not public_key or not short_ids:
            raise RuntimeError("У Reality-подключения отсутствуют publicKey или shortIds")
        params.update({
            "security": "reality",
            "pbk": public_key,
            "sid": short_ids[0],
            "fp": "chrome",
        })
        if server_names:
            params["sni"] = server_names[0]
        dest = reality.get("dest") or reality_settings.get("dest")
        if dest:
            # dest is server-side only; it must not be copied as a client URI field.
            pass
    elif security == "tls":
        params["security"] = "tls"
        tls = as_json(stream.get("tlsSettings"))
        if tls.get("serverName"):
            params["sni"] = tls["serverName"]
    else:
        params["security"] = "none"

    query = "&".join(
        f"{quote(str(k))}={quote(str(v), safe='-_')}"
        for k, v in params.items()
    )
    label = inbound.get("remark") or f"3X-UI-{inbound.get('id', 'VLESS')}"
    return f"vless://{client_id}@{panel_host}:{port}?{query}#{quote(label)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-only", action="store_true", help="Только вывести готовый URI, не импортировать")
    args = parser.parse_args()

    xui_url = required("XUI_URL")
    api_token = os.getenv("XUI_API_TOKEN", "").strip()
    xui_user = os.getenv("XUI_USER", "").strip()
    xui_password = os.getenv("XUI_PASSWORD", "").strip()
    if not api_token and (not xui_user or not xui_password):
        raise SystemExit("Задайте XUI_API_TOKEN или обе переменные XUI_USER и XUI_PASSWORD")
    verify = os.getenv("XUI_VERIFY_TLS", "0").lower() in {"1", "true", "yes"}

    session = requests.Session()
    session.verify = verify
    # Newer 3X-UI releases may disable the legacy cookie login and return
    # 403. The API token is the preferred authentication method there.
    if api_token:
        session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        })
        try:
            inbounds = get_inbounds(session, xui_url)
        except requests.exceptions.SSLError as exc:
            if not xui_url.lower().startswith("https://") or "wrong_version_number" not in str(exc).lower():
                raise
            xui_url = "http://" + xui_url.split("://", 1)[1]
            print("Предупреждение: API панели отвечает обычным HTTP, переключаюсь на http://", file=sys.stderr)
            inbounds = get_inbounds(session, xui_url)
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"3X-UI отклонила API-токен ({exc}). Проверьте токен и URL панели."
            ) from exc
    else:
        xui_url = login_xui(session, xui_url, xui_user, xui_password)
        inbounds = get_inbounds(session, xui_url)

    inbound = choose_inbound(inbounds, os.getenv("XUI_INBOUND_ID"))
    panel_host = urlparse(xui_url).hostname
    uri = make_vless_uri(inbound, panel_host)
    print("Готовый VLESS URI:")
    print(uri)

    if args.print_only:
        return

    volta_url = required("VOLTA_URL")
    volta_user = required("VOLTA_ADMIN_USER")
    volta_password = required("VOLTA_ADMIN_PASSWORD")
    volta = requests.Session()
    login = volta.post(api_url(volta_url, "/login"), data={"username": volta_user, "password": volta_password}, timeout=20)
    login.raise_for_status()
    if "/login" in login.url.rstrip("/"):
        raise RuntimeError("VoltaVPN не авторизовал администратора; проверьте VOLTA_ADMIN_USER/PASSWORD")

    response = volta.post(api_url(volta_url, "/admin/configs/add"), data={
        "single_uri": uri,
        "country_code": os.getenv("VOLTA_COUNTRY_CODE", "DE"),
        "make_primary": "1",
    }, timeout=60)
    response.raise_for_status()
    print("Конфигурация отправлена в VoltaVPN. Проверьте вкладку «Конфигурации» в админке.")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
