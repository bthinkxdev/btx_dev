"""The judgement layer: who is on track, what needs attention, which area is weak, and what to do about it.

All of it is fixed rules over the numbers from `metrics`, so the answer is the same every time and works with no AI.
The AI only turns these results into readable advice.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify
from crm.models import Contract, DeskAction, Invoice, Lead
from crm.models.billing import INV_OPEN
from crm.models.leads import OPEN_STAGES

MAX_SUGGESTIONS_PER_DAY = 25
RANK = {'red': 0, 'amber': 1, 'green': 2}


# ------------------------------------------------------------------ people
def rate_person(row: dict, d: date) -> dict:
    """Red, amber or green for one salesperson, with the reasons in plain words."""
    red, amber = [], []
    n = row['overdue_followups']
    if n >= 5:
        red.append(f'{n} follow-ups overdue')
    elif n >= 2:
        amber.append(f'{n} follow-ups overdue')
    n = row['stale_leads']
    if n >= 5:
        red.append(f'{n} open leads untouched for {7}+ days')
    elif n >= 2:
        amber.append(f'{n} open leads untouched for {7}+ days')
    day_over = d < india.today() or timezone.localtime().hour >= 15
    if day_over and d.weekday() < 6 and row['open_leads'] > 0 and row['activities'] == 0:
        amber.append('nothing logged today')
    status = 'red' if red else 'amber' if amber else 'green'
    well = row['won'] > 0 or row['calls'] >= 8 or row['followups_done'] >= 5
    return {'status': status, 'reasons': red + amber, 'doing_well': bool(well and status != 'red')}


def _goal(ctx: dict, metric: str):
    return next((g for g in ctx['goals'] if g['metric'] == metric), None)


# ------------------------------------------------------------------ flags
def flags(ctx: dict) -> list[dict]:
    """Everything that needs the owner's attention right now, most serious first."""
    out = []

    def add(level, area, text):
        out.append({'level': level, 'area': area, 'text': text})

    for p in ctx['team']:
        if p['status'] != 'green':
            add('red' if p['status'] == 'red' else 'amber', 'sales', f"{p['name']}: " + '; '.join(p['reasons']))
    dl = ctx['delivery']
    if dl['overdue']:
        add('red' if dl['overdue'] >= 5 else 'amber', 'delivery', f"{dl['overdue']} deliverables are overdue")
    if dl['missed_30d']:
        add('red', 'delivery', f"{dl['missed_30d']} promised items were missed in the last 30 days")
    lead = ctx['leads']
    if lead['unassigned_open']:
        add('amber', 'sales', f"{lead['unassigned_open']} open leads have no owner")
    rev = ctx['revenue']
    if rev['overdue_over_30d']:
        add('red', 'money', f"Rs {int(rev['overdue_over_30d']):,} is more than 30 days overdue")
    elif rev['overdue_amount']:
        add('amber', 'money', f"Rs {int(rev['overdue_amount']):,} of invoices are past their due date")
    for g in ctx['goals']:
        if g['status'] in ('behind', 'at_risk'):
            add('red' if g['status'] == 'behind' else 'amber', 'goals',
                f"Goal '{g['label']}': {g['pct']}% done with {g['days_left']} days left, on pace for {_fmt(g, g['projected'])} "
                f"against {_fmt(g, g['target'])}")
    if ctx['customers']['at_risk']:
        add('amber', 'customers', f"{len(ctx['customers']['at_risk'])} clients show warning signs of leaving")
    for key in ('leads_created', 'collected', 'delivered'):
        c = ctx['compare'][key]
        if c['avg7'] >= 3 and c['vs_avg7'] is not None and c['vs_avg7'] <= -30:
            add('amber', 'trend', f"{c['label']} are {abs(c['vs_avg7']):.0f}% below the recent daily average")
    out.sort(key=lambda x: RANK.get(x['level'], 3))
    return out


def _fmt(goal: dict, value) -> str:
    if goal['money']:
        return f'Rs {int(value):,}'
    return f'{value:g}%' if goal.get('percent_metric') else f'{value:g}'


# ------------------------------------------------------------------ weak and strong areas
def weak_areas(ctx: dict) -> list[dict]:
    """Business areas ranked by how much they need strengthening, each with the evidence and the fix."""
    fn = ctx['funnel']
    out = []

    def add(area, severity, evidence, fix):
        out.append({'area': area, 'severity': severity, 'evidence': evidence, 'fix': fix})

    leads_goal, win_goal, rev_goal = _goal(ctx, 'new_leads'), _goal(ctx, 'won_value'), _goal(ctx, 'revenue_collected')
    # Lead volume
    if leads_goal and leads_goal['status'] in ('behind', 'at_risk'):
        add('Lead volume', 85 if leads_goal['status'] == 'behind' else 60,
            f"{leads_goal['actual']:g} of {leads_goal['target']:g} leads this month, on pace for {leads_goal['projected']:g}",
            'Put more budget or effort into the source that gives the most leads, and ask every salesperson for referrals.')
    elif win_goal and win_goal['status'] in ('behind', 'at_risk') and fn['win_rate'] and fn['avg_deal_value']:
        deals = win_goal['remaining'] / fn['avg_deal_value']
        need = deals / (fn['win_rate'] / 100) / max(win_goal['days_left'], 1)
        if need > fn['leads_per_day'] * 1.2:
            add('Lead volume', 70, f"To reach the won-value goal you need about {need:.1f} new leads a day; you are getting {fn['leads_per_day']}",
                'Raise the daily lead target and review the lead sources.')
    # Conversion
    if fn['closed_90d'] >= 5 and fn['win_rate'] is not None and fn['win_rate'] < 25:
        add('Conversion', 75 if fn['win_rate'] < 15 else 45,
            f"Win rate is {fn['win_rate']}% over 90 days ({fn['won_90d']} won, {fn['lost_90d']} lost)",
            'Review lost-lead reasons, tighten qualification, and send a quotation within a day of the first call.')
    # Follow-up discipline
    lead = ctx['leads']
    if lead['overdue_followups'] >= 4 or lead['stale_open'] >= 4:
        sev = 70 if (lead['overdue_followups'] >= 10 or lead['stale_open'] >= 10) else 45
        add('Follow-up discipline', sev, f"{lead['overdue_followups']} follow-ups overdue and {lead['stale_open']} open leads untouched for a week",
            'Clear overdue follow-ups first thing each morning and give every open lead a dated next step.')
    # Collections
    rev = ctx['revenue']
    ratio = (rev['overdue_amount'] / rev['outstanding']) if rev['outstanding'] else 0
    if ratio >= 0.4 and rev['overdue_amount']:
        add('Collections', 80, f"Rs {int(rev['overdue_amount']):,} of Rs {int(rev['outstanding']):,} outstanding is overdue",
            'Call the largest overdue clients this week and agree a dated payment promise.')
    elif fn['collection_ratio'] is not None and fn['collection_ratio'] < 70:
        add('Collections', 65, f"Only {fn['collection_ratio']}% of what was invoiced in 90 days has been collected",
            'Send reminders earlier and ask for part-payment up front on new work.')
    elif rev_goal and rev_goal['status'] in ('behind', 'at_risk'):
        add('Collections', 60, f"Collected Rs {int(rev_goal['actual']):,} of Rs {int(rev_goal['target']):,} this month",
            'Follow up on open invoices and bring forward any billing that is ready.')
    # Delivery
    dl = ctx['delivery']
    if dl['overdue'] >= 5 or (fn['on_time_pct'] is not None and fn['on_time_pct'] < 80):
        add('Delivery', 70, f"{dl['overdue']} items overdue; {fn['on_time_pct'] if fn['on_time_pct'] is not None else 'n/a'}% delivered on time in 30 days",
            'Rebalance work from the people with the most overdue items and agree dates with clients for anything slipping.')
    elif dl['missed_30d']:
        add('Delivery', 55, f"{dl['missed_30d']} promised items were missed in 30 days", 'Look at why items slip before the due date, not after.')
    # Retention
    risk = ctx['customers']['at_risk']
    if len(risk) >= 3:
        add('Customer retention', 70, f'{len(risk)} clients show warning signs', 'Speak to each at-risk client this week, starting with the largest contract.')
    elif risk:
        add('Customer retention', 45, f'{len(risk)} client shows warning signs', 'Speak to the client and settle the open issue.')
    out.sort(key=lambda x: -x['severity'])
    return out


def strengths(ctx: dict) -> list[str]:
    fn = ctx['funnel']
    out = []
    for g in ctx['goals']:
        if g['status'] in ('ahead', 'on_track'):
            out.append(f"{g['label']}: {g['pct']}% of the goal, {'ahead of' if g['status'] == 'ahead' else 'on'} pace")
    if fn['win_rate'] is not None and fn['win_rate'] >= 30 and fn['closed_90d'] >= 5:
        out.append(f"Strong win rate: {fn['win_rate']}%")
    if fn['on_time_pct'] is not None and fn['on_time_pct'] >= 95:
        out.append(f"Delivery is reliable: {fn['on_time_pct']}% on time")
    if fn['collection_ratio'] is not None and fn['collection_ratio'] >= 90:
        out.append(f"Collections are healthy: {fn['collection_ratio']}% of invoiced value collected")
    for p in ctx['team']:
        if p['doing_well']:
            bits = [x for x in (f"{p['won']} won" if p['won'] else '', f"{p['calls']} calls" if p['calls'] >= 8 else '',
                                f"{p['followups_done']} follow-ups done" if p['followups_done'] >= 5 else '') if x]
            out.append(f"{p['name']} is doing well today: " + ', '.join(bits))
    return out


# ------------------------------------------------------------------ next month
def roadmap(ctx: dict, d: date) -> dict:
    """A deterministic plan for next month: where to focus and a target for each person."""
    fn = ctx['funnel']
    nxt = india.add_months(india.month_start(d), 1)
    baseline = {
        'new_leads': fn['leads_90d'] / 3,
        'won_value': (fn['avg_deal_value'] or 0) * fn['won_90d'] / 3,
        'revenue_collected': fn['collected_90d'] / 3,
    }
    labels = {'new_leads': 'New leads', 'won_value': 'Deals won (Rs)', 'revenue_collected': 'Cash collected (Rs)'}
    targets = []
    for key, label in labels.items():
        g = _goal(ctx, key)
        if g:
            value, basis = g['target'], 'your goal for this month'
        else:
            value, basis = round(baseline[key] * 1.10, 0), 'last 90 days average plus 10%'
        targets.append({'metric': key, 'label': label, 'target': value, 'basis': basis, 'money': key != 'new_leads'})
    sellers = [p for p in ctx['team'] if p['role'] != 'Telecaller'] or ctx['team']
    n = len(sellers) or 1
    per_person = [{'name': p['name'], 'leads': round(targets[0]['target'] / n), 'won_value': round(targets[1]['target'] / n)}
                  for p in sellers]
    weak = ctx['weak_areas'][:3]
    return {'month': nxt.strftime('%B %Y'), 'focus': weak, 'targets': targets, 'per_person': per_person,
            'first_week': [w['fix'] for w in weak] or ['Keep the current routine and review the numbers weekly.']}


# ------------------------------------------------------------------ suggested actions
def _manager_or_owner():
    User = get_user_model()
    mgr = User.objects.filter(is_active=True, crm_profile__roles__code='sales_manager').order_by('id').first()
    return mgr or (notify.owners() or [None])[0]


def _add(batch: list, **kw) -> None:
    batch.append(kw)


def suggest_actions(ctx: dict, d: date | None = None) -> int:
    """Create today's suggestions from the rules. Safe to run repeatedly: each suggestion has a key for the day.
    Returns how many new ones were added."""
    d = d or india.today()
    tag = f'{d:%Y%m%d}'
    batch: list[dict] = []
    follow_at = india.next_working_day_at(10, 30)
    # 1. stale leads: schedule a follow-up for the oldest few per person
    for p in ctx['team']:
        if p['stale_leads'] < 3:
            continue
        stale = (Lead.objects.filter(owner_id=p['id'], stage__in=OPEN_STAGES, archived_at__isnull=True,
                                     last_activity_at__lt=timezone.now() - timedelta(days=7))
                 .order_by('last_activity_at')[:5])
        for lead in stale:
            days = (timezone.now() - (lead.last_activity_at or lead.created_at)).days
            _add(batch, kind='follow_up', key=f'stale:{lead.pk}:{tag}', title=f'Follow up with {lead.name} ({p["name"]})',
                 detail=f'No activity for {days} days. Stage: {lead.get_stage_display()}.', area='sales',
                 assignee_id=p['id'], lead=lead, due_at=follow_at, url=reverse('crm:lead_detail', args=[lead.pk]))
    # 2. overdue follow-ups: ask the person to clear them
    for p in ctx['team']:
        if p['overdue_followups'] >= 5:
            _add(batch, kind='nudge', key=f"overdue-fu:{p['id']}:{tag}", title=f"Ask {p['name']} to clear {p['overdue_followups']} overdue follow-ups",
                 detail='Clear the overdue follow-ups before anything new today.', area='sales', assignee_id=p['id'],
                 url=reverse('crm:followup_calendar'))
    # 3. unassigned leads
    if ctx['leads']['unassigned_open']:
        boss = _manager_or_owner()
        if boss:
            _add(batch, kind='nudge', key=f'unassigned:{tag}', title=f"Assign {ctx['leads']['unassigned_open']} unowned leads",
                 detail='Open leads with no owner are not being worked.', area='sales', assignee_id=boss.pk, url=reverse('crm:stale_leads'))
    # 4. overdue deliverables per assignee
    for p in ctx['delivery']['by_person']:
        if p['overdue'] >= 2 and p.get('id'):
            _add(batch, kind='nudge', key=f"overdue-dl:{p['id']}:{tag}", title=f"Ask {p['name']} to deliver {p['overdue']} overdue items",
                 detail='Deliver them today, or reschedule with a reason so the client knows.', area='delivery',
                 assignee_id=p['id'], url=reverse('crm:deliverable_mine'))
    # 5. invoices overdue 15+ days: chase the largest few
    for inv in (Invoice.objects.filter(status__in=INV_OPEN, due_date__lt=d - timedelta(days=15)).select_related('client', 'client__owner')
                .order_by('-balance')[:5]):
        days = (d - inv.due_date).days
        _add(batch, kind='nudge', key=f'chase:{inv.pk}:{tag}', title=f'Chase Rs {int(inv.balance):,} from {inv.client.display_name}',
             detail=f'Invoice {inv.number} is {days} days past due.', area='money', assignee_id=inv.client.owner_id,
             url=reverse('crm:invoice_detail', args=[inv.pk]))
    # 6. contracts ending within 30 days
    for c in (Contract.objects.filter(status='active', end_date__gte=d, end_date__lte=d + timedelta(days=30), auto_renew=False)
              .select_related('client').order_by('end_date')[:5]):
        _add(batch, kind='nudge', key=f'renew:{c.pk}:{tag}', title=f'Start the renewal talk with {c.client.display_name}',
             detail=f'Contract ends {c.end_date:%d %b} and does not auto-renew.', area='customers', assignee_id=c.owner_id,
             url=reverse('crm:contract_detail', args=[c.pk]))

    existing = DeskAction.objects.filter(for_date=d).count()
    made = 0
    for item in batch:
        if existing + made >= MAX_SUGGESTIONS_PER_DAY:
            break
        if not item.get('assignee_id'):
            continue
        try:
            with transaction.atomic():
                _, created = DeskAction.objects.get_or_create(
                    source_key=item['key'][:100],
                    defaults={'kind': item['kind'], 'title': item['title'][:200], 'detail': item['detail'][:500],
                              'area': item['area'], 'assignee_id': item['assignee_id'], 'lead': item.get('lead'),
                              'due_at': item.get('due_at'), 'url': item.get('url', '')[:300], 'for_date': d})
            made += int(created)
        except IntegrityError:
            continue
    return made


# ------------------------------------------------------------------ approving or dismissing
class ActionError(Exception):
    pass


def approve(action: DeskAction, user) -> DeskAction:
    """Do what the suggestion says. Only reachable from an owner pressing Approve."""
    from crm.modules.leads import services as lead_svc
    from crm.modules.leads.services import LeadError
    if action.status != 'suggested':
        raise ActionError('This suggestion has already been handled.')
    try:
        if action.kind == 'follow_up':
            if action.lead is None or action.assignee is None:
                raise ActionError('The lead is no longer available.')
            if action.lead.stage not in OPEN_STAGES or action.lead.archived_at:
                raise ActionError('That lead is no longer open.')
            due = action.due_at if action.due_at and action.due_at > timezone.now() else india.next_working_day_at(10, 30)
            lead_svc.schedule_follow_up(action.lead, kind='call', due_at=due, assignee=action.assignee,
                                        purpose=f'Suggested by the business staff: {action.detail}'[:200], actor=user)
            action.result = f'Follow-up booked for {timezone.localtime(due):%d %b %H:%M}.'
        else:
            notify.notify([action.assignee], action.title, body=action.detail, url=action.url, event_type='desk_action',
                          level='warning', dedupe_key=f'desk-action:{action.pk}')
            action.result = f'{action.assignee.first_name or action.assignee.get_username()} has been notified.'
        action.status = 'approved'
    except (LeadError, ActionError) as exc:
        action.status = 'failed'
        action.result = str(exc)[:300]
    action.decided_by = user
    action.decided_at = timezone.now()
    action.save(update_fields=['status', 'result', 'decided_by', 'decided_at'])
    audit.log(f'desk.action_{action.status}', action, actor=user, changes={'title': action.title, 'result': action.result})
    return action


def dismiss(action: DeskAction, user) -> DeskAction:
    if action.status != 'suggested':
        raise ActionError('This suggestion has already been handled.')
    action.status = 'dismissed'
    action.decided_by = user
    action.decided_at = timezone.now()
    action.save(update_fields=['status', 'decided_by', 'decided_at'])
    audit.log('desk.action_dismissed', action, actor=user, changes={'title': action.title})
    return action
