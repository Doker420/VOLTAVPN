import os
import uuid
import qrcode
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from app.models import User, Subscription, Config, Payment, db
from app.payment import create_platega_payment, create_cryptobot_payment, check_payment_status
from app.collector import get_working_configs

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').replace(' ', '').split(',') if x.isdigit()]
bot_app = None
flask_app = None

PLANS = {
    '1_month': {'name': '1 месяц', 'days': 30, 'price': 199},
    '2_months': {'name': '2 месяца', 'days': 60, 'price': 378},
    '3_months': {'name': '3 месяца', 'days': 90, 'price': 567},
    '6_months': {'name': '6 месяцев', 'days': 180, 'price': 1134},
    '12_months': {'name': '12 месяцев', 'days': 360, 'price': 2268},
}

def build_deep_links(sub_url):
    """One-tap import deep links for popular clients."""
    from urllib.parse import quote
    enc = quote(sub_url, safe='')
    name = quote('VOLTA VPN', safe='')
    return {
        'karing': f"karing://install-config?url={enc}&name={name}",
        'v2rayng': f"v2rayng://install-sub?url={enc}&name={name}",
        'streisand': f"streisand://import/{enc}",
        'hiddify': f"hiddify://import/{enc}#{name}",
        'singbox': f"sing-box://import-remote-profile?url={enc}#{name}",
    }

def get_main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton("⚡ Подключиться"), KeyboardButton("👤 Моя подписка")],
        [KeyboardButton("💳 Купить / Продлить"), KeyboardButton("📥 QR / Ссылка")],
        [KeyboardButton("📊 Статус"), KeyboardButton("❓ Инструкция")],
    ]
    if is_admin:
        keyboard.append([KeyboardButton("🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_or_create_user(telegram_user):
    with flask_app.app_context():
        user = User.query.filter_by(telegram_id=telegram_user.id).first()
        if not user:
            user = User(
                telegram_id=telegram_user.id,
                username=telegram_user.username or f"user_{telegram_user.id}",
                email=None
            )
            db.session.add(user)
            db.session.commit()

        # Check or provision subscription
        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
        if not sub and not user.is_trial_used:
            sub_token = uuid.uuid4().hex
            base_url = flask_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')
            config_link = f"{base_url}/sub/{sub_token}"

            sub = Subscription(
                user_id=user.id,
                plan='free_trial',
                sub_token=sub_token,
                end_date=datetime.utcnow() + timedelta(days=30),
                config_link=config_link,
                is_active=True,
                payment_status='paid'
            )
            db.session.add(sub)
            user.is_trial_used = True
            db.session.commit()
        return user, sub

def generate_qr_image(data, token):
    qr = qrcode.QRCode(version=1, box_size=10, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#10b981", back_color="#ffffff")

    static_dir = os.path.join(flask_app.root_path, 'static', 'qr')
    os.makedirs(static_dir, exist_ok=True)
    filepath = os.path.join(static_dir, f"bot_qr_{token}.png")
    img.save(filepath)
    return filepath

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, sub = get_or_create_user(user)
    is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)

    if not sub:
        msg = (
            f"👋 **Привет, {user.first_name}!**\n\n"
            f"⚡ **VOLTA** — молниеносный VPN с автоподпиской для РФ!\n\n"
            f"ℹ️ Бесплатный период уже был использован на вашем аккаунте.\n"
            f"Оформите подписку всего за **199 ₽/мес**, чтобы продолжить."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]])
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        return

    days_left = sub.days_left()
    status_text = f"🟢 Активна ({days_left} дн.)" if not sub.is_expired() else "🔴 Истекла"

    msg = (
        f"👋 **Привет, {user.first_name}!**\n\n"
        f"⚡ **VOLTA** — молниеносный VPN с автоподпиской для РФ!\n\n"
        f"📌 **Ваш статус:** {status_text}\n"
        f"🔗 **Ссылка подписки:** `{sub.config_link}`\n\n"
        f"🎁 Вам начислен **бесплатный период 30 дней**!\n"
        f"Нажмите **«⚡ Подключиться»** для импорта в один тап или используйте меню ниже."
    )
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_keyboard(is_admin))

async def my_sub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    if not sub or sub.is_expired():
        msg = (
            f"❌ **Ваша подписка истекла!**\n\n"
            f"Для возобновления работы автообновляемых VPN-конфигураций продлите подписку всего за **199 ₽/мес**."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]
        ])
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        return

    days_left = sub.days_left()
    msg = (
        f"🛡️ **Ваша текущая подписка**\n\n"
        f"• **Тариф:** {sub.plan}\n"
        f"• **Осталось:** {days_left} дн.\n"
        f"• **Дата окончания:** {sub.end_date.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"🔗 **Персональная ссылка подписки:**\n`{sub.config_link}`\n\n"
        f"💡 Скопируйте ссылку и вставьте в клиент (Karing, v2rayN, Streisand, NekoBox, Hiddify) в раздел «Подписки»."
    )
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_keyboard())

async def qr_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    if not sub or sub.is_expired():
        await update.message.reply_text("⚠️ Ваша подписка истекла! Нажмите «💳 Купить / Продлить» для продления.")
        return

    qr_path = generate_qr_image(sub.config_link, sub.sub_token)

    caption = (
        f"📱 **QR-код вашей подписки**\n\n"
        f"🔗 `{sub.config_link}`\n\n"
        f"Откройте **Karing**, **Streisand** или **v2rayN** и отсканируйте QR для моментального импорта!"
    )
    with open(qr_path, 'rb') as photo:
        await update.message.reply_photo(photo=photo, caption=caption, parse_mode='Markdown')


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-tap instant connect: sends deep links for popular clients."""
    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    if not sub or sub.is_expired():
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]])
        await update.message.reply_text(
            "⚠️ Подписка истекла. Продлите доступ, чтобы подключиться.",
            reply_markup=keyboard
        )
        return

    links = build_deep_links(sub.config_link)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Karing (iOS/Android)", url=links['karing'])],
        [InlineKeyboardButton("🤖 v2rayNG (Android)", url=links['v2rayng'])],
        [InlineKeyboardButton("🍏 Streisand (iOS)", url=links['streisand'])],
        [InlineKeyboardButton("🛡 Hiddify (все ОС)", url=links['hiddify'])],
        [InlineKeyboardButton("📦 sing-box", url=links['singbox'])],
    ])
    msg = (
        "⚡ **Мгновенное подключение**\n\n"
        "Выберите ваше приложение — подписка импортируется автоматически, без копирования.\n\n"
        "Если приложение ещё не установлено, поставьте его через «❓ Инструкция» и повторите."
    )
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only service statistics."""
    user = update.effective_user
    with flask_app.app_context():
        db_user = User.query.filter_by(telegram_id=user.id).first()
        is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)
        if not is_admin:
            await update.message.reply_text("⛔ Доступ только для администраторов.")
            return

        now = datetime.utcnow()
        month_ago = now - timedelta(days=30)
        total_users = User.query.count()
        active_subs = Subscription.query.filter(Subscription.is_active == True, Subscription.end_date > now).count()
        paid_payments = Payment.query.filter_by(status='paid').all()
        revenue_total = sum(p.amount for p in paid_payments)
        revenue_month = sum(p.amount for p in paid_payments if p.paid_at and p.paid_at >= month_ago)
        total_configs = Config.query.count()
        working = Config.query.filter_by(is_working=True).count()

    msg = (
        "🛠 **Админ-панель VOLTA**\n\n"
        f"👥 Пользователей: **{total_users}**\n"
        f"⚡ Активных подписок: **{active_subs}**\n"
        f"💰 Доход всего: **{revenue_total} ₽**\n"
        f"📈 Доход за 30 дней: **{revenue_month} ₽**\n\n"
        f"🌐 Конфигов: **{working}** рабочих из **{total_configs}**\n\n"
        f"🔗 Полная панель: {flask_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')}/admin"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def buy_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("1 месяц — 199 ₽", callback_data="plan_1_month")],
        [InlineKeyboardButton("2 месяца — 378 ₽ (-5%)", callback_data="plan_2_months")],
        [InlineKeyboardButton("3 месяца — 567 ₽ (-5%)", callback_data="plan_3_months")],
        [InlineKeyboardButton("6 месяцев — 1134 ₽ (-5%)", callback_data="plan_6_months")],
        [InlineKeyboardButton("12 месяцев — 2268 ₽ (-5%)", callback_data="plan_12_months")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        "💎 **Выберите подходящий тариф:**\n\n"
        "• Все протоколы (VLESS / Trojan / Shadowsocks / Hysteria2 / VMess / Tuic)\n"
        "• Автоматическая проверка каждый час\n"
        "• Работает во всех регионах РФ"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with flask_app.app_context():
        total = Config.query.filter_by(is_working=True).count()
        vless = Config.query.filter_by(is_working=True, protocol='vless').count()
        ss = Config.query.filter_by(is_working=True, protocol='ss').count()
        trojan = Config.query.filter_by(is_working=True, protocol='trojan').count()
        hy2 = Config.query.filter_by(is_working=True, protocol='hysteria2').count()

    msg = (
        "📊 **Мониторинг серверов VOLTA**\n\n"
        f"✅ **Всего рабочих конфигов:** {total}\n"
        f"⚡ **VLESS:** {vless}\n"
        f"🚀 **Shadowsocks:** {ss}\n"
        f"🔒 **Trojan:** {trojan}\n"
        f"🔥 **Hysteria2:** {hy2}\n\n"
        "🔄 Автообновление и перепроверка пинга выполняются каждый час."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 **Инструкция по подключению:**\n\n"
        "1️⃣ **Скопируйте вашу ссылку подписки** через меню «📥 QR / Ссылка».\n"
        "2️⃣ **Установите клиент для вашего устройства:**\n"
        "   • **iOS / iPhone:** [Karing](https://apps.apple.com/app/karing/id6472431552) или [Streisand](https://apps.apple.com/app/streisand/id6450534064)\n"
        "   • **Android:** [Karing](https://play.google.com/store/apps/details?id=com.v2ray.ang) или [NekoBox](https://github.com/MatsuriDayo/NekoBoxForAndroid)\n"
        "   • **Windows:** [v2rayN](https://github.com/2dust/v2rayN)\n"
        "3️⃣ **Добавьте подписку:** Вставьте вашу ссылку или отсканируйте QR-код.\n"
        "4️⃣ **Нажмите «Обновить подписку»** и выберите самый быстрый сервер!"
    )
    await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=True)

async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buy_menu":
        await buy_menu_command(update, context)
        return

    plan_id = query.data.replace("plan_", "")
    plan = PLANS.get(plan_id)
    if not plan:
        await query.edit_message_text("Неверный тариф.")
        return

    keyboard = [
        [InlineKeyboardButton("💳 Platega.io (Карты / СБП)", callback_data=f"pay_platega_{plan_id}")],
        [InlineKeyboardButton("💎 CryptoBot (@CryptoBot)", callback_data=f"pay_cryptobot_{plan_id}")],
        [InlineKeyboardButton("⬅️ Назад к тарифам", callback_data="buy_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"Тариф: **{plan['name']}** ({plan['price']} ₽)\nВыберите способ оплаты:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    method = parts[1]
    plan_id = "_".join(parts[2:])

    plan = PLANS.get(plan_id)
    if not plan:
        await query.edit_message_text("Ошибка в тарифе.")
        return

    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    with flask_app.app_context():
        if method == 'platega':
            payment_url, ext_id = create_platega_payment(db_user, plan, sub.id)
            method_name = "Platega.io"
        else:
            payment_url, ext_id = create_cryptobot_payment(db_user, plan, sub.id)
            method_name = "CryptoBot"

    keyboard = [
        [InlineKeyboardButton(f"🔗 Оплатить {plan['price']} ₽ ({method_name})", url=payment_url)],
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"checkpay_{ext_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="buy_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"💳 **Счет на оплату сформирован!**\n\n"
        f"• **Тариф:** {plan['name']}\n"
        f"• **Сумма:** {plan['price']} ₽\n"
        f"• **Шлюз:** {method_name}\n\n"
        f"После оплаты нажмите **«Проверить оплату»** для моментального продления подписки.",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def check_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ext_id = query.data.replace("checkpay_", "")
    user = update.effective_user

    with flask_app.app_context():
        payment = Payment.query.filter_by(external_id=ext_id).first()
        method = payment.payment_method if payment else 'platega'
        status = check_payment_status(ext_id, method)
        
        if status == 'paid':
            db_user, sub = get_or_create_user(user)
            await query.edit_message_text(
                f"🎉 **Оплата успешно подтверждена!**\n\n"
                f"Ваша подписка продлена. Осталось дней: **{sub.days_left()}**\n\n"
                f"🔗 **Ссылка подписки:**\n`{sub.config_link}`",
                parse_mode='Markdown'
            )
        else:
            await query.answer("⌛ Оплата еще не поступила. Попробуйте через 1-2 минуты.", show_alert=True)

async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "⚡ Подключиться":
        await connect_command(update, context)
    elif text == "👤 Моя подписка":
        await my_sub_command(update, context)
    elif text == "💳 Купить / Продлить":
        await buy_menu_command(update, context)
    elif text == "📥 QR / Ссылка":
        await qr_link_command(update, context)
    elif text == "📊 Статус":
        await stats_command(update, context)
    elif text == "❓ Инструкция":
        await help_command(update, context)
    elif text == "🛠 Админ-панель":
        await admin_command(update, context)

def init_bot(app):
    global bot_app, flask_app
    flask_app = app

    if not BOT_TOKEN:
        print("[Bot] TELEGRAM_BOT_TOKEN not provided, running web server without Telegram bot.")
        return

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("sub", my_sub_command))
    bot_app.add_handler(CommandHandler("connect", connect_command))
    bot_app.add_handler(CommandHandler("buy", buy_menu_command))
    bot_app.add_handler(CommandHandler("stats", stats_command))
    bot_app.add_handler(CommandHandler("admin", admin_command))
    bot_app.add_handler(CommandHandler("help", help_command))

    bot_app.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^(plan_|buy_menu)"))
    bot_app.add_handler(CallbackQueryHandler(payment_callback, pattern=r"^pay_"))
    bot_app.add_handler(CallbackQueryHandler(check_pay_callback, pattern=r"^checkpay_"))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))

    def run_bot():
        try:
            print("[Bot] Telegram Bot polling started successfully.")
            bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            print(f"[Bot] Polling error: {e}")

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
