"""Manual journals, reversals, chart of accounts, periods and deferred revenue (Part 8.1, 8.4, 8.6, 8.7)."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, permissions, posting
from crm.core.money import ZERO, q2
from crm.models import (Account, BankAccount, BankReconciliation, BankTransaction, CompanySettings, DeferredRevenueSchedule,
                        Invoice, JournalEntry, Period, PostingOutbox)


class AccountingError(ValidationError):
    pass


def journal_url(je) -> str:
    return reverse('crm:journal_detail', args=[je.pk])


# ---------------------------------------------------------------- manual journals

def create_manual_journal(actor, *, entry_date: date, narration: str, lines: list[posting.Line], post: bool = True,
                          message: str = '') -> JournalEntry:
    """Save as draft or post. Above the policy limit, posting needs Accountant approval (maker-checker)."""
    if not narration.strip():
        raise AccountingError('Add a narration.')
    for ln in lines:
        acc = posting.account(ln.account)
        if not acc.allow_manual:
            raise AccountingError(f'{acc} does not accept manual journals; it is maintained by the system.')
    total = sum((q2(ln.debit) for ln in lines), ZERO)
    limit = CompanySettings.get().journal_approval_above
    needs_approval = post and total > limit
    if post:
        posting.ensure_period_open(entry_date, actor)
    with transaction.atomic():
        status = 'pending_approval' if needs_approval else ('posted' if post else 'draft')
        je = posting.build_journal(entry_date=entry_date, narration=narration, source_type='manual', source_id='',
                                   event_key=f'manual:{timezone.now().timestamp()}:{actor.pk}', lines=lines, user=actor,
                                   is_manual=True, status=status)
        je.source_id = str(je.pk)
        je.event_key = f'manual:{je.pk}'
        je.save(update_fields=['source_id', 'event_key'])
        if needs_approval:
            approvals.submit('journal', je, title=f'Manual journal {posting_money(total)}: {narration[:80]}', module='accounting',
                             requested_by=actor, level='accountant', amount=total,
                             reasons=[f'Above the {posting_money(limit)} journal limit'], message=message)
        audit.log('journal.posted' if status == 'posted' else f'journal.{status}', je, actor=actor,
                  changes={'total': str(total), 'date': entry_date})
    return je


def posting_money(v) -> str:
    from crm.core.pdf import _money
    return _money(v)


def post_draft(je: JournalEntry, actor, message: str = ''):
    if je.status != 'draft' or not je.is_manual:
        raise AccountingError('Only a draft manual journal can be posted.')
    total = je.total_debit
    if total > CompanySettings.get().journal_approval_above:
        with transaction.atomic():
            JournalEntry.objects.filter(pk=je.pk).update(status='pending_approval')
            approvals.submit('journal', je, title=f'Manual journal {posting_money(total)}: {je.narration[:80]}', module='accounting',
                             requested_by=actor, level='accountant', amount=total, reasons=['Above the journal limit'], message=message)
        return je
    with transaction.atomic():
        posting.post_entry(je, actor)
        audit.log('journal.posted', je, actor=actor)
    return je


def discard_draft(je: JournalEntry, actor):
    if je.status != 'draft':
        raise AccountingError('Only a draft can be discarded.')
    with transaction.atomic():
        je.lines.all().delete()
        audit.log('journal.discarded', je, actor=actor, changes={'narration': je.narration})
        JournalEntry.objects.filter(pk=je.pk).delete()


def _journal_approved(req, user):
    je = JournalEntry.objects.get(pk=req.object_id)
    if je.status == 'pending_approval':
        posting.post_entry(je, user)
        audit.log('journal.posted', je, actor=user, changes={'approval': req.pk})


def _journal_rejected(req, user):
    JournalEntry.objects.filter(pk=req.object_id, status='pending_approval').update(status='draft')


approvals.register('journal', _journal_approved, _journal_rejected)


def request_reversal(je: JournalEntry, actor, reason: str, on: date | None = None):
    """Accountants reverse directly; others request approval."""
    if len(reason.strip()) < 10:
        raise AccountingError('Give a reason of at least 10 characters.')
    if je.status != 'posted':
        raise AccountingError('Only a posted journal can be reversed.')
    if hasattr(je, 'reversed_by_entry'):
        raise AccountingError('This journal has already been reversed.')
    if not je.is_manual and je.source_type in ('invoice', 'payment', 'creditnote', 'refund', 'paymentallocation'):
        raise AccountingError('System journals are corrected from their source document (credit note, cancellation, '
                              'allocation reversal or refund), so the documents and the ledger stay in step.')
    if permissions.has_perm(actor, 'accounting', 'approve'):
        return posting.reverse_entry(je, actor, reason, on)
    return approvals.submit('journal_reversal', je, title=f'Reverse {je.number}', module='accounting', requested_by=actor,
                            level='accountant', amount=je.total_debit, reasons=['Journal reversal'], message=reason,
                            payload={'on': (on or timezone.localdate()).isoformat()})


def _reversal_approved(req, user):
    je = JournalEntry.objects.get(pk=req.object_id)
    posting.reverse_entry(je, user, req.message, date.fromisoformat(req.payload.get('on')) if req.payload.get('on') else None)


approvals.register('journal_reversal', _reversal_approved)


# ---------------------------------------------------------------- chart of accounts

def account_has_postings(acc: Account) -> bool:
    return acc.lines.exists()


def save_account(acc: Account, actor, *, is_new: bool):
    if not is_new:
        old = Account.objects.get(pk=acc.pk)
        if account_has_postings(old) and (old.account_type != acc.account_type or old.code != acc.code):
            raise AccountingError('This account has postings: only the name, group and description can change.')
        if old.is_system and old.system_code != acc.system_code:
            raise AccountingError('The system mapping of this account cannot change.')
    acc.save()
    audit.log('account.created' if is_new else 'account.updated', acc, actor=actor,
              changes={'code': acc.code, 'name': acc.name, 'type': acc.account_type})
    return acc


def deactivate_account(acc: Account, actor):
    if acc.is_system:
        raise AccountingError('System accounts used by the posting map cannot be deactivated.')
    if posting.balance(acc) != 0:
        raise AccountingError('Only an account with a zero balance can be deactivated.')
    Account.objects.filter(pk=acc.pk).update(is_active=False)
    audit.log('account.deactivated', acc, actor=actor)


def create_bank_account(actor, *, name, kind, bank_name='', account_number='', ifsc='', opening_balance=ZERO, opening_date=None,
                        is_default_receipt=False):
    with transaction.atomic():
        prefix = {'bank': '101', 'cash': '106', 'gateway': '105'}[kind]
        used = set(Account.objects.filter(code__startswith=prefix).values_list('code', flat=True))
        code = next(f'{prefix}{i}' for i in range(1, 10) if f'{prefix}{i}' not in used)
        acc = Account.objects.create(code=code, name=name, account_type='asset', group='Bank' if kind != 'cash' else 'Cash',
                                     allow_manual=True)
        ba = BankAccount.objects.create(name=name, kind=kind, bank_name=bank_name, account_number=account_number, ifsc=ifsc.upper(),
                                        ledger_account=acc, is_default_receipt=is_default_receipt, created_by=actor)
        if is_default_receipt:
            BankAccount.objects.exclude(pk=ba.pk).update(is_default_receipt=False)
        if opening_balance:
            posting.build_journal(entry_date=opening_date or timezone.localdate(), narration=f'Opening balance {name}',
                                  source_type='bankaccount', source_id=str(ba.pk), event_key='opening',
                                  lines=[posting.Line(acc, debit=opening_balance), posting.Line(posting.OWNER_CAPITAL, credit=opening_balance)],
                                  user=actor)
        audit.log('bank_account.created', ba, actor=actor, changes={'kind': kind, 'opening': str(opening_balance)})
    return ba


def transfer(actor, *, from_acct: BankAccount, to_acct: BankAccount, amount: Decimal, on: date, note: str = ''):
    """Inter-account transfer and petty-cash top-up: one journal, both sides (Part 8.8)."""
    amount = q2(amount)
    if from_acct.pk == to_acct.pk:
        raise AccountingError('Choose two different accounts.')
    if amount <= 0:
        raise AccountingError('Enter an amount above ₹0.')
    je = posting.build_journal(entry_date=on, narration=f'Transfer {from_acct} → {to_acct}. {note}'.strip(), source_type='transfer',
                               source_id=f'{from_acct.pk}-{to_acct.pk}-{timezone.now().timestamp()}', event_key='transfer',
                               lines=[posting.Line(to_acct.ledger_account, debit=amount), posting.Line(from_acct.ledger_account, credit=amount)],
                               user=actor, is_manual=True)
    audit.log('bank.transfer', je, actor=actor, changes={'amount': str(amount), 'from': str(from_acct), 'to': str(to_acct)})
    return je


# ---------------------------------------------------------------- periods (8.7)

def get_period(year: int, month: int) -> Period:
    p, _ = Period.objects.get_or_create(year=year, month=month)
    return p


def close_checklist(p: Period) -> list[dict]:
    from crm.models import PayrollRun
    start = date(p.year, p.month, 1)
    end = date(p.year, p.month, calendar.monthrange(p.year, p.month)[1])
    items = []
    for ba in BankAccount.objects.filter(is_active=True, kind__in=['bank', 'gateway']):
        has_lines = BankTransaction.objects.filter(bank_account=ba, txn_date__lte=end).exists()
        rec = BankReconciliation.objects.filter(bank_account=ba, statement_date__gte=end).exists()
        items.append({'label': f'{ba} reconciled to {end:%d-%b-%Y}', 'ok': rec or not has_lines,
                      'url': reverse('crm:reconciliation') + f'?account={ba.pk}'})
    queue = PostingOutbox.objects.filter(status__in=['pending', 'failed']).count()
    items.append({'label': 'Posting queue empty', 'ok': queue == 0, 'detail': f'{queue} waiting' if queue else '',
                  'url': reverse('crm:accounting_dashboard')})
    drafts = Invoice.objects.filter(status__in=['draft', 'pending_approval'], invoice_date__range=(start, end)).count()
    items.append({'label': 'No draft invoices dated in the period', 'ok': drafts == 0, 'detail': f'{drafts} drafts' if drafts else '',
                  'url': reverse('crm:invoice_list') + '?quick=drafts'})
    from crm.models import Employee
    if Employee.objects.filter(status__in=['active', 'on_notice']).exists():
        locked = PayrollRun.objects.filter(period=start, status__in=['approved', 'paid']).exists()
        items.append({'label': 'Payroll for the month approved', 'ok': locked, 'url': reverse('crm:payroll_list')})
    pending_def = DeferredRevenueSchedule.objects.filter(period_start__lte=end, posted=False).count()
    items.append({'label': 'Deferred revenue released', 'ok': pending_def == 0, 'detail': f'{pending_def} rows' if pending_def else '',
                  'url': reverse('crm:accounting_dashboard')})
    from .reports import trial_balance
    tb = trial_balance(end)
    items.append({'label': 'Trial balance balanced', 'ok': tb['balanced'], 'url': reverse('crm:trial_balance') + f'?as_of={end}'})
    pending_j = JournalEntry.objects.filter(status__in=['draft', 'pending_approval'], entry_date__range=(start, end)).count()
    items.append({'label': 'No draft or unapproved journals in the period', 'ok': pending_j == 0,
                  'detail': f'{pending_j} journals' if pending_j else '', 'url': reverse('crm:journal_list') + '?status=draft'})
    return items


def soft_close(p: Period, actor):
    permissions.require(actor, 'accounting', 'approve')
    if p.status != 'open':
        raise AccountingError('Only an open period can be soft-closed.')
    Period.objects.filter(pk=p.pk).update(status='soft_closed', closed_by=actor, closed_at=timezone.now())
    audit.log('period.soft_closed', p, actor=actor)


def request_close(p: Period, actor, note: str = ''):
    items = close_checklist(p)
    red = [i['label'] for i in items if not i['ok']]
    if red:
        raise AccountingError('Not ready to close: ' + '; '.join(red) + '.')
    if p.status == 'closed':
        raise AccountingError('This period is already closed.')
    if permissions.has_perm(actor, 'settings', 'configure'):
        _close(p, actor, note)
        return None
    return approvals.submit('period_close', p, title=f'Close period {p}', module='accounting', requested_by=actor, level='owner',
                            reasons=['Month-end close'], message=note)


def _close(p, user, note=''):
    Period.objects.filter(pk=p.pk).update(status='closed', closed_by=user, closed_at=timezone.now(), reason=note[:300])
    audit.log('period.closed', p, actor=user, reason=note)


approvals.register('period_close', lambda req, user: _close(Period.objects.get(pk=req.object_id), user, req.message))


def request_reopen(p: Period, actor, reason: str):
    if len(reason.strip()) < 10:
        raise AccountingError('Give a reason of at least 10 characters.')
    if p.status == 'open':
        raise AccountingError('This period is open.')
    if p.status == 'soft_closed' and permissions.has_perm(actor, 'accounting', 'approve'):
        _reopen(p, actor, reason)
        return None
    return approvals.submit('period_reopen', p, title=f'Reopen period {p}', module='accounting', requested_by=actor, level='owner',
                            reasons=['Reopen a closed period'], message=reason)


def _reopen(p, user, reason):
    Period.objects.filter(pk=p.pk).update(status='open', reopened_by=user, reopened_at=timezone.now(), reason=reason[:300])
    audit.log('period.reopened', p, actor=user, reason=reason)


approvals.register('period_reopen', lambda req, user: _reopen(Period.objects.get(pk=req.object_id), user, req.message))


# ---------------------------------------------------------------- deferred revenue (8.6)

def release_deferred(upto: date | None = None) -> int:
    upto = upto or india.today()
    n = 0
    for row in DeferredRevenueSchedule.objects.filter(posted=False, period_start__lte=upto).order_by('period_start')[:500]:
        if posting.period_status(row.period_start) == 'closed':
            continue
        with transaction.atomic():
            posting.enqueue('deferred_release', row, 'released')
        n += 1
    return n


def posting_queue_alert() -> int:
    """Alert accountants when the posting queue has rows older than 5 minutes (Part 8.1 #3)."""
    from datetime import timedelta
    old = PostingOutbox.objects.filter(status__in=['pending', 'failed'], created_at__lte=timezone.now() - timedelta(minutes=5))
    n = old.count()
    if n:
        notify.notify(notify.users_with_perm('accounting', 'approve'), f'{n} ledger posting(s) waiting',
                      body=(old.filter(status='failed').values_list('last_error', flat=True).first() or '')[:300],
                      url=reverse('crm:accounting_dashboard'), event_type='posting_queue', level='error',
                      dedupe_key=f'posting-queue:{timezone.localtime():%Y%m%d%H}')
    return n


def retry_postings(actor) -> int:
    permissions.require(actor, 'accounting', 'approve')
    n = posting.retry_failed()
    audit.log('posting.retried', actor=actor, object_type='postingoutbox', changes={'rows': n})
    return n
