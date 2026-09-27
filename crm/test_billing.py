"""Simple billing: create a bill (with or without a lead), partial/full payment, PDF receipt."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import Bill, BillPayment, EmployeeProfile, Lead
from crm.services.simple_billing import create_quick_bill

User = get_user_model()


class SimpleBillingServiceTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('billing_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.lead = Lead.objects.create(employee=self.rep, name='Acme Co', phone='9990001111')

    def test_bill_with_lead_and_line_items(self):
        bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[
                {'description': 'Starter package', 'quantity': Decimal('1'), 'unit_price': Decimal('5000')},
                {'description': 'Add-on', 'quantity': Decimal('2'), 'unit_price': Decimal('500')},
            ],
            received_amount=Decimal('6000'),
            payment_method=BillPayment.PaymentMethod.UPI,
        )
        self.assertEqual(bill.lead_id, self.lead.pk)
        self.assertIsNone(bill.project_id)
        self.assertIsNone(bill.client_id)
        self.assertEqual(bill.total_amount, Decimal('6000.00'))
        self.assertEqual(bill.amount_paid, Decimal('6000.00'))
        self.assertEqual(bill.balance_due, Decimal('0.00'))
        self.assertEqual(bill.status, Bill.Status.PAID)
        self.assertEqual(bill.line_items.count(), 2)
        self.assertTrue(bill.bill_number)

    def test_bill_with_no_lead_at_all(self):
        bill = create_quick_bill(
            created_by=self.rep,
            lead=None,
            line_items=[{'description': 'Walk-in service', 'quantity': Decimal('1'), 'unit_price': Decimal('1000')}],
            received_amount=Decimal('1000'),
            payment_method=BillPayment.PaymentMethod.CASH,
        )
        self.assertIsNone(bill.lead_id)
        self.assertIsNone(bill.project_id)
        self.assertIsNone(bill.client_id)
        self.assertEqual(bill.total_amount, Decimal('1000.00'))
        self.assertEqual(bill.status, Bill.Status.PAID)

    def test_partial_payment_tracks_balance_due(self):
        bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[{'description': 'Full package', 'quantity': Decimal('1'), 'unit_price': Decimal('10000')}],
            received_amount=Decimal('4000'),
            payment_method=BillPayment.PaymentMethod.CASH,
        )
        self.assertEqual(bill.total_amount, Decimal('10000.00'))
        self.assertEqual(bill.amount_paid, Decimal('4000.00'))
        self.assertEqual(bill.balance_due, Decimal('6000.00'))
        self.assertEqual(bill.status, Bill.Status.PARTIALLY_PAID)

    def test_zero_received_still_creates_bill_as_issued(self):
        bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[{'description': 'Consulting', 'quantity': Decimal('1'), 'unit_price': Decimal('2000')}],
            received_amount=Decimal('0'),
            payment_method=BillPayment.PaymentMethod.CASH,
        )
        self.assertEqual(bill.amount_paid, Decimal('0.00'))
        self.assertEqual(bill.balance_due, Decimal('2000.00'))
        self.assertEqual(bill.status, Bill.Status.ISSUED)
        self.assertEqual(bill.payments.count(), 0)

    def test_pdf_receipt_renders_without_a_project(self):
        bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[{'description': 'Package', 'quantity': Decimal('1'), 'unit_price': Decimal('3000')}],
            received_amount=Decimal('3000'),
            payment_method=BillPayment.PaymentMethod.BANK_TRANSFER,
        )
        self.assertTrue(bill.pdf_file)
        bill.pdf_file.open('rb')
        content = bill.pdf_file.read()
        bill.pdf_file.close()
        self.assertGreater(len(content), 100)
        self.assertTrue(content.startswith(b'%PDF'))


class BillCreateViewTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('billing_view_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)
        self.lead = Lead.objects.create(employee=self.rep, name='Beta Co', phone='9990002222')

    def test_create_bill_via_view_redirects_to_detail(self):
        resp = self.client.post('/crm/billing/create/', {
            'lead_id': str(self.lead.pk),
            'item_description': ['Package A'],
            'item_quantity': ['1'],
            'item_unit_price': ['1500'],
            'received_amount': '1500',
            'payment_method': 'cash',
            'notes': '',
        })
        self.assertEqual(resp.status_code, 302)
        bill = Bill.objects.get(lead=self.lead)
        self.assertEqual(bill.total_amount, Decimal('1500.00'))
        self.assertEqual(bill.status, Bill.Status.PAID)

    def test_create_bill_without_lead_via_view(self):
        resp = self.client.post('/crm/billing/create/', {
            'item_description': ['Custom item'],
            'item_quantity': ['1'],
            'item_unit_price': ['750'],
            'received_amount': '500',
            'payment_method': 'upi',
        })
        self.assertEqual(resp.status_code, 302)
        bill = Bill.objects.get(description='')
        self.assertIsNone(bill.lead_id)
        self.assertEqual(bill.balance_due, Decimal('250.00'))
        self.assertEqual(bill.status, Bill.Status.PARTIALLY_PAID)

    def test_create_bill_with_no_items_shows_error(self):
        resp = self.client.post('/crm/billing/create/', {
            'item_description': [''],
            'item_quantity': ['1'],
            'item_unit_price': ['0'],
            'received_amount': '0',
            'payment_method': 'cash',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Add at least one item', resp.content.decode())
