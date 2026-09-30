import pytest
import sys
import os
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta
from app import create_app, db
from app.models import User, Subscription, Payment, AffiliateReward, WithdrawalRequest, Referral, AppSetting
from app.collector import seed_default_configs
from app.payment import activate_paid_subscription, process_affiliate_commission
import app.bot as bot_module


@pytest.fixture
def app():
    app = create_app()
    app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'AFFILIATE_COMMISSION_PERCENT': '75',
        'MIN_WITHDRAWAL_AMOUNT': '100',
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


def test_referral_link_landing_and_registration(client, app):
    """
    Test 1: Accessing /r/<ref_code> saves referrer code in session
    and registers a new user with referred_by_id linked to the referrer.
    """
    with app.app_context():
        referrer = User(
            username="top_partner",
            email="partner@example.com",
            ref_code="partner123",
            affiliate_balance=0.0,
            affiliate_earned_total=0.0,
        )
        referrer.set_password("pass1234")
        db.session.add(referrer)
        db.session.commit()
        referrer_id = referrer.id

    # 1. Landing on /r/partner123
    landing_resp = client.get('/r/partner123', follow_redirects=True)
    assert landing_resp.status_code == 200

    # 2. Registering new invited user
    reg_resp = client.post('/register', data={
        'username': 'referred_friend',
        'email': 'friend@example.com',
        'password': 'friendpass123',
    }, follow_redirects=True)
    assert reg_resp.status_code == 200

    with app.app_context():
        friend = User.query.filter_by(username='referred_friend').first()
        assert friend is not None
        assert friend.referred_by_id == referrer_id

        # Verify Referral log created
        ref_record = Referral.query.filter_by(referrer_id=referrer_id, invited_user_id=friend.id).first()
        assert ref_record is not None


def test_affiliate_75_percent_commission_on_payment(app):
    """
    Test 2: When an invited user pays for a subscription (e.g. 199 ₽),
    the referrer receives 75% (149.25 ₽) into affiliate_balance and affiliate_earned_total.
    """
    with app.app_context():
        referrer = User(
            username="promoter",
            ref_code="promo75",
            affiliate_balance=50.0,
            affiliate_earned_total=50.0,
        )
        referrer.set_password("pass1234")
        db.session.add(referrer)
        db.session.commit()
        ref_id = referrer.id

        client_user = User(
            username="buyer_user",
            referred_by_id=ref_id,
        )
        client_user.set_password("pass1234")
        db.session.add(client_user)
        db.session.commit()
        buyer_id = client_user.id

        # Create paid payment for 1 month (199 ₽)
        payment = Payment(
            user_id=buyer_id,
            amount=199,
            plan='1_month',
            status='pending',
            payment_method='yoomoney',
            external_id=uuid.uuid4().hex,
        )
        db.session.add(payment)
        db.session.commit()

        # Activate paid subscription which triggers commission
        activate_paid_subscription(payment)

        # Check buyer subscription
        sub = Subscription.query.filter_by(user_id=buyer_id).first()
        assert sub is not None
        assert sub.is_active is True
        assert payment.status == 'paid'

        # Check affiliate commission calculation
        referrer = db.session.get(User, ref_id)
        expected_commission = round(199.0 * 0.75, 2)  # 149.25
        assert referrer.affiliate_balance == 50.0 + expected_commission
        assert referrer.affiliate_earned_total == 50.0 + expected_commission

        # Check AffiliateReward entry
        reward = AffiliateReward.query.filter_by(referrer_id=ref_id, payment_id=payment.id).first()
        assert reward is not None
        assert reward.referred_user_id == buyer_id
        assert reward.payment_amount == 199.0
        assert reward.percent == 75.0
        assert reward.reward_amount == expected_commission


def test_affiliate_page_and_withdrawal_flow(client, app):
    """
    Test 3: Web partner page, withdrawal request submission, balance deduction,
    and admin approval & rejection workflow.
    """
    with app.app_context():
        admin = User(
            username="admin_user",
            email="admin@example.com",
            is_admin=True,
        )
        admin.set_password("adminpass123")
        db.session.add(admin)

        partner = User(
            username="earner",
            email="earner@example.com",
            ref_code="earner100",
            affiliate_balance=500.0,
            affiliate_earned_total=1500.0,
        )
        partner.set_password("earnerpass123")
        db.session.add(partner)
        db.session.commit()
        partner_id = partner.id
        admin_id = admin.id

    # 1. Partner views /affiliate page
    client.post('/login', data={'username': 'earner', 'password': 'earnerpass123'})
    resp = client.get('/affiliate')
    assert resp.status_code == 200
    assert b'500' in resp.data
    assert b'75%' in resp.data
    assert b'VoltaVPN' in resp.data

    # 2. Partner requests withdrawal of 400 ₽ via SBP
    with_resp = client.post('/affiliate/withdraw', data={
        'amount': '400',
        'payout_method': 'sbp',
        'payout_details': '+79998887766 (Т-Банк)',
    }, follow_redirects=True)
    assert with_resp.status_code == 200

    with app.app_context():
        partner = User.query.get(partner_id)
        assert partner.affiliate_balance == 100.0  # 500 - 400

        w_req = WithdrawalRequest.query.filter_by(user_id=partner_id, status='pending').first()
        assert w_req is not None
        assert w_req.amount == 400.0
        assert w_req.payout_method == 'sbp'
        assert '+79998887766' in w_req.payout_details
        w_req_id = w_req.id

    # 3. Admin views admin withdrawals tab and approves the request
    client.post('/logout')
    client.post('/login', data={'username': 'admin_user', 'password': 'adminpass123'})
    admin_dash = client.get('/admin')
    assert admin_dash.status_code == 200
    assert b'400' in admin_dash.data

    appr_resp = client.post(f'/admin/withdraw/{w_req_id}/approve', data={
        'comment': 'Выплачено через СБП #99281'
    }, follow_redirects=True)
    assert appr_resp.status_code == 200

    with app.app_context():
        w_req = WithdrawalRequest.query.get(w_req_id)
        assert w_req.status == 'completed'
        assert w_req.processed_at is not None
        assert 'СБП #99281' in w_req.admin_comment


def test_withdrawal_rejection_refunds_partner_balance(client, app):
    """
    Test 4: When an admin rejects a pending withdrawal request,
    the requested amount is automatically refunded back to user's affiliate_balance.
    """
    with app.app_context():
        admin = User(username="admin_sup", is_admin=True)
        admin.set_password("adminpass")
        db.session.add(admin)

        partner = User(username="partner_rej", affiliate_balance=0.0)
        partner.set_password("pass123")
        db.session.add(partner)
        db.session.commit()

        w_req = WithdrawalRequest(
            user_id=partner.id,
            amount=250.0,
            payout_method='card',
            payout_details='2200 0000 0000 1111',
            status='pending',
        )
        db.session.add(w_req)
        db.session.commit()
        w_id = w_req.id
        p_id = partner.id

    client.post('/login', data={'username': 'admin_sup', 'password': 'adminpass'})
    rej_resp = client.post(f'/admin/withdraw/{w_id}/reject', data={
        'comment': 'Некорректный номер карты'
    }, follow_redirects=True)
    assert rej_resp.status_code == 200

    with app.app_context():
        w_req = db.session.get(WithdrawalRequest, w_id)
        partner = db.session.get(User, p_id)
        assert w_req.status == 'rejected'
        assert partner.affiliate_balance == 250.0  # Refunded


def test_custom_commission_percentage_and_settings(client, app):
    """
    Test 5: Admin can change commission percent (e.g. 80%) via settings
    and subsequent subscriptions calculate 80% commission.
    """
    with app.app_context():
        admin = User(username="superadmin", is_admin=True)
        admin.set_password("admin123")
        db.session.add(admin)

        partner = User(username="pro_partner", affiliate_balance=0.0, affiliate_earned_total=0.0)
        partner.set_password("pass123")
        db.session.add(partner)
        db.session.commit()

        client_u = User(username="new_buyer", referred_by_id=partner.id)
        client_u.set_password("pass123")
        db.session.add(client_u)
        db.session.commit()
        p_id = partner.id
        b_id = client_u.id

    client.post('/login', data={'username': 'superadmin', 'password': 'admin123'})
    save_resp = client.post('/admin/settings/save', data={
        'affiliate_commission_percent': '80',
        'min_withdrawal_amount': '150',
        'yoomoney_receiver': '4100118544926615',
        'yoomoney_token': '',
        'yoomoney_secret': '',
        'support_email': 'support@vpn.stas-max.ru',
        'support_telegram': '@ILSupport',
        'required_channel': '',
        'required_channel_url': '',
    }, follow_redirects=True)
    assert save_resp.status_code == 200

    with app.app_context():
        assert AppSetting.get('AFFILIATE_COMMISSION_PERCENT') == '80'
        assert AppSetting.get('MIN_WITHDRAWAL_AMOUNT') == '150'

        # Buyer pays 567 ₽ (3 months)
        payment = Payment(
            user_id=b_id,
            amount=567,
            plan='3_months',
            status='pending',
            payment_method='platega',
            external_id=uuid.uuid4().hex,
        )
        db.session.add(payment)
        db.session.commit()

        activate_paid_subscription(payment)

        partner = db.session.get(User, p_id)
        expected_reward = round(567.0 * 0.80, 2)  # 453.60
        assert partner.affiliate_balance == expected_reward
        assert partner.affiliate_earned_total == expected_reward


def test_bot_get_or_create_user_with_referral(app):
    """
    Test 6: Bot's get_or_create_user automatically links referrer and awards trial.
    """
    bot_module.init_bot(app)

    class MockTGUser:
        def __init__(self, id, username):
            self.id = id
            self.username = username
            self.first_name = username

    with app.app_context():
        referrer = User(
            telegram_id=987654321,
            telegram_verified=True,
            username="bot_referrer",
            ref_code="botref77",
            affiliate_balance=0.0,
        )
        db.session.add(referrer)
        db.session.commit()
        ref_id = referrer.id

        tg_guest = MockTGUser(123456789, "guest_referred")
        user_ctx, sub_ctx = bot_module.get_or_create_user(tg_guest, auto_trial=True, referrer_code="botref77")

        assert user_ctx is not None
        assert user_ctx.referred_by_id == ref_id
        assert sub_ctx is not None
        assert sub_ctx.days_left() <= 3 and sub_ctx.days_left() >= 2
