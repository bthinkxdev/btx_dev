"""Nightly integrity checks (Part 14). Each check is read-only and reports what it found.

A failing check never changes data: it raises an alert to the owners and shows on
Jobs and health, so a person decides the fix.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.db.models import F, Q, Sum

from .money import ZERO

logger = logging.getLogger('crm.integrity')


class IntegrityFailed(Exception):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def _result(code, label, ok, detail='', items=None):
    return {'code': code, 'label': label, 'ok': bool(ok), 'detail': detail, 'items': [str(i) for i in (items or [])][:20]}


def check_journals():
    from crm.models import JournalLine
    bad = list(JournalLine.objects.filter(posted=True).values('entry_id').annotate(d=Sum('debit'), c=Sum('credit'))
               .exclude(d=F('c')).values_list('entry_id', flat=True)[:50])
    return _result('journals', 'Every posted journal balances', not bad,
                   f'{len(bad)} posted journal(s) where debits differ from credits.' if bad else 'All posted journals balance.',
                   [f'Journal #{i}' for i in bad])


def check_trial_balance():
    from crm.models import JournalLine
    agg = JournalLine.objects.filter(posted=True).aggregate(d=Sum('debit'), c=Sum('credit'))
    d, c = agg['d'] or ZERO, agg['c'] or ZERO
    return _result('trial_balance', 'Trial balance totals agree', d == c, f'Debits {d:,.2f}, credits {c:,.2f}.')


def check_ar():
    from crm.core import india
    from crm.modules.accounting.reports import ar_tie_out
    r = ar_tie_out(india.today())
    return _result('ar_tie_out', 'Receivables ledger matches open invoices', r['ok'],
                   f"Control {r['control']:,.2f}, invoices {r['subledger']:,.2f}, difference {r['difference']:,.2f}.")


def check_ap():
    from crm.core import india
    from crm.modules.accounting.reports import ap_tie_out
    r = ap_tie_out(india.today())
    return _result('ap_tie_out', 'Payables ledger matches open bills', r['ok'],
                   f"Control {r['control']:,.2f}, bills {r['subledger']:,.2f}, difference {r['difference']:,.2f}.")


def check_invoice_balances():
    """Stored balance = grand total minus allocations, TDS, advances, credit notes and write-off."""
    from crm.models import Invoice
    from crm.models.billing import INV_POSTED
    bad = []
    for inv in Invoice.objects.filter(status__in=INV_POSTED).iterator(chunk_size=500):
        allocs = inv.allocations.filter(reversed_at__isnull=True).aggregate(a=Sum('amount'), t=Sum('tds_amount'))
        credited = inv.credit_notes.filter(status='issued').aggregate(s=Sum('total'))['s'] or ZERO
        expected = inv.grand_total - (allocs['a'] or ZERO) - (allocs['t'] or ZERO) - credited - (inv.written_off or ZERO)
        if expected != inv.balance or inv.balance < 0:
            bad.append(f'{inv.number or inv.pk}: stored {inv.balance}, expected {expected}')
    return _result('invoice_balances', 'Invoice balances match their allocations and credits', not bad,
                   f'{len(bad)} invoice(s) out of step.' if bad else 'All invoice balances agree.', bad)


def check_payments():
    from crm.models import Payment, PaymentAllocation
    from crm.models.payments import P_COUNTED
    bad = []
    sums = dict(PaymentAllocation.objects.filter(reversed_at__isnull=True).values('payment_id')
                .annotate(s=Sum(F('amount') + F('tds_amount'))).values_list('payment_id', 's'))
    for p in Payment.objects.filter(Q(status__in=P_COUNTED) | Q(allocated_amount__gt=0)).iterator(chunk_size=500):
        alloc = sums.get(p.pk, ZERO) or ZERO
        if alloc != p.allocated_amount:
            bad.append(f'{p.receipt_number or p.pk}: allocated field {p.allocated_amount}, allocations {alloc}')
        elif p.allocated_amount > p.amount + p.tds_amount - p.refunded_amount + Decimal('0.005'):
            bad.append(f'{p.receipt_number or p.pk}: allocated {p.allocated_amount} is more than received')
    return _result('payments', 'Payments are never over-allocated', not bad,
                   f'{len(bad)} payment(s) out of step.' if bad else 'All payment allocations agree.', bad)


def check_postings():
    from crm.models import PostingOutbox
    failed = PostingOutbox.objects.filter(status='failed').count()
    return _result('postings', 'No failed ledger postings', failed == 0,
                   f'{failed} business event(s) failed to post to the ledger.' if failed else 'The posting queue is clear.')


_NUM = re.compile(r'^(?P<prefix>.+)/(?P<fy>\d{4}-\d{2})/(?P<n>\d+)$')


def check_numbering():
    """Tax documents are gap-free per series and financial year (Part 1.5)."""
    from crm.models import CreditNote, DocumentSequence, Invoice
    problems = []
    for model, doc_type in ((Invoice, 'INV'), (CreditNote, 'CN')):
        seen: dict = {}
        for number in model.objects.exclude(number='').values_list('number', flat=True):
            m = _NUM.match(number or '')
            if m:
                seen.setdefault(m['fy'], []).append(int(m['n']))
        for seq in DocumentSequence.objects.filter(doc_type=doc_type).exclude(fiscal_year='ALL'):
            nums = sorted(seen.get(seq.fiscal_year, []))
            issued = seq.next_number - 1
            dupes = len(nums) - len(set(nums))
            missing = sorted(set(range(1, issued + 1)) - set(nums))
            if dupes:
                problems.append(f'{doc_type} {seq.fiscal_year}: {dupes} duplicate number(s)')
            if missing:
                problems.append(f'{doc_type} {seq.fiscal_year}: missing {", ".join(map(str, missing[:10]))}'
                                f'{"…" if len(missing) > 10 else ""}')
            if nums and nums[-1] > issued:
                problems.append(f'{doc_type} {seq.fiscal_year}: number {nums[-1]} is beyond the series ({issued})')
    return _result('numbering', 'Invoice and credit note numbers have no gaps', not problems,
                   f'{len(problems)} series problem(s).' if problems else 'All series are continuous.', problems)


def check_audit_chain():
    from crm.core.audit import verify_chain
    ok, msg = verify_chain()
    return _result('audit_chain', 'Audit log matches its hash checkpoints', ok, msg)


CHECKS = [check_journals, check_trial_balance, check_ar, check_ap, check_invoice_balances, check_payments, check_postings,
          check_numbering, check_audit_chain]


def run_checks(actor=None) -> dict:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # a broken check is itself a failure, never silent
            logger.exception('Integrity check %s crashed', fn.__name__)
            results.append(_result(fn.__name__.replace('check_', ''), fn.__name__.replace('check_', '').replace('_', ' ').capitalize(),
                                   False, f'The check could not run: {exc}'))
    failed = [r for r in results if not r['ok']]
    from crm.core import audit
    audit.log('integrity.checked', actor=actor, object_type='integrity', result='success' if not failed else 'failure',
              changes={'failed': [r['code'] for r in failed]})
    if failed:
        from django.urls import reverse
        from crm.core import notify
        notify.notify(notify.owners(), f'{len(failed)} integrity check(s) failed',
                      body='; '.join(r['label'] for r in failed)[:480], url=reverse('crm:job_list') + '#integrity',
                      event_type='integrity', level='critical', email=True, dedupe_key=f'integrity:{",".join(r["code"] for r in failed)}')
    return {'checks': results, 'passed': len(results) - len(failed), 'failed': len(failed)}


def nightly():
    """Job entry point: a failed check marks the run failed so it shows in the failed-job queue."""
    result = run_checks()
    if result['failed']:
        raise IntegrityFailed('; '.join(f"{r['label']}: {r['detail']}" for r in result['checks'] if not r['ok']), result)
    return result
