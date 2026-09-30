import pytest
import sys
import os
import uuid
import base64
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta
from app import create_app, db
from app.models import User, Subscription, Config, Payment, SupportMessage, AppSetting
from app.collector import (
    seed_default_configs,
    add_custom_config,
    add_batch_configs,
    get_working_configs,
    generate_subscription_feed,
    probe_all_configs,
    delete_dead_configs,
)
from app.payment import (
    create_yoomoney_payment,
    check_yoomoney_payment,
    process_yoomoney_webhook,
    activate_paid_subscription,
)


@pytest.fixture
def app():
    app = create_app()
    app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'YOOMONEY_RECEIVER': '4100118544926615',
        'YOOMONEY_TOKEN': 'test_token',
        'YOOMONEY_NOTIFICATION_SECRET': 'test_secret',
    })

    with app.app_context():
        db.create_all()
        seed_default_configs()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_site_registration_grants_3_days_trial(client, app):
    """
    Test 1: Web registration creates account, auto-logs in,
    and provisions a 3-day free trial subscription immediately.
    """
    resp = client.post('/register', data={
        'username': 'testuser1',
        'email': 'testuser1@example.com',
        'password': 'password123',
        'confirm_password': 'password123',
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b'dashboard' in resp.request.path.encode() or b'3 \xd0\xb4\xd0\xbd' in resp.data or b'VOLTA' in resp.data

    with app.app_context():
        user = User.query.filter_by(username='testuser1').first()
        assert user is not None
        assert user.is_trial_used is True
        assert user.email == 'testuser1@example.com'

        sub = Subscription.query.filter_by(user_id=user.id).first()
        assert sub is not None
        assert sub.plan == 'free_trial'
        assert sub.is_active is True
        assert sub.is_expired() is False
        assert sub.days_left() <= 3 and sub.days_left() >= 2
        assert 'дн' in sub.time_left_str()
        assert sub.config_link is not None


def test_registration_validation(client, app):
    """
    Test registration validation for duplicate username and short password.
    """
    # Create first user
    client.post('/register', data={
        'username': 'unique_user',
        'password': 'password123',
    })

    # Duplicate username
    resp = client.post('/register', data={
        'username': 'unique_user',
        'password': 'password456',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert 'уже существует'.encode('utf-8') in resp.data

    # Short password
    resp2 = client.post('/register', data={
        'username': 'new_user2',
        'password': '12',
    }, follow_redirects=True)
    assert 'не менее 4 символов'.encode('utf-8') in resp2.data


def test_dynamic_subscription_feed(client, app):
    """
    Test 3: Subscription endpoint /sub/<token> returns active working configs for VPN clients,
    and returns rich web portal for web browsers.
    Auto-updates dynamically when new configs are added.
    """
    # Register user and get sub token
    client.post('/register', data={
        'username': 'sub_user',
        'password': 'password123',
    })

    with app.app_context():
        user = User.query.filter_by(username='sub_user').first()
        sub = Subscription.query.filter_by(user_id=user.id).first()
        token = sub.sub_token

    # 1. VPN client requests subscription feed
    resp = client.get(f'/sub/{token}', headers={'User-Agent': 'v2rayNG/1.8.5'})
    assert resp.status_code == 200
    assert 'Subscription-Userinfo' in resp.headers
    assert resp.headers.get('Profile-Update-Interval') == '1'

    # Decode base64 feed
    decoded = base64.b64decode(resp.data).decode('utf-8')
    assert 'VoltaVPN' in decoded
    initial_count = len(decoded.strip().splitlines())
    assert initial_count > 0

    # 2. Browser requests subscription page -> Web portal HTML
    resp_browser = client.get(f'/sub/{token}', headers={'Accept': 'text/html,application/xhtml+xml', 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'})
    assert resp_browser.status_code == 200
    assert 'VoltaVPN'.encode('utf-8') in resp_browser.data
    assert 'Karing'.encode('utf-8') in resp_browser.data
    assert 'v2rayNG'.encode('utf-8') in resp_browser.data
    assert 'Быстрый импорт'.encode('utf-8') in resp_browser.data

    # Add new custom config (explicitly marked working in unit test environment)
    with app.app_context():
        new_uri = "vless://abcdef12-3456-7890-abcd-ef1234567890@jp.volta-node.net:443?type=tcp&security=reality#VoltaVPN-JP-New"
        add_custom_config(new_uri, country_code='JP', is_working=True)

    # Fetch feed again — it dynamically includes the new node!
    resp2 = client.get(f'/sub/{token}', headers={'User-Agent': 'Karing/1.0'})
    decoded2 = base64.b64decode(resp2.data).decode('utf-8')
    assert 'Япония' in decoded2 or 'JP' in decoded2 or 'jp.volta-node.net' in decoded2


def test_tg_login_and_update_profile(client, app):
    """
    Test seamless Telegram 1-click web login (/tg-login/<token>)
    and updating profile credentials in personal cabinet (/update-profile).
    """
    with app.app_context():
        user = User(
            username='tg_bot_user',
            telegram_id=987654321,
            telegram_verified=True,
            login_token='secret_login_token_abc',
        )
        db.session.add(user)
        db.session.commit()

    # 1. 1-click login from Telegram bot
    resp = client.get('/tg-login/secret_login_token_abc', follow_redirects=True)
    assert resp.status_code == 200
    assert 'Личный кабинет'.encode('utf-8') in resp.data

    # 2. Update profile: set custom password & email
    resp_update = client.post('/update-profile', data={
        'username': 'tg_bot_user_renamed',
        'email': 'myuser@vpn.stas-max.ru',
        'password': 'newpassword123',
    }, follow_redirects=True)
    assert resp_update.status_code == 200

    # 3. Verify user can now also log in with the new password
    client.get('/logout')
    resp_login = client.post('/login', data={
        'username': 'tg_bot_user_renamed',
        'password': 'newpassword123',
    }, follow_redirects=True)
    assert resp_login.status_code == 200
    assert 'Личный кабинет'.encode('utf-8') in resp_login.data


def test_expired_subscription_feed(client, app):
    """
    Test that an expired subscription returns a notice in the feed.
    """
    with app.app_context():
        user = User(username='expired_user', password_hash='hash')
        db.session.add(user)
        db.session.commit()

        sub = Subscription(
            user_id=user.id,
            plan='free_trial',
            sub_token='expired_token_123',
            end_date=datetime.utcnow() - timedelta(days=1),
            is_active=True,
        )
        db.session.add(sub)
        db.session.commit()

    resp = client.get('/sub/expired_token_123')
    assert resp.status_code == 200
    decoded = base64.b64decode(resp.data).decode('utf-8')
    assert 'истекла' in decoded or 'expired' in decoded.lower() or '00000000' in decoded


def test_yoomoney_payment_creation_and_webhook(client, app):
    """
    Test YooMoney P2P/Token payment creation, URL structure, and webhook activation.
    """
    with app.app_context():
        user = User(username='pay_user', password_hash='hash')
        db.session.add(user)
        db.session.commit()

        # 1. Create payment
        plan = {'name': '1 месяц', 'days': 30, 'price': 199}
        pay_url, label = create_yoomoney_payment(user, plan, 1, payment_type='AC')
        assert pay_url.startswith('https://yoomoney.ru/quickpay/confirm.xml')
        assert 'sum=199' in pay_url
        assert f"label={label}" in pay_url

        payment = Payment.query.filter_by(external_id=label).first()
        assert payment is not None
        assert payment.status == 'pending'
        assert payment.payment_method == 'yoomoney'

        # 2. Process YooMoney webhook callback
        import hashlib
        secret = 'test_secret'
        notif_type = 'p2p-incoming'
        op_id = 'test_op_12345'
        amount = '199.00'
        currency = '643'
        dt = '2026-09-30T12:00:00Z'
        sender = '4100123456789'
        codepro = 'false'
        check_str = f"{notif_type}&{op_id}&{amount}&{currency}&{dt}&{sender}&{codepro}&{secret}&{label}"
        sha1_hash = hashlib.sha1(check_str.encode('utf-8')).hexdigest()

        data = {
            'notification_type': notif_type,
            'operation_id': op_id,
            'amount': amount,
            'currency': currency,
            'datetime': dt,
            'sender': sender,
            'codepro': codepro,
            'label': label,
            'sha1_hash': sha1_hash,
        }

        ok, msg = process_yoomoney_webhook(data)
        assert ok is True

        # Check payment is marked paid and subscription extended
        payment = Payment.query.filter_by(external_id=label).first()
        assert payment.status == 'paid'

        sub = Subscription.query.filter_by(user_id=user.id).first()
        assert sub is not None
        assert sub.is_active is True
        assert sub.days_left() >= 29


def test_support_chat_realtime_api(client, app):
    """
    Test Support Chat: user/guest sends message with Name & Email,
    messages are retrieved with sender_email, and admin replies to conversation thread.
    """
    session_id = 'test_guest_session_1'

    # 1. Guest sends message with name and email
    resp = client.post('/api/support/send', json={
        'session_id': session_id,
        'name': 'Алексей',
        'email': 'alex@example.com',
        'text': 'Здравствуйте! Как настроить VPN на iPhone?'
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ok'
    assert data['message']['text'] == 'Здравствуйте! Как настроить VPN на iPhone?'
    assert data['message']['sender_name'] == 'Алексей'
    assert data['message']['sender_email'] == 'alex@example.com'

    # 2. Fetch messages in session
    resp2 = client.get(f'/api/support/messages?session_id={session_id}')
    assert resp2.status_code == 200
    messages = resp2.get_json()['messages']
    assert len(messages) == 1
    assert messages[0]['sender_email'] == 'alex@example.com'

    # 3. Create admin user and login
    with app.app_context():
        admin = User(username='superadmin', is_admin=True)
        admin.set_password('adminpass123')
        db.session.add(admin)
        db.session.commit()

    client.post('/login', data={'username': 'superadmin', 'password': 'adminpass123'})

    # 4. Admin views chats list
    resp_chats = client.get('/api/admin/support/chats')
    assert resp_chats.status_code == 200
    chats_data = resp_chats.get_json()['chats']
    assert len(chats_data) >= 1
    assert any(c['session_id'] == session_id for c in chats_data)

    # 5. Admin sends reply
    resp_reply = client.post('/api/admin/support/reply', json={
        'session_id': session_id,
        'text': 'Установите приложение Karing из App Store и вставьте ссылку подписки.'
    })
    assert resp_reply.status_code == 200

    # 6. User re-fetches messages and sees admin reply
    resp3 = client.get(f'/api/support/messages?session_id={session_id}')
    all_msgs = resp3.get_json()['messages']
    assert len(all_msgs) == 2
    assert all_msgs[1]['sender_type'] == 'admin'
    assert 'Karing' in all_msgs[1]['text']


def test_telegram_unlink_flow(client, app):
    """
    Test Telegram unlinking route in dashboard.
    """
    with app.app_context():
        user = User(username='tg_linked_user', telegram_id=55544433, telegram_verified=True)
        user.set_password('secret123')
        db.session.add(user)
        db.session.commit()

    client.post('/login', data={'username': 'tg_linked_user', 'password': 'secret123'})
    resp = client.post('/unlink-telegram', follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        updated = User.query.filter_by(username='tg_linked_user').first()
        assert updated.telegram_id is None
        assert updated.telegram_verified is False


def test_admin_panel_features(client, app):
    """
    Test Admin Panel: access control, extending user subscriptions,
    toggling configs, adding custom configs, testing all configs.
    """
    with app.app_context():
        admin = User(username='admin_boss', is_admin=True)
        admin.set_password('boss1234')
        normal_user = User(username='regular_user')
        normal_user.set_password('user1234')
        db.session.add_all([admin, normal_user])
        db.session.commit()
        normal_id = normal_user.id

    # Normal user is blocked from /admin
    client.post('/login', data={'username': 'regular_user', 'password': 'user1234'})
    resp_blocked = client.get('/admin')
    assert resp_blocked.status_code == 302  # redirects

    # Admin logs in
    client.get('/logout')
    client.post('/login', data={'username': 'admin_boss', 'password': 'boss1234'})

    # Admin dashboard renders
    resp_admin = client.get('/admin')
    assert resp_admin.status_code == 200
    assert 'Панель управления VOLTA'.encode('utf-8') in resp_admin.data

    # Admin extends regular user's subscription by +30 days
    resp_ext = client.post(f'/admin/user/{normal_id}/extend', data={
        'days': 30,
        'plan_name': '1 месяц'
    }, follow_redirects=True)
    assert resp_ext.status_code == 200

    with app.app_context():
        sub = Subscription.query.filter_by(user_id=normal_id).first()
        assert sub is not None
        assert sub.is_active is True
        assert sub.days_left() >= 29

    # Admin adds batch configs
    batch_text = """
vless://11111111-2222-3333-4444-555555555555@de3.volta-node.net:443?type=tcp&security=reality#VOLTA-DE-Batch1
trojan://pass1234@nl3.volta-node.net:443?security=tls#VOLTA-NL-Batch2
"""
    resp_batch = client.post('/admin/configs/add', data={
        'batch_text': batch_text,
    }, follow_redirects=True)
    assert resp_batch.status_code == 200

    with app.app_context():
        assert Config.query.filter_by(host='de3.volta-node.net').first() is not None
        assert Config.query.filter_by(host='nl3.volta-node.net').first() is not None

    # Admin saves settings (Support, YooMoney, Required Channel)
    resp_settings = client.post('/admin/settings/save', data={
        'support_email': 'support@vpn.stas-max.ru',
        'support_telegram': '@ILSupport',
        'required_channel': '@volta_channel_official',
        'required_channel_url': 'https://t.me/volta_channel_official',
        'yoomoney_wallet': '4100112345678901',
    }, follow_redirects=True)
    assert resp_settings.status_code == 200

    with app.app_context():
        from app.models import AppSetting
        assert AppSetting.get('REQUIRED_CHANNEL') == '@volta_channel_official'
        assert AppSetting.get('REQUIRED_CHANNEL_URL') == 'https://t.me/volta_channel_official'
        assert AppSetting.get('SUPPORT_TELEGRAM') == '@ILSupport'


def test_legal_and_knowledge_base_pages(client, app):
    """
    Test Privacy Policy (/privacy), Terms of Service (/terms),
    Connection Instructions (/instructions), and Knowledge Base / FAQ (/faq).
    """
    # 1. Privacy Policy
    resp_privacy = client.get('/privacy')
    assert resp_privacy.status_code == 200
    assert 'Политика конфиденциальности'.encode('utf-8') in resp_privacy.data
    assert 'No-Logs'.encode('utf-8') in resp_privacy.data

    # 2. Terms of Service
    resp_terms = client.get('/terms')
    assert resp_terms.status_code == 200
    assert 'Пользовательское соглашение'.encode('utf-8') in resp_terms.data
    assert 'оферт'.encode('utf-8') in resp_terms.data

    # 3. Connection Instructions
    resp_instructions = client.get('/instructions')
    assert resp_instructions.status_code == 200
    assert 'Инструкция по подключению'.encode('utf-8') in resp_instructions.data
    assert 'Karing'.encode('utf-8') in resp_instructions.data
    assert 'v2rayNG'.encode('utf-8') in resp_instructions.data

    # 4. FAQ / Knowledge base
    resp_faq = client.get('/faq')
    assert resp_faq.status_code == 200
    assert 'Часто задаваемые вопросы'.encode('utf-8') in resp_faq.data
    assert 'VLESS Reality'.encode('utf-8') in resp_faq.data

    # 5. Client Open Bridge (/open/karing/public)
    resp_open = client.get('/open/karing/public')
    assert resp_open.status_code == 200
    assert 'karing://install-config'.encode('utf-8') in resp_open.data
    assert 'Karing'.encode('utf-8') in resp_open.data

    # 6. Dynamic QR code route (/qr/<token> and /qr/<token>.png)
    resp_qr = client.get('/qr/public')
    assert resp_qr.status_code == 200
    assert resp_qr.mimetype == 'image/png'
    assert len(resp_qr.data) > 100
    assert resp_qr.data[:4] == b'\x89PNG'


def test_batch_config_import_and_parser(client, app):
    """
    Test parsing and importing of multi-format VPN configs:
    - Base64 encoded subscription block
    - Sing-box JSON format outbounds
    - Strict rejection of dead nodes (is_working=False)
    """
    from app.collector import parse_configs_from_text

    # 1. Base64 encoded block
    raw_vless = "vless://11111111-2222-3333-4444-555555555555@de1.example.com:443?security=reality&sni=test.com#DE1"
    raw_trojan = "trojan://password123@nl1.example.com:443?security=tls&sni=test.com#NL1"
    b64_feed = base64.b64encode(f"{raw_vless}\n{raw_trojan}".encode('utf-8')).decode('utf-8')

    parsed_uris = parse_configs_from_text(b64_feed)
    assert len(parsed_uris) == 2
    assert any("de1.example.com" in u for u in parsed_uris)
    assert any("nl1.example.com" in u for u in parsed_uris)

    # 2. Sing-box JSON format
    json_block = json.dumps({
        "outbounds": [
            {
                "type": "vless",
                "tag": "SingBox-DE",
                "server": "singbox.example.com",
                "server_port": 443,
                "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "tls": {
                    "enabled": True,
                    "server_name": "gateway.icloud.com",
                    "reality": {
                        "public_key": "some_pub_key",
                        "short_id": "01234567"
                    }
                }
            }
        ]
    })
    parsed_json = parse_configs_from_text(json_block)
    assert len(parsed_json) == 1
    assert "singbox.example.com" in parsed_json[0]
    assert "vless://" in parsed_json[0]

    # 3. Batch import tests with TCP checking
    with app.app_context():
        # Inserting dead fake hosts without connectivity test -> added, but is_working=False
        added, working = add_batch_configs(b64_feed, test_connectivity=True)
        assert added == 2
        # Since fake domains are unreachable, working count must be 0
        assert working == 0

        cfg = Config.query.filter_by(host="de1.example.com").first()
        assert cfg is not None
        assert cfg.is_working is False


def test_subscription_24h_grace_period_lifecycle(client, app):
    """
    Test +24 hours grace period for subscriptions:
    - Active while now < end_date
    - In grace period when end_date <= now < end_date + 24h
    - Retains working VPN configs feed during grace period
    - Displays grace period indicator and timer
    - Expired when now >= end_date + 24h
    """
    with app.app_context():
        user = User(
            username='grace_user',
            password_hash='dummy_hash',
            email='grace@example.com',
            login_token='grace_login_token',
        )
        db.session.add(user)
        db.session.commit()

        # Case 1: Subscription ended 6 hours ago (in +24h grace window)
        now = datetime.utcnow()
        sub = Subscription(
            user_id=user.id,
            plan='1_month',
            sub_token='grace_sub_token_123',
            start_date=now - timedelta(days=30),
            end_date=now - timedelta(hours=6),
            grace_hours=24,
            is_active=True,
            payment_status='paid',
        )
        db.session.add(sub)
        db.session.commit()

        assert sub.is_in_grace_period() is True
        assert sub.is_expired() is False
        assert user.active_subscription() is not None
        assert user.active_subscription().id == sub.id
        assert "Льготный период" in sub.time_left_str()

    # Subscription feed should serve working configs during grace period
    resp_client = client.get('/sub/grace_sub_token_123', headers={'User-Agent': 'v2rayNG/1.8.5'})
    assert resp_client.status_code == 200
    assert 'Subscription-Userinfo' in resp_client.headers
    assert resp_client.headers.get('Profile-Title') == 'VoltaVPN (Льготный период)'
    decoded = base64.b64decode(resp_client.data).decode('utf-8')
    assert 'VoltaVPN' in decoded

    # Browser portal should show grace period banner
    resp_browser = client.get('/sub/grace_sub_token_123', headers={'Accept': 'text/html'})
    assert resp_browser.status_code == 200
    assert 'Льготный период'.encode('utf-8') in resp_browser.data
    assert '+24 часа на оплату'.encode('utf-8') in resp_browser.data

    # Case 2: Subscription ended 25 hours ago (beyond 24h grace window)
    with app.app_context():
        sub_obj = Subscription.query.filter_by(sub_token='grace_sub_token_123').first()
        sub_obj.end_date = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()

        assert sub_obj.is_in_grace_period() is False
        assert sub_obj.is_expired() is True
        assert user.active_subscription() is None

    # Expired feed should return blocked/expired notice
    resp_expired = client.get('/sub/grace_sub_token_123', headers={'User-Agent': 'v2rayNG/1.8.5'})
    assert resp_expired.status_code == 200
    assert 'VoltaVPN (истекла)' in resp_expired.headers.get('Profile-Title', '')
    decoded_exp = base64.b64decode(resp_expired.data).decode('utf-8')
    assert 'истекла' in decoded_exp or '00000000-0000-0000-0000-000000000000' in decoded_exp



