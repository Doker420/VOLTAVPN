from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler
import os
import threading
from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()
login_manager = LoginManager()
scheduler = BackgroundScheduler()

def _run_lightweight_migrations():
    """
    Adds tables/columns introduced after the DB was first created. Safe and idempotent.
    """
    from sqlalchemy import text, inspect
    try:
        inspector = inspect(db.engine)
        user_cols = [c['name'] for c in inspector.get_columns('user')]
    except Exception:
        return

    if 'login_token' not in user_cols:
        try:
            db.session.execute(text('ALTER TABLE user ADD COLUMN login_token VARCHAR(64)'))
            db.session.commit()
            print("[Migrate] Added user.login_token column")
        except Exception as e:
            print(f"[Migrate] login_token add skipped: {e}")
    if 'is_trial_used' not in user_cols:
        try:
            db.session.execute(text('ALTER TABLE user ADD COLUMN is_trial_used BOOLEAN DEFAULT 0'))
            db.session.commit()
            print("[Migrate] Added user.is_trial_used column")
        except Exception as e:
            print(f"[Migrate] is_trial_used add skipped: {e}")
    for col_name, ddl in (
        ('telegram_verified', 'ALTER TABLE user ADD COLUMN telegram_verified BOOLEAN DEFAULT 0'),
        ('link_code', 'ALTER TABLE user ADD COLUMN link_code VARCHAR(32)'),
        ('ref_code', 'ALTER TABLE user ADD COLUMN ref_code VARCHAR(32)'),
        ('reg_ip', 'ALTER TABLE user ADD COLUMN reg_ip VARCHAR(64)'),
    ):
        if col_name not in user_cols:
            try:
                db.session.execute(text(ddl))
                db.session.commit()
                print(f"[Migrate] Added user.{col_name} column")
            except Exception as e:
                print(f"[Migrate] {col_name} add skipped: {e}")

    # Config geo columns
    try:
        config_cols = [c['name'] for c in inspector.get_columns('config')]
    except Exception:
        config_cols = []
    if config_cols:
        if 'country' not in config_cols:
            try:
                db.session.execute(text('ALTER TABLE config ADD COLUMN country VARCHAR(64)'))
                db.session.commit()
                print("[Migrate] Added config.country column")
            except Exception as e:
                print(f"[Migrate] country add skipped: {e}")
        if 'country_code' not in config_cols:
            try:
                db.session.execute(text('ALTER TABLE config ADD COLUMN country_code VARCHAR(4)'))
                db.session.commit()
                print("[Migrate] Added config.country_code column")
            except Exception as e:
                print(f"[Migrate] country_code add skipped: {e}")

    # Subscription notification columns
    try:
        sub_cols = [c['name'] for c in inspector.get_columns('subscription')]
    except Exception:
        sub_cols = []
    if sub_cols:
        if 'notified_24h' not in sub_cols:
            try:
                db.session.execute(text('ALTER TABLE subscription ADD COLUMN notified_24h BOOLEAN DEFAULT 0'))
                db.session.commit()
            except Exception as e:
                print(f"[Migrate] notified_24h add skipped: {e}")
        if 'notified_expired' not in sub_cols:
            try:
                db.session.execute(text('ALTER TABLE subscription ADD COLUMN notified_expired BOOLEAN DEFAULT 0'))
                db.session.commit()
            except Exception as e:
                print(f"[Migrate] notified_expired add skipped: {e}")

    # SupportMessage email column
    try:
        sup_cols = [c['name'] for c in inspector.get_columns('support_message')]
    except Exception:
        sup_cols = []
    if sup_cols and 'sender_email' not in sup_cols:
        try:
            db.session.execute(text('ALTER TABLE support_message ADD COLUMN sender_email VARCHAR(120)'))
            db.session.commit()
        except Exception as e:
            print(f"[Migrate] sender_email add skipped: {e}")

def create_app():
    flask_app = Flask(__name__)
    flask_app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'volta-secret-key-2026')
    
    # Ensure instance directory exists
    os.makedirs(flask_app.instance_path, exist_ok=True)
    db_path = os.path.join(flask_app.instance_path, 'vpnhub.db')
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{db_path}')
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Config options for payment gateways
    flask_app.config['PLATEGA_MERCHANT_ID'] = os.getenv('PLATEGA_MERCHANT_ID')
    flask_app.config['PLATEGA_SECRET'] = os.getenv('PLATEGA_SECRET')
    flask_app.config['PLATEGA_API_KEY'] = os.getenv('PLATEGA_API_KEY')
    flask_app.config['PLATEGA_SHOP_ID'] = os.getenv('PLATEGA_SHOP_ID')
    flask_app.config['CRYPTOBOT_API_TOKEN'] = os.getenv('CRYPTOBOT_API_TOKEN')
    flask_app.config['YOOMONEY_RECEIVER'] = os.getenv('YOOMONEY_RECEIVER', '')
    flask_app.config['YOOMONEY_TOKEN'] = os.getenv('YOOMONEY_TOKEN', '')
    flask_app.config['YOOMONEY_NOTIFICATION_SECRET'] = os.getenv('YOOMONEY_NOTIFICATION_SECRET', '')
    flask_app.config['SUPPORT_EMAIL'] = os.getenv('SUPPORT_EMAIL', 'support@vpn.stas-max.ru')
    flask_app.config['SUPPORT_TELEGRAM'] = os.getenv('SUPPORT_TELEGRAM', '@ILSupport')
    flask_app.config['WEBHOOK_URL'] = os.getenv('WEBHOOK_URL', 'http://localhost:5001')
    flask_app.config['BOT_USERNAME'] = os.getenv('BOT_USERNAME', '')

    db.init_app(flask_app)
    login_manager.init_app(flask_app)
    login_manager.login_view = 'index'

    with flask_app.app_context():
        from app.routes import register_routes
        import app.models
        db.create_all()
        _run_lightweight_migrations()
        register_routes(flask_app)

        # Seed configs immediately if none exist
        from app.collector import seed_default_configs, collect_configs
        from app.models import Config
        try:
            if Config.query.count() == 0:
                seed_default_configs()
        except Exception as e:
            print(f"[Init] Seed notice: {e}")

        if not scheduler.running:
            try:
                scheduler.add_job(func=collect_configs, trigger='interval', hours=1, id='config_collector')
                from app.bot import dispatch_expiry_checks
                scheduler.add_job(func=dispatch_expiry_checks, trigger='interval', minutes=30, id='expiry_notifier')
                scheduler.start()
            except Exception as e:
                print(f"[Scheduler] Start notice: {e}")

    from app.bot import init_bot
    init_bot(flask_app)

    return flask_app

app = create_app()
