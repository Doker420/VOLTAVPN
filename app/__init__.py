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

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'vpnhub-secret-key-2026')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///vpnhub.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Config options for payment gateways
    app.config['PLATEGA_API_KEY'] = os.getenv('PLATEGA_API_KEY')
    app.config['PLATEGA_SHOP_ID'] = os.getenv('PLATEGA_SHOP_ID')
    app.config['CRYPTOBOT_API_TOKEN'] = os.getenv('CRYPTOBOT_API_TOKEN')
    app.config['WEBHOOK_URL'] = os.getenv('WEBHOOK_URL', 'http://localhost:5000')

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'index'

    with app.app_context():
        from app import routes, models
        from app.routes import register_routes
        db.create_all()
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
