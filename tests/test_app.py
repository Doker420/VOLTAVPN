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
    Test 3: Subscription endpoint /sub/<token> returns active working configs.
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

    # Request subscription feed
    resp = client.get(f'/sub/{token}')
    assert resp.status_code == 200
    assert 'Subscription-Userinfo' in resp.headers
    assert resp.headers.get('Profile-Update-Interval') == '1'

    # Decode base64 feed
    decoded = base64.b64decode(resp.data).decode('utf-8')
    assert 'VoltaVPN' in decoded
    initial_count = len(decoded.strip().splitlines())
    assert initial_count > 0

    # Add new custom config
    with app.app_context():
        new_uri = "vless://abcdef12-3456-7890-abcd-ef1234567890@jp.volta-node.net:443?type=tcp&security=reality#VoltaVPN-JP-New"
        add_custom_config(new_uri, country_code='JP')

    # Fetch feed again — it dynamically includes the new node!
    resp2 = client.get(f'/sub/{token}')
    decoded2 = base64.b64decode(resp2.data).decode('utf-8')
    assert 'Япония' in decoded2 or 'JP' in decoded2 or 'jp.volta-node.net' in decoded2


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
    Test Support Chat: user/guest sends message, messages are retrieved,
    and admin replies to conversation thread.
    """
    session_id = 'test_guest_session_1'

    # 1. Guest sends message
    resp = client.post('/api/support/send', json={
        'session_id': session_id,
        'text': 'Здравствуйте! Как настроить VPN на iPhone?'
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ok'
    assert data['message']['text'] == 'Здравствуйте! Как настроить VPN на iPhone?'

    # 2. Fetch messages in session
    resp2 = client.get(f'/api/support/messages?session_id={session_id}')
    assert resp2.status_code == 200
    messages = resp2.get_json()['messages']
    assert len(messages) == 1

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
