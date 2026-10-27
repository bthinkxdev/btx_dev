"""Dashboards (Part 13.1).

Every card is a link. A card that counts records is computed by running the very list
page it links to (same view class, same query string, same user), so the number on the
card always equals the total on the list. Cards the user has no permission for are left
out, and every list is already limited to the user's data scope.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Count, F, Q, Sum
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from crm.core import india, permissions
from crm.core.money import ZERO, q2

PRESETS = [('today', 'Today'), ('week', 'This week'), ('month', 'This month'), ('last_month', 'Last month'),
           ('quarter', 'This quarter'), ('fy', 'This financial year')]


@dataclass
class Card:
    key: str
    label: str
    value: object
    href: str = ''
    kind: str = 'count'          # count | money | text | percent
    icon: str = 'chart'
    note: str = ''               # e.g. the date range the card covers
    tone: str = ''               # '' | warn | bad | good
    sub: str = ''                # secondary line, e.g. "12 invoices"
    extra: dict = field(default_factory=dict)


@dataclass
class Range:
    start: date
    end: date
    preset: str

    @property
    def label(self) -> str:
        if self.preset == 'today':
            return 'Today'
        if self.start == self.end:
            return f'{self.start:%d %b %Y}'
        return f'{self.start:%d %b} – {self.end:%d %b %Y}'

    def params(self, on: str | None = None) -> dict:
        p = {'from': self.start.isoformat(), 'to': self.end.isoformat()}
        if on:
            p['on'] = on
        return p


def date_range(request, default: str = 'month') -> Range:
    today = india.today()
    preset = request.GET.get('preset', '') or ('' if request.GET.get('from') else default)
    if preset == 'today':
        return Range(today, today, preset)
    if preset == 'week':
        return Range(today - timedelta(days=today.weekday()), today, preset)
    if preset == 'last_month':
        end = india.month_start(today) - timedelta(days=1)
        return Range(india.month_start(end), end, preset)
    if preset == 'quarter':
        q0 = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        return Range(q0, today, preset)
    if preset == 'fy':
        return Range(india.fy_bounds(today)[0], today, preset)
    if preset == 'month':
        return Range(india.month_start(today), today, preset)
    try:
        start = date.fromisoformat(request.GET.get('from', ''))
        end = date.fromisoformat(request.GET.get('to', '')) if request.GET.get('to') else today
    except ValueError:
        return Range(india.month_start(today), today, 'month')
    if end < start:
        start, end = end, start
    if (end - start).days > 3 * 366:
        start = end - timedelta(days=3 * 366)
    return Range(start, end, 'custom')


# ---------------------------------------------------------------- list-backed cards

def run_list(request, view_cls, params: dict):
    """The filtered queryset the list page would show for these params, or None without permission."""
    user = request.user
    if not permissions.has_perm(user, view_cls.module, view_cls.action):
        return None
    fake = copy.copy(request)
    fake.GET = QueryDict(urlencode(params, doseq=True))
    view = view_cls()
    view.setup(fake)
    return view.get_filtered()


def list_card(request, key, label, view_cls, url_name, params: dict, *, agg=None, kind=None, icon='chart',
              note='', tone_if_any='', sub_count: str = '') -> Card | None:
    qs = run_list(request, view_cls, params)
    if qs is None:
        return None
    count = qs.count()
    if agg is not None:
        value = qs.order_by().aggregate(v=Sum(agg))['v'] or ZERO
        kind = kind or 'money'
    else:
        value = count
        kind = kind or 'count'
    href = reverse(url_name) + ('?' + urlencode(params) if params else '')
    return Card(key, label, value, href=href, kind=kind, icon=icon, note=note,
                tone=tone_if_any if count else '', sub=(f'{count} {sub_count}' if sub_count else ''))


def _views():
    from crm.modules.billing.views import InvoiceList
    from crm.modules.contracts.views import ContractList
    from crm.modules.expenses.views import ExpenseList
    from crm.modules.leads.views import LeadList
    from crm.modules.payments.views import GatewayEventList, LinkList, PaymentList
    from crm.modules.projects.views import ProjectList, TaskList, TicketList
    from crm.modules.quotations.views import QuotationList
    return {'invoice': InvoiceList, 'contract': ContractList, 'expense': ExpenseList, 'lead': LeadList,
            'event': GatewayEventList, 'link': LinkList, 'payment': PaymentList, 'project': ProjectList,
            'task': TaskList, 'ticket': TicketList, 'quotation': QuotationList}


def _compact(cards):
    return [c for c in cards if c is not None]


# ---------------------------------------------------------------- shared figures

def pending_approvals_for(user, overdue_hours: int | None = None):
    from crm.core import approvals
    from crm.models import ApprovalRequest
    qs = ApprovalRequest.objects.filter(status='pending').exclude(requested_by=user)
    if overdue_hours is not None:
        qs = qs.filter(requested_at__lt=timezone.now() - timedelta(hours=overdue_hours))
    return [r for r in qs.order_by('requested_at') if approvals.can_decide(user, r)[0]]


def cash_position():
    from crm.core import posting
    from crm.models import BankAccount
    return sum((posting.balance(b.ledger_account) for b in BankAccount.objects.filter(is_active=True).select_related('ledger_account')), ZERO)


def monthly_series(months: int = 6) -> dict:
    """Invoiced (pre-tax, billed) vs collected (cash received) per month; definitions never blur."""
    from crm.models import Invoice, Payment
    from crm.models.billing import INV_POSTED
    from crm.models.payments import P_COUNTED
    today = india.today()
    first = india.add_months(india.month_start(today), -(months - 1))
    labels, billed, collected = [], [], []
    for i in range(months):
        m = india.add_months(first, i)
        end = india.month_end(m)
        labels.append(f'{m:%b %y}')
        billed.append(float(Invoice.objects.filter(status__in=INV_POSTED, invoice_date__gte=m, invoice_date__lte=end)
                            .aggregate(s=Sum('taxable_total'))['s'] or 0))
        collected.append(float(Payment.objects.filter(status__in=P_COUNTED, payment_date__gte=m, payment_date__lte=end)
                               .aggregate(s=Sum('amount'))['s'] or 0))
    return {'labels': labels, 'series': [{'name': 'Invoiced (before tax)', 'data': billed},
                                         {'name': 'Collected', 'data': collected}]}


# ---------------------------------------------------------------- dashboards

def owner(request, rng: Range) -> dict:
    from crm.models import JobRun
    from crm.modules.accounting import reports
    v = _views()
    user = request.user
    today = india.today()
    cards = [
        list_card(request, 'collected_today', 'Collected today', v['payment'], 'crm:payment_list',
                  {'quick': 'collected', 'from': today.isoformat(), 'to': today.isoformat()}, agg='amount', icon='rupee',
                  note='Today', sub_count='payments'),
        list_card(request, 'collected', 'Collected', v['payment'], 'crm:payment_list', {'quick': 'collected', **rng.params()},
                  agg='amount', icon='rupee', note=rng.label, sub_count='payments'),
        list_card(request, 'outstanding', 'Outstanding receivables', v['invoice'], 'crm:invoice_list', {'quick': 'unpaid'},
                  agg='balance', icon='invoice', note='As of now', sub_count='invoices'),
    ]
    if permissions.has_perm(user, 'accounting', 'view'):
        pl = reports.profit_loss(rng.start, rng.end)
        pl_href = f"{reverse('crm:profit_loss')}?start={rng.start.isoformat()}&end={rng.end.isoformat()}"
        cards += [
            Card('revenue', 'Revenue (recognised)', pl['total_income'], pl_href, 'money', 'chart', rng.label),
            Card('expenses', 'Costs and expenses', pl['total_direct'] + pl['total_expense'], pl_href, 'money', 'receipt', rng.label),
            Card('gross_profit', 'Gross profit', pl['gross_profit'], pl_href, 'money', 'trophy', rng.label,
                 tone='bad' if pl['gross_profit'] < 0 else '', sub=f"{pl['gross_margin']}% margin" if pl['gross_margin'] is not None else ''),
            Card('operating_profit', 'Operating profit', pl['net_profit'], pl_href, 'money', 'trophy', rng.label,
                 tone='bad' if pl['net_profit'] < 0 else ''),
        ]
    if permissions.has_perm(user, 'banking', 'view'):
        cards.append(Card('cash', 'Cash and bank balance', cash_position(), reverse('crm:bank_account_list'), 'money', 'bank', 'Book balance now'))
    lead_qs = run_list(request, v['lead'], {'quick': 'open'})
    if lead_qs is not None:
        weighted = sum((q2(val * prob / Decimal(100)) for val, prob in lead_qs.values_list('value', 'probability')), ZERO)
        cards.append(Card('forecast', 'Weighted pipeline', weighted, reverse('crm:lead_list') + '?quick=open', 'money', 'funnel',
                          'Open leads, value × probability', sub=f'{lead_qs.count()} open leads'))
    if permissions.get_scope(user, 'contracts', 'view') == 'company':
        from crm.modules.contracts.services import mrr
        cards.append(Card('mrr', 'Monthly recurring revenue', mrr(today), reverse('crm:contract_list') + '?quick=active', 'money',
                          'refresh', 'Active contracts today'))
    cards += [
        list_card(request, 'renewals', 'Renewals due in 60 days', v['contract'], 'crm:contract_list', {'quick': 'renewal'},
                  icon='calendar', note='Next 60 days', tone_if_any='warn'),
    ]
    overdue = pending_approvals_for(user, overdue_hours=24)
    cards.append(Card('approvals', 'Approvals waiting over 24 h', len(overdue), reverse('crm:approval_list') + '?overdue=1',
                      'count', 'shield', 'Waiting on you', tone='warn' if overdue else ''))
    if permissions.has_perm(user, 'automation', 'view'):
        failed = JobRun.objects.filter(status='failed').count()
        cards.append(Card('failed_jobs', 'Failed jobs', failed, reverse('crm:job_list') + '?status=failed', 'count', 'alert',
                          'Not yet resolved', tone='bad' if failed else ''))
    cards += [
        list_card(request, 'leads_in', 'Leads received', v['lead'], 'crm:lead_list', rng.params('created'), icon='users', note=rng.label),
        list_card(request, 'unworked', 'Leads not yet contacted', v['lead'], 'crm:lead_list', {'quick': 'unworked'}, icon='phone',
                  note='Open, no first contact', tone_if_any='warn'),
    ]
    charts = {}
    if permissions.has_perm(user, 'invoices', 'view') and permissions.get_scope(user, 'invoices', 'view') == 'company':
        charts['trend'] = monthly_series()
    return {'cards': _compact(cards), 'charts': charts}


def sales(request, rng: Range) -> dict:
    from crm.models import Lead, Payment, SalesTarget
    from crm.models.leads import STAGES
    STAGE_LABELS = dict(STAGES)
    from crm.models.payments import P_COUNTED
    v = _views()
    user = request.user
    cards = _compact([
        list_card(request, 'leads_in', 'Leads in', v['lead'], 'crm:lead_list', rng.params('created'), icon='users', note=rng.label),
        list_card(request, 'assigned', 'Leads assigned', v['lead'], 'crm:lead_list', rng.params('assigned'), icon='user', note=rng.label),
        list_card(request, 'sla', 'First-response SLA breaches', v['lead'], 'crm:lead_list', {'quick': 'sla_breached', **rng.params('created')},
                  icon='clock', note=f'Leads created {rng.label}', tone_if_any='bad'),
        list_card(request, 'overdue', 'Overdue follow-ups', v['lead'], 'crm:lead_list', {'quick': 'overdue'}, icon='alert',
                  note='As of now', tone_if_any='bad'),
        list_card(request, 'stale', 'Stale leads (no activity 3 days)', v['lead'], 'crm:lead_list', {'quick': 'no_activity'},
                  icon='clock2', note='As of now', tone_if_any='warn'),
        list_card(request, 'quotes_sent', 'Quotations sent', v['quotation'], 'crm:quotation_list', rng.params('sent'),
                  agg='grand_total', icon='document', note=rng.label, sub_count='quotations'),
        list_card(request, 'quotes_viewed', 'Quotations first viewed', v['quotation'], 'crm:quotation_list', rng.params('viewed'),
                  icon='eye', note=rng.label),
        list_card(request, 'links_opened', 'Payment links opened', v['link'], 'crm:link_list', rng.params('opened'), icon='link', note=rng.label),
        list_card(request, 'links_paid', 'Payment links paid', v['link'], 'crm:link_list', rng.params('paid'), agg='amount_paid',
                  icon='rupee', note=rng.label, sub_count='links'),
        list_card(request, 'won', 'Won', v['lead'], 'crm:lead_list', rng.params('won'), agg='won_value', icon='trophy',
                  note=rng.label, sub_count='deals'),
    ])
    charts = {}
    leads_qs = run_list(request, v['lead'], rng.params('created'))
    if leads_qs is not None:
        order = ['new', 'contacted', 'qualified', 'quotation_prepared', 'quotation_sent', 'negotiation', 'won']
        total = leads_qs.count()
        funnel, prev = [], None
        for s in order:
            n = total if s == 'new' else leads_qs.filter(Q(stage_history__to_stage=s) | Q(stage=s)).distinct().count()
            funnel.append({'label': STAGE_LABELS[s], 'count': n, 'conv': round(n * 100 / prev, 1) if prev else None,
                           'width': int(n * 100 / total) if total else 0})
            prev = n or prev
        charts['funnel'] = funnel
    # Target gap for this month, in the user's deal scope (Part 5.3).
    if permissions.has_perm(user, 'deals', 'view'):
        month = india.month_start(india.today())
        scope = permissions.get_scope(user, 'deals', 'view')
        targets = SalesTarget.objects.filter(month=month, user__isnull=False)
        if scope == 'own':
            targets = targets.filter(user=user)
        elif scope in ('team', 'department'):
            targets = targets.filter(user_id__in=permissions.team_member_ids(user))
        ids = list(targets.values_list('user_id', flat=True))
        target_total = targets.aggregate(s=Sum('collected_value'))['s'] or ZERO
        collected = Payment.objects.filter(status__in=P_COUNTED, client__owner_id__in=ids, payment_date__gte=month,
                                           payment_date__lte=india.today()).aggregate(s=Sum('amount'))['s'] or ZERO if ids else ZERO
        cards.append(Card('target_gap', 'Collection target gap', max(target_total - collected, ZERO), reverse('crm:targets'),
                          'money', 'target', f'{month:%B %Y}', sub=f'{collected / target_total * 100:.0f}% of target' if target_total else 'No targets set'))
    by_owner = []
    if leads_qs is not None and permissions.get_scope(user, 'leads', 'view') != 'own':
        by_owner = list(leads_qs.values('owner__first_name', 'owner__last_name', 'owner__username')
                        .annotate(n=Count('id'), won=Count('id', filter=Q(stage='won'))).order_by('-n')[:10])
    return {'cards': cards, 'charts': charts, 'by_owner': by_owner,
            'unassigned': Lead.objects.filter(owner__isnull=True, stage='new', archived_at__isnull=True).count()
            if permissions.has_perm(user, 'leads', 'assign') else None}


def finance(request, rng: Range) -> dict:
    from crm.core import posting
    from crm.models import BankTransaction
    v = _views()
    user = request.user
    cards = _compact([
        list_card(request, 'outstanding', 'Receivables outstanding', v['invoice'], 'crm:invoice_list', {'quick': 'unpaid'},
                  agg='balance', icon='invoice', note='As of now', sub_count='invoices'),
        list_card(request, 'overdue', 'Overdue receivables', v['invoice'], 'crm:invoice_list', {'quick': 'overdue'},
                  agg='balance', icon='alert', note='Past due date', sub_count='invoices'),
        list_card(request, 'unallocated', 'Unallocated payments', v['payment'], 'crm:payment_list', {'quick': 'unallocated'},
                  icon='swap', note='Verified, not fully allocated', tone_if_any='warn'),
        list_card(request, 'verify', 'Manual payments to verify', v['payment'], 'crm:payment_list', {'quick': 'pending'},
                  agg='amount', icon='check', note='Waiting for a checker', sub_count='payments'),
        list_card(request, 'webhooks', 'Failed gateway events', v['event'], 'crm:gateway_events', {'quick': 'failed'},
                  icon='alert', note='Need reprocessing', tone_if_any='bad'),
        list_card(request, 'payables', 'Payables due', v['expense'], 'crm:expense_list', {'quick': 'to_pay'},
                  agg=F('payable_amount') - F('paid_amount'), icon='receipt', note='Approved, unpaid', sub_count='bills'),
        list_card(request, 'payables_overdue', 'Overdue bills', v['expense'], 'crm:expense_list', {'quick': 'overdue'},
                  icon='alert', note='Past due date', tone_if_any='bad'),
        list_card(request, 'collected', 'Collected', v['payment'], 'crm:payment_list', {'quick': 'collected', **rng.params()},
                  agg='amount', icon='rupee', note=rng.label, sub_count='payments'),
    ])
    if permissions.has_perm(user, 'banking', 'view'):
        n = BankTransaction.objects.filter(status__in=['unmatched', 'suggested']).count()
        cards.append(Card('unreconciled', 'Unreconciled bank lines', n, reverse('crm:reconciliation'), 'count', 'bank',
                          'All bank accounts', tone='warn' if n else ''))
    if permissions.has_perm(user, 'accounting', 'view'):
        today = india.today()
        out = sum((posting.balance(posting.account(c), as_of=today) for c in (posting.OUTPUT_CGST, posting.OUTPUT_SGST, posting.OUTPUT_IGST)), ZERO)
        inp = sum((posting.balance(posting.account(c), as_of=today) for c in (posting.INPUT_CGST, posting.INPUT_SGST, posting.INPUT_IGST)), ZERO)
        cards += [
            Card('gst', 'GST payable (net of ITC)', out - inp, reverse('crm:tax_reports'), 'money', 'document', 'Ledger balance now',
                 sub=f'Output {out:,.0f} · ITC {inp:,.0f}'),
            Card('tds_rec', 'TDS receivable', posting.balance(posting.account(posting.TDS_RECEIVABLE)), reverse('crm:tds_receivable'),
                 'money', 'receipt', 'Deducted by clients'),
            Card('tds_pay', 'TDS payable', posting.balance(posting.account(posting.TDS_PAYABLE)), reverse('crm:tds_payable'),
                 'money', 'receipt', 'Deducted from vendors'),
        ]
    ageing_rows = None
    unpaid = run_list(request, v['invoice'], {'quick': 'unpaid'})
    if unpaid is not None:
        from crm.modules.billing.views import BUCKETS
        buckets, over_n, over_v = [], 0, ZERO
        for k in BUCKETS:
            qs = run_list(request, v['invoice'], {'quick': 'unpaid', 'age': k})
            n, amt = qs.count(), qs.order_by().aggregate(s=Sum('balance'))['s'] or ZERO
            buckets.append({'key': k, 'label': f'{k} days overdue', 'count': n, 'amount': amt,
                            'href': reverse('crm:invoice_list') + f'?quick=unpaid&age={k}'})
            over_n, over_v = over_n + n, over_v + amt
        total_n, total_v = unpaid.count(), unpaid.order_by().aggregate(s=Sum('balance'))['s'] or ZERO
        buckets.insert(0, {'key': '', 'label': 'Not yet due', 'count': total_n - over_n, 'amount': total_v - over_v,
                           'href': reverse('crm:invoice_list') + '?quick=unpaid'})
        ageing_rows = {'buckets': buckets, 'count': total_n, 'total': total_v}
    return {'cards': cards, 'charts': {}, 'ageing': ageing_rows}


def delivery(request, rng: Range) -> dict:
    from crm.models import Blocker, Milestone, Project, TimeEntry
    v = _views()
    user = request.user
    today = india.today()
    week_end = today + timedelta(days=6 - today.weekday())
    cards = _compact([
        list_card(request, 'active', 'Active projects', v['project'], 'crm:project_list', {'quick': 'active'}, icon='folder', note='Now'),
        list_card(request, 'at_risk', 'Projects at risk', v['project'], 'crm:project_list', {'quick': 'at_risk'}, icon='alert',
                  note='Late, overdue tasks or budget burn over 90%', tone_if_any='bad'),
        list_card(request, 'overdue_tasks', 'Overdue tasks', v['task'], 'crm:task_list', {'quick': 'overdue'}, icon='tasks',
                  note='As of now', tone_if_any='warn'),
        list_card(request, 'done_tasks', 'Tasks completed', v['task'], 'crm:task_list', rng.params('completed'), icon='check', note=rng.label),
        list_card(request, 'tickets_breached', 'Tickets past SLA', v['ticket'], 'crm:ticket_list', {'quick': 'breached'},
                  icon='ticket', note='As of now', tone_if_any='bad'),
    ])
    projects = run_list(request, v['project'], {})
    milestones, blockers, util = [], [], []
    if projects is not None:
        pids = list(projects.values_list('pk', flat=True))
        milestones = list(Milestone.objects.filter(project_id__in=pids, due_date__lte=week_end).exclude(status='accepted')
                          .select_related('project').order_by('due_date')[:20])
        blockers = list(Blocker.objects.filter(task__project_id__in=pids, resolved_at__isnull=True)
                        .select_related('task__project', 'task__assignee').order_by('created_at')[:20])
        cards.append(Card('milestones', 'Milestones due this week', len(milestones), reverse('crm:project_list') + '?quick=active',
                          'count', 'calendar', f'Up to {week_end:%d %b}', tone='warn' if any(m.due_date < today for m in milestones) else ''))
        cards.append(Card('blockers', 'Open blockers', Blocker.objects.filter(task__project_id__in=pids, resolved_at__isnull=True).count(),
                          reverse('crm:blockers'), 'count', 'alert', 'Unresolved'))
        # Utilisation: hours logged in range against 8 h per working day (Part 10.4).
        if permissions.has_perm(user, 'tasks', 'approve'):
            from crm.modules.hr.services import working_days
            days = max(working_days(rng.start, rng.end), 1)
            rows = (TimeEntry.objects.filter(project_id__in=pids, entry_date__gte=rng.start, entry_date__lte=rng.end)
                    .exclude(status='rejected').values('user_id', 'user__first_name', 'user__last_name', 'user__username')
                    .annotate(h=Sum('hours')).order_by('-h'))
            for r in rows[:15]:
                util.append({'name': f"{r['user__first_name']} {r['user__last_name']}".strip() or r['user__username'],
                             'hours': r['h'], 'pct': int(r['h'] * 100 / (days * 8))})
    return {'cards': cards, 'charts': {}, 'milestones': milestones, 'blockers': blockers, 'util': util, 'today': today}


def hr(request, rng: Range) -> dict:
    from crm.models import Attendance, Employee, LeaveRequest, PayrollRun
    user = request.user
    today = india.today()
    cards, on_leave, joiners, exits = [], [], [], []
    if permissions.has_perm(user, 'attendance', 'view') and permissions.get_scope(user, 'attendance', 'view') != 'own':
        active = Employee.objects.exclude(status='exited').filter(joining_date__lte=today)
        att = Attendance.objects.filter(att_date=today, employee__in=active)
        marked = att.count()
        present = att.filter(status__in=['present', 'half_day', 'wfh']).count()
        href = reverse('crm:attendance') + f'?date={today.isoformat()}'
        cards += [Card('present', 'Present today', present, href, 'count', 'check', f'{today:%d %b}', sub=f'of {active.count()} employees'),
                  Card('unmarked', 'Not marked yet', max(active.count() - marked, 0), href, 'count', 'clock', f'{today:%d %b}')]
    if permissions.has_perm(user, 'leave', 'approve'):
        pending = LeaveRequest.objects.filter(status='pending').count()
        cards.append(Card('leave_pending', 'Leave requests pending', pending, reverse('crm:hr_approvals'), 'count', 'leave',
                          'Waiting for a decision', tone='warn' if pending else ''))
    if permissions.has_perm(user, 'leave', 'view') and permissions.get_scope(user, 'leave', 'view') != 'own':
        on_leave = list(LeaveRequest.objects.filter(status='approved', start_date__lte=today, end_date__gte=today)
                        .select_related('employee', 'leave_type'))
        cards.append(Card('on_leave', 'On leave today', len(on_leave), reverse('crm:leave_list'), 'count', 'leave', f'{today:%d %b}'))
    if permissions.has_perm(user, 'employees', 'view'):
        joiners = list(Employee.objects.filter(status='onboarding', joining_date__lte=today + timedelta(days=30)).order_by('joining_date'))
        exits = list(Employee.objects.filter(status='on_notice').order_by('last_working_day'))
        cards += [Card('joiners', 'Joining in 30 days', len(joiners), reverse('crm:employee_list') + '?quick=onboarding', 'count', 'user', 'Onboarding'),
                  Card('exits', 'On notice', len(exits), reverse('crm:employee_list') + '?quick=notice', 'count', 'logout', 'Exits in progress')]
    run = None
    if permissions.has_perm(user, 'payroll', 'view') and permissions.can_see(user, 'salary'):
        run = PayrollRun.objects.exclude(status='reversed').order_by('-period').first()
        cards.append(Card('payroll', 'Latest payroll', run.get_status_display() if run else 'None yet',
                          reverse('crm:payroll_detail', args=[run.pk]) if run else reverse('crm:payroll_list'), 'text', 'payroll',
                          f'{run.period:%B %Y}' if run else ''))
    return {'cards': cards, 'charts': {}, 'on_leave': on_leave, 'joiners': joiners, 'exits': exits, 'run': run}


DASHBOARDS = {
    'owner': ('Owner dashboard', owner, ('reports', 'view')),
    'sales': ('Sales dashboard', sales, ('leads', 'view')),
    'finance': ('Finance dashboard', finance, ('invoices', 'view')),
    'delivery': ('Delivery dashboard', delivery, ('projects', 'view')),
    'hr': ('HR dashboard', hr, ('employees', 'view')),
}


# Dashboards that show company-wide figures: a narrower (own/team) scope may not open them.
COMPANY_ONLY = {'owner', 'finance', 'hr'}


def can_open(user, key: str) -> bool:
    _, _, (module, action) = DASHBOARDS[key]
    if key in COMPANY_ONLY:
        return permissions.has_scope(user, module, action, 'company')
    return permissions.has_perm(user, module, action)


def apply_prefs(user, key: str, cards: list[Card]) -> tuple[list[Card], list[Card]]:
    """User-configured order and hidden cards, within the set the role allows (Part 13.1)."""
    profile = getattr(user, 'crm_profile', None)
    prefs = (profile.dashboard_prefs or {}).get(key, {}) if profile else {}
    hidden = set(prefs.get('hidden', []))
    order = {k: i for i, k in enumerate(prefs.get('order', []))}
    ranked = sorted(enumerate(cards), key=lambda t: (order.get(t[1].key, 1000 + t[0])))
    shown = [c for _, c in ranked if c.key not in hidden]
    return shown, [c for _, c in ranked]
