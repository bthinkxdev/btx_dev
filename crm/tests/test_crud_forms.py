"""Real POST-level CRUD coverage for the sales pipeline (leads, clients, service
catalogue, quotations, invoices). These forms had no dedicated test file before, and
that's exactly where two real NOT NULL / KeyError crashes were found: a ModelForm
field declared required=False mapped to a model column with no null=True lets
construct_instance() write None into a NOT NULL column (Service.floor_price,
Lead.value), and _style() raised KeyError on a radio/checkbox field with a
validation error (StyledFormMixin._style). Each crash is reproduced here with a
real POST through the real view/URL so a regression shows up as a 500, not just a
unit-level clean() assertion.
"""
from __future__ import annotations

from decimal import Decimal

from django.urls import reverse

from crm.models import Client, Lead, LeadSource, Service

from .base import CrmTestCase, days


class LeadCrudTest(CrmTestCase):
    def test_create_lead_with_valid_data(self):
        self.login('sales')
        src = LeadSource.objects.filter(channel='manual').first() or LeadSource.objects.create(name='Manual', channel='manual')
        r = self.client.post(reverse('crm:lead_create'), {
            'name': 'Test Lead', 'phone': '+919812345678', 'email': '', 'company': 'Test Co', 'city': 'Kochi',
            'state': '32', 'services': [self.service.pk], 'budget_min': '', 'budget_max': '', 'value': '25000',
            'expected_close_date': '', 'decision_maker': '', 'source': src.pk, 'campaign': '', 'referrer': '',
            'priority': 'warm', 'next_follow_up_at': '2027-01-01T10:00', 'notes': '', 'tags': [],
        })
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else '')
        self.assertTrue(Lead.objects.filter(name='Test Lead').exists())

    def test_create_lead_with_blank_value_defaults_instead_of_crashing(self):
        """Regression: create_lead() already coerced blank value/budget to 0; confirm the view-level flow."""
        self.login('sales')
        src = LeadSource.objects.filter(channel='manual').first() or LeadSource.objects.create(name='Manual', channel='manual')
        r = self.client.post(reverse('crm:lead_create'), {
            'name': 'Blank Value Lead', 'phone': '+919812345679', 'email': '', 'company': '', 'city': '',
            'state': '32', 'services': [self.service.pk], 'budget_min': '', 'budget_max': '', 'value': '',
            'expected_close_date': '', 'decision_maker': '', 'source': src.pk, 'campaign': '', 'referrer': '',
            'priority': 'warm', 'next_follow_up_at': '2027-01-01T10:00', 'notes': '', 'tags': [],
        })
        self.assertEqual(r.status_code, 302)
        lead = Lead.objects.get(name='Blank Value Lead')
        self.assertEqual(lead.value, Decimal('0.00'))

    def test_edit_lead_with_blank_value_does_not_500(self):
        """Regression: lead_edit uses form.save(commit=False) directly; a blank 'value' field
        used to raise IntegrityError (NOT NULL constraint failed: crm_lead.value)."""
        from django.utils import timezone
        self.login('sales')
        src = LeadSource.objects.filter(channel='manual').first() or LeadSource.objects.create(name='Manual2', channel='manual')
        lead = Lead.objects.create(name='Edit Me', phone='+919812345680', source=src, value=Decimal('5000'),
                                   owner=self.u['sales'], next_follow_up_at=timezone.now() + days(1))
        r = self.client.post(reverse('crm:lead_edit', kwargs={'pk': lead.pk}), {
            'name': 'Edit Me', 'phone': '+919812345680', 'email': '', 'company': '', 'city': '', 'state': '32',
            'services': [self.service.pk], 'budget_min': '', 'budget_max': '2000', 'value': '', 'expected_close_date': '',
            'decision_maker': '', 'source': lead.source_id, 'campaign': '', 'referrer': '', 'priority': 'warm',
            'next_follow_up_at': '2027-01-01T10:00', 'notes': '', 'tags': [], 'row_version': lead.row_version,
        })
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else '')
        lead.refresh_from_db()
        self.assertEqual(lead.value, Decimal('2000.00'))

    def test_lead_edit_validation_error_on_radio_field_does_not_500(self):
        """Regression: StyledFormMixin._style() raised KeyError: 'class' when a RadioSelect
        or CheckboxSelectMultiple field (base='') had a validation error, because the
        'class' attr was never set for those widgets."""
        from django.utils import timezone
        self.login('sales')
        src = LeadSource.objects.filter(channel='manual').first() or LeadSource.objects.create(name='Manual3', channel='manual')
        lead = Lead.objects.create(name='Radio Err', phone='+919812345681', source=src, value=Decimal('1000'),
                                   owner=self.u['sales'], next_follow_up_at=timezone.now() + days(1))
        # Submitting with no phone and no email fails validation (self.add_error('phone', ...))
        # while 'services' (a CheckboxSelectMultiple) is also present on the form - this is the
        # exact shape that used to crash _style() when re-rendering the bound, errored form.
        r = self.client.post(reverse('crm:lead_edit', kwargs={'pk': lead.pk}), {
            'name': 'Radio Err', 'phone': '', 'email': '', 'company': '', 'city': '', 'state': '32',
            'services': [self.service.pk], 'budget_min': '', 'budget_max': '', 'value': '1000',
            'expected_close_date': '', 'decision_maker': '', 'source': src.pk, 'campaign': '', 'referrer': '',
            'priority': 'warm', 'next_follow_up_at': '', 'notes': '', 'tags': [], 'row_version': lead.row_version,
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Enter a phone number or an email address.')


class ClientCrudTest(CrmTestCase):
    def test_create_client(self):
        self.login('sales')
        r = self.client.post(reverse('crm:client_create'), {
            'legal_name': 'New Client Pvt Ltd', 'display_name': '', 'client_type': 'company', 'industry': '',
            'website': '', 'phone': '+919812340000', 'email': 'newclient@example.test', 'gstin': '', 'pan': '',
            'customer_type': 'unregistered', 'tds_applicable': False, 'tds_section': '', 'billing_line1': 'Line 1',
            'billing_line2': '', 'billing_city': 'Kochi', 'billing_state': '32', 'billing_pincode': '682001',
            'billing_country': 'India', 'shipping_same': True, 'shipping_line1': '', 'shipping_line2': '',
            'shipping_city': '', 'shipping_state': '', 'shipping_pincode': '', 'owner': self.u['sales'].pk,
            'credit_limit': '', 'payment_terms_days': 30, 'dunning_paused': False, 'notes': '',
        })
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else '')
        self.assertTrue(Client.objects.filter(legal_name='New Client Pvt Ltd').exists())


class ServiceCrudTest(CrmTestCase):
    def test_create_service_with_blank_floor_price_and_cost_estimate(self):
        """Regression: ServiceForm.floor_price/cost_estimate were required=False while
        Service.floor_price/cost_estimate have no null=True, raising IntegrityError
        (NOT NULL constraint failed: crm_service.floor_price) on an empty submission."""
        self.login('owner')
        r = self.client.post(reverse('crm:service_create'), {
            'name': 'New Service', 'code': 'new-service', 'category': 'Web', 'description': 'x', 'deliverables': 'x',
            'sac_code': '998314', 'taxability': 'taxable', 'tax_rate': '18', 'default_price': '10000',
            'price_mode': 'exclusive', 'floor_price': '', 'cost_estimate': '', 'unit': 'Nos',
            'billing_model': 'one_time', 'revenue_recognition': 'immediate', 'estimated_hours': '0',
            'task_template': '', 'is_active': True,
        })
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else '')
        s = Service.objects.get(code='new-service')
        self.assertEqual(s.floor_price, Decimal('0.00'))
        self.assertEqual(s.cost_estimate, Decimal('0.00'))


class QuotationBuilderCrudTest(CrmTestCase):
    def test_create_quotation_for_client_with_blank_discount_and_cost_estimate(self):
        from crm.core import india
        self.login('sales')
        today = india.today()
        r = self.client.post(reverse('crm:quotation_create'), {
            'src_client': self.client_obj.pk,
            'title': '', 'reference': '', 'quotation_date': today.isoformat(),
            'valid_until': (today + days(30)).isoformat(), 'owner': self.u['sales'].pk,
            'contact_name': '', 'contact_email': '', 'contact_phone': '', 'customer_name': 'Acme',
            'customer_type': 'unregistered', 'customer_gstin': '', 'customer_state': '32', 'billing_address': '',
            'place_of_supply': '', 'price_mode': 'exclusive', 'discount_mode': '', 'discount_value': '',
            'discount_reason': '', 'advance_percent': '0', 'payment_terms_days': '15', 'balance_terms': '',
            'late_fee_text': '', 'deliverables': '', 'exclusions': '', 'assumptions': '', 'timeline': '',
            'revision_limits': '', 'terms': '', 'signatory_name': '', 'internal_notes': '', 'cost_estimate': '',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '1000',
            'lines-0-service': self.service.pk, 'lines-0-description': 'Store build', 'lines-0-sac_code': '998314',
            'lines-0-quantity': '1', 'lines-0-unit': 'Nos', 'lines-0-unit_price': '10000', 'lines-0-discount_mode': '',
            'lines-0-discount_value': '', 'lines-0-taxability': 'taxable', 'lines-0-tax_rate': '18',
            'ms-TOTAL_FORMS': '0', 'ms-INITIAL_FORMS': '0', 'ms-MIN_NUM_FORMS': '0', 'ms-MAX_NUM_FORMS': '1000',
        })
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else
                         (r.context['lines'].errors if r.status_code == 200 else ''))

    def test_create_invoice_for_client_with_blank_discount_and_tds(self):
        from crm.core import india
        self.login('finance')
        today = india.today()
        r = self.client.post(reverse('crm:invoice_create') + f'?client={self.client_obj.pk}', {
            'src_client': self.client_obj.pk,
            'owner': self.u['finance'].pk, 'invoice_date': today.isoformat(),
            'due_date': (today + days(15)).isoformat(), 'po_number': '', 'milestone_ref': '',
            'customer_name': 'Acme', 'customer_type': 'unregistered', 'customer_gstin': '', 'customer_state': '32',
            'billing_address': '', 'shipping_address': '', 'place_of_supply': '', 'price_mode': 'exclusive',
            'discount_mode': '', 'discount_value': '', 'discount_reason': '', 'tds_expected': '', 'notes': '',
            'terms': '', 'internal_notes': '',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '1000',
            'lines-0-service': self.service.pk, 'lines-0-description': 'Store build', 'lines-0-sac_code': '998314',
            'lines-0-quantity': '1', 'lines-0-unit': 'Nos', 'lines-0-unit_price': '10000', 'lines-0-discount_mode': '',
            'lines-0-discount_value': '', 'lines-0-taxability': 'taxable', 'lines-0-tax_rate': '18',
        })
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(r.status_code, 302, r.context['form'].errors if r.status_code == 200 else
                         (r.context['lines'].errors if r.status_code == 200 else ''))
