# VoltaVPN — Сервис VPN-подписок с автоматическим сбором и проверкой узлов

Полнофункциональный веб-сервис и Telegram-бот на Python (Flask + SQLAlchemy + python-telegram-bot + APScheduler) для автоматической раздачи проверенных VPN-конфигураций (VLESS Reality, Trojan, Shadowsocks-2022, Hysteria 2).

---

## ⚡ Ключевые возможности

1. **Мгновенный бесплатный пробный период 3 дня**:
   - При регистрации на сайте или первом запуске Telegram-бота пользователь сразу получает 3 дня полного доступа.
   - Защита от злоупотреблений пробным периодом.

2. **Гарантия рабочих конфигураций и Zero-Downtime**:
   - Фоновый планировщик (`APScheduler`) каждый час проверяет доступность всех серверов по TCP Ping.
   - Неработающие узлы автоматически исключаются из раздачи.
   - Подписка доставляется с заголовками `Profile-Title: VoltaVPN` и `Update-Interval: 1` — клиентские приложения автоматически обновляют список серверов каждый час в фоновом режиме.

3. **Юридическая документация для платежных систем**:
   - 🔒 **Политика конфиденциальности** (`/privacy`): строгая No-Logs политика, отсутствие логирования трафика, DNS-запросов и истории посещений.
   - 📜 **Пользовательское соглашение / Оферта** (`/terms`): предмет договора, правила использования, 100% возврат средств (14-дневная гарантия возврата).
   - Команды `/terms`, `/privacy`, `/rules` и меню в Telegram-боте.

4. **Инструкции по подключению со схемами и скриншотами**:
   - 📱 **iOS (iPhone / iPad)**: Karing, Streisand, V2Box.
   - 🤖 **Android**: v2rayNG, Karing, NekoBox.
   - 💻 **Windows**: Hiddify, v2rayN, Karing.
   - 🍏 **macOS**: Streisand, Hiddify.
   - 📺 **Android TV и Роутеры**: Keenetic, OpenWrt, v2rayNG TV.
   - Страница `/instructions` и команда `/instructions` / `/guide` в боте с кнопками импорта в 1 клик (`karing://`, `v2rayng://`, `hiddify://`, `streisand://`).

5. **Часто задаваемые вопросы (База знаний / FAQ)**:
   - Страница `/faq` с поиском в реальном времени и фильтрацией по категориям (Безопасность, Настройка, Оплата, Скорость, Решение проблем).
   - Аккордеон FAQ на главной странице `/` и в личном кабинете.
   - Команда `/faq` и инлайн-рубрикатор в Telegram-боте.

6. **Платежные системы**:
   - **ЮMoney**: банковские карты РФ (МИР, Visa, Mastercard), СБП, кошелек ЮMoney с автоматическим Webhook-уведомлением.
   - **Platega.io**: карты, СБП.
   - **CryptoBot**: USDT, TON, BTC через Telegram Wallet.

7. **Онлайн-чат поддержки в реальном времени**:
   - Виджет онлайн-чата на сайте с мгновенным опросом сообщений.
   - Пересылка сообщений администраторам в Telegram с кнопкой «Ответить».

8. **Админ-панель** (`/admin`):
   - Мониторинг статистики, управление пользователями, продление подписок, массовое добавление конфигураций (включая авто-декодирование Base64-подписок).
   - Ручной запуск тестирования серверов.

---

## 🛠 Установка и запуск

### 1. Клонирование и зависимости

```bash
git clone https://github.com/Doker420/VOLTAVPN.git
cd VOLTAVPN
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Настройка окружения (`.env`)

Создайте файл `.env`:

```env
FLASK_SECRET_KEY=generate-a-strong-random-key
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ADMIN_IDS=123456789
ADMIN_PASSWORD=strong_admin_pass
WEBHOOK_URL=https://your-domain.com
YOOMONEY_RECEIVER=4100118544926615
YOOMONEY_NOTIFICATION_SECRET=your_secret_from_yoomoney
SUPPORT_EMAIL=support@voltavpn.net
SUPPORT_TELEGRAM=@voltavpn_support
```

### 3. Запуск сервиса

```bash
python3 run.py
```

Веб-сервер будет запущен на `http://0.0.0.0:5000`.

---

## 🧪 Запуск тестов

```bash
pytest -v
```

Все 27 тестов проверяют регистрацию, 3-дневный триал, динамический фид подписки, парсинг всех протоколов (VLESS, Trojan, SS SIP002/legacy, Hysteria 2, VMess, IPv6), платежи ЮMoney, чат поддержки, оферту, политику конфиденциальности, FAQ и Telegram-бота.

---

## 📦 Ссылки

- **Ветка в репозитории**: `arena/01a0f001-voltavpn`
- **Скачать архив проекта**: `GET /download/zip`
