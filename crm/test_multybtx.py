"""MultyBTX CRM page: admin-only listing of pages.OfferSignup (Navaratri offer) onboardings."""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import EmployeeProfile
from pages.models import OfferSignup

User = get_user_model()


class MultyBTXAccessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('mbx_admin', password='x')
        EmployeeProfile.objects.filter(user=self.admin).update(crm_role='admin')
        self.rep = User.objects.create_user('mbx_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')

    def test_non_admin_forbidden(self):
        self.client.force_login(self.rep)
        resp = self.client.get(reverse('crm:multybtx_list'))
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('crm:multybtx_list'))
        self.assertEqual(resp.status_code, 302)

    def test_admin_sees_signups_and_stats(self):
        OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Asha', phone='9876543210', place='Trivandrum',
            business_name='Asha Boutique', plan=OfferSignup.Plan.STANDARD, amount=Decimal('7999'),
            status=OfferSignup.Status.PAID, cashfree_order_id='ORDER1',
        )
        OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Vinod', phone='9876543211', place='Kochi',
            business_name='Vinod Mart', plan=OfferSignup.Plan.UNLIMITED, amount=Decimal('9999'),
            status=OfferSignup.Status.PENDING, cashfree_order_id='ORDER2',
        )
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('crm:multybtx_list'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('Asha Boutique', body)
        self.assertIn('Vinod Mart', body)
        # Revenue stat only counts the PAID signup (7999), not the pending one (9999).
        self.assertEqual(resp.context['stats']['revenue'], Decimal('7999'))
        self.assertEqual(resp.context['stats']['paid'], 1)
        self.assertEqual(resp.context['stats']['pending'], 1)

    def test_filter_by_status(self):
        OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Asha', phone='9876543210', place='Trivandrum',
            business_name='Asha Boutique', amount=Decimal('7999'),
            status=OfferSignup.Status.PAID, cashfree_order_id='ORDER3',
        )
        OfferSignup.objects.create(
            offer_slug='navaratri-2026', name='Vinod', phone='9876543211', place='Kochi',
            business_name='Vinod Mart', amount=Decimal('9999'),
            status=OfferSignup.Status.FAILED, cashfree_order_id='ORDER4',
        )
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('crm:multybtx_list'), {'status': 'paid'})
        body = resp.content.decode()
        self.assertIn('Asha Boutique', body)
        self.assertNotIn('Vinod Mart', body)
