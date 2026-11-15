"""The analyst: understands a question, gathers only the numbers it needs, and writes the answer.

Built for a small free model, so there is no tool-calling loop. Plain keyword rules choose which sections of
`metrics.full_context` the question needs, Python assembles exactly those numbers, and the model gets one call with
one block of data. If the model is unavailable the same sections are written out by `builtin`, so the owner always
gets a complete answer.

Privacy: only aggregate numbers and staff first names go to the model. Client names are replaced by C1, C2... in the
data and swapped back in the reply, so no client detail leaves the server.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from django.utils import timezone

from crm.core import india, llm
from crm.models import DeskAction, DeskBriefing
from crm.modules.chatbot.analyst import _money

from . import metrics, plan, rules

logger = logging.getLogger('crm.desk')

# (intent, trigger phrases). A question can match several; "overview" is the default.
INTENTS = [
    ('roadmap', ('next month', 'roadmap', 'month plan', 'plan for', 'plan the month', 'monthly plan')),
    ('plan_gap', ('business plan', 'strategy', 'weak', 'strong', 'improve', 'gap', 'goal', 'target', 'on track', 'destination',
                  'strengthen', 'focus')),
    ('todo', ('what should', 'to do', 'todo', 'assign', 'priorit', 'action', 'need to do', 'what do i do', 'what do we do')),
    ('tomorrow', ('tomorrow', 'upcoming', 'next day', 'coming up')),
    ('team', ('delay', 'slow', 'late', 'team', 'staff', 'sales', 'who ', 'perform', 'executive', 'telecaller', 'person',
              'employee', 'going good', 'doing well', 'best')),
    ('customers', ('customer', 'client', 'churn', 'lose', 'losing', 'onboard', 'retention', 'at risk', 'at-risk')),
    ('revenue', ('revenue', 'money', 'collect', 'payment', 'invoice', 'cash', 'income', 'receivable', 'outstanding', 'earn')),
    ('delivery', ('deliver', 'pending', 'overdue', 'item', 'work done', 'workload')),
    ('leads', ('lead', 'pipeline', 'source', 'conversion', 'enquir')),
    ('compare', ('compare', 'yesterday', 'last week', 'growth', 'trend', 'down', 'change', 'versus', ' vs ')),
]
SUGGEST_FOR = {'todo', 'team', 'roadmap', 'plan_gap', 'delivery', 'customers'}


def route(question: str) -> list[str]:
    q = f' {question.lower()} '
    hits = [name for name, words in INTENTS if any(w in q for w in words)]
    return hits or ['overview']


# ------------------------------------------------------------------ client aliases
class Aliases:
    """C1, C2... for client names on the way to the model, and back again on the way out."""

    def __init__(self):
        self.to_alias: dict[str, str] = {}

    def tag(self, name: str) -> str:
        return self.to_alias.setdefault(name, f'C{len(self.to_alias) + 1}')

    def restore(self, text: str) -> str:
        back = {v: k for k, v in self.to_alias.items()}
        return re.sub(r'\bC\d+\b', lambda m: back.get(m.group(0), m.group(0)), text)


# ------------------------------------------------------------------ the data block for each intent
def _compare_block(ctx: dict, keys=None) -> dict:
    out = {}
    for k, c in ctx['compare'].items():
        if keys and k not in keys:
            continue
        label = c['label'] + (' (Rs)' if c['money'] else '')
        out[label] = {'today': c['today'], 'yesterday': c['yesterday'], 'daily_average_last_7_days': c['avg7'],
                      'change_vs_yesterday_pct': c['vs_yesterday'], 'change_vs_average_pct': c['vs_avg7']}
    return out


def _team_block(ctx: dict, full: bool) -> list[dict]:
    keys = ['name', 'role', 'status', 'reasons', 'leads_created', 'calls', 'followups_done', 'overdue_followups',
            'stale_leads', 'won', 'won_value', 'lost', 'collected', 'quotes_sent']
    if not full:
        return [{'name': p['name'], 'status': p['status'], 'reasons': p['reasons'], 'won': p['won'], 'leads_created': p['leads_created']}
                for p in ctx['team']]
    seven = {}
    try:
        from crm.modules.chatbot import analyst as sales_analyst
        seven = {p['name'].split()[0]: p for p in sales_analyst.snapshot(7)['people']}
    except Exception:
        logger.exception('7-day team snapshot failed')
    rows = []
    for p in ctx['team']:
        row = {k: p[k] for k in keys}
        s = seven.get(p['name'].split()[0])
        if s:
            row['last_7_days'] = {'win_rate_pct': s['win_rate_pct'], 'avg_first_response_minutes': s['avg_first_response_minutes'],
                                  'won_deals': s['won_deals'], 'quotations_sent': s['quotations_sent']}
        rows.append(row)
    return rows


def _delivery_block(ctx: dict, al: Aliases) -> dict:
    d = dict(ctx['delivery'])
    d['overdue_items'] = [{**i, 'client': al.tag(i['client'])} for i in d['overdue_items']]
    d['on_time_pct_30_days'] = ctx['funnel']['on_time_pct']
    return d


def _customers_block(ctx: dict, al: Aliases) -> dict:
    c = ctx['customers']
    return {'onboarded_today': [al.tag(n) for n in c['onboarded_today']],
            'contracts_activated_today': [al.tag(n) for n in c['contracts_activated_today']],
            'lost_today': [al.tag(n) for n in c['lost_today']],
            'new_clients_this_month': c['new_clients_month'], 'active_clients': c['active_clients'],
            'at_risk': [{'client': al.tag(r['client']), 'warning_signs': r['reasons']} for r in c['at_risk']]}


def build_data(intents: list[str], ctx: dict, al: Aliases) -> dict:
    """Only the sections the question needs. Kept small so a free model can read all of it."""
    data: dict = {'as_of': ctx['as_of']}
    want = set(intents)
    overview = 'overview' in want
    if overview:
        data['today_vs_before'] = _compare_block(ctx)
        data['delivery'] = _delivery_block(ctx, al)
        data['tomorrow'] = ctx['tomorrow']
        data['team'] = _team_block(ctx, full=False)
        data['customers'] = _customers_block(ctx, al)
    if 'compare' in want and not overview:
        data['today_vs_before'] = _compare_block(ctx)
    if 'team' in want:
        data['team'] = _team_block(ctx, full=True)
    if 'leads' in want:
        data['leads'] = ctx['leads']
        data['today_vs_before'] = _compare_block(ctx, ('leads_created', 'leads_won', 'leads_lost', 'won_value', 'quotes_sent', 'calls'))
    if 'revenue' in want:
        data['revenue'] = ctx['revenue']
        data['today_vs_before'] = {**data.get('today_vs_before', {}), **_compare_block(ctx, ('collected', 'invoiced'))}
    if 'delivery' in want and not overview:
        data['delivery'] = _delivery_block(ctx, al)
        data['tomorrow'] = ctx['tomorrow']
    if 'tomorrow' in want and 'tomorrow' not in data:
        data['tomorrow'] = ctx['tomorrow']
    if 'customers' in want and not overview:
        data['customers'] = _customers_block(ctx, al)
    if want & {'plan_gap', 'roadmap', 'todo'} or ctx['goals']:
        data['goals_this_month'] = ctx['goals']
    if want & {'plan_gap', 'roadmap'}:
        data['weak_areas'] = ctx['weak_areas']
        data['strengths'] = rules.strengths(ctx)
        data['funnel_last_90_days'] = ctx['funnel']
    if 'roadmap' in want:
        data['roadmap'] = rules.roadmap(ctx, date.fromisoformat(ctx['date']))
    if want & {'todo', 'overview'}:
        data['needs_attention'] = ctx['flags'][:8]
    if 'todo' in want:
        data['weak_areas'] = ctx['weak_areas'][:3]
        data['suggested_actions_waiting_for_approval'] = [a.title for a in
                                                          DeskAction.objects.filter(for_date=date.fromisoformat(ctx['date']), status='suggested')[:8]]
    return data


# ------------------------------------------------------------------ the model prompt
SYSTEM = """You are the private business analyst for the owner of a small digital-services company. You report to the owner. Staff are the sales team and the people who deliver client work.

Answer ONLY from the DATA below. Every number comes from the business database; never calculate new totals, never invent a number, reason or person. If the data does not contain the answer, say what is missing. Amounts are INR: write them as Rs 1,23,456. "C1", "C2" are client names: keep them exactly as written.

How to answer:
- Start with the answer in one sentence.
- Then 2 to 6 short bullets that quote the numbers, naming staff by first name. Compare with yesterday or the weekly average when the data has it.
- When asked about people, be fair and specific: say who is going well and who is delayed, and why, using their numbers.
- Give a possible reason only when the data supports it, and label it "possible reason".
- End with 1 to 3 concrete next actions when the question is about what to do, plans, weaknesses or goals.
- Plain text, simple "-" bullets, **bold** for names and key numbers, under 230 words unless asked for detail.
{plan}
DATA:
{data}
"""

BRIEF = {
    'morning': 'Write the MORNING briefing: what the owner should know and do today. Sections, each with 1 to 3 bullets: **Today at a glance**, **Who needs a push**, **Who is going well**, **Do today**, **Coming tomorrow**.',
    'evening': 'Write the EVENING briefing: how today went. Sections, each with 1 to 3 bullets: **Today at a glance**, **Against yesterday**, **Team**, **Customers**, **Tomorrow**, **Needs your attention**.',
    'manual': 'Write a short status briefing of the business right now. Sections, each with 1 to 3 bullets: **Where we stand**, **Against yesterday**, **Team**, **Needs your attention**.',
}


def _plan_block() -> str:
    text = plan.context_text()
    return f'\nThe owner\'s plan and priorities (use to judge what matters):\n{text}\n' if text else ''


def _ask_model(instruction: str, data: dict, history: list[dict], question: str) -> tuple[str, str]:
    system = SYSTEM.format(plan=_plan_block(), data=json.dumps(data, default=str, ensure_ascii=False))
    msgs = [{'role': 'system', 'content': system}]
    for h in history[-6:]:
        msgs.append({'role': 'user' if h.get('role') == 'user' else 'assistant', 'content': str(h.get('text', ''))[:1200]})
    msgs.append({'role': 'user', 'content': f'{instruction}\n\n{question}'.strip()})
    return llm.complete(msgs, max_tokens=800, temperature=0.2, timeout=35)


# ------------------------------------------------------------------ answers without the model
def _chg(c: dict) -> str:
    v = c['vs_yesterday']
    if v is None:
        return ''
    return f" ({'up' if v > 0 else 'down' if v < 0 else 'same'}{'' if v == 0 else f' {abs(v):.0f}%'} on yesterday)"


def _val(c: dict, key: str = 'today') -> str:
    return _money(c[key]) if c['money'] else f"{c[key]:g}"


def b_overview(ctx: dict) -> str:
    c = ctx['compare']
    dl = ctx['delivery']
    lines = [f"**Business today** (as of {ctx['as_of']})",
             f"- New leads: **{_val(c['leads_created'])}**{_chg(c['leads_created'])}. Won: **{_val(c['leads_won'])}** ({_val(c['won_value'])}). Lost: **{_val(c['leads_lost'])}**.",
             f"- Cash collected: **{_val(c['collected'])}**{_chg(c['collected'])}. Invoiced: **{_val(c['invoiced'])}**.",
             f"- Delivered today: **{dl['delivered_today']}** items. Still pending today: **{dl['pending_today']}**. Overdue: **{dl['overdue']}**.",
             f"- New clients: **{_val(c['clients_onboarded'])}**. Calls logged: **{_val(c['calls'])}**. Follow-ups done: **{_val(c['followups_done'])}**."]
    return '\n'.join(lines)


def b_attention(ctx: dict, limit: int = 8) -> str:
    if not ctx['flags']:
        return '**Needs attention**\n- Nothing urgent right now.'
    return '**Needs attention**\n' + '\n'.join(f"- {f['text']}" for f in ctx['flags'][:limit])


def b_team(ctx: dict) -> str:
    if not ctx['team']:
        return '**Team**\n- No sales users found yet.'
    order = {'red': 0, 'amber': 1, 'green': 2}
    lines = ['**Team today**']
    for p in sorted(ctx['team'], key=lambda p: (order[p['status']], -p['won_value'], p['name'])):
        tag = {'red': 'Delayed', 'amber': 'Watch', 'green': 'Good'}[p['status']]
        why = '; '.join(p['reasons']) or ('doing well' if p['doing_well'] else 'on track')
        lines.append(f"- **{p['name']}** ({tag}): {p['leads_created']} leads created, {p['calls']} calls, {p['followups_done']} follow-ups done, "
                     f"{p['won']} won ({_money(p['won_value'])}), {_money(p['collected'])} collected. {why}.")
    return '\n'.join(lines)


def b_customers(ctx: dict) -> str:
    c = ctx['customers']
    lines = ['**Customers**',
             f"- Onboarded today: {', '.join(c['onboarded_today']) or 'none'}. New this month: {c['new_clients_month']}.",
             f"- Contracts ended today: {', '.join(c['lost_today']) or 'none'}."]
    if c['at_risk']:
        lines.append('- Could be lost:')
        lines += [f"  - **{r['client']}**: " + '; '.join(r['reasons']) for r in c['at_risk'][:6]]
    else:
        lines.append('- No client shows warning signs.')
    return '\n'.join(lines)


def b_revenue(ctx: dict) -> str:
    r = ctx['revenue']
    lines = ['**Money**',
             f"- Collected today {_money(r['collected_today'])}; this month {_money(r['collected_month'])} "
             f"(same point last month {_money(r['collected_last_month_same_period'])}).",
             f"- Invoiced this month {_money(r['invoiced_month'])}. Outstanding {_money(r['outstanding'])}, of which "
             f"{_money(r['overdue_amount'])} is overdue ({r['overdue_invoices']} invoices; {_money(r['overdue_over_30d'])} over 30 days)."]
    if r['collected_month_by_owner']:
        lines.append('- Collected this month by account owner: ' + ', '.join(f'{n} {_money(v)}' for n, v in
                                                                              sorted(r['collected_month_by_owner'].items(), key=lambda x: -x[1])))
    return '\n'.join(lines)


def b_delivery(ctx: dict) -> str:
    d = ctx['delivery']
    lines = ['**Delivery**',
             f"- Delivered today {d['delivered_today']}; pending today {d['pending_today']}; overdue {d['overdue']}; "
             f"in review {d['in_review']}; waiting on clients {d['awaiting_client']}; missed in 30 days {d['missed_30d']}."]
    for p in d['by_person'][:6]:
        if p['overdue'] or p['pending_today'] or p['delivered']:
            lines.append(f"  - **{p['name']}**: {p['delivered']} delivered, {p['pending_today']} due today, {p['overdue']} overdue")
    for i in d['overdue_items'][:4]:
        lines.append(f"  - Late {i['days_late']}d: {i['title']} for {i['client']} ({i['person']})")
    return '\n'.join(lines)


def b_tomorrow(ctx: dict) -> str:
    t = ctx['tomorrow']
    lines = [f"**Tomorrow** ({t['date']})",
             f"- {t['deliverables_due']} deliverables due" + (f" ({', '.join(f'{n} {c}' for n, c in t['deliverables_by_person'].items())})" if t['deliverables_by_person'] else '') + '.',
             f"- {t['followups_due']} lead follow-ups scheduled.",
             f"- {t['invoices_to_raise']} recurring invoices to raise ({_money(t['invoices_to_raise_value'])}).",
             f"- {t['quotes_expiring']} quotations expire, {t['contracts_ending']} contracts end, {t['tasks_due']} tasks due."]
    return '\n'.join(lines)


def b_leads(ctx: dict) -> str:
    l = ctx['leads']
    lines = ['**Leads**',
             f"- Open {l['open']} (pipeline {_money(l['pipeline_value'])}); unassigned {l['unassigned_open']}; untouched 7+ days {l['stale_open']}; "
             f"follow-ups overdue {l['overdue_followups']}, due today {l['followups_due_today']}."]
    if l['created_today_by_person']:
        lines.append('- Created today by: ' + ', '.join(f'{n} {c}' for n, c in l['created_today_by_person'].items()))
    if l['created_today_by_source']:
        lines.append('- Today by source: ' + ', '.join(f'{n} {c}' for n, c in l['created_today_by_source'].items()))
    if l['lost_reasons_30d']:
        lines.append('- Lost in 30 days because: ' + ', '.join(f'{n} {c}' for n, c in l['lost_reasons_30d'].items()))
    return '\n'.join(lines)


def b_goals(ctx: dict) -> str:
    lines = ['**Goals this month**']
    if not ctx['goals']:
        lines.append('- No goals set yet. Add them under Goals and plan, or upload your business plan.')
    word = {'ahead': 'ahead', 'on_track': 'on track', 'at_risk': 'at risk', 'behind': 'behind'}
    for g in ctx['goals']:
        fmt = (lambda v: _money(v)) if g['money'] else (lambda v: f'{v:g}{"%" if g["percent_metric"] else ""}')
        need = f", needs {fmt(g['per_day_needed'])} a day" if g['per_day_needed'] and g['money'] else ''
        lines.append(f"- **{g['label']}**: {fmt(g['actual'])} of {fmt(g['target'])} ({g['pct']}%), {word[g['status']]}{need}.")
    return '\n'.join(lines)


def b_plan_gap(ctx: dict) -> str:
    lines = [b_goals(ctx), '', '**Areas to strengthen**']
    if ctx['weak_areas']:
        for w in ctx['weak_areas'][:5]:
            lines.append(f"- **{w['area']}**: {w['evidence']}. Fix: {w['fix']}")
    else:
        lines.append('- Nothing stands out as weak right now.')
    st = rules.strengths(ctx)
    if st:
        lines += ['', '**Going well**'] + [f'- {s}' for s in st[:6]]
    return '\n'.join(lines)


def b_roadmap(ctx: dict) -> str:
    r = rules.roadmap(ctx, date.fromisoformat(ctx['date']))
    lines = [f"**Plan for {r['month']}**", '**Targets**']
    for t in r['targets']:
        lines.append(f"- {t['label']}: **{_money(t['target']) if t['money'] else int(t['target'])}** ({t['basis']})")
    if r['per_person']:
        lines.append('**Per person**')
        lines += [f"- {p['name']}: {p['leads']} leads, {_money(p['won_value'])} won" for p in r['per_person']]
    lines.append('**Focus first**')
    lines += [f"- **{w['area']}**: {w['fix']}" for w in r['focus']] or ['- Keep the current routine.']
    return '\n'.join(lines)


def b_todo(ctx: dict) -> str:
    lines = ['**Do today**']
    todos = [f"- {f['text']}" for f in ctx['flags'] if f['level'] == 'red'][:4]
    todos += [f"- {w['fix']} ({w['area']})" for w in ctx['weak_areas'][:2]]
    lines += todos or ['- Nothing urgent. Keep follow-ups and deliveries on schedule.']
    return '\n'.join(lines)


BUILTIN = {
    'overview': lambda c: '\n\n'.join([b_overview(c), b_attention(c, 5), b_tomorrow(c)]),
    'compare': b_overview, 'team': b_team, 'customers': b_customers, 'revenue': b_revenue, 'delivery': b_delivery,
    'tomorrow': b_tomorrow, 'leads': b_leads, 'plan_gap': b_plan_gap, 'roadmap': b_roadmap,
    'todo': lambda c: b_todo(c) + '\n\n' + b_tomorrow(c),
}


def builtin(intents: list[str], ctx: dict) -> str:
    seen, blocks = set(), []
    for i in intents:
        text = BUILTIN[i](ctx)
        if text not in seen:
            seen.add(text)
            blocks.append(text)
    blocks.append('_Written by the built-in summary. The AI writer is not available right now, so these are the plain numbers._')
    return '\n\n'.join(blocks)


# ------------------------------------------------------------------ evidence shown under each answer
def evidence(intents: list[str], ctx: dict) -> list[str]:
    c, out = ctx['compare'], [f"Figures as of {ctx['as_of']}"]
    want = set(intents)
    if want & {'overview', 'compare', 'leads'}:
        out.append(f"Today: {_val(c['leads_created'])} leads, {_val(c['leads_won'])} won, {_val(c['collected'])} collected "
                   f"(yesterday {_val(c['leads_created'], 'yesterday')} leads, {_val(c['collected'], 'yesterday')} collected)")
    if want & {'team', 'overview'}:
        n = {s: sum(1 for p in ctx['team'] if p['status'] == s) for s in ('green', 'amber', 'red')}
        out.append(f"{len(ctx['team'])} salespeople: {n['green']} good, {n['amber']} watch, {n['red']} delayed")
    if want & {'delivery', 'overview', 'tomorrow'}:
        d = ctx['delivery']
        out.append(f"Delivery: {d['delivered_today']} delivered, {d['pending_today']} due today, {d['overdue']} overdue; "
                   f"{ctx['tomorrow']['deliverables_due']} due tomorrow")
    if want & {'revenue'}:
        r = ctx['revenue']
        out.append(f"Money: {_money(r['collected_month'])} collected this month, {_money(r['outstanding'])} outstanding")
    if want & {'customers'}:
        out.append(f"{len(ctx['customers']['at_risk'])} clients show warning signs; {ctx['customers']['new_clients_month']} new this month")
    if want & {'plan_gap', 'roadmap', 'todo'}:
        out.append(f"{len(ctx['goals'])} goals this month; {len(ctx['weak_areas'])} weak areas found; "
                   f"90-day win rate {ctx['funnel']['win_rate'] if ctx['funnel']['win_rate'] is not None else 'n/a'}%")
    return out


# ------------------------------------------------------------------ entry points
def pending_actions(d: date, limit: int = 6) -> list[dict]:
    return [{'id': a.pk, 'title': a.title, 'detail': a.detail, 'kind': a.kind, 'area': a.area,
             'who': (a.assignee.first_name or a.assignee.get_username()) if a.assignee else ''}
            for a in DeskAction.objects.filter(for_date=d, status='suggested').select_related('assignee')[:limit]]


def ask(question: str, history: list[dict] | None = None) -> dict:
    question = (question or '').strip()[:500]
    ctx = metrics.full_context()
    intents = route(question) if question else ['overview']
    d = date.fromisoformat(ctx['date'])
    if set(intents) & SUGGEST_FOR:
        rules.suggest_actions(ctx, d)
    result = {'intents': intents, 'evidence': evidence(intents, ctx),
              'suggestions': pending_actions(d) if set(intents) & SUGGEST_FOR else []}
    if not question:
        return {**result, 'answer': builtin(intents, ctx), 'engine': 'rules'}
    al = Aliases()
    data = build_data(intents, ctx, al)
    try:
        text, model = _ask_model('', data, history or [], question)
        return {**result, 'answer': al.restore(text), 'engine': model}
    except llm.LLMUnavailable:
        return {**result, 'answer': builtin(intents, ctx), 'engine': 'rules'}


def generate_briefing(kind: str = 'manual', d: date | None = None) -> DeskBriefing:
    """Write and store a briefing. Morning and evening ones are kept once a day; on-request ones are always new."""
    d = d or india.today()
    ctx = metrics.full_context(d, fresh=True)
    rules.suggest_actions(ctx, d)
    al = Aliases()
    data = build_data(['overview', 'todo', 'plan_gap'], ctx, al)
    data.pop('suggested_actions_waiting_for_approval', None)
    try:
        text, engine = _ask_model(BRIEF[kind], data, [], 'Write the briefing now.')
        text = al.restore(text)
    except llm.LLMUnavailable:
        text = builtin(['overview', 'team', 'todo'] if kind == 'morning' else ['overview', 'team', 'customers', 'tomorrow'], ctx)
        engine = 'rules'
    saved = {'numbers': ctx['numbers'], 'flags': ctx['flags'][:8], 'weak_areas': ctx['weak_areas'][:3]}
    if kind == 'manual':
        return DeskBriefing.objects.create(kind=kind, for_date=d, text=text, engine=engine, data=saved)
    obj, _ = DeskBriefing.objects.update_or_create(kind=kind, for_date=d, defaults={'text': text, 'engine': engine, 'data': saved,
                                                                                      'created_at': timezone.now()})
    return obj


def latest_briefing(d: date | None = None) -> DeskBriefing | None:
    d = d or india.today()
    return DeskBriefing.objects.filter(for_date=d).first() or DeskBriefing.objects.first()
