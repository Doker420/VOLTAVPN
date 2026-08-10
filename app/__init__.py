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
    Adds columns introduced after the DB was first created, without requiring
    Alembic. Safe and idempotent for SQLite.
    """
    from sqlalchemy import text, inspect
    inspector = inspect(db.engine)
    try:
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

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'vpnhub-secret-key-2026')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///vpnhub.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Config options for payment gateways
    # Platega: X-MerchantId + X-Secret (see https://docs.platega.io/)
    app.config['PLATEGA_MERCHANT_ID'] = os.getenv('PLATEGA_MERCHANT_ID')
    app.config['PLATEGA_SECRET'] = os.getenv('PLATEGA_SECRET')
    # Legacy fallbacks (deprecated)
    app.config['PLATEGA_API_KEY'] = os.getenv('PLATEGA_API_KEY')
    app.config['PLATEGA_SHOP_ID'] = os.getenv('PLATEGA_SHOP_ID')
    app.config['CRYPTOBOT_API_TOKEN'] = os.getenv('CRYPTOBOT_API_TOKEN')
    app.config['WEBHOOK_URL'] = os.getenv('WEBHOOK_URL', 'http://localhost:5000')
    # Bot username (without @) for building t.me deep links on the site
    app.config['BOT_USERNAME'] = os.getenv('BOT_USERNAME')

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'index'

    with app.app_context():
        from app import routes, models
        from app.routes import register_routes
        db.create_all()
        _run_lightweight_migrations()
        register_routes(app)

        if not scheduler.running:
            from app.collector import collect_configs
            scheduler.add_job(func=collect_configs, trigger='interval', hours=1, id='config_collector')
            scheduler.start()

            # Trigger initial config fetch in background if empty
            def initial_collect():
                with app.app_context():
                    from app.models import Config
                    if Config.query.count() == 0:
                        collect_configs()

            threading.Thread(target=initial_collect, daemon=True).start()

    from app.bot import init_bot
    init_bot(app)

    return app

app = create_app()
