#!/usr/bin/env bash
# One-command fresh VPS bootstrap: 3X-UI + VLESS Reality + ready URI.
# Run as root on a fresh Debian/Ubuntu VPS:
#   bash bootstrap_fresh_vps.sh
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт от root." >&2
  exit 1
fi

VPN_PORT="${VPN_PORT:-16012}"
TARGET="${REALITY_TARGET:-www.cloudflare.com}"
REPO_RAW="https://raw.githubusercontent.com/Doker420/VOLTAVPN/arena/01a0f0db-voltavpn/scripts/create_clean_reality.py"

export DEBIAN_FRONTEND=noninteractive
echo "[1/5] Установка системных пакетов..."
apt-get update -y
apt-get install -y curl ca-certificates python3 python3-requests jq ufw

if ! command -v x-ui >/dev/null 2>&1 && [[ ! -x /usr/local/x-ui/x-ui ]]; then
  echo "[2/5] Установка 3X-UI..."
  XUI_NONINTERACTIVE=1 bash <(curl -fsSL https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh)
else
  echo "[2/5] 3X-UI уже установлен, пропускаю установку."
fi

RESULT_FILE="/etc/x-ui/install-result.env"
if [[ ! -f "$RESULT_FILE" ]]; then
  echo "Не найден $RESULT_FILE. Получите данные панели командой: x-ui settings" >&2
  exit 1
fi

# Official non-interactive installer writes shell variables here.
# shellcheck disable=SC1090
source "$RESULT_FILE"
PANEL_PORT="${PORT:-${PANEL_PORT:-}}"
PANEL_PATH="${WEB_BASE_PATH:-${WEBBASEPATH:-}}"
PANEL_USER="${USERNAME:-${USER:-}}"
PANEL_PASSWORD="${PASSWORD:-}"
PANEL_TOKEN="${API_TOKEN:-${TOKEN:-}}"

if [[ -z "$PANEL_PORT" || -z "$PANEL_PATH" ]]; then
  echo "Не удалось определить PORT или WEB_BASE_PATH из $RESULT_FILE" >&2
  cat "$RESULT_FILE" >&2
  exit 1
fi

systemctl enable --now x-ui >/dev/null 2>&1 || true
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow "${PANEL_PORT}/tcp" >/dev/null 2>&1 || true
ufw allow "${VPN_PORT}/tcp" >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

WORKDIR="/root/volta-bootstrap"
mkdir -p "$WORKDIR"
SCRIPT="$WORKDIR/create_clean_reality.py"
if [[ -f "scripts/create_clean_reality.py" ]]; then
  cp scripts/create_clean_reality.py "$SCRIPT"
else
  curl -fsSL "$REPO_RAW" -o "$SCRIPT"
fi
chmod 700 "$SCRIPT"

export XUI_URL="http://127.0.0.1:${PANEL_PORT}/${PANEL_PATH}"
export XUI_API_TOKEN="$PANEL_TOKEN"
export XUI_USER="$PANEL_USER"
export XUI_PASSWORD="$PANEL_PASSWORD"

echo "[3/5] Создание VLESS Reality на порту ${VPN_PORT}..."
OUTPUT="$WORKDIR/result.txt"
python3 "$SCRIPT" --no-volta --port "$VPN_PORT" | tee "$OUTPUT"

grep '^vless://' "$OUTPUT" > /root/volta-vless-uri.txt || true

echo "[4/5] Проверка порта..."
sleep 2
ss -lntp | grep -E ":${VPN_PORT}[[:space:]]" || {
  echo "ВНИМАНИЕ: Xray не слушает порт ${VPN_PORT}. Проверьте: journalctl -u x-ui -n 100 --no-pager" >&2
  exit 1
}

echo "[5/5] Готово."
echo
printf '%s\n' '================ НАСТРОЙКИ ================'
printf 'Панель:  http://%s:%s/%s\n' "$(curl -4 -fsS --max-time 5 https://api.ipify.org || echo SERVER_IP)" "$PANEL_PORT" "$PANEL_PATH"
printf 'Логин:   %s\n' "$PANEL_USER"
printf 'Пароль:  %s\n' "$PANEL_PASSWORD"
printf 'Порт VPN: %s/tcp\n' "$VPN_PORT"
printf 'URI сохранён: /root/volta-vless-uri.txt\n'
printf '%s\n' '============================================='
cat /root/volta-vless-uri.txt
