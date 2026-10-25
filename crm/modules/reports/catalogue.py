"""Report catalogue (Part 13.2).

Each report is a function (user, params) -> Table. Every query is limited twice: to the
user's scope on the underlying module (the same rule the list pages use) and to their
scope on Reports. Scheduled reports run through the same functions as their owner, so a
manager's emailed report holds only the manager's team.

Definitions that must never blur: revenue (recognised in the ledger), invoicing (billed),
collections (cash received), profit (revenue minus costs).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.urls import reverse

from crm.core import india, permissions
from crm.core.money import ZERO, q2


@dataclass
class Table:
    columns: list                      # [(label, kind)] kind: text | count | money | percent | number | hours
    rows: list = field(default_factory=list)       # [{'cells': [...], 'href': str, 'key': str}]
    totals: list | None = None
    notes: list = field(default_factory=list)
    chart: dict | None = None


@dataclass
class Params:
    start: date
    end: date
    filters: dict
    compare: bool = False


def _scoped(qs, user, module, owner_fields=None):
    """Narrower of the module scope and the Reports scope."""
    qs = permissions.scoped(qs, user, module, 'view', owner_fields)
    return permissions.scoped(qs, user, 'reports', 'view', owner_fields or permissions.OWNER_FIELDS.get(module, ('owner',)))


def _uname(first, last, username):
    return f'{first or ""} {last or ""}'.strip() or username or 'Unassigned'


def _pct(a, b):
    return q2(Decimal(a) * 100 / Decimal(b)) if b else None


def _users(user):
    User = get_user_model()
    return [(u.pk, u.get_full_name() or u.username) for u in User.objects.filter(is_active=True, crm_profile__isnull=False).order_by('first_name')]


def _sources(user):
    from crm.models import LeadSource
    return list(LeadSource.objects.values_list('pk', 'name'))


def _services(user):
    from crm.models import Service
    return list(Service.objects.filter(is_active=True).values_list('pk', 'name'))


def _teams(user):
    from crm.models import Team
    return list(Team.objects.filter(is_active=True).values_list('pk', 'name'))


def _departments(user):
    from crm.models import Department
    return list(Department.objects.values_list('pk', 'name'))


def _categories(user):
    from crm.models import ExpenseCategory
    return list(ExpenseCategory.objects.values_list('pk', 'name'))


def _projects(user):
    from crm.models import Project
    if not permissions.has_perm(user, 'projects', 'view'):
        return []
    return [(p.pk, str(p)) for p in _scoped(Project.objects.all(), user, 'projects')[:300]]


FILTERS = {
    'owner': ('Owner', _users), 'source': ('Source', _sources), 'service': ('Service', _services), 'team': ('Team', _teams),
    'department': ('Department', _departments), 'category': ('Category', _categories), 'project': ('Project', _projects),
}


def _int(params, key):
    v = params.filters.get(key, '')
    return int(v) if str(v).isdigit() else None


def _lead_qs(user, p: Params):
    from crm.models import Lead
    qs = _scoped(Lead.objects.filter(archived_at__isnull=True, merged_into__isnull=True), user, 'leads')
    if _int(p, 'owner'):
        qs = qs.filter(owner_id=_int(p, 'owner'))
    if _int(p, 'source'):
        qs = qs.filter(source_id=_int(p, 'source'))
    if _int(p, 'team'):
        qs = qs.filter(team_id=_int(p, 'team'))
    if _int(p, 'service'):
        qs = qs.filter(services__id=_int(p, 'service')).distinct()
    return qs


def _lead_href(p: Params, **extra):
    from urllib.parse import urlencode
    q = {'from': p.start.isoformat(), 'to': p.end.isoformat(), 'on': 'created'}
    if _int(p, 'owner'):
        q['owner'] = _int(p, 'owner')
    if _int(p, 'source'):
        q['source'] = _int(p, 'source')
    q.update(extra)
    return reverse('crm:lead_list') + '?' + urlencode(q)


# ---------------------------------------------------------------- sales

FUNNEL_ORDER = ['new', 'contacted', 'qualified', 'quotation_prepared', 'quotation_sent', 'negotiation', 'won']


def funnel(user, p: Params) -> Table:
    from crm.models import StageHistory
    from crm.models.leads import STAGES
    labels = dict(STAGES)
    cohort = _lead_qs(user, p).filter(created_at__date__gte=p.start, created_at__date__lte=p.end)
    total = cohort.count()
    t = Table([('Stage', 'text'), ('Leads reached', 'count'), ('Conversion from previous', 'percent'),
               ('Conversion from new', 'percent'), ('Average days in previous stage', 'number')])
    prev = None
    ids = list(cohort.values_list('pk', flat=True))
    for s in FUNNEL_ORDER:
        n = total if s == 'new' else cohort.filter(Q(stage_history__to_stage=s) | Q(stage=s)).distinct().count()
        avg = StageHistory.objects.filter(lead_id__in=ids, to_stage=s).aggregate(a=Avg('seconds_in_previous'))['a'] if s != 'new' else None
        t.rows.append({'key': s, 'cells': [labels[s], n, _pct(n, prev) if prev else None, _pct(n, total),
                                           round(avg / 86400, 1) if avg else None],
                       'href': _lead_href(p, stage=s) if s != 'new' else _lead_href(p)})
        prev = n or prev
    lost = cohort.filter(stage='lost').count()
    t.notes.append(f'Cohort: leads created {p.start:%d %b %Y} to {p.end:%d %b %Y}. {lost} of {total} were lost.')
    t.chart = {'type': 'bar', 'labels': [r['cells'][0] for r in t.rows], 'series': [{'name': 'Leads', 'data': [r['cells'][1] for r in t.rows]}]}
    return t


def sources(user, p: Params) -> Table:
    from crm.models import Campaign, LeadSource
    t = Table([('Source', 'text'), ('Leads', 'count'), ('Qualified', 'count'), ('Won', 'count'), ('Won value', 'money'),
               ('Campaign spend', 'money'), ('Cost per lead', 'money'), ('Won value per lead', 'money')])
    base = _lead_qs(user, p).filter(created_at__date__gte=p.start, created_at__date__lte=p.end)
    tot = [0, 0, 0, ZERO, ZERO]
    for src in LeadSource.objects.order_by('name'):
        qs = base.filter(source=src)
        n = qs.count()
        spend = sum((c.budget for c in Campaign.objects.filter(source=src).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=p.end)).filter(Q(end_date__isnull=True) | Q(end_date__gte=p.start))), ZERO)
        if not n and not spend:
            continue
        qual = qs.filter(Q(stage_history__to_stage='qualified') | Q(stage__in=['qualified', 'quotation_prepared', 'quotation_sent',
                                                                                  'negotiation', 'won'])).distinct().count()
        won = qs.filter(stage='won')
        won_n = won.count()
        won_v = won.aggregate(s=Sum('won_value'))['s'] or ZERO
        t.rows.append({'key': str(src.pk), 'cells': [src.name, n, qual, won_n, won_v, spend, q2(spend / n) if n else None,
                                                     q2(won_v / n) if n else None], 'href': _lead_href(p, source=src.pk)})
        tot = [tot[0] + n, tot[1] + qual, tot[2] + won_n, tot[3] + won_v, tot[4] + spend]
    t.totals = ['Total', tot[0], tot[1], tot[2], tot[3], tot[4], q2(tot[4] / tot[0]) if tot[0] else None,
                q2(tot[3] / tot[0]) if tot[0] else None]
    t.notes.append('Campaign spend counts every campaign of the source that ran during the period.')
    return t


def response(user, p: Params) -> Table:
    from crm.models import CompanySettings
    sla = CompanySettings.get().first_response_minutes
    qs = _lead_qs(user, p).filter(created_at__date__gte=p.start, created_at__date__lte=p.end)
    t = Table([('Owner', 'text'), ('Leads', 'count'), ('Contacted', 'count'), ('Average first response (min)', 'number'),
               (f'Breached {sla}-min SLA', 'count'), ('Breach rate', 'percent'), ('Average time unassigned (min)', 'number')])
    rows = qs.values('owner_id', 'owner__first_name', 'owner__last_name', 'owner__username').annotate(
        n=Count('id'), contacted=Count('id', filter=Q(first_contacted_at__isnull=False)),
        frt=Avg('first_response_seconds'),
        breached=Count('id', filter=Q(first_response_seconds__gt=sla * 60) | Q(first_contacted_at__isnull=True))).order_by('-n')
    for r in rows:
        mine = qs.filter(owner_id=r['owner_id'], assigned_at__isnull=False)
        waits = [(a - c).total_seconds() for a, c in mine.values_list('assigned_at', 'created_at') if a and c and a >= c]
        t.rows.append({'key': str(r['owner_id']), 'cells': [
            _uname(r['owner__first_name'], r['owner__last_name'], r['owner__username']), r['n'], r['contacted'],
            round(r['frt'] / 60, 1) if r['frt'] else None, r['breached'], _pct(r['breached'], r['n']),
            round(sum(waits) / len(waits) / 60, 1) if waits else None],
            'href': _lead_href(p, owner=r['owner_id'] or 'none', quick='sla_breached')})
    t.notes.append('A breach is a first response slower than the SLA, or a lead never contacted.')
    return t


def forecast(user, p: Params) -> Table:
    from crm.models.leads import OPEN_STAGES
    qs = _lead_qs(user, p).filter(stage__in=OPEN_STAGES)
    t = Table([('Expected close month', 'text'), ('Open leads', 'count'), ('Value', 'money'), ('Weighted value', 'money')])
    buckets: dict = {}
    for close, value, prob in qs.values_list('expected_close_date', 'value', 'probability'):
        key = close.replace(day=1) if close else None
        b = buckets.setdefault(key, [0, ZERO, ZERO])
        b[0] += 1
        b[1] += value
        b[2] += q2(value * prob / Decimal(100))
    tot = [0, ZERO, ZERO]
    for key in sorted(buckets, key=lambda k: (k is None, k or date.min)):
        n, v, w = buckets[key]
        t.rows.append({'key': key.isoformat() if key else 'none', 'cells': [f'{key:%B %Y}' if key else 'No date set', n, v, w],
                       'href': reverse('crm:pipeline')})
        tot = [tot[0] + n, tot[1] + v, tot[2] + w]
    t.totals = ['Total', *tot]
    t.notes.append('Weighted value is deal value (before tax) times stage probability. Overdue close dates stay in their month.')
    return t


def quotations(user, p: Params) -> Table:
    from crm.models import Quotation
    from crm.models.quotations import REJECTION_REASONS
    qs = _scoped(Quotation.objects.filter(sent_at__date__gte=p.start, sent_at__date__lte=p.end), user, 'quotations')
    if _int(p, 'owner'):
        qs = qs.filter(owner_id=_int(p, 'owner'))
    if _int(p, 'service'):
        qs = qs.filter(lines__service_id=_int(p, 'service')).distinct()
    won_st = ['accepted', 'converted']
    lost_st = ['rejected', 'expired', 'void']
    t = Table([('Owner', 'text'), ('Sent', 'count'), ('Won', 'count'), ('Lost or expired', 'count'), ('Open', 'count'),
               ('Win rate (count)', 'percent'), ('Sent value', 'money'), ('Won value', 'money'), ('Win rate (value)', 'percent'),
               ('Average days to decision', 'number')])
    for r in qs.values('owner_id', 'owner__first_name', 'owner__last_name', 'owner__username').annotate(
            n=Count('id'), won=Count('id', filter=Q(status__in=won_st)), lost=Count('id', filter=Q(status__in=lost_st)),
            v=Sum('taxable_total'), wv=Sum('taxable_total', filter=Q(status__in=won_st))).order_by('-n'):
        decided = qs.filter(owner_id=r['owner_id'], accepted_at__isnull=False)
        days = [(a - s).days for a, s in decided.values_list('accepted_at', 'sent_at') if a and s]
        decided_n = r['won'] + r['lost']
        t.rows.append({'key': str(r['owner_id']), 'cells': [
            _uname(r['owner__first_name'], r['owner__last_name'], r['owner__username']), r['n'], r['won'], r['lost'],
            r['n'] - decided_n, _pct(r['won'], decided_n), r['v'] or ZERO, r['wv'] or ZERO, _pct(r['wv'] or 0, r['v'] or 0),
            round(sum(days) / len(days), 1) if days else None],
            'href': reverse('crm:quotation_list') + f'?on=sent&from={p.start.isoformat()}&to={p.end.isoformat()}'})
    reasons = dict(REJECTION_REASONS)
    for r in qs.filter(status='rejected').values('rejection_reason').annotate(n=Count('id')).order_by('-n'):
        t.notes.append(f"Rejected — {reasons.get(r['rejection_reason'], 'Not given')}: {r['n']}")
    t.notes.insert(0, 'Values are before tax. Win rate counts decided quotations only (won, rejected, expired or void).')
    return t


def collections(user, p: Params) -> Table:
    from crm.models import PaymentLink
    qs = _scoped(PaymentLink.objects.filter(created_at__date__gte=p.start, created_at__date__lte=p.end).exclude(status='draft'),
                 user, 'payment_links')
    if _int(p, 'owner'):
        qs = qs.filter(owner_id=_int(p, 'owner'))
    t = Table([('Created by', 'text'), ('Links', 'count'), ('Opened', 'count'), ('Paid', 'count'), ('With failed attempt', 'count'),
               ('Expired', 'count'), ('Conversion', 'percent'), ('Amount asked', 'money'), ('Collected', 'money'),
               ('Average hours to pay', 'number')])
    for r in qs.values('owner_id', 'owner__first_name', 'owner__last_name', 'owner__username').annotate(
            n=Count('id', distinct=True), opened=Count('id', filter=Q(open_count__gt=0), distinct=True),
            paid=Count('id', filter=Q(paid_at__isnull=False), distinct=True),
            failed=Count('id', filter=Q(attempts__status='failed'), distinct=True),
            expired=Count('id', filter=Q(status='expired'), distinct=True)).order_by('-n'):
        mine = qs.filter(owner_id=r['owner_id'])
        asked = mine.aggregate(s=Sum('amount'))['s'] or ZERO
        got = mine.aggregate(s=Sum('amount_paid'))['s'] or ZERO
        hours = [(pd - c).total_seconds() / 3600 for pd, c in mine.filter(paid_at__isnull=False).values_list('paid_at', 'created_at')]
        t.rows.append({'key': str(r['owner_id']), 'cells': [
            _uname(r['owner__first_name'], r['owner__last_name'], r['owner__username']), r['n'], r['opened'], r['paid'], r['failed'],
            r['expired'], _pct(r['paid'], r['n']), asked, got, round(sum(hours) / len(hours), 1) if hours else None],
            'href': reverse('crm:link_list') + f'?from={p.start.isoformat()}&to={p.end.isoformat()}'})
    return t


# ---------------------------------------------------------------- delivery, people, spend

def delivery(user, p: Params) -> Table:
    from crm.models import Project
    qs = _scoped(Project.objects.exclude(status='cancelled'), user, 'projects').filter(start_date__lte=p.end).filter(
        Q(closed_at__isnull=True) | Q(closed_at__date__gte=p.start))
    if _int(p, 'project'):
        qs = qs.filter(pk=_int(p, 'project'))
    t = Table([('Project', 'text'), ('Status', 'text'), ('Budget hours', 'hours'), ('Hours logged', 'hours'), ('Budget burn', 'percent'),
               ('Hours in period', 'hours'), ('Due', 'text'), ('On time', 'text'), ('Overdue tasks', 'count')])
    today = india.today()
    for pr in qs.distinct().order_by('due_date'):
        logged = pr.hours_logged
        in_period = pr.time_entries.exclude(status='rejected').filter(entry_date__gte=p.start, entry_date__lte=p.end) \
            .aggregate(s=Sum('hours'))['s'] or Decimal('0')
        if pr.status == 'completed' and pr.closed_at:
            on_time = 'Yes' if pr.closed_at.date() <= pr.due_date else 'No'
        else:
            on_time = 'Late' if pr.due_date < today else 'On track'
        overdue = pr.tasks.exclude(status__in=['done', 'cancelled']).filter(due_date__lt=today).count()
        t.rows.append({'key': str(pr.pk), 'cells': [str(pr), pr.get_status_display(), pr.budget_hours, logged,
                                                    _pct(logged, pr.budget_hours), in_period, f'{pr.due_date:%d-%b-%Y}', on_time, overdue],
                       'href': reverse('crm:project_detail', args=[pr.pk])})
    done = [r for r in t.rows if r['cells'][7] in ('Yes', 'No')]
    if done:
        t.notes.append(f"On-time rate for completed projects: {_pct(sum(1 for r in done if r['cells'][7] == 'Yes'), len(done))}%.")
    return t


def payroll_cost(user, p: Params) -> Table:
    from crm.models import Payslip
    if not permissions.can_see(user, 'salary'):
        raise permissions.PermissionDenied('Salary figures need the salary field group.')
    qs = _scoped(Payslip.objects.filter(run__period__gte=india.month_start(p.start), run__period__lte=p.end)
                 .exclude(run__status__in=['reversed', 'draft']), user, 'payroll')
    if _int(p, 'department'):
        qs = qs.filter(employee__department_id=_int(p, 'department'))
    months = sorted(set(qs.values_list('run__period', flat=True)))
    t = Table([('Department', 'text')] + [(f'{m:%b %Y}', 'money') for m in months] + [('Total cost', 'money'), ('Headcount (latest)', 'count')])
    data: dict = {}
    for dept, period, gross, employer, comm, reimb, adj in qs.values_list(
            'employee__department__name', 'run__period', 'gross', 'employer_contribution', 'commissions', 'reimbursements', 'adjustments'):
        d = data.setdefault(dept or 'No department', {})
        d[period] = d.get(period, ZERO) + gross + employer + comm + adj
    col_tot = {m: ZERO for m in months}
    for dept in sorted(data):
        vals = [data[dept].get(m, ZERO) for m in months]
        for m, v in zip(months, vals):
            col_tot[m] += v
        head = qs.filter(run__period=months[-1], employee__department__name=dept if dept != 'No department' else None).count() if months else 0
        t.rows.append({'key': dept, 'cells': [dept, *vals, sum(vals, ZERO), head], 'href': reverse('crm:payroll_list')})
    t.totals = ['Total', *[col_tot[m] for m in months], sum(col_tot.values(), ZERO), qs.filter(run__period=months[-1]).count() if months else 0]
    t.notes.append('Cost = gross pay + employer contributions + commissions + adjustments. Reimbursements are excluded (they are expenses).')
    return t


def expenses(user, p: Params) -> Table:
    from crm.models import Expense, ExpenseCategory
    from crm.modules.accounting.reports import previous_period
    live = ['approved', 'partially_paid', 'paid']

    def window(s, e):
        qs = _scoped(Expense.objects.filter(status__in=live, bill_date__gte=s, bill_date__lte=e), user, 'expenses')
        if _int(p, 'project'):
            qs = qs.filter(project_id=_int(p, 'project'))
        if _int(p, 'category'):
            qs = qs.filter(category_id=_int(p, 'category'))
        return {r['category_id']: (r['n'], r['v'] or ZERO) for r in qs.values('category_id').annotate(n=Count('id'), v=Sum('amount_before_tax'))}
    now = window(p.start, p.end)
    ps, pe = previous_period(p.start, p.end)
    before = window(ps, pe)
    t = Table([('Category', 'text'), ('Bills', 'count'), ('Spend before tax', 'money'), ('Previous period', 'money'), ('Change', 'percent')])
    names = dict(ExpenseCategory.objects.values_list('pk', 'name'))
    tot = [0, ZERO, ZERO]
    for cid in sorted(set(now) | set(before), key=lambda c: -(now.get(c, (0, ZERO))[1])):
        n, v = now.get(cid, (0, ZERO))
        pv = before.get(cid, (0, ZERO))[1]
        t.rows.append({'key': str(cid), 'cells': [names.get(cid, '—'), n, v, pv, _pct(v - pv, pv) if pv else None],
                       'href': reverse('crm:expense_list') + f'?category={cid}&from={p.start.isoformat()}&to={p.end.isoformat()}'})
        tot = [tot[0] + n, tot[1] + v, tot[2] + pv]
    t.totals = ['Total', tot[0], tot[1], tot[2], _pct(tot[1] - tot[2], tot[2]) if tot[2] else None]
    t.notes.append(f'Approved and paid bills dated in the period. Previous period: {ps:%d %b %Y} to {pe:%d %b %Y}.')
    t.chart = {'type': 'bar', 'labels': [r['cells'][0] for r in t.rows[:12]],
               'series': [{'name': 'This period', 'data': [float(r['cells'][2]) for r in t.rows[:12]]},
                          {'name': 'Previous', 'data': [float(r['cells'][3]) for r in t.rows[:12]]}]}
    return t


def profitability(user, p: Params) -> Table:
    """Revenue (invoiced before tax, net of credit notes) minus direct cost (labour at cost rate + direct and allocated expenses)."""
    from crm.models import CreditNote, Invoice, InvoiceLine, Project
    from crm.models.billing import INV_POSTED
    from .services import project_cost
    view = p.filters.get('by', 'client')
    inv = _scoped(Invoice.objects.filter(status__in=INV_POSTED, invoice_date__gte=p.start, invoice_date__lte=p.end), user, 'invoices')
    cn = CreditNote.objects.filter(status='issued', note_date__gte=p.start, note_date__lte=p.end, invoice__in=inv)
    projects = _scoped(Project.objects.all(), user, 'projects').distinct() if permissions.has_perm(user, 'projects', 'view') \
        else Project.objects.none()
    t = Table([(view.capitalize(), 'text'), ('Revenue (billed, before tax)', 'money'), ('Direct cost', 'money'),
               ('Margin', 'money'), ('Margin %', 'percent')])
    if not permissions.can_see(user, 'cost_price'):
        t.notes.append('Costs are hidden without the cost-price field group; only revenue is shown.')
    rows = {}

    def add(key, label, rev=ZERO, cost=ZERO, href=''):
        r = rows.setdefault(key, {'label': label, 'rev': ZERO, 'cost': ZERO, 'href': href})
        r['rev'] += rev
        r['cost'] += cost

    show_cost = permissions.can_see(user, 'cost_price')
    if view == 'project':
        for r in inv.filter(project__isnull=False).values('project_id').annotate(v=Sum('taxable_total')):
            add(r['project_id'], '', rev=r['v'] or ZERO)
        for r in cn.filter(invoice__project__isnull=False).values('invoice__project_id').annotate(v=Sum('taxable_value')):
            add(r['invoice__project_id'], '', rev=-(r['v'] or ZERO))
        for pr in projects.filter(pk__in=list(rows) or [0]) if not show_cost else projects:
            c = project_cost(pr)['total'] if show_cost else ZERO
            if pr.pk in rows or c:
                add(pr.pk, str(pr), cost=c, href=reverse('crm:project_detail', args=[pr.pk]))
                rows[pr.pk]['label'] = str(pr)
    elif view == 'service':
        lines = InvoiceLine.objects.filter(invoice__in=inv)
        for r in lines.values('service_id', 'service__name').annotate(v=Sum('taxable_value')):
            add(r['service_id'], r['service__name'] or 'No service', rev=r['v'] or ZERO)
        if show_cost:
            for pr in projects.filter(service__isnull=False):
                c = project_cost(pr)['total']
                if c:
                    add(pr.service_id, pr.service.name, cost=c)
        t.notes.append('Service revenue uses invoice lines; credit notes are not split by service.')
    else:
        from .services import client_profitability
        for r in inv.values('client_id', 'client__display_name').annotate(v=Sum('taxable_total')):
            add(r['client_id'], r['client__display_name'], rev=r['v'] or ZERO, href=reverse('crm:client_detail', args=[r['client_id']]))
        for r in cn.values('invoice__client_id').annotate(v=Sum('taxable_value')):
            if r['invoice__client_id'] in rows:
                rows[r['invoice__client_id']]['rev'] -= r['v'] or ZERO
        if show_cost:
            from crm.models import Client
            for c in Client.objects.filter(pk__in=list(rows)):
                rows[c.pk]['cost'] += client_profitability(c)['direct_cost']
        t.notes.append('Client cost covers all of the client\'s projects (lifetime) and expenses allocated to the client.')
    tot = [ZERO, ZERO]
    for key, r in sorted(rows.items(), key=lambda kv: -kv[1]['rev']):
        margin = r['rev'] - r['cost'] if show_cost else None
        t.rows.append({'key': str(key), 'cells': [r['label'] or '—', r['rev'], r['cost'] if show_cost else None, margin,
                                                  _pct(margin, r['rev']) if show_cost and r['rev'] else None], 'href': r['href']})
        tot = [tot[0] + r['rev'], tot[1] + r['cost']]
    t.totals = ['Total', tot[0], tot[1] if show_cost else None, tot[0] - tot[1] if show_cost else None,
                _pct(tot[0] - tot[1], tot[0]) if show_cost and tot[0] else None]
    return t


def mrr(user, p: Params) -> Table:
    from crm.modules.contracts.services import mrr_movements
    if permissions.get_scope(user, 'contracts', 'view') != 'company' or permissions.get_scope(user, 'reports', 'view') != 'company':
        raise permissions.PermissionDenied('MRR covers all contracts and needs company-wide access.')
    m = mrr_movements(p.start, p.end)
    t = Table([('Movement', 'text'), ('Monthly value', 'money')])
    for label, key in (('MRR at start', 'start'), ('New', 'new'), ('Expansion', 'expansion'), ('Contraction', 'contraction'),
                       ('Churned', 'churn'), ('MRR at end', 'end'), ('Net new MRR', 'net_new'), ('Annual run rate', 'arr')):
        t.rows.append({'key': key, 'cells': [label, m[key]], 'href': reverse('crm:renewals') if key == 'churn' else ''})
    return t


@dataclass
class Report:
    slug: str
    title: str
    group: str
    definition: str
    module: str
    func: object = None
    url_name: str = ''           # an existing dedicated page instead of a table
    filters: tuple = ()
    choices: dict = field(default_factory=dict)


REPORTS = [
    Report('funnel', 'Sales funnel and conversion', 'Sales', 'Leads created in the period and how far each got, stage to stage, with average time spent before each move.',
           'leads', funnel, filters=('owner', 'team', 'source', 'service')),
    Report('sources', 'Lead-source performance', 'Sales', 'Leads, qualified, won and won value per source, against campaign spend.', 'leads', sources,
           filters=('owner', 'source')),
    Report('response', 'Lead response and SLA', 'Sales', 'First-response time, SLA breaches and time unassigned, per owner.', 'leads', response,
           filters=('team', 'owner', 'source')),
    Report('forecast', 'Weighted forecast', 'Sales', 'Open deal value times stage probability, by expected close month.', 'deals', forecast,
           filters=('owner', 'team')),
    Report('quotations', 'Quotation win and loss', 'Sales', 'Quotations sent in the period: win rate by count and value, reasons and time to decision.',
           'quotations', quotations, filters=('owner', 'service')),
    Report('collections', 'Payment collection', 'Money', 'Payment links created in the period: opened, paid, failed and expired, conversion and time to pay.',
           'payment_links', collections, filters=('owner',)),
    Report('ageing', 'Receivables ageing', 'Money', 'Open invoice balances by days past due, with promises and disputes.', 'invoices', url_name='crm:receivables'),
    Report('mrr', 'MRR and renewals', 'Money', 'Monthly recurring revenue movements between two dates: new, expansion, contraction, churn.',
           'contracts', mrr),
    Report('delivery', 'Project delivery and utilisation', 'Delivery', 'Hours against budget, burn, on-time delivery and overdue tasks per project.',
           'projects', delivery, filters=('project',)),
    Report('payroll_cost', 'Employee cost and payroll', 'People', 'Loaded employee cost by department and month from approved payroll runs.',
           'payroll', payroll_cost, filters=('department',)),
    Report('expenses', 'Expenses by category', 'Money', 'Approved spend before tax by category, against the previous period of the same length.',
           'expenses', expenses, filters=('category', 'project')),
    Report('profitability', 'Client, project and service profitability', 'Money',
           'Revenue billed before tax (net of credit notes) minus direct cost: labour at cost rate plus direct and allocated expenses.',
           'invoices', profitability, filters=('by',), choices={'by': [('client', 'By client'), ('project', 'By project'), ('service', 'By service')]}),
    Report('pl', 'Profit and loss', 'Accounts', 'Recognised revenue and expenses from posted journals.', 'accounting', url_name='crm:profit_loss'),
    Report('cash_flow', 'Cash flow', 'Accounts', 'Cash in and out of bank and cash accounts.', 'accounting', url_name='crm:cash_flow'),
    Report('balance_sheet', 'Balance sheet', 'Accounts', 'Assets, liabilities and equity as of a date.', 'accounting', url_name='crm:balance_sheet'),
    Report('tax', 'GST and TDS', 'Accounts', 'GSTR-1 outward supplies, ITC, GSTR-3B summary, TDS receivable and payable.', 'accounting', url_name='crm:tax_reports'),
    Report('audit', 'Audit and access', 'Control', 'Sensitive actions, exports, permission changes and failed logins.', 'audit', url_name='crm:audit_log'),
]
BY_SLUG = {r.slug: r for r in REPORTS}


def visible(user) -> list[Report]:
    if not permissions.has_perm(user, 'reports', 'view'):
        return []
    out = []
    for r in REPORTS:
        if not permissions.has_perm(user, r.module, 'view'):
            continue
        if r.slug == 'payroll_cost' and not permissions.can_see(user, 'salary'):
            continue
        if r.slug == 'mrr' and (permissions.get_scope(user, 'contracts', 'view') != 'company'
                                or permissions.get_scope(user, 'reports', 'view') != 'company'):
            continue
        out.append(r)
    return out


def params_from(query, default_days: int = 30) -> Params:
    today = india.today()
    try:
        start = date.fromisoformat(query.get('from', ''))
    except ValueError:
        start = india.month_start(today)
    try:
        end = date.fromisoformat(query.get('to', ''))
    except ValueError:
        end = today
    if end < start:
        start, end = end, start
    filters = {k: query.get(k, '') for k in list(FILTERS) + ['by'] if query.get(k)}
    return Params(start, end, filters)


def run(report: Report, user, params: Params) -> Table:
    return report.func(user, params)


def _fmt(value, kind):
    if value is None:
        return ''
    if kind == 'money':
        return f'{Decimal(value):.2f}'
    return str(value)


def to_csv(report: Report, table: Table, params: Params) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report.title, f'{params.start:%d-%b-%Y} to {params.end:%d-%b-%Y}'])
    w.writerow([c[0] for c in table.columns])
    for r in table.rows:
        w.writerow([_safe(_fmt(v, table.columns[i][1])) for i, v in enumerate(r['cells'])])
    if table.totals:
        w.writerow([_safe(_fmt(v, table.columns[i][1])) for i, v in enumerate(table.totals)])
    for n in table.notes:
        w.writerow([n])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def _safe(v: str) -> str:
    """Block spreadsheet formula injection, but keep negative numbers."""
    if v and v[0] in '=+@\t\r' or (v[:1] == '-' and not _num(v)):
        return "'" + v
    return v


def _num(v: str) -> bool:
    try:
        Decimal(v)
        return True
    except Exception:
        return False


def run_schedules(now=None) -> int:
    """Job: email due scheduled reports as CSV, computed as their owner (scope preserved)."""
    from django.http import QueryDict
    from django.utils import timezone
    from crm.core import notify
    from crm.models import ReportSchedule
    now = timezone.localtime(now or timezone.now())
    sent = 0
    for s in ReportSchedule.objects.filter(is_active=True, hour__lte=now.hour).select_related('user'):
        if s.last_sent_at and timezone.localtime(s.last_sent_at).date() == now.date():
            continue
        if s.frequency == 'weekly' and now.weekday() != 0 or s.frequency == 'monthly' and now.day != 1:
            continue
        report = BY_SLUG.get(s.report)
        user = s.user
        if report is None or report.func is None or not user.is_active or report not in visible(user) or not user.email:
            s.last_error = 'Report unavailable or no email address for the owner.'
            s.save(update_fields=['last_error'])
            continue
        q = QueryDict(s.querystring)
        params = params_from(q)
        # Rolling window: keep the saved length, ending today.
        length = (params.end - params.start).days
        params.end = now.date()
        params.start = params.end - timedelta(days=length)
        try:
            table = run(report, user, params)
            data = to_csv(report, table, params)
            notify.send_email(user.email, f'{report.title}: {params.start:%d %b} – {params.end:%d %b %Y}',
                              f'Your scheduled report is attached.\n\n{report.definition}\n\nIt contains only records you are allowed to see.',
                              attachments=[(f'{report.slug}-{params.end:%Y%m%d}.csv', data, 'text/csv')], template_code='report_schedule')
            from crm.core import audit
            from crm.models import ExportLog
            ExportLog.objects.create(user=user, module=f'report:{report.slug}', filters={'schedule': s.pk, 'q': s.querystring},
                                     columns=[c[0] for c in table.columns], row_count=len(table.rows), file_format='csv')
            audit.log('report.scheduled_sent', actor=None, object_type='reportschedule', object_id=str(s.pk),
                      changes={'report': report.slug, 'rows': len(table.rows), 'to': user.email})
            s.last_sent_at = timezone.now()
            s.last_error = ''
            sent += 1
        except Exception as exc:  # one bad schedule never stops the others
            s.last_error = str(exc)[:300]
        s.save(update_fields=['last_sent_at', 'last_error'])
    return sent
