"""
Payroll runs (Part 12.4). Draft -> In review -> Pending approval -> Approved and locked -> Paid.

A locked run is never edited: corrections are PayrollAdjustment rows picked up by the next run.
Statutory components (PF, PT, TDS) are configuration rows validated by the CA.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, posting
from crm.core.money import ZERO, q2
from crm.models import (CommissionEntry, Employee, Expense, PayrollAdjustment, PayrollRun, Payslip, SalaryStructure)
from crm.modules.hr.services import lop_days, working_days


class PayrollError(ValidationError):
    pass


EDITABLE = ('draft', 'in_review')


def run_url(run) -> str:
    return reverse('crm:payroll_detail', args=[run.pk])


def structure_for(e: Employee, on: date):
    return SalaryStructure.objects.filter(employee=e, effective_from__lte=on).prefetch_related('lines__component').first()


def create_run(period: date, actor) -> PayrollRun:
    period = india.month_start(period)
    if period > india.month_start(india.today()):
        raise PayrollError('Payroll cannot be created for a future month.')
    if posting.period_status(india.month_end(period)) == 'closed':
        raise PayrollError('The accounting period is closed.')
    try:
        with transaction.atomic():
            run = PayrollRun.objects.create(period=period, prepared_by=actor, created_by=actor)
    except IntegrityError:
        raise PayrollError(f'A payroll run for {period:%b-%Y} already exists.')
    recalculate(run, actor)
    audit.log('payroll.created', run, actor=actor)
    run.refresh_from_db()
    return run


def _employees(run: PayrollRun):
    start, end = run.period, india.month_end(run.period)
    return Employee.objects.filter(joining_date__lte=end).exclude(status='exited', last_working_day__lt=start).select_related('manager')


def calculate_slip(run: PayrollRun, e: Employee, wd: int) -> dict:
    start, end = run.period, india.month_end(run.period)
    s = structure_for(e, end)
    exceptions = []
    if s is None:
        return {'employee': e, 'error': 'No salary structure', 'blocking': True}
    emp_start = max(start, e.joining_date)
    emp_end = min(end, e.last_working_day) if e.last_working_day else end
    employed = working_days(emp_start, emp_end) if emp_end >= emp_start else 0
    lop = min(lop_days(e, emp_start, emp_end), Decimal(employed))
    paid = Decimal(employed) - lop
    factor = (paid / Decimal(wd)) if wd else Decimal('0')
    values = {ln.component.code: ln for ln in s.lines.all()}
    lines, basic = [], ZERO
    basic_line = values.get('basic')
    if basic_line:
        basic = q2(basic_line.value * (factor if basic_line.component.prorate else Decimal('1')))
    gross = ZERO
    deductions = ZERO
    employer = ZERO
    computed = []
    for ln in sorted(s.lines.all(), key=lambda x: (x.component.kind != 'earning', x.component.sort)):
        c = ln.component
        if c.calculation == 'fixed':
            amt = q2(ln.value * (factor if c.prorate else Decimal('1')))
        elif c.calculation == 'pct_basic':
            amt = q2(basic * ln.value / Decimal('100'))
        else:
            computed.append(ln)
            continue
        if c.code == 'basic':
            amt = basic
        lines.append({'code': c.code, 'name': c.name, 'kind': c.kind, 'amount': str(amt)})
        if c.kind == 'earning':
            gross += amt
        elif c.kind == 'deduction':
            deductions += amt
        else:
            employer += amt
    for ln in computed:
        c = ln.component
        amt = q2(gross * ln.value / Decimal('100'))
        lines.append({'code': c.code, 'name': c.name, 'kind': c.kind, 'amount': str(amt)})
        if c.kind == 'deduction':
            deductions += amt
        elif c.kind == 'employer':
            employer += amt
        else:
            gross += amt
    comm_rows = list(CommissionEntry.objects.filter(user_id=e.user_id, status='approved', payroll_run__isnull=True)) if e.user_id else []
    reimb_rows = list(Expense.objects.filter(kind='reimbursement', employee_id=e.user_id, status='approved')) if e.user_id else []
    adj_rows = list(PayrollAdjustment.objects.filter(employee=e, applied_run__isnull=True))
    commissions = sum((x.amount for x in comm_rows), ZERO)
    reimb = sum((x.balance for x in reimb_rows), ZERO)
    adj = sum((x.amount for x in adj_rows), ZERO)
    # The exact rows are stored on the payslip so approval settles only what was paid.
    for code, label, amt, rows in (('commissions', 'Commissions', commissions, comm_rows), ('reimbursements', 'Reimbursements', reimb, reimb_rows),
                                   ('adjustments', 'Adjustments', adj, adj_rows)):
        if rows:
            lines.append({'code': code, 'name': label, 'kind': 'earning', 'amount': str(amt), 'ids': [x.pk for x in rows]})
    total_gross = gross + commissions + reimb + adj
    net = total_gross - deductions
    if net < 0:
        exceptions.append('Negative net pay')
    if e.joining_date >= start:
        exceptions.append('New joiner (pro-rated)')
    if e.last_working_day and e.last_working_day <= end:
        exceptions.append('Exit this month (full and final)')
    if not (e.bank_account_number and e.bank_ifsc):
        exceptions.append('Missing bank details')
    prev = Payslip.objects.filter(employee=e, run__period__lt=start).exclude(run__status='reversed').order_by('-run__period').first()
    if prev and prev.net and abs(net - prev.net) > prev.net * Decimal('0.2'):
        exceptions.append(f'Net changed more than 20% (was {prev.net})')
    return {'employee': e, 'working_days': Decimal(wd), 'paid_days': paid, 'lop_days': lop, 'gross': total_gross, 'deductions': deductions,
            'employer': employer, 'commissions': commissions, 'reimbursements': reimb, 'adjustments': adj, 'net': net, 'lines': lines,
            'exceptions': exceptions, 'blocking': net < 0}


def recalculate(run: PayrollRun, actor) -> dict:
    with transaction.atomic():
        run = PayrollRun.objects.select_for_update().get(pk=run.pk)
        if run.status not in EDITABLE:
            raise PayrollError('This run is locked. Corrections go into the next run as adjustments.')
        before = {'gross': run.gross_total, 'net': run.net_total}
        wd = working_days(run.period, india.month_end(run.period))
        run.payslips.all().delete()
        totals = {'gross': ZERO, 'ded': ZERO, 'emp': ZERO, 'net': ZERO}
        exceptions = []
        for e in _employees(run):
            r = calculate_slip(run, e, wd)
            if r.get('error'):
                exceptions.append({'employee': e.name, 'code': e.code, 'issue': r['error'], 'blocking': True})
                continue
            Payslip.objects.create(run=run, employee=e, working_days=r['working_days'], paid_days=r['paid_days'], lop_days=r['lop_days'],
                                   gross=r['gross'], deductions=r['deductions'], employer_contribution=r['employer'],
                                   commissions=r['commissions'], reimbursements=r['reimbursements'], adjustments=r['adjustments'],
                                   net=r['net'], lines=r['lines'])
            totals['gross'] += r['gross']
            totals['ded'] += r['deductions']
            totals['emp'] += r['employer']
            totals['net'] += r['net']
            for x in r['exceptions']:
                exceptions.append({'employee': e.name, 'code': e.code, 'issue': x, 'blocking': x == 'Negative net pay'})
        run.working_days = wd
        run.gross_total, run.deduction_total, run.employer_total, run.net_total = totals['gross'], totals['ded'], totals['emp'], totals['net']
        run.exceptions = exceptions
        run.last_calculated_at = timezone.now()
        run.save()
        audit.log('payroll.recalculated', run, actor=actor, changes={'net': str(run.net_total), 'previous_net': str(before['net'])})
    return {'before': before, 'after': {'gross': run.gross_total, 'net': run.net_total}}


def blocking(run: PayrollRun) -> list:
    return [x for x in run.exceptions if x.get('blocking')]


def to_review(run: PayrollRun, actor):
    if run.status != 'draft':
        raise PayrollError('Only a draft can move to review.')
    PayrollRun.objects.filter(pk=run.pk).update(status='in_review', reviewed_by=actor)
    audit.log('payroll.in_review', run, actor=actor)


def submit(run: PayrollRun, actor, message: str = ''):
    if run.status not in EDITABLE:
        raise PayrollError('Only a draft or reviewed run can be submitted.')
    if blocking(run):
        raise PayrollError('Fix the blocking exceptions first: ' + '; '.join(f'{x["employee"]}: {x["issue"]}' for x in blocking(run)) + '.')
    if not run.payslips.exists():
        raise PayrollError('There are no payslips in this run.')
    with transaction.atomic():
        PayrollRun.objects.filter(pk=run.pk).update(status='pending_approval')
        approvals.submit('payroll', run, title=f'Approve {run}: net ₹{run.net_total:,.2f}', module='payroll', requested_by=actor,
                         level='owner', amount=run.net_total, reasons=[f'{run.payslips.count()} payslips'],
                         after={'gross': str(run.gross_total), 'net': str(run.net_total)}, message=message)


def _approved(req, user):
    with transaction.atomic():
        run = PayrollRun.objects.select_for_update().get(pk=req.object_id)
        if run.status != 'pending_approval':
            return
        if run.prepared_by_id == user.pk:
            raise approvals.ApprovalError('The preparer cannot approve the payroll run.')
        posting.ensure_period_open(india.month_end(run.period), user)
        total = sum((p.net for p in run.payslips.all()), ZERO)
        if total != run.net_total:
            raise approvals.ApprovalError('Payslips do not add up to the run total. Recalculate the run.')
        run.status, run.approved_by, run.approved_at = 'approved', user, timezone.now()
        run.save(update_fields=['status', 'approved_by', 'approved_at'])
        for slip in run.payslips.all():
            ids = {ln['code']: ln.get('ids', []) for ln in slip.lines if ln.get('ids')}
            if CommissionEntry.objects.filter(pk__in=ids.get('commissions', []), status='approved', payroll_run__isnull=True) \
                    .update(status='paid', payroll_run=run) != len(ids.get('commissions', [])):
                raise approvals.ApprovalError('Commission entries changed since the run was calculated. Recalculate the run.')
            for x in Expense.objects.filter(pk__in=ids.get('reimbursements', []), status='approved'):
                Expense.objects.filter(pk=x.pk).update(paid_amount=x.payable_amount, status='paid')
            if PayrollAdjustment.objects.filter(pk__in=ids.get('adjustments', []), applied_run__isnull=True) \
                    .update(applied_run=run) != len(ids.get('adjustments', [])):
                raise approvals.ApprovalError('Adjustments changed since the run was calculated. Recalculate the run.')
        posting.enqueue('payroll_approved', run, 'approved')
        audit.log('payroll.approved', run, actor=user, changes={'net': str(run.net_total)})


def _rejected(req, user):
    PayrollRun.objects.filter(pk=req.object_id, status='pending_approval').update(status='in_review')


approvals.register('payroll', _approved, _rejected)


def payment_file(run: PayrollRun, actor) -> str:
    if run.status not in ('approved', 'partially_paid'):
        raise PayrollError('Approve the run first.')
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Employee code', 'Name', 'Account number', 'IFSC', 'Amount', 'Narration'])
    for p in run.payslips.filter(status='pending').select_related('employee'):
        e = p.employee
        w.writerow([e.code, e.name, e.bank_account_number, e.bank_ifsc, f'{p.net:.2f}', f'Salary {run.period:%b-%Y}'])
    audit.log('payroll.payment_file', run, actor=actor, changes={'rows': run.payslips.filter(status='pending').count()})
    return buf.getvalue()


def mark_paid(run: PayrollRun, actor, *, slip_ids: list[int], bank_account, paid_on: date, reference: str):
    if run.status not in ('approved', 'partially_paid'):
        raise PayrollError('Approve the run first.')
    if not reference.strip():
        raise PayrollError('Enter the bank batch reference.')
    if paid_on > india.today():
        raise PayrollError('The date cannot be in the future.')
    with transaction.atomic():
        run = PayrollRun.objects.select_for_update().get(pk=run.pk)
        slips = list(run.payslips.select_for_update().filter(pk__in=slip_ids, status='pending'))
        if not slips:
            raise PayrollError('Select unpaid payslips.')
        amount = sum((p.net for p in slips), ZERO)
        Payslip.objects.filter(pk__in=[p.pk for p in slips]).update(status='paid', paid_at=timezone.now(), payment_reference=reference[:80])
        run.status = 'paid' if not run.payslips.filter(status='pending').exists() else 'partially_paid'
        if run.status == 'paid':
            run.paid_at = timezone.now()
        run.save(update_fields=['status', 'paid_at'])
        key = f'paid:{reference.strip()[:40]}:{min(p.pk for p in slips)}'
        posting.enqueue('payroll_paid', run, key, {'bank_account': bank_account.pk, 'amount': str(amount), 'date': paid_on.isoformat(),
                                                   'key': key})
        audit.log('payroll.paid', run, actor=actor, changes={'slips': len(slips), 'amount': str(amount), 'reference': reference})
    return amount


def release_payslips(run: PayrollRun, actor) -> int:
    if run.status != 'paid':
        raise PayrollError('Release payslips after the run is fully paid.')
    PayrollRun.objects.filter(pk=run.pk).update(payslips_released_at=timezone.now())
    users = [p.employee.user for p in run.payslips.select_related('employee__user') if p.employee.user_id]
    notify.notify(users, f'Your payslip for {run.period:%B %Y} is ready', url=reverse('crm:my_payslips'), event_type='payslip',
                  level='success', email=True)
    audit.log('payroll.payslips_released', run, actor=actor, changes={'employees': len(users)})
    return len(users)


def reverse_run(run: PayrollRun, actor, reason: str):
    from crm.core import permissions
    if not permissions.has_perm(actor, 'settings', 'configure'):
        permissions.denied(actor, 'payroll', 'approve', detail='reverse payroll')
    if len(reason.strip()) < 10:
        raise PayrollError('Give a reason of at least 10 characters.')
    if run.status != 'approved':
        raise PayrollError('Only an approved, unpaid run can be reversed. Paid salaries are corrected with adjustments.')
    from crm.models import JournalEntry
    with transaction.atomic():
        je = JournalEntry.objects.filter(source_type='payrollrun', source_id=str(run.pk), event_key='approved', status='posted').first()
        if je is not None:
            posting.reverse_entry(je, actor, f'Payroll reversed: {reason}')
        CommissionEntry.objects.filter(payroll_run=run).update(status='approved', payroll_run=None)
        PayrollAdjustment.objects.filter(applied_run=run).update(applied_run=None)
        for slip in run.payslips.all():
            ids = next((ln.get('ids', []) for ln in slip.lines if ln.get('code') == 'reimbursements'), [])
            Expense.objects.filter(pk__in=ids, status='paid', payments__isnull=True).update(status='approved', paid_amount=ZERO)
        PayrollRun.objects.filter(pk=run.pk).update(status='reversed', reversal_reason=reason[:300])
        audit.log('payroll.reversed', run, actor=actor, reason=reason)


def add_adjustment(employee: Employee, actor, *, amount: Decimal, reason: str) -> PayrollAdjustment:
    if not amount:
        raise PayrollError('Enter a non-zero amount.')
    if len(reason.strip()) < 10:
        raise PayrollError('Give a reason of at least 10 characters.')
    a = PayrollAdjustment.objects.create(employee=employee, amount=q2(amount), reason=reason[:300], created_by=actor)
    audit.log('payroll.adjustment_added', employee, actor=actor, changes={'amount': str(amount)}, reason=reason)
    return a


def loaded_cost_rate(e: Employee) -> Decimal:
    """Monthly gross plus employer contributions over standard hours (176) for delivery costing."""
    s = structure_for(e, india.today())
    if s is None:
        return ZERO
    gross = sum((ln.value for ln in s.lines.all() if ln.component.kind == 'earning' and ln.component.calculation == 'fixed'), ZERO)
    basic = next((ln.value for ln in s.lines.all() if ln.component.code == 'basic'), ZERO)
    employer = sum((q2(basic * ln.value / 100) for ln in s.lines.all() if ln.component.kind == 'employer' and ln.component.calculation == 'pct_basic'), ZERO)
    return q2((gross + employer) / Decimal('176'))
