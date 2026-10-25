"""
Default posting map (Part 8.3). Each handler turns an outbox row into a journal
spec. Account mapping lives in the chart of accounts (system codes), so the
accountant can rename accounts freely.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .money import ZERO, D
from .posting import (AP, AR, BAD_DEBTS, CUSTOMER_ADVANCES, DEFERRED_REVENUE, EMPLOYER_CONTRIB, GATEWAY_CLEARING, GATEWAY_FEES,
                      GST_ON_FEES, INPUT_CGST, INPUT_IGST, INPUT_SGST, OUTPUT_CGST, OUTPUT_IGST, OUTPUT_SGST,
                      RECURRING_REVENUE, REIMBURSEMENTS_PAYABLE, ROUND_OFF, SALARIES_PAYABLE, SALARY_EXPENSE,
                      SERVICE_REVENUE, STATUTORY_PAYABLE, TDS_PAYABLE, TDS_RECEIVABLE, COMMISSION_EXPENSE, Line,
                      account)


def _deposit_account(payment):
    if payment.deposit_account_id:
        return payment.deposit_account.ledger_account
    return account(GATEWAY_CLEARING)


def _defer(inv, ln) -> bool:
    """Over-time services billed for a period longer than a month post to deferred revenue (Part 8.6)."""
    from crm.core.india import add_months, month_start
    from crm.models import DeferredRevenueSchedule
    sched = inv.billing_schedule
    if ln.service is None or ln.service.revenue_recognition != 'over_time' or sched is None:
        return False
    months = []
    m = month_start(sched.period_start)
    while m <= sched.period_end:
        months.append(m)
        m = add_months(m, 1)
    if len(months) < 2:
        return False
    each = (ln.taxable_value / len(months)).quantize(Decimal('0.01'))
    for i, m in enumerate(months):
        amt = ln.taxable_value - each * (len(months) - 1) if i == len(months) - 1 else each
        DeferredRevenueSchedule.objects.get_or_create(invoice_line=ln, period_start=m, defaults={'amount': amt})
    return True


def deferred_release(row):
    from crm.models import DeferredRevenueSchedule
    d = DeferredRevenueSchedule.objects.select_related('invoice_line__invoice').get(pk=row.source_id)
    inv = d.invoice_line.invoice
    return {'entry_date': max(d.period_start, inv.invoice_date), 'narration': f'Revenue release {inv.number} {d.period_start:%b-%Y}',
            'event_key': 'released',
            'lines': [Line(DEFERRED_REVENUE, debit=d.amount, client_id=inv.client_id, project_id=inv.project_id),
                      Line(RECURRING_REVENUE, credit=d.amount, client_id=inv.client_id, service_id=d.invoice_line.service_id,
                           project_id=inv.project_id)]}


def invoice_issued(row):
    from crm.models import Invoice
    from .catalog_rules import revenue_account_for_line
    inv = Invoice.objects.get(pk=row.source_id)
    lines = [Line(AR, debit=inv.grand_total, description=f'Invoice {inv.number}', client_id=inv.client_id,
                  project_id=inv.project_id)]
    revenue = defaultdict(lambda: ZERO)
    for ln in inv.lines.all():
        if _defer(inv, ln):
            revenue[(account(DEFERRED_REVENUE), ln.service_id)] += ln.taxable_value
        else:
            revenue[(revenue_account_for_line(ln), ln.service_id)] += ln.taxable_value
    for (acc, service_id), amount in revenue.items():
        lines.append(Line(acc, credit=amount, description=f'Revenue {inv.number}', client_id=inv.client_id,
                          service_id=service_id, project_id=inv.project_id))
    for code, amount in ((OUTPUT_CGST, inv.cgst_total), (OUTPUT_SGST, inv.sgst_total), (OUTPUT_IGST, inv.igst_total)):
        if amount:
            lines.append(Line(code, credit=amount, description=f'Output GST {inv.number}', client_id=inv.client_id))
    if inv.round_off:
        lines.append(Line(ROUND_OFF, credit=inv.round_off, description='Round-off'))
    return {'entry_date': inv.invoice_date, 'narration': f'Tax invoice {inv.number} to {inv.client}',
            'event_key': 'issued', 'lines': lines, 'user': inv.issued_by}


def invoice_cancelled(row):
    from crm.models import Invoice, JournalEntry
    from django.utils import timezone
    inv = Invoice.objects.get(pk=row.source_id)
    original = JournalEntry.objects.filter(source_type='invoice', source_id=str(inv.pk), event_key='issued').first()
    if original is None:
        return None
    lines = [Line(l.account, debit=l.credit, credit=l.debit, description=f'Cancellation {inv.number}',
                  client_id=l.client_id, service_id=l.service_id, project_id=l.project_id) for l in original.lines.all()]
    return {'entry_date': timezone.localdate(), 'narration': f'Cancellation of invoice {inv.number}: {inv.cancel_reason}',
            'event_key': 'cancelled', 'lines': lines}


def payment_received(row):
    """Gateway or verified manual payment: Dr bank/clearing (+TDS receivable), Cr AR (allocated) + Cr advances."""
    from crm.models import Payment
    p = Payment.objects.get(pk=row.source_id)
    allocated = D(row.payload.get('allocated', '0'))
    advance = p.gross_settled - allocated
    lines = [Line(_deposit_account(p), debit=p.amount, description=f'Receipt {p.receipt_number}', client_id=p.client_id)]
    if p.tds_amount:
        lines.append(Line(TDS_RECEIVABLE, debit=p.tds_amount, description=f'TDS {p.tds_section}', client_id=p.client_id))
    if allocated:
        lines.append(Line(AR, credit=allocated, description=f'Receipt {p.receipt_number}', client_id=p.client_id))
    if advance:
        lines.append(Line(CUSTOMER_ADVANCES, credit=advance, description=f'Advance {p.receipt_number}', client_id=p.client_id))
    return {'entry_date': p.payment_date, 'narration': f'Payment {p.receipt_number} from {p.client}',
            'event_key': 'received', 'lines': lines, 'user': p.verified_by}


def advance_allocated(row):
    from crm.models import PaymentAllocation
    a = PaymentAllocation.objects.select_related('payment', 'invoice').get(pk=row.source_id)
    return {'entry_date': a.created_at.date(), 'narration': f'Advance {a.payment} adjusted to {a.invoice}',
            'event_key': 'allocated',
            'lines': [Line(CUSTOMER_ADVANCES, debit=a.total, client_id=a.invoice.client_id),
                      Line(AR, credit=a.total, client_id=a.invoice.client_id)]}


def allocation_reversed(row):
    from crm.models import PaymentAllocation
    from django.utils import timezone
    a = PaymentAllocation.objects.select_related('payment', 'invoice').get(pk=row.source_id)
    return {'entry_date': timezone.localdate(), 'narration': f'Allocation reversal {a.payment} / {a.invoice}',
            'event_key': 'reversed',
            'lines': [Line(AR, debit=a.total, client_id=a.invoice.client_id),
                      Line(CUSTOMER_ADVANCES, credit=a.total, client_id=a.invoice.client_id)]}


def credit_note_issued(row):
    from crm.models import CreditNote
    cn = CreditNote.objects.select_related('invoice').get(pk=row.source_id)
    cid = cn.invoice.client_id
    lines = [Line(SERVICE_REVENUE, debit=cn.taxable_value, description=f'Credit note {cn.number}', client_id=cid)]
    for code, amount in ((OUTPUT_CGST, cn.cgst), (OUTPUT_SGST, cn.sgst), (OUTPUT_IGST, cn.igst)):
        if amount:
            lines.append(Line(code, debit=amount, client_id=cid))
    lines.append(Line(AR, credit=cn.total, description=f'Credit note {cn.number}', client_id=cid))
    return {'entry_date': cn.note_date, 'narration': f'Credit note {cn.number} against {cn.invoice.number}',
            'event_key': 'issued', 'lines': lines, 'user': cn.issued_by}


def refund_processed(row):
    from crm.models import Refund
    r = Refund.objects.select_related('payment').get(pk=row.source_id)
    debit_code = row.payload.get('debit', CUSTOMER_ADVANCES)
    cid = r.payment.client_id
    return {'entry_date': (r.processed_at.date() if r.processed_at else r.created_at.date()),
            'narration': f'Refund of {r.payment}', 'event_key': 'processed',
            'lines': [Line(debit_code, debit=r.amount, client_id=cid),
                      Line(_deposit_account(r.payment), credit=r.amount, client_id=cid)]}


def settlement_recorded(row):
    from crm.models import Settlement
    s = Settlement.objects.select_related('bank_account').get(pk=row.source_id)
    lines = [Line(s.bank_account.ledger_account, debit=s.net, description=f'Settlement {s.settlement_id}')]
    if s.fees:
        lines.append(Line(GATEWAY_FEES, debit=s.fees))
    if s.tax_on_fees:
        lines.append(Line(GST_ON_FEES, debit=s.tax_on_fees))
    if s.tds:
        lines.append(Line(TDS_RECEIVABLE, debit=s.tds))
    lines.append(Line(GATEWAY_CLEARING, credit=s.gross))
    return {'entry_date': s.settled_on, 'narration': f'Gateway settlement {s.settlement_id}', 'event_key': 'recorded',
            'lines': lines}


def invoice_written_off(row):
    from crm.models import Invoice
    inv = Invoice.objects.get(pk=row.source_id)
    amount = D(row.payload['amount'])
    return {'entry_date': inv.written_off_at.date(), 'narration': f'Bad-debt write-off {inv.number}',
            'event_key': 'written_off',
            'lines': [Line(BAD_DEBTS, debit=amount, client_id=inv.client_id),
                      Line(AR, credit=amount, client_id=inv.client_id)]}


def tds_written_off(row):
    from crm.models import TdsReceivable
    from django.utils import timezone
    t = TdsReceivable.objects.get(pk=row.source_id)
    return {'entry_date': timezone.localdate(), 'narration': f'Unclaimable TDS written off ({t.client}, {t.fiscal_year} {t.quarter})',
            'event_key': 'written_off',
            'lines': [Line(BAD_DEBTS, debit=t.amount, client_id=t.client_id), Line(TDS_RECEIVABLE, credit=t.amount, client_id=t.client_id)]}


def expense_approved(row):
    from crm.models import Expense
    e = Expense.objects.select_related('category').get(pk=row.source_id)
    itc = e.itc_eligibility == 'eligible'
    expense_amount = e.amount_before_tax + (ZERO if itc else e.tax_total)
    allocations = list(e.allocations.all())
    lines = []
    if allocations:
        remaining = expense_amount
        for i, a in enumerate(allocations):
            amt = remaining if i == len(allocations) - 1 else (expense_amount * a.percent / Decimal('100')).quantize(Decimal('0.01'))
            remaining -= amt
            lines.append(Line(e.category.account, debit=amt, description=e.description[:300], project_id=a.project_id,
                              client_id=a.client_id, department_id=a.department_id))
    else:
        lines.append(Line(e.category.account, debit=expense_amount, description=e.description[:300], project_id=e.project_id))
    if itc:
        for code, amount in ((INPUT_CGST, e.cgst), (INPUT_SGST, e.sgst), (INPUT_IGST, e.igst)):
            if amount:
                lines.append(Line(code, debit=amount))
    payable_code = REIMBURSEMENTS_PAYABLE if e.kind == 'reimbursement' else AP
    lines.append(Line(payable_code, credit=e.payable_amount, description=f'{e.number} {e.vendor or e.employee or ""}'))
    if e.tds_amount:
        lines.append(Line(TDS_PAYABLE, credit=e.tds_amount, description=f'TDS {e.tds_section}'))
    return {'entry_date': e.bill_date, 'narration': f'{e.get_kind_display()} {e.number}: {e.description}',
            'event_key': 'approved', 'lines': lines, 'user': e.approved_by}


def expense_paid(row):
    from crm.models import ExpensePayment
    p = ExpensePayment.objects.select_related('expense', 'bank_account').get(pk=row.source_id)
    payable_code = REIMBURSEMENTS_PAYABLE if p.expense.kind == 'reimbursement' else AP
    return {'entry_date': p.payment_date, 'narration': f'Payment for {p.expense.number}', 'event_key': 'paid',
            'lines': [Line(payable_code, debit=p.amount), Line(p.bank_account.ledger_account, credit=p.amount)],
            'user': p.created_by}


def expense_reversed(row):
    from crm.models import Expense, JournalEntry
    from django.utils import timezone
    e = Expense.objects.get(pk=row.source_id)
    original = JournalEntry.objects.filter(source_type='expense', source_id=str(e.pk), event_key='approved').first()
    if original is None:
        return None
    return {'entry_date': timezone.localdate(), 'narration': f'Reversal of {e.number}', 'event_key': 'reversed',
            'lines': [Line(l.account, debit=l.credit, credit=l.debit, project_id=l.project_id, client_id=l.client_id,
                           department_id=l.department_id) for l in original.lines.all()]}


def tds_challan(row):
    from crm.models import TdsChallan
    c = TdsChallan.objects.select_related('bank_account').get(pk=row.source_id)
    return {'entry_date': c.paid_on, 'narration': f'TDS remittance {c.section} CIN {c.cin}', 'event_key': 'paid',
            'lines': [Line(TDS_PAYABLE, debit=c.amount), Line(c.bank_account.ledger_account, credit=c.amount)]}


def payroll_approved(row):
    from crm.models import PayrollRun
    run = PayrollRun.objects.get(pk=row.source_id)
    commissions = sum((p.commissions for p in run.payslips.all()), ZERO)
    reimbursements = sum((p.reimbursements for p in run.payslips.all()), ZERO)
    adjustments = sum((p.adjustments for p in run.payslips.all()), ZERO)
    salary = run.gross_total - commissions - reimbursements - adjustments
    lines = [Line(SALARY_EXPENSE, debit=salary + adjustments, description='Salaries')]
    if commissions:
        lines.append(Line(COMMISSION_EXPENSE, debit=commissions))
    if reimbursements:
        lines.append(Line(REIMBURSEMENTS_PAYABLE, debit=reimbursements))
    if run.employer_total:
        lines.append(Line(EMPLOYER_CONTRIB, debit=run.employer_total))
    lines.append(Line(SALARIES_PAYABLE, credit=run.net_total))
    statutory = run.deduction_total + run.employer_total
    if statutory:
        lines.append(Line(STATUTORY_PAYABLE, credit=statutory))
    from crm.core.india import month_end
    return {'entry_date': month_end(run.period), 'narration': f'Payroll {run.period:%b-%Y}', 'event_key': 'approved',
            'lines': lines, 'user': run.approved_by}


def payroll_paid(row):
    from crm.models import BankAccount, PayrollRun
    run = PayrollRun.objects.get(pk=row.source_id)
    bank = BankAccount.objects.get(pk=row.payload['bank_account'])
    amount = D(row.payload['amount'])
    from datetime import date
    return {'entry_date': date.fromisoformat(row.payload['date']), 'narration': f'Salary payment {run.period:%b-%Y}',
            'event_key': row.payload.get('key', 'paid'),
            'lines': [Line(SALARIES_PAYABLE, debit=amount), Line(bank.ledger_account, credit=amount)]}


def bank_line_entry(row):
    from crm.models import Account, BankTransaction
    t = BankTransaction.objects.select_related('bank_account').get(pk=row.source_id)
    counter = Account.objects.get(pk=row.payload['account_id'])
    amount = abs(t.amount)
    bank_acc = t.bank_account.ledger_account
    if t.amount > 0:
        lines = [Line(bank_acc, debit=amount, description=t.description[:300]), Line(counter, credit=amount)]
    else:
        lines = [Line(counter, debit=amount, description=t.description[:300]), Line(bank_acc, credit=amount)]
    return {'entry_date': t.txn_date, 'narration': row.payload.get('narration') or t.description[:300],
            'event_key': 'entry', 'lines': lines}


HANDLERS = {
    'invoice_issued': invoice_issued,
    'invoice_cancelled': invoice_cancelled,
    'invoice_written_off': invoice_written_off,
    'tds_written_off': tds_written_off,
    'deferred_release': deferred_release,
    'payment_received': payment_received,
    'advance_allocated': advance_allocated,
    'allocation_reversed': allocation_reversed,
    'credit_note_issued': credit_note_issued,
    'refund_processed': refund_processed,
    'settlement_recorded': settlement_recorded,
    'expense_approved': expense_approved,
    'expense_paid': expense_paid,
    'expense_reversed': expense_reversed,
    'tds_challan': tds_challan,
    'payroll_approved': payroll_approved,
    'payroll_paid': payroll_paid,
    'bank_line_entry': bank_line_entry,
}
