from flask import render_template, redirect, url_for, flash, request, jsonify, current_app, Response, abort, session
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription, Config, Payment, TrialClaim, SupportMessage, AppSetting, Referral, AffiliateReward, WithdrawalRequest
from app.payment import (
    create_platega_payment,
    create_cryptobot_payment,
    create_xrocket_payment,
    create_yoomoney_payment,
    payment_method_enabled,
    check_payment_status,
    process_yoomoney_webhook,
    activate_paid_subscription,
    _yoomoney_credentials,
)
from app.collector import (
    generate_subscription_feed,
    get_working_configs,
    country_flag,
    country_name,
    add_custom_config,
    add_batch_configs,
    test_all_configs,
    delete_dead_configs,
    seed_default_configs,
)
import qrcode
import os
import uuid
import base64
from functools import wraps
from urllib.parse import quote
from datetime import datetime, timedelta
from sqlalchemy import func, or_, desc

# Free trial configuration
TRIAL_DAYS = 3
MAX_TRIALS_PER_IP_PER_DAY = 5
REFERRALS_REQUIRED = 3

PLANS = {
    'free_trial': {'name': 'Пробный период (3 дня)', 'days': 3, 'price': 0, 'badge': '3 дня бесплатно'},
    '1_month': {'name': '1 месяц', 'days': 30, 'price': 199, 'badge': '199 ₽ / мес'},
    '2_months': {'name': '2 месяца', 'days': 60, 'price': 378, 'badge': 'Скидка 5%'},
    '3_months': {'name': '3 месяца', 'days': 90, 'price': 567, 'badge': 'Выгодно'},
    '6_months': {'name': '6 месяцев', 'days': 180, 'price': 1134, 'badge': 'Популярный'},
    '12_months': {'name': '12 месяцев', 'days': 360, 'price': 2268, 'badge': 'Максимальный'},
}


def _support_contacts():
    email = AppSetting.get('SUPPORT_EMAIL') or current_app.config.get('SUPPORT_EMAIL', 'support@vpn.stas-max.ru')
    telegram = AppSetting.get('SUPPORT_TELEGRAM') or current_app.config.get('SUPPORT_TELEGRAM', '@ILSupport')
    bot_username = current_app.config.get('BOT_USERNAME', '')
    return {
        'email': email,
        'telegram': telegram,
        'telegram_link': f"https://t.me/{telegram.replace('@', '')}",
        'bot_username': bot_username,
        'online': True,
        'response_time': '~2-5 минут',
    }


def _referral_info(user):
    """Returns referral progress, affiliate earnings, commission rate, and withdrawal history."""
    if not getattr(user, 'ref_code', None):
        try:
            user.ref_code = uuid.uuid4().hex[:10]
            db.session.commit()
        except Exception:
            db.session.rollback()

    ref_count_direct = User.query.filter_by(referred_by_id=user.id).count() if getattr(user, 'id', None) else 0
    ref_count_logs = Referral.query.filter_by(referrer_id=user.id).count() if getattr(user, 'id', None) else 0
    count = max(ref_count_direct, ref_count_logs)

    bot_username = current_app.config.get('BOT_USERNAME') or os.getenv('BOT_USERNAME', 'volta_vpn_bot')
    if bot_username and user.ref_code:
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.ref_code}"
    else:
        ref_link = f"{get_base_url()}/r/{user.ref_code}" if user.ref_code else get_base_url()

    web_ref_link = f"{get_base_url()}/r/{user.ref_code}" if user.ref_code else get_base_url()

    share_text = (
        "⚡ Забирай быстрый и надежный VPN для России — VoltaVPN! "
        "Автообновление серверов, YouTube в 4K и 3 дня бесплатно 👇"
    )
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(share_text, safe='')}"

    try:
        commission_percent = float(AppSetting.get('AFFILIATE_COMMISSION_PERCENT') or current_app.config.get('AFFILIATE_COMMISSION_PERCENT', '75'))
    except Exception:
        commission_percent = 75.0

    try:
        min_withdrawal = float(AppSetting.get('MIN_WITHDRAWAL_AMOUNT') or current_app.config.get('MIN_WITHDRAWAL_AMOUNT', '100'))
    except Exception:
        min_withdrawal = 100.0

    rewards = AffiliateReward.query.filter_by(referrer_id=user.id).order_by(AffiliateReward.created_at.desc()).limit(30).all() if getattr(user, 'id', None) else []
    withdrawals = WithdrawalRequest.query.filter_by(user_id=user.id).order_by(WithdrawalRequest.created_at.desc()).limit(30).all() if getattr(user, 'id', None) else []

    affiliate_balance = round(user.affiliate_balance or 0.0, 2) if getattr(user, 'affiliate_balance', None) else 0.0
    affiliate_earned_total = round(user.affiliate_earned_total or 0.0, 2) if getattr(user, 'affiliate_earned_total', None) else 0.0

    now = datetime.utcnow()
    active_refs = (
        Subscription.query.join(User, Subscription.user_id == User.id)
        .filter(User.referred_by_id == user.id, Subscription.is_active == True, Subscription.end_date > now)
        .count()
    ) if getattr(user, 'id', None) else 0

    return {
        'required': REFERRALS_REQUIRED,
        'count': count,
        'referrals_count': count,
        'remaining': max(0, REFERRALS_REQUIRED - count),
        'ref_link': ref_link,
        'web_ref_link': web_ref_link,
        'share_url': share_url,
        'unlocked': count >= REFERRALS_REQUIRED,
        'commission_percent': int(commission_percent) if commission_percent.is_integer() else commission_percent,
        'min_withdrawal': int(min_withdrawal) if min_withdrawal.is_integer() else min_withdrawal,
        'balance': affiliate_balance,
        'affiliate_balance': affiliate_balance,
        'earned_total': affiliate_earned_total,
        'affiliate_earned_total': affiliate_earned_total,
        'active_refs': active_refs,
        'rewards': rewards,
        'rewards_history': rewards,
        'withdrawals': withdrawals,
        'withdrawals_history': withdrawals,
    }


def _client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def _ip_trial_count(ip, hours=24):
    if not ip:
        return 0
    since = datetime.utcnow() - timedelta(hours=hours)
    return TrialClaim.query.filter(TrialClaim.ip == ip, TrialClaim.created_at >= since).count()


def grant_trial(user, telegram_id=None, ip=None, days=TRIAL_DAYS):
    """
    Grants 3-day free trial subscription to user.
    """
    sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
    if sub and sub.is_active and not sub.is_expired():
        return sub, None

    sub_token = uuid.uuid4().hex
    config_link = f"{get_base_url()}/sub/{sub_token}"
    qr_path = generate_qr_code(config_link, sub_token)
    
    if not sub:
        sub = Subscription(
            user_id=user.id,
            plan='free_trial',
            sub_token=sub_token,
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=days),
            config_link=config_link,
            qr_code_path=qr_path,
            is_active=True,
            payment_status='paid',
            notified_24h=False,
            notified_expired=False,
        )
        db.session.add(sub)
    else:
        sub.plan = 'free_trial'
        sub.start_date = datetime.utcnow()
        sub.end_date = datetime.utcnow() + timedelta(days=days)
        sub.is_active = True
        sub.payment_status = 'paid'
        sub.config_link = config_link
        sub.qr_code_path = qr_path
        sub.notified_24h = False
        sub.notified_expired = False

    user.is_trial_used = True
    tg = telegram_id or user.telegram_id
    if tg or ip:
        db.session.add(TrialClaim(telegram_id=tg, ip=ip, user_id=user.id))

    db.session.commit()
    return sub, None


def _country_breakdown(limit=40):
    rows = (
        db.session.query(
            Config.country_code,
            func.count(Config.id),
            func.avg(Config.latency_ms),
        )
        .filter(Config.is_working == True, Config.country_code.isnot(None))
        .group_by(Config.country_code)
        .order_by(func.count(Config.id).desc())
        .limit(limit)
        .all()
    )
    result = []
    for code, count, avg_ping in rows:
        result.append({
            'code': code,
            'name': country_name(code),
            'flag': country_flag(code),
            'count': count,
            'avg_ping': round(avg_ping) if avg_ping is not None else None,
        })
    return result


def get_base_url():
    """
    Intelligently determines the public base URL for subscriptions, QR codes, and deep links:
    1. If manually configured in AppSetting (Admin Panel), use it.
    2. If in active HTTP request context from a real client domain/IP (e.g. https://vpn.stas-max.ru or 192.168.x.x), use request scheme and host.
    3. Fallback to WEBHOOK_URL or default production domain.
    """
    from flask import has_request_context, request

    try:
        db_url = AppSetting.get('WEBHOOK_URL')
        # A local value can accidentally be saved from development/admin setup.
        # Never put it into a QR code or subscription link: that URL is not
        # reachable from the user's phone and makes website imports fail.
        if db_url and str(db_url).strip():
            candidate = str(db_url).strip().rstrip('/')
            if not any(localhost in candidate.lower() for localhost in ('localhost', '127.0.0.1', '0.0.0.0')):
                # Public subscription links must use TLS. An old HTTP value
                # in the admin settings otherwise gets copied into every QR.
                if candidate.lower().startswith('http://'):
                    candidate = 'https://' + candidate[7:]
                return candidate
    except Exception:
        pass

    if has_request_context():
        scheme = request.headers.get('X-Forwarded-Proto') or request.headers.get('X-Scheme') or request.scheme
        host = request.headers.get('X-Forwarded-Host') or request.host
        if host and not any(lh in host.lower() for lh in ['localhost:5000', '127.0.0.1:5000']):
            # Reverse proxies sometimes omit X-Forwarded-Proto. Production
            # subscription URLs should still never downgrade to HTTP.
            if str(scheme).lower() == 'http' and not any(localhost in host.lower() for localhost in ('localhost', '127.0.0.1', '0.0.0.0')):
                scheme = 'https'
            return f"{scheme}://{host}".rstrip('/')

    env_url = current_app.config.get('WEBHOOK_URL') or os.getenv('WEBHOOK_URL')
    if env_url and not any(lh in env_url.lower() for lh in ['localhost', '127.0.0.1']):
        env_url = env_url.strip().rstrip('/')
        return ('https://' + env_url[7:]) if env_url.lower().startswith('http://') else env_url

    if has_request_context():
        scheme = request.headers.get('X-Forwarded-Proto') or request.scheme
        host = request.headers.get('X-Forwarded-Host') or request.host
        return f"{scheme}://{host}".rstrip('/')

    return (env_url or 'https://vpn.stas-max.ru').rstrip('/')


def generate_qr_code(data, token):
    qr = qrcode.QRCode(version=1, box_size=10, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#10b981", back_color="#0a0a0f")

    static_dir = os.path.join(current_app.root_path, 'static', 'qr')
    os.makedirs(static_dir, exist_ok=True)
    filename = f"qr_{token}.png"
    filepath = os.path.join(static_dir, filename)
    img.save(filepath)
    return f"qr/{filename}"


def build_deep_links(sub_url):
    # Make app imports deterministic: clients must receive the feed, not the
    # browser portal, even when their user-agent is not recognized.
    feed_url = sub_url + ('&' if '?' in sub_url else '?') + 'format=base64'
    enc = quote(feed_url, safe='')
    name = quote('VoltaVPN', safe='')
    return {
        'v2rayng': f"v2rayng://install-sub?url={enc}&name={name}",
        'hiddify': f"hiddify://import/{enc}#{name}",
        'streisand': f"streisand://import/{feed_url}",
        'karing': f"karing://install-config?url={enc}&name={name}",
        'clash': f"clash://install-config?url={enc}&name={name}",
        'singbox': f"sing-box://import-remote-profile?url={enc}#{name}",
        'raw': sub_url,
    }


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            flash('Доступ запрещён: требуются права администратора', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def register_routes(flask_app):
    @flask_app.teardown_appcontext
    def shutdown_session(exception=None):
        db.session.remove()

    @flask_app.errorhandler(500)
    def internal_server_error(e):
        try:
            db.session.rollback()
        except Exception:
            pass
        return render_template('base.html'), 500

    @flask_app.context_processor
    def inject_helpers():
        from urllib.parse import quote
        return {
            'quote': quote,
            'get_base_url': get_base_url,
            'support_info': _support_contacts(),
        }

    @flask_app.route('/')
    def index():
        working_count = Config.query.filter_by(is_working=True).count()
        vless_count = Config.query.filter_by(is_working=True, protocol='vless').count()
        ss_count = Config.query.filter_by(is_working=True, protocol='ss').count()
        trojan_count = Config.query.filter_by(is_working=True, protocol='trojan').count()
        hy2_count = Config.query.filter_by(is_working=True, protocol='hysteria2').count()

        stats = {
            'total': working_count or 150,
            'vless': vless_count or 45,
            'ss': ss_count or 35,
            'trojan': trojan_count or 30,
            'hysteria2': hy2_count or 25,
            'updated': datetime.utcnow().strftime('%H:%M MSK')
        }
        countries = _country_breakdown()
        support_info = _support_contacts()
        return render_template('index.html', plans=PLANS, stats=stats, countries=countries, support_info=support_info)

    @flask_app.route('/privacy')
    def privacy():
        return render_template('privacy.html', support_info=_support_contacts())

    @flask_app.route('/terms')
    def terms():
        return render_template('terms.html', support_info=_support_contacts())

    @flask_app.route('/instructions')
    def instructions():
        return render_template('instructions.html', support_info=_support_contacts())

    @flask_app.route('/faq')
    def faq():
        return render_template('faq.html', support_info=_support_contacts())

    @flask_app.route('/r/<ref_code>')
    def referral_landing(ref_code):
        ref_code = (ref_code or '').strip()
        session['ref_code'] = ref_code
        resp = redirect(url_for('index', ref=ref_code, auth='register'))
        resp.set_cookie('ref_code', ref_code, max_age=60*60*24*30)  # 30 days
        return resp

    @flask_app.route('/affiliate')
    @flask_app.route('/partner')
    def affiliate_page():
        commission_percent = AppSetting.get('AFFILIATE_COMMISSION_PERCENT', '75')
        min_withdrawal = AppSetting.get('MIN_WITHDRAWAL_AMOUNT', '100')
        ref_info = _referral_info(current_user) if current_user.is_authenticated else None
        return render_template(
            'affiliate.html',
            commission_percent=commission_percent,
            min_withdrawal=min_withdrawal,
            ref_info=ref_info,
            support_info=_support_contacts()
        )

    @flask_app.route('/affiliate/withdraw', methods=['POST'])
    @login_required
    def affiliate_withdraw():
        amount_raw = (request.form.get('amount') or '0').strip().replace(',', '.')
        payout_method = (request.form.get('payout_method') or '').strip().lower()
        payout_details = (request.form.get('payout_details') or '').strip()

        try:
            amount = float(amount_raw)
        except ValueError:
            flash('Пожалуйста, укажите корректную сумму для вывода.', 'danger')
            return redirect(url_for('dashboard') + '#affiliate-program')

        user_balance = current_user.affiliate_balance or 0.0

        if amount <= 0:
            flash('Сумма вывода должна быть больше нуля.', 'danger')
            return redirect(url_for('dashboard') + '#affiliate-program')

        # Option: Pay own subscription directly from affiliate balance
        if payout_method == 'balance_sub':
            plan_id = request.form.get('plan_id', '1_month')
            plan = PLANS.get(plan_id, PLANS['1_month'])
            required_price = float(plan['price'])

            if user_balance < required_price:
                flash(f'Для оплаты тарифа "{plan["name"]}" необходимо {required_price} ₽ (ваш баланс: {user_balance:.2f} ₽).', 'danger')
                return redirect(url_for('dashboard') + '#affiliate-program')

            current_user.affiliate_balance = round(user_balance - required_price, 2)
            
            # Create completed withdrawal record for ledger
            req_rec = WithdrawalRequest(
                user_id=current_user.id,
                amount=required_price,
                payout_method='balance_sub',
                payout_details=f'Продление подписки: {plan["name"]}',
                status='completed',
                processed_at=datetime.utcnow(),
            )
            db.session.add(req_rec)

            # Extend or create subscription
            days = plan['days']
            sub = current_user.active_subscription() or current_user.latest_subscription()
            if sub and sub.is_active and not sub.is_expired():
                sub.end_date = sub.end_date + timedelta(days=days)
                sub.plan = plan_id
                sub.payment_status = 'paid'
            else:
                sub_token = uuid.uuid4().hex
                sub = Subscription(
                    user_id=current_user.id,
                    plan=plan_id,
                    sub_token=sub_token,
                    end_date=datetime.utcnow() + timedelta(days=days),
                    config_link=f"{get_base_url()}/sub/{sub_token}",
                    is_active=True,
                    payment_status='paid',
                )
                db.session.add(sub)

            db.session.commit()
            flash(f'🎉 Подписка успешно оплачена с партнёрского баланса! Добавлено +{days} дней.', 'success')
            return redirect(url_for('dashboard'))

        # Standard withdrawal request
        min_payout_str = AppSetting.get('MIN_WITHDRAWAL_AMOUNT') or current_app.config.get('MIN_WITHDRAWAL_AMOUNT', '100')
        try:
            min_payout = float(min_payout_str)
        except ValueError:
            min_payout = 100.0

        if amount < min_payout:
            flash(f'Минимальная сумма для вывода составляет {min_payout:.0f} ₽.', 'warning')
            return redirect(url_for('dashboard') + '#affiliate-program')

        if amount > user_balance:
            flash(f'Недостаточно средств на партнёрском балансе (доступно {user_balance:.2f} ₽).', 'danger')
            return redirect(url_for('dashboard') + '#affiliate-program')

        if not payout_details:
            flash('Пожалуйста, укажите реквизиты для выплаты (номер карты, телефон СБП или кошелек).', 'warning')
            return redirect(url_for('dashboard') + '#affiliate-program')

        current_user.affiliate_balance = round(user_balance - amount, 2)
        withdraw_req = WithdrawalRequest(
            user_id=current_user.id,
            amount=amount,
            payout_method=payout_method,
            payout_details=payout_details,
            status='pending',
        )
        db.session.add(withdraw_req)
        db.session.commit()

        # Notify admins
        try:
            import app.bot as bot_module
            if bot_module.ADMIN_IDS and bot_module.bot_app and bot_module.bot_app.bot:
                import asyncio
                import html
                admin_text = (
                    f"💸 <b>Новая заявка на вывод средств (Партнёрка)!</b>\n\n"
                    f"👤 Партнёр: <b>{html.escape(current_user.username)}</b> (ID: {current_user.id})\n"
                    f"💰 Сумма: <b>{amount} ₽</b>\n"
                    f"💳 Способ: <b>{html.escape(withdraw_req.method_label())}</b>\n"
                    f"📝 Реквизиты: <code>{html.escape(payout_details)}</code>\n\n"
                    f"🔗 Управление: {get_base_url()}/admin#withdrawals"
                )
                loop = getattr(bot_module.bot_app, '_custom_loop', None)
                if loop and loop.is_running():
                    for a_id in bot_module.ADMIN_IDS:
                        asyncio.run_coroutine_threadsafe(
                            bot_module.bot_app.bot.send_message(chat_id=a_id, text=admin_text, parse_mode='HTML'),
                            loop
                        )
        except Exception as e:
            print(f"[Affiliate] Admin notify error: {e}")

        flash(f'✅ Заявка на вывод {amount:.2f} ₽ успешно принята в обработку! Выплата поступит в ближайшее время.', 'success')
        return redirect(url_for('dashboard') + '#affiliate-program')

    @flask_app.route('/register', methods=['POST'])
    def register():
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not username or not password:
            flash('Пожалуйста, укажите имя пользователя и пароль', 'warning')
            return redirect(url_for('index', auth='register'))

        if len(username) < 3:
            flash('Имя пользователя должно содержать не менее 3 символов', 'warning')
            return redirect(url_for('index', auth='register'))

        if len(password) < 4:
            flash('Пароль должен содержать не менее 4 символов', 'warning')
            return redirect(url_for('index', auth='register'))

        if confirm_password and password != confirm_password:
            flash('Пароли не совпадают', 'warning')
            return redirect(url_for('index', auth='register'))

        if User.query.filter_by(username=username).first():
            flash('Пользователь с таким логином уже существует', 'danger')
            return redirect(url_for('index', auth='register'))

        if email:
            if User.query.filter_by(email=email).first():
                flash('Пользователь с такой почтой уже зарегистрирован', 'danger')
                return redirect(url_for('index', auth='register'))

        ref_code = (request.form.get('ref_code') or session.get('ref_code') or request.cookies.get('ref_code') or '').strip()
        referrer = User.query.filter_by(ref_code=ref_code).first() if ref_code else None

        user = User(
            username=username,
            email=email if email else None,
            telegram_id=None,
            telegram_verified=False,
            link_code=uuid.uuid4().hex[:12],
            login_token=uuid.uuid4().hex,
            ref_code=uuid.uuid4().hex[:10],
            referred_by_id=referrer.id if referrer else None,
            reg_ip=_client_ip(),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        if referrer:
            ref_record = Referral(
                referrer_id=referrer.id,
                invited_telegram_id=0,
                invited_user_id=user.id,
            )
            db.session.add(ref_record)
            db.session.commit()

        # Automatically provision 3-day free trial on site registration
        sub, _ = grant_trial(user, ip=_client_ip(), days=TRIAL_DAYS)
        login_user(user)

        flash('🎉 Аккаунт успешно создан! Ваш бесплатный период на 3 дня уже активирован.', 'success')
        return redirect(url_for('dashboard', welcome=1))

    @flask_app.route('/quick-start')
    def quick_start():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard', welcome=1))
        return redirect(url_for('index', auth='register'))

    @flask_app.route('/link-telegram')
    @login_required
    def link_telegram():
        if not current_user.link_code:
            current_user.link_code = uuid.uuid4().hex[:12]
            db.session.commit()

        bot_username = current_app.config.get('BOT_USERNAME') or os.getenv('BOT_USERNAME', 'volta_vpn_bot')
        deep_link = f"https://t.me/{bot_username}?start=link_{current_user.link_code}"

        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()
        return render_template(
            'link_telegram.html',
            deep_link=deep_link,
            bot_username=bot_username,
            link_code=current_user.link_code,
            verified=current_user.telegram_verified,
            has_sub=bool(sub),
        )

    @flask_app.route('/unlink-telegram', methods=['POST'])
    @login_required
    def unlink_telegram():
        current_user.telegram_id = None
        current_user.telegram_verified = False
        current_user.link_code = uuid.uuid4().hex[:12]
        db.session.commit()
        flash('Telegram-аккаунт успешно отвязан.', 'info')
        return redirect(url_for('dashboard'))

    @flask_app.route('/login', methods=['POST'])
    def login():
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter(or_(User.username == username, User.email == username)).first()

        if user and user.check_password(password):
            login_user(user)
            # Ensure user has subscription or grant trial if new
            sub = Subscription.query.filter_by(user_id=user.id).first()
            if not sub and not user.is_trial_used:
                grant_trial(user, ip=_client_ip(), days=TRIAL_DAYS)
            return redirect(url_for('dashboard'))

        flash('Неверное имя пользователя или пароль', 'danger')
        return redirect(url_for('index', auth='login'))

    @flask_app.route('/logout')
    @login_required
    def logout():
        logout_user()
        flash('Вы вышли из личного кабинета', 'info')
        return redirect(url_for('index'))

    @flask_app.route('/tg-login/<token>')
    def tg_login(token):
        user = User.query.filter_by(login_token=token).first()
        if not user:
            flash('Ссылка для входа недействительна или устарела. Откройте Telegram-бота и запросите ссылку заново.', 'danger')
            return redirect(url_for('index', auth='login'))
        login_user(user, remember=True)
        flash(f'🎉 Добро пожаловать, {user.username}! Вы успешно вошли в личный кабинет.', 'success')
        return redirect(url_for('dashboard', welcome=1))

    @flask_app.route('/update-profile', methods=['POST'])
    @login_required
    def update_profile():
        new_username = (request.form.get('username') or '').strip()
        new_email = (request.form.get('email') or '').strip().lower()
        new_password = (request.form.get('password') or '').strip()

        if new_username and new_username != current_user.username:
            existing = User.query.filter(User.username == new_username, User.id != current_user.id).first()
            if existing:
                flash('Это имя пользователя уже занято.', 'danger')
                return redirect(url_for('dashboard') + '#profile-settings')
            current_user.username = new_username

        if new_email:
            existing_email = User.query.filter(User.email == new_email, User.id != current_user.id).first()
            if existing_email:
                flash('Этот email уже привязан к другому аккаунту.', 'danger')
                return redirect(url_for('dashboard') + '#profile-settings')
            current_user.email = new_email

        if new_password:
            if len(new_password) < 6:
                flash('Пароль должен содержать минимум 6 символов.', 'danger')
                return redirect(url_for('dashboard') + '#profile-settings')
            current_user.set_password(new_password)

        db.session.commit()
        flash('Данные профиля успешно обновлены!', 'success')
        return redirect(url_for('dashboard') + '#profile-settings')

    @flask_app.route('/dashboard')
    @login_required
    def dashboard():
        if not current_user.link_code:
            current_user.link_code = uuid.uuid4().hex[:12]
            db.session.commit()

        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()

        # If user has no subscription yet, grant 3 days trial
        if not sub and not current_user.is_trial_used:
            sub, _ = grant_trial(current_user, ip=_client_ip(), days=TRIAL_DAYS)

        # Refresh / generate QR code and config link with fresh domain
        if sub:
            sub.config_link = f"{get_base_url()}/sub/{sub.sub_token}"
            try:
                sub.qr_code_path = generate_qr_code(sub.config_link, sub.sub_token)
            except Exception:
                sub.qr_code_path = f"qr/qr_{sub.sub_token}.png"
            db.session.commit()

        working_configs = get_working_configs(limit=12)
        deep_links = build_deep_links(sub.config_link) if sub and sub.config_link else {}
        welcome = request.args.get('welcome') == '1'
        payment_success = request.args.get('payment') == 'success'
        if payment_success:
            flash('🎉 Оплата принята! Ваша подписка успешно активна.', 'success')

        countries = _country_breakdown()
        ref_info = _referral_info(current_user)
        support_info = _support_contacts()
        bot_username = current_app.config.get('BOT_USERNAME') or os.getenv('BOT_USERNAME', 'volta_vpn_bot')

        return render_template(
            'dashboard.html',
            sub=sub,
            plans=PLANS,
            configs=working_configs,
            deep_links=deep_links,
            welcome=welcome,
            countries=countries,
            ref_info=ref_info,
            support_info=support_info,
            bot_username=bot_username,
        )

    @flask_app.route('/subscribe/<plan_id>')
    @login_required
    def subscribe(plan_id):
        plan = PLANS.get(plan_id)
        if not plan:
            flash('Неверный тарифный план', 'warning')
            return redirect(url_for('index'))

        if plan_id == 'free_trial':
            sub = Subscription.query.filter_by(user_id=current_user.id, is_active=True).first()
            if sub and not sub.is_expired():
                flash('У вас уже есть активная подписка!', 'info')
                return redirect(url_for('dashboard'))

            sub, err = grant_trial(current_user, ip=_client_ip(), days=TRIAL_DAYS)
            if err:
                flash(err, 'warning')
            else:
                flash('Бесплатный период на 3 дня успешно активирован!', 'success')
            return redirect(url_for('dashboard', welcome=1))

        receiver, token, _ = _yoomoney_credentials()
        support_info = _support_contacts()
        payment_methods = {method: payment_method_enabled(method) for method in ('yoomoney', 'platega', 'cryptobot', 'xrocket')}
        return render_template('subscribe.html', plan=plan, plan_id=plan_id, yoomoney_receiver=receiver, support_info=support_info, payment_methods=payment_methods)

    @flask_app.route('/payment/create', methods=['POST'])
    @login_required
    def create_payment():
        plan_id = request.form.get('plan_id')
        method = request.form.get('method', 'yoomoney').strip().lower()
        if method not in {'yoomoney', 'platega', 'cryptobot', 'xrocket'} or not payment_method_enabled(method):
            return jsonify({'error': 'Этот способ оплаты отключён администратором.'}), 400
        plan = PLANS.get(plan_id)

        if not plan or plan['price'] == 0:
            return jsonify({'error': 'Invalid plan'}), 400

        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()
        sub_id = sub.id if sub else 0

        payment_url = None
        external_id = None

        if method == 'yoomoney':
            payment_url, external_id = create_yoomoney_payment(current_user, plan, sub_id)
        elif method == 'platega':
            payment_url, external_id = create_platega_payment(current_user, plan, sub_id)
        elif method == 'cryptobot':
            payment_url, external_id = create_cryptobot_payment(current_user, plan, sub_id)
        elif method == 'xrocket':
            payment_url, external_id = create_xrocket_payment(current_user, plan, sub_id)
        else:
            return jsonify({'error': 'Invalid payment method'}), 400

        if not payment_url:
            return jsonify({'error': 'Платёжный шлюз недоступен. Попробуйте другой способ оплаты.'}), 502

        return jsonify({'payment_url': payment_url, 'external_id': external_id, 'method': method})

    @flask_app.route('/payment/check/<external_id>')
    @login_required
    def check_payment(external_id):
        payment = Payment.query.filter_by(external_id=external_id, user_id=current_user.id).first()
        if not payment:
            return jsonify({'status': 'pending'})

        status = check_payment_status(external_id, payment.payment_method)
        if status == 'paid':
            sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()
            config_link = sub.config_link if sub else f"{get_base_url()}/sub/public"
            return jsonify({'status': 'success', 'config_link': config_link})

        return jsonify({'status': 'pending'})

    @flask_app.route('/payment/callback/yoomoney', methods=['POST'])
    def yoomoney_callback():
        """
        YooMoney HTTP notification (webhook).
        """
        success, message = process_yoomoney_webhook(request.form)
        if success:
            return Response("OK", status=200, mimetype='text/plain')
        return Response(f"Error: {message}", status=400, mimetype='text/plain')

    @flask_app.route('/payment/callback/platega', methods=['POST'])
    def platega_callback():
        merchant_id = current_app.config.get('PLATEGA_MERCHANT_ID') or current_app.config.get('PLATEGA_SHOP_ID')
        secret = current_app.config.get('PLATEGA_SECRET') or current_app.config.get('PLATEGA_API_KEY')
        req_mid = request.headers.get('X-MerchantId')
        req_secret = request.headers.get('X-Secret')
        if not merchant_id or not secret or req_mid != merchant_id or req_secret != secret:
            return jsonify({'error': 'unauthorized'}), 401

        data = request.get_json(silent=True) or {}
        tx_id = data.get('id')
        status = str(data.get('status', '')).upper()
        if not tx_id:
            return jsonify({'error': 'bad request'}), 400

        payment = Payment.query.filter_by(external_id=str(tx_id)).first()
        if not payment:
            return jsonify({'status': 'ignored'}), 200

        if status == 'CONFIRMED' and payment.status != 'paid':
            activate_paid_subscription(payment)
        elif status == 'CANCELED':
            payment.status = 'canceled'
            db.session.commit()

        return jsonify({'status': 'ok'}), 200

    @flask_app.route('/open/<client>/<sub_token>')
    @flask_app.route('/open/<client>')
    def open_client(client, sub_token=None):
        sub_token = sub_token or request.args.get('token') or 'public'
        sub_url = f"{get_base_url()}/sub/{sub_token}"
        deep_links = build_deep_links(sub_url)
        target_scheme = deep_links.get(client.lower(), sub_url)

        client_titles = {
            'karing': 'Karing',
            'v2rayng': 'v2rayNG',
            'streisand': 'Streisand',
            'hiddify': 'Hiddify',
            'singbox': 'sing-box',
            'clash': 'Clash',
        }
        client_downloads = {
            'karing': 'https://karing.app/',
            'v2rayng': 'https://github.com/2dust/v2rayNG/releases',
            'streisand': 'https://apps.apple.com/app/streisand/id6450534064',
            'hiddify': 'https://github.com/hiddify/hiddify-next/releases',
            'singbox': 'https://sing-box.sagernet.org/',
            'clash': 'https://github.com/clash-verge-rev/clash-verge-rev/releases',
        }
        client_title = client_titles.get(client.lower(), client.capitalize())
        download_url = client_downloads.get(client.lower(), f"{get_base_url()}/instructions")

        return render_template(
            'open_client.html',
            client_name=client.lower(),
            client_title=client_title,
            target_scheme=target_scheme,
            sub_url=sub_url,
            download_url=download_url,
        )

    @flask_app.route('/qr/<sub_token>')
    @flask_app.route('/qr/<sub_token>.png')
    def dynamic_qr_image(sub_token):
        """
        Dynamically renders the QR code PNG image for the subscription URL
        using the current request's domain/host, guaranteeing the QR is never stale
        or pointing to localhost.
        """
        # Force the machine-readable response even when a phone's QR scanner
        # opens the link in a normal browser first. Without this parameter the
        # browser gets the HTML portal instead of the subscription feed.
        sub_url = f"{get_base_url()}/sub/{sub_token}?format=base64"
        qr = qrcode.QRCode(version=1, box_size=10, border=3)
        qr.add_data(sub_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#10b981", back_color="#0a0a0f")

        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.getvalue(), mimetype='image/png', headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        })

    @flask_app.route('/sub/<sub_token>')
    def subscription_feed(sub_token):
        """
        Dynamic Subscription Endpoint:
        - VPN clients (Karing, v2rayNG, Streisand, Hiddify, sing-box, Clash, Shadowrocket, curl, etc.)
          or requests with ?format=raw / ?format=base64 receive the base64-encoded config feed with profile headers.
        - Web browsers (Safari, Chrome, Firefox, Telegram WebKit) receive an interactive Subscription Portal
          with 1-click import buttons, QR code, remaining time, and direct app links.
        """
        def _sub_headers(sub_obj, title='VoltaVPN'):
            if sub_obj is not None and sub_obj.is_in_grace_period():
                title = 'VoltaVPN (Льготный период)'
            headers = {
                'Profile-Update-Interval': '1',
                'Update-Interval': '1',
                'Profile-Title': title,
                'Profile-Web-Page-Url': f"{get_base_url()}/dashboard",
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0',
            }
            if sub_obj is not None:
                expire_ts = int(sub_obj.effective_end_date().timestamp() if sub_obj.is_in_grace_period() else sub_obj.end_date.timestamp())
                total = 1099511627776  # 1 TiB
                headers['Subscription-Userinfo'] = (
                    f"upload=0; download=0; total={total}; expire={expire_ts}"
                )
            return headers

        format_param = request.args.get('format', '').lower().strip()
        user_agent = request.headers.get('User-Agent', '').lower()
        accept_header = request.headers.get('Accept', '').lower()

        is_vpn_client = any(client in user_agent for client in [
            'happ', 'karing', 'v2rayng', 'streisand', 'hiddify', 'sing-box', 'clash',
            'shadowrocket', 'quantumult', 'loon', 'surge', 'nekobox', 'matsuri',
            'curl', 'python-requests', 'wget', 'go-http-client', 'okhttp', 'dart'
        ])

        is_browser = ('text/html' in accept_header or 'application/xhtml+xml' in accept_header) and not is_vpn_client
        wants_raw = format_param in ['raw', 'base64', 'b64'] or (is_vpn_client and not is_browser)

        if sub_token == 'public':
            if is_browser and not wants_raw:
                return render_template(
                    'subscription_portal.html',
                    sub=None,
                    sub_token='public',
                    sub_url=f"{get_base_url()}/sub/public",
                    login_url=f"{get_base_url()}/dashboard",
                    plan_name='Публичный доступ',
                    is_active=True,
                    is_expired=False,
                    time_left="Неограниченно",
                    end_date="Бессрочно",
                    qr_code_url="/qr/public",
                    deep_links=build_deep_links(f"{get_base_url()}/sub/public"),
                )
            feed = generate_subscription_feed(is_base64=True, limit=100)
            return Response(feed, mimetype='text/plain; charset=utf-8',
                            headers=_sub_headers(None, 'VoltaVPN'))

        sub = Subscription.query.filter_by(sub_token=sub_token).first()
        if not sub:
            if is_browser and not wants_raw:
                return render_template('subscription_not_found.html', sub_token=sub_token), 404
            return Response("Invalid Subscription Token", status=404, mimetype='text/plain')

        if is_browser and not wants_raw:
            user = sub.user
            login_url = f"{get_base_url()}/tg-login/{user.login_token}" if (user and user.login_token) else f"{get_base_url()}/dashboard"
            sub_url = f"{get_base_url()}/sub/{sub.sub_token}"
            try:
                sub.qr_code_path = generate_qr_code(sub_url, sub.sub_token)
                db.session.commit()
            except Exception:
                pass

            plan_name = PLANS.get(sub.plan, {}).get('name', sub.plan)
            return render_template(
                'subscription_portal.html',
                sub=sub,
                user=user,
                sub_token=sub.sub_token,
                sub_url=sub_url,
                login_url=login_url,
                plan_name=plan_name,
                is_active=sub.is_active,
                is_expired=sub.is_expired(),
                time_left=sub.time_left_str(),
                end_date=sub.end_date.strftime('%d.%m.%Y %H:%M'),
                qr_code_url=f"/qr/{sub.sub_token}",
                deep_links=build_deep_links(sub_url),
            )

        if format_param == 'txt':
            raw_feed = generate_subscription_feed(is_base64=False, limit=150)
            return Response(raw_feed, mimetype='text/plain; charset=utf-8',
                            headers=_sub_headers(sub, 'VoltaVPN'))

        if not sub.is_active or sub.is_expired():
            blocked_msg = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?encryption=none&security=none#%E2%9A%A0%EF%B8%8F%20VoltaVPN%20%7C%20%D0%9F%D0%BE%D0%B4%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%20%D0%B8%D1%81%D1%82%D0%B5%D0%BA%D0%BB%D0%B0!%20%D0%9F%D1%80%D0%BE%D0%B4%D0%BB%D0%B8%D1%82%D0%B5%20%D0%BD%D0%B0%20VoltaVPN"
            b64_blocked = base64.b64encode(blocked_msg.encode('utf-8')).decode('utf-8')
            headers = {
                'Profile-Title': 'VoltaVPN (истекла)',
                'Profile-Update-Interval': '1',
                'Subscription-Userinfo': f"upload=0; download=0; total=0; expire={int(sub.end_date.timestamp())}",
            }
            return Response(b64_blocked, mimetype='text/plain; charset=utf-8', headers=headers)

        # Return dynamically generated feed of verified working configs
        feed = generate_subscription_feed(is_base64=True, limit=150)
        return Response(feed, mimetype='text/plain; charset=utf-8',
                        headers=_sub_headers(sub, 'VoltaVPN'))

    @flask_app.route('/download')
    @flask_app.route('/download/zip')
    def download_archive():
        from flask import send_file
        zip_path = os.path.join(current_app.root_path, 'static', 'voltavpn.zip')
        if not os.path.exists(zip_path):
            import subprocess
            subprocess.run(['zip', '-r', zip_path, '.', '-x', './.git/*', '*/__pycache__/*', '*/.pytest_cache/*', './instance/*', '*.db'], cwd=os.path.abspath(os.path.join(current_app.root_path, '..')))
        return send_file(zip_path, as_attachment=True, download_name='voltavpn.zip', mimetype='application/zip')

    @flask_app.route('/api/stats')
    def api_stats():
        working_count = Config.query.filter_by(is_working=True).count()
        vless_count = Config.query.filter_by(is_working=True, protocol='vless').count()
        ss_count = Config.query.filter_by(is_working=True, protocol='ss').count()
        trojan_count = Config.query.filter_by(is_working=True, protocol='trojan').count()
        hy2_count = Config.query.filter_by(is_working=True, protocol='hysteria2').count()

        return jsonify({
            'total': working_count,
            'vless': vless_count,
            'ss': ss_count,
            'trojan': trojan_count,
            'hysteria2': hy2_count,
            'countries': _country_breakdown(),
            'timestamp': datetime.utcnow().isoformat()
        })

    @flask_app.route('/api/countries')
    def api_countries():
        return jsonify({'countries': _country_breakdown()})

    @flask_app.route('/api/me')
    @login_required
    def api_me():
        sub = current_user.latest_subscription()
        return jsonify({
            'id': current_user.id,
            'username': current_user.username,
            'email': current_user.email,
            'telegram_verified': bool(current_user.telegram_verified),
            'is_trial_used': bool(current_user.is_trial_used),
            'has_active_sub': bool(sub and sub.is_active and not sub.is_expired()),
            'days_left': sub.days_left() if sub else 0,
        })

    # ----------------------------- Live Support Chat API -----------------------------
    @flask_app.route('/api/support/info')
    def support_info():
        return jsonify(_support_contacts())

    @flask_app.route('/api/support/messages', methods=['GET'])
    def support_get_messages():
        session_id = request.args.get('session_id')
        if not session_id:
            if current_user.is_authenticated:
                session_id = f"user_{current_user.id}"
            else:
                return jsonify({'messages': []})

        messages = SupportMessage.query.filter_by(session_id=session_id).order_by(SupportMessage.created_at.asc()).all()
        return jsonify({
            'session_id': session_id,
            'messages': [m.to_dict() for m in messages]
        })

    @flask_app.route('/api/support/send', methods=['POST'])
    def support_send_message():
        data = request.get_json(silent=True) or request.form
        text = (data.get('text') or '').strip()
        session_id = (data.get('session_id') or '').strip()

        if not text:
            return jsonify({'error': 'Текст сообщения не может быть пустым'}), 400

        if not session_id:
            if current_user.is_authenticated:
                session_id = f"user_{current_user.id}"
            else:
                session_id = f"guest_{uuid.uuid4().hex[:12]}"

        sender_name = (data.get('name') or '').strip()
        sender_email = (data.get('email') or '').strip()

        if current_user.is_authenticated:
            user_id = current_user.id
            sender_type = 'user'
            if not sender_name:
                sender_name = current_user.username
            if not sender_email and current_user.email:
                sender_email = current_user.email
            elif sender_email and not current_user.email:
                current_user.email = sender_email
                db.session.commit()
        else:
            user_id = None
            sender_type = 'guest'
            if not sender_name:
                sender_name = 'Посетитель'

        msg = SupportMessage(
            session_id=session_id,
            user_id=user_id,
            sender_type=sender_type,
            sender_name=sender_name,
            sender_email=sender_email if sender_email else None,
            text=text,
            is_read=False,
        )
        db.session.add(msg)
        db.session.commit()

        # Trigger Telegram Bot Admin notification
        try:
            from app.bot import notify_admins_support
            notify_admins_support(msg)
        except Exception as e:
            print(f"[Support] Bot notification notice: {e}")

        return jsonify({'status': 'ok', 'session_id': session_id, 'message': msg.to_dict()})

    # ----------------------------- Admin Support API -----------------------------
    @flask_app.route('/api/admin/support/chats')
    @admin_required
    def admin_support_chats():
        """
        Returns all active support conversation threads for admin live chat panel.
        """
        sessions = db.session.query(SupportMessage.session_id).distinct().all()
        chats = []
        for (s_id,) in sessions:
            last_msg = SupportMessage.query.filter_by(session_id=s_id).order_by(SupportMessage.created_at.desc()).first()
            if not last_msg:
                continue
            unread_count = SupportMessage.query.filter_by(session_id=s_id, sender_type='user', is_read=False).count() + \
                           SupportMessage.query.filter_by(session_id=s_id, sender_type='guest', is_read=False).count()
            user_obj = db.session.get(User, last_msg.user_id) if last_msg.user_id else None

            chats.append({
                'session_id': s_id,
                'user_name': user_obj.username if user_obj else (last_msg.sender_name or 'Гость'),
                'user_id': user_obj.id if user_obj else None,
                'telegram_id': user_obj.telegram_id if user_obj else None,
                'last_message': last_msg.text,
                'last_time': last_msg.created_at.strftime('%H:%M %d.%m'),
                'timestamp': last_msg.created_at.isoformat(),
                'unread_count': unread_count,
            })

        chats.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'chats': chats})

    @flask_app.route('/api/admin/support/messages/<session_id>')
    @admin_required
    def admin_support_messages(session_id):
        messages = SupportMessage.query.filter_by(session_id=session_id).order_by(SupportMessage.created_at.asc()).all()
        # Mark as read
        for m in messages:
            if m.sender_type in ['user', 'guest'] and not m.is_read:
                m.is_read = True
        db.session.commit()
        return jsonify({'session_id': session_id, 'messages': [m.to_dict() for m in messages]})

    @flask_app.route('/api/admin/support/reply', methods=['POST'])
    @admin_required
    def admin_support_reply():
        data = request.get_json(silent=True) or request.form
        session_id = (data.get('session_id') or '').strip()
        text = (data.get('text') or '').strip()

        if not session_id or not text:
            return jsonify({'error': 'Missing session_id or text'}), 400

        msg = SupportMessage(
            session_id=session_id,
            user_id=current_user.id,
            sender_type='admin',
            sender_name=f"Админ ({current_user.username})",
            text=text,
            is_read=True,
        )
        db.session.add(msg)
        db.session.commit()

        return jsonify({'status': 'ok', 'message': msg.to_dict()})

    # ----------------------------- Admin Panel Web UI & Actions -----------------------------
    @flask_app.route('/admin')
    @admin_required
    def admin_dashboard():
        now = datetime.utcnow()
        month_ago = now - timedelta(days=30)

        total_users = User.query.count()
        active_subs = Subscription.query.filter(
            Subscription.is_active == True, Subscription.end_date > now
        ).count()
        trial_subs = Subscription.query.filter_by(plan='free_trial').count()
        paid_subs = Subscription.query.filter(Subscription.plan != 'free_trial').count()

        paid_payments = Payment.query.filter_by(status='paid').all()
        revenue_total = sum(p.amount for p in paid_payments)
        revenue_month = sum(p.amount for p in paid_payments if p.paid_at and p.paid_at >= month_ago)

        total_configs = Config.query.count()
        working_configs = Config.query.filter_by(is_working=True).count()

        proto_stats = {}
        for proto in ['vless', 'trojan', 'ss', 'hysteria2', 'vmess', 'tuic']:
            proto_stats[proto] = Config.query.filter_by(is_working=True, protocol=proto).count()

        # Query all users with search filter if requested
        search_query = request.args.get('q', '').strip()
        if search_query:
            users_list = User.query.filter(
                or_(
                    User.username.ilike(f"%{search_query}%"),
                    User.email.ilike(f"%{search_query}%"),
                )
            ).order_by(User.created_at.desc()).limit(100).all()
        else:
            users_list = User.query.order_by(User.created_at.desc()).limit(100).all()

        payments_list = Payment.query.order_by(Payment.created_at.desc()).limit(50).all()
        configs_list = Config.query.order_by(Config.is_working.desc(), Config.latency_ms.asc()).limit(100).all()
        withdrawals_list = WithdrawalRequest.query.order_by(WithdrawalRequest.created_at.desc()).limit(100).all()
        affiliate_rewards_list = AffiliateReward.query.order_by(AffiliateReward.created_at.desc()).limit(100).all()

        # 7-day signup trend
        signup_trend = []
        for i in range(6, -1, -1):
            day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = User.query.filter(User.created_at >= day_start, User.created_at < day_end).count()
            signup_trend.append({'date': day_start.strftime('%d.%m'), 'count': count})

        yoomoney_receiver, yoomoney_token, yoomoney_secret = _yoomoney_credentials()
        support_contacts = _support_contacts()
        webhook_url = AppSetting.get('WEBHOOK_URL') or current_app.config.get('WEBHOOK_URL', '')
        required_channel = AppSetting.get('REQUIRED_CHANNEL') or current_app.config.get('REQUIRED_CHANNEL', '')
        required_channel_url = AppSetting.get('REQUIRED_CHANNEL_URL') or current_app.config.get('REQUIRED_CHANNEL_URL', '')
        affiliate_commission_percent = AppSetting.get('AFFILIATE_COMMISSION_PERCENT') or current_app.config.get('AFFILIATE_COMMISSION_PERCENT', '75')
        min_withdrawal_amount = AppSetting.get('MIN_WITHDRAWAL_AMOUNT') or current_app.config.get('MIN_WITHDRAWAL_AMOUNT', '100')
        payment_enabled = {m: payment_method_enabled(m) for m in ('yoomoney', 'platega', 'cryptobot', 'xrocket')}
        xrocket_token = AppSetting.get('XROCKET_API_TOKEN') or ''
        xrocket_currency = AppSetting.get('XROCKET_CURRENCY') or 'USDT'
        xrocket_rub_rate = AppSetting.get('XROCKET_RUB_RATE') or '100'
        free_config_collection_enabled = (AppSetting.get('FREE_CONFIG_COLLECTION_ENABLED', 'true') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'}

        stats = {
            'total_users': total_users,
            'active_subs': active_subs,
            'trial_subs': trial_subs,
            'paid_subs': paid_subs,
            'revenue_total': revenue_total,
            'revenue_month': revenue_month,
            'total_configs': total_configs,
            'working_configs': working_configs,
            'dead_configs': max(0, total_configs - working_configs),
            'proto_stats': proto_stats,
            'signup_trend': signup_trend,
            'pending_withdrawals': WithdrawalRequest.query.filter_by(status='pending').count(),
            'total_affiliate_paid': sum(w.amount for w in WithdrawalRequest.query.filter_by(status='completed').all()),
            'total_affiliate_rewards': sum(r.reward_amount for r in AffiliateReward.query.all()),
        }

        return render_template(
            'admin.html',
            stats=stats,
            users=users_list,
            payments=payments_list,
            configs=configs_list,
            withdrawals=withdrawals_list,
            affiliate_rewards=affiliate_rewards_list,
            affiliate_commission_percent=affiliate_commission_percent,
            min_withdrawal_amount=min_withdrawal_amount,
            payment_enabled=payment_enabled,
            xrocket_token=xrocket_token,
            xrocket_currency=xrocket_currency,
            xrocket_rub_rate=xrocket_rub_rate,
            yoomoney_receiver=yoomoney_receiver,
            yoomoney_token=yoomoney_token,
            yoomoney_secret=yoomoney_secret,
            webhook_url=webhook_url,
            support_contacts=support_contacts,
            required_channel=required_channel,
            required_channel_url=required_channel_url,
            free_config_collection_enabled=free_config_collection_enabled,
            search_query=search_query,
        )

    @flask_app.route('/admin/collect', methods=['POST'])
    @admin_required
    def admin_collect():
        from app.collector import collect_configs
        try:
            working = collect_configs()
            flash(f'Сбор завершён: {working} рабочих конфигураций в базе данных.', 'success')
        except Exception as e:
            flash(f'Ошибка сбора: {e}', 'danger')
        return redirect(url_for('admin_dashboard'))

    @flask_app.route('/admin/configs/add', methods=['POST'])
    @admin_required
    def admin_add_configs():
        single_uri = (request.form.get('single_uri') or '').strip()
        batch_text = (request.form.get('batch_text') or '').strip()
        country_code = (request.form.get('country_code') or '').strip()
        make_primary = (request.form.get('make_primary') or '').lower() in {'1', 'true', 'yes', 'on'}

        if single_uri:
            try:
                cfg, err = add_custom_config(single_uri, country_code=country_code)
                if err:
                    flash(f'Ошибка добавления: {err}', 'danger')
                else:
                    if make_primary:
                        Config.query.filter(Config.id != cfg.id).update({Config.is_primary: False}, synchronize_session=False)
                        cfg.is_primary = True
                        db.session.commit()
                    flash(f'Конфигурация {cfg.protocol.upper()} успешно добавлена ({cfg.country}).', 'success')
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception('Failed to add custom config')
                flash(f'Ошибка добавления конфигурации: {exc}', 'danger')

        if batch_text:
            try:
                result = add_batch_configs(batch_text)
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception('Failed to add config batch')
                result = (0, 0)
                flash(f'Ошибка импорта конфигураций: {exc}', 'danger')
            count, working = result if isinstance(result, tuple) else (result, result)
            if count == 0:
                flash('Не удалось распознать конфигурации из введённого текста.', 'warning')
            else:
                flash(f'Импортировано {count} конфигураций (рабочих: {working}, неактивных: {count - working}).', 'success' if working > 0 else 'warning')

        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/configs/<int:config_id>/toggle', methods=['POST'])
    @admin_required
    def admin_toggle_config(config_id):
        cfg = Config.query.get_or_404(config_id)
        cfg.is_working = not cfg.is_working
        cfg.checked_at = datetime.utcnow()
        db.session.commit()
        flash(f'Статус конфигурации #{cfg.id} изменён на {"🟢 Рабочий" if cfg.is_working else "🔴 Отключён"}.', 'info')
        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/configs/<int:config_id>/primary', methods=['POST'])
    @admin_required
    def admin_set_primary_config(config_id):
        """Make any config the preferred first entry in АВТОВЫБОР."""
        cfg = Config.query.get_or_404(config_id)
        Config.query.filter(Config.id != cfg.id).update({Config.is_primary: False}, synchronize_session=False)
        cfg.is_primary = True
        db.session.commit()
        # Regenerate the local feed immediately; the subscription endpoint is dynamic too.
        try:
            from app.collector import save_configs_to_repo
            save_configs_to_repo()
        except Exception:
            current_app.logger.exception('Failed to regenerate config feed')
        flash(f'Конфигурация #{cfg.id} назначена основной: она будет первой в АВТОВЫБОР и получит приоритет при сканировании.', 'success')
        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/configs/<int:config_id>/delete', methods=['POST'])
    @admin_required
    def admin_delete_config(config_id):
        cfg = Config.query.get_or_404(config_id)
        db.session.delete(cfg)
        db.session.commit()
        flash(f'Конфигурация #{config_id} удалена.', 'info')
        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/configs/delete-dead', methods=['POST'])
    @admin_required
    def admin_delete_dead_configs():
        deleted = delete_dead_configs()
        flash(f'Удалено {deleted} нерабочих конфигураций.', 'info')
        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/configs/test-all', methods=['POST'])
    @admin_required
    def admin_test_all_configs():
        res = test_all_configs()
        flash(f'Тестирование завершено: {res["working"]} рабочих из {res["total"]} (недоступно: {res["dead"]}).', 'success')
        return redirect(url_for('admin_dashboard') + '#configs')

    @flask_app.route('/admin/user/<int:user_id>/extend', methods=['POST'])
    @admin_required
    def admin_extend_user(user_id):
        user = User.query.get_or_404(user_id)
        days = int(request.form.get('days', 30))
        plan_name = request.form.get('plan_name', '1 месяц')

        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
        base = get_base_url()
        now = datetime.utcnow()

        if sub and sub.is_active and not sub.is_expired():
            sub.end_date = sub.end_date + timedelta(days=days)
            sub.plan = plan_name
            sub.notified_24h = False
            sub.notified_expired = False
        else:
            sub_token = uuid.uuid4().hex
            config_link = f"{base}/sub/{sub_token}"
            qr_path = generate_qr_code(config_link, sub_token)
            if not sub:
                sub = Subscription(
                    user_id=user.id,
                    plan=plan_name,
                    sub_token=sub_token,
                    start_date=now,
                    end_date=now + timedelta(days=days),
                    config_link=config_link,
                    qr_code_path=qr_path,
                    is_active=True,
                    payment_status='paid',
                    notified_24h=False,
                    notified_expired=False,
                )
                db.session.add(sub)
            else:
                sub.plan = plan_name
                sub.start_date = now
                sub.end_date = now + timedelta(days=days)
                sub.is_active = True
                sub.payment_status = 'paid'
                sub.config_link = config_link
                sub.qr_code_path = qr_path
                sub.notified_24h = False
                sub.notified_expired = False

        db.session.commit()
        flash(f'Подписка пользователя {user.username} продлена на {days} дн. (до {sub.end_date.strftime("%d.%m.%Y")}).', 'success')
        return redirect(url_for('admin_dashboard') + '#users')

    @flask_app.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
    @admin_required
    def admin_toggle_user(user_id):
        user = User.query.get_or_404(user_id)
        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
        if sub:
            sub.is_active = not sub.is_active
            db.session.commit()
            flash(f'Подписка пользователя {user.username} переключена (активна: {sub.is_active}).', 'info')
        else:
            grant_trial(user, ip=_client_ip(), days=TRIAL_DAYS)
            flash(f'Для пользователя {user.username} создана активная подписка на 3 дня.', 'info')
        return redirect(url_for('admin_dashboard') + '#users')

    @flask_app.route('/admin/user/<int:user_id>/set-admin', methods=['POST'])
    @admin_required
    def admin_set_admin(user_id):
        user = User.query.get_or_404(user_id)
        user.is_admin = not user.is_admin
        db.session.commit()
        flash(f'Права администратора для {user.username} {"выданы" if user.is_admin else "отозваны"}.', 'info')
        return redirect(url_for('admin_dashboard') + '#users')

    @flask_app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
    @admin_required
    def admin_delete_user(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('Вы не можете удалить свой собственный аккаунт', 'danger')
            return redirect(url_for('admin_dashboard') + '#users')
        username = user.username
        db.session.delete(user)
        db.session.commit()
        flash(f'Пользователь {username} удалён.', 'info')
        return redirect(url_for('admin_dashboard') + '#users')

    @flask_app.route('/admin/payment/<int:payment_id>/confirm', methods=['POST'])
    @admin_required
    def admin_confirm_payment(payment_id):
        payment = Payment.query.get_or_404(payment_id)
        activate_paid_subscription(payment)
        flash(f'Платёж #{payment.id} вручную подтверждён и подписка активирована.', 'success')
        return redirect(url_for('admin_dashboard') + '#payments')

    @flask_app.route('/admin/withdraw/<int:withdraw_id>/approve', methods=['POST'])
    @admin_required
    def admin_approve_withdrawal(withdraw_id):
        w_req = WithdrawalRequest.query.get_or_404(withdraw_id)
        if w_req.status == 'completed':
            flash('Заявка уже была подтверждена ранее.', 'info')
            return redirect(url_for('admin_dashboard') + '#withdrawals')

        w_req.status = 'completed'
        w_req.processed_at = datetime.utcnow()
        w_req.admin_comment = (request.form.get('comment') or 'Выплачено').strip()
        db.session.commit()

        # Notify user via bot if telegram linked
        user = w_req.user
        if user.telegram_id and user.telegram_verified:
            try:
                import app.bot as bot_module
                if bot_module.bot_app and bot_module.bot_app.bot:
                    import asyncio
                    loop = getattr(bot_module.bot_app, '_custom_loop', None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot_module.bot_app.bot.send_message(
                                chat_id=user.telegram_id,
                                text=(
                                    f"✅ <b>Выплата партнёрских средств выполнена!</b>\n\n"
                                    f"💰 Сумма: <b>{w_req.amount} ₽</b>\n"
                                    f"💳 Способ: <b>{w_req.method_label()}</b>\n"
                                    f"📝 Реквизиты: <code>{w_req.payout_details}</code>\n\n"
                                    f"Спасибо за сотрудничество с VoltaVPN! 🚀"
                                ),
                                parse_mode='HTML'
                            ),
                            loop
                        )
            except Exception as e:
                print(f"[Withdraw] Notify error: {e}")

        flash(f'Заявка #{w_req.id} на сумму {w_req.amount} ₽ подтверждена и отмечена как выплаченная.', 'success')
        return redirect(url_for('admin_dashboard') + '#withdrawals')

    @flask_app.route('/admin/withdraw/<int:withdraw_id>/reject', methods=['POST'])
    @admin_required
    def admin_reject_withdrawal(withdraw_id):
        w_req = WithdrawalRequest.query.get_or_404(withdraw_id)
        if w_req.status == 'rejected':
            flash('Заявка уже отклонена.', 'info')
            return redirect(url_for('admin_dashboard') + '#withdrawals')

        if w_req.status == 'pending':
            # Refund balance back to partner
            user = w_req.user
            user.affiliate_balance = round((user.affiliate_balance or 0.0) + w_req.amount, 2)

        w_req.status = 'rejected'
        w_req.processed_at = datetime.utcnow()
        w_req.admin_comment = (request.form.get('comment') or 'Отклонено администратором').strip()
        db.session.commit()

        flash(f'Заявка #{w_req.id} отклонена, {w_req.amount} ₽ возвращены на партнёрский баланс пользователя.', 'warning')
        return redirect(url_for('admin_dashboard') + '#withdrawals')

    @flask_app.route('/admin/settings/save', methods=['POST'])
    @admin_required
    def admin_save_settings():
        webhook_url = (request.form.get('webhook_url') or '').strip()
        yoomoney_receiver = (request.form.get('yoomoney_receiver') or '').strip()
        yoomoney_token = (request.form.get('yoomoney_token') or '').strip()
        yoomoney_secret = (request.form.get('yoomoney_secret') or '').strip()
        support_email = (request.form.get('support_email') or '').strip()
        support_telegram = (request.form.get('support_telegram') or '').strip()
        required_channel = (request.form.get('required_channel') or '').strip()
        required_channel_url = (request.form.get('required_channel_url') or '').strip()
        affiliate_commission_percent = (request.form.get('affiliate_commission_percent') or '75').strip()
        min_withdrawal_amount = (request.form.get('min_withdrawal_amount') or '100').strip()
        free_config_collection_enabled = 'true' if request.form.get('free_config_collection_enabled') == 'on' else 'false'
        payment_switches = {
            'YOOMONEY': 'true' if request.form.get('payment_yoomoney_enabled') == 'on' else 'false',
            'PLATEGA': 'true' if request.form.get('payment_platega_enabled') == 'on' else 'false',
            'CRYPTOBOT': 'true' if request.form.get('payment_cryptobot_enabled') == 'on' else 'false',
            'XROCKET': 'true' if request.form.get('payment_xrocket_enabled') == 'on' else 'false',
        }

        if webhook_url:
            AppSetting.set('WEBHOOK_URL', webhook_url, 'Публичный домен сервиса (https://...)')
        AppSetting.set('YOOMONEY_RECEIVER', yoomoney_receiver, 'YooMoney кошелёк')
        AppSetting.set('YOOMONEY_TOKEN', yoomoney_token, 'YooMoney OAuth токен')
        AppSetting.set('YOOMONEY_NOTIFICATION_SECRET', yoomoney_secret, 'YooMoney секрет уведомлений')
        AppSetting.set('SUPPORT_EMAIL', support_email, 'Email поддержки')
        AppSetting.set('SUPPORT_TELEGRAM', support_telegram, 'Telegram поддержки')
        AppSetting.set('REQUIRED_CHANNEL', required_channel, 'Обязательный канал для ОП')
        AppSetting.set('REQUIRED_CHANNEL_URL', required_channel_url, 'Ссылка на обязательный канал')
        AppSetting.set('AFFILIATE_COMMISSION_PERCENT', affiliate_commission_percent, 'Процент партнёрского вознаграждения (%)')
        AppSetting.set('MIN_WITHDRAWAL_AMOUNT', min_withdrawal_amount, 'Минимальная сумма для вывода (₽)')
        AppSetting.set('FREE_CONFIG_COLLECTION_ENABLED', free_config_collection_enabled, 'Сбор бесплатных конфигураций из открытых источников')
        for gateway, enabled in payment_switches.items():
            AppSetting.set(f'PAYMENT_{gateway}_ENABLED', enabled, f'Платёжная система {gateway}')
        AppSetting.set('XROCKET_API_TOKEN', (request.form.get('xrocket_token') or '').strip(), 'xRocket API token')
        AppSetting.set('XROCKET_CURRENCY', (request.form.get('xrocket_currency') or 'USDT').strip().upper(), 'Валюта xRocket')
        AppSetting.set('XROCKET_RUB_RATE', (request.form.get('xrocket_rub_rate') or '100').strip(), 'Курс рублей за единицу xRocket валюты')

        flash('Настройки успешно сохранены!', 'success')
        return redirect(url_for('admin_dashboard') + '#settings')
