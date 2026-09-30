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
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_trial_used = db.Column(db.Boolean, default=False)
    login_token = db.Column(db.String(64), unique=True, nullable=True)
    reg_ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    subscriptions = db.relationship('Subscription', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def active_subscription(self):
        now = datetime.utcnow()
        return Subscription.query.filter(
            Subscription.user_id == self.id,
            Subscription.is_active == True,
            Subscription.end_date > now
        ).order_by(Subscription.end_date.desc()).first()

    def latest_subscription(self):
        return Subscription.query.filter_by(user_id=self.id).order_by(Subscription.created_at.desc()).first()


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan = db.Column(db.String(50), nullable=False)
    sub_token = db.Column(db.String(64), unique=True, nullable=False, default=lambda: uuid.uuid4().hex)
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date = db.Column(db.DateTime, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    config_link = db.Column(db.String(500), nullable=True)
    qr_code_path = db.Column(db.String(500), nullable=True)
    payment_id = db.Column(db.String(100), nullable=True)
    payment_status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def days_left(self):
        if not self.is_active or self.is_expired():
            return 0
        delta = self.end_date - datetime.utcnow()
        return max(0, delta.days + (1 if delta.seconds > 0 else 0))

    def time_left_str(self):
        if not self.is_active or self.is_expired():
            return "Истекла"
        delta = self.end_date - datetime.utcnow()
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days} дн. {hours} ч." if hours > 0 else f"{days} дн."
        return f"{hours} ч." if hours > 0 else "Менее часа"

    def is_expired(self):
        return datetime.utcnow() > self.end_date


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
    invited_telegram_id = db.Column(db.BigInteger, unique=True, nullable=False)
    invited_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SupportMessage(db.Model):
    """
    Real-time support chat messages between website visitors/users and admins.
    """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    sender_type = db.Column(db.String(20), nullable=False, default='user')  # 'user', 'guest', 'admin', 'bot'
    sender_name = db.Column(db.String(80), nullable=False, default='Пользователь')
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
