import requests
import uuid
from datetime import datetime, timedelta
from app.models import Payment, Subscription, db
from flask import current_app

PLATEGA_API_URL = "https://platega.io/api/v1"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

def create_platega_payment(user, plan, subscription_id):
    """
    Creates an invoice using Platega.io API.
    Fallback to formatted checkout link if API parameters are missing.
    """
    api_key = current_app.config.get('PLATEGA_API_KEY')
    shop_id = current_app.config.get('PLATEGA_SHOP_ID')

    ext_id = f"platega_{subscription_id}_{uuid.uuid4().hex[:8]}"

    if api_key and shop_id:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "shop_id": shop_id,
            "amount": plan['price'],
            "currency": "RUB",
            "order_id": ext_id,
            "description": f"VPNHUB РџРѕРґРїРёСЃРєР°: {plan['name']}",
            "return_url": current_app.config.get('WEBHOOK_URL', 'http://localhost:5000') + "/dashboard",
        }
        try:
            response = requests.post(f"{PLATEGA_API_URL}/invoices", json=payload, headers=headers, timeout=10)
            data = response.json()
            if data.get('id'):
                payment = Payment(
                    user_id=user.id,
                    amount=plan['price'],
                    plan=plan['name'],
                    payment_method='platega',
                    external_id=str(data['id']),
                    status='pending'
                )
                db.session.add(payment)
                db.session.commit()
                return data.get('pay_url') or data.get('url'), str(data['id'])
        except Exception as e:
            print(f"[Payment] Platega API error: {e}")

    # Fallback checkout URL
    payment_url = f"https://platega.io/pay?amount={plan['price']}&currency=RUB&order_id={ext_id}"
    payment = Payment(
        user_id=user.id,
        amount=plan['price'],
        plan=plan['name'],
        payment_method='platega',
        external_id=ext_id,
        status='pending'
    )
    db.session.add(payment)
    db.session.commit()
    return payment_url, ext_id

def create_cryptobot_payment(user, plan, subscription_id):
    """
    Creates an invoice using CryptoBot API (@CryptoBot / CryptoPay).
    Fallback to direct bot link if API key is missing.
    """
    api_token = current_app.config.get('CRYPTOBOT_API_TOKEN')
    ext_id = f"crypto_{subscription_id}_{uuid.uuid4().hex[:8]}"

    if api_token:
        headers = {"Crypto-Pay-API-Token": api_token, "Content-Type": "application/json"}
        payload = {
            "amount": str(plan['price']),
            "currency_type": "fiat",
            "fiat": "RUB",
            "accepted_assets": "USDT,TON,BTC",
            "description": f"VPNHUB РџРѕРґРїРёСЃРєР°: {plan['name']}",
            "payload": ext_id,
        }
        try:
            response = requests.post(f"{CRYPTOBOT_API_URL}/createInvoice", json=payload, headers=headers, timeout=10)
            data = response.json()
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
        except Exception as e:
            print(f"[Payment] CryptoBot API error: {e}")

    # Fallback to direct CryptoBot link
    payment_url = f"https://t.me/CryptoBot?start=pay_{plan['price']}_RUB"
    payment = Payment(
        user_id=user.id,
        amount=plan['price'],
        plan=plan['name'],
        payment_method='cryptobot',
        external_id=ext_id,
        status='pending'
    )
    db.session.add(payment)
    db.session.commit()
    return payment_url, ext_id

def check_payment_status(external_id, method):
    """
    Checks payment status and activates user subscription if paid.
    """
    payment = Payment.query.filter_by(external_id=str(external_id)).first()
    if not payment:
        return 'pending'

    if payment.status == 'paid':
        return 'paid'

    is_paid = False

    if method == 'platega':
        api_key = current_app.config.get('PLATEGA_API_KEY')
        if api_key:
            headers = {"Authorization": f"Bearer {api_key}"}
            try:
                response = requests.get(f"{PLATEGA_API_URL}/invoices/{external_id}", headers=headers, timeout=10)
                data = response.json()
                if data.get('status') in ['paid', 'completed', 'success']:
                    is_paid = True
            except Exception as e:
                print(f"[Payment] Platega check error: {e}")

    elif method == 'cryptobot':
        api_token = current_app.config.get('CRYPTOBOT_API_TOKEN')
        if api_token:
            headers = {"Crypto-Pay-API-Token": api_token}
            try:
                response = requests.get(f"{CRYPTOBOT_API_URL}/getInvoices?invoice_ids={external_id}", headers=headers, timeout=10)
                data = response.json()
                if data.get('ok') and data.get('result') and len(data['result']) > 0:
                    if data['result'][0].get('status') == 'paid':
                        is_paid = True
            except Exception as e:
                print(f"[Payment] CryptoBot check error: {e}")

    if is_paid:
        payment.status = 'paid'
        payment.paid_at = datetime.utcnow()

        # Map plan name to days precisely
        plan_days = {
            '1 РјРµСЃСЏС†': 30,
            '2 РјРµСЃСЏС†Р°': 60,
            '3 РјРµСЃСЏС†Р°': 90,
            '6 РјРµСЃСЏС†РµРІ': 180,
            '12 РјРµСЃСЏС†РµРІ': 360,
        }
        days_to_add = plan_days.get(payment.plan, 30)

        sub = Subscription.query.filter_by(user_id=payment.user_id).order_by(Subscription.created_at.desc()).first()

        if sub and sub.is_active and not sub.is_expired():
            sub.end_date = sub.end_date + timedelta(days=days_to_add)
        else:
            if not sub:
                sub = Subscription(user_id=payment.user_id, plan=payment.plan, end_date=datetime.utcnow() + timedelta(days=days_to_add))
                db.session.add(sub)
            else:
                sub.plan = payment.plan
                sub.start_date = datetime.utcnow()
                sub.end_date = datetime.utcnow() + timedelta(days=days_to_add)
                sub.is_active = True
                sub.payment_status = 'paid'

        db.session.commit()
        return 'paid'

    return 'pending'

