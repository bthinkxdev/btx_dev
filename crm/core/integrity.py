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


def _result(code, label, ok, detail='', items=None, warn=False):
    """``warn`` marks an operational gap: it is shown and alerted, but does not fail the nightly job like a money check."""
    return {'code': code, 'label': label, 'ok': bool(ok), 'detail': detail, 'items': [str(i) for i in (items or [])][:20], 'warn': warn}


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


def _gap(code, label, rows, fine, bad):
    rows = list(rows)
    return _result(code, label, not rows, bad.format(n=len(rows)) if rows else fine, rows[:20], warn=True)


def check_leads_next_action():
    from crm.models import Lead
    from crm.models.leads import OPEN_STAGES
    qs = (Lead.objects.filter(stage__in=OPEN_STAGES, archived_at__isnull=True, owner__isnull=False).exclude(follow_ups__status='pending')
          .select_related('owner'))
    return _gap('leads_next_action', 'Every open lead has a pending follow-up', [f'{l}, owner {l.owner}' for l in qs[:50]],
                'Every open lead has a dated follow-up.', '{n} open lead(s) have no pending follow-up.')


def check_leads_owned():
    from datetime import timedelta
    from django.utils import timezone
    from crm.models import Lead
    from crm.models.leads import OPEN_STAGES
    inactive = Lead.objects.filter(stage__in=OPEN_STAGES, archived_at__isnull=True, owner__is_active=False).select_related('owner')
    waiting = Lead.objects.filter(stage='new', archived_at__isnull=True, owner__isnull=True, created_at__lt=timezone.now() - timedelta(days=1))
    rows = [f'{l}, owner {l.owner} is disabled' for l in inactive[:30]] + [f'{l}, unassigned for over a day' for l in waiting[:30]]
    return _gap('leads_owned', 'Every open lead has an active owner', rows, 'Every open lead is with someone who can work it.',
                '{n} lead(s) are with a disabled login or have waited over a day for an owner.')


def check_clients_owned():
    from crm.models import Client
    qs = Client.objects.filter(status='active', archived_at__isnull=True, owner__is_active=False).select_related('owner')
    return _gap('clients_owned', 'Every customer has an active owner', [f'{c}, owner {c.owner} is disabled' for c in qs[:50]],
                'Every customer is with someone who can serve them.', '{n} customer(s) belong to a disabled login.')


def check_contract_coverage():
    from crm.core import india
    from crm.models import BillingSchedule, Contract
    from crm.models.catalog import RECURRING_MODELS
    today = india.today()
    rec = Contract.objects.filter(status='active', lines__billing_model__in=list(RECURRING_MODELS)).distinct().select_related('client')
    rows = []
    for c in rec:
        if not hasattr(c, 'delivery_plan'):
            rows.append(f'{c} for {c.client}: no monthly delivery plan')
        if not c.end_date or (c.end_date - today).days > 35:
            if not BillingSchedule.objects.filter(contract_line__contract=c, status__in=['scheduled', 'failed'], invoice_date__gte=today).exists():
                rows.append(f'{c} for {c.client}: no future billing is scheduled')
    failed = BillingSchedule.objects.filter(status='failed', contract_line__contract__status='active').count()
    if failed:
        rows.append(f'{failed} recurring invoice(s) failed to generate')
    return _gap('contract_coverage', 'Every monthly contract has a plan and billing ahead', rows,
                'Every active monthly contract has a delivery plan and billing scheduled.', '{n} contract problem(s).')


def check_work_owned():
    from crm.models import Deliverable, DeliveryPlan, Task
    rows = [f'Deliverable "{d.title}" for {d.client}: nobody assigned' for d in Deliverable.objects.filter(
        status__in=['upcoming', 'in_progress', 'in_review', 'awaiting_client'], assignee__isnull=True, plan__status='active').select_related('client')[:20]]
    rows += [f'Deliverable "{d.title}" for {d.client}: assigned to disabled {d.assignee}' for d in Deliverable.objects.filter(
        status__in=['upcoming', 'in_progress', 'in_review', 'awaiting_client'], assignee__is_active=False).select_related('client', 'assignee')[:20]]
    rows += [f'Plan for {p.client}: account owner {p.account_owner} is disabled' for p in DeliveryPlan.objects.filter(
        status='active', account_owner__is_active=False).select_related('client', 'account_owner')[:20]]
    rows += [f'Task "{t.title}": {"nobody assigned" if t.assignee_id is None else "assigned to disabled " + str(t.assignee)}'
             for t in Task.objects.exclude(status__in=['done', 'cancelled']).filter(Q(assignee__isnull=True) | Q(assignee__is_active=False))
             .select_related('assignee')[:20]]
    return _gap('work_owned', 'Every open deliverable and task has an active person', rows,
                'Every open deliverable and task is with someone who can do it.', '{n} deliverable/task/plan problem(s).')


def check_hr_links():
    from django.contrib.auth import get_user_model
    from crm.core import permissions
    from crm.modules.worktime.services import MCP_USERNAME
    qs = (get_user_model().objects.filter(is_active=True, crm_employee__isnull=True).exclude(username=MCP_USERNAME)
          .filter(crm_profile__roles__isnull=False).distinct())
    rows = [f'{u.get_full_name() or u.get_username()} has a login but no HR record' for u in qs if not permissions.is_super_admin(u)]
    return _gap('hr_links', 'Every staff login has an HR record', rows[:50], 'Every staff login is linked to an HR record.',
                '{n} login(s) have no HR record, so leave, shift and attendance are missing.')


def check_won_flow():
    from datetime import timedelta
    from django.utils import timezone
    from crm.models import Lead
    won = Lead.objects.filter(stage='won', archived_at__isnull=True)
    rows = [f'{l}: won without a customer record' for l in won.filter(client__isnull=True)[:20]]
    rows += [f'{l}: won with nobody credited' for l in won.filter(credited_to__isnull=True, owner__isnull=False)[:20]]
    stalled = won.filter(client__isnull=False, won_at__lt=timezone.now() - timedelta(days=14), client__contracts__isnull=True,
                         client__projects__isnull=True, client__invoices__isnull=True).select_related('client')
    rows += [f'{l}: won over 14 days ago, nothing started for {l.client}' for l in stalled[:20]]
    return _gap('won_flow', 'Every win became a customer with work started', rows, 'Every win has a customer and work under way.',
                '{n} won lead(s) need a customer, a credit or a start.')


def check_quiet_clients():
    from crm.modules.clients import health
    rows = [f'{c.display_name}: no contact for {d} days (owner {c.owner})' for c, d in health.quiet_clients()[:50]]
    return _gap('quiet_clients', 'No live customer has gone quiet', rows, 'Every live customer has been in touch within 30 days.',
                '{n} live customer(s) have had no contact for 30 days or more.')


OPERATIONAL = [check_leads_next_action, check_leads_owned, check_clients_owned, check_contract_coverage, check_work_owned, check_hr_links,
               check_won_flow, check_quiet_clients]

CHECKS = [check_journals, check_trial_balance, check_ar, check_ap, check_invoice_balances, check_payments, check_postings,
          check_numbering, check_audit_chain] + OPERATIONAL


def run_checks(actor=None) -> dict:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # a broken check is itself a failure, never silent
            logger.exception('Integrity check %s crashed', fn.__name__)
            results.append(_result(fn.__name__.replace('check_', ''), fn.__name__.replace('check_', '').replace('_', ' ').capitalize(),
                                   False, f'The check could not run: {exc}'))
    failed = [r for r in results if not r['ok'] and not r.get('warn')]
    gaps = [r for r in results if not r['ok'] and r.get('warn')]
    from crm.core import audit
    audit.log('integrity.checked', actor=actor, object_type='integrity', result='success' if not failed else 'failure',
              changes={'failed': [r['code'] for r in failed]})
    if failed:
        from django.urls import reverse
        from crm.core import notify
        notify.notify(notify.owners(), f'{len(failed)} integrity check(s) failed',
                      body='; '.join(r['label'] for r in failed)[:480], url=reverse('crm:job_list') + '#integrity',
                      event_type='integrity', level='critical', email=True, dedupe_key=f'integrity:{",".join(r["code"] for r in failed)}')
    if gaps:
        from django.urls import reverse
        from django.utils import timezone
        from crm.core import notify
        notify.notify(notify.owners(), f'{len(gaps)} operational gap(s) need attention',
                      body='; '.join(r['label'] for r in gaps)[:480], url=reverse('crm:job_list') + '#integrity',
                      event_type='integrity', level='warning', dedupe_key=f'opsgaps:{timezone.localdate():%Y%m%d}')
    return {'checks': results, 'passed': len(results) - len(failed) - len(gaps), 'failed': len(failed), 'gaps': len(gaps)}


def nightly():
    """Job entry point: a failed check marks the run failed so it shows in the failed-job queue."""
    result = run_checks()
    if result['failed']:
        raise IntegrityFailed('; '.join(f"{r['label']}: {r['detail']}" for r in result['checks'] if not r['ok']), result)
    return result
