"""Simple billing: create a bill (with or without a lead), partial/full payment, PDF receipt."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import Bill, BillPayment, EmployeeProfile, Lead
from crm.services.simple_billing import add_payment_to_bill, create_quick_bill

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

    def test_create_bill_redirects_with_preview_flag(self):
        resp = self.client.post('/crm/billing/create/', {
            'lead_id': str(self.lead.pk),
            'item_description': ['Package A'],
            'item_quantity': ['1'],
            'item_unit_price': ['1500'],
            'received_amount': '1500',
            'payment_method': 'cash',
        })
        bill = Bill.objects.get(lead=self.lead)
        self.assertEqual(resp.url, f'/crm/billing/bills/{bill.pk}/?preview=1')

    def test_bill_detail_auto_opens_preview_when_flagged(self):
        resp = self.client.post('/crm/billing/create/', {
            'lead_id': str(self.lead.pk),
            'item_description': ['Package A'],
            'item_quantity': ['1'],
            'item_unit_price': ['1500'],
            'received_amount': '1500',
            'payment_method': 'cash',
        }, follow=True)
        body = resp.content.decode()
        self.assertIn('bill-preview-modal', body)
        self.assertIn("getElementById('bill-preview-modal');\n  if (modal", body)

    def test_bill_detail_does_not_auto_open_preview_normally(self):
        bill = Bill.objects.get(lead=self.lead) if Bill.objects.filter(lead=self.lead).exists() else None
        if bill is None:
            self.client.post('/crm/billing/create/', {
                'lead_id': str(self.lead.pk),
                'item_description': ['Package A'],
                'item_quantity': ['1'],
                'item_unit_price': ['1500'],
                'received_amount': '1500',
                'payment_method': 'cash',
            })
            bill = Bill.objects.get(lead=self.lead)
        body = self.client.get(f'/crm/billing/bills/{bill.pk}/').content.decode()
        self.assertIn('bill-preview-modal', body)  # modal markup always present
        # but not auto-triggered
        self.assertNotIn("getElementById('bill-preview-modal');\n  if (modal", body)

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

    def test_bill_create_prefills_lead_from_query_param(self):
        resp = self.client.get(f'/crm/billing/create/?lead={self.lead.pk}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.lead.name, resp.content.decode())


class AddPaymentServiceTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('billing_pay_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.lead = Lead.objects.create(employee=self.rep, name='Gamma Co', phone='9990003333')
        self.bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[{'description': 'Package', 'quantity': Decimal('1'), 'unit_price': Decimal('10000')}],
            received_amount=Decimal('4000'),
            payment_method=BillPayment.PaymentMethod.CASH,
        )

    def test_second_payment_reduces_balance_and_updates_status(self):
        self.assertEqual(self.bill.balance_due, Decimal('6000.00'))
        payment = add_payment_to_bill(
            self.bill, amount=Decimal('6000'), payment_method=BillPayment.PaymentMethod.UPI,
            created_by=self.rep,
        )
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.amount_paid, Decimal('10000.00'))
        self.assertEqual(self.bill.balance_due, Decimal('0.00'))
        self.assertEqual(self.bill.status, Bill.Status.PAID)
        self.assertEqual(payment.receipt_number, f'{self.bill.bill_number}/P2')
        self.assertTrue(payment.receipt_pdf)

    def test_second_payment_still_partial(self):
        add_payment_to_bill(
            self.bill, amount=Decimal('2000'), payment_method=BillPayment.PaymentMethod.CASH,
            created_by=self.rep,
        )
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.amount_paid, Decimal('6000.00'))
        self.assertEqual(self.bill.balance_due, Decimal('4000.00'))
        self.assertEqual(self.bill.status, Bill.Status.PARTIALLY_PAID)

    def test_bill_pdf_is_regenerated_after_payment(self):
        self.assertTrue(self.bill.pdf_file)
        add_payment_to_bill(
            self.bill, amount=Decimal('6000'), payment_method=BillPayment.PaymentMethod.CASH,
            created_by=self.rep,
        )
        self.bill.refresh_from_db()
        self.assertTrue(self.bill.pdf_file)
        self.bill.pdf_file.open('rb')
        content = self.bill.pdf_file.read()
        self.bill.pdf_file.close()
        self.assertTrue(content.startswith(b'%PDF'))

    def test_payment_receipt_pdf_is_valid(self):
        payment = add_payment_to_bill(
            self.bill, amount=Decimal('1000'), payment_method=BillPayment.PaymentMethod.CASH,
            created_by=self.rep,
        )
        payment.receipt_pdf.open('rb')
        content = payment.receipt_pdf.read()
        payment.receipt_pdf.close()
        self.assertTrue(content.startswith(b'%PDF'))


class BillPaymentViewTests(TestCase):
    def setUp(self):
        self.rep = User.objects.create_user('billing_pay_view_rep', password='x')
        EmployeeProfile.objects.filter(user=self.rep).update(crm_role='sales')
        self.client.force_login(self.rep)
        self.lead = Lead.objects.create(employee=self.rep, name='Delta Co', phone='9990004444')
        self.bill = create_quick_bill(
            created_by=self.rep,
            lead=self.lead,
            line_items=[{'description': 'Package', 'quantity': Decimal('1'), 'unit_price': Decimal('5000')}],
            received_amount=Decimal('2000'),
            payment_method=BillPayment.PaymentMethod.CASH,
        )

    def test_record_payment_via_view(self):
        resp = self.client.post(f'/crm/billing/bills/{self.bill.pk}/payments/add/', {
            'amount': '3000', 'payment_method': 'upi', 'notes': 'final settlement',
        })
        self.assertEqual(resp.status_code, 302)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.balance_due, Decimal('0.00'))
        self.assertEqual(self.bill.status, Bill.Status.PAID)

    def test_record_payment_rejects_zero_amount(self):
        resp = self.client.post(f'/crm/billing/bills/{self.bill.pk}/payments/add/', {
            'amount': '0', 'payment_method': 'cash',
        })
        self.assertEqual(resp.status_code, 302)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.amount_paid, Decimal('2000.00'))  # unchanged

    def test_payment_receipt_download(self):
        self.client.post(f'/crm/billing/bills/{self.bill.pk}/payments/add/', {
            'amount': '3000', 'payment_method': 'cash',
        })
        payment = self.bill.payments.order_by('-created_at').first()
        resp = self.client.get(f'/crm/billing/payments/{payment.pk}/receipt/')
        self.assertEqual(resp.status_code, 200)

    def test_lead_detail_shows_billing_panel(self):
        resp = self.client.get(f'/crm/leads/{self.lead.pk}/')
        body = resp.content.decode()
        self.assertIn(self.bill.bill_number, body)
        self.assertIn('New bill', body)


class StampImageProcessingTests(TestCase):
    """The seal/signature are dark ink on a light background — the opposite
    of the white-on-black logo — so they need the inverted cutout mode or
    they render as a solid block with the ink itself cut out."""

    def test_seal_and_signature_keep_their_own_ink_color(self):
        from crm.billing_constants import ASSETS
        from crm.services.simple_billing import _prepare_stamp_image

        for name in ('seal', 'signature'):
            with self.subTest(asset=name):
                buf, _h = _prepare_stamp_image(ASSETS[name], max_width_pt=70, dark_on_light=True)
                from PIL import Image
                img = Image.open(buf)
                pixels = list(img.getdata())
                opaque = [p for p in pixels if p[3] > 100]
                self.assertGreater(len(opaque), 0, f'{name} produced no visible ink pixels')
                # Ink should not be washed-out near-white (that would mean the
                # background got kept instead of the actual stamp/signature lines).
                r, g, b, _a = opaque[len(opaque) // 2]
                self.assertLess((r + g + b) / 3, 200)

    def test_logo_still_uses_light_on_dark_mode(self):
        from crm.billing_constants import ASSETS
        from crm.services.simple_billing import _prepare_stamp_image

        buf, _h = _prepare_stamp_image(ASSETS['logo'], max_width_pt=70)
        from PIL import Image
        img = Image.open(buf)
        opaque = [p for p in img.getdata() if p[3] > 100]
        self.assertGreater(len(opaque), 0)
        r, g, b, _a = opaque[len(opaque) // 2]
        self.assertEqual((r, g, b), (0, 0, 0))  # logo ink is flattened to black
