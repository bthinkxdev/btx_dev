"""
Minimal Cashfree Payment Gateway (Orders API) client — just the three calls
an offer landing page needs: create an order, check its status, and verify
an incoming webhook's signature. Not a general payments abstraction; add to
it directly if a second payment flow needs the same thing.

Docs: https://docs.cashfree.com/docs/payment-gateway (Orders API v2023-08-01)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API_VERSION = '2023-08-01'


class CashfreeError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.CASHFREE_APP_ID and settings.CASHFREE_SECRET_KEY)


def _base_url() -> str:
    if settings.CASHFREE_ENV == 'production':
        return 'https://api.cashfree.com/pg'
    return 'https://sandbox.cashfree.com/pg'


def sdk_mode() -> str:
    """What the Cashfree JS SDK's Cashfree({mode}) call expects."""
    return 'production' if settings.CASHFREE_ENV == 'production' else 'sandbox'


def _headers() -> dict:
    return {
        'x-client-id': settings.CASHFREE_APP_ID,
        'x-client-secret': settings.CASHFREE_SECRET_KEY,
        'x-api-version': API_VERSION,
        'Content-Type': 'application/json',
    }


def create_order(*, order_id: str, amount, customer_id: str, customer_phone: str,
                  customer_name: str, return_url: str) -> dict:
    """Returns the Cashfree order response (includes payment_session_id)."""
    if not is_configured():
        raise CashfreeError('Cashfree is not configured (CASHFREE_APP_ID/CASHFREE_SECRET_KEY missing).')

    payload = {
        'order_id': order_id,
        'order_amount': float(amount),
        'order_currency': 'INR',
        'customer_details': {
            'customer_id': customer_id,
            'customer_phone': customer_phone,
            'customer_name': customer_name,
        },
        'order_meta': {
            'return_url': return_url,
        },
    }
    try:
        resp = requests.post(f'{_base_url()}/orders', json=payload, headers=_headers(), timeout=15)
    except requests.RequestException as exc:
        logger.error('Cashfree create_order request failed: %s', exc)
        raise CashfreeError('Could not reach the payment gateway. Please try again.') from exc

    if resp.status_code >= 400:
        logger.error('Cashfree create_order failed (%s): %s', resp.status_code, resp.text[:500])
        raise CashfreeError('The payment gateway rejected this request. Please try again.')

    return resp.json()


def fetch_order(order_id: str) -> dict:
    """Returns the Cashfree order response — check `order_status` (ACTIVE/PAID/EXPIRED/...)."""
    try:
        resp = requests.get(f'{_base_url()}/orders/{order_id}', headers=_headers(), timeout=15)
    except requests.RequestException as exc:
        logger.error('Cashfree fetch_order request failed: %s', exc)
        raise CashfreeError('Could not reach the payment gateway.') from exc

    if resp.status_code >= 400:
        logger.error('Cashfree fetch_order failed (%s): %s', resp.status_code, resp.text[:500])
        raise CashfreeError('Could not look up this order.')

    return resp.json()


def verify_webhook_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """HMAC-SHA256(timestamp + raw_body, secret_key), base64-encoded — per
    Cashfree's webhook signature spec. Returns False (not an exception) on
    any malformed input so callers can just `if not verify(...): reject`."""
    if not (timestamp and signature and settings.CASHFREE_SECRET_KEY):
        return False
    try:
        message = (timestamp + raw_body.decode('utf-8')).encode('utf-8')
        computed = base64.b64encode(
            hmac.new(settings.CASHFREE_SECRET_KEY.encode('utf-8'), message, hashlib.sha256).digest()
        ).decode('utf-8')
        return hmac.compare_digest(computed, signature)
    except (UnicodeDecodeError, ValueError):
        return False
