"""Part 9.8, 10.6 and 11.6 acceptance tests: contracts, projects, timesheets, expenses."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, india, posting
from crm.models import (BankAccount, BillingSchedule, Contract, ContractLine, Expense, ExpenseCategory, Invoice, Project,
                        RecurringExpense, Task, Vendor)
from crm.modules.contracts import services as cs
from crm.modules.expenses import services as es
from crm.modules.projects import services as ps

from .base import CrmTestCase


class ContractTests(CrmTestCase):
    def _contract(self, model='monthly', price=Decimal('12000'), months=12, auto_issue=False):
        today = india.today()
        c = Contract.objects.create(client=self.client_obj, owner=self.u['manager'], title='SEO retainer', start_date=today,
                                    end_date=india.add_months(today, months) - timedelta(days=1),
                                    invoice_policy='auto_issue' if auto_issue else 'draft')
        ContractLine.objects.create(contract=c, service=self.service, description='SEO', unit_price=price, billing_model=model,
                                    start_date=today, sac_code='998365')
        return cs.activate(c, self.u['manager'])

    def test_annual_line_counts_one_twelfth_in_mrr(self):
        self._contract(model='annual', price=Decimal('120000'))
        self.assertEqual(cs.mrr(), Decimal('10000.00'))

    def test_schedule_is_idempotent(self):
        c = self._contract()
        n = BillingSchedule.objects.filter(contract_line__contract=c).count()
        self.assertEqual(n, 12)
        self.assertEqual(cs.generate_schedule(c), 0)

    def test_recurring_invoice_generated_once(self):
        c = self._contract()
        self.assertEqual(cs.generate_recurring_invoices(), 1)
        self.assertEqual(cs.generate_recurring_invoices(), 0)
        inv = Invoice.objects.get(contract=c)
        self.assertEqual(inv.taxable_total, Decimal('12000.00'))
        self.assertEqual(inv.status, 'draft')

    def test_amend_keeps_old_version(self):
        c = self._contract()
        new = cs.amend(c, self.u['manager'], effective_from=india.today() + timedelta(days=40), reason='Price increase from next cycle')
        ContractLine.objects.filter(contract=new).update(unit_price=Decimal('15000'))
        cs.activate(new, self.u['manager'])
        c.refresh_from_db()
        self.assertEqual(c.status, 'superseded')
        self.assertEqual(c.lines.first().unit_price, Decimal('12000.00'))
        self.assertTrue(BillingSchedule.objects.filter(contract_line__contract=c, status='skipped').exists())
        self.assertEqual(new.number, c.number)

    def test_renew_creates_next_term(self):
        c = self._contract(months=1)
        new = cs.renew(c, self.u['manager'], new_end=india.add_months(c.end_date, 12), price_change_percent=Decimal('10'))
        self.assertEqual(new.start_date, c.end_date + timedelta(days=1))
        self.assertEqual(new.lines.first().unit_price, Decimal('13200.00'))


class ProjectTests(CrmTestCase):
    def _project(self):
        p = ps.create_project(self.u['pm'], client=self.client_obj, name='Store build', manager=self.u['pm'], start_date=india.today(),
                              due_date=india.today() + timedelta(days=30))
        ps.add_member(p, self.u['pm'], user=self.u['dev'], allocation_percent=50)
        return p

    def test_close_blocked_by_unaccepted_milestone(self):
        p = self._project()
        from crm.models import Milestone
        Milestone.objects.create(project=p, name='Design', due_date=india.today())
        Project.objects.filter(pk=p.pk).update(status='active')
        p.refresh_from_db()
        with self.assertRaisesMessage(ps.ProjectError, 'Design'):
            ps.request_close(p, self.u['pm'])

    def test_25_hours_rejected(self):
        p = self._project()
        ps.log_time(self.u['dev'], project=p, entry_date=india.today(), hours=Decimal('20'))
        with self.assertRaisesMessage(ps.ProjectError, '24 hours'):
            ps.log_time(self.u['dev'], project=p, entry_date=india.today(), hours=Decimal('5'))

    def test_dependency_cycle_blocked(self):
        p = self._project()
        a = Task.objects.create(project=p, title='A')
        b = Task.objects.create(project=p, title='B')
        c = Task.objects.create(project=p, title='C')
        b.depends_on.add(a)
        c.depends_on.add(b)
        with self.assertRaises(ps.ProjectError):
            ps.save_task(a, self.u['pm'], deps=[c], is_new=False)

    def test_blocked_needs_reason_and_creates_blocker(self):
        p = self._project()
        t = Task.objects.create(project=p, title='Build', assignee=self.u['dev'])
        with self.assertRaises(ps.ProjectError):
            ps.set_task_status(t, self.u['dev'], 'blocked')
        ps.set_task_status(t, self.u['dev'], 'blocked', reason='Waiting for logo files')
        self.assertTrue(t.blockers.filter(status='open').exists())

    def test_scope_change_leaves_contract_value(self):
        p = self._project()
        from crm.models import ChangeRequest
        cr = ChangeRequest.objects.create(project=p, title='Extra page', description='Careers page', price=Decimal('5000'))
        ps.submit_change(cr, self.u['pm'])
        approvals.approve(approvals.pending_for(cr), self.u['owner'])
        inv = ps.bill_change(ChangeRequest.objects.get(pk=cr.pk), self.u['finance'])
        self.assertEqual(inv.taxable_total, Decimal('5000.00'))
        p.refresh_from_db()
        self.assertEqual(p.contract_value, Decimal('0'))

    def test_developer_cannot_open_other_project(self):
        p = ps.create_project(self.u['owner'], client=self.client_obj, name='Secret', manager=self.u['owner'], start_date=india.today(),
                              due_date=india.today() + timedelta(days=5))
        self.login('dev')
        r = self.client.get(reverse('crm:project_detail', args=[p.pk]))
        self.assertEqual(r.status_code, 404)

    def test_pages_render(self):
        p = self._project()
        t = Task.objects.create(project=p, title='Build', assignee=self.u['dev'])
        self.login('pm')
        for url in [reverse('crm:project_list'), reverse('crm:project_detail', args=[p.pk]), reverse('crm:project_create'),
                    reverse('crm:task_list'), reverse('crm:task_list') + '?view=board', reverse('crm:task_detail', args=[t.pk]),
                    reverse('crm:timesheet'), reverse('crm:timesheet_approvals'), reverse('crm:blockers'), reverse('crm:ticket_list'),
                    reverse('crm:ticket_create'), reverse('crm:task_create') + f'?project={p.pk}']:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for tab in ('tasks', 'milestones', 'team', 'time', 'changes', 'activity'):
            self.assertEqual(self.client.get(reverse('crm:project_detail', args=[p.pk]) + f'?tab={tab}').status_code, 200, tab)
        r = self.client.post(reverse('crm:task_status', args=[t.pk]), {'to': 'in_progress'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(r.json()['ok'])


class ExpenseTests(CrmTestCase):
    def _expense(self, amount=Decimal('30000'), **kw):
        cat = ExpenseCategory.objects.get(name='Office and miscellaneous')
        e = Expense(kind='expense', category=cat, description='Laptop stands', bill_date=india.today(), amount_before_tax=amount,
                    cgst=Decimal('0'), sgst=Decimal('0'), itc_eligibility='na', created_by=self.u['sales'], **kw)
        es.recompute(e)
        e.save()
        return e

    def test_allocation_keeps_total(self):
        e = self._expense()
        p1, p2, p3 = (Project.objects.create(code=f'PRJ-T{i}', name=f'P{i}', client=self.client_obj, manager=self.u['pm'],
                                             start_date=india.today(), due_date=india.today()) for i in range(3))
        es.save_allocations(e, [{'project': p1, 'percent': '33.33'}, {'project': p2, 'percent': '33.33'}, {'project': p3, 'percent': '33.34'}],
                            self.u['finance'])
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(sum(a.amount for a in e.allocations.all()), Decimal('30000.00'))

    def test_duplicate_vendor_invoice_detected(self):
        v = Vendor.objects.create(name='Acme Supplies')
        self._expense(vendor=v, bill_number='INV-9')
        e2 = Expense(kind='vendor_bill', vendor=v, bill_number='INV-9', category=ExpenseCategory.objects.first(), description='x',
                     bill_date=india.today(), amount_before_tax=Decimal('10'))
        self.assertTrue(es.duplicates(e2))

    def test_submit_without_proof_blocked_and_self_approval_blocked(self):
        from crm.models import CompanySettings
        CompanySettings.objects.update(expense_proof_above=Decimal('2000'))
        e = self._expense()
        with self.assertRaisesMessage(es.ExpenseError, 'Attach the bill'):
            es.submit(e, self.u['sales'])
        from crm.modules.common.views import save_attachment
        save_attachment(e, SimpleUploadedFile('bill.pdf', b'%PDF-1.4 test', content_type='application/pdf'), self.u['sales'])
        es.submit(e, self.u['sales'])
        req = approvals.pending_for(e, 'expense')
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(req, self.u['sales'])

    def test_recurring_twice_one_bill(self):
        r = RecurringExpense.objects.create(name='Office rent', category=ExpenseCategory.objects.get(name='Rent'),
                                            amount_before_tax=Decimal('25000'), tax_rate=Decimal('18'), next_run=india.today())
        self.assertEqual(es.run_recurring(r), 1)
        r.refresh_from_db()
        r.next_run = india.today()
        r.save()
        self.assertEqual(es.run_recurring(r), 0)
        self.assertEqual(Expense.objects.filter(recurring=r).count(), 1)

    def test_vendor_payment_with_tds_balances(self):
        cat = ExpenseCategory.objects.get(name='Professional fees')
        v = Vendor.objects.create(name='CA Firm', tds_section='194J', tds_rate=Decimal('10'))
        e = Expense(kind='vendor_bill', vendor=v, category=cat, description='Audit fees', bill_date=india.today(), bill_number='A1',
                    amount_before_tax=Decimal('50000'), cgst=Decimal('4500'), sgst=Decimal('4500'), vendor_gstin='', itc_eligibility='na',
                    created_by=self.u['finance'])
        es.recompute(e)
        self.assertEqual(e.tds_amount, Decimal('5000.00'))
        e.save()
        from crm.models import CompanySettings
        CompanySettings.objects.update(expense_proof_above=Decimal('10000000'))
        es.submit(e, self.u['finance'])
        with self.captureOnCommitCallbacks(execute=True):
            approvals.approve(approvals.pending_for(e, 'expense'), self.u['owner'])
        e.refresh_from_db()
        self.assertEqual(e.status, 'approved')
        bank = BankAccount.objects.filter(kind='bank').first()
        with self.captureOnCommitCallbacks(execute=True):
            es.pay(e, self.u['finance'], amount=e.payable_amount, payment_date=india.today(), bank_account=bank, reference='NEFT1')
        self.assertEqual(self.balance(posting.AP), Decimal('0'))
        self.assertEqual(self.balance(posting.TDS_PAYABLE), Decimal('5000.00'))
        self.assertEqual(posting.balance(bank.ledger_account), -Decimal('54000.00'))
        self.assertTrue(self.journal_balanced())

    def test_every_expense_needs_the_owner(self):
        e = self._expense(amount=Decimal('500'))  # small, below the old manager limit and the proof threshold
        es.submit(e, self.u['finance'])
        req = approvals.pending_for(e, 'expense')
        self.assertEqual(req.required_level, 'owner')
        self.assertFalse(approvals.can_decide(self.u['accountant'], req)[0])
        self.assertTrue(approvals.can_decide(self.u['owner'], req)[0])

    def test_pages_render(self):
        e = self._expense()
        self.login('finance')
        for url in [reverse('crm:expense_list'), reverse('crm:expense_create'), reverse('crm:expense_detail', args=[e.pk]),
                    reverse('crm:expense_approvals'), reverse('crm:vendor_list'), reverse('crm:vendor_create'),
                    reverse('crm:recurring_expense_list'), reverse('crm:tds_payable'), reverse('crm:contract_list'),
                    reverse('crm:renewals'), reverse('crm:billing_schedule')]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
