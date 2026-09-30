import os
import uuid
import html
import qrcode
import threading
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from app.models import User, Subscription, Config, Payment, SupportMessage, AppSetting, db
from app.payment import create_platega_payment, create_cryptobot_payment, create_yoomoney_payment, check_payment_status
from app.collector import get_working_configs

load_dotenv()

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
BOT_USERNAME = os.getenv('BOT_USERNAME', '')
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_TELEGRAM_IDS', '').replace(' ', '').split(',') if x.isdigit()]
bot_app = None
flask_app = None

# Free trial days
TRIAL_DAYS = 3
REFERRALS_REQUIRED = 3

PLANS = {
    '1_month': {'name': '1 месяц', 'days': 30, 'price': 199},
    '2_months': {'name': '2 месяца', 'days': 60, 'price': 378},
    '3_months': {'name': '3 месяца', 'days': 90, 'price': 567},
    '6_months': {'name': '6 месяцев', 'days': 180, 'price': 1134},
    '12_months': {'name': '12 месяцев', 'days': 360, 'price': 2268},
}

PLAN_LABELS = {
    'free_trial': 'Бесплатный период (3 дня)',
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
    if flask_app:
        return flask_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')
    return os.getenv('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')


def sub_link(sub):
    return f"{base_url()}/sub/{sub.sub_token}"


def generate_qr_image(data, token):
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#10b981", back_color="#0a0a0f")

    static_dir = os.path.join(flask_app.root_path, 'static', 'qr') if flask_app else 'app/static/qr'
    os.makedirs(static_dir, exist_ok=True)
    filepath = os.path.join(static_dir, f"qr_{token}.png")
    img.save(filepath)
    return filepath


def get_required_channel():
    """
    Returns (channel_handle_or_id, channel_url) or (None, None).
    """
    channel = None
    url = None
    if flask_app:
        with flask_app.app_context():
            channel = AppSetting.get('REQUIRED_CHANNEL') or flask_app.config.get('REQUIRED_CHANNEL')
            url = AppSetting.get('REQUIRED_CHANNEL_URL') or flask_app.config.get('REQUIRED_CHANNEL_URL')
    if not channel:
        channel = os.getenv('REQUIRED_CHANNEL')
    if not url:
        url = os.getenv('REQUIRED_CHANNEL_URL')

    if channel and str(channel).strip():
        channel_str = str(channel).strip()
        if not url:
            if channel_str.startswith('@'):
                url = f"https://t.me/{channel_str.lstrip('@')}"
            else:
                url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/"
        return channel_str, str(url).strip()
    return None, None


async def is_user_subscribed_to_channel(user_id, bot):
    """
    Checks whether user is subscribed to the mandatory channel (ОП).
    Admins automatically bypass check.
    If no channel is configured, returns True.
    """
    if user_id in ADMIN_IDS:
        return True

    channel, url = get_required_channel()
    if not channel:
        return True

    if not bot:
        return True

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        if member.status in ['creator', 'administrator', 'member', 'restricted']:
            return True
        return False
    except Exception as e:
        print(f"[Bot] Mandatory channel check ({channel}, user {user_id}): {e}")
        return False


def get_channel_gate_keyboard(channel_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на наш канал", url=channel_url)],
        [InlineKeyboardButton("✅ Я подписался (Проверить)", callback_data="check_channel_sub")],
    ])


def get_channel_gate_text(channel_handle, channel_url):
    return (
        "📢 <b>Обязательная подписка на канал!</b>\n\n"
        "Для использования бота, получения <b>3 дней бесплатного доступа</b> "
        "и актуальных серверов VoltaVPN, пожалуйста, подпишитесь на наш официальный новостной канал:\n\n"
        f"👉 <b>Канал:</b> <a href=\"{esc(channel_url)}\">{esc(channel_handle)}</a>\n\n"
        "После подписки нажмите кнопку <b>«✅ Я подписался»</b> ниже, чтобы разблокировать доступ!"
    )


def get_main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton("⚡ Подключиться"), KeyboardButton("👤 Моя подписка")],
        [KeyboardButton("💳 Купить / Продлить"), KeyboardButton("📥 QR / Ссылка")],
        [KeyboardButton("📖 Инструкция"), KeyboardButton("❓ Частые вопросы")],
        [KeyboardButton("🎁 Пригласить друзей"), KeyboardButton("💬 Поддержка")],
        [KeyboardButton("📜 Соглашение & No-Logs"), KeyboardButton("📊 Статус")],
    ]
    if is_admin:
        keyboard.append([KeyboardButton("🛠 Админ-панель"), KeyboardButton("📩 Чаты поддержки")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


class UserCtx:
    __slots__ = ('id', 'telegram_id', 'username', 'is_admin', 'login_token', 'ref_code')

    def __init__(self, user):
        self.id = user.id
        self.telegram_id = user.telegram_id
        self.username = user.username
        self.is_admin = bool(user.is_admin)
        self.login_token = user.login_token
        self.ref_code = getattr(user, 'ref_code', None)


class SubCtx:
    __slots__ = ('id', 'plan', 'sub_token', 'config_link', 'end_date',
                 'is_active', '_days_left', '_expired', '_time_str')

    def __init__(self, sub):
        self.id = sub.id
        self.plan = sub.plan
        self.sub_token = sub.sub_token
        self.config_link = sub.config_link
        self.end_date = sub.end_date
        self.is_active = sub.is_active
        self._days_left = sub.days_left()
        self._expired = sub.is_expired()
        self._time_str = sub.time_left_str()

    def days_left(self):
        return self._days_left

    def is_expired(self):
        return self._expired

    def time_str(self):
        return self._time_str


def get_or_create_user(telegram_user, auto_trial=True):
    """
    Returns (UserCtx, SubCtx|None). Automatically provisions 3-day free trial on first start.
    """
    with flask_app.app_context():
        user = User.query.filter_by(telegram_id=telegram_user.id).first()
        is_new = False
        if not user:
            user = User(
                telegram_id=telegram_user.id,
                telegram_verified=True,
                username=telegram_user.username or f"user_{telegram_user.id}",
                email=None,
                login_token=uuid.uuid4().hex,
                ref_code=uuid.uuid4().hex[:10],
                is_admin=telegram_user.id in ADMIN_IDS,
            )
            db.session.add(user)
            db.session.commit()
            is_new = True
        else:
            if not user.telegram_verified:
                user.telegram_verified = True
            if telegram_user.id in ADMIN_IDS and not user.is_admin:
                user.is_admin = True
            if not user.login_token:
                user.login_token = uuid.uuid4().hex
            if not user.ref_code:
                user.ref_code = uuid.uuid4().hex[:10]
            db.session.commit()

        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()

        # Auto grant 3-day trial if user has no subscription and hasn't used trial
        if auto_trial and not sub and not user.is_trial_used:
            sub_token = uuid.uuid4().hex
            config_link = f"{base_url()}/sub/{sub_token}"
            qr_path = None
            try:
                p = generate_qr_image(config_link, sub_token)
                qr_path = os.path.relpath(p, os.path.join(flask_app.root_path, 'static')).replace('\\', '/')
            except Exception:
                qr_path = None

            sub = Subscription(
                user_id=user.id,
                plan='free_trial',
                sub_token=sub_token,
                start_date=datetime.utcnow(),
                end_date=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
                config_link=config_link,
                qr_code_path=qr_path,
                is_active=True,
                payment_status='paid',
            )
            db.session.add(sub)
            user.is_trial_used = True
            db.session.commit()

        if sub:
            fresh = f"{base_url()}/sub/{sub.sub_token}"
            if sub.config_link != fresh:
                sub.config_link = fresh
                db.session.commit()

        return UserCtx(user), (SubCtx(sub) if sub else None)


def notify_admins_support(msg):
    """
    Sends notification to Telegram bot admins about a new support message with quick reply button.
    """
    if not bot_app or not ADMIN_IDS:
        return

    text_preview = (msg.text[:300] + '...') if len(msg.text) > 300 else msg.text
    email_line = f"📧 <b>Email:</b> {esc(msg.sender_email)}\n" if getattr(msg, 'sender_email', None) else ""
    notify_text = (
        f"📩 <b>Новое сообщение в поддержку сайта!</b>\n\n"
        f"👤 <b>От:</b> {esc(msg.sender_name)} ({esc(msg.sender_type)})\n"
        f"{email_line}"
        f"🔑 <b>Сессия:</b> <code>{esc(msg.session_id)}</code>\n"
        f"💬 <b>Текст:</b>\n<i>{esc(text_preview)}</i>\n\n"
        f"👉 <b>Ответить:</b> нажмите кнопку ниже или введите:\n<code>/reply {esc(msg.session_id)} Ваш ответ</code>"
    )

    # Inline button for 1-tap admin reply
    safe_sess = msg.session_id[:40]
    reply_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить на сообщение", callback_data=f"rep_{safe_sess}")]
    ])

    async def _send():
        for admin_id in ADMIN_IDS:
            try:
                await bot_app.bot.send_message(
                    chat_id=admin_id,
                    text=notify_text,
                    parse_mode='HTML',
                    reply_markup=reply_kb,
                )
            except Exception as e:
                print(f"[Bot] Failed to send support alert to {admin_id}: {e}")

    # Schedule sending in bot loop
    try:
        loop = getattr(bot_app, '_custom_loop', None)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), loop)
        else:
            threading.Thread(target=lambda: asyncio.run(_send()), daemon=True).start()
    except Exception as e:
        print(f"[Bot] Error dispatching support alert: {e}")


def dispatch_expiry_checks():
    """
    Triggered periodically by scheduler to notify users before/upon expiration.
    """
    if not bot_app:
        return

    async def _run():
        await check_expiry_and_notify_users()

    try:
        loop = getattr(bot_app, '_custom_loop', None)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_run(), loop)
        else:
            threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()
    except Exception as e:
        print(f"[Bot] Expiry check dispatch error: {e}")


async def check_expiry_and_notify_users():
    """
    Checks active subscriptions of verified Telegram users and sends renewal alerts.
    """
    if not bot_app or not flask_app:
        return

    with flask_app.app_context():
        now = datetime.utcnow()
        # 1. 24h warning
        threshold_24h = now + timedelta(hours=24)
        subs_24h = (
            Subscription.query.join(User)
            .filter(
                Subscription.is_active == True,
                Subscription.end_date > now,
                Subscription.end_date <= threshold_24h,
                Subscription.notified_24h == False,
                User.telegram_id.isnot(None),
                User.telegram_verified == True,
            )
            .all()
        )

        for sub in subs_24h:
            hours_left = max(1, int((sub.end_date - now).total_seconds() // 3600))
            tg_id = sub.user.telegram_id
            msg_text = (
                f"⏳ <b>Внимание! Ваша подписка VoltaVPN заканчивается через {hours_left} ч.</b>\n\n"
                f"Тариф: <b>{sub.plan}</b>\n"
                f"Окончание: <b>{sub.end_date.strftime('%d.%m.%Y %H:%M')} UTC</b>\n\n"
                f"Продлите подписку, чтобы сохранить бесперебойный доступ к VPN:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Продлить подписку", callback_data="buy_menu")],
                [InlineKeyboardButton("🌐 Личный кабинет", url=f"{base_url()}/dashboard")],
            ])
            try:
                await bot_app.bot.send_message(chat_id=tg_id, text=msg_text, parse_mode='HTML', reply_markup=kb)
                sub.notified_24h = True
            except Exception as e:
                print(f"[Bot] Expiry 24h notice failed for {tg_id}: {e}")

        # 2. Expired notification
        subs_exp = (
            Subscription.query.join(User)
            .filter(
                Subscription.is_active == True,
                Subscription.end_date <= now,
                Subscription.notified_expired == False,
                User.telegram_id.isnot(None),
                User.telegram_verified == True,
            )
            .all()
        )

        for sub in subs_exp:
            tg_id = sub.user.telegram_id
            msg_text = (
                f"⚠️ <b>Срок действия вашей подписки VoltaVPN истек!</b>\n\n"
                f"Серверы временно отключены. Чтобы возобновить работу, нажмите кнопку продления ниже:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Возобновить подписку", callback_data="buy_menu")],
                [InlineKeyboardButton("🌐 Открыть сайт", url=base_url())],
            ])
            try:
                await bot_app.bot.send_message(chat_id=tg_id, text=msg_text, parse_mode='HTML', reply_markup=kb)
                sub.notified_expired = True
            except Exception as e:
                print(f"[Bot] Expired notice failed for {tg_id}: {e}")

        db.session.commit()


# ----------------------------- Bot Handlers -----------------------------
async def check_channel_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    is_subbed = await is_user_subscribed_to_channel(user.id, bot_inst)

    if not is_subbed and user.id not in ADMIN_IDS:
        channel, url = get_required_channel()
        await query.answer(
            f"❌ Вы еще не подписались на наш канал!\nПожалуйста, подпишитесь на {channel} и повторите проверку.",
            show_alert=True
        )
        return

    await query.answer("🎉 Спасибо за подписку! Доступ к VoltaVPN разблокирован.", show_alert=True)
    db_user, sub = get_or_create_user(user, auto_trial=True)
    is_admin = db_user.is_admin or user.id in ADMIN_IDS

    sub_status = "🟢 <b>Активна (3 дня бесплатно)</b>" if sub and not sub.is_expired() else "⚪ Нет активной подписки"
    days_text = f"Осталось времени: <b>{sub.time_str()}</b>" if sub and not sub.is_expired() else ""

    welcome_msg = (
        f"⚡ <b>Добро пожаловать в VOLTA VPN!</b>\n\n"
        f"Молниеносный VPN с автоматической проверкой и обновлением серверов каждый час.\n\n"
        f"📊 <b>Ваш статус:</b> {sub_status}\n"
        f"{days_text}\n\n"
        f"Используйте кнопки меню ниже для подключения и управления подпиской."
    )
    try:
        await query.edit_message_text(welcome_msg, parse_mode='HTML')
    except Exception:
        pass
    if context and context.bot:
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text="👇 Меню управления VoltaVPN:",
                reply_markup=get_main_keyboard(is_admin)
            )
        except Exception:
            pass


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    is_admin = user.id in ADMIN_IDS

    # Check mandatory channel subscription
    if not is_admin:
        is_subbed = await is_user_subscribed_to_channel(user.id, bot_inst)
        if not is_subbed:
            get_or_create_user(user, auto_trial=False)
            channel, url = get_required_channel()
            await update.message.reply_text(
                get_channel_gate_text(channel, url),
                parse_mode='HTML',
                reply_markup=get_channel_gate_keyboard(url),
                disable_web_page_preview=True
            )
            return

    # Handle web link token /start link_<code>
    if args and args[0].startswith("link_"):
        link_code = args[0].replace("link_", "")
        with flask_app.app_context():
            web_user = User.query.filter_by(link_code=link_code).first()
            if web_user:
                web_user.telegram_id = user.id
                web_user.telegram_verified = True
                if not web_user.is_trial_used:
                    from app.routes import grant_trial
                    grant_trial(web_user, telegram_id=user.id, days=TRIAL_DAYS)
                db.session.commit()

                await update.message.reply_text(
                    f"🎉 <b>Telegram успешно привязан к аккаунту {esc(web_user.username)}!</b>\n\n"
                    f"Ваш бесплатный период на 3 дня активен. Можете вернуться на сайт или управлять подпиской прямо здесь.",
                    parse_mode='HTML',
                    reply_markup=get_main_keyboard(is_admin=web_user.is_admin or user.id in ADMIN_IDS)
                )
                return

    db_user, sub = get_or_create_user(user, auto_trial=True)
    is_admin = db_user.is_admin or user.id in ADMIN_IDS

    sub_status = "🟢 <b>Активна (3 дня бесплатно)</b>" if sub and not sub.is_expired() else "⚪ Нет активной подписки"
    days_text = f"Осталось времени: <b>{sub.time_str()}</b>" if sub and not sub.is_expired() else ""

    welcome_msg = (
        f"⚡ <b>Добро пожаловать в VOLTA VPN!</b>\n\n"
        f"Молниеносный VPN с автоматической проверкой и обновлением серверов каждый час.\n\n"
        f"📊 <b>Ваш статус:</b> {sub_status}\n"
        f"{days_text}\n\n"
        f"Используйте кнопки меню ниже для подключения и управления подпиской."
    )
    await update.message.reply_text(welcome_msg, parse_mode='HTML', reply_markup=get_main_keyboard(is_admin))


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    if user.id not in ADMIN_IDS and not await is_user_subscribed_to_channel(user.id, bot_inst):
        channel, url = get_required_channel()
        await update.message.reply_text(
            get_channel_gate_text(channel, url),
            parse_mode='HTML',
            reply_markup=get_channel_gate_keyboard(url),
            disable_web_page_preview=True
        )
        return

    db_user, sub = get_or_create_user(user, auto_trial=True)

    if not sub or sub.is_expired():
        keyboard = [
            [InlineKeyboardButton("💳 Выбрать тариф", callback_data="buy_menu")],
            [InlineKeyboardButton("🌐 Открыть сайт", url=base_url())],
        ]
        await update.message.reply_text(
            "⚠️ <b>У вас нет активной подписки.</b>\n\nВыберите тариф для продолжения работы:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    link = sub_link(sub)
    web_login_url = f"{base_url()}/tg-login/{db_user.login_token}" if db_user.login_token else base_url()
    sub_tok = sub.sub_token

    keyboard = [
        [InlineKeyboardButton("📱 Karing (iOS / Android)", url=f"{base_url()}/open/karing/{sub_tok}")],
        [InlineKeyboardButton("🤖 v2rayNG (Android)", url=f"{base_url()}/open/v2rayng/{sub_tok}")],
        [InlineKeyboardButton("🍏 Streisand (iOS / Mac)", url=f"{base_url()}/open/streisand/{sub_tok}")],
        [InlineKeyboardButton("🛡 Hiddify (Windows / Android / Mac)", url=f"{base_url()}/open/hiddify/{sub_tok}")],
        [InlineKeyboardButton("🌐 Войти в личный кабинет на сайте", url=web_login_url)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        f"⚡ <b>Ваша подписка VoltaVPN активна!</b>\n\n"
        f"⏳ Срок действия: <b>{sub.time_str()}</b>\n"
        f"📱 Разрешено устройств: <b>до 3 устройств одновременно</b>\n\n"
        f"🔗 <b>Ссылка подписки (нажмите для копирования):</b>\n"
        f"<code>{esc(link)}</code>\n\n"
        f"💡 Нажмите на кнопку вашего приложения ниже для быстрого импорта:"
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)


async def my_sub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    if user.id not in ADMIN_IDS and not await is_user_subscribed_to_channel(user.id, bot_inst):
        channel, url = get_required_channel()
        await update.message.reply_text(
            get_channel_gate_text(channel, url),
            parse_mode='HTML',
            reply_markup=get_channel_gate_keyboard(url),
            disable_web_page_preview=True
        )
        return

    db_user, sub = get_or_create_user(user, auto_trial=False)

    if not sub:
        await update.message.reply_text(
            "У вас пока нет активной подписки. Нажмите «⚡ Подключиться» или «💳 Купить / Продлить».",
            reply_markup=get_main_keyboard(db_user.is_admin)
        )
        return

    status = "🟢 Активна" if not sub.is_expired() else "🔴 Истекла"
    plan_name = PLAN_LABELS.get(sub.plan, sub.plan)

    msg = (
        f"👤 <b>Информация о подписке:</b>\n\n"
        f"• <b>Тариф:</b> {esc(plan_name)}\n"
        f"• <b>Статус:</b> {status}\n"
        f"• <b>Осталось:</b> {sub.time_str()}\n"
        f"• <b>Окончание:</b> {sub.end_date.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(sub_link(sub))}</code>"
    )
    keyboard = [
        [InlineKeyboardButton("💳 Продлить подписку", callback_data="buy_menu")],
        [InlineKeyboardButton("📥 Получить QR-код", callback_data="get_qr")],
    ]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def qr_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    if user.id not in ADMIN_IDS and not await is_user_subscribed_to_channel(user.id, bot_inst):
        channel, url = get_required_channel()
        await update.message.reply_text(
            get_channel_gate_text(channel, url),
            parse_mode='HTML',
            reply_markup=get_channel_gate_keyboard(url),
            disable_web_page_preview=True
        )
        return

    db_user, sub = get_or_create_user(user, auto_trial=True)

    if not sub:
        await update.message.reply_text("Сначала активируйте подписку через «⚡ Подключиться».")
        return

    link = sub_link(sub)
    try:
        qr_file = generate_qr_image(link, sub.sub_token)
        with open(qr_file, 'rb') as f:
            await update.message.reply_photo(
                photo=f,
                caption=(
                    f"📥 <b>QR-код вашей подписки VOLTA</b>\n\n"
                    f"Отсканируйте камерой в приложении Karing, Streisand или v2rayN.\n\n"
                    f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>"
                ),
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(
            f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>",
            parse_mode='HTML'
        )


async def buy_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    if user and user.id not in ADMIN_IDS and not await is_user_subscribed_to_channel(user.id, bot_inst):
        channel, url = get_required_channel()
        if update.callback_query:
            await update.callback_query.answer("⚠️ Требуется подписка на канал!", show_alert=True)
            await update.callback_query.edit_message_text(
                get_channel_gate_text(channel, url),
                parse_mode='HTML',
                reply_markup=get_channel_gate_keyboard(url),
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(
                get_channel_gate_text(channel, url),
                parse_mode='HTML',
                reply_markup=get_channel_gate_keyboard(url),
                disable_web_page_preview=True
            )
        return

    keyboard = [
        [InlineKeyboardButton("1 месяц — 199 ₽", callback_data="plan_1_month")],
        [InlineKeyboardButton("2 месяца — 378 ₽ (-5%)", callback_data="plan_2_months")],
        [InlineKeyboardButton("3 месяца — 567 ₽ (-5%)", callback_data="plan_3_months")],
        [InlineKeyboardButton("6 месяцев — 1134 ₽ (-5%)", callback_data="plan_6_months")],
        [InlineKeyboardButton("12 месяцев — 2268 ₽ (-5%)", callback_data="plan_12_months")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        "💎 <b>Выберите подходящий тарифный план:</b>\n\n"
        "• Все протоколы: VLESS Reality, Trojan, Shadowsocks, Hysteria2\n"
        "• Серверы в Германии, Нидерландах, США, Финляндии, Швеции и др.\n"
        "• Автоматическая проверка задержки и обновление каждый час\n"
        "• Оплата банковскими картами (МИР, Visa, MC), СБП, ЮMoney, Криптовалютой"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)


async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buy_menu":
        await buy_menu_command(update, context)
        return
    if query.data == "get_qr":
        user = update.effective_user
        db_user, sub = get_or_create_user(user, auto_trial=True)
        if sub:
            link = sub_link(sub)
            qr_file = generate_qr_image(link, sub.sub_token)
            with open(qr_file, 'rb') as f:
                await query.message.reply_photo(
                    photo=f,
                    caption=f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>",
                    parse_mode='HTML'
                )
        return

    plan_id = query.data.replace("plan_", "")
    plan = PLANS.get(plan_id)
    if not plan:
        await query.edit_message_text("Неверный тариф.")
        return

    keyboard = [
        [InlineKeyboardButton("💳 Банковские карты / СБП / ЮMoney (YooMoney)", callback_data=f"pay_yoomoney_{plan_id}")],
        [InlineKeyboardButton("💳 Platega.io (Карты / СБП)", callback_data=f"pay_platega_{plan_id}")],
        [InlineKeyboardButton("💎 CryptoBot (@CryptoBot)", callback_data=f"pay_cryptobot_{plan_id}")],
        [InlineKeyboardButton("⬅️ Назад к тарифам", callback_data="buy_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"Тариф: <b>{esc(plan['name'])}</b> ({plan['price']} ₽)\n\nВыберите удобный способ оплаты:",
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
    db_user, sub = get_or_create_user(user, auto_trial=False)

    payment_url = None
    ext_id = None
    try:
        with flask_app.app_context():
            if method == 'yoomoney':
                payment_url, ext_id = create_yoomoney_payment(db_user, plan, sub.id if sub else 0)
                method_name = "YooMoney (Карты / СБП)"
            elif method == 'platega':
                payment_url, ext_id = create_platega_payment(db_user, plan, sub.id if sub else 0)
                method_name = "Platega.io"
            else:
                payment_url, ext_id = create_cryptobot_payment(db_user, plan, sub.id if sub else 0)
                method_name = "CryptoBot"
    except Exception as e:
        print(f"[Bot] Payment creation error: {e}")

    if not payment_url:
        await query.edit_message_text(
            "⚠️ Платёжный шлюз временно недоступен. Попробуйте другой способ оплаты.",
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
        f"• <b>Способ:</b> {esc(method_name)}\n\n"
        f"Нажмите кнопку оплаты, а затем — «Проверить оплату».",
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
        method = payment.payment_method if payment else 'yoomoney'
        status = check_payment_status(ext_id, method)

        if status == 'paid':
            db_user, sub = get_or_create_user(user, auto_trial=False)
            link = sub_link(sub) if sub else base_url()
            await query.edit_message_text(
                f"🎉 <b>Оплата успешно подтверждена!</b>\n\n"
                f"Ваша подписка продлена. Осталось времени: <b>{sub.time_str() if sub else '30 дней'}</b>\n\n"
                f"🔗 <b>Ссылка подписки:</b>\n<code>{esc(link)}</code>",
                parse_mode='HTML'
            )
        else:
            await query.answer("⌛ Оплата еще не поступила. Попробуйте через 1-2 минуты.", show_alert=True)


async def reply_support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command: /reply <session_id> <message text>
    """
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: <code>/reply &lt;session_id&gt; &lt;текст ответа&gt;</code>",
            parse_mode='HTML'
        )
        return

    session_id = args[0]
    reply_text = " ".join(args[1:])

    with flask_app.app_context():
        msg = SupportMessage(
            session_id=session_id,
            user_id=None,
            sender_type='admin',
            sender_name=f"Поддержка VOLTA ({user.username or user.first_name})",
            text=reply_text,
            is_read=True,
        )
        db.session.add(msg)
        # Mark previous user messages in session as read
        SupportMessage.query.filter_by(session_id=session_id, sender_type='user').update({'is_read': True})
        SupportMessage.query.filter_by(session_id=session_id, sender_type='guest').update({'is_read': True})
        db.session.commit()

    await update.message.reply_text(
        f"✅ <b>Ответ отправлен в чат поддержки!</b>\n\n"
        f"🔑 Сессия: <code>{esc(session_id)}</code>\n"
        f"💬 Текст: <i>{esc(reply_text)}</i>",
        parse_mode='HTML'
    )


async def chats_support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command: /chats - view active support conversations
    """
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    with flask_app.app_context():
        sessions = db.session.query(SupportMessage.session_id).distinct().limit(10).all()
        if not sessions:
            await update.message.reply_text("📭 Нет обращений в поддержку.")
            return

        lines = ["💬 <b>Последние диалоги поддержки:</b>\n"]
        for (s_id,) in sessions:
            last = SupportMessage.query.filter_by(session_id=s_id).order_by(SupportMessage.created_at.desc()).first()
            if last:
                unread = SupportMessage.query.filter_by(session_id=s_id, is_read=False).count()
                badge = f"🔴 ({unread} новых)" if unread > 0 else "🟢"
                lines.append(
                    f"{badge} <code>{esc(s_id)}</code> ({esc(last.sender_name)}):\n"
                    f"<i>{esc(last.text[:80])}</i>\n"
                    f"Ответить: <code>/reply {esc(s_id)} ...</code>\n"
                )

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with flask_app.app_context():
        email = AppSetting.get('SUPPORT_EMAIL') or flask_app.config.get('SUPPORT_EMAIL', 'support@vpn.stas-max.ru')
        telegram = AppSetting.get('SUPPORT_TELEGRAM') or flask_app.config.get('SUPPORT_TELEGRAM', '@ILSupport')

    msg = (
        "💬 <b>Служба поддержки VoltaVPN:</b>\n\n"
        f"✉️ <b>Email поддержки:</b> {esc(email)}\n"
        f"📱 <b>Telegram для связи:</b> {esc(telegram)}\n"
        f"🌐 <b>Онлайн-чат на сайте:</b> {esc(base_url())}\n\n"
        "⚡ Среднее время ответа оператора: 2–5 минут."
    )
    await update.message.reply_text(msg, parse_mode='HTML')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with flask_app.app_context():
        total = Config.query.filter_by(is_working=True).count()
        vless = Config.query.filter_by(is_working=True, protocol='vless').count()
        ss = Config.query.filter_by(is_working=True, protocol='ss').count()
        trojan = Config.query.filter_by(is_working=True, protocol='trojan').count()
        hy2 = Config.query.filter_by(is_working=True, protocol='hysteria2').count()

    msg = (
        "📊 <b>Мониторинг серверов VOLTA</b>\n\n"
        f"✅ <b>Всего рабочих серверов:</b> {total}\n"
        f"⚡ <b>VLESS Reality:</b> {vless}\n"
        f"🚀 <b>Shadowsocks:</b> {ss}\n"
        f"🔒 <b>Trojan:</b> {trojan}\n"
        f"🔥 <b>Hysteria2:</b> {hy2}\n\n"
        "🔄 Автообновление и перепроверка пинга выполняются каждый час."
    )
    await update.message.reply_text(msg, parse_mode='HTML')


async def instructions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detailed multi-platform connection guide with interactive inline menus.
    """
    user = update.effective_user
    db_user, sub = get_or_create_user(user, auto_trial=True)
    web_instructions_url = f"{base_url()}/instructions"

    keyboard = [
        [
            InlineKeyboardButton("📱 iPhone / iPad (iOS)", callback_data="guide_ios"),
            InlineKeyboardButton("🤖 Android", callback_data="guide_android"),
        ],
        [
            InlineKeyboardButton("💻 Windows", callback_data="guide_windows"),
            InlineKeyboardButton("🍏 macOS", callback_data="guide_mac"),
        ],
        [
            InlineKeyboardButton("📺 Android TV / Роутеры", callback_data="guide_tv"),
        ],
        [
            InlineKeyboardButton("🌐 Полная иллюстрированная инструкция на сайте", url=web_instructions_url),
        ],
    ]

    msg = (
        "📖 <b>Инструкции по настройке VoltaVPN:</b>\n\n"
        "Выберите вашу платформу ниже, чтобы получить пошаговую инструкцию с ссылками на скачивание и кнопками быстрого импорта:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    db_user, sub = get_or_create_user(user, auto_trial=True)

    link = sub_link(sub) if sub else f"{base_url()}/sub/public"
    sub_tok = sub.sub_token if sub else "public"

    back_btn = InlineKeyboardButton("« Назад к выбору устройства", callback_data="guide_main")

    if data == "guide_ios":
        text = (
            "📱 <b>Инструкция для iOS (iPhone / iPad):</b>\n\n"
            "1️⃣ <b>Установите приложение из App Store:</b>\n"
            "   • Рекомендуем <b>Karing</b> или <b>Streisand</b>.\n\n"
            "2️⃣ <b>Добавьте подписку:</b>\n"
            "   • Нажмите кнопку быстрого импорта ниже или скопируйте ссылку.\n\n"
            "3️⃣ <b>Включите VPN:</b>\n"
            "   • В приложении нажмите «Подключить» и разрешите добавление VPN-конфигурации.\n\n"
            f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>"
        )
        kb = [
            [InlineKeyboardButton("⚡ Импорт в Karing (1 клик)", url=f"{base_url()}/open/karing/{sub_tok}")],
            [InlineKeyboardButton("📥 Импорт в Streisand", url=f"{base_url()}/open/streisand/{sub_tok}")],
            [InlineKeyboardButton("🍎 Скачать Karing в App Store", url="https://apps.apple.com/app/karing/id6472431552")],
            [InlineKeyboardButton("🍎 Скачать Streisand в App Store", url="https://apps.apple.com/app/streisand/id6450534064")],
            [back_btn],
        ]
    elif data == "guide_android":
        text = (
            "🤖 <b>Инструкция для Android:</b>\n\n"
            "1️⃣ <b>Установите приложение:</b>\n"
            "   • <b>v2rayNG</b> (из Google Play или GitHub) или <b>Karing</b>.\n\n"
            "2️⃣ <b>Добавьте подписку:</b>\n"
            "   • Нажмите кнопку быстрого импорта ниже.\n\n"
            "3️⃣ <b>Обновите подписку и подключитесь:</b>\n"
            "   • Нажмите 3 точки в углу экрана → «Обновить подписку» → выберите сервер и нажмите кнопку подключения.\n\n"
            f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>"
        )
        kb = [
            [InlineKeyboardButton("⚡ Импорт в v2rayNG (1 клик)", url=f"{base_url()}/open/v2rayng/{sub_tok}")],
            [InlineKeyboardButton("⚡ Импорт в Karing (1 клик)", url=f"{base_url()}/open/karing/{sub_tok}")],
            [InlineKeyboardButton("🤖 Скачать v2rayNG (Google Play)", url="https://play.google.com/store/apps/details?id=com.v2ray.ang")],
            [back_btn],
        ]
    elif data == "guide_windows":
        text = (
            "💻 <b>Инструкция для Windows (10 / 11):</b>\n\n"
            "1️⃣ <b>Скачайте Hiddify или v2rayN:</b>\n"
            "   • Hiddify — самый удобный современный клиент с русским интерфейсом.\n\n"
            "2️⃣ <b>Добавьте подписку:</b>\n"
            "   • В Hiddify нажмите «+ Новый профиль» → «Добавить из буфера».\n\n"
            "3️⃣ <b>Включите режим VPN (TUN):</b>\n"
            "   • Нажмите кнопку «Подключить» в центре экрана.\n\n"
            f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>"
        )
        kb = [
            [InlineKeyboardButton("🛡 Открыть в Hiddify", url=f"{base_url()}/open/hiddify/{sub_tok}")],
            [InlineKeyboardButton("💻 Скачать Hiddify (GitHub)", url="https://github.com/hiddify/hiddify-next/releases")],
            [InlineKeyboardButton("💻 Скачать v2rayN (GitHub)", url="https://github.com/2dust/v2rayN/releases")],
            [back_btn],
        ]
    elif data == "guide_mac":
        text = (
            "🍏 <b>Инструкция для macOS:</b>\n\n"
            "1️⃣ <b>Установите Streisand или Hiddify:</b>\n"
            "   • Streisand доступен прямо в Mac App Store.\n\n"
            "2️⃣ <b>Добавьте подписку:</b>\n"
            "   • В приложении нажмите «+» → «Add Subscription» и вставьте ссылку.\n\n"
            f"🔗 <b>Ваша ссылка подписки:</b>\n<code>{esc(link)}</code>"
        )
        kb = [
            [InlineKeyboardButton("🍏 Открыть в Streisand", url=f"{base_url()}/open/streisand/{sub_tok}")],
            [InlineKeyboardButton("🍏 Скачать Streisand (Mac App Store)", url="https://apps.apple.com/app/streisand/id6450534064")],
            [InlineKeyboardButton("💻 Скачать Hiddify DMG", url="https://github.com/hiddify/hiddify-next/releases")],
            [back_btn],
        ]
    else:  # guide_tv
        text = (
            "📺 <b>Инструкция для Android TV и Роутеров:</b>\n\n"
            "• <b>Android TV:</b> Установите v2rayNG или Hiddify из магазина ТВ и отсканируйте QR-код вашей подписки.\n"
            "• <b>Роутеры Keenetic / OpenWrt:</b> Поддерживаются протоколы VLESS Reality и Shadowsocks-2022.\n\n"
            "💬 Напишите нам в поддержку @ILSupport, если нужна помощь с настройкой роутера!"
        )
        kb = [
            [InlineKeyboardButton("📥 Получить QR-код для ТВ", callback_data="get_qr")],
            [InlineKeyboardButton("💬 Написать в поддержку", url="https://t.me/ILSupport")],
            [back_btn],
        ]

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))


async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    FAQ menu in Telegram bot.
    """
    web_faq_url = f"{base_url()}/faq"
    keyboard = [
        [
            InlineKeyboardButton("🛡️ No-Logs & Безопасность", callback_data="faq_sec"),
            InlineKeyboardButton("💳 Оплата & Возврат", callback_data="faq_pay"),
        ],
        [
            InlineKeyboardButton("⚡ Скорость & YouTube 4K", callback_data="faq_speed"),
            InlineKeyboardButton("🔄 Автообновление серверов", callback_data="faq_update"),
        ],
        [
            InlineKeyboardButton("🌐 Открыть полную базу знаний FAQ на сайте", url=web_faq_url),
        ],
    ]

    msg = (
        "❓ <b>Часто задаваемые вопросы (FAQ) VoltaVPN:</b>\n\n"
        "Выберите интересующий вас раздел или перейдите на страницу базы знаний:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def faq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    back_btn = InlineKeyboardButton("« Назад к разделам FAQ", callback_data="faq_main")

    if data == "faq_sec":
        text = (
            "🛡️ <b>Безопасность и политика No-Logs:</b>\n\n"
            "• <b>Ведутся ли логи?</b> Категорически нет! Мы не отслеживаем и не храним историю сайтов, DNS-запросы и IP-адреса.\n"
            "• <b>Почему VLESS Reality не блокируется?</b> Трафик маскируется под обычный защищенный HTTPS-трафик крупных ресурсов (Apple, Microsoft), исключая блокировку провайдерами и ТСПУ."
        )
    elif data == "faq_pay":
        text = (
            "💳 <b>Оплата и возврат средств:</b>\n\n"
            "• <b>Как оплатить?</b> Банковские карты РФ (МИР, Visa, MC), СБП, ЮMoney, Криптовалюта.\n"
            "• <b>Есть ли пробный период?</b> Да, 3 дня бесплатно при регистрации без ввода карты!\n"
            "• <b>Гарантия возврата:</b> 100% возврат средств в течение 14 дней, если сервис вам не подошел."
        )
    elif data == "faq_speed":
        text = (
            "⚡ <b>Скорость и работа сервисов:</b>\n\n"
            "• <b>YouTube 4K:</b> Серверы подключены к портам до 10 Гбит/с — видео открывается мгновенно без зависаний.\n"
            "• <b>Лимиты:</b> Безлимитный трафик на всех тарифах без ограничений по скорости.\n"
            "• <b>Устройства:</b> До 3 устройств одновременно на одну подписку (по дефолту)."
        )
    else:  # faq_update
        text = (
            "🔄 <b>Автообновление серверов:</b>\n\n"
            "• Наш сервер каждый час тестирует сетевую доступность всех узлов.\n"
            "• Ваше приложение автоматически обновляет список серверов в фоновом режиме (заголовок Update-Interval: 1 час).\n"
            "• Вам не нужно ничего перенастраивать вручную!"
        )

    kb = [[back_btn]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Terms of service and Privacy Policy in Telegram bot.
    """
    web_terms_url = f"{base_url()}/terms"
    web_privacy_url = f"{base_url()}/privacy"

    keyboard = [
        [
            InlineKeyboardButton("📜 Пользовательское соглашение", url=web_terms_url),
            InlineKeyboardButton("🔒 Политика конфиденциальности", url=web_privacy_url),
        ],
        [
            InlineKeyboardButton("💬 Связаться с поддержкой", callback_data="buy_menu"),
        ]
    ]

    msg = (
        "📜 <b>Пользовательское соглашение и No-Logs политика VoltaVPN:</b>\n\n"
        "1️⃣ <b>100% No-Logs:</b> Мы никогда не логируем вашу сетевую активность, трафик и историю посещений.\n"
        "2️⃣ <b>Гарантия возврата:</b> Полный возврат средств в течение 14 дней по первому запросу.\n"
        "3️⃣ <b>Прозрачные условия:</b> 3 дня бесплатного тестового периода без привязки карт.\n"
        "4️⃣ <b>Безопасные платежи:</b> Все платежи обрабатываются через защищенные шлюзы (ЮMoney, СБП, Банковские карты РФ, CryptoBot) по стандарту PCI DSS.\n\n"
        "Ознакомьтесь с полными текстами документов по кнопкам ниже:"
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await instructions_command(update, context)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    with flask_app.app_context():
        total_users = User.query.count()
        now = datetime.utcnow()
        active_subs = Subscription.query.filter(Subscription.is_active == True, Subscription.end_date > now).count()
        revenue = sum(p.amount for p in Payment.query.filter_by(status='paid').all())
        working = Config.query.filter_by(is_working=True).count()
        total_configs = Config.query.count()

    msg = (
        f"🛠 <b>Админ-панель VOLTA</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"⚡ Активных подписок: <b>{active_subs}</b>\n"
        f"💰 Выручка всего: <b>{revenue} ₽</b>\n"
        f"🌐 Рабочих серверов: <b>{working}</b> из <b>{total_configs}</b>\n\n"
        f"🔗 Веб-панель: {esc(base_url())}/admin"
    )
    await update.message.reply_text(msg, parse_mode='HTML')


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_inst = context.bot if context else (bot_app.bot if bot_app else None)
    if user.id not in ADMIN_IDS and not await is_user_subscribed_to_channel(user.id, bot_inst):
        channel, url = get_required_channel()
        await update.message.reply_text(
            get_channel_gate_text(channel, url),
            parse_mode='HTML',
            reply_markup=get_channel_gate_keyboard(url),
            disable_web_page_preview=True
        )
        return

    db_user, sub = get_or_create_user(user, auto_trial=True)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{db_user.ref_code}" if BOT_USERNAME else f"{base_url()}/r/{db_user.ref_code}"

    from urllib.parse import quote
    share_text = "⚡ Быстрый и надежный VPN для России — VoltaVPN! Забирай доступ 👇"
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(share_text, safe='')}"

    keyboard = [
        [InlineKeyboardButton("📤 Поделиться с друзьями", url=share_url)],
    ]
    msg = (
        f"🎁 <b>Реферальная программа VoltaVPN</b>\n\n"
        f"Делитесь вашей персональной ссылкой с друзьями:\n"
        f"<code>{esc(ref_link)}</code>"
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_reply_btn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await query.answer("У вас нет прав администратора.", show_alert=True)
        return

    session_id = query.data.replace('rep_', '')
    context.user_data['pending_reply_session'] = session_id

    await query.message.reply_text(
        f"✍️ <b>Режим быстрого ответа:</b>\n\n"
        f"🔑 <b>Сессия:</b> <code>{esc(session_id)}</code>\n\n"
        f"<i>Напишите текст ответа следующим сообщением — он моментально отобразится у пользователя в онлайн-чате на сайте:</i>",
        parse_mode='HTML'
    )


async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    user = update.effective_user

    # If admin was in pending reply mode via button
    if user.id in ADMIN_IDS and context.user_data.get('pending_reply_session'):
        session_id = context.user_data.pop('pending_reply_session')
        with flask_app.app_context():
            msg = SupportMessage(
                session_id=session_id,
                user_id=None,
                sender_type='admin',
                sender_name=f"Поддержка VoltaVPN ({user.username or user.first_name})",
                text=text,
                is_read=True,
            )
            db.session.add(msg)
            SupportMessage.query.filter_by(session_id=session_id, sender_type='user').update({'is_read': True})
            SupportMessage.query.filter_by(session_id=session_id, sender_type='guest').update({'is_read': True})
            db.session.commit()
        await update.message.reply_text(
            f"✅ <b>Ответ успешно отправлен в онлайн-чат!</b>\n\n"
            f"🔑 Сессия: <code>{esc(session_id)}</code>\n"
            f"💬 Текст: <i>{esc(text)}</i>",
            parse_mode='HTML'
        )
        return

    # If admin replied to a message in Telegram via swipe/reply
    if user.id in ADMIN_IDS and update.message.reply_to_message:
        replied_text = update.message.reply_to_message.text or ''
        import re
        match = re.search(r'Сессия:\s*([a-zA-Z0-9_\-]+)', replied_text)
        if match:
            session_id = match.group(1)
            with flask_app.app_context():
                msg = SupportMessage(
                    session_id=session_id,
                    user_id=None,
                    sender_type='admin',
                    sender_name=f"Поддержка VoltaVPN ({user.username or user.first_name})",
                    text=text,
                    is_read=True,
                )
                db.session.add(msg)
                SupportMessage.query.filter_by(session_id=session_id, sender_type='user').update({'is_read': True})
                SupportMessage.query.filter_by(session_id=session_id, sender_type='guest').update({'is_read': True})
                db.session.commit()
            await update.message.reply_text(f"✅ Ответ отправлен в сессию <code>{esc(session_id)}</code>.", parse_mode='HTML')
            return

    if text == "⚡ Подключиться":
        await connect_command(update, context)
    elif text == "👤 Моя подписка":
        await my_sub_command(update, context)
    elif text == "💳 Купить / Продлить":
        await buy_menu_command(update, context)
    elif text == "📥 QR / Ссылка":
        await qr_link_command(update, context)
    elif text == "🎁 Пригласить друзей":
        await invite_command(update, context)
    elif text == "📊 Статус":
        await stats_command(update, context)
    elif text in ["📖 Инструкция", "❓ Инструкция", "Инструкция"]:
        await instructions_command(update, context)
    elif text in ["❓ Частые вопросы", "FAQ", "Частые вопросы"]:
        await faq_command(update, context)
    elif text in ["📜 Соглашение & No-Logs", "📜 Соглашение", "Политика конфиденциальности", "Оферта"]:
        await terms_command(update, context)
    elif text == "💬 Поддержка":
        await support_command(update, context)
    elif text == "🛠 Админ-панель":
        await admin_command(update, context)
    elif text == "📩 Чаты поддержки":
        await chats_support_command(update, context)


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
    bot_app.add_handler(CommandHandler("invite", invite_command))
    bot_app.add_handler(CommandHandler("buy", buy_menu_command))
    bot_app.add_handler(CommandHandler("stats", stats_command))
    bot_app.add_handler(CommandHandler("admin", admin_command))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("instructions", instructions_command))
    bot_app.add_handler(CommandHandler("guide", instructions_command))
    bot_app.add_handler(CommandHandler("faq", faq_command))
    bot_app.add_handler(CommandHandler("terms", terms_command))
    bot_app.add_handler(CommandHandler("privacy", terms_command))
    bot_app.add_handler(CommandHandler("rules", terms_command))
    bot_app.add_handler(CommandHandler("support", support_command))
    bot_app.add_handler(CommandHandler("reply", reply_support_command))
    bot_app.add_handler(CommandHandler("r", reply_support_command))
    bot_app.add_handler(CommandHandler("chats", chats_support_command))

    bot_app.add_handler(CallbackQueryHandler(instructions_command, pattern=r"^guide_main$"))
    bot_app.add_handler(CallbackQueryHandler(guide_callback, pattern=r"^guide_"))
    bot_app.add_handler(CallbackQueryHandler(faq_command, pattern=r"^faq_main$"))
    bot_app.add_handler(CallbackQueryHandler(faq_callback, pattern=r"^faq_"))
    bot_app.add_handler(CallbackQueryHandler(admin_reply_btn_callback, pattern=r"^rep_"))
    bot_app.add_handler(CallbackQueryHandler(check_channel_sub_callback, pattern=r"^(check_channel_sub|check_sub)$"))
    bot_app.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^(plan_|buy_menu|get_qr)"))
    bot_app.add_handler(CallbackQueryHandler(payment_callback, pattern=r"^pay_"))
    bot_app.add_handler(CallbackQueryHandler(check_pay_callback, pattern=r"^checkpay_"))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))

    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_app._custom_loop = loop
        try:
            print("[Bot] Telegram Bot polling started successfully.")
            bot_app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,
                stop_signals=None,
            )
        except Exception as e:
            print(f"[Bot] Polling notice: {e}")

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
