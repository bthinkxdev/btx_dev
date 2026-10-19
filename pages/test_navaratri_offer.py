"""Navaratri offer landing page: form validation, seat-limit gating, and the
Cashfree order/webhook/return flow (Cashfree's own API is always mocked —
these tests never make a real network call)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pages import cashfree
from pages.models import OfferSignup


@override_settings(CASHFREE_APP_ID='test-app-id', CASHFREE_SECRET_KEY='test-secret', CASHFREE_ENV='sandbox')
class NavaratriOfferPageTests(TestCase):
    def test_page_loads_with_form(self):
        resp = self.client.get(reverse('pages:navaratri_offer'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'nvr-form')
        self.assertContains(resp, '7999')

    def test_sold_out_hides_form(self):
        for i in range(50):
            OfferSignup.objects.create(
                offer_slug='navaratri-2026', name=f'N{i}', phone='9000000000', place='X',
                business_name=f'B{i}', amount=Decimal('799'), status=OfferSignup.Status.PAID,
                cashfree_order_id=f'ORDER{i}',
            )
        resp = self.client.get(reverse('pages:navaratri_offer'))
        self.assertNotContains(resp, 'id="nvr-form"')
        self.assertContains(resp, 'offer closed')

    @patch('pages.offer_views.cashfree.create_order')
    def test_submit_creates_pending_signup_and_returns_session(self, mock_create):
        mock_create.return_value = {'payment_session_id': 'session_abc123'}
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Asha Nair', 'phone': '9876543210', 'place': 'Trivandrum', 'business_name': 'Asha Boutique',
            'plan': 'standard',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['payment_session_id'], 'session_abc123')

        signup = OfferSignup.objects.get(business_name='Asha Boutique')
        self.assertEqual(signup.status, OfferSignup.Status.PENDING)
        self.assertEqual(signup.plan, OfferSignup.Plan.STANDARD)
        self.assertEqual(signup.amount, Decimal('7999'))
        self.assertTrue(signup.cashfree_order_id)

    @patch('pages.offer_views.cashfree.create_order')
    def test_submit_unlimited_plan_charges_9999(self, mock_create):
        mock_create.return_value = {'payment_session_id': 'session_xyz'}
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Vinod Kumar', 'phone': '9876543211', 'place': 'Kochi', 'business_name': 'Vinod Mart',
            'plan': 'unlimited',
        })
        self.assertEqual(resp.status_code, 200)
        signup = OfferSignup.objects.get(business_name='Vinod Mart')
        self.assertEqual(signup.plan, OfferSignup.Plan.UNLIMITED)
        self.assertEqual(signup.amount, Decimal('9999'))

    @patch('pages.offer_views.cashfree.create_order')
    def test_submit_ignores_client_submitted_amount(self, mock_create):
        """The price is looked up server-side from the plan — a tampered
        'amount' field in the POST body must have zero effect."""
        mock_create.return_value = {'payment_session_id': 'session_tamper'}
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Tamper Test', 'phone': '9876543212', 'place': 'Kochi', 'business_name': 'Tamper Co',
            'plan': 'standard', 'amount': '1',
        })
        self.assertEqual(resp.status_code, 200)
        signup = OfferSignup.objects.get(business_name='Tamper Co')
        self.assertEqual(signup.amount, Decimal('7999'))

    def test_submit_rejects_invalid_phone(self):
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Asha Nair', 'phone': '123', 'place': 'Trivandrum', 'business_name': 'Asha Boutique',
            'plan': 'standard',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('phone', resp.json()['errors'])
        self.assertFalse(OfferSignup.objects.filter(business_name='Asha Boutique').exists())

    def test_submit_blocked_when_sold_out(self):
        for i in range(50):
            OfferSignup.objects.create(
                offer_slug='navaratri-2026', name=f'N{i}', phone='9000000000', place='X',
                business_name=f'B{i}', amount=Decimal('799'), status=OfferSignup.Status.PAID,
                cashfree_order_id=f'ORDER{i}',
            )
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Late Comer', 'phone': '9876543210', 'place': 'Kochi', 'business_name': 'Late Co',
            'plan': 'standard',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(OfferSignup.objects.filter(business_name='Late Co').exists())

    @patch('pages.offer_views.cashfree.create_order')
    def test_submit_cleans_up_signup_on_gateway_error(self, mock_create):
        mock_create.side_effect = cashfree.CashfreeError('gateway down')
        resp = self.client.post(reverse('pages:navaratri_offer_submit'), {
            'name': 'Asha Nair', 'phone': '9876543210', 'place': 'Trivandrum', 'business_name': 'Asha Boutique',
            'plan': 'standard',
        })
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(OfferSignup.objects.filter(business_name='Asha Boutique').exists())

    @patch('pages.offer_views.cashfree.fetch_order')
    def test_return_view_marks_paid_on_success(self, mock_fetch):
        signup = OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Asha', phone='9876543210', place='Trivandrum',
            business_name='Asha Boutique', amount=Decimal('799'), status=OfferSignup.Status.PENDING,
            cashfree_order_id='ORDERXYZ',
        )
        mock_fetch.return_value = {'order_status': 'PAID'}
        resp = self.client.get(reverse('pages:navaratri_offer_return'), {'order_id': 'ORDERXYZ'})
        self.assertEqual(resp.status_code, 200)
        signup.refresh_from_db()
        self.assertEqual(signup.status, OfferSignup.Status.PAID)
        self.assertIsNotNone(signup.paid_at)
        self.assertContains(resp, "You're onboarded")

    @patch('pages.offer_views.cashfree.fetch_order')
    def test_return_view_marks_failed_on_expiry(self, mock_fetch):
        signup = OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Asha', phone='9876543210', place='Trivandrum',
            business_name='Asha Boutique', amount=Decimal('799'), status=OfferSignup.Status.PENDING,
            cashfree_order_id='ORDEREXP',
        )
        mock_fetch.return_value = {'order_status': 'EXPIRED'}
        resp = self.client.get(reverse('pages:navaratri_offer_return'), {'order_id': 'ORDEREXP'})
        signup.refresh_from_db()
        self.assertEqual(signup.status, OfferSignup.Status.FAILED)
        self.assertContains(resp, "didn't go through")

    def _signed_webhook_post(self, payload: dict):
        body = json.dumps(payload).encode('utf-8')
        timestamp = str(int(timezone.now().timestamp()))
        signature = base64.b64encode(
            hmac.new(b'test-secret', (timestamp + body.decode('utf-8')).encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        return self.client.post(
            reverse('pages:navaratri_offer_webhook'),
            data=body,
            content_type='application/json',
            HTTP_X_WEBHOOK_TIMESTAMP=timestamp,
            HTTP_X_WEBHOOK_SIGNATURE=signature,
        )

    def test_webhook_marks_paid_with_valid_signature(self):
        signup = OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Asha', phone='9876543210', place='Trivandrum',
            business_name='Asha Boutique', amount=Decimal('799'), status=OfferSignup.Status.PENDING,
            cashfree_order_id='ORDERWH1',
        )
        resp = self._signed_webhook_post({
            'type': 'PAYMENT_SUCCESS_WEBHOOK',
            'data': {
                'order': {'order_id': 'ORDERWH1'},
                'payment': {'payment_status': 'SUCCESS', 'cf_payment_id': 'cfpay123'},
            },
        })
        self.assertEqual(resp.status_code, 200)
        signup.refresh_from_db()
        self.assertEqual(signup.status, OfferSignup.Status.PAID)
        self.assertEqual(signup.cashfree_payment_id, 'cfpay123')

    def test_webhook_rejects_bad_signature(self):
        resp = self.client.post(
            reverse('pages:navaratri_offer_webhook'),
            data=json.dumps({'data': {'order': {'order_id': 'X'}, 'payment': {'payment_status': 'SUCCESS'}}}),
            content_type='application/json',
            HTTP_X_WEBHOOK_TIMESTAMP='123',
            HTTP_X_WEBHOOK_SIGNATURE='not-a-real-signature',
        )
        self.assertEqual(resp.status_code, 403)


class CashfreeSignatureTests(TestCase):
    @override_settings(CASHFREE_SECRET_KEY='my-secret')
    def test_verify_webhook_signature_roundtrip(self):
        body = b'{"hello":"world"}'
        timestamp = '1700000000'
        expected = base64.b64encode(
            hmac.new(b'my-secret', (timestamp + body.decode()).encode(), hashlib.sha256).digest()
        ).decode()
        self.assertTrue(cashfree.verify_webhook_signature(body, timestamp, expected))
        self.assertFalse(cashfree.verify_webhook_signature(body, timestamp, 'wrong'))
        self.assertFalse(cashfree.verify_webhook_signature(body, '', expected))
