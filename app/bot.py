import os
import uuid
import html
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

PLAN_LABELS = {
    'free_trial': 'Бесплатный период',
    '1_month': '1 месяц',
    '2_months': '2 месяца',
    '3_months': '3 месяца',
    '6_months': '6 месяцев',
    '12_months': '12 месяцев',
}

def esc(value):
    """Escape a value for Telegram HTML parse mode."""
    return html.escape(str(value), quote=False)

def base_url():
    return flask_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')

def sub_link(sub):
    """
    Always build the subscription URL from the CURRENT WEBHOOK_URL, so links
    never show a stale 'localhost' persisted before the domain was configured.
    """
    return f"{base_url()}/sub/{sub.sub_token}"

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

class UserCtx:
    """Detached snapshot of a user, safe to use outside the DB session."""
    __slots__ = ('id', 'telegram_id', 'username', 'is_admin', 'login_token')

    def __init__(self, user):
        self.id = user.id
        self.telegram_id = user.telegram_id
        self.username = user.username
        self.is_admin = bool(user.is_admin)
        self.login_token = user.login_token


class SubCtx:
    """Detached snapshot of a subscription, safe to use outside the DB session."""
    __slots__ = ('id', 'plan', 'sub_token', 'config_link', 'end_date',
                 'is_active', '_days_left', '_expired')

    def __init__(self, sub):
        self.id = sub.id
        self.plan = sub.plan
        self.sub_token = sub.sub_token
        self.config_link = sub.config_link
        self.end_date = sub.end_date
        self.is_active = sub.is_active
        self._days_left = sub.days_left()
        self._expired = sub.is_expired()

    def days_left(self):
        return self._days_left

    def is_expired(self):
        return self._expired


def get_or_create_user(telegram_user):
    """
    Returns (UserCtx, SubCtx|None) — plain snapshots detached from the ORM
    session, so callers can safely read attributes after the context closes.

    A Telegram-originated user is considered verified (telegram_verified=True),
    since interacting with the bot proves control of the Telegram account.
    Trial provisioning is guarded by the anti-abuse ledger (TrialClaim): one
    trial per telegram_id, ever.
    """
    from app.models import TrialClaim

    with flask_app.app_context():
        user = User.query.filter_by(telegram_id=telegram_user.id).first()
        if not user:
            user = User(
                telegram_id=telegram_user.id,
                telegram_verified=True,
                username=telegram_user.username or f"user_{telegram_user.id}",
                email=None
            )
            db.session.add(user)
            db.session.commit()
        elif not user.telegram_verified:
            user.telegram_verified = True
            db.session.commit()

        # Ensure the user has a login token for one-tap web LK access from the bot
        if not user.login_token:
            user.login_token = uuid.uuid4().hex
            db.session.commit()

        # Provision trial only once per telegram_id (anti-abuse ledger)
        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
        already_claimed = TrialClaim.query.filter_by(telegram_id=telegram_user.id).first() is not None
        if not sub and not user.is_trial_used and not already_claimed:
            sub_token = uuid.uuid4().hex
            config_link = f"{base_url()}/sub/{sub_token}"

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
            db.session.add(TrialClaim(telegram_id=telegram_user.id, ip=None, user_id=user.id))
            db.session.commit()
        elif sub:
            # Refresh stored link to the current domain if it drifted (e.g. localhost)
            fresh = f"{base_url()}/sub/{sub.sub_token}"
            if sub.config_link != fresh:
                sub.config_link = fresh
                db.session.commit()

        # Snapshot everything the handlers need BEFORE the session closes
        return UserCtx(user), (SubCtx(sub) if sub else None)

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

def link_web_account(telegram_user, code):
    """
    Binds a web-registered account (identified by link_code) to this Telegram
    user and grants the trial once (guarded by TrialClaim). Returns a status
    string: 'linked', 'already', 'trial_used', 'invalid'.
    """
    from app.models import TrialClaim

    with flask_app.app_context():
        web_user = User.query.filter_by(link_code=code).first()
        if not web_user:
            return 'invalid'

        # If this telegram already has its own account, do not allow double-claim.
        existing_tg = User.query.filter_by(telegram_id=telegram_user.id).first()
        if existing_tg and existing_tg.id != web_user.id:
            # Telegram already used elsewhere; block linking to farm trials.
            return 'tg_taken'

        already_claimed = TrialClaim.query.filter_by(telegram_id=telegram_user.id).first() is not None

        web_user.telegram_id = telegram_user.id
        web_user.telegram_verified = True
        if not web_user.login_token:
            web_user.login_token = uuid.uuid4().hex
        db.session.commit()

        sub = Subscription.query.filter_by(user_id=web_user.id).order_by(Subscription.created_at.desc()).first()
        if sub:
            return 'already'
        if web_user.is_trial_used or already_claimed:
            return 'trial_used'

        sub_token = uuid.uuid4().hex
        config_link = f"{base_url()}/sub/{sub_token}"
        qr_path = None
        try:
            qr_path = generate_qr_image(config_link, sub_token)
            # store relative path like the web side expects
            qr_path = os.path.relpath(qr_path, os.path.join(flask_app.root_path, 'static')).replace('\\', '/')
        except Exception:
            qr_path = None

        sub = Subscription(
            user_id=web_user.id,
            plan='free_trial',
            sub_token=sub_token,
            end_date=datetime.utcnow() + timedelta(days=30),
            config_link=config_link,
            qr_code_path=qr_path,
            is_active=True,
            payment_status='paid',
        )
        db.session.add(sub)
        web_user.is_trial_used = True
        db.session.add(TrialClaim(telegram_id=telegram_user.id, ip=None, user_id=web_user.id))
        db.session.commit()
        return 'linked'


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Handle deep-link account binding: /start link_<code>
    args = context.args if hasattr(context, 'args') else None
    if args and args[0].startswith('link_'):
        code = args[0][len('link_'):]
        result = link_web_account(user, code)
        if result == 'invalid':
            await update.message.reply_text("⚠️ Код привязки недействителен. Откройте сайт → «Привязать Telegram» ещё раз.")
        elif result == 'tg_taken':
            await update.message.reply_text("⛔ Этот Telegram уже привязан к другому аккаунту. Один Telegram — один бесплатный период.")
        elif result in ('already', 'trial_used'):
            db_user, sub = get_or_create_user(user)
            is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)
            await update.message.reply_text(
                "✅ Telegram привязан! Аккаунт уже имеет подписку или бесплатный период был использован ранее.",
                reply_markup=get_main_keyboard(is_admin)
            )
        else:  # linked
            db_user, sub = get_or_create_user(user)
            is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)
            link = sub_link(sub) if sub else base_url()
            await update.message.reply_text(
                "🎉 <b>Telegram привязан, бесплатный период на 30 дней активирован!</b>\n\n"
                f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>\n\n"
                "Нажмите «⚡ Подключиться» для импорта в один тап.",
                parse_mode='HTML', reply_markup=get_main_keyboard(is_admin)
            )
        return

    db_user, sub = get_or_create_user(user)
    is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)

    if not sub:
        msg = (
            f"👋 <b>Привет, {esc(user.first_name)}!</b>\n\n"
            f"⚡ <b>VOLTA</b> — молниеносный VPN с автоподпиской для РФ!\n\n"
            f"ℹ️ Бесплатный период уже был использован на вашем аккаунте.\n"
            f"Оформите подписку всего за <b>199 ₽/мес</b>, чтобы продолжить."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]])
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        return

    days_left = sub.days_left()
    status_text = f"🟢 Активна ({days_left} дн.)" if not sub.is_expired() else "🔴 Истекла"
    link = sub_link(sub)

    msg = (
        f"👋 <b>Привет, {esc(user.first_name)}!</b>\n\n"
        f"⚡ <b>VOLTA</b> — молниеносный VPN с автоподпиской для РФ!\n\n"
        f"📌 <b>Ваш статус:</b> {status_text}\n"
        f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>\n\n"
        f"🎁 Вам начислен <b>бесплатный период 30 дней</b>!\n"
        f"Нажмите «⚡ Подключиться» для импорта в один тап или используйте меню ниже."
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard(is_admin))

async def my_sub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, sub = get_or_create_user(user)
    is_admin = user.id in ADMIN_IDS or (db_user and db_user.is_admin)

    if not sub or sub.is_expired():
        msg = (
            f"❌ <b>Ваша подписка истекла!</b>\n\n"
            f"Для возобновления работы автообновляемых VPN-конфигураций продлите подписку всего за <b>199 ₽/мес</b>."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]
        ])
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        return

    days_left = sub.days_left()
    link = sub_link(sub)
    plan_name = PLAN_LABELS.get(sub.plan, sub.plan)
    msg = (
        f"🛡️ <b>Ваша текущая подписка</b>\n\n"
        f"• <b>Тариф:</b> {esc(plan_name)}\n"
        f"• <b>Осталось:</b> {days_left} дн.\n"
        f"• <b>Дата окончания:</b> {sub.end_date.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"🔗 <b>Персональная ссылка подписки:</b>\n<code>{esc(link)}</code>\n\n"
        f"💡 Скопируйте ссылку и вставьте в клиент (Karing, v2rayN, Streisand, NekoBox, Hiddify) в раздел «Подписки»."
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard(is_admin))

async def qr_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    if not sub or sub.is_expired():
        await update.message.reply_text("⚠️ Ваша подписка истекла! Нажмите «💳 Купить / Продлить» для продления.")
        return

    link = sub_link(sub)
    qr_path = generate_qr_image(link, sub.sub_token)

    caption = (
        f"📱 <b>QR-код вашей подписки</b>\n\n"
        f"🔗 <code>{esc(link)}</code>\n\n"
        f"Откройте <b>Karing</b>, <b>Streisand</b> или <b>v2rayN</b> и отсканируйте QR для моментального импорта!"
    )
    with open(qr_path, 'rb') as photo:
        await update.message.reply_photo(photo=photo, caption=caption, parse_mode='HTML')


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Instant connect. Telegram inline-button URLs only allow http(s)/tg schemes,
    so custom client schemes (karing://, hiddify://, ...) cannot be buttons.
    We send an https button to the site LK and list the one-tap links as
    tappable text inside the message.
    """
    user = update.effective_user
    db_user, sub = get_or_create_user(user)

    if not sub or sub.is_expired():
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оформить подписку", callback_data="buy_menu")]])
        await update.message.reply_text(
            "⚠️ Подписка истекла. Продлите доступ, чтобы подключиться.",
            reply_markup=keyboard
        )
        return

    link = sub_link(sub)
    links = build_deep_links(link)
    # Auto-login link to the site LK (token-based), so the button opens the
    # dashboard already logged in.
    if db_user and db_user.login_token:
        dashboard_url = f"{base_url()}/tg-login/{db_user.login_token}"
    else:
        dashboard_url = f"{base_url()}/dashboard"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть личный кабинет", url=dashboard_url)],
    ])
    msg = (
        "⚡ <b>Мгновенное подключение</b>\n\n"
        "Откройте <b>личный кабинет</b> на сайте — там подключение в один тап "
        "и QR-код для всех приложений.\n\n"
        f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>\n\n"
        "Или откройте напрямую в приложении (нажмите на нужную ссылку):\n"
        f"• <a href=\"{esc(links['karing'])}\">Karing</a>\n"
        f"• <a href=\"{esc(links['v2rayng'])}\">v2rayNG</a>\n"
        f"• <a href=\"{esc(links['streisand'])}\">Streisand</a>\n"
        f"• <a href=\"{esc(links['hiddify'])}\">Hiddify</a>\n"
        f"• <a href=\"{esc(links['singbox'])}\">sing-box</a>"
    )
    await update.message.reply_text(
        msg, parse_mode='HTML', reply_markup=keyboard, disable_web_page_preview=True
    )


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
        "🛠 <b>Админ-панель VOLTA</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"⚡ Активных подписок: <b>{active_subs}</b>\n"
        f"💰 Доход всего: <b>{revenue_total} ₽</b>\n"
        f"📈 Доход за 30 дней: <b>{revenue_month} ₽</b>\n\n"
        f"🌐 Конфигов: <b>{working}</b> рабочих из <b>{total_configs}</b>\n\n"
        f"🔗 Полная панель: {esc(base_url())}/admin"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

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
        "💎 <b>Выберите подходящий тариф:</b>\n\n"
        "• Все протоколы (VLESS / Trojan / Shadowsocks / Hysteria2 / VMess / Tuic)\n"
        "• Автоматическая проверка каждый час\n"
        "• Работает во всех регионах РФ"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with flask_app.app_context():
        total = Config.query.filter_by(is_working=True).count()
        vless = Config.query.filter_by(is_working=True, protocol='vless').count()
        ss = Config.query.filter_by(is_working=True, protocol='ss').count()
        trojan = Config.query.filter_by(is_working=True, protocol='trojan').count()
        hy2 = Config.query.filter_by(is_working=True, protocol='hysteria2').count()

    msg = (
        "📊 <b>Мониторинг серверов VOLTA</b>\n\n"
        f"✅ <b>Всего рабочих конфигов:</b> {total}\n"
        f"⚡ <b>VLESS:</b> {vless}\n"
        f"🚀 <b>Shadowsocks:</b> {ss}\n"
        f"🔒 <b>Trojan:</b> {trojan}\n"
        f"🔥 <b>Hysteria2:</b> {hy2}\n\n"
        "🔄 Автообновление и перепроверка пинга выполняются каждый час."
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>Инструкция по подключению:</b>\n\n"
        "1️⃣ <b>Скопируйте вашу ссылку подписки</b> через меню «📥 QR / Ссылка».\n"
        "2️⃣ <b>Установите клиент для вашего устройства:</b>\n"
        "   • <b>iOS / iPhone:</b> <a href=\"https://apps.apple.com/app/karing/id6472431552\">Karing</a> или <a href=\"https://apps.apple.com/app/streisand/id6450534064\">Streisand</a>\n"
        "   • <b>Android:</b> <a href=\"https://play.google.com/store/apps/details?id=com.v2ray.ang\">Karing</a> или <a href=\"https://github.com/MatsuriDayo/NekoBoxForAndroid\">NekoBox</a>\n"
        "   • <b>Windows:</b> <a href=\"https://github.com/2dust/v2rayN\">v2rayN</a>\n"
        "3️⃣ <b>Добавьте подписку:</b> вставьте вашу ссылку или отсканируйте QR-код.\n"
        "4️⃣ <b>Нажмите «Обновить подписку»</b> и выберите самый быстрый сервер!"
    )
    await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)

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
        f"Тариф: <b>{esc(plan['name'])}</b> ({plan['price']} ₽)\nВыберите способ оплаты:",
        parse_mode='HTML',
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

    payment_url = None
    try:
        with flask_app.app_context():
            if method == 'platega':
                payment_url, ext_id = create_platega_payment(db_user, plan, sub.id if sub else 0)
                method_name = "Platega.io"
            else:
                payment_url, ext_id = create_cryptobot_payment(db_user, plan, sub.id if sub else 0)
                method_name = "CryptoBot"
    except Exception as e:
        print(f"[Bot] Payment creation error: {e}")

    if not payment_url:
        await query.edit_message_text(
            "⚠️ Платёжный шлюз временно недоступен. Попробуйте позже или выберите другой способ оплаты.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="buy_menu")]])
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"🔗 Оплатить {plan['price']} ₽ ({method_name})", url=payment_url)],
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"checkpay_{ext_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="buy_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"💳 <b>Счёт на оплату сформирован!</b>\n\n"
        f"• <b>Тариф:</b> {esc(plan['name'])}\n"
        f"• <b>Сумма:</b> {plan['price']} ₽\n"
        f"• <b>Шлюз:</b> {esc(method_name)}\n\n"
        f"После оплаты нажмите «Проверить оплату» для моментального продления подписки.",
        parse_mode='HTML',
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
            link = sub_link(sub) if sub else base_url()
            await query.edit_message_text(
                f"🎉 <b>Оплата успешно подтверждена!</b>\n\n"
                f"Ваша подписка продлена. Осталось дней: <b>{sub.days_left() if sub else 0}</b>\n\n"
                f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>",
                parse_mode='HTML'
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
        # Each thread needs its own asyncio event loop; the Flask thread's loop
        # is not available here, so create and set a dedicated one.
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            print("[Bot] Telegram Bot polling started successfully.")
            # stop_signals=None is required: run_polling() otherwise installs OS
            # signal handlers (set_wakeup_fd), which only work in the main thread.
            bot_app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,
                stop_signals=None,
            )
        except Exception as e:
            print(f"[Bot] Polling error: {e}")

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
