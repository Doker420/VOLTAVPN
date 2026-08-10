from flask import render_template, redirect, url_for, flash, request, jsonify, current_app, Response, abort
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription, Config, Payment, TrialClaim
from app.payment import create_platega_payment, create_cryptobot_payment, check_payment_status, activate_paid_subscription
from app.collector import generate_subscription_feed, get_working_configs, country_flag, country_name
import qrcode
import os
import uuid
import base64
from functools import wraps
from urllib.parse import quote
from datetime import datetime, timedelta
from sqlalchemy import func


# Anti-abuse tunables
TRIAL_DAYS = 30
MAX_TRIALS_PER_IP_PER_DAY = 3


def _client_ip():
    """Best-effort real client IP behind a reverse proxy (Nginx sets XFF)."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def _ip_trial_count(ip, hours=24):
    if not ip:
        return 0
    since = datetime.utcnow() - timedelta(hours=hours)
    return TrialClaim.query.filter(TrialClaim.ip == ip, TrialClaim.created_at >= since).count()


def _telegram_already_claimed(telegram_id):
    if not telegram_id:
        return False
    return TrialClaim.query.filter_by(telegram_id=telegram_id).first() is not None


def grant_trial(user, telegram_id=None, ip=None):
    """
    Central, anti-abuse-guarded trial provisioning. Returns (Subscription|None, error_message|None).
    Rules:
      - a Telegram account is REQUIRED and can claim a trial only once (ever),
      - an IP can start at most MAX_TRIALS_PER_IP_PER_DAY trials per day,
      - a user account can use its trial only once.
    """
    if user.is_trial_used:
        return None, 'Бесплатный период уже был использован на этом аккаунте.'

    tg = telegram_id or user.telegram_id
    if not tg or not user.telegram_verified:
        return None, 'Для активации бесплатного периода привяжите Telegram-аккаунт.'

    if _telegram_already_claimed(tg):
        return None, 'На этот Telegram уже был выдан бесплатный период.'

    if ip and _ip_trial_count(ip) >= MAX_TRIALS_PER_IP_PER_DAY:
        return None, 'Превышен лимит бесплатных активаций с этого адреса. Попробуйте позже или оформите подписку.'

    sub_token = uuid.uuid4().hex
    config_link = f"{get_base_url()}/sub/{sub_token}"
    qr_path = generate_qr_code(config_link, sub_token)
    sub = Subscription(
        user_id=user.id,
        plan='free_trial',
        sub_token=sub_token,
        end_date=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
        config_link=config_link,
        qr_code_path=qr_path,
        is_active=True,
        payment_status='paid',
    )
    db.session.add(sub)
    user.is_trial_used = True
    db.session.add(TrialClaim(telegram_id=tg, ip=ip, user_id=user.id))
    db.session.commit()
    return sub, None


def _country_breakdown(limit=40):
    """Returns list of {code, name, flag, count, avg_ping} for working configs."""
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

PLANS = {
    'free_trial': {'name': 'Бесплатный пробный период', 'days': 30, 'price': 0, 'badge': '30 дней бесплатно'},
    '1_month': {'name': '1 месяц', 'days': 30, 'price': 199, 'badge': '199 ₽ / мес'},
    '2_months': {'name': '2 месяца', 'days': 60, 'price': 378, 'badge': 'Скидка 5%'},
    '3_months': {'name': '3 месяца', 'days': 90, 'price': 567, 'badge': 'Выгодно'},
    '6_months': {'name': '6 месяцев', 'days': 180, 'price': 1134, 'badge': 'Популярный'},
    '12_months': {'name': '12 месяцев', 'days': 360, 'price': 2268, 'badge': 'Максимальный'},
}

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
    """
    Builds one-tap import deep links for popular clients from a subscription URL.
    These allow instant connection without manual copy-paste.
    """
    enc = quote(sub_url, safe='')
    name = quote('VOLTA VPN', safe='')
    return {
        # v2rayNG / v2rayN universal import
        'v2rayng': f"v2rayng://install-sub?url={enc}&name={name}",
        # Hiddify one-click
        'hiddify': f"hiddify://import/{enc}#{name}",
        # Streisand (iOS)
        'streisand': f"streisand://import/{enc}",
        # Karing
        'karing': f"karing://install-config?url={enc}&name={name}",
        # Clash / Clash Meta / Stash
        'clash': f"clash://install-config?url={enc}&name={name}",
        # Sing-box / SFI / SFA
        'singbox': f"sing-box://import-remote-profile?url={enc}#{name}",
        # Raw subscription URL (manual)
        'raw': sub_url,
    }

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def register_routes(app):
    @app.route('/')
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
        return render_template('index.html', plans=PLANS, stats=stats, countries=countries)

    @app.route('/register', methods=['POST'])
    def register():
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')

        if not username or not password:
            flash('Укажите логин и пароль', 'warning')
            return redirect(url_for('index'))

        if User.query.filter_by(username=username).first():
            flash('Пользователь с таким именем уже существует', 'danger')
            return redirect(url_for('index'))

        user = User(
            username=username,
            email=email if email else None,
            telegram_id=None,
            telegram_verified=False,
            link_code=uuid.uuid4().hex[:12],
            reg_ip=_client_ip(),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        # No trial yet — the user must verify Telegram first (anti-abuse).
        flash('Аккаунт создан! Привяжите Telegram, чтобы получить бесплатный период.', 'info')
        return redirect(url_for('link_telegram'))

    @app.route('/quick-start')
    def quick_start():
        """
        Free trial no longer works anonymously. Send the user to registration,
        which then requires Telegram verification before the trial is granted.
        """
        if current_user.is_authenticated:
            if current_user.telegram_verified:
                return redirect(url_for('dashboard', welcome=1))
            return redirect(url_for('link_telegram'))
        flash('Зарегистрируйтесь и привяжите Telegram, чтобы получить 30 дней бесплатно.', 'info')
        return redirect(url_for('index', register=1))

    @app.route('/link-telegram')
    @login_required
    def link_telegram():
        """
        Shows the deep link to the Telegram bot that binds this account and
        grants the trial (once). Trial is provisioned by the bot on /start.
        """
        if not current_user.link_code:
            current_user.link_code = uuid.uuid4().hex[:12]
            db.session.commit()

        bot_username = current_app.config.get('BOT_USERNAME')
        deep_link = None
        if bot_username:
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

    @app.route('/login', methods=['POST'])
    def login():
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))

        flash('Неверное имя пользователя или пароль', 'danger')
        return redirect(url_for('index'))

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('index'))

    @app.route('/tg-login/<token>')
    def tg_login(token):
        """
        Token-based auto-login from the Telegram bot. Opens the dashboard
        already authenticated. Tokens are per-user and stable (regenerated only
        if missing), so treat the URL as a secret like the subscription link.
        """
        user = User.query.filter_by(login_token=token).first()
        if not user:
            flash('Ссылка входа недействительна. Откройте бот и нажмите «Подключиться» снова.', 'danger')
            return redirect(url_for('index'))
        login_user(user)
        return redirect(url_for('dashboard', welcome=1))

    @app.route('/dashboard')
    @login_required
    def dashboard():
        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()

        # Gate: no anonymous/auto trial. Trial requires a verified Telegram link.
        if not sub:
            if not current_user.telegram_verified:
                flash('Привяжите Telegram, чтобы активировать бесплатный период.', 'info')
                return redirect(url_for('link_telegram'))
            if not current_user.is_trial_used:
                created, err = grant_trial(current_user, telegram_id=current_user.telegram_id, ip=_client_ip())
                if err:
                    flash(err, 'warning')
                else:
                    sub = created

        # Ensure QR code exists
        if sub and not sub.qr_code_path:
            sub.config_link = f"{get_base_url()}/sub/{sub.sub_token}"
            sub.qr_code_path = generate_qr_code(sub.config_link, sub.sub_token)
            db.session.commit()

        working_configs = get_working_configs(limit=10)
        deep_links = build_deep_links(sub.config_link) if sub and sub.config_link else {}
        welcome = request.args.get('welcome') == '1'
        countries = _country_breakdown()
        return render_template('dashboard.html', sub=sub, plans=PLANS, configs=working_configs,
                               deep_links=deep_links, welcome=welcome, countries=countries)

    @app.route('/subscribe/<plan_id>')
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

            if not current_user.telegram_verified:
                flash('Привяжите Telegram, чтобы активировать бесплатный период.', 'info')
                return redirect(url_for('link_telegram'))

            created, err = grant_trial(current_user, telegram_id=current_user.telegram_id, ip=_client_ip())
            if err:
                flash(err, 'warning')
            else:
                flash('Пробный период на 30 дней активирован!', 'success')
            return redirect(url_for('dashboard'))

        return render_template('subscribe.html', plan=plan, plan_id=plan_id)

    @app.route('/payment/create', methods=['POST'])
    @login_required
    def create_payment():
        plan_id = request.form.get('plan_id')
        method = request.form.get('method')
        plan = PLANS.get(plan_id)

        if not plan or plan['price'] == 0:
            return jsonify({'error': 'Invalid plan'}), 400

        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()
        sub_id = sub.id if sub else 0

        if method == 'platega':
            payment_url, external_id = create_platega_payment(current_user, plan, sub_id)
        elif method == 'cryptobot':
            payment_url, external_id = create_cryptobot_payment(current_user, plan, sub_id)
        else:
            return jsonify({'error': 'Invalid payment method'}), 400

        if not payment_url:
            return jsonify({'error': 'Платёжный шлюз недоступен. Проверьте настройки или попробуйте позже.'}), 502

        return jsonify({'payment_url': payment_url, 'external_id': external_id})

    @app.route('/payment/check/<external_id>')
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

    @app.route('/payment/callback/platega', methods=['POST'])
    def platega_callback():
        """
        Platega status callback (Настройки → Callback URLs).
        Verifies X-MerchantId/X-Secret, then activates subscription on CONFIRMED.
        Must be reachable over public HTTPS (no localhost/self-signed).
        """
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

    @app.route('/sub/<sub_token>')
    def subscription_feed(sub_token):
        """
        Dynamic Subscription Endpoint for V2Ray / Karing / Streisand / NekoBox / Hiddify clients.
        Checks active status & returns Base64 encoded verified config list.
        Sends Subscription-Userinfo + Profile headers so clients display the
        expiry date, title and update interval.
        """
        def _sub_headers(sub_obj, title):
            headers = {
                'Profile-Update-Interval': '12',
                'Profile-Title': title,
                'Content-Disposition': f'inline; filename="{title}"',
            }
            if sub_obj is not None:
                # expire is a UNIX timestamp; upload/download/total are bytes.
                expire_ts = int(sub_obj.end_date.timestamp())
                # Advertise a large quota so clients don't show "0 B"; usage is unknown.
                total = 1099511627776  # 1 TiB
                headers['Subscription-Userinfo'] = (
                    f"upload=0; download=0; total={total}; expire={expire_ts}"
                )
            return headers

        if sub_token == 'public':
            feed = generate_subscription_feed(is_base64=True, limit=100)
            return Response(feed, mimetype='text/plain; charset=utf-8',
                            headers=_sub_headers(None, 'VOLTA VPN'))

        sub = Subscription.query.filter_by(sub_token=sub_token).first()
        if not sub:
            return Response("Invalid Subscription Token", status=404, mimetype='text/plain')

        if not sub.is_active or sub.is_expired():
            # Return notice for client
            blocked_msg = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?encryption=none&security=none#%E2%9A%A0%EF%B8%8F%20%D0%9F%D0%BE%D0%B4%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%20%D0%B8%D1%81%D1%82%D0%B5%D0%BA%D0%BB%D0%B0!%20%D0%9F%D1%80%D0%BE%D0%B4%D0%BB%D0%B8%D1%82%D0%B5%20%D0%BD%D0%B0%20VOLTA"
            b64_blocked = base64.b64encode(blocked_msg.encode('utf-8')).decode('utf-8')
            # expire in the past so clients mark it expired
            headers = {
                'Profile-Title': 'VOLTA VPN (истекла)',
                'Subscription-Userinfo': f"upload=0; download=0; total=0; expire={int(sub.end_date.timestamp())}",
            }
            return Response(b64_blocked, mimetype='text/plain; charset=utf-8', headers=headers)

        # Generate base64 feed from verified working configs
        feed = generate_subscription_feed(is_base64=True, limit=150)
        return Response(feed, mimetype='text/plain; charset=utf-8',
                        headers=_sub_headers(sub, 'VOLTA VPN'))

    @app.route('/api/stats')
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

    @app.route('/api/countries')
    def api_countries():
        return jsonify({'countries': _country_breakdown()})

    @app.route('/api/me')
    @login_required
    def api_me():
        return jsonify({
            'telegram_verified': bool(current_user.telegram_verified),
            'is_trial_used': bool(current_user.is_trial_used),
        })

    # ----------------------------- Admin Panel -----------------------------
    @app.route('/admin')
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

        recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
        recent_payments = Payment.query.order_by(Payment.created_at.desc()).limit(10).all()

        # 7-day signup trend
        signup_trend = []
        for i in range(6, -1, -1):
            day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = User.query.filter(User.created_at >= day_start, User.created_at < day_end).count()
            signup_trend.append({'date': day_start.strftime('%d.%m'), 'count': count})

        stats = {
            'total_users': total_users,
            'active_subs': active_subs,
            'trial_subs': trial_subs,
            'paid_subs': paid_subs,
            'revenue_total': revenue_total,
            'revenue_month': revenue_month,
            'total_configs': total_configs,
            'working_configs': working_configs,
            'dead_configs': total_configs - working_configs,
            'proto_stats': proto_stats,
            'signup_trend': signup_trend,
        }
        return render_template(
            'admin.html',
            stats=stats,
            recent_users=recent_users,
            recent_payments=recent_payments,
        )

    @app.route('/admin/collect', methods=['POST'])
    @admin_required
    def admin_collect():
        from app.collector import collect_configs
        try:
            working = collect_configs()
            flash(f'Сбор завершён: {working} рабочих конфигураций.', 'success')
        except Exception as e:
            flash(f'Ошибка сбора: {e}', 'danger')
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
    @admin_required
    def admin_toggle_user(user_id):
        user = User.query.get_or_404(user_id)
        sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
        if sub:
            sub.is_active = not sub.is_active
            db.session.commit()
            flash(f'Подписка пользователя {user.username} переключена.', 'info')
        return redirect(url_for('admin_dashboard'))
