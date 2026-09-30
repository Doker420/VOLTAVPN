import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from app.models import User, Subscription, SupportMessage, Payment
from app.bot import get_or_create_user, PLANS, TRIAL_DAYS


@pytest.fixture
def app():
    app = create_app()
    app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


class DummyTgUser:
    def __init__(self, user_id, username='tg_user'):
        self.id = user_id
        self.username = username
        self.first_name = 'Test'


def test_bot_user_auto_grant_3_day_trial(app):
    """
    Test bot auto-grants 3-day trial on first interaction.
    """
    import app.bot as bot_module
    bot_module.flask_app = app

    tg_user = DummyTgUser(12345678, 'telegram_tester')
    user_ctx, sub_ctx = get_or_create_user(tg_user, auto_trial=True)

    assert user_ctx.telegram_id == 12345678
    assert sub_ctx is not None
    assert sub_ctx.plan == 'free_trial'
    assert sub_ctx.days_left() <= 3 and sub_ctx.days_left() >= 2
    assert not sub_ctx.is_expired()


def test_bot_admin_support_reply(app):
    """
    Test admin reply via database model creates SupportMessage for session.
    """
    with app.app_context():
        # User message
        msg1 = SupportMessage(
            session_id='sess_100',
            user_id=None,
            sender_type='guest',
            sender_name='Гость',
            text='Как оплатить?',
        )
        db.session.add(msg1)
        db.session.commit()

        # Admin reply
        msg2 = SupportMessage(
            session_id='sess_100',
            user_id=None,
            sender_type='admin',
            sender_name='Поддержка VOLTA (Admin)',
            text='Через YooMoney или картой на сайте.',
            is_read=True,
        )
        db.session.add(msg2)
        db.session.commit()

        all_msgs = SupportMessage.query.filter_by(session_id='sess_100').all()
        assert len(all_msgs) == 2
        assert all_msgs[1].sender_type == 'admin'
