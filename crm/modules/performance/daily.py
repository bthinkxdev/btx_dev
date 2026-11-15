"""The daily report: what a person did today, short enough to paste into a chat and complete enough to need nothing else.

Content, in order: working time, then (for sales) leads, calls and meetings, follow-ups, wins, quotations, collections and
clients onboarded, then tasks and client deliverables. Lines with nothing to say are left out except the core sales lines,
because a zero there is information. Each report is frozen with a code; changing the day afterwards makes a new version."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.db.models import Count, Q, Sum
from django.utils import timezone

from crm.core import attribution, india, permissions
from crm.core.money import ZERO
from crm.models import DailyReport

logger = logging.getLogger('crm.dailyreport')

SALES_ROLES = ('sales_executive', 'sales_manager', 'telecaller')
ON_TIME_GRACE = timedelta(hours=1)
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
TOKEN_SALT = 'crm-daily-report'
TOKEN_MAX_AGE = 600
MAX_NAMES = 3


def is_sales(user) -> bool:
    return bool(permissions.has_role(user, *SALES_ROLES))


def _bounds(day: date):
    s = timezone.make_aware(datetime.combine(day, time.min))
    return s, s + timedelta(days=1)


def _money(v) -> str:
    from crm.templatetags.crm_tags import inr0
    return inr0(v)


def _names(items, limit: int = MAX_NAMES) -> str:
    items = [str(i) for i in items if i]
    extra = len(items) - limit
    return ', '.join(items[:limit]) + (f' +{extra} more' if extra > 0 else '')


# ---------------------------------------------------------------- the numbers

def build(user, day: date, now: datetime | None = None) -> dict:
    from crm.models import (Client, Deliverable, DeliverableAsset, FollowUp, Lead, LeadActivity, Quotation, Task)
    from crm.modules.worktime import services as wt
    from crm.models.leads import OPEN_STAGES
    now = now or timezone.now()
    s, e = _bounds(day)
    uid = [user.pk]
    t = wt.day_summary(user, day, now)
    d: dict = {
        'name': user.get_full_name() or user.get_username(), 'day': day.isoformat(), 'sales': is_sales(user),
        'time': {'in': timezone.localtime(t['first_in']).strftime('%H:%M') if t['first_in'] else None,
                 'out': timezone.localtime(t['last_out']).strftime('%H:%M') if t['last_out'] else None,
                 'worked': t['worked_text'], 'break': t['break_text'], 'breaks': t['break_count'], 'worked_seconds': t['worked']},
    }
    if d['sales']:
        created = Lead.objects.filter(created_by=user, created_at__gte=s, created_at__lt=e)
        assigned = Lead.objects.filter(owner=user, assigned_at__gte=s, assigned_at__lt=e)
        acts = LeadActivity.objects.filter(actor=user, occurred_at__gte=s, occurred_at__lt=e)
        kinds = {r['kind']: r['n'] for r in acts.values('kind').annotate(n=Count('id'))}
        fus_done = FollowUp.objects.filter(assignee=user, status='done', completed_at__gte=s, completed_at__lt=e)
        on_time = sum(1 for f in fus_done.only('due_at', 'completed_at') if f.completed_at <= f.due_at + ON_TIME_GRACE)
        skipped = FollowUp.objects.filter(assignee=user, status='cancelled', completed_at__gte=s, completed_at__lt=e).exclude(
            notes__in=['Lead won', 'Lead lost', 'Assignee offboarded'])
        pending = FollowUp.objects.filter(assignee=user, status='pending')
        won = Lead.objects.filter(attribution.won_q(uid), stage='won', won_at__gte=s, won_at__lt=e)
        quotes = Quotation.objects.filter(owner=user, sent_at__gte=s, sent_at__lt=e, version=1)
        paid = [(p.client.display_name, p.amount) for p in attribution_payments(user, day)]
        onboarded = Client.objects.filter(created_at__gte=s, created_at__lt=e).filter(
            Q(source_lead__credited_to=user) | Q(source_lead__credited_to__isnull=True, source_lead__owner=user) |
            Q(source_lead__isnull=True, owner=user))
        openq = Lead.objects.filter(owner=user, stage__in=OPEN_STAGES, archived_at__isnull=True)
        d['sales_data'] = {
            'leads_new': created.count(), 'leads_new_value': str(created.aggregate(v=Sum('value'))['v'] or ZERO),
            'leads_assigned': assigned.count(), 'leads_assigned_value': str(assigned.aggregate(v=Sum('value'))['v'] or ZERO),
            'calls': kinds.get('call', 0), 'connected': acts.filter(kind='call', outcome='connected').count(),
            'meetings': kinds.get('meeting', 0), 'messages': kinds.get('email', 0) + kinds.get('whatsapp', 0),
            'fu_done': fus_done.count(), 'fu_on_time': on_time, 'fu_skipped': skipped.count(), 'fu_pending': pending.count(),
            'fu_due_now': pending.filter(due_at__lt=e).count(),
            'won': won.count(), 'won_value': str(won.aggregate(v=Sum('won_value'))['v'] or ZERO),
            'lost': Lead.objects.filter(owner=user, stage='lost', lost_at__gte=s, lost_at__lt=e).count(),
            'quotes': quotes.count(), 'quotes_value': str(quotes.aggregate(v=Sum('grand_total'))['v'] or ZERO),
            'collected': str(sum((a for _, a in paid), ZERO)), 'collected_from': [f'{n} {_plain(a)}' for n, a in paid],
            'clients': onboarded.count(), 'client_names': [c.display_name for c in onboarded[:5]],
            'open_leads': openq.count(), 'open_value': str(openq.aggregate(v=Sum('value'))['v'] or ZERO),
        }
    tasks_done = Task.objects.filter(assignee=user, status='done', completed_at__gte=s, completed_at__lt=e).select_related('project')
    d['tasks'] = {
        'done': tasks_done.count(), 'titles': [t.title for t in tasks_done[:5]], 'projects': sorted({t.project.name for t in tasks_done}),
        'overdue': Task.objects.filter(assignee=user).exclude(status__in=['done', 'cancelled']).filter(due_date__lt=day + timedelta(days=1)).count(),
    }
    delivered = Deliverable.objects.filter(delivered_by=user, delivered_at__gte=s, delivered_at__lt=e).select_related('client')
    kinds_done: dict[str, int] = {}
    for x in delivered:
        key = x.category or x.title
        kinds_done[key] = kinds_done.get(key, 0) + max(x.quantity_done, 1)
    open_states = ['upcoming', 'in_progress', 'in_review', 'awaiting_client']
    d['deliverables'] = {
        'delivered': delivered.count(), 'by_kind': kinds_done, 'clients': sorted({x.client.display_name for x in delivered}),
        'files': DeliverableAsset.objects.filter(added_by=user, created_at__gte=s, created_at__lt=e).count(),
        'pending': Deliverable.objects.filter(assignee=user, status__in=open_states, due_date__lte=day).count(),
        'missed': Deliverable.objects.filter(assignee=user, missed_at__gte=s, missed_at__lt=e).count(),
    }
    return d


def _plain(v) -> str:
    return f'{Decimal(v):,.0f}'


def attribution_payments(user, day: date):
    from crm.models import Payment
    qs = Payment.objects.filter(status__in=attribution.COUNTED, payment_date=day).select_related('client__source_lead')
    return [p for p in qs if attribution.payment_credit_id(p) == user.pk]


# ---------------------------------------------------------------- the words

def render(d: dict) -> str:
    day = date.fromisoformat(d['day'])
    t = d['time']
    when = (f"In {t['in']} | Out {t['out'] or 'still working'} | Worked {t['worked']} | Break {t['break']}" if t['in']
            else 'No login recorded')
    out = [f"DAILY REPORT | {d['name']}", f'{day:%a %d %b %Y}', when]
    s = d.get('sales_data')
    if s:
        out += ['', 'SALES',
                f"Leads: {s['leads_new']} new ({_money(Decimal(s['leads_new_value']))}), {s['leads_assigned']} assigned ({_money(Decimal(s['leads_assigned_value']))})",
                f"Calls: {s['calls']} ({s['connected']} connected) | Meetings {s['meetings']} | Messages {s['messages']}",
                f"Follow-ups: {s['fu_done']} done ({s['fu_on_time']} on time), {s['fu_skipped']} skipped | {s['fu_pending']} pending ({s['fu_due_now']} due now)",
                f"Won: {s['won']} ({_money(Decimal(s['won_value']))})" + (f" | Lost {s['lost']}" if s['lost'] else ''),
                f"Collected: {_money(Decimal(s['collected']))}" + (f" ({_names(s['collected_from'])})" if s['collected_from'] else '')]
        if s['quotes']:
            out.append(f"Quotations sent: {s['quotes']} ({_money(Decimal(s['quotes_value']))})")
        if s['clients']:
            out.append(f"Clients onboarded: {s['clients']} ({_names(s['client_names'])})")
        out.append(f"Pipeline: {s['open_leads']} open leads, {_money(Decimal(s['open_value']))}")
    k = d['tasks']
    if k['done'] or k['overdue']:
        line = f"Tasks done: {k['done']}" + (f" ({_names(k['titles'])})" if k['titles'] else '')
        if k['projects']:
            line += f" | Projects: {_names(k['projects'])}"
        if k['overdue']:
            line += f" | Overdue {k['overdue']}"
        out += ['', 'PROJECTS AND TASKS', line]
    q = d['deliverables']
    if q['delivered'] or q['pending'] or q['files'] or q['missed']:
        kinds = ', '.join(f'{n} {name}' for name, n in q['by_kind'].items())
        line = f"Delivered: {q['delivered']}" + (f' - {kinds}' if kinds else '') + (f" ({_names(q['clients'])})" if q['clients'] else '')
        extra = [f"Files added {q['files']}" if q['files'] else '', f"Pending {q['pending']}" if q['pending'] else '',
                 f"Missed {q['missed']}" if q['missed'] else '']
        out += ['', 'CLIENT DELIVERABLES', ' | '.join([line] + [x for x in extra if x])]
    return '\n'.join(out)


# ---------------------------------------------------------------- codes and the frozen copy

def _check(user_id, day: date, seq: int, digest: str, rand: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f'{user_id}|{day}|{seq}|{digest}|{rand}'.encode(), hashlib.sha256).hexdigest()[:2].upper()


def _new_code(user, day: date, seq: int, digest: str) -> str:
    while True:
        rand = ''.join(secrets.choice(ALPHABET) for _ in range(4))
        code = f'DR{day:%y%m%d}-{rand}-{_check(user.pk, day, seq, digest, rand)}'
        if not DailyReport.objects.filter(code=code).exists():
            return code


def generate(user, day: date | None = None, now: datetime | None = None) -> DailyReport | None:
    """Freeze the report for this person and day. If nothing changed since the last one, that one is returned."""
    from crm.modules.worktime import services as wt
    if not wt.tracked(user):
        return None
    now = now or timezone.now()
    day = day or wt.local_day(now)
    data = build(user, day, now)
    body = render(data)
    digest = hashlib.sha256(body.encode()).hexdigest()
    last = DailyReport.objects.filter(user=user, day=day).order_by('-seq').first()
    if last is not None and last.digest == digest:
        return last
    seq = (last.seq if last else 0) + 1
    code = _new_code(user, day, seq, digest)
    return DailyReport.objects.create(user=user, day=day, seq=seq, code=code, text=f'{body}\nCode: {code}', data=data, digest=digest)


def verify(report: DailyReport) -> bool:
    """True when the stored report still matches the code it was issued under: nothing in it was altered afterwards."""
    body = report.text.rsplit('\nCode: ', 1)[0]
    if hashlib.sha256(body.encode()).hexdigest() != report.digest:
        return False
    parts = report.code.split('-')
    return len(parts) == 3 and _check(report.user_id, report.day, report.seq, report.digest, parts[1]) == parts[2]


def same_text(report: DailyReport, pasted: str) -> bool:
    norm = lambda t: '\n'.join(line.strip() for line in (t or '').replace('\r', '').strip().split('\n') if line.strip())
    return norm(report.text) == norm(pasted)


def token_for(report: DailyReport) -> str:
    return signing.dumps({'r': report.pk}, salt=TOKEN_SALT)


def report_from_token(token: str) -> DailyReport | None:
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None
    return DailyReport.objects.filter(pk=data.get('r')).select_related('user').first()


def mark_shown(report: DailyReport) -> None:
    DailyReport.objects.filter(pk=report.pk, shown_at__isnull=True).update(shown_at=timezone.now())


def pending_for(user) -> DailyReport | None:
    """An earlier day's report the person has never been shown (their day was closed for them, not by them)."""
    from crm.modules.worktime import services as wt
    if not wt.tracked(user):
        return None
    return (DailyReport.objects.filter(user=user, shown_at__isnull=True, day__lt=india.today()).order_by('-day', '-seq').first())
