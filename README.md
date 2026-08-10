# VOLTAVPN
🛡 Бесплатный VPN сервис Сайт + бот для управления подписками.  🌍
# VPNHub

Современный Flask-сайт + Telegram-бот для продажи VPN-подписок с автоматическим сбором и проверкой конфигураций.

## Возможности

- 🌐 **Сайт**: Landing page, личный кабинет, оплата подписок, QR-коды
- 🤖 **Telegram-бот**: /start, /getconfig, /buy, пробный период 30 дней
- 🔄 **Автосбор конфигов**: Каждый час сбор с GitHub, проверка, сохранение рабочих
- 💳 **Оплата**: Platega.io + CryptoBot
- 📱 **QR-коды**: Генерация для каждой подписки

## Требования

- Python 3.9+
- pip
- Git
- Telegram Bot Token (от @BotFather)
- Аккаунт Platega.io (опционально)
- Аккаунт CryptoBot (опционально)

## Установка

### 1. Клонирование репозитория

```bash
git clone <your-repo-url>
cd VPNHUB
```

### 2. Создание виртуального окружения

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка переменных окружения

Скопируйте пример конфигурации:
```bash
cp .env.example .env
```

Отредактируйте `.env` файл:

```env
FLASK_SECRET_KEY=your-secret-key-here
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
PLATEGA_API_KEY=your-platega-api-key
PLATEGA_SHOP_ID=your-shop-id
CRYPTOBOT_API_TOKEN=your-cryptobot-token
WEBHOOK_URL=https://your-domain.com
DATABASE_URL=sqlite:///vpnhub.db
CONFIG_REPO_URL=https://github.com/your-repo/configs.git
CONFIG_REPO_TOKEN=your-github-token
```

### 5. Инициализация базы данных

База данных создается автоматически при первом запуске. Для ручного создания:

```bash
python -c "from app import create_app; create_app()"
```

## Запуск

### Разработка

```bash
python run.py
```

Сайт будет доступен на `http://localhost:5000`

### Продакшен

Рекомендуется использовать Gunicorn + Nginx:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 "app:app"
```

## Настройка Telegram бота

1. Создайте бота через [@BotFather](https://t.me/BotFather)
2. Получите токен
3. Добавьте токен в `.env` файл: `TELEGRAM_BOT_TOKEN=your-token`
4. Перезапустите приложение

## Настройка платежей

### Platega.io

1. Зарегистрируйтесь на [platega.io](https://platega.io)
2. Создайте магазин
3. Получите API ключ и Shop ID
4. Добавьте в `.env`:
   ```env
   PLATEGA_API_KEY=your-api-key
   PLATEGA_SHOP_ID=your-shop-id
   ```

### CryptoBot

1. Создайте бота через [@CryptoBot](https://t.me/CryptoBot)
2. Получите API токен
3. Добавьте в `.env`:
   ```env
   CRYPTOBOT_API_TOKEN=your-token
   ```

## Настройка автосбора конфигов

Конфиги собираются автоматически каждый час из:
- https://github.com/igareck/vpn-configs-for-russia/blob/main/BLACK_SS+All_RUS.txt
- https://github.com/igareck/vpn-configs-for-russia/blob/main/BLACK_VLESS_RUS.txt
- https://github.com/igareck/vpn-configs-for-russia/blob/main/BLACK_VLESS_RUS_mobile.txt

Для сохранения конфигов в ваш репозиторий настройте:
```env
CONFIG_REPO_URL=https://github.com/your-username/configs.git
CONFIG_REPO_TOKEN=github-personal-access-token
```

## Тарифы

- Бесплатный пробный период: 30 дней
- 1 месяц: 199₽
- 2 месяца: 378₽
- 3 месяца: 567₽
- 6 месяцев: 1134₽
- 12 месяцев: 2268₽

## Структура проекта

```
VPNHUB/
├── app/
│   ├── __init__.py      # Flask app factory
│   ├── models.py        # SQLAlchemy модели
│   ├── routes.py        # Web routes
│   ├── bot.py           # Telegram bot handlers
│   ├── collector.py     # Сборщик и проверщик конфигов
│   ├── payment.py       # Платежные интеграции
│   ├── templates/       # HTML шаблоны
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── dashboard.html
│   │   └── subscribe.html
│   └── static/
│       ├── css/
│       │   └── style.css
│       └── js/
├── configs/             # Собранные конфиги
├── .env.example         # Пример переменных окружения
├── .env                 # Ваши переменные (не коммитить!)
├── requirements.txt     # Зависимости
├── run.py               # Точка входа
└── README.md            # Документация
```

## Разработка

### Запуск тестов

```bash
# Добавьте тесты в папку tests/
pytest tests/
```

### Линтинг

```bash
pip install flake8
flake8 app/
```

## Безопасность

- Никогда не коммитьте `.env` файл
- Используйте сильный `FLASK_SECRET_KEY` в продакшене
- Настройте HTTPS через обратный прокси (Nginx)
- Регулярно обновляйте зависимости

## Troubleshooting

### Ошибка greenlet при установке

```bash
pip install greenlet --only-binary=:all:
```

### Бот не запускается

Проверьте, что `TELEGRAM_BOT_TOKEN` установлен в `.env`

### Платежи не работают

Проверьте API ключи в `.env` и доступность платежных систем

## Лицензия

MIT
