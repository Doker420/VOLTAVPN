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


def get_main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton("⚡ Подключиться"), KeyboardButton("👤 Моя подписка")],
        [KeyboardButton("💳 Купить / Продлить"), KeyboardButton("📥 QR / Ссылка")],
        [KeyboardButton("🎁 Пригласить друзей"), KeyboardButton("📊 Статус")],
        [KeyboardButton("❓ Инструкция"), KeyboardButton("💬 Поддержка")],
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
    Sends notification to Telegram bot admins about a new support message.
    """
    if not bot_app or not ADMIN_IDS:
        return

    text_preview = (msg.text[:300] + '...') if len(msg.text) > 300 else msg.text
    notify_text = (
        f"📩 <b>Новое сообщение в поддержку сайта!</b>\n\n"
        f"👤 <b>От:</b> {esc(msg.sender_name)} ({esc(msg.sender_type)})\n"
        f"🔑 <b>Сессия:</b> <code>{esc(msg.session_id)}</code>\n"
        f"💬 <b>Текст:</b>\n<i>{esc(text_preview)}</i>\n\n"
        f"👉 <b>Ответить:</b> <code>/reply {esc(msg.session_id)} Ваш ответ</code>"
    )

    async def _send():
        for admin_id in ADMIN_IDS:
            try:
                await bot_app.bot.send_message(
                    chat_id=admin_id,
                    text=notify_text,
                    parse_mode='HTML',
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


# ----------------------------- Bot Handlers -----------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []

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

    from urllib.parse import quote
    enc = quote(link, safe='')
    name = quote('VoltaVPN', safe='')

    keyboard = [
        [InlineKeyboardButton("📱 Karing (iOS / Android)", url=f"karing://install-config?url={enc}&name={name}")],
        [InlineKeyboardButton("🤖 v2rayNG (Android)", url=f"v2rayng://install-sub?url={enc}&name={name}")],
        [InlineKeyboardButton("🍏 Streisand (iOS)", url=f"streisand://import/{enc}")],
        [InlineKeyboardButton("🛡 Hiddify", url=f"hiddify://import/{enc}#{name}")],
        [InlineKeyboardButton("🌐 Войти в личный кабинет на сайте", url=web_login_url)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        f"⚡ <b>Ваша подписка VOLTA активна!</b>\n\n"
        f"⏳ Срок действия: <b>{sub.time_str()}</b>\n\n"
        f"🔗 <b>Ссылка подписки (скопируйте или нажмите на кнопку приложения):</b>\n"
        f"<code>{esc(link)}</code>\n\n"
        f"💡 Нажмите на название вашего приложения ниже для мгновенного импорта:"
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)


async def my_sub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
        email = AppSetting.get('SUPPORT_EMAIL') or flask_app.config.get('SUPPORT_EMAIL', 'support@voltavpn.net')
        telegram = AppSetting.get('SUPPORT_TELEGRAM') or flask_app.config.get('SUPPORT_TELEGRAM', '@voltavpn_support')

    msg = (
        "💬 <b>Служба поддержки VOLTA VPN:</b>\n\n"
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>Инструкция по подключению:</b>\n\n"
        "1️⃣ <b>Скопируйте вашу ссылку подписки</b> через меню «⚡ Подключиться» или «📥 QR / Ссылка».\n"
        "2️⃣ <b>Установите приложение для вашего устройства:</b>\n"
        "   • <b>iOS / iPhone:</b> Karing или Streisand\n"
        "   • <b>Android:</b> Karing или v2rayNG\n"
        "   • <b>Windows:</b> v2rayN или Hiddify\n"
        "3️⃣ <b>Добавьте подписку:</b> вставьте вашу ссылку или отсканируйте QR-код.\n"
        "4️⃣ <b>Нажмите «Обновить подписку»</b> и выберите самый быстрый сервер!"
    )
    await update.message.reply_text(msg, parse_mode='HTML')


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
    db_user, sub = get_or_create_user(user, auto_trial=True)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{db_user.ref_code}" if BOT_USERNAME else f"{base_url()}/r/{db_user.ref_code}"

    from urllib.parse import quote
    share_text = "⚡ Быстрый и бесплатный VPN для России — VOLTA! Забирай доступ 👇"
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(share_text, safe='')}"

    keyboard = [
        [InlineKeyboardButton("📤 Поделиться с друзьями", url=share_url)],
    ]
    msg = (
        f"🎁 <b>Реферальная программа VOLTA</b>\n\n"
        f"Делитесь вашей персональной ссылкой с друзьями:\n"
        f"<code>{esc(ref_link)}</code>"
    )
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    user = update.effective_user

    # If admin replied to a message in Telegram
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
                    sender_name=f"Поддержка VOLTA ({user.username or user.first_name})",
                    text=text,
                    is_read=True,
                )
                db.session.add(msg)
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
    elif text == "❓ Инструкция":
        await help_command(update, context)
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
    bot_app.add_handler(CommandHandler("support", support_command))
    bot_app.add_handler(CommandHandler("reply", reply_support_command))
    bot_app.add_handler(CommandHandler("r", reply_support_command))
    bot_app.add_handler(CommandHandler("chats", chats_support_command))

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
