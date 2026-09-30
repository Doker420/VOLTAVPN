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


def test_mandatory_channel_subscription_gate(app, monkeypatch):
    """
    Test mandatory channel subscription gate when REQUIRED_CHANNEL is set.
    """
    import asyncio
    import app.bot as bot_module
    from app.models import AppSetting
    bot_module.flask_app = app
    bot_module.ADMIN_IDS = []

    with app.app_context():
        AppSetting.set('REQUIRED_CHANNEL', '@voltachannel')
        AppSetting.set('REQUIRED_CHANNEL_URL', 'https://t.me/voltachannel')

    class DummyMessage:
        def __init__(self):
            self.replied_text = None
            self.reply_markup = None

        async def reply_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
            self.replied_text = text
            self.reply_markup = reply_markup
            return self

    class DummyCallbackQuery:
        def __init__(self, data):
            self.data = data
            self.message = DummyMessage()
            self.alert_text = None
            self.is_alert = False

        async def answer(self, text=None, show_alert=False):
            self.alert_text = text
            self.is_alert = show_alert

        async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
            self.message.replied_text = text
            self.message.reply_markup = reply_markup
            return self.message

    class DummyBot:
        def __init__(self):
            self.sent_messages = []

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
            self.sent_messages.append({'chat_id': chat_id, 'text': text, 'reply_markup': reply_markup})

    class DummyContext:
        def __init__(self, bot=None):
            self.bot = bot or DummyBot()
            self.args = []
            self.user_data = {}

    class DummyUpdate:
        def __init__(self, user_id):
            self.effective_user = DummyTgUser(user_id)
            self.message = DummyMessage()
            self.callback_query = None

    async def _run_gate():
        # 1. Non-subscribed user executes /start -> Gated!
        sub_status = {'subscribed': False}

        async def mock_is_subbed(user_id, bot_instance):
            return sub_status['subscribed']

        monkeypatch.setattr(bot_module, 'is_user_subscribed_to_channel', mock_is_subbed)

        dummy_bot = DummyBot()
        ctx = DummyContext(bot=dummy_bot)
        update = DummyUpdate(55443322)

        await bot_module.start_command(update, ctx)
        assert update.message.replied_text is not None
        assert 'Обязательная подписка на канал' in update.message.replied_text
        assert '@voltachannel' in update.message.replied_text
        assert update.message.reply_markup is not None

        # 2. User tries connect command -> Gated!
        connect_update = DummyUpdate(55443322)
        await bot_module.connect_command(connect_update, ctx)
        assert 'Обязательная подписка на канал' in connect_update.message.replied_text

        # 3. User clicks "Я подписался" callback while still not subscribed
        cb_update = DummyUpdate(55443322)
        cb_update.callback_query = DummyCallbackQuery('check_channel_sub')
        await bot_module.check_channel_sub_callback(cb_update, ctx)
        assert cb_update.callback_query.alert_text is not None
        assert 'Вы еще не подписались' in cb_update.callback_query.alert_text

        # 4. User subscribes and clicks "Я подписался" callback again -> Success!
        sub_status['subscribed'] = True
        cb_update2 = DummyUpdate(55443322)
        cb_update2.callback_query = DummyCallbackQuery('check_channel_sub')
        await bot_module.check_channel_sub_callback(cb_update2, ctx)
        assert 'Спасибо за подписку' in cb_update2.callback_query.alert_text
        assert 'Добро пожаловать в VOLTA VPN' in cb_update2.callback_query.message.replied_text

        # 5. Check user was granted 3-day trial after subscription
        with app.app_context():
            user = User.query.filter_by(telegram_id=55443322).first()
            assert user is not None
            sub = Subscription.query.filter_by(user_id=user.id).first()
            assert sub is not None
            assert not sub.is_expired()
            assert sub.days_left() <= 3 and sub.days_left() >= 2

    asyncio.run(_run_gate())


def test_link_telegram_merges_existing_tg_user_without_unique_constraint_error(app, monkeypatch):
    """
    Test linking Telegram when an existing user with the same telegram_id already exists in the database.
    Ensures no SQLite UNIQUE constraint failed error occurs, and data is seamlessly merged.
    """
    import asyncio
    import uuid
    import app.bot as bot_module
    from datetime import datetime, timedelta
    bot_module.flask_app = app
    bot_module.ADMIN_IDS = []

    tg_id = int(str(uuid.uuid4().int)[:9])
    link_code = uuid.uuid4().hex[:12]
    web_username = f"web_{uuid.uuid4().hex[:8]}"
    sub_token = uuid.uuid4().hex

    with app.app_context():
        # Web user registered on website
        web_user = User(
            username=web_username,
            email=f"{web_username}@example.com",
            link_code=link_code,
            telegram_id=None,
        )
        web_user.set_password('webpass123')
        db.session.add(web_user)

        # Telegram user was previously created by bot
        tg_stub_user = User(
            username=f'tg_stub_{tg_id}',
            telegram_id=tg_id,
            telegram_verified=True,
        )
        db.session.add(tg_stub_user)
        db.session.commit()

        # Add subscription to the tg stub
        sub_tg = Subscription(
            user_id=tg_stub_user.id,
            plan='1_month',
            sub_token=sub_token,
            end_date=datetime.utcnow() + timedelta(days=30),
            is_active=True,
        )
        db.session.add(sub_tg)
        db.session.commit()

    class DummyMessage:
        def __init__(self):
            self.replied_text = None
            self.reply_markup = None

        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            self.replied_text = text
            self.reply_markup = reply_markup
            return self

    class DummyBot:
        def __init__(self):
            pass

    class DummyContext:
        def __init__(self, args=None):
            self.bot = DummyBot()
            self.args = args or []
            self.user_data = {}

    class DummyUpdate:
        def __init__(self, user_id):
            self.effective_user = DummyTgUser(user_id)
            self.message = DummyMessage()

    async def _run_link():
        async def mock_is_subbed(user_id, bot_instance):
            return True

        monkeypatch.setattr(bot_module, 'is_user_subscribed_to_channel', mock_is_subbed)

        ctx = DummyContext(args=[f"link_{link_code}"])
        update = DummyUpdate(tg_id)

        # Execute start command with link code
        await bot_module.start_command(update, ctx)

        assert 'Telegram успешно привязан' in update.message.replied_text
        assert web_username in update.message.replied_text

        with app.app_context():
            updated_web_user = User.query.filter_by(username=web_username).first()
            assert updated_web_user.telegram_id == tg_id
            assert updated_web_user.telegram_verified is True
            # Check subscription was transferred
            transferred_sub = Subscription.query.filter_by(sub_token=sub_token).first()
            assert transferred_sub.user_id == updated_web_user.id

    asyncio.run(_run_link())


def test_bot_web_login_command(app):
    """
    Test /login and /web command outputs login token link.
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

    async def _run():
        update = DummyUpdate(77889900)
        await bot_module.web_login_command(update, None)
        assert 'Личный кабинет VoltaVPN' in update.message.replied_text
        assert update.message.reply_markup is not None

    asyncio.run(_run())



