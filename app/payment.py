import requests
import uuid
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlencode
from app.models import Payment, Subscription, AppSetting, User, AffiliateReward, Referral, db
from flask import current_app

PLATEGA_API_URL = "https://app.platega.io"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"
XROCKET_API_URL = "https://pay.xrocket.exchange"
YOOMONEY_QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm.xml"
YOOMONEY_API_URL = "https://yoomoney.ru/api"


def payment_method_enabled(method):
    """Admin-controlled gateway switch; disabled gateways cannot be used by API."""
    default = 'false' if method.lower() == 'xrocket' else 'true'
    value = AppSetting.get(f'PAYMENT_{method.upper()}_ENABLED', default)
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _xrocket_credentials():
    token = (AppSetting.get('XROCKET_API_TOKEN') or current_app.config.get('XROCKET_API_TOKEN') or '').strip()
    currency = (AppSetting.get('XROCKET_CURRENCY') or current_app.config.get('XROCKET_CURRENCY') or 'USDT').strip().upper()
    try:
        rate = float(AppSetting.get('XROCKET_RUB_RATE') or current_app.config.get('XROCKET_RUB_RATE') or 100)
    except (TypeError, ValueError):
        rate = 100.0
    return token, currency, rate


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


def create_xrocket_payment(user, plan, subscription_id):
    """Create a crypto invoice through xRocket Pay Legacy API."""
    token, currency, rub_rate = _xrocket_credentials()
    if not token or rub_rate <= 0:
        print('[Payment] xRocket not configured (need token and RUB rate).')
        return None, None

    amount = round(float(plan['price']) / rub_rate, 2)
    payload_ref = f'sub_{subscription_id}_{uuid.uuid4().hex[:8]}'
    headers = {'Rocket-Pay-Key': token, 'Content-Type': 'application/json'}
    body = {
        'amount': amount,
        'currency': currency,
        'description': f'VOLTA VPN: {plan["name"]}',
        'payload': payload_ref,
        'callbackUrl': f'{current_app.config.get("WEBHOOK_URL", "").rstrip("/")}/payment/callback/xrocket',
    }
    try:
        response = requests.post(f'{XROCKET_API_URL}/tg-invoices', json=body, headers=headers, timeout=15)
        data = response.json()
        result = data.get('data', data.get('result', data))
        invoice_id = result.get('id') or result.get('invoiceId') or result.get('invoice_id')
        pay_url = result.get('link') or result.get('payUrl') or result.get('pay_url') or result.get('url')
        if response.ok and invoice_id and pay_url:
            payment = Payment(user_id=user.id, amount=plan['price'], plan=plan['name'],
                              payment_method='xrocket', external_id=str(invoice_id), status='pending')
            db.session.add(payment)
            db.session.commit()
            return pay_url, str(invoice_id)
        print(f'[Payment] xRocket create failed (HTTP {response.status_code}): {str(data)[:300]}')
    except Exception as exc:
        print(f'[Payment] xRocket API error: {exc}')
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

    elif method == 'xrocket':
        token, _, _ = _xrocket_credentials()
        if token:
            try:
                response = requests.get(f'{XROCKET_API_URL}/tg-invoices/{external_id}', headers={'Rocket-Pay-Key': token}, timeout=15)
                data = response.json()
                result = data.get('data', data.get('result', data))
                status = str(result.get('status', '')).lower()
                if status in {'paid', 'active_paid', 'completed'} or result.get('paid') is True:
                    is_paid = True
            except Exception as e:
                print(f'[Payment] xRocket check notice: {e}')

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


def process_affiliate_commission(payment):
    """
    Calculates and awards affiliate commission to the referrer for this paid subscription.
    Default commission is 75% (configurable in Admin Panel or .env).
    """
    try:
        user = db.session.get(User, payment.user_id)
        if not user:
            return

        referrer_id = user.referred_by_id
        if not referrer_id:
            # Check Referral table as fallback
            ref = Referral.query.filter(
                (Referral.invited_user_id == user.id) |
                (Referral.invited_telegram_id == user.telegram_id if user.telegram_id else False)
            ).first()
            if ref:
                referrer_id = ref.referrer_id
                user.referred_by_id = referrer_id

        if not referrer_id:
            return

        referrer = db.session.get(User, referrer_id)
        if not referrer or referrer.id == user.id:
            return

        # Check if this payment was already rewarded
        existing_reward = AffiliateReward.query.filter_by(payment_id=payment.id).first()
        if existing_reward:
            return

        try:
            percent_str = AppSetting.get('AFFILIATE_COMMISSION_PERCENT') or current_app.config.get('AFFILIATE_COMMISSION_PERCENT', '75')
            commission_percent = float(percent_str)
        except Exception:
            commission_percent = 75.0

        if commission_percent <= 0:
            return

        reward_amount = round(payment.amount * (commission_percent / 100.0), 2)
        reward = AffiliateReward(
            referrer_id=referrer.id,
            referred_user_id=user.id,
            payment_id=payment.id,
            payment_amount=payment.amount,
            commission_percent=commission_percent,
            reward_amount=reward_amount,
        )
        db.session.add(reward)
        referrer.affiliate_balance = round((referrer.affiliate_balance or 0.0) + reward_amount, 2)
        referrer.affiliate_earned_total = round((referrer.affiliate_earned_total or 0.0) + reward_amount, 2)
        db.session.commit()

        # If referrer has Telegram, send instant notification
        if referrer.telegram_id and referrer.telegram_verified:
            try:
                import app.bot as bot_module
                if bot_module.bot_app and bot_module.bot_app.bot:
                    import html
                    import asyncio
                    base_url = current_app.config.get('WEBHOOK_URL', 'http://localhost:5000').rstrip('/')
                    tg_text = (
                        f"🎉 <b>Партнёрское начисление VoltaVPN!</b>\n\n"
                        f"👤 Реферал: <b>{html.escape(user.username)}</b>\n"
                        f"💳 Сумма оплаты: <b>{payment.amount} ₽</b>\n"
                        f"💰 Начислено партнёрских: <b>+{reward_amount} ₽</b> ({int(commission_percent)}%)\n\n"
                        f"💵 Доступно к выводу: <b>{referrer.affiliate_balance} ₽</b>\n\n"
                        f"Вывести средства можно через команду /partner или в личном кабинете на сайте."
                    )
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💸 Заказать вывод средств", callback_data="aff_withdraw")],
                        [InlineKeyboardButton("🌐 Личный кабинет", url=f"{base_url}/dashboard#affiliate-program")],
                    ])
                    loop = getattr(bot_module.bot_app, '_custom_loop', None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot_module.bot_app.bot.send_message(
                                chat_id=referrer.telegram_id,
                                text=tg_text,
                                parse_mode='HTML',
                                reply_markup=kb
                            ),
                            loop
                        )
            except Exception as e:
                print(f"[Payment] Referrer notification notice: {e}")
    except Exception as e:
        print(f"[Payment] Affiliate processing error: {e}")


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
        sub.notified_24h = False
        sub.notified_expired = False
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
                notified_24h=False,
                notified_expired=False,
            )
            db.session.add(sub)
        else:
            sub.plan = payment.plan
            sub.start_date = datetime.utcnow()
            sub.end_date = datetime.utcnow() + timedelta(days=days_to_add)
            sub.is_active = True
            sub.payment_status = 'paid'
            sub.notified_24h = False
            sub.notified_expired = False
            if not sub.config_link:
                sub.config_link = config_link

    db.session.commit()

    # Process 75% affiliate commission for the referrer
    process_affiliate_commission(payment)

    return sub
