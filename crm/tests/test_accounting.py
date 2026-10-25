"""Part 8.9 acceptance tests: ledger integrity, periods, reversal, bank import and reconciliation, settlements."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, posting
from crm.models import BankAccount, BankTransaction, JournalEntry, JournalLine, Period
from crm.modules.accounting import banking, reports
from crm.modules.accounting import services as acc

from .base import CrmTestCase


class LedgerTests(CrmTestCase):
    def test_unbalanced_journal_rejected(self):
        with self.assertRaises(posting.PostingError):
            posting.build_journal(entry_date=timezone.localdate(), narration='x', source_type='test', source_id='1', event_key='a',
                                  lines=[posting.Line(posting.AR, debit=Decimal('100')), posting.Line(posting.SERVICE_REVENUE, credit=Decimal('90'))])

    def test_database_rejects_unbalanced_posted_journal(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalEntry.objects.create(entry_date=timezone.localdate(), narration='x', source_type='t', source_id='9', event_key='e',
                                        status='posted', total_debit=Decimal('10'), total_credit=Decimal('9'))

    def test_same_event_posts_once(self):
        inv = self.make_invoice()
        row = posting.enqueue('invoice_issued', inv, 'issued')
        posting.process(row.pk)
        posting.process(row.pk)
        self.assertEqual(JournalEntry.objects.filter(source_type='invoice', source_id=str(inv.pk), event_key='issued').count(), 1)

    def test_invoice_posting_amounts(self):
        inv = self.make_invoice()
        je = JournalEntry.objects.get(source_type='invoice', source_id=str(inv.pk))
        self.assertEqual(je.total_debit, Decimal('11800.00'))
        self.assertEqual(self.balance(posting.SERVICE_REVENUE), Decimal('10000.00'))
        self.assertEqual(self.balance(posting.OUTPUT_CGST) + self.balance(posting.OUTPUT_SGST), Decimal('1800.00'))
        tb = reports.trial_balance(timezone.localdate())
        self.assertTrue(tb['balanced'])
        self.assertTrue(reports.ar_tie_out(timezone.localdate())['ok'])
        self.assertTrue(reports.balance_sheet(timezone.localdate())['balanced'])

    def test_closed_period_rejects_posting(self):
        today = timezone.localdate()
        Period.objects.update_or_create(year=today.year, month=today.month, defaults={'status': 'closed'})
        with self.assertRaisesMessage(posting.PostingError, 'is closed'):
            acc.create_manual_journal(self.u['accountant'], entry_date=today, narration='Test',
                                      lines=[posting.Line('BANK_PRIMARY', debit=Decimal('5')), posting.Line(posting.OWNER_CAPITAL, credit=Decimal('5'))])

    def test_manual_journal_reverse(self):
        je = acc.create_manual_journal(self.u['accountant'], entry_date=timezone.localdate(), narration='Capital introduced',
                                       lines=[posting.Line('BANK_PRIMARY', debit=Decimal('50000')),
                                              posting.Line(posting.OWNER_CAPITAL, credit=Decimal('50000'))])
        self.assertEqual(je.status, 'posted')
        rev = acc.request_reversal(je, self.u['accountant'], 'Entered in the wrong month')
        je.refresh_from_db()
        self.assertEqual(je.status, 'reversed')
        self.assertEqual(rev.reversal_of_id, je.pk)
        self.assertEqual(self.balance(posting.OWNER_CAPITAL), Decimal('0'))

    def test_large_manual_journal_needs_approval(self):
        je = acc.create_manual_journal(self.u['finance'], entry_date=timezone.localdate(), narration='Loan received',
                                       lines=[posting.Line('BANK_PRIMARY', debit=Decimal('500000')), posting.Line('LOANS', credit=Decimal('500000'))])
        self.assertEqual(je.status, 'pending_approval')
        self.assertFalse(JournalLine.objects.filter(entry=je, posted=True).exists())
        req = approvals.pending_for(je)
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(req, self.u['finance'])
        approvals.approve(req, self.u['accountant'])
        je.refresh_from_db()
        self.assertEqual(je.status, 'posted')

    def test_system_journal_cannot_be_reversed_manually(self):
        inv = self.make_invoice()
        je = JournalEntry.objects.get(source_type='invoice', source_id=str(inv.pk))
        with self.assertRaises(acc.AccountingError):
            acc.request_reversal(je, self.u['accountant'], 'Trying to undo an invoice')


class BankTests(CrmTestCase):
    CSV = ('Statement for account\n'
           'Date,Narration,Ref No,Withdrawal,Deposit,Balance\n'
           '01-10-2026,UPI/RENT,R1,25000.00,,75000.00\n'
           '02-10-2026,NEFT ACME KERALA UTR777,UTR777,,11800.00,86800.00\n'
           '03-10-2026,BANK CHARGES,,59.00,,86741.00\n')

    def _file(self):
        return SimpleUploadedFile('stmt.csv', self.CSV.encode(), content_type='text/csv')

    def test_import_twice_flags_duplicates(self):
        bank = BankAccount.objects.filter(kind='bank').first()
        imp1 = banking.import_statement(bank, self._file(), self.u['finance'])
        self.assertEqual(imp1.line_count, 3)
        imp2 = banking.import_statement(bank, self._file(), self.u['finance'])
        self.assertEqual(imp2.line_count, 0)
        self.assertEqual(imp2.duplicate_count, 3)
        self.assertEqual(BankTransaction.objects.filter(bank_account=bank).count(), 3)

    def test_match_create_and_finalise(self):
        from crm.modules.payments import services as ps
        bank = BankAccount.objects.filter(kind='bank').first()
        inv = self.make_invoice()
        p = ps.record_manual(self.u['finance'], client=self.client_obj, invoice=inv, amount=inv.grand_total,
                             payment_date=inv.invoice_date, method='bank_transfer', reference_number='UTR777', deposit_account=bank)
        with self.captureOnCommitCallbacks(execute=True):
            ps.verify_manual(p, self.u['finance2'])
        banking.import_statement(bank, self._file(), self.u['finance'])
        neft = BankTransaction.objects.get(bank_account=bank, reference='UTR777')
        self.assertEqual(neft.status, 'matched')
        charge = BankTransaction.objects.get(bank_account=bank, description='BANK CHARGES')
        banking.create_from_line(charge, self.u['finance'], account=posting.account(posting.BANK_CHARGES))
        rent = BankTransaction.objects.get(bank_account=bank, reference='R1')
        banking.create_from_line(rent, self.u['finance'], account=posting.account('RENT'))
        book = posting.balance(bank.ledger_account)
        self.assertEqual(book, Decimal('11800.00') - Decimal('59.00') - Decimal('25000.00'))
        # The statement started at 1,00,000 opening which is not in the books: finalise must refuse.
        with self.assertRaises(banking.BankError):
            banking.finalise(bank, self.u['accountant'], as_of=timezone.localdate(), statement_balance=Decimal('86741.00'))
        rec = banking.finalise(bank, self.u['accountant'], as_of=timezone.localdate(), statement_balance=book)
        self.assertEqual(BankTransaction.objects.filter(reconciliation=rec).count(), 3)

    def test_settlement_clears_gateway(self):
        from crm.models import Payment
        from crm.modules.payments import services as ps
        from crm.modules.payments import gateway
        q = self.make_quotation()
        link = ps.create_link(q, self.u['sales'], purpose='full', amount=q.grand_total, description='x',
                              expires_at=timezone.now() + timedelta(days=2))
        a, _ = ps.start_attempt(link, ip='', return_url='http://t/__ORDER__', notify_url='http://t/n')
        ad = gateway.SandboxAdapter()
        raw = ad.simulated_event(a, 'success')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('crm:gateway_webhook', args=['sandbox']), raw, content_type='application/json',
                             HTTP_X_WEBHOOK_TIMESTAMP='1', HTTP_X_WEBHOOK_SIGNATURE=ad.sign(raw, '1'))
        p = Payment.objects.get(link=link)
        self.assertEqual(self.balance(posting.GATEWAY_CLEARING), p.amount)
        bank = BankAccount.objects.filter(kind='bank').first()
        with self.captureOnCommitCallbacks(execute=True):
            ps.record_settlement(self.u['accountant'], provider='sandbox', settlement_id='S1', settled_on=timezone.localdate(),
                                 gross=p.amount, fees=Decimal('236.00'), tax_on_fees=Decimal('42.48'), tds=Decimal('0'),
                                 bank_account=bank, payment_ids=[p.pk])
        self.assertEqual(self.balance(posting.GATEWAY_CLEARING), Decimal('0'))
        self.assertEqual(self.balance(posting.GATEWAY_FEES), Decimal('236.00'))
        self.assertEqual(posting.balance(bank.ledger_account), p.amount - Decimal('278.48'))
        self.assertTrue(self.journal_balanced())


class AccountingPagesTests(CrmTestCase):
    def test_pages_render(self):
        self.make_invoice()
        self.login('accountant')
        bank = BankAccount.objects.filter(kind='bank').first()
        for name, q in [('accounting_dashboard', ''), ('account_list', ''), ('journal_list', ''), ('journal_create', ''),
                        ('general_ledger', f'?account={bank.ledger_account_id}'), ('trial_balance', '?compare=2026-01-01'),
                        ('profit_loss', '?preset=month'), ('balance_sheet', ''), ('cash_flow', ''), ('tax_reports', ''),
                        ('tax_reports', '?export=gstr1_csv'), ('tax_reports', '?export=gstr3b_json'), ('period_list', ''),
                        ('bank_account_list', ''), ('reconciliation', f'?account={bank.pk}&statement_balance=0'),
                        ('account_create', '')]:
            r = self.client.get(reverse(f'crm:{name}') + q)
            self.assertEqual(r.status_code, 200, f'{name}{q}')
        je = JournalEntry.objects.first()
        self.assertEqual(self.client.get(reverse('crm:journal_detail', args=[je.pk])).status_code, 200)

    def test_manual_journal_form_posts(self):
        self.login('accountant')
        from crm.models import Account
        bankacc = Account.objects.get(system_code='BANK_PRIMARY')
        cap = Account.objects.get(system_code=posting.OWNER_CAPITAL)
        data = {'h-entry_date': timezone.localdate().isoformat(), 'h-narration': 'Capital', 'lines-TOTAL_FORMS': '2',
                'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '2', 'lines-MAX_NUM_FORMS': '60',
                'lines-0-account': bankacc.pk, 'lines-0-debit': '1000', 'lines-1-account': cap.pk, 'lines-1-credit': '1000', 'action': 'post'}
        r = self.client.post(reverse('crm:journal_create'), data)
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        self.assertEqual(self.balance(posting.OWNER_CAPITAL), Decimal('1000'))
        data['lines-1-credit'] = '900'
        r = self.client.post(reverse('crm:journal_create'), data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'must be equal')

    def test_sales_cannot_open_accounting(self):
        self.login('sales')
        self.assertEqual(self.client.get(reverse('crm:trial_balance')).status_code, 403)
