"""Exact numbers for the AI business staff. Everything here is plain Python and SQL: the AI model never calculates
anything, it only explains what this module returns.

Dates are Indian calendar days. "Collected" is verified cash received; "invoiced" is billed value before tax.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from crm.core import attribution, india
from crm.core.money import ZERO
from crm.models import (BillingSchedule, BusinessGoal, Client, Contract, Deliverable, FollowUp, Invoice, Lead,
                        LeadActivity, Payment, Quotation, Task, Ticket)
from crm.models.billing import INV_OPEN, INV_POSTED
from crm.models.deliverables import OPEN_STATUSES
from crm.models.desk import METRIC_CHOICES, MONEY_METRICS
from crm.models.leads import OPEN_STAGES
from crm.models.payments import P_PART_REFUNDED, P_VERIFIED
from crm.models.quotations import Q_LIVE

SALES_ROLES = ('sales_manager', 'sales_executive', 'telecaller')
STALE_DAYS = 7
CASH_STATUSES = [P_VERIFIED, P_PART_REFUNDED]
METRIC_LABELS = dict(METRIC_CHOICES)
NUMBER_KEYS = ['leads_created', 'leads_won', 'won_value', 'leads_lost', 'collected', 'invoiced', 'delivered',
               'clients_onboarded', 'calls', 'followups_done', 'quotes_sent']
NUMBER_LABELS = {
    'leads_created': 'New leads', 'leads_won': 'Deals won', 'won_value': 'Won value', 'leads_lost': 'Leads lost',
    'collected': 'Cash collected', 'invoiced': 'Invoiced', 'delivered': 'Items delivered',
    'clients_onboarded': 'New clients', 'calls': 'Calls logged', 'followups_done': 'Follow-ups done',
    'quotes_sent': 'Quotations sent',
}
MONEY_KEYS = ('won_value', 'collected', 'invoiced')


# ------------------------------------------------------------------ small helpers
def f(value) -> float:
    return round(float(value or 0), 2)


def bounds(d: date) -> tuple[datetime, datetime]:
    start = timezone.make_aware(datetime.combine(d, time.min))
    return start, start + timedelta(days=1)


def person_name(u) -> str:
    return (u.first_name or u.get_username()) if u else 'Nobody'


def _names() -> dict:
    return {u.pk: person_name(u) for u in get_user_model().objects.filter(is_active=True)}


def sales_people():
    User = get_user_model()
    return (User.objects.filter(is_active=True, crm_profile__roles__code__in=SALES_ROLES).distinct()
            .select_related('crm_profile').order_by('first_name', 'username'))


# ------------------------------------------------------------------ the day, in numbers
def day_numbers(d: date) -> dict:
    """What happened on one day. Every figure can be rebuilt for any past date, so comparisons need no history."""
    s, e = bounds(d)
    won = Lead.objects.filter(stage='won', won_at__gte=s, won_at__lt=e)
    return {
        'leads_created': Lead.objects.filter(created_at__gte=s, created_at__lt=e).count(),
        'leads_won': won.count(),
        'won_value': f(won.aggregate(v=Sum('won_value'))['v']),
        'leads_lost': Lead.objects.filter(stage='lost', lost_at__gte=s, lost_at__lt=e).count(),
        'collected': f(Payment.objects.filter(status__in=CASH_STATUSES, payment_date=d).aggregate(v=Sum('amount'))['v']),
        'invoiced': f(Invoice.objects.filter(status__in=INV_POSTED, invoice_date=d).aggregate(v=Sum('taxable_total'))['v']),
        'delivered': Deliverable.objects.filter(status='delivered', delivered_at__gte=s, delivered_at__lt=e).count(),
        'clients_onboarded': Client.objects.filter(created_at__gte=s, created_at__lt=e, archived_at__isnull=True).count(),
        'calls': LeadActivity.objects.filter(kind='call', occurred_at__gte=s, occurred_at__lt=e).count(),
        'followups_done': FollowUp.objects.filter(status='done', completed_at__gte=s, completed_at__lt=e).count(),
        'quotes_sent': Quotation.objects.filter(sent_at__gte=s, sent_at__lt=e).count(),
    }


def numbers_for(d: date) -> dict:
    """day_numbers with a short cache, because a dashboard asks for the same few days repeatedly."""
    today = india.today()
    key = f'desk:nums:{d.isoformat()}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    data = day_numbers(d)
    cache.set(key, data, 90 if d >= today else 3600)
    return data


def compare(d: date) -> dict:
    """Each figure against yesterday and against the average of the 7 days before that."""
    now = numbers_for(d)
    yest = numbers_for(d - timedelta(days=1))
    prior = [numbers_for(d - timedelta(days=k)) for k in range(2, 9)]
    out = {}
    for k in NUMBER_KEYS:
        avg = sum(p[k] for p in prior) / len(prior)
        out[k] = {'key': k, 'label': NUMBER_LABELS[k], 'money': k in MONEY_KEYS, 'today': now[k], 'yesterday': yest[k],
                  'avg7': round(avg, 2), 'vs_yesterday': _change(now[k], yest[k]), 'vs_avg7': _change(now[k], avg)}
    return out


def _change(now, before):
    """Percent change, or None when there is nothing to compare with."""
    if not before:
        return None if not now else 100.0
    return round((now - before) * 100 / before, 1)


def trend(d: date, days: int = 7) -> dict:
    rows = [(d - timedelta(days=k)) for k in range(days - 1, -1, -1)]
    nums = [numbers_for(x) for x in rows]
    return {'labels': [x.strftime('%d %b') for x in rows],
            'series': [{'name': 'New leads', 'data': [n['leads_created'] for n in nums]},
                       {'name': 'Cash collected (Rs)', 'data': [n['collected'] for n in nums]}]}


# ------------------------------------------------------------------ delivery
def delivery(d: date) -> dict:
    s, e = bounds(d)
    live = Deliverable.objects.filter(plan__status='active')
    open_ = live.filter(status__in=OPEN_STATUSES)
    names = _names()
    people: dict[int | None, dict] = {}

    def row(pk):
        return people.setdefault(pk, {'id': pk, 'name': names.get(pk, 'Unassigned'), 'delivered': 0, 'pending_today': 0, 'overdue': 0})

    for r in live.filter(status='delivered', delivered_at__gte=s, delivered_at__lt=e).values('assignee').annotate(n=Count('id')):
        row(r['assignee'])['delivered'] = r['n']
    for r in open_.filter(due_date=d).values('assignee').annotate(n=Count('id')):
        row(r['assignee'])['pending_today'] = r['n']
    for r in open_.filter(due_date__lt=d).values('assignee').annotate(n=Count('id')):
        row(r['assignee'])['overdue'] = r['n']
    late = open_.filter(due_date__lt=d).select_related('client').order_by('due_date')[:8]
    return {
        'delivered_today': live.filter(status='delivered', delivered_at__gte=s, delivered_at__lt=e).count(),
        'pending_today': open_.filter(due_date=d).count(),
        'overdue': open_.filter(due_date__lt=d).count(),
        'missed_30d': live.filter(missed_at__gte=s - timedelta(days=30)).count(),
        'in_review': open_.filter(status='in_review').count(),
        'awaiting_client': open_.filter(status='awaiting_client').count(),
        'open_total': open_.count(),
        'by_person': sorted(people.values(), key=lambda p: (-p['overdue'], -p['pending_today'], p['name'])),
        'overdue_items': [{'title': x.title, 'client': x.client.display_name, 'due': x.due_date.isoformat(),
                           'days_late': (d - x.due_date).days, 'person': names.get(x.assignee_id, 'Unassigned')}
                          for x in late],
    }


def on_time_rate(d: date, days: int = 30) -> int | None:
    """Share of items delivered in the last `days` days that arrived on or before their current due date."""
    s = bounds(d - timedelta(days=days))[0]
    rows = Deliverable.objects.filter(status='delivered', delivered_at__gte=s, delivered_at__lt=bounds(d)[1])
    total = rows.count()
    if not total:
        return None
    late = sum(1 for x in rows.only('delivered_at', 'due_date') if x.was_late)
    return round((total - late) * 100 / total)


# ------------------------------------------------------------------ tomorrow
def tomorrow(d: date) -> dict:
    t = d + timedelta(days=1)
    s, e = bounds(t)
    names = _names()
    due = Deliverable.objects.filter(plan__status='active', status__in=OPEN_STATUSES, due_date=t)
    by_person: dict[str, int] = {}
    for r in due.values('assignee').annotate(n=Count('id')):
        by_person[names.get(r['assignee'], 'Unassigned')] = r['n']
    fu = FollowUp.objects.filter(status='pending', due_at__gte=s, due_at__lt=e)
    fu_by: dict[str, int] = {}
    for r in fu.values('assignee').annotate(n=Count('id')):
        fu_by[names.get(r['assignee'], 'Unassigned')] = r['n']
    billing = BillingSchedule.objects.filter(status='scheduled', invoice_date=t)
    return {
        'date': t.isoformat(),
        'deliverables_due': due.count(),
        'deliverables_by_person': by_person,
        'followups_due': fu.count(),
        'followups_by_person': fu_by,
        'invoices_to_raise': billing.count(),
        'invoices_to_raise_value': f(billing.aggregate(v=Sum('amount'))['v']),
        'quotes_expiring': Quotation.objects.filter(status__in=Q_LIVE, valid_until=t).count(),
        'contracts_ending': Contract.objects.filter(status='active', end_date=t).count(),
        'tasks_due': Task.objects.filter(due_date=t).exclude(status__in=['done', 'cancelled']).count(),
    }


# ------------------------------------------------------------------ team
def team(d: date) -> list[dict]:
    """One row per salesperson for the day, with the red/amber/green rating from crm.modules.desk.rules."""
    from . import rules
    s, e = bounds(d)
    now = timezone.now()
    stale_before = now - timedelta(days=STALE_DAYS)
    rows = []
    for u in sales_people():
        codes = {r.code for r in u.crm_profile.roles.all()}
        mine = Lead.objects.filter(owner=u, archived_at__isnull=True)
        won = Lead.objects.filter(owner=u, stage='won', won_at__gte=s, won_at__lt=e)
        acts = LeadActivity.objects.filter(actor=u, occurred_at__gte=s, occurred_at__lt=e)
        row = {
            'id': u.pk, 'name': person_name(u),
            'role': 'Sales manager' if 'sales_manager' in codes else ('Telecaller' if codes == {'telecaller'} else 'Sales executive'),
            'leads_created': Lead.objects.filter(created_by=u, created_at__gte=s, created_at__lt=e).count(),
            'leads_assigned': Lead.objects.filter(owner=u, assigned_at__gte=s, assigned_at__lt=e).count(),
            'calls': acts.filter(kind='call').count(),
            'activities': acts.count(),
            'followups_done': u.crm_follow_ups.filter(status='done', completed_at__gte=s, completed_at__lt=e).count(),
            'overdue_followups': u.crm_follow_ups.filter(status='pending', due_at__lt=now).count(),
            'open_leads': mine.filter(stage__in=OPEN_STAGES).count(),
            'stale_leads': mine.filter(stage__in=OPEN_STAGES, last_activity_at__lt=stale_before).count(),
            'won': won.count(),
            'won_value': f(won.aggregate(v=Sum('won_value'))['v']),
            'lost': Lead.objects.filter(owner=u, stage='lost', lost_at__gte=s, lost_at__lt=e).count(),
            'quotes_sent': Quotation.objects.filter(owner=u, sent_at__gte=s, sent_at__lt=e).count(),
            'collected': f(attribution.collected_total([u.pk], d, d, CASH_STATUSES)),
        }
        row.update(rules.rate_person(row, d))
        rows.append(row)
    return rows


# ------------------------------------------------------------------ leads
def leads(d: date) -> dict:
    s, e = bounds(d)
    ms = bounds(india.month_start(d))[0]
    now = timezone.now()
    names = _names()
    by_creator: dict[str, int] = {}
    for r in Lead.objects.filter(created_at__gte=s, created_at__lt=e).values('created_by').annotate(n=Count('id')):
        by_creator[names.get(r['created_by'], 'Website and automatic')] = r['n']
    by_source = {r['source__name']: r['n'] for r in
                 Lead.objects.filter(created_at__gte=s, created_at__lt=e).values('source__name').annotate(n=Count('id')).order_by('-n')[:8]}
    month_source = {r['source__name']: r['n'] for r in
                    Lead.objects.filter(created_at__gte=ms, created_at__lt=e).values('source__name').annotate(n=Count('id')).order_by('-n')[:8]}
    open_ = Lead.objects.filter(stage__in=OPEN_STAGES, archived_at__isnull=True)
    reasons = {r['lost_reason'] or 'not recorded': r['n'] for r in
               Lead.objects.filter(stage='lost', lost_at__gte=s - timedelta(days=30), lost_at__lt=e)
               .values('lost_reason').annotate(n=Count('id')).order_by('-n')[:5]}
    return {
        'created_today_by_person': by_creator,
        'created_today_by_source': by_source,
        'created_this_month_by_source': month_source,
        'open': open_.count(),
        'pipeline_value': f(open_.aggregate(v=Sum('value'))['v']),
        'unassigned_open': open_.filter(owner__isnull=True).count(),
        'stale_open': open_.filter(last_activity_at__lt=now - timedelta(days=STALE_DAYS)).count(),
        'overdue_followups': FollowUp.objects.filter(status='pending', due_at__lt=now).count(),
        'followups_due_today': FollowUp.objects.filter(status='pending', due_at__gte=s, due_at__lt=e).count(),
        'lost_reasons_30d': reasons,
    }


# ------------------------------------------------------------------ revenue
def revenue(d: date) -> dict:
    ms = india.month_start(d)
    names = _names()
    cash = Payment.objects.filter(status__in=CASH_STATUSES)
    by_owner = {}
    for uid, v in attribution.collected_by_user(d, d, CASH_STATUSES).items():
        by_owner[names.get(uid, 'Unassigned')] = by_owner.get(names.get(uid, 'Unassigned'), 0) + f(v)
    by_owner_month = {}
    for uid, v in attribution.collected_by_user(ms, d, CASH_STATUSES).items():
        by_owner_month[names.get(uid, 'Unassigned')] = by_owner_month.get(names.get(uid, 'Unassigned'), 0) + f(v)
    open_inv = Invoice.objects.filter(status__in=INV_OPEN)
    overdue = open_inv.filter(due_date__lt=d)
    prev_ms = india.add_months(ms, -1)
    same_day_last_month = min(india.month_end(prev_ms), prev_ms.replace(day=min(d.day, 28)))
    return {
        'collected_today': f(cash.filter(payment_date=d).aggregate(v=Sum('amount'))['v']),
        'collected_month': f(cash.filter(payment_date__gte=ms, payment_date__lte=d).aggregate(v=Sum('amount'))['v']),
        'collected_last_month_same_period': f(cash.filter(payment_date__gte=prev_ms, payment_date__lte=same_day_last_month)
                                              .aggregate(v=Sum('amount'))['v']),
        'invoiced_month': f(Invoice.objects.filter(status__in=INV_POSTED, invoice_date__gte=ms, invoice_date__lte=d)
                            .aggregate(v=Sum('taxable_total'))['v']),
        'outstanding': f(open_inv.aggregate(v=Sum('balance'))['v']),
        'overdue_amount': f(overdue.aggregate(v=Sum('balance'))['v']),
        'overdue_invoices': overdue.count(),
        'overdue_over_30d': f(open_inv.filter(due_date__lt=d - timedelta(days=30)).aggregate(v=Sum('balance'))['v']),
        'collected_today_by_owner': by_owner,
        'collected_month_by_owner': by_owner_month,
    }


# ------------------------------------------------------------------ customers
def customers(d: date) -> dict:
    s, e = bounds(d)
    onboarded = list(Client.objects.filter(created_at__gte=s, created_at__lt=e, archived_at__isnull=True)
                     .values_list('display_name', flat=True)[:10])
    activated = list(Contract.objects.filter(activated_at__gte=s, activated_at__lt=e).select_related('client')
                     .values_list('client__display_name', flat=True)[:10])
    lost = list(Contract.objects.filter(Q(terminated_on=d) | Q(status='expired', end_date=d)).select_related('client')
                .values_list('client__display_name', flat=True)[:10])
    return {
        'onboarded_today': onboarded,
        'contracts_activated_today': activated,
        'lost_today': lost,
        'new_clients_month': Client.objects.filter(created_at__gte=bounds(india.month_start(d))[0], archived_at__isnull=True).count(),
        'active_clients': Client.objects.filter(status='active', archived_at__isnull=True).count(),
        'at_risk': at_risk(d),
    }


def at_risk(d: date, limit: int = 10) -> list[dict]:
    """Clients likely to be lost, with the reasons. Ranked by how many warning signs they show."""
    found: dict[int, dict] = {}

    def add(client, reason, weight=1):
        r = found.setdefault(client.pk, {'id': client.pk, 'client': client.display_name, 'reasons': [], 'score': 0})
        r['reasons'].append(reason)
        r['score'] += weight

    for c in Contract.objects.filter(status='active', end_date__gte=d, end_date__lte=d + timedelta(days=30), auto_renew=False) \
            .select_related('client'):
        add(c.client, f'contract ends {c.end_date:%d %b} with no auto-renew', 2)
    overdue = (Invoice.objects.filter(status__in=INV_OPEN, due_date__lt=d - timedelta(days=30)).select_related('client')
               .values('client').annotate(v=Sum('balance'), n=Count('id')))
    clients = {c.pk: c for c in Client.objects.filter(pk__in=[r['client'] for r in overdue])}
    for r in overdue:
        add(clients[r['client']], f"{r['n']} invoice(s) unpaid for over 30 days (Rs {int(r['v'] or 0):,})", 2)
    for t in Ticket.objects.filter(status__in=('open', 'in_progress', 'waiting'), sla_due_at__lt=timezone.now()).select_related('client'):
        add(t.client, f'support ticket {t.number} is past its deadline')
    missed = (Deliverable.objects.filter(missed_at__gte=timezone.now() - timedelta(days=30)).values('client').annotate(n=Count('id'))
              .filter(n__gte=2))
    mc = {c.pk: c for c in Client.objects.filter(pk__in=[r['client'] for r in missed])}
    for r in missed:
        add(mc[r['client']], f"{r['n']} promised items missed in the last 30 days", 2)
    return sorted(found.values(), key=lambda r: (-r['score'], r['client']))[:limit]


# ------------------------------------------------------------------ goals
def goal_actual(metric: str, start: date, end: date):
    """The figure for a goal's metric between two dates, inclusive."""
    s, e = bounds(start)[0], bounds(end)[1]
    if metric == 'revenue_collected':
        return f(Payment.objects.filter(status__in=CASH_STATUSES, payment_date__gte=start, payment_date__lte=end)
                 .aggregate(v=Sum('amount'))['v'])
    if metric == 'revenue_invoiced':
        return f(Invoice.objects.filter(status__in=INV_POSTED, invoice_date__gte=start, invoice_date__lte=end)
                 .aggregate(v=Sum('taxable_total'))['v'])
    if metric == 'won_value':
        return f(Lead.objects.filter(stage='won', won_at__gte=s, won_at__lt=e).aggregate(v=Sum('won_value'))['v'])
    if metric == 'new_leads':
        return Lead.objects.filter(created_at__gte=s, created_at__lt=e).count()
    if metric == 'new_clients':
        return Client.objects.filter(created_at__gte=s, created_at__lt=e, archived_at__isnull=True).count()
    if metric == 'deliverables_delivered':
        return Deliverable.objects.filter(status='delivered', delivered_at__gte=s, delivered_at__lt=e).count()
    if metric == 'win_rate':
        won = Lead.objects.filter(stage='won', won_at__gte=s, won_at__lt=e).count()
        lost = Lead.objects.filter(stage='lost', lost_at__gte=s, lost_at__lt=e).count()
        return round(won * 100 / (won + lost), 1) if (won + lost) else 0
    return 0


def goal_progress(d: date) -> list[dict]:
    """Every goal set for this month against what has happened so far, and whether the pace will reach it."""
    ms = india.month_start(d)
    days_total = india.month_end(d).day
    elapsed = d.day
    rows = []
    for g in BusinessGoal.objects.filter(month=ms).order_by('metric'):
        target = float(g.target)
        actual = float(goal_actual(g.metric, ms, d))
        pct = round(actual * 100 / target) if target else 0
        if g.metric == 'win_rate':
            projected = actual
            per_day = None
            remaining = max(target - actual, 0)
        else:
            projected = actual / elapsed * days_total if elapsed else 0
            remaining = max(target - actual, 0)
            per_day = round(remaining / max(days_total - elapsed, 1), 2) if remaining else 0
        if actual >= target:
            status = 'ahead'
        elif projected >= target * 0.95:
            status = 'on_track'
        elif projected >= target * 0.75:
            status = 'at_risk'
        else:
            status = 'behind'
        rows.append({'id': g.pk, 'metric': g.metric, 'label': METRIC_LABELS[g.metric], 'money': g.metric in MONEY_METRICS,
                     'percent_metric': g.metric == 'win_rate', 'target': target, 'actual': actual, 'pct': pct,
                     'projected': round(projected, 1), 'remaining': round(remaining, 2), 'per_day_needed': per_day,
                     'days_left': days_total - elapsed, 'status': status, 'source': g.source,
                     'expected_pct': round(elapsed * 100 / days_total)})
    return rows


# ------------------------------------------------------------------ the longer view (used for weak areas)
def funnel(d: date) -> dict:
    """Ninety days of the sales and money funnel, used to see which stage is the weak one."""
    start = d - timedelta(days=90)
    s, e = bounds(start)[0], bounds(d)[1]
    created = Lead.objects.filter(created_at__gte=s, created_at__lt=e).count()
    won = Lead.objects.filter(stage='won', won_at__gte=s, won_at__lt=e)
    won_n = won.count()
    lost_n = Lead.objects.filter(stage='lost', lost_at__gte=s, lost_at__lt=e).count()
    won_value = float(won.aggregate(v=Sum('won_value'))['v'] or 0)
    collected = float(Payment.objects.filter(status__in=CASH_STATUSES, payment_date__gte=start, payment_date__lte=d)
                      .aggregate(v=Sum('amount'))['v'] or 0)
    invoiced = float(Invoice.objects.filter(status__in=INV_POSTED, invoice_date__gte=start, invoice_date__lte=d)
                     .aggregate(v=Sum('taxable_total'))['v'] or 0)
    closed = won_n + lost_n
    return {
        'leads_90d': created, 'leads_per_day': round(created / 90, 2),
        'won_90d': won_n, 'lost_90d': lost_n, 'closed_90d': closed,
        'win_rate': round(won_n * 100 / closed, 1) if closed else None,
        'avg_deal_value': round(won_value / won_n, 2) if won_n else None,
        'collected_90d': round(collected, 2), 'invoiced_90d': round(invoiced, 2),
        'collection_ratio': round(collected * 100 / invoiced) if invoiced else None,
        'on_time_pct': on_time_rate(d),
    }


def stock(d: date) -> dict:
    """The position at this moment. Cannot be rebuilt for a past date, which is why snapshots keep it."""
    from crm.modules.contracts import services as contract_svc
    lead = leads(d)
    rev = revenue(d)
    return {
        'open_leads': lead['open'], 'pipeline_value': lead['pipeline_value'], 'unassigned_leads': lead['unassigned_open'],
        'stale_leads': lead['stale_open'], 'overdue_followups': lead['overdue_followups'],
        'outstanding': rev['outstanding'], 'overdue_amount': rev['overdue_amount'],
        'active_contracts': Contract.objects.filter(status='active').count(),
        'mrr': f(contract_svc.mrr(d)),
        'active_clients': Client.objects.filter(status='active', archived_at__isnull=True).count(),
        'overdue_deliverables': Deliverable.objects.filter(plan__status='active', status__in=OPEN_STATUSES, due_date__lt=d).count(),
    }


# ------------------------------------------------------------------ everything, for the page and the analyst
def full_context(d: date | None = None, *, fresh: bool = False) -> dict:
    """All the sections in one dict. Cached for a minute so the page and a chat message do not both pay for it."""
    from . import rules
    d = d or india.today()
    key = f'desk:ctx:{d.isoformat()}'
    if not fresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    ctx = {
        'date': d.isoformat(),
        'as_of': timezone.localtime().strftime('%d %b %Y %H:%M'),
        'numbers': numbers_for(d) if d < india.today() else day_numbers(d),
        'compare': compare(d),
        'delivery': delivery(d),
        'tomorrow': tomorrow(d),
        'team': team(d),
        'leads': leads(d),
        'revenue': revenue(d),
        'customers': customers(d),
        'goals': goal_progress(d),
        'funnel': funnel(d),
        'stock': stock(d),
    }
    ctx['flags'] = rules.flags(ctx)
    ctx['weak_areas'] = rules.weak_areas(ctx)
    cache.set(key, ctx, 60)
    return ctx
