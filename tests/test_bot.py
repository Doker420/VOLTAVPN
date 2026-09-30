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


def test_bot_instructions_and_faq_commands(app):
    """
    Test bot instructions, FAQ, and terms command outputs.
    """
    import asyncio
    import app.bot as bot_module
    bot_module.flask_app = app

    class DummyMessage:
        def __init__(self):
            self.replied_text = None
            self.reply_markup = None

        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            self.replied_text = text
            self.reply_markup = reply_markup
            return self

    class DummyUpdate:
        def __init__(self, user_id):
            self.effective_user = DummyTgUser(user_id)
            self.message = DummyMessage()
            self.callback_query = None

    async def _run():
        update = DummyUpdate(99887766)
        
        # 1. Instructions command
        await bot_module.instructions_command(update, None)
        assert 'Инструкции по настройке VoltaVPN' in update.message.replied_text
        assert update.message.reply_markup is not None

        # 2. FAQ command
        await bot_module.faq_command(update, None)
        assert 'Часто задаваемые вопросы' in update.message.replied_text

        # 3. Terms command
        await bot_module.terms_command(update, None)
        assert 'No-Logs' in update.message.replied_text
        assert 'Пользовательское соглашение' in update.message.replied_text

        # 4. Admin reply button mode
        class DummyCallbackQuery:
            def __init__(self, data):
                self.data = data
                self.message = DummyMessage()
            async def answer(self, text=None, show_alert=False):
                pass

        class DummyContext:
            def __init__(self):
                self.user_data = {}

        bot_module.ADMIN_IDS = [99887766]
        admin_update = DummyUpdate(99887766)
        admin_update.callback_query = DummyCallbackQuery('rep_sess_custom_99')
        ctx = DummyContext()

        await bot_module.admin_reply_btn_callback(admin_update, ctx)
        assert ctx.user_data.get('pending_reply_session') == 'sess_custom_99'
        assert 'Режим быстрого ответа' in admin_update.callback_query.message.replied_text

        # 5. Admin sends the reply text
        msg_update = DummyUpdate(99887766)
        msg_update.message.text = 'Привет, вот ответ из бота!'
        await bot_module.handle_text_buttons(msg_update, ctx)

        with app.app_context():
            replied = SupportMessage.query.filter_by(session_id='sess_custom_99').first()
            assert replied is not None
            assert replied.text == 'Привет, вот ответ из бота!'
            assert replied.sender_type == 'admin'

    asyncio.run(_run())

