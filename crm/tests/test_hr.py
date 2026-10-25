"""Part 12.6 acceptance tests: salary privacy, leave overlap, locked payroll, offboarding, clawback, payroll totals."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, india, posting
from crm.models import (CommissionEntry, CommissionRule, Employee, Lead, LeaveType, PayrollAdjustment, PayrollRun, Role,
                        SalaryComponent, SalaryStructure, SalaryStructureLine, Task)
from crm.modules.hr import services as hs
from crm.modules.payroll import runs

from .base import CrmTestCase


class HRBase(CrmTestCase):
    def emp(self, key, code, manager=None, basic=Decimal('30000'), hra_pct=Decimal('40')):
        e = Employee.objects.create(user=self.u[key], code=code, name=self.u[key].first_name, work_email=f'{key}@co.test',
                                    joining_date=date(2025, 1, 1), status='active', manager=manager, bank_account_number='1234567890',
                                    bank_ifsc='HDFC0000001')
        s = SalaryStructure.objects.create(employee=e, effective_from=date(2025, 1, 1))
        comps = {c.code: c for c in SalaryComponent.objects.all()}
        SalaryStructureLine.objects.create(structure=s, component=comps['basic'], value=basic)
        SalaryStructureLine.objects.create(structure=s, component=comps['hra'], value=hra_pct)
        SalaryStructureLine.objects.create(structure=s, component=comps['pf_employee'], value=Decimal('12'))
        SalaryStructureLine.objects.create(structure=s, component=comps['pf_employer'], value=Decimal('12'))
        SalaryStructureLine.objects.create(structure=s, component=comps['pt'], value=Decimal('200'))
        return e


class LeaveTests(HRBase):
    def test_overlap_blocked(self):
        mgr = self.emp('manager', 'E001')
        e = self.emp('sales', 'E002', manager=mgr)
        cl = LeaveType.objects.get(code__iexact='CL')
        start = india.today() + timedelta(days=10)
        r = hs.apply_leave(e, self.u['sales'], leave_type=cl, start=start, end=start + timedelta(days=1), reason='Family function')
        hs.decide_leave(r, self.u['manager'], approve=True)
        with self.assertRaisesMessage(hs.HRError, 'overlap'):
            hs.apply_leave(e, self.u['sales'], leave_type=cl, start=start + timedelta(days=1), end=start + timedelta(days=2), reason='Again')

    def test_applicant_cannot_approve_own(self):
        e = self.emp('hr', 'E010')
        cl = LeaveType.objects.get(code__iexact='CL')
        start = india.today() + timedelta(days=5)
        r = hs.apply_leave(e, self.u['hr'], leave_type=cl, start=start, end=start, reason='Errand')
        with self.assertRaises(hs.HRError):
            hs.decide_leave(r, self.u['hr'], approve=True)

    def test_balance_checked(self):
        e = self.emp('sales', 'E003')
        cl = LeaveType.objects.get(code__iexact='CL')
        start = date(india.today().year, 12, 1)
        with self.assertRaisesMessage(hs.HRError, 'Not enough'):
            hs.apply_leave(e, self.u['sales'], leave_type=cl, start=start, end=start + timedelta(days=20), reason='Long trip')


class PayrollTests(HRBase):
    def setUp(self):
        super().setUp()
        self.e1 = self.emp('sales', 'E101')
        self.e2 = self.emp('dev', 'E102', basic=Decimal('25000'))
        self.period = india.add_months(india.month_start(india.today()), -1)

    def test_totals_reconcile_and_lock(self):
        run = runs.create_run(self.period, self.u['hr'])
        self.assertEqual(run.payslips.count(), 2)
        self.assertEqual(sum(p.net for p in run.payslips.all()), run.net_total)
        p = run.payslips.get(employee=self.e1)
        # basic 30000, HRA 40% = 12000, PF 12% of basic = 3600, PT 200
        self.assertEqual(p.gross, Decimal('42000.00'))
        self.assertEqual(p.deductions, Decimal('3800.00'))
        self.assertEqual(p.net, Decimal('38200.00'))
        runs.submit(run, self.u['hr'])
        with self.assertRaises(approvals.ApprovalError):
            approvals.approve(approvals.pending_for(run), self.u['hr'])
        with self.captureOnCommitCallbacks(execute=True):
            approvals.approve(approvals.pending_for(run), self.u['owner'])
        run.refresh_from_db()
        self.assertEqual(run.status, 'approved')
        with self.assertRaises(runs.PayrollError):
            runs.recalculate(run, self.u['hr'])
        self.assertEqual(self.balance(posting.SALARIES_PAYABLE), run.net_total)
        self.assertTrue(self.journal_balanced())
        from crm.models import BankAccount
        bank = BankAccount.objects.filter(kind='bank').first()
        with self.captureOnCommitCallbacks(execute=True):
            runs.mark_paid(run, self.u['hr'], slip_ids=list(run.payslips.values_list('pk', flat=True)), bank_account=bank,
                           paid_on=india.today(), reference='SAL-BATCH-1')
        run.refresh_from_db()
        self.assertEqual(run.status, 'paid')
        self.assertEqual(self.balance(posting.SALARIES_PAYABLE), Decimal('0'))

    def test_correction_after_lock_goes_to_next_run(self):
        run = runs.create_run(self.period, self.u['hr'])
        runs.submit(run, self.u['hr'])
        approvals.approve(approvals.pending_for(run), self.u['owner'])
        adj = runs.add_adjustment(self.e1, self.u['hr'], amount=Decimal('1500'), reason='Missed overtime for last month')
        nxt = runs.create_run(india.month_start(india.today()), self.u['hr'])
        p = nxt.payslips.get(employee=self.e1)
        self.assertEqual(p.adjustments, Decimal('1500.00'))
        adj.refresh_from_db()
        self.assertIsNone(adj.applied_run)

    def test_salary_tab_hidden_from_executive(self):
        self.login('sales_b')
        r = self.client.get(reverse('crm:employee_detail', args=[self.e1.pk]) + '?tab=payroll')
        self.assertIn(r.status_code, (200, 403))
        self.assertNotContains(r, 'Salary structures', status_code=r.status_code)
        r = self.client.get(reverse('crm:payroll_list'))
        self.assertEqual(r.status_code, 403)

    def test_pages_render(self):
        run = runs.create_run(self.period, self.u['hr'])
        self.login('hr')
        for url in [reverse('crm:employee_list'), reverse('crm:employee_create'), reverse('crm:employee_detail', args=[self.e1.pk]),
                    reverse('crm:attendance'), reverse('crm:leave_list'), reverse('crm:hr_approvals'), reverse('crm:holiday_list'),
                    reverse('crm:payroll_list'), reverse('crm:payroll_detail', args=[run.pk]), reverse('crm:salary_structures'),
                    reverse('crm:salary_structures') + f'?employee={self.e1.pk}', reverse('crm:my_payslips')]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for tab in ('attendance', 'leave', 'documents', 'payroll', 'activity'):
            self.assertEqual(self.client.get(reverse('crm:employee_detail', args=[self.e1.pk]) + f'?tab={tab}').status_code, 200, tab)


class OffboardingAndCommissionTests(HRBase):
    def test_offboarding_reassigns(self):
        mgr = self.emp('manager', 'E201')
        e = self.emp('sales', 'E202', manager=mgr)
        from crm.models import Project
        p = Project.objects.create(code='PRJ-X', name='X', client=self.client_obj, manager=self.u['pm'], start_date=india.today(),
                                   due_date=india.today())
        t = Task.objects.create(project=p, title='Open task', assignee=self.u['sales'])
        hs.initiate_offboarding(e, self.u['hr'], last_day=india.today(), reason='Resigned')
        s = hs.complete_exit(Employee.objects.get(pk=e.pk), self.u['hr'])
        t.refresh_from_db()
        self.assertEqual(t.assignee_id, self.u['manager'].pk)
        self.u['sales'].refresh_from_db()
        self.assertFalse(self.u['sales'].is_active)
        self.assertEqual(s['tasks'], 1)

    def test_clawback_on_credit_note(self):
        from crm.modules.billing import services as bs
        CommissionRule.objects.create(name='Invoiced 5%', basis='invoiced', rate_percent=Decimal('5'), effective_from=date(2025, 1, 1))
        inv = self.make_invoice()
        entry = CommissionEntry.objects.get(invoice=inv, amount__gt=0)
        self.assertEqual(entry.amount, Decimal('500.00'))
        with self.captureOnCommitCallbacks(execute=True):
            cn = bs.create_credit_note(inv, self.u['accountant'], taxable=Decimal('10000'), reason='cancellation')
            if cn.status != 'issued':
                approvals.approve(approvals.pending_for(cn), self.u['owner'])
        self.assertTrue(CommissionEntry.objects.filter(invoice=inv, amount=Decimal('-500.00')).exists())
