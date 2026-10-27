"""
Expenses, vendor bills, reimbursements, vendor payments, recurring bills and TDS payable (Part 11).

An expense is recorded once; allocations spread its cost and never create a second expense.
Approval is maker-checker; approval posts the payable, payment posts payable to bank.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, numbering, permissions, posting
from crm.core.money import ZERO, q2
from crm.models import CompanySettings, Expense, ExpenseAllocation, ExpensePayment, RecurringExpense, TdsChallan, Vendor
from crm.models.expenses import EXPENSE_PAYABLE


class ExpenseError(ValidationError):
    pass


def expense_url(e) -> str:
    return reverse('crm:expense_detail', args=[e.pk])


# ---------------------------------------------------------------- calculation

def suggested_tds(e: Expense) -> tuple[str, Decimal]:
    """Section and amount from the vendor (first) or category, above the category threshold (CA-approved rules)."""
    if e.kind == 'reimbursement':
        return '', ZERO
    section = (e.vendor.tds_section if e.vendor_id and e.vendor.tds_section else '') or e.category.tds_section
    rate = (e.vendor.tds_rate if e.vendor_id and e.vendor.tds_rate else ZERO) or e.category.tds_rate
    if not section or not rate or e.amount_before_tax <= e.category.tds_threshold:
        return '', ZERO
    return section, q2(e.amount_before_tax * rate / Decimal('100'))


def recompute(e: Expense, *, auto_tds: bool = True):
    e.total = q2(e.amount_before_tax + e.cgst + e.sgst + e.igst)
    if auto_tds and not e.tds_section:
        e.tds_section, e.tds_amount = suggested_tds(e)
    if e.tds_amount > e.amount_before_tax:
        raise ExpenseError('TDS cannot be more than the amount before tax.')
    e.payable_amount = e.total - e.tds_amount


def duplicates(e: Expense):
    qs = Expense.objects.exclude(pk=e.pk or 0).exclude(status='reversed')
    out = []
    if e.vendor_id and e.bill_number:
        out += list(qs.filter(vendor_id=e.vendor_id, bill_number__iexact=e.bill_number))
    if e.vendor_id:
        out += [x for x in qs.filter(vendor_id=e.vendor_id, amount_before_tax=e.amount_before_tax,
                                     bill_date__range=(e.bill_date - timedelta(days=7), e.bill_date + timedelta(days=7))) if x not in out]
    if e.kind == 'reimbursement' and e.employee_id:
        out += [x for x in qs.filter(employee_id=e.employee_id, amount_before_tax=e.amount_before_tax, bill_date=e.bill_date) if x not in out]
    return out


def proof_count(e: Expense) -> int:
    from django.contrib.contenttypes.models import ContentType
    from crm.models import Attachment
    return Attachment.objects.filter(content_type=ContentType.objects.get_for_model(Expense), object_id=str(e.pk), archived_at__isnull=True).count()


def submit_blockers(e: Expense) -> list[str]:
    c = CompanySettings.get()
    errs = []
    if e.amount_before_tax <= 0:
        errs.append('Enter the amount')
    if e.total > c.expense_proof_above and not proof_count(e):
        errs.append(f'Attach the bill or receipt (required above {posting_money(c.expense_proof_above)})')
    if e.kind == 'vendor_bill' and not e.vendor_id:
        errs.append('Choose the vendor')
    if e.kind == 'reimbursement' and not e.employee_id:
        errs.append('Choose who is reimbursed')
    if e.vendor_id and e.vendor.status == 'blocked':
        errs.append('The vendor is blocked')
    if e.itc_eligibility == 'eligible' and e.tax_total and not (e.vendor_gstin or (e.vendor_id and e.vendor.gstin)):
        errs.append('Input credit needs the vendor GSTIN')
    allocs = list(e.allocations.all())
    if allocs and sum((a.percent for a in allocs), Decimal('0')) != Decimal('100'):
        errs.append('Allocations must add up to 100%')
    if e.classification == 'direct' and not (e.project_id or allocs):
        errs.append('A direct cost needs a project or an allocation')
    if posting.period_status(e.bill_date) == 'closed':
        errs.append('The bill date is in a closed period')
    if duplicates(e) and not e.duplicate_override_reason:
        errs.append('Possible duplicate: confirm it is not a duplicate first')
    return errs


def posting_money(v) -> str:
    from crm.core.pdf import _money
    return _money(v)


def save_allocations(e: Expense, rows: list[dict], actor):
    """rows = [{'project','client','department','percent'}]; amounts split exactly, last row takes the remainder."""
    if e.status not in ('draft', 'correction_requested', 'approved', 'partially_paid', 'paid'):
        raise ExpenseError('Allocations cannot change in this status.')
    total_pct = sum((Decimal(r['percent']) for r in rows), Decimal('0'))
    if rows and total_pct != Decimal('100'):
        raise ExpenseError(f'Allocations must add up to 100%. They add up to {total_pct.normalize():f}%.')
    base = e.amount_before_tax + (ZERO if e.itc_eligibility == 'eligible' else e.tax_total)
    with transaction.atomic():
        e.allocations.all().delete()
        remaining = base
        for i, r in enumerate(rows):
            amt = remaining if i == len(rows) - 1 else q2(base * Decimal(r['percent']) / Decimal('100'))
            remaining -= amt
            ExpenseAllocation.objects.create(expense=e, project=r.get('project'), client=r.get('client'), department=r.get('department'),
                                             percent=Decimal(r['percent']), amount=amt)
        audit.log('expense.allocated', e, actor=actor, changes={'rows': len(rows)})


# ---------------------------------------------------------------- workflow (11.1)

def approval_level(e: Expense) -> str:
    """Every expense, whatever the amount, needs the owner's confirmation."""
    return 'owner'


def submit(e: Expense, actor, message: str = ''):
    with transaction.atomic():
        e = Expense.objects.select_for_update().get(pk=e.pk)
        if e.status not in ('draft', 'correction_requested', 'rejected'):
            raise ExpenseError('Only a draft can be submitted.')
        recompute(e, auto_tds=False)
        errs = submit_blockers(e)
        if errs:
            raise ExpenseError('Before submitting: ' + '; '.join(errs) + '.')
        e.status = 'submitted'
        e.submitted_at = timezone.now()
        e.save(update_fields=['status', 'submitted_at', 'total', 'payable_amount'])
        approvals.submit('expense', e, title=f'{e.get_kind_display()} {posting_money(e.total)}: {e.description[:80]}', module='expenses',
                         requested_by=actor, level=approval_level(e), amount=e.total,
                         reasons=[f'{e.category} · {e.vendor or e.employee or ""}'], message=message)
        audit.log('expense.submitted', e, actor=actor, changes={'total': str(e.total)})


def _approved(req, user):
    with transaction.atomic():
        e = Expense.objects.select_for_update().get(pk=req.object_id)
        if e.status != 'submitted':
            return
        if e.created_by_id == user.pk or e.employee_id == user.pk:
            raise approvals.ApprovalError('You cannot approve your own expense.')
        posting.ensure_period_open(e.bill_date, user)
        e.number = e.number or numbering.allocate('EXP', e.bill_date)
        e.status = 'approved'
        e.approved_by = user
        e.approved_at = timezone.now()
        e.decision_comment = req.comment
        e.save(update_fields=['number', 'status', 'approved_by', 'approved_at', 'decision_comment'])
        posting.enqueue('expense_approved', e, 'approved')
        audit.log('expense.approved', e, actor=user, changes={'number': e.number})


def _rejected(req, user):
    Expense.objects.filter(pk=req.object_id, status='submitted').update(status='rejected', decision_comment=req.comment[:500])


approvals.register('expense', _approved, _rejected)


def request_correction(e: Expense, actor, comment: str):
    if len(comment.strip()) < 10:
        raise ExpenseError('Say what needs correcting (at least 10 characters).')
    req = approvals.pending_for(e, 'expense')
    if req is None or e.status != 'submitted':
        raise ExpenseError('Only a submitted expense can be sent back.')
    ok, why = approvals.can_decide(actor, req)
    if not ok:
        raise ExpenseError(why)
    with transaction.atomic():
        approvals.cancel(req, actor, f'Correction requested: {comment}', run_reject_handler=False)
        Expense.objects.filter(pk=e.pk).update(status='correction_requested', decision_comment=comment[:500])
        audit.log('expense.correction_requested', e, actor=actor, reason=comment)
    who = e.employee or e.created_by
    if who:
        notify.notify([who], f'Correction needed: {e.description[:60]}', body=comment, url=expense_url(e), event_type='expense_correction',
                      level='warning')


def pay(e: Expense, actor, *, amount: Decimal, payment_date, bank_account, method='bank_transfer', reference='') -> ExpensePayment:
    amount = q2(amount)
    with transaction.atomic():
        e = Expense.objects.select_for_update().get(pk=e.pk)
        if e.status not in EXPENSE_PAYABLE:
            raise ExpenseError('Only an approved bill can be paid.')
        if e.vendor_id and e.vendor.status == 'blocked':
            raise ExpenseError('The vendor is blocked: payments are not allowed.')
        if amount <= 0 or amount > e.balance:
            raise ExpenseError(f'Pay between ₹0.01 and the balance of {posting_money(e.balance)}.')
        if payment_date > india.today():
            raise ExpenseError('The payment date cannot be in the future.')
        if method != 'cash' and not reference:
            raise ExpenseError('Enter the payment reference.')
        posting.ensure_period_open(payment_date, actor)
        try:
            with transaction.atomic():
                p = ExpensePayment.objects.create(expense=e, amount=amount, payment_date=payment_date, bank_account=bank_account,
                                                  method=method, reference=reference.strip(), created_by=actor)
        except IntegrityError:
            raise ExpenseError(f'Reference {reference} is already recorded for this bank account.')
        e.paid_amount += amount
        e.status = 'paid' if e.paid_amount >= e.payable_amount else 'partially_paid'
        e.save(update_fields=['paid_amount', 'status'])
        posting.enqueue('expense_paid', p, 'paid')
        audit.log('expense.paid', e, actor=actor, changes={'amount': str(amount), 'reference': reference})
    return p


def reverse_expense(e: Expense, actor, reason: str):
    if not permissions.has_perm(actor, 'accounting', 'approve'):
        permissions.denied(actor, 'accounting', 'approve', detail='reverse expense')
    if len(reason.strip()) < 10:
        raise ExpenseError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        e = Expense.objects.select_for_update().get(pk=e.pk)
        if e.status != 'approved' or e.paid_amount:
            raise ExpenseError('Only an approved, unpaid expense can be reversed.')
        e.status = 'reversed'
        e.decision_comment = reason[:500]
        e.save(update_fields=['status', 'decision_comment'])
        posting.enqueue('expense_reversed', e, 'reversed')
        audit.log('expense.reversed', e, actor=actor, reason=reason)


# ---------------------------------------------------------------- recurring (11.1)

STEP_MONTHS = {'monthly': 1, 'quarterly': 3, 'annual': 12}


def run_recurring(r: RecurringExpense, actor=None, upto=None) -> int:
    upto = upto or india.today()
    made = 0
    while r.status == 'active' and r.next_run <= upto:
        if r.end_date and r.next_run > r.end_date:
            RecurringExpense.objects.filter(pk=r.pk).update(status='ended')
            break
        tax = q2(r.amount_before_tax * r.tax_rate / Decimal('100'))
        vendor_state = r.vendor.state if r.vendor_id else ''
        intra = not vendor_state or vendor_state == CompanySettings.get().state_code
        e = Expense(kind='vendor_bill' if r.vendor_id else 'expense', vendor=r.vendor, category=r.category,
                    description=f'{r.name} {r.next_run:%b-%Y}', bill_date=r.next_run, due_date=r.next_run + timedelta(days=15),
                    amount_before_tax=r.amount_before_tax, cgst=q2(tax / 2) if intra else ZERO,
                    sgst=tax - q2(tax / 2) if intra else ZERO, igst=ZERO if intra else tax, itc_eligibility=r.category.itc_default,
                    vendor_gstin=r.vendor.gstin if r.vendor_id else '', recurring=r, recurring_period=r.next_run, created_by=actor)
        recompute(e)
        try:
            with transaction.atomic():
                e.save()
                made += 1
                audit.log('expense.recurring_generated', e, actor=actor, changes={'recurring': r.pk, 'period': r.next_run})
        except IntegrityError:
            pass  # this period already exists: idempotent
        r.next_run = india.add_months(r.next_run, STEP_MONTHS[r.frequency])
        RecurringExpense.objects.filter(pk=r.pk).update(next_run=r.next_run)
    return made


def generate_recurring() -> int:
    return sum(run_recurring(r) for r in RecurringExpense.objects.filter(status='active', next_run__lte=india.today()))


# ---------------------------------------------------------------- TDS payable (11.5)

def record_challan(actor, *, section, amount, paid_on, cin, bank_account) -> TdsChallan:
    amount = q2(amount)
    if amount <= 0:
        raise ExpenseError('Enter an amount above ₹0.')
    if paid_on > india.today():
        raise ExpenseError('The date cannot be in the future.')
    with transaction.atomic():
        if TdsChallan.objects.filter(cin=cin).exists():
            raise ExpenseError('This CIN is already recorded.')
        c = TdsChallan.objects.create(section=section, amount=amount, paid_on=paid_on, cin=cin.strip().upper(),
                                      quarter=india.fy_quarter(paid_on), fiscal_year=india.fy_label(paid_on), bank_account=bank_account,
                                      created_by=actor)
        posting.enqueue('tds_challan', c, 'paid')
        audit.log('tds.challan_recorded', c, actor=actor, changes={'section': section, 'amount': str(amount), 'cin': cin})
    return c


def tds_by_vendor(fy: str):
    from django.db.models import Sum
    start_year = int(fy[:4])
    from datetime import date
    return Expense.objects.filter(bill_date__range=(date(start_year, 4, 1), date(start_year + 1, 3, 31)), tds_amount__gt=0,
                                  status__in=['approved', 'partially_paid', 'paid']).values('vendor__name', 'vendor__pan', 'tds_section') \
        .annotate(amount=Sum('tds_amount'), base=Sum('amount_before_tax')).order_by('vendor__name')


# ---------------------------------------------------------------- vendors

class DuplicateVendorError(ExpenseError):
    """Raised instead of silently saving when possible_vendor_duplicates() finds a match and
    the caller hasn't confirmed this is a genuinely new vendor."""
    def __init__(self, dupes):
        self.dupes = dupes
        super().__init__('A vendor with the same name, PAN or GSTIN exists.')


def save_vendor(form, actor, *, confirm_new: bool = False) -> Vendor:
    """Validated-save shared by the web view (crm.modules.expenses.views.vendor_form) and the
    MCP tool. `form` must already be a bound, valid VendorForm."""
    v = form.instance if form.instance.pk else None
    dupes = list(possible_vendor_duplicates(form.cleaned_data['name'], form.cleaned_data.get('pan', ''),
                                            form.cleaned_data.get('gstin', ''), exclude=v.pk if v else None))
    if dupes and not confirm_new:
        raise DuplicateVendorError(dupes)
    obj = form.save(commit=False)
    obj.updated_by = actor
    if not obj.pk:
        obj.created_by = actor
    obj.save()
    audit.log('vendor.saved', obj, actor=actor, changes={'fields': form.changed_data})
    return obj


def set_vendor_status(v: Vendor, actor, status: str, reason: str = ''):
    if status not in dict(Vendor.STATUSES):
        raise ExpenseError('Unknown status.')
    if status != 'active' and len(reason.strip()) < 10:
        raise ExpenseError('Give a reason of at least 10 characters.')
    Vendor.objects.filter(pk=v.pk).update(status=status, status_reason=reason[:300] if status != 'active' else '')
    audit.log('vendor.status', v, actor=actor, changes={'to': status}, reason=reason)


def vendor_outstanding(v: Vendor) -> Decimal:
    return sum((e.balance for e in v.expenses.filter(status__in=EXPENSE_PAYABLE)), ZERO)


def vendor_tds_ytd(v: Vendor) -> Decimal:
    from django.db.models import Sum
    start, _ = india.fy_bounds(india.today())
    return v.expenses.filter(bill_date__gte=start, status__in=['approved', 'partially_paid', 'paid']).aggregate(s=Sum('tds_amount'))['s'] or ZERO


def possible_vendor_duplicates(name: str, pan: str = '', gstin: str = '', exclude=None):
    q = Q(name__iexact=name.strip())
    if pan:
        q |= Q(pan__iexact=pan)
    if gstin:
        q |= Q(gstin__iexact=gstin)
    qs = Vendor.objects.filter(q)
    return qs.exclude(pk=exclude) if exclude else qs
