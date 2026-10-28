"""AI sales analyst: summarises each sales executive's and manager's numbers and answers questions.

Only aggregate numbers and staff first names go to the AI model; no customer names, phones or emails.
Without an API key (or when the free quota is used up) a built-in leaderboard summary is shown instead.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from crm.core import llm
from crm.core.money import ZERO
from crm.models import Lead, LeadActivity, Payment, Quotation
from crm.models.leads import OPEN_STAGES

SALES_ROLES = ('sales_manager', 'sales_executive', 'telecaller')
PERIODS = {'7': 'Last 7 days', '30': 'Last 30 days', '90': 'Last 90 days'}
STALE_DAYS = 7


def _num(v):
    return float(v) if isinstance(v, Decimal) else v


def sales_people():
    User = get_user_model()
    return (User.objects.filter(is_active=True, crm_profile__roles__code__in=SALES_ROLES).distinct()
            .select_related('crm_profile').order_by('first_name', 'username'))


def snapshot(days: int = 30) -> dict:
    """Per-person performance for the last `days` days, plus team totals."""
    now = timezone.now()
    since = now - timedelta(days=days)
    stale_before = now - timedelta(days=STALE_DAYS)
    people = []
    for u in sales_people():
        codes = {r.code for r in u.crm_profile.roles.all()}
        mine = Lead.objects.filter(owner=u, archived_at__isnull=True)
        won = Lead.objects.filter(owner=u, stage='won', won_at__gte=since)
        lost = Lead.objects.filter(owner=u, stage='lost', lost_at__gte=since)
        won_n, lost_n = won.count(), lost.count()
        acts = LeadActivity.objects.filter(actor=u, occurred_at__gte=since)
        frt = Lead.objects.filter(owner=u, first_response_seconds__isnull=False, created_at__gte=since) \
            .aggregate(a=Avg('first_response_seconds'))['a']
        people.append({
            'name': u.get_full_name() or u.username,
            'role': 'Sales manager' if 'sales_manager' in codes else ('Telecaller' if codes == {'telecaller'} else 'Sales executive'),
            'leads_assigned': Lead.objects.filter(owner=u, assigned_at__gte=since).count(),
            'open_leads': mine.filter(stage__in=OPEN_STAGES).count(),
            'stale_open_leads': mine.filter(stage__in=OPEN_STAGES, last_activity_at__lt=stale_before).count(),
            'won_deals': won_n,
            'won_value_inr': _num(won.aggregate(s=Sum('won_value'))['s'] or ZERO),
            'lost_deals': lost_n,
            'win_rate_pct': round(won_n * 100 / (won_n + lost_n), 1) if (won_n + lost_n) else None,
            'activities_logged': acts.count(),
            'calls_logged': acts.filter(kind='call').count(),
            'quotations_sent': Quotation.objects.filter(owner=u, sent_at__gte=since).count(),
            'followups_done': u.crm_follow_ups.filter(status='done', completed_at__gte=since).count(),
            'followups_overdue_now': u.crm_follow_ups.filter(status='pending', due_at__lt=now).count(),
            'avg_first_response_minutes': int(frt // 60) if frt else None,
            'collected_inr': _num(Payment.objects.filter(status__in=['verified', 'partially_refunded'], client__owner=u,
                                                         payment_date__gte=since.date()).aggregate(s=Sum('amount'))['s'] or ZERO),
        })
    team = {
        'new_leads': Lead.objects.filter(created_at__gte=since).count(),
        'unassigned_open_leads': Lead.objects.filter(owner__isnull=True, stage__in=OPEN_STAGES, archived_at__isnull=True).count(),
        'won_deals': sum(p['won_deals'] for p in people),
        'won_value_inr': sum(p['won_value_inr'] for p in people),
        'leads_by_source': {r['source__name']: r['n'] for r in
                            Lead.objects.filter(created_at__gte=since).values('source__name').annotate(n=Count('id')).order_by('-n')[:8]},
    }
    return {'period_days': days, 'as_of': timezone.localtime(now).strftime('%d %b %Y %H:%M'), 'people': people, 'team': team}


SYSTEM = """You are the sales performance analyst inside the BThinkX CRM. You are talking to the owner or the sales manager.

Answer ONLY from the JSON data provided (aggregate numbers per salesperson; amounts are INR). If something is not in the data, say so; never invent numbers or reasons. Be specific: name the people and quote their numbers. Compare fairly: a person with few leads has a noisy win rate, say so. Lead with the answer, then 2-5 short bullets of evidence, then 1-3 concrete coaching actions when asked or useful. Be constructive and fair about people; focus on behaviours that can be improved (response time, follow-up discipline, calls, quotations, stale leads). Keep it under 220 words unless asked for detail. Use plain text with simple "-" bullets and **bold** for names and key numbers. Format INR as Rs 1,23,456.

DATA:
{data}
"""


def _money(v) -> str:
    n = int(round(v or 0))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ','.join(parts + [tail])
    return f'Rs {"-" if n < 0 else ""}{s}'


def builtin_summary(snap: dict) -> str:
    people = snap['people']
    if not people:
        return 'No sales users found yet.'
    days = snap['period_days']
    ranked = sorted(people, key=lambda p: (p['won_value_inr'], p['won_deals'], p['activities_logged']), reverse=True)
    lines = [f"**Sales performance, last {days} days** (as of {snap['as_of']})"]
    for i, p in enumerate(ranked, 1):
        wr = f"{p['win_rate_pct']}% win rate" if p['win_rate_pct'] is not None else 'no closed deals'
        fr = f", first response {p['avg_first_response_minutes']} min" if p['avg_first_response_minutes'] is not None else ''
        lines.append(f"{i}. **{p['name']}** ({p['role']}): {p['won_deals']} won ({_money(p['won_value_inr'])}), {wr}, "
                     f"{p['calls_logged']} calls, {p['quotations_sent']} quotes{fr}.")
    flags = []
    for p in people:
        if p['followups_overdue_now'] >= 5:
            flags.append(f"**{p['name']}** has {p['followups_overdue_now']} overdue follow-ups.")
        if p['stale_open_leads'] >= 5:
            flags.append(f"**{p['name']}** has {p['stale_open_leads']} open leads with no activity for {STALE_DAYS}+ days.")
        if p['avg_first_response_minutes'] and p['avg_first_response_minutes'] > 60:
            flags.append(f"**{p['name']}** takes {p['avg_first_response_minutes']} min on average to respond to a new lead.")
    if snap['team']['unassigned_open_leads']:
        flags.append(f"{snap['team']['unassigned_open_leads']} open leads are not assigned to anyone.")
    if flags:
        lines.append('\n**Needs attention**')
        lines += [f'- {f}' for f in flags]
    lines.append('\n_Built-in summary. Connect the free AI key to ask questions in plain English._')
    return '\n'.join(lines)


def ask(question: str, days: int, history: list[dict]) -> dict:
    snap = snapshot(days)
    question = (question or '').strip()[:500]
    if not question:
        return {'answer': builtin_summary(snap), 'engine': 'rules'}
    msgs = [{'role': 'system', 'content': SYSTEM.format(data=json.dumps(snap, default=str))}]
    for h in history[-8:]:
        role = 'user' if h.get('role') == 'user' else 'assistant'
        msgs.append({'role': role, 'content': str(h.get('text', ''))[:1500]})
    msgs.append({'role': 'user', 'content': question})
    try:
        text, model = llm.complete(msgs, max_tokens=700, temperature=0.2, timeout=30)
        return {'answer': text, 'engine': model}
    except llm.LLMUnavailable:
        return {'answer': builtin_summary(snap), 'engine': 'rules'}
