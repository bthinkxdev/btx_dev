"""
Posting engine (Part 8.1, 8.3).

Business code calls `enqueue()` inside its own transaction: the outbox row commits
atomically with the business change. `process()` turns the outbox row into a
balanced, posted journal; it runs right after commit and again from the job
runner for anything that failed. A unique (source_type, source_id, event_key) on
JournalEntry means one source event can post only once, however often it retries.

Posted journals are immutable; corrections are reversing journals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import audit
from .money import ZERO, D, q2

logger = logging.getLogger('crm.posting')

# System account codes used by the posting map (seeded by crm_bootstrap).
AR = 'AR'
GATEWAY_CLEARING = 'GATEWAY_CLEARING'
CUSTOMER_ADVANCES = 'CUSTOMER_ADVANCES'
OUTPUT_CGST = 'OUTPUT_CGST'
OUTPUT_SGST = 'OUTPUT_SGST'
OUTPUT_IGST = 'OUTPUT_IGST'
INPUT_CGST = 'INPUT_CGST'
INPUT_SGST = 'INPUT_SGST'
INPUT_IGST = 'INPUT_IGST'
TDS_RECEIVABLE = 'TDS_RECEIVABLE'
TDS_PAYABLE = 'TDS_PAYABLE'
SERVICE_REVENUE = 'SERVICE_REVENUE'
RECURRING_REVENUE = 'RECURRING_REVENUE'
DEFERRED_REVENUE = 'DEFERRED_REVENUE'
DISCOUNTS_ALLOWED = 'DISCOUNTS_ALLOWED'
GATEWAY_FEES = 'GATEWAY_FEES'
GST_ON_FEES = 'GST_ON_FEES'
ROUND_OFF = 'ROUND_OFF'
BAD_DEBTS = 'BAD_DEBTS'
BAD_DEBT_RECOVERY = 'BAD_DEBT_RECOVERY'
AP = 'AP'
REIMBURSEMENTS_PAYABLE = 'REIMBURSEMENTS_PAYABLE'
SALARIES_PAYABLE = 'SALARIES_PAYABLE'
SALARY_EXPENSE = 'SALARY_EXPENSE'
EMPLOYER_CONTRIB = 'EMPLOYER_CONTRIB'
STATUTORY_PAYABLE = 'STATUTORY_PAYABLE'
COMMISSION_EXPENSE = 'COMMISSION_EXPENSE'
BANK_CHARGES = 'BANK_CHARGES'
INTEREST_INCOME = 'INTEREST_INCOME'
OWNER_DRAWINGS = 'OWNER_DRAWINGS'
OWNER_CAPITAL = 'OWNER_CAPITAL'
SUSPENSE = 'SUSPENSE'
RETAINED_EARNINGS = 'RETAINED_EARNINGS'


class PostingError(ValidationError):
    pass


@dataclass
class Line:
    account: object          # Account instance or system code
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ''
    client_id: int | None = None
    project_id: int | None = None
    service_id: int | None = None
    department_id: int | None = None


def account(code_or_obj):
    from crm.models import Account
    if isinstance(code_or_obj, Account):
        return code_or_obj
    try:
        return Account.objects.get(system_code=code_or_obj)
    except Account.DoesNotExist as exc:
        raise PostingError(f'Ledger account "{code_or_obj}" is not configured. Run crm_bootstrap.') from exc


def period_status(on: date) -> str:
    from crm.models import Period
    p = Period.objects.filter(year=on.year, month=on.month).first()
    return p.status if p else 'open'


def ensure_period_open(on: date, user=None, allow_soft_closed_for_accountant: bool = True):
    status = period_status(on)
    if status == 'closed':
        import calendar
        raise PostingError(f'The period {calendar.month_abbr[on.month]}-{on.year} is closed. '
                           f'Ask the Owner to reopen it before posting.')
    if status == 'soft_closed':
        from . import permissions
        if not (allow_soft_closed_for_accountant and user is not None
                and permissions.has_perm(user, 'accounting', 'approve')):
            import calendar
            raise PostingError(f'The period {calendar.month_abbr[on.month]}-{on.year} is soft-closed. '
                               f'Only an accountant can post into it.')


def build_journal(*, entry_date: date, narration: str, source_type: str, source_id: str, event_key: str,
                  lines: list[Line], user=None, is_manual: bool = False, status: str = 'posted',
                  reversal_of=None):
    """Create a journal. Validates balance to the paisa; rejects if the period is closed."""
    from crm.models import JournalEntry, JournalLine
    from . import numbering
    cleaned = []
    for ln in lines:
        # A negative debit is a credit and vice versa; a line carrying both is netted.
        net = q2(ln.debit) - q2(ln.credit)
        if net == 0:
            continue
        dr, cr = (net, ZERO) if net > 0 else (ZERO, -net)
        cleaned.append((ln, dr, cr))
    total_dr = sum((c[1] for c in cleaned), ZERO)
    total_cr = sum((c[2] for c in cleaned), ZERO)
    if len(cleaned) < 2:
        raise PostingError('A journal needs at least two lines.')
    if total_dr != total_cr:
        raise PostingError(f'Debits ({total_dr}) and credits ({total_cr}) must be equal.')
    if status == 'posted':
        ensure_period_open(entry_date, user)
    entry = JournalEntry.objects.create(
        entry_date=entry_date, narration=narration[:500], source_type=source_type, source_id=str(source_id),
        event_key=event_key[:80], status='draft', is_manual=is_manual, total_debit=total_dr, total_credit=total_cr,
        created_by=user, reversal_of=reversal_of,
    )
    for ln, dr, cr in cleaned:
        acc = account(ln.account)
        if not acc.is_active:
            raise PostingError(f'Account {acc} is inactive.')
        JournalLine.objects.create(
            entry=entry, account=acc, debit=dr, credit=cr, description=ln.description[:300],
            client_id=ln.client_id, project_id=ln.project_id, service_id=ln.service_id,
            department_id=ln.department_id, entry_date=entry_date, posted=False,
        )
    if status == 'posted':
        post_entry(entry, user)
    elif status == 'pending_approval':
        entry.status = 'pending_approval'
        entry.save(update_fields=['status'])
    return entry


def post_entry(entry, user=None):
    from crm.models import JournalEntry
    from . import numbering
    ensure_period_open(entry.entry_date, user)
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status == 'posted':
        return entry
    totals = entry.lines.values_list('debit', 'credit')
    dr = sum((t[0] for t in totals), ZERO)
    cr = sum((t[1] for t in totals), ZERO)
    if dr != cr or dr == 0:
        raise PostingError('Debits and credits must be equal before posting.')
    entry.number = numbering.allocate('JV', entry.entry_date)
    entry.status = 'posted'
    entry.posted_at = timezone.now()
    entry.posted_by = user
    entry.total_debit, entry.total_credit = dr, cr
    entry.save(update_fields=['number', 'status', 'posted_at', 'posted_by', 'total_debit', 'total_credit'])
    entry.lines.update(posted=True)
    return entry


def reverse_entry(entry, user, reason: str, on: date | None = None):
    """Correction is a reversing journal linked to the original (Part 8.1 #4)."""
    from crm.models import JournalEntry
    with transaction.atomic():
        entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
        if entry.status != 'posted':
            raise PostingError('Only a posted journal can be reversed.')
        if hasattr(entry, 'reversed_by_entry'):
            raise PostingError('This journal has already been reversed.')
        on = on or timezone.localdate()
        lines = [Line(account=l.account, debit=l.credit, credit=l.debit, description=f'Reversal: {l.description}'[:300],
                      client_id=l.client_id, project_id=l.project_id, service_id=l.service_id,
                      department_id=l.department_id) for l in entry.lines.all()]
        rev = build_journal(entry_date=on, narration=f'Reversal of {entry.number}: {reason}'[:500],
                            source_type=entry.source_type, source_id=entry.source_id,
                            event_key=f'reversal:{entry.pk}', lines=lines, user=user, reversal_of=entry)
        entry.status = 'reversed'
        entry.save(update_fields=['status'])
        audit.log('journal.reversed', entry, actor=user, reason=reason, changes={'reversal': rev.number})
    return rev


# ---------------------------------------------------------------- outbox

def enqueue(event_type: str, source, event_key: str, payload: dict | None = None):
    """Record a posting request inside the caller's transaction; processed after commit."""
    from crm.models import PostingOutbox
    source_type = source._meta.model_name
    full_key = f'{source_type}:{source.pk}:{event_key}'
    try:
        with transaction.atomic():
            row = PostingOutbox.objects.create(event_key=full_key[:160], event_type=event_type,
                                               source_type=source_type, source_id=str(source.pk),
                                               payload=audit._clean(payload or {}))
    except IntegrityError:
        return PostingOutbox.objects.get(event_key=full_key[:160])
    transaction.on_commit(lambda: process_safely(row.pk))
    return row


def process_safely(outbox_id: int):
    try:
        process(outbox_id)
    except Exception:  # recorded on the row; job runner retries
        logger.exception('Posting %s failed', outbox_id)


def process(outbox_id: int):
    from crm.models import JournalEntry, PostingOutbox
    from . import posting_rules
    with transaction.atomic():
        row = PostingOutbox.objects.select_for_update().get(pk=outbox_id)
        if row.status in ('posted', 'skipped'):
            return row
        row.attempts += 1
        try:
            with transaction.atomic():
                handler = posting_rules.HANDLERS.get(row.event_type)
                if handler is None:
                    raise PostingError(f'No posting rule for {row.event_type}.')
                spec = handler(row)
                if spec is None:
                    row.status = 'skipped'
                    row.last_error = ''
                else:
                    existing = JournalEntry.objects.filter(source_type=row.source_type, source_id=row.source_id,
                                                           event_key=spec['event_key'][:80]).first()
                    journal = existing or build_journal(source_type=row.source_type, source_id=row.source_id, **spec)
                    row.journal = journal
                    row.status = 'posted'
                    row.posted_at = timezone.now()
                    row.last_error = ''
        except Exception as exc:
            row.status = 'failed'
            row.last_error = str(exc)[:2000]
        row.save()
        return row


def retry_failed(limit: int = 200) -> int:
    from crm.models import PostingOutbox
    n = 0
    for row in PostingOutbox.objects.filter(status__in=['pending', 'failed']).order_by('id')[:limit]:
        process_safely(row.pk)
        n += 1
    return n


# ---------------------------------------------------------------- balances

def balance(account_obj, as_of: date | None = None, start: date | None = None, **dims) -> Decimal:
    """Signed balance: positive in the account's normal direction."""
    from django.db.models import Sum
    from crm.models import JournalLine
    qs = JournalLine.objects.filter(account=account_obj, posted=True)
    if as_of:
        qs = qs.filter(entry_date__lte=as_of)
    if start:
        qs = qs.filter(entry_date__gte=start)
    for k, v in dims.items():
        qs = qs.filter(**{k: v})
    agg = qs.aggregate(d=Sum('debit'), c=Sum('credit'))
    d, c = D(agg['d'] or 0), D(agg['c'] or 0)
    return d - c if account_obj.is_debit_normal else c - d


def receivable_for_client(client, as_of: date | None = None) -> Decimal:
    return balance(account(AR), as_of=as_of, client_id=client.pk)
