from flask import render_template, redirect, url_for, flash, request, jsonify, current_app, Response, abort
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Subscription, Config, Payment
from app.payment import create_platega_payment, create_cryptobot_payment, check_payment_status
from app.collector import generate_subscription_feed, get_working_configs
import qrcode
import os
import uuid
import base64
from functools import wraps
from urllib.parse import quote
from datetime import datetime, timedelta

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
        return render_template('index.html', plans=PLANS, stats=stats)

    @app.route('/register', methods=['POST'])
    def register():
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        telegram_id = request.form.get('telegram_id')

        if User.query.filter_by(username=username).first():
            flash('Пользователь с таким именем уже существует', 'danger')
            return redirect(url_for('index'))

        user = User(
            username=username,
            email=email if email else None,
            telegram_id=int(telegram_id) if telegram_id and telegram_id.isdigit() else None
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        # Provision 30-day Free Trial automatically
        sub_token = uuid.uuid4().hex
        config_link = f"{get_base_url()}/sub/{sub_token}"
        qr_path = generate_qr_code(config_link, sub_token)

        sub = Subscription(
            user_id=user.id,
            plan='free_trial',
            sub_token=sub_token,
            end_date=datetime.utcnow() + timedelta(days=30),
            config_link=config_link,
            qr_code_path=qr_path,
            is_active=True,
            payment_status='paid'
        )
        db.session.add(sub)
        user.is_trial_used = True
        db.session.commit()

        login_user(user)
        flash('Добро пожаловать! Ваш бесплатный период на 30 дней успешно активирован!', 'success')
        return redirect(url_for('dashboard'))

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

    @app.route('/dashboard')
    @login_required
    def dashboard():
        sub = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).first()
        if not sub and not current_user.is_trial_used:
            # Auto create trial only if never used before
            sub_token = uuid.uuid4().hex
            config_link = f"{get_base_url()}/sub/{sub_token}"
            qr_path = generate_qr_code(config_link, sub_token)
            sub = Subscription(
                user_id=current_user.id,
                plan='free_trial',
                sub_token=sub_token,
                end_date=datetime.utcnow() + timedelta(days=30),
                config_link=config_link,
                qr_code_path=qr_path,
                is_active=True
            )
            db.session.add(sub)
            current_user.is_trial_used = True
            db.session.commit()

        # Ensure QR code exists
        if sub and not sub.qr_code_path:
            sub.config_link = f"{get_base_url()}/sub/{sub.sub_token}"
            sub.qr_code_path = generate_qr_code(sub.config_link, sub.sub_token)
            db.session.commit()

        working_configs = get_working_configs(limit=10)
        deep_links = build_deep_links(sub.config_link) if sub and sub.config_link else {}
        return render_template('dashboard.html', sub=sub, plans=PLANS, configs=working_configs, deep_links=deep_links)

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

            if current_user.is_trial_used:
                flash('Бесплатный период уже был использован. Выберите платный тариф для продления.', 'warning')
                return redirect(url_for('dashboard'))

            sub_token = uuid.uuid4().hex
            config_link = f"{get_base_url()}/sub/{sub_token}"
            qr_path = generate_qr_code(config_link, sub_token)
            sub = Subscription(
                user_id=current_user.id,
                plan=plan_id,
                sub_token=sub_token,
                end_date=datetime.utcnow() + timedelta(days=30),
                config_link=config_link,
                qr_code_path=qr_path,
                is_active=True,
                payment_status='paid'
            )
            db.session.add(sub)
            current_user.is_trial_used = True
            db.session.commit()

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

    @app.route('/sub/<sub_token>')
    def subscription_feed(sub_token):
        """
        Dynamic Subscription Endpoint for V2Ray / Karing / Streisand / NekoBox / Hiddify clients.
        Checks active status & returns Base64 encoded verified config list.
        """
        if sub_token == 'public':
            feed = generate_subscription_feed(is_base64=True, limit=100)
            return Response(feed, mimetype='text/plain; charset=utf-8')

        sub = Subscription.query.filter_by(sub_token=sub_token).first()
        if not sub:
            return Response("Invalid Subscription Token", status=404, mimetype='text/plain')

        if not sub.is_active or sub.is_expired():
            # Return notice for client
            blocked_msg = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?encryption=none&security=none#%E2%9A%A0%EF%B8%8F%20%D0%9F%D0%BE%D0%B4%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%20%D0%B8%D1%81%D1%82%D0%B5%D0%BA%D0%BB%D0%B0!%20%D0%9F%D1%80%D0%BE%D0%B4%D0%BB%D0%B8%D1%82%D0%B5%20%D0%BD%D0%B0%20VOLTA"
            b64_blocked = base64.b64encode(blocked_msg.encode('utf-8')).decode('utf-8')
            return Response(b64_blocked, mimetype='text/plain; charset=utf-8')

        # Generate base64 feed from verified working configs
        feed = generate_subscription_feed(is_base64=True, limit=150)
        return Response(feed, mimetype='text/plain; charset=utf-8')

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
            'timestamp': datetime.utcnow().isoformat()
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
