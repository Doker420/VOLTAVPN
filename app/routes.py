from flask import render_template, redirect, url_for, flash, request, jsonify, current_app, Response, abort
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription, Config, Payment, TrialClaim, SupportMessage, AppSetting
from app.payment import (
    create_platega_payment,
    create_cryptobot_payment,
    create_yoomoney_payment,
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
    """Returns referral progress + Telegram share button URL."""
    from app.models import Referral

    if not getattr(user, 'ref_code', None):
        try:
            user.ref_code = uuid.uuid4().hex[:10]
            db.session.commit()
        except Exception:
            db.session.rollback()

    count = Referral.query.filter_by(referrer_id=user.id).count() if user.ref_code else 0
    bot_username = current_app.config.get('BOT_USERNAME')
    if bot_username and user.ref_code:
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.ref_code}"
    else:
        ref_link = f"{get_base_url()}/r/{user.ref_code}" if user.ref_code else get_base_url()

    share_text = (
        "⚡ Молниеносный быстрый VPN для России — VOLTA! "
        "Автообновляемые серверы, работает где угодно. Попробуй бесплатно 👇"
    )
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(share_text, safe='')}"

    return {
        'required': REFERRALS_REQUIRED,
        'count': count,
        'remaining': max(0, REFERRALS_REQUIRED - count),
        'ref_link': ref_link,
        'share_url': share_url,
        'unlocked': count >= REFERRALS_REQUIRED,
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
    return current_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')


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
    enc = quote(sub_url, safe='')
    name = quote('VoltaVPN', safe='')
    return {
        'v2rayng': f"v2rayng://install-sub?url={enc}&name={name}",
        'hiddify': f"hiddify://import/{enc}#{name}",
        'streisand': f"streisand://import/{enc}",
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

        user = User(
            username=username,
            email=email if email else None,
            telegram_id=None,
            telegram_verified=False,
            link_code=uuid.uuid4().hex[:12],
            login_token=uuid.uuid4().hex,
            reg_ip=_client_ip(),
        )
        user.set_password(password)
        db.session.add(user)
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
            flash('Ссылка входа недействительна. Откройте бот и запросите ссылку снова.', 'danger')
            return redirect(url_for('index'))
        login_user(user)
        return redirect(url_for('dashboard', welcome=1))

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

        # Refresh / generate QR code and config link
        if sub:
            sub.config_link = f"{get_base_url()}/sub/{sub.sub_token}"
            if not sub.qr_code_path or not os.path.exists(os.path.join(current_app.root_path, 'static', sub.qr_code_path)):
                sub.qr_code_path = generate_qr_code(sub.config_link, sub.sub_token)
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
        return render_template('subscribe.html', plan=plan, plan_id=plan_id, yoomoney_receiver=receiver, support_info=support_info)

    @flask_app.route('/payment/create', methods=['POST'])
    @login_required
    def create_payment():
        plan_id = request.form.get('plan_id')
        method = request.form.get('method', 'yoomoney')
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

    @flask_app.route('/sub/<sub_token>')
    def subscription_feed(sub_token):
        """
        Dynamic Subscription Endpoint for V2Ray / Karing / Streisand / NekoBox / Hiddify / Sing-box.
        Checks active status & returns verified Base64 configs.
        Sends Profile headers so client apps automatically update periodically.
        """
        def _sub_headers(sub_obj, title='VoltaVPN'):
            headers = {
                'Profile-Update-Interval': '1',
                'Update-Interval': '1',
                'Profile-Title': title,
                'Profile-Web-Page-Url': f"{get_base_url()}/dashboard",
                'Content-Disposition': f'inline; filename="{title}"',
            }
            if sub_obj is not None:
                expire_ts = int(sub_obj.end_date.timestamp())
                total = 1099511627776  # 1 TiB
                headers['Subscription-Userinfo'] = (
                    f"upload=0; download=0; total={total}; expire={expire_ts}"
                )
            return headers

        if sub_token == 'public':
            feed = generate_subscription_feed(is_base64=True, limit=100)
            return Response(feed, mimetype='text/plain; charset=utf-8',
                            headers=_sub_headers(None, 'VoltaVPN'))

        sub = Subscription.query.filter_by(sub_token=sub_token).first()
        if not sub:
            return Response("Invalid Subscription Token", status=404, mimetype='text/plain')

        if not sub.is_active or sub.is_expired():
            blocked_msg = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?encryption=none&security=none#%E2%9A%A0%EF%B8%8F%20VoltaVPN%20%7C%20%D0%9F%D0%BE%D0%B4%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%20%D0%B8%D1%81%D1%82%D0%B5%D0%BA%D0%BB%D0%B0!%20%D0%9F%D1%80%D0%BE%D0%B4%D0%BB%D0%B8%D1%82%D0%B5%20%D0%BD%D0%B0%20VoltaVPN"
            b64_blocked = base64.b64encode(blocked_msg.encode('utf-8')).decode('utf-8')
            headers = {
                'Profile-Title': 'VoltaVPN (истекла)',
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
            user_obj = User.query.get(last_msg.user_id) if last_msg.user_id else None

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

        # 7-day signup trend
        signup_trend = []
        for i in range(6, -1, -1):
            day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = User.query.filter(User.created_at >= day_start, User.created_at < day_end).count()
            signup_trend.append({'date': day_start.strftime('%d.%m'), 'count': count})

        yoomoney_receiver, yoomoney_token, yoomoney_secret = _yoomoney_credentials()
        support_contacts = _support_contacts()
        required_channel = AppSetting.get('REQUIRED_CHANNEL') or current_app.config.get('REQUIRED_CHANNEL', '')
        required_channel_url = AppSetting.get('REQUIRED_CHANNEL_URL') or current_app.config.get('REQUIRED_CHANNEL_URL', '')

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
        }

        return render_template(
            'admin.html',
            stats=stats,
            users=users_list,
            payments=payments_list,
            configs=configs_list,
            yoomoney_receiver=yoomoney_receiver,
            yoomoney_token=yoomoney_token,
            yoomoney_secret=yoomoney_secret,
            support_contacts=support_contacts,
            required_channel=required_channel,
            required_channel_url=required_channel_url,
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

        if single_uri:
            cfg, err = add_custom_config(single_uri, country_code=country_code)
            if err:
                flash(f'Ошибка добавления: {err}', 'danger')
            else:
                flash(f'Конфигурация {cfg.protocol.upper()} успешно добавлена ({cfg.country}).', 'success')

        if batch_text:
            count = add_batch_configs(batch_text)
            flash(f'Импортировано {count} конфигураций.', 'success')

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

    @flask_app.route('/admin/settings/save', methods=['POST'])
    @admin_required
    def admin_save_settings():
        yoomoney_receiver = (request.form.get('yoomoney_receiver') or '').strip()
        yoomoney_token = (request.form.get('yoomoney_token') or '').strip()
        yoomoney_secret = (request.form.get('yoomoney_secret') or '').strip()
        support_email = (request.form.get('support_email') or '').strip()
        support_telegram = (request.form.get('support_telegram') or '').strip()
        required_channel = (request.form.get('required_channel') or '').strip()
        required_channel_url = (request.form.get('required_channel_url') or '').strip()

        AppSetting.set('YOOMONEY_RECEIVER', yoomoney_receiver, 'YooMoney кошелёк')
        AppSetting.set('YOOMONEY_TOKEN', yoomoney_token, 'YooMoney OAuth токен')
        AppSetting.set('YOOMONEY_NOTIFICATION_SECRET', yoomoney_secret, 'YooMoney секрет уведомлений')
        AppSetting.set('SUPPORT_EMAIL', support_email, 'Email поддержки')
        AppSetting.set('SUPPORT_TELEGRAM', support_telegram, 'Telegram поддержки')
        AppSetting.set('REQUIRED_CHANNEL', required_channel, 'Обязательный канал для ОП')
        AppSetting.set('REQUIRED_CHANNEL_URL', required_channel_url, 'Ссылка на обязательный канал')

        flash('Настройки успешно сохранены!', 'success')
        return redirect(url_for('admin_dashboard') + '#settings')
