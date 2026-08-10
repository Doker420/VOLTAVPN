# Установка VOLTA на сервер Ubuntu 22.04

Пошаговая инструкция по развёртыванию VOLTA (Flask-сайт + Telegram-бот + ежечасный сборщик VPN-конфигов) на чистом сервере Ubuntu 22.04 LTS.

> Важно про архитектуру: Telegram-бот (long polling) и планировщик APScheduler запускаются **внутри процесса Flask-приложения**. Поэтому Gunicorn запускается строго с **одним воркером** (`-w 1`). Несколько воркеров породят несколько поллеров Telegram (ошибка `Conflict: terminated by other getUpdates`) и продублируют ежечасный сбор конфигов. Масштабируйте потоками (`--threads`), а не воркерами.

---

## 0. Предварительные требования

- Сервер с Ubuntu 22.04 LTS и root-доступом (или пользователь с `sudo`).
- Доменное имя, указывающее A-записью на IP сервера (для HTTPS). Например `volta.example.com`.
- Telegram Bot Token от [@BotFather](https://t.me/BotFather).
- (Опционально) Ключи Platega.io и токен CryptoBot для приёма платежей.

---

## 1. Базовая подготовка системы

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git nginx ufw
```

Проверьте версию Python (нужен 3.9+):

```bash
python3 --version
```

---

## 2. Создание системного пользователя

Запускать сервис от root небезопасно. Создаём отдельного пользователя:

```bash
sudo adduser --system --group --home /opt/volta volta
```

---

## 3. Загрузка кода

```bash
sudo git clone <URL-вашего-репозитория> /opt/volta/app
# либо скопируйте файлы проекта в /opt/volta/app вручную (scp/rsync)

sudo chown -R volta:volta /opt/volta
```

Структура должна быть такой: `/opt/volta/app/run.py`, `/opt/volta/app/requirements.txt`, `/opt/volta/app/app/…`.

---

## 4. Виртуальное окружение и зависимости

```bash
cd /opt/volta/app
sudo -u volta python3 -m venv venv
sudo -u volta ./venv/bin/pip install --upgrade pip
sudo -u volta ./venv/bin/pip install -r requirements.txt
sudo -u volta ./venv/bin/pip install gunicorn
```

> На Ubuntu 22.04 `greenlet`/`cryptography`/`Pillow` ставятся из готовых wheel без компиляции. Если сборка всё же начнётся и упадёт, установите заголовки: `sudo apt install -y build-essential python3-dev libffi-dev`.

---

## 5. Настройка переменных окружения

```bash
sudo -u volta cp .env.example .env
sudo -u volta nano .env
```

Заполните значения:

```env
FLASK_SECRET_KEY=<сгенерируйте: python3 -c "import secrets; print(secrets.token_hex(32))">
TELEGRAM_BOT_TOKEN=<токен от BotFather>
ADMIN_TELEGRAM_IDS=<ваш_telegram_id>          # можно несколько через запятую
PLATEGA_API_KEY=<ключ или пусто>
PLATEGA_SHOP_ID=<shop id или пусто>
CRYPTOBOT_API_TOKEN=<токен или пусто>
WEBHOOK_URL=https://volta.example.com          # ваш реальный домен со https
DATABASE_URL=sqlite:////opt/volta/app/instance/volta.db
```

> `WEBHOOK_URL` используется для генерации ссылок подписки и deep-links мгновенного подключения — обязательно укажите реальный публичный `https`-домен, иначе клиенты получат `localhost`.

Создайте каталог для базы данных:

```bash
sudo -u volta mkdir -p /opt/volta/app/instance
```

---

## 6. Проверка запуска вручную

```bash
cd /opt/volta/app
sudo -u volta ./venv/bin/python run.py
```

Приложение должно подняться на `0.0.0.0:5000`, при первом старте начнётся фоновый сбор конфигов. Остановите его (`Ctrl+C`) и переходите к systemd.

---

## 7. Systemd-сервис (Gunicorn, 1 воркер)

Создайте юнит:

```bash
sudo nano /etc/systemd/system/volta.service
```

Содержимое:

```ini
[Unit]
Description=VOLTA VPN (Flask + Telegram bot + collector)
After=network.target

[Service]
Type=simple
User=volta
Group=volta
WorkingDirectory=/opt/volta/app
EnvironmentFile=/opt/volta/app/.env
ExecStart=/opt/volta/app/venv/bin/gunicorn \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --bind 127.0.0.1:5000 \
    run:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> `run:app` использует объект `app`, который уже создаётся в `run.py` через `from app import app`. Ровно 1 воркер обязателен из-за бот-поллера и планировщика.

Запустите и включите автозагрузку:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now volta
sudo systemctl status volta
```

Логи:

```bash
sudo journalctl -u volta -f
```

---

## 8. Nginx как обратный прокси

```bash
sudo nano /etc/nginx/sites-available/volta
```

Содержимое:

```nginx
server {
    listen 80;
    server_name volta.example.com;

    # Отдаём статику напрямую (быстрее)
    location /static/ {
        alias /opt/volta/app/app/static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Активируйте конфиг:

```bash
sudo ln -s /etc/nginx/sites-available/volta /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## 9. HTTPS через Let's Encrypt

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d volta.example.com
```

Certbot автоматически пропишет 443, редирект с 80 и настроит автопродление. Проверка автопродления:

```bash
sudo certbot renew --dry-run
```

После выпуска сертификата убедитесь, что `WEBHOOK_URL` в `.env` = `https://volta.example.com`, и перезапустите сервис:

```bash
sudo systemctl restart volta
```

---

## 10. Файрвол

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

Порт 5000 наружу НЕ открываем — доступ к приложению только через Nginx.

---

## 11. Назначение администратора

Админ-панель `/admin` и админ-команды бота доступны только администраторам.

- Для бота: укажите свой Telegram ID в `ADMIN_TELEGRAM_IDS` (узнать ID можно у [@userinfobot](https://t.me/userinfobot)).
- Для веб-аккаунта: зарегистрируйтесь на сайте, затем выдайте права:

```bash
cd /opt/volta/app
sudo -u volta ./venv/bin/python make_admin.py <ваш_логин>
# список админов:
sudo -u volta ./venv/bin/python make_admin.py --list
```

После этого пункт «Админ» появится в навигации сайта, а `/admin` откроется (200 вместо 403).

---

## 12. Автосбор конфигов в Git-репозиторий (опционально)

Сборщик каждый час пишет проверенные конфиги в `/opt/volta/app/configs/` и пытается сделать `git commit && git push`. Чтобы пуш работал в ваш репозиторий:

```bash
cd /opt/volta/app/configs
sudo -u volta git init
sudo -u volta git remote add origin https://<TOKEN>@github.com/<user>/<repo>.git
sudo -u volta git config user.email "bot@volta"
sudo -u volta git config user.name "VOLTA Bot"
```

Используйте GitHub Personal Access Token в URL для неинтерактивного push. Если Git не настроен — сбор всё равно работает, файлы просто хранятся локально.

---

## 13. Проверка работоспособности

```bash
# Сайт отвечает
curl -I https://volta.example.com

# API статистики (JSON со счётчиками конфигов)
curl https://volta.example.com/api/stats

# Публичная подписка (Base64)
curl https://volta.example.com/sub/public
```

В Telegram отправьте боту `/start` — должен активироваться пробный период и появиться меню с кнопкой «⚡ Подключиться».

---

## 14. Обновление приложения

```bash
cd /opt/volta/app
sudo -u volta git pull
sudo -u volta ./venv/bin/pip install -r requirements.txt
sudo systemctl restart volta
```

---

## 15. Резервное копирование

Вся критичная информация — в SQLite и QR-кодах:

```bash
# База данных
sudo cp /opt/volta/app/instance/volta.db /opt/volta/backups/volta-$(date +%F).db
```

Рекомендуется настроить ежедневный `cron` на копирование `instance/volta.db` в отдельное хранилище.

---

## Диагностика

| Симптом | Причина / решение |
|---|---|
| `Conflict: terminated by other getUpdates` в логах | Запущено >1 воркера Gunicorn. В юните должно быть `--workers 1`. Проверьте, что бот не запущен ещё где-то с тем же токеном. |
| Клиентам приходит ссылка с `localhost` | Не задан `WEBHOOK_URL` в `.env`. Укажите публичный `https`-домен и перезапустите сервис. |
| Конфиги не собираются | Проверьте `sudo journalctl -u volta -f`; убедитесь, что сервер имеет исходящий доступ к `raw.githubusercontent.com`. |
| `/admin` возвращает 403 | Аккаунт не админ. Выполните `make_admin.py <логин>`. |
| 502 Bad Gateway в Nginx | Сервис `volta` не запущен или упал. `sudo systemctl status volta`. |
| Ошибка сборки `greenlet`/`cryptography` при `pip install` | `sudo apt install -y build-essential python3-dev libffi-dev` и повторите установку. |

---

Готово. VOLTA развёрнут на Ubuntu 22.04 с HTTPS, автозапуском, обратным прокси и ежечасным автосбором проверенных VPN-конфигураций.
