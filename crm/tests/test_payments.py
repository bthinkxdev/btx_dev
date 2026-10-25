"""Part 6.12 and 7.11 acceptance tests: payment links, webhooks, manual payments, allocation, credit notes, refunds."""
from __future__ import annotations

from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from crm.core import posting
from crm.models import (GatewayEvent, JournalEntry, Payment, PaymentAllocation, Refund, SecurityEvent, TdsReceivable)
from crm.modules.payments import gateway
from crm.modules.payments import services as ps

from .base import CrmTestCase, days


class PaymentLinkTests(CrmTestCase):
    def _link(self, q=None, amount=None, **kw):
        q = q or self.make_quotation()
        return ps.create_link(q, self.u['sales'], purpose=kw.pop('purpose', 'full'), amount=amount or q.grand_total,
                              description='Website', expires_at=timezone.now() + days(5), **kw)

    def _webhook(self, attempt, outcome='success', times=1):
        adapter = gateway.SandboxAdapter()
        raw = adapter.simulated_event(attempt, outcome)
        ts = str(int(timezone.now().timestamp()))
        resp = None
        for _ in range(times):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(reverse('crm:gateway_webhook', args=['sandbox']), raw, content_type='application/json',
                                        HTTP_X_WEBHOOK_TIMESTAMP=ts, HTTP_X_WEBHOOK_SIGNATURE=adapter.sign(raw, ts))
        return resp

    def _attempt(self, link, amount=None):
        a, _ = ps.start_attempt(link, amount=amount, ip='1.2.3.4', return_url='http://t/__ORDER__', notify_url='http://t/n')
        return a

    def test_amount_above_collectable_rejected(self):
        q = self.make_quotation()
        with self.assertRaisesMessage(ps.PaymentError, 'Maximum collectable'):
            self._link(q, amount=q.grand_total + 1)

    def test_draft_quotation_not_linkable(self):
        q = self.make_quotation(accepted=False)
        q.status = 'draft'
        q.save(update_fields=['status'])
        with self.assertRaises(ps.PaymentError):
            self._link(q)

    def test_one_active_link_per_source_and_purpose(self):
        q = self.make_quotation()
        self._link(q)
        with self.assertRaisesMessage(ps.PaymentError, 'already exists'):
            self._link(q)

    def test_same_webhook_five_times_one_payment(self):
        link = self._link()
        attempt = self._attempt(link)
        resp = self._webhook(attempt, times=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Payment.objects.filter(link=link).count(), 1)
        p = Payment.objects.get(link=link)
        self.assertEqual(p.status, 'verified')
        self.assertTrue(p.receipt_number.startswith('RCPT/'))
        self.assertEqual(GatewayEvent.objects.filter(link=link).count(), 1)
        link.refresh_from_db()
        self.assertEqual(link.status, 'paid')
        self.assertEqual(JournalEntry.objects.filter(source_type='payment', source_id=str(p.pk)).count(), 1)
        # Advance before invoice: receipt voucher + customer advance liability.
        self.assertTrue(hasattr(p, 'receipt_voucher'))
        self.assertEqual(self.balance(posting.CUSTOMER_ADVANCES), p.amount)
        self.assertTrue(self.journal_balanced())

    def test_invalid_signature_401_and_security_event(self):
        link = self._link()
        attempt = self._attempt(link)
        raw = gateway.SandboxAdapter().simulated_event(attempt, 'success')
        resp = self.client.post(reverse('crm:gateway_webhook', args=['sandbox']), raw, content_type='application/json',
                                HTTP_X_WEBHOOK_TIMESTAMP='1', HTTP_X_WEBHOOK_SIGNATURE='bad')
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(SecurityEvent.objects.filter(kind='webhook_bad_signature').exists())

    def test_failed_then_success(self):
        link = self._link()
        a1 = self._attempt(link)
        self._webhook(a1, 'failure')
        a1.refresh_from_db()
        self.assertEqual(a1.status, 'failed')
        link.refresh_from_db()
        self.assertEqual(link.status, 'active')
        a2 = self._attempt(link)
        self._webhook(a2, 'success')
        link.refresh_from_db()
        self.assertEqual(link.status, 'paid')
        self.assertEqual(link.attempts.count(), 2)

    def test_paid_after_cancellation_goes_to_review(self):
        link = self._link()
        attempt = self._attempt(link)
        ps.cancel_link(link, self.u['sales'], 'Customer changed scope')
        self._webhook(attempt, 'success')
        p = Payment.objects.get(link=link)
        self.assertEqual(p.status, 'review')
        self.assertEqual(p.receipt_number, '')
        self.assertFalse(PaymentAllocation.objects.filter(payment=p).exists())

    def test_partial_payment_balance(self):
        q = self.make_quotation()          # 10,000 + 18% = 11,800
        self.assertEqual(q.grand_total, Decimal('11800.00'))
        link = self._link(q, allow_partial=True, min_partial_amount=Decimal('1000'))
        attempt = self._attempt(link, amount='5000')
        self._webhook(attempt)
        link.refresh_from_db()
        self.assertEqual(link.status, 'partially_paid')
        self.assertEqual(link.balance, Decimal('6800.00'))
        # Tax share of the 5,000 collected: 5000 * 18/118 = 762.71
        v = Payment.objects.get(link=link).receipt_voucher
        self.assertEqual(v.cgst + v.sgst, Decimal('762.71'))

    def test_public_pages(self):
        link = self._link()
        token = ps.public_token(link)
        r = self.client.get(reverse('crm:pay_page', args=[token]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Pay ₹11,800.00')
        self.assertNotContains(r, link.internal_note or 'zzz-internal')
        self.assertEqual(self.client.get(reverse('crm:pay_page', args=['nope'])).status_code, 404)
        r = self.client.post(reverse('crm:pay_page', args=[token]))
        self.assertEqual(r.status_code, 302)
        attempt = link.attempts.get()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(r['Location'], {'outcome': 'success'})
        self.assertEqual(r.status_code, 302)
        link.refresh_from_db()
        self.assertEqual(link.status, 'paid')
        r = self.client.get(reverse('crm:pay_page', args=[token]) + f'?order={attempt.gateway_order_id}')
        self.assertContains(r, 'Payment received')


class ManualPaymentTests(CrmTestCase):
    def test_submitter_cannot_verify(self):
        inv = self.make_invoice()
        p = ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=inv.balance, payment_date=inv.invoice_date,
                             method='upi', reference_number='UTR1', deposit_account=None)
        with self.assertRaisesMessage(ps.PaymentError, 'different Finance user'):
            ps.verify_manual(p, self.u['finance'])
        inv.refresh_from_db()
        self.assertEqual(inv.balance, inv.grand_total)
        with self.captureOnCommitCallbacks(execute=True):
            ps.verify_manual(p, self.u['finance2'])
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.balance, 0)
        self.assertEqual(self.balance(posting.AR, client_id=self.client_obj.pk), Decimal('0'))
        self.assertTrue(self.journal_balanced())

    def test_tds_clears_invoice(self):
        inv = self.make_invoice(price=Decimal('100000'))       # 1,18,000 with GST
        tds = Decimal('10000')
        p = ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=inv.grand_total - tds, tds_amount=tds,
                             tds_section='194J', payment_date=inv.invoice_date, method='bank_transfer', reference_number='NEFT9')
        with self.captureOnCommitCallbacks(execute=True):
            ps.verify_manual(p, self.u['finance2'])
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.tds_withheld, tds)
        self.assertEqual(TdsReceivable.objects.get(payment=p).amount, tds)
        self.assertEqual(self.balance(posting.TDS_RECEIVABLE), tds)

    def test_proof_required_above_limit(self):
        from crm.models import CompanySettings
        CompanySettings.objects.update(manual_payment_proof_above=Decimal('500'))
        inv = self.make_invoice()
        with self.assertRaisesMessage(ps.PaymentError, 'Attach proof'):
            ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=Decimal('600'),
                             payment_date=inv.invoice_date, method='upi', reference_number='P1')

    def test_duplicate_reference_blocked(self):
        from crm.models import BankAccount
        acct = BankAccount.objects.filter(kind='bank').first()
        inv = self.make_invoice()
        ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=Decimal('100'), payment_date=inv.invoice_date,
                         method='upi', reference_number='SAME', deposit_account=acct)
        with self.assertRaisesMessage(ps.PaymentError, 'already recorded'):
            ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=Decimal('100'),
                             payment_date=inv.invoice_date, method='upi', reference_number='SAME', deposit_account=acct)

    def test_verify_view_forbidden_for_sales(self):
        inv = self.make_invoice()
        p = ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=Decimal('100'), payment_date=inv.invoice_date,
                             method='upi', reference_number='X1')
        self.login('sales')
        r = self.client.post(reverse('crm:payment_action', args=[p.pk, 'verify']))
        self.assertIn(r.status_code, (403, 404))
        p.refresh_from_db()
        self.assertEqual(p.status, 'pending_verification')


class InvoiceFlowTests(CrmTestCase):
    def test_numbers_are_consecutive(self):
        a = self.make_invoice()
        b = self.make_invoice()
        na, nb = int(a.number.rsplit('/', 1)[1]), int(b.number.rsplit('/', 1)[1])
        self.assertEqual(nb, na + 1)
        self.assertTrue(a.pdf_sha256)

    def test_advance_then_invoice_adjustment(self):
        q = self.make_quotation()
        link = ps.create_link(q, self.u['sales'], purpose='full', amount=q.grand_total, description='x',
                              expires_at=timezone.now() + days(3))
        a, _ = ps.start_attempt(link, ip='', return_url='http://t/__ORDER__', notify_url='http://t/n')
        adapter = gateway.SandboxAdapter()
        raw = adapter.simulated_event(a, 'success')
        ts = '1700000000'
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:gateway_webhook', args=['sandbox']), raw, content_type='application/json',
                             HTTP_X_WEBHOOK_TIMESTAMP=ts, HTTP_X_WEBHOOK_SIGNATURE=adapter.sign(raw, ts))
        p = Payment.objects.get(link=link)
        inv = self.make_invoice(quotation=q)
        from crm.modules.billing import services as bs
        with self.captureOnCommitCallbacks(execute=True):
            bs.apply_advances(inv, self.u['finance'], [(p, p.amount)])
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.advance_adjusted, inv.grand_total)
        self.assertEqual(self.balance(posting.CUSTOMER_ADVANCES), Decimal('0'))
        self.assertEqual(self.balance(posting.AR, client_id=self.client_obj.pk), Decimal('0'))
        self.assertTrue(self.journal_balanced())

    def test_cancel_posts_reversal(self):
        from crm.modules.billing import services as bs
        inv = self.make_invoice()
        with self.captureOnCommitCallbacks(execute=True):
            bs.cancel(inv, self.u['accountant'], 'Raised to the wrong client')
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'cancelled')
        self.assertTrue(inv.number)
        self.assertEqual(self.balance(posting.AR, client_id=self.client_obj.pk), Decimal('0'))

    def test_credit_note_and_refund(self):
        from crm.modules.billing import services as bs
        inv = self.make_invoice()
        p = ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=inv.grand_total,
                             payment_date=inv.invoice_date, method='upi', reference_number='R1')
        with self.captureOnCommitCallbacks(execute=True):
            ps.verify_manual(p, self.u['finance2'])
            cn = bs.create_credit_note(inv, self.u['accountant'], taxable=Decimal('5000'), reason='discount', treatment='refund')
        cn.refresh_from_db()
        if cn.status != 'issued':
            from crm.core import approvals
            with self.captureOnCommitCallbacks(execute=True):
                approvals.approve(approvals.pending_for(cn, 'credit_note'), self.u['owner'])
            cn.refresh_from_db()
        self.assertEqual(cn.status, 'issued')
        self.assertEqual(cn.total, Decimal('5900.00'))
        p.refresh_from_db()
        self.assertEqual(ps.held_advance(p), Decimal('5900.00'))
        with self.captureOnCommitCallbacks(execute=True):
            r = ps.request_refund(p, self.u['finance'], amount=Decimal('5900'), reason='service_issue', method='manual')
            from crm.core import approvals
            approvals.approve(approvals.pending_for(r, 'refund'), self.u['finance2'])
            ps.mark_manual_refunded(Refund.objects.get(pk=r.pk), self.u['finance2'], 'IMPS123')
        p.refresh_from_db()
        self.assertEqual(p.status, 'partially_refunded')
        self.assertEqual(self.balance(posting.CUSTOMER_ADVANCES), Decimal('0'))
        self.assertEqual(self.balance(posting.AR, client_id=self.client_obj.pk), Decimal('0'))
        self.assertTrue(self.journal_balanced())

    def test_pages_render(self):
        inv = self.make_invoice()
        self.login('owner')
        for name, args in [('invoice_list', []), ('invoice_detail', [inv.pk]), ('receivables', []), ('advances', []),
                           ('tds_receivable', []), ('credit_note_list', []), ('payment_list', []), ('link_list', []),
                           ('payment_verify_queue', []), ('allocation', []), ('refund_list', []), ('gateway_events', []),
                           ('settlement_list', []), ('payment_record', []), ('client_statement', [self.client_obj.pk])]:
            r = self.client.get(reverse(f'crm:{name}', args=args))
            self.assertEqual(r.status_code, 200, name)
        r = self.client.get(reverse('crm:invoice_create') + f'?client={self.client_obj.pk}')
        self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse('crm:invoice_pdf', args=[inv.pk]))
        self.assertEqual(r['Content-Type'], 'application/pdf')
