"""
Payment gateway adapters (Part 6.8).

Every provider implements the same small interface so the rest of the system never
talks to a provider directly:

    create_order(attempt, link, return_url, notify_url) -> CheckoutSession
    fetch_payments(order_id)                              -> list[GatewayPayment]
    refund(order_id, refund_ref, amount, note)            -> RefundResult
    verify_signature(raw_body, headers)                   -> bool
    parse_webhook(payload, raw_body)                      -> WebhookEvent | None

Credentials come from Company settings (secret stored encrypted, write-only), falling
back to the CASHFREE_* environment variables already used by the website offers.
The built-in sandbox exists only for testing and is refused in live mode.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

from crm.core import crypto
from crm.core.money import D

logger = logging.getLogger('crm.gateway')


class GatewayError(Exception):
    """Raised with a customer-safe message; details go to the log."""


@dataclass
class CheckoutSession:
    order_id: str
    session_id: str
    provider: str
    mode: str = 'sandbox'


@dataclass
class GatewayPayment:
    payment_id: str
    status: str                 # captured | failed | pending | abandoned
    amount: Decimal
    method: str = ''
    failure_code: str = ''
    failure_reason: str = ''
    raw: dict = field(default_factory=dict)


@dataclass
class WebhookEvent:
    event_id: str
    event_type: str            # payment | refund | dispute | other
    order_id: str = ''
    payment: GatewayPayment | None = None
    refund_id: str = ''
    refund_status: str = ''
    dispute_id: str = ''
    dispute_amount: Decimal | None = None
    dispute_reason: str = ''


@dataclass
class RefundResult:
    status: str                # processed | initiated | failed
    gateway_refund_id: str = ''
    error: str = ''


METHOD_MAP = {
    'upi': 'upi', 'credit_card': 'card', 'debit_card': 'card', 'card': 'card', 'prepaid_card': 'card',
    'net_banking': 'netbanking', 'netbanking': 'netbanking', 'wallet': 'wallet', 'app': 'wallet',
    'pay_later': 'other', 'cardless_emi': 'other', 'emi': 'card',
}

FAILURE_WORDS = {
    'USER_DROPPED': 'You closed the payment window before finishing.',
    'TRANSACTION_DECLINED': 'Your bank declined the payment.',
    'INSUFFICIENT_FUNDS': 'The account did not have enough balance.',
    'AUTHENTICATION_FAILED': 'The OTP or PIN check did not pass.',
    'TIMEOUT': 'The bank did not respond in time.',
}


def plain_failure(code: str, reason: str = '') -> str:
    return FAILURE_WORDS.get((code or '').upper(), reason or 'The payment did not go through.')


class BaseAdapter:
    provider = ''
    live = False

    @property
    def mode(self) -> str:
        return 'production' if self.live else 'sandbox'

    def ready(self) -> tuple[bool, str]:
        return False, 'Payments are not configured. Contact administrator.'


class CashfreeAdapter(BaseAdapter):
    """Cashfree Orders API v2023-08-01, same flow as pages/cashfree.py."""
    provider = 'cashfree'
    API_VERSION = '2023-08-01'

    def __init__(self, app_id: str, secret: str, live: bool):
        self.app_id, self.secret, self.live = app_id, secret, live

    def ready(self):
        if not (self.app_id and self.secret):
            return False, 'Payments are not configured. Contact administrator.'
        return True, ''

    def _base(self) -> str:
        return 'https://api.cashfree.com/pg' if self.live else 'https://sandbox.cashfree.com/pg'

    def test_connection(self) -> tuple[bool, str]:
        """Look up an order that cannot exist: 404 proves the keys are accepted, 401/403 means they are not."""
        import requests
        try:
            resp = requests.get(f'{self._base()}/orders/crm-connection-test', headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            logger.warning('Cashfree connection test failed: %s', exc)
            return False, 'Could not reach Cashfree. Check the server\'s internet access and try again.'
        if resp.status_code in (401, 403):
            return False, f'Cashfree rejected the App ID or secret ({self.mode} mode). Check that the keys match the mode.'
        if resp.status_code in (200, 404):
            return True, f'Connected to Cashfree {self.mode}. The keys were accepted.'
        return False, f'Cashfree answered with status {resp.status_code}. Try again in a minute.'

    def _headers(self) -> dict:
        return {'x-client-id': self.app_id, 'x-client-secret': self.secret, 'x-api-version': self.API_VERSION,
                'Content-Type': 'application/json'}

    def _call(self, method: str, path: str, payload=None):
        import requests
        try:
            resp = requests.request(method, f'{self._base()}{path}', json=payload, headers=self._headers(), timeout=15)
        except requests.RequestException as exc:
            logger.error('Cashfree %s %s failed: %s', method, path, exc)
            raise GatewayError('Could not reach the payment gateway. Please try again in a minute.') from exc
        if resp.status_code >= 400:
            logger.error('Cashfree %s %s returned %s: %s', method, path, resp.status_code, resp.text[:500])
            raise GatewayError('The payment gateway rejected this request. Please try again or contact us.')
        try:
            return resp.json()
        except ValueError as exc:
            raise GatewayError('The payment gateway sent an unreadable reply.') from exc

    def create_order(self, attempt, link, return_url: str, notify_url: str) -> CheckoutSession:
        phone = (link.contact_phone or link.client.phone or '').lstrip('+')
        if phone.startswith('91') and len(phone) == 12:
            phone = phone[2:]
        payload = {
            'order_id': attempt.gateway_order_id,
            'order_amount': float(attempt.amount),
            'order_currency': 'INR',
            'customer_details': {
                'customer_id': f'client-{link.client_id}',
                'customer_phone': phone or '9999999999',
                'customer_name': (link.contact_name or link.client.legal_name)[:100],
                'customer_email': link.contact_email or link.client.email or None,
            },
            'order_meta': {'return_url': return_url, 'notify_url': notify_url,
                           'payment_methods': ','.join(_cf_methods(link.methods)) or None},
            'order_note': link.description[:200],
            'order_tags': {'link': link.code},
        }
        payload['customer_details'] = {k: v for k, v in payload['customer_details'].items() if v}
        payload['order_meta'] = {k: v for k, v in payload['order_meta'].items() if v}
        data = self._call('POST', '/orders', payload)
        sid = data.get('payment_session_id')
        if not sid:
            raise GatewayError('The payment gateway did not return a checkout session.')
        return CheckoutSession(order_id=attempt.gateway_order_id, session_id=sid, provider=self.provider, mode=self.mode)

    def fetch_payments(self, order_id: str) -> list[GatewayPayment]:
        data = self._call('GET', f'/orders/{order_id}/payments')
        return [_cf_payment(p) for p in (data if isinstance(data, list) else [])]

    def refund(self, order_id: str, refund_ref: str, amount: Decimal, note: str) -> RefundResult:
        try:
            data = self._call('POST', f'/orders/{order_id}/refunds',
                              {'refund_amount': float(amount), 'refund_id': refund_ref, 'refund_note': note[:100]})
        except GatewayError as exc:
            return RefundResult(status='failed', error=str(exc))
        st = (data.get('refund_status') or '').upper()
        return RefundResult(status='processed' if st == 'SUCCESS' else 'failed' if st in ('CANCELLED', 'FAILED') else 'initiated',
                            gateway_refund_id=str(data.get('cf_refund_id') or ''))

    def verify_signature(self, raw_body: bytes, headers) -> bool:
        ts = headers.get('x-webhook-timestamp', '')
        sig = headers.get('x-webhook-signature', '')
        if not (ts and sig and self.secret):
            return False
        try:
            msg = (ts + raw_body.decode('utf-8')).encode('utf-8')
        except UnicodeDecodeError:
            return False
        computed = base64.b64encode(hmac.new(self.secret.encode(), msg, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(computed, sig)

    def parse_webhook(self, payload: dict, raw_body: bytes) -> WebhookEvent | None:
        etype = str(payload.get('type', ''))
        data = payload.get('data') or {}
        order_id = str((data.get('order') or {}).get('order_id', ''))
        digest = hashlib.sha256(raw_body).hexdigest()[:24]
        if 'REFUND' in etype:
            r = data.get('refund') or {}
            st = (r.get('refund_status') or '').upper()
            return WebhookEvent(event_id=f'{etype}:{r.get("cf_refund_id") or digest}:{st}', event_type='refund',
                                order_id=str(r.get('order_id') or order_id), refund_id=str(r.get('refund_id') or ''),
                                refund_status='processed' if st == 'SUCCESS' else 'failed' if st in ('CANCELLED', 'FAILED') else 'initiated')
        if 'DISPUTE' in etype:
            d = data.get('dispute') or {}
            return WebhookEvent(event_id=f'{etype}:{d.get("dispute_id") or digest}', event_type='dispute',
                                order_id=str((data.get('order_details') or {}).get('order_id') or order_id),
                                dispute_id=str(d.get('dispute_id') or ''), dispute_amount=D(d.get('dispute_amount') or 0),
                                dispute_reason=str(d.get('reason_description') or d.get('dispute_type') or '')[:300])
        if 'PAYMENT' in etype:
            p = dict(data.get('payment') or {})
            if data.get('error_details'):
                p['error_details'] = data['error_details']
            gp = _cf_payment(p)
            return WebhookEvent(event_id=f'{etype}:{gp.payment_id or digest}', event_type='payment', order_id=order_id, payment=gp)
        return WebhookEvent(event_id=f'{etype or "unknown"}:{digest}', event_type='other', order_id=order_id)


def _cf_methods(methods) -> list[str]:
    m = {'upi': 'upi', 'card': 'cc,dc', 'netbanking': 'nb', 'wallet': 'app'}
    return [m[x] for x in methods or [] if x in m]


def _cf_payment(p: dict) -> GatewayPayment:
    st = (p.get('payment_status') or '').upper()
    status = {'SUCCESS': 'captured', 'FAILED': 'failed', 'USER_DROPPED': 'abandoned', 'CANCELLED': 'failed',
              'PENDING': 'pending', 'NOT_ATTEMPTED': 'pending', 'FLAGGED': 'pending'}.get(st, 'pending')
    err = p.get('error_details') or {}
    code = err.get('error_code') or (st if status in ('failed', 'abandoned') else '')
    return GatewayPayment(payment_id=str(p.get('cf_payment_id') or ''), status=status, amount=D(p.get('payment_amount') or 0),
                          method=METHOD_MAP.get(str(p.get('payment_group') or '').lower(), 'other'),
                          failure_code=str(code)[:80], failure_reason=plain_failure(code, err.get('error_description') or '')[:300],
                          raw={k: p.get(k) for k in ('cf_payment_id', 'payment_status', 'payment_amount', 'payment_group',
                                                       'payment_time', 'bank_reference')})


class SandboxAdapter(BaseAdapter):
    """Built-in simulator so the full flow (attempt -> signed webhook -> payment) can be tested without a provider."""
    provider = 'sandbox'

    def __init__(self):
        self.secret = hashlib.sha256(f'crm-sandbox:{settings.SECRET_KEY}'.encode()).hexdigest()

    def ready(self):
        return True, ''

    def create_order(self, attempt, link, return_url, notify_url):
        return CheckoutSession(order_id=attempt.gateway_order_id, session_id=f'sandbox_{attempt.gateway_order_id}',
                               provider=self.provider, mode='sandbox')

    def fetch_payments(self, order_id):
        from crm.models import PaymentAttempt
        a = PaymentAttempt.objects.filter(gateway_order_id=order_id).first()
        if a is None or not a.gateway_payment_id:
            return []
        return [GatewayPayment(payment_id=a.gateway_payment_id, status=a.status if a.status in ('captured', 'failed', 'abandoned') else 'pending',
                               amount=a.amount, method=a.method or 'upi')]

    def refund(self, order_id, refund_ref, amount, note):
        return RefundResult(status='processed', gateway_refund_id=f'sbx_rf_{refund_ref}')

    def sign(self, raw_body: bytes, ts: str) -> str:
        return base64.b64encode(hmac.new(self.secret.encode(), (ts + raw_body.decode()).encode(), hashlib.sha256).digest()).decode()

    def verify_signature(self, raw_body, headers):
        ts, sig = headers.get('x-webhook-timestamp', ''), headers.get('x-webhook-signature', '')
        return bool(ts and sig) and hmac.compare_digest(self.sign(raw_body, ts), sig)

    def parse_webhook(self, payload, raw_body):
        return CashfreeAdapter('', '', False).parse_webhook(payload, raw_body)

    def simulated_event(self, attempt, outcome: str) -> bytes:
        status = {'success': 'SUCCESS', 'failure': 'FAILED', 'dropped': 'USER_DROPPED'}[outcome]
        body = {'type': f'PAYMENT_{"SUCCESS" if outcome == "success" else "FAILED" if outcome == "failure" else "USER_DROPPED"}_WEBHOOK',
                'data': {'order': {'order_id': attempt.gateway_order_id, 'order_amount': float(attempt.amount)},
                         'payment': {'cf_payment_id': f'sbx_{attempt.pk}_{outcome}', 'payment_status': status,
                                     'payment_amount': float(attempt.amount), 'payment_group': 'upi'},
                         'error_details': {} if outcome == 'success' else {'error_code': 'TRANSACTION_DECLINED' if outcome == 'failure' else 'USER_DROPPED'}}}
        return json.dumps(body, separators=(',', ':')).encode()


class NotConfigured(BaseAdapter):
    provider = 'none'


def get_adapter() -> BaseAdapter:
    from crm.models import CompanySettings
    c = CompanySettings.get()
    if c.gateway_provider == 'sandbox':
        if c.gateway_live:
            return NotConfigured()
        return SandboxAdapter()
    if c.gateway_provider == 'cashfree':
        secret = crypto.decrypt(c.gateway_secret_enc) if c.gateway_secret_enc else ''
        return CashfreeAdapter(c.gateway_app_id or settings.CASHFREE_APP_ID, secret or settings.CASHFREE_SECRET_KEY, c.gateway_live)
    if settings.CASHFREE_APP_ID and settings.CASHFREE_SECRET_KEY:
        return CashfreeAdapter(settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY, settings.CASHFREE_ENV == 'production')
    return NotConfigured()


def adapter_for(provider: str) -> BaseAdapter:
    a = get_adapter()
    if a.provider == provider:
        return a
    if provider == 'sandbox':
        from crm.models import CompanySettings
        return NotConfigured() if CompanySettings.get().gateway_live else SandboxAdapter()
    if provider == 'cashfree':
        return CashfreeAdapter(settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY, settings.CASHFREE_ENV == 'production')
    return NotConfigured()


def is_test_mode() -> bool:
    a = get_adapter()
    return a.provider == 'sandbox' or (a.provider == 'cashfree' and not a.live)
