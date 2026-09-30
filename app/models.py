from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db, login_manager
import uuid

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=True)
    telegram_verified = db.Column(db.Boolean, default=False)
    link_code = db.Column(db.String(32), unique=True, nullable=True)
    ref_code = db.Column(db.String(32), unique=True, nullable=True)
    referred_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_trial_used = db.Column(db.Boolean, default=False)
    login_token = db.Column(db.String(64), unique=True, nullable=True)
    reg_ip = db.Column(db.String(64), nullable=True)
    affiliate_balance = db.Column(db.Float, default=0.0)
    affiliate_earned_total = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    subscriptions = db.relationship('Subscription', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def active_subscription(self):
        # Find latest subscription that has not passed its effective end date (including +24h grace period)
        sub = Subscription.query.filter(
            Subscription.user_id == self.id,
            Subscription.is_active == True
        ).order_by(Subscription.end_date.desc()).first()
        if sub and not sub.is_expired():
            return sub
        return None

    def latest_subscription(self):
        return Subscription.query.filter_by(user_id=self.id).order_by(Subscription.created_at.desc()).first()


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan = db.Column(db.String(50), nullable=False)
    sub_token = db.Column(db.String(64), unique=True, nullable=False, default=lambda: uuid.uuid4().hex)
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date = db.Column(db.DateTime, nullable=False)
    grace_hours = db.Column(db.Integer, default=24)
    is_active = db.Column(db.Boolean, default=True)
    config_link = db.Column(db.String(500), nullable=True)
    qr_code_path = db.Column(db.String(500), nullable=True)
    payment_id = db.Column(db.String(100), nullable=True)
    payment_status = db.Column(db.String(50), default='pending')
    notified_24h = db.Column(db.Boolean, default=False)
    notified_expired = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def effective_end_date(self):
        """Returns the ultimate cutoff date including the +24 hours grace period."""
        hours = self.grace_hours if self.grace_hours is not None else 24
        return self.end_date + timedelta(hours=hours)

    def is_in_grace_period(self):
        """Returns True if the main subscription term ended but the user is within +24h grace window."""
        if not self.is_active:
            return False
        now = datetime.utcnow()
        return self.end_date <= now < self.effective_end_date()

    def is_expired(self):
        """Returns True only when the full term PLUS the +24 hours grace period has elapsed."""
        if not self.is_active:
            return True
        return datetime.utcnow() >= self.effective_end_date()

    def days_left(self):
        if not self.is_active or self.is_expired():
            return 0
        now = datetime.utcnow()
        if now < self.end_date:
            delta = self.end_date - now
            return max(1, delta.days + (1 if delta.seconds > 0 else 0))
        return 1  # 1 day left during grace period

    def time_left_str(self):
        if not self.is_active or self.is_expired():
            return "Истекла"
        now = datetime.utcnow()
        if self.is_in_grace_period():
            delta = self.effective_end_date() - now
            hours = delta.seconds // 3600
            mins = (delta.seconds % 3600) // 60
            return f"Льготный период (+{hours}ч {mins}м на оплату)"

        delta = self.end_date - now
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days} дн. {hours} ч." if hours > 0 else f"{days} дн."
        return f"{hours} ч." if hours > 0 else "Менее часа"


class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    protocol = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    host = db.Column(db.String(255), nullable=True)
    port = db.Column(db.Integer, nullable=True)
    latency_ms = db.Column(db.Float, nullable=True)
    country = db.Column(db.String(64), nullable=True)
    country_code = db.Column(db.String(4), nullable=True)
    is_working = db.Column(db.Boolean, default=True)
    source_url = db.Column(db.String(500), nullable=True)
    collected_at = db.Column(db.DateTime, default=datetime.utcnow)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'protocol': self.protocol,
            'content': self.content,
            'host': self.host,
            'port': self.port,
            'latency_ms': self.latency_ms,
            'country': self.country,
            'country_code': self.country_code,
            'is_working': self.is_working,
            'source_url': self.source_url,
            'checked_at': self.checked_at.strftime('%d.%m.%Y %H:%M') if self.checked_at else None,
        }


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), default='RUB')
    plan = db.Column(db.String(50), nullable=False)
    payment_method = db.Column(db.String(50), nullable=False)
    external_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('payments', lazy=True))


class TrialClaim(db.Model):
    """
    Anti-abuse ledger: records every free-trial grant keyed by telegram_id and
    registration IP so trials cannot be farmed across many accounts.
    """
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, index=True, nullable=True)
    ip = db.Column(db.String(64), index=True, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Referral(db.Model):
    """
    Tracks who invited whom.
    """
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True, nullable=False)
    invited_telegram_id = db.Column(db.BigInteger, index=True, nullable=True)
    invited_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    bonus_days_given = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def referred_user_id(self):
        return self.invited_user_id

    @referred_user_id.setter
    def referred_user_id(self, val):
        self.invited_user_id = val


class SupportMessage(db.Model):
    """
    Real-time support chat messages between website visitors/users and admins.
    """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    sender_type = db.Column(db.String(20), nullable=False, default='user')  # 'user', 'guest', 'admin', 'bot'
    sender_name = db.Column(db.String(80), nullable=False, default='Пользователь')
    sender_email = db.Column(db.String(120), nullable=True)
    text = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('support_messages', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'sender_type': self.sender_type,
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'text': self.text,
            'is_read': self.is_read,
            'created_at': self.created_at.strftime('%H:%M %d.%m.%Y'),
            'time_short': self.created_at.strftime('%H:%M'),
            'timestamp': self.created_at.isoformat(),
        }


class AppSetting(db.Model):
    """
    Key-value application settings (e.g. payment gateway credentials, support contacts).
    """
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get(key, default=None):
        try:
            setting = AppSetting.query.filter_by(key=key).first()
            return setting.value if setting and setting.value is not None and setting.value != '' else default
        except Exception:
            return default

    @staticmethod
    def set(key, value, description=None):
        try:
            setting = AppSetting.query.filter_by(key=key).first()
            if not setting:
                setting = AppSetting(key=key, value=value, description=description)
                db.session.add(setting)
            else:
                setting.value = value
                if description:
                    setting.description = description
            db.session.commit()
            return setting
        except Exception:
            db.session.rollback()
            return None


class AffiliateReward(db.Model):
    """
    Records commissions earned by referrers when invited users purchase subscriptions.
    Default commission is 75% (configurable via Admin Panel).
    """
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True, nullable=False)
    referred_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('payment.id'), nullable=True)
    payment_amount = db.Column(db.Float, nullable=False)
    commission_percent = db.Column(db.Float, default=75.0)
    reward_amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    referrer = db.relationship('User', foreign_keys=[referrer_id], backref=db.backref('affiliate_rewards', lazy=True, order_by='AffiliateReward.created_at.desc()'))
    referred_user = db.relationship('User', foreign_keys=[referred_user_id], backref=db.backref('referral_commissions_generated', lazy=True))
    payment = db.relationship('Payment', backref=db.backref('affiliate_reward', uselist=False))

    @property
    def percent(self):
        return self.commission_percent


class WithdrawalRequest(db.Model):
    """
    Partner withdrawal requests for earned affiliate commissions.
    Supported payout methods: SBP, Bank Cards, YooMoney, USDT TRC20, TON, or internal subscription balance.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payout_method = db.Column(db.String(50), nullable=False)  # 'sbp', 'card', 'yoomoney', 'usdt', 'ton', 'balance_sub'
    payout_details = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(30), default='pending')      # 'pending', 'completed', 'rejected'
    admin_comment = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('withdrawal_requests', lazy=True, order_by='WithdrawalRequest.created_at.desc()'))

    def method_label(self):
        labels = {
            'sbp': 'СБП (Номер телефона)',
            'card': 'Банковская карта РФ (МИР/Visa/MC)',
            'yoomoney': 'ЮMoney кошелёк',
            'usdt': 'USDT (TRC-20)',
            'ton': 'TON кошелёк',
            'balance_sub': 'Оплата подписки с баланса',
        }
        return labels.get(self.payout_method, self.payout_method.upper())

    def status_badge_class(self):
        if self.status == 'completed':
            return 'badge-neon-emerald'
        elif self.status == 'rejected':
            return 'bg-danger'
        return 'badge-neon-indigo'

    def status_label(self):
        labels = {
            'pending': '⏳ В обработке',
            'completed': '✅ Выплачено',
            'rejected': '❌ Отклонено',
        }
        return labels.get(self.status, self.status)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
