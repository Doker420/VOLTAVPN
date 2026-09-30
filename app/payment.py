import requests
import uuid
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlencode
from app.models import Payment, Subscription, AppSetting, db
from flask import current_app

PLATEGA_API_URL = "https://app.platega.io"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"
YOOMONEY_QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm.xml"
YOOMONEY_API_URL = "https://yoomoney.ru/api"


def _platega_credentials():
    merchant_id = (
        AppSetting.get('PLATEGA_MERCHANT_ID')
        or current_app.config.get('PLATEGA_MERCHANT_ID')
        or current_app.config.get('PLATEGA_SHOP_ID')
    )
    secret = (
        AppSetting.get('PLATEGA_SECRET')
        or current_app.config.get('PLATEGA_SECRET')
        or current_app.config.get('PLATEGA_API_KEY')
    )
    return merchant_id, secret


def _yoomoney_credentials():
    receiver = (
        AppSetting.get('YOOMONEY_RECEIVER')
        or current_app.config.get('YOOMONEY_RECEIVER')
        or '4100118544926615'
    )
    token = (
        AppSetting.get('YOOMONEY_TOKEN')
        or current_app.config.get('YOOMONEY_TOKEN')
    )
    secret = (
        AppSetting.get('YOOMONEY_NOTIFICATION_SECRET')
        or current_app.config.get('YOOMONEY_NOTIFICATION_SECRET')
    )
    return receiver, token, secret


def create_yoomoney_payment(user, plan, subscription_id, payment_type='AC'):
    """
    Creates a YooMoney P2P / Token payment link for card or wallet payments.
    payment_type: 'AC' (Bank cards МИР/Visa/Mastercard), 'PC' (YooMoney wallet).
    Returns (payment_url, label_external_id).
    """
    receiver, token, _ = _yoomoney_credentials()
    base = current_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')
    label = f"sub_{subscription_id}_{uuid.uuid4().hex[:10]}"

    payment = Payment(
        user_id=user.id,
        amount=plan['price'],
        plan=plan['name'],
        payment_method='yoomoney',
        external_id=label,
        status='pending',
    )
    db.session.add(payment)
    db.session.commit()

    params = {
        'receiver': receiver,
        'quickpay-form': 'shop',
        'targets': f"VOLTA VPN: {plan['name']}",
        'paymentType': payment_type,  # 'AC' for bank card, 'PC' for yoomoney
        'sum': str(plan['price']),
        'label': label,
        'successURL': f"{base}/dashboard?payment=success&label={label}",
    }
    payment_url = f"{YOOMONEY_QUICKPAY_URL}?{urlencode(params)}"
    return payment_url, label


def check_yoomoney_payment(external_id):
    """
    Checks YooMoney payment status by token via operation-history API.
    """
    payment = Payment.query.filter_by(external_id=str(external_id)).first()
    if not payment:
        return 'pending'

    if payment.status == 'paid':
        return 'paid'

    _, token, _ = _yoomoney_credentials()
    if not token:
        # If no token configured, cannot auto-check via API (relies on webhook/manual)
        return 'pending'

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    payload = {
        'label': str(external_id),
        'records': 10,
        'type': 'deposition',
    }

    try:
        response = requests.post(
            f"{YOOMONEY_API_URL}/operation-history",
            headers=headers,
            data=payload,
            timeout=15,
        )
        data = response.json()
        operations = data.get('operations', [])
        for op in operations:
            if op.get('status') == 'success':
                op_amount = float(op.get('amount', 0))
                # Account for tiny processor fee if applicable
                if op_amount >= payment.amount * 0.95:
                    activate_paid_subscription(payment)
                    return 'paid'
    except Exception as e:
        print(f"[Payment] YooMoney token check notice: {e}")

    return 'pending'


def process_yoomoney_webhook(data):
    """
    Processes YooMoney HTTP notification (webhook).
    Verifies SHA1 hash signature and activates subscription on success.
    """
    notification_type = data.get('notification_type', '')
    operation_id = data.get('operation_id', '')
    amount = data.get('amount', '')
    currency = data.get('currency', '')
    datetime_str = data.get('datetime', '')
    sender = data.get('sender', '')
    codepro = data.get('codepro', '')
    label = data.get('label', '')
    sha1_hash = data.get('sha1_hash', '')

    if not label:
        return False, "Missing label"

    payment = Payment.query.filter_by(external_id=str(label)).first()
    if not payment:
        return False, "Payment not found"

    _, _, secret = _yoomoney_credentials()
    if secret:
        check_str = f"{notification_type}&{operation_id}&{amount}&{currency}&{datetime_str}&{sender}&{codepro}&{secret}&{label}"
        calculated_hash = hashlib.sha1(check_str.encode('utf-8')).hexdigest()
        if calculated_hash.lower() != str(sha1_hash).lower():
            print(f"[Payment] YooMoney webhook invalid SHA1 signature: got {sha1_hash}, expected {calculated_hash}")
            return False, "Invalid signature"

    if codepro == 'true':
        # Protected with protection code, not yet deposited
        return False, "Protection code required"

    activate_paid_subscription(payment)
    return True, "OK"


def create_platega_payment(user, plan, subscription_id):
    """
    Creates a transaction via Platega POST /v2/transaction/process.
    """
    merchant_id, secret = _platega_credentials()
    if not merchant_id or not secret:
        print("[Payment] Platega not configured (need PLATEGA_MERCHANT_ID / PLATEGA_SECRET).")
        return None, None

    base = current_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')
    payload_ref = f"sub_{subscription_id}_{uuid.uuid4().hex[:8]}"

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
    }
    body = {
        "paymentDetails": {
            "amount": plan['price'],
            "currency": "RUB",
        },
        "description": f"VOLTA VPN: {plan['name']}",
        "return": f"{base}/dashboard",
        "failedUrl": f"{base}/dashboard",
        "payload": payload_ref,
        "metadata": {
            "userId": str(getattr(user, 'telegram_id', None) or user.id),
            "userName": user.username or f"user_{user.id}",
        },
    }

    try:
        response = requests.post(
            f"{PLATEGA_API_URL}/v2/transaction/process",
            json=body, headers=headers, timeout=15,
        )
        try:
            data = response.json()
        except ValueError:
            print(f"[Payment] Platega non-JSON (HTTP {response.status_code}): {response.text[:200]}")
            return None, None

        tx_id = data.get('transactionId')
        pay_url = data.get('url')
        if response.ok and tx_id and pay_url:
            payment = Payment(
                user_id=user.id,
                amount=plan['price'],
                plan=plan['name'],
                payment_method='platega',
                external_id=str(tx_id),
                status='pending',
            )
            db.session.add(payment)
            db.session.commit()
            return pay_url, str(tx_id)

        print(f"[Payment] Platega create failed (HTTP {response.status_code}): {str(data)[:200]}")
    except Exception as e:
        print(f"[Payment] Platega API error: {e}")

    return None, None


def create_cryptobot_payment(user, plan, subscription_id):
    """
    Creates an invoice using CryptoBot API (@CryptoBot / CryptoPay).
    """
    api_token = AppSetting.get('CRYPTOBOT_API_TOKEN') or current_app.config.get('CRYPTOBOT_API_TOKEN')
    if not api_token:
        print("[Payment] CryptoBot not configured (need CRYPTOBOT_API_TOKEN).")
        return None, None

    payload_ref = f"sub_{subscription_id}_{uuid.uuid4().hex[:8]}"
    headers = {"Crypto-Pay-API-Token": api_token, "Content-Type": "application/json"}
    payload = {
        "amount": str(plan['price']),
        "currency_type": "fiat",
        "fiat": "RUB",
        "accepted_assets": "USDT,TON,BTC",
        "description": f"VOLTA VPN: {plan['name']}",
        "payload": payload_ref,
    }
    try:
        response = requests.post(f"{CRYPTOBOT_API_URL}/createInvoice", json=payload, headers=headers, timeout=15)
        try:
            data = response.json()
        except ValueError:
            print(f"[Payment] CryptoBot non-JSON (HTTP {response.status_code}): {response.text[:200]}")
            return None, None
        if data.get('ok') and data.get('result'):
            invoice = data['result']
            inv_id = str(invoice.get('invoice_id'))
            payment = Payment(
                user_id=user.id,
                amount=plan['price'],
                plan=plan['name'],
                payment_method='cryptobot',
                external_id=inv_id,
                status='pending'
            )
            db.session.add(payment)
            db.session.commit()
            return invoice.get('pay_url') or invoice.get('bot_invoice_url'), inv_id
        print(f"[Payment] CryptoBot create failed: {str(data)[:200]}")
    except Exception as e:
        print(f"[Payment] CryptoBot API error: {e}")

    return None, None


def check_payment_status(external_id, method):
    """
    Checks payment status and activates user subscription if paid.
    Supports yoomoney, platega, and cryptobot.
    """
    payment = Payment.query.filter_by(external_id=str(external_id)).first()
    if not payment:
        return 'pending'

    if payment.status == 'paid':
        return 'paid'

    if method == 'yoomoney':
        return check_yoomoney_payment(external_id)

    is_paid = False

    if method == 'platega':
        merchant_id, secret = _platega_credentials()
        if merchant_id and secret:
            headers = {"X-MerchantId": merchant_id, "X-Secret": secret}
            try:
                response = requests.get(f"{PLATEGA_API_URL}/transaction/{external_id}", headers=headers, timeout=15)
                data = response.json()
                if str(data.get('status')).upper() == 'CONFIRMED':
                    is_paid = True
            except Exception as e:
                print(f"[Payment] Platega check notice: {e}")

    elif method == 'cryptobot':
        api_token = AppSetting.get('CRYPTOBOT_API_TOKEN') or current_app.config.get('CRYPTOBOT_API_TOKEN')
        if api_token:
            headers = {"Crypto-Pay-API-Token": api_token}
            try:
                response = requests.get(f"{CRYPTOBOT_API_URL}/getInvoices?invoice_ids={external_id}", headers=headers, timeout=10)
                data = response.json()
                if data.get('ok') and data.get('result') and len(data['result']) > 0:
                    if data['result'][0].get('status') == 'paid':
                        is_paid = True
            except Exception as e:
                print(f"[Payment] CryptoBot check notice: {e}")

    if is_paid:
        activate_paid_subscription(payment)
        return 'paid'

    return 'pending'


def _plan_days(plan_name):
    plan_days = {
        'Бесплатный период (3 дня)': 3,
        'Пробный период 3 дня': 3,
        '3 дня бесплатно': 3,
        '1 месяц': 30,
        '2 месяца': 60,
        '3 месяца': 90,
        '6 месяцев': 180,
        '12 месяцев': 360,
    }
    days = plan_days.get(plan_name)
    if days is None:
        import re
        m = re.match(r'\s*(\d+)', plan_name or '')
        months = int(m.group(1)) if m else 1
        days = months * 30
    return days


def activate_paid_subscription(payment):
    """
    Marks the payment paid and extends/creates the user's subscription.
    """
    if payment.status == 'paid':
        return
    payment.status = 'paid'
    payment.paid_at = datetime.utcnow()

    days_to_add = _plan_days(payment.plan)
    sub = Subscription.query.filter_by(user_id=payment.user_id).order_by(Subscription.created_at.desc()).first()

    base_url = current_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')

    if sub and sub.is_active and not sub.is_expired():
        sub.end_date = sub.end_date + timedelta(days=days_to_add)
        sub.plan = payment.plan
        sub.payment_status = 'paid'
    else:
        sub_token = uuid.uuid4().hex
        config_link = f"{base_url}/sub/{sub_token}"
        if not sub:
            sub = Subscription(
                user_id=payment.user_id,
                plan=payment.plan,
                sub_token=sub_token,
                end_date=datetime.utcnow() + timedelta(days=days_to_add),
                config_link=config_link,
                is_active=True,
                payment_status='paid',
            )
            db.session.add(sub)
        else:
            sub.plan = payment.plan
            sub.start_date = datetime.utcnow()
            sub.end_date = datetime.utcnow() + timedelta(days=days_to_add)
            sub.is_active = True
            sub.payment_status = 'paid'
            if not sub.config_link:
                sub.config_link = config_link

    db.session.commit()
    return sub
