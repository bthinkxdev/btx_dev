"""Sales performance: every number on a person's profile, worked out from the records themselves.

Definitions, in one place so a number can always be explained:

* **Won revenue** counts for the person the win is *credited to* (the owner when the owner closed it, or whoever the closer
  chose). Older wins fall back to the owner.
* **Collected** is verified payments in the period, credited to the person who gets credit for the lead the client came
  from, else the client's owner.
* **Follow-up skipped** = cancelled by a person (not because the lead was won or lost) or still pending more than 24 hours
  after it was due. **On time** = done within an hour of the due time.
* **Not logged in** = a working day (Monday to Friday unless HR changed it, minus holidays) with no login, no manual
  attendance and no approved leave, from the person's joining date.
* **Targets** are monthly. For a week or a custom range they are pro-rated by working days.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from crm.core import attribution, india
from crm.core.money import ZERO
from crm.models import (Attendance, Client, Deliverable, Employee, FollowUp, Lead, LeadActivity, LeaveRequest, Payment, Quotation,
                        SalesTarget, StaffDayStat, Task, Team, UserProfile, WorkPeriod)
from crm.models.leads import OPEN_STAGES, STAGES

FIRST_RESPONSE_OK_MINUTES = 60
ON_TIME_GRACE = timedelta(hours=1)
SKIP_AFTER = timedelta(hours=24)
STALE_DAYS = 3
FUNNEL = ['new', 'contacted', 'qualified', 'quotation_prepared', 'quotation_sent', 'negotiation', 'won']
STAGE_LABELS = dict(STAGES)

# thresholds for the "needs attention" strip
BEHIND_PACE = Decimal('0.70')
LONG_BREAK_MINUTES = 90
SKIPPED_BAD = 5


# ---------------------------------------------------------------- periods

@dataclass
class Period:
    key: str
    label: str
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def bounds(self) -> tuple[datetime, datetime]:
        return (timezone.make_aware(datetime.combine(self.start, time.min)),
                timezone.make_aware(datetime.combine(self.end + timedelta(days=1), time.min)))

    def previous(self) -> 'Period | None':
        if self.key == 'all':
            return None
        if self.key in ('month', 'last_month') or (self.key == 'pick_month'):
            first = self.start.replace(day=1)
            pend = first - timedelta(days=1)
            return Period('previous', pend.strftime('%B %Y'), pend.replace(day=1), pend)
        if self.key in ('year', 'last_year', 'pick_year'):
            y = self.start.year - 1
            return Period('previous', str(y), date(y, 1, 1), date(y, 12, 31))
        length = self.days
        pend = self.start - timedelta(days=1)
        return Period('previous', 'the period before', pend - timedelta(days=length - 1), pend)

    def months(self) -> list[date]:
        out, d = [], self.start.replace(day=1)
        while d <= self.end:
            out.append(d)
            d = india.add_months(d, 1)
        return out

    @property
    def is_current(self) -> bool:
        return self.start <= india.today() <= self.end


def period_from(params, today: date | None = None, earliest: date | None = None) -> Period:
    """The period asked for: month picker, year picker, a preset, or custom dates. Years are January to December."""
    today = today or india.today()
    if params.get('month'):
        try:
            d = date.fromisoformat(params['month'] + '-01')
            end = min(india.add_months(d, 1) - timedelta(days=1), today) if d.replace(day=1) == today.replace(day=1) else india.add_months(d, 1) - timedelta(days=1)
            return Period('pick_month', d.strftime('%B %Y'), d, end)
        except ValueError:
            pass
    if params.get('year'):
        try:
            y = int(params['year'])
            if 2000 <= y <= today.year:
                return Period('pick_year', str(y), date(y, 1, 1), min(date(y, 12, 31), today) if y == today.year else date(y, 12, 31))
        except ValueError:
            pass
    key = params.get('range', '')
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    last_month_end = first - timedelta(days=1)
    presets = {
        'today': ('Today', today, today),
        'week': ('This week', monday, today),
        'last_week': ('Last week', monday - timedelta(days=7), monday - timedelta(days=1)),
        'month': ('This month', first, today),
        'last_month': ('Last month', last_month_end.replace(day=1), last_month_end),
        'quarter': ('This quarter', quarter_start, today),
        'year': (f'{today.year}', date(today.year, 1, 1), today),
        'last_year': (f'{today.year - 1}', date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
        'all': ('All time', earliest or date(today.year, 1, 1), today),
    }
    if key in presets:
        label, s, e = presets[key]
        return Period(key, label, s, e)
    if params.get('from') or params.get('to'):
        try:
            e = min(date.fromisoformat(params['to']), today) if params.get('to') else today
            s = min(date.fromisoformat(params['from']), e) if params.get('from') else e - timedelta(days=29)
            if (e - s).days > 3700:
                s = e - timedelta(days=3700)
            return Period('custom', f'{s:%d %b %Y} to {e:%d %b %Y}', s, e)
        except ValueError:
            pass
    label, s, e = presets['month']
    return Period('month', label, s, e)


PRESETS = [('today', 'Today'), ('week', 'This week'), ('last_week', 'Last week'), ('month', 'This month'), ('last_month', 'Last month'),
           ('quarter', 'This quarter'), ('year', 'This year'), ('last_year', 'Last year'), ('all', 'All time')]


def earliest_day(uids) -> date:
    """The first day there is anything to report for these people, for the All time view."""
    today = india.today()
    days = []
    for qs, f in ((Lead.objects.filter(owner_id__in=uids), 'created_at'), (WorkPeriod.objects.filter(user_id__in=uids), 'day'),
                  (FollowUp.objects.filter(assignee_id__in=uids), 'due_at'), (SalesTarget.objects.filter(user_id__in=uids), 'month')):
        v = qs.order_by(f).values_list(f, flat=True).first()
        if v:
            days.append(v.date() if hasattr(v, 'date') else v)
    for e in Employee.objects.filter(user_id__in=uids):
        days.append(e.joining_date)
    return min(days) if days else today


# ---------------------------------------------------------------- who is on whose team

def team_members(manager) -> list:
    """The people a sales manager leads: the teams they manage plus anyone who reports to them. Never themselves."""
    from django.contrib.auth import get_user_model
    ids = set(UserProfile.objects.filter(team__manager=manager, team__is_active=True).values_list('user_id', flat=True))
    ids |= set(UserProfile.objects.filter(manager=manager).values_list('user_id', flat=True))
    ids.discard(manager.pk)
    return list(get_user_model().objects.filter(pk__in=ids, is_active=True).select_related('crm_profile').order_by('first_name', 'username'))


def managed_teams(manager):
    return list(Team.objects.filter(manager=manager, is_active=True))


# ---------------------------------------------------------------- the numbers

def _pct(a, b):
    return int(Decimal(a) * 100 / Decimal(b)) if b else None


def _credited(qs_field_prefix, uids):
    return attribution.won_q(uids, qs_field_prefix)


def targets_for(uids, p: Period, team_ids=()) -> dict:
    """Targets across the period, summed over the people (or a team target when one is set), pro-rated by working days."""
    out = {k: ZERO for k in ('leads', 'quotations', 'clients', 'calls', 'meetings', 'won', 'collected')}
    has = False
    for m in p.months():
        m_end = india.add_months(m, 1) - timedelta(days=1)
        s, e = max(p.start, m), min(p.end, m_end)
        total = len(india.working_days_list(m, m_end)) or 1
        share = Decimal(len(india.working_days_list(s, e))) / Decimal(total)
        rows = list(SalesTarget.objects.filter(month=m, user_id__in=uids))
        team_rows = list(SalesTarget.objects.filter(month=m, team_id__in=team_ids)) if team_ids else []
        use = team_rows or rows
        for t in use:
            has = True
            out['leads'] += Decimal(t.lead_count) * share
            out['quotations'] += Decimal(t.quotation_count) * share
            out['clients'] += Decimal(t.client_count) * share
            out['calls'] += Decimal(t.call_count) * share
            out['meetings'] += Decimal(t.meeting_count) * share
            out['won'] += t.won_value * share
            out['collected'] += t.collected_value * share
    out['has'] = has
    return out


def compute(uids, p: Period, now: datetime | None = None, team_ids=()) -> dict:
    """Everything for these people over the period. One person for an individual profile, several for a team."""
    uids = list(uids)
    now = now or timezone.now()
    today = india.today()
    s_dt, e_dt = p.bounds()
    m: dict = {'uids': uids, 'period': p}

    # --- leads and revenue
    cohort = Lead.objects.filter(owner_id__in=uids).filter(Q(assigned_at__gte=s_dt, assigned_at__lt=e_dt) |
                                                          Q(assigned_at__isnull=True, created_at__gte=s_dt, created_at__lt=e_dt))
    m['leads'] = cohort.count()
    won_qs = Lead.objects.filter(stage='won', won_at__gte=s_dt, won_at__lt=e_dt).filter(_credited('', uids))
    won_rows = list(won_qs.values('won_value', 'won_at', 'assigned_at', 'created_at'))
    m['won'] = len(won_rows)
    m['won_value'] = sum((r['won_value'] or ZERO for r in won_rows), ZERO)
    lost = Lead.objects.filter(stage='lost', lost_at__gte=s_dt, lost_at__lt=e_dt, owner_id__in=uids)
    m['lost'] = lost.count()
    m['win_rate'] = _pct(m['won'], m['won'] + m['lost'])
    m['avg_deal'] = (m['won_value'] / m['won']) if m['won'] else None
    cycles = [(r['won_at'] - (r['assigned_at'] or r['created_at'])).days for r in won_rows]
    m['days_to_close'] = round(sum(cycles) / len(cycles), 1) if cycles else None
    m['lost_reasons'] = [{'label': dict(Lead._meta.get_field('lost_reason').choices).get(r['lost_reason'], r['lost_reason']), 'n': r['n']}
                         for r in lost.values('lost_reason').annotate(n=Count('id')).order_by('-n')]
    q = Quotation.objects.filter(owner_id__in=uids, sent_at__gte=s_dt, sent_at__lt=e_dt, version=1)
    m['quotes'] = q.count()
    m['quotes_value'] = q.aggregate(s=Sum('grand_total'))['s'] or ZERO
    clients = Client.objects.filter(created_at__gte=s_dt, created_at__lt=e_dt).filter(
        Q(source_lead__credited_to_id__in=uids) | Q(source_lead__credited_to__isnull=True, source_lead__owner_id__in=uids) |
        Q(source_lead__isnull=True, owner_id__in=uids))
    m['clients'] = clients.count()
    m['collected'] = attribution.collected_total(uids, p.start, p.end)

    # --- pipeline right now, and the funnel of the cohort
    openq = Lead.objects.filter(owner_id__in=uids, stage__in=OPEN_STAGES, archived_at__isnull=True)
    m['open_leads'] = openq.count()
    m['open_value'] = openq.aggregate(s=Sum('value'))['s'] or ZERO
    m['weighted'] = sum((l.value * l.probability / 100 for l in openq.only('value', 'probability')), ZERO)
    m['stale'] = openq.filter(Q(last_activity_at__lt=now - timedelta(days=STALE_DAYS)) | Q(last_activity_at__isnull=True,
                                                                                         created_at__lt=now - timedelta(days=STALE_DAYS))).count()
    total = m['leads']
    funnel, prev = [], None
    for st in FUNNEL:
        n = total if st == 'new' else cohort.filter(Q(stage_history__to_stage=st) | Q(stage=st)).distinct().count()
        funnel.append({'stage': st, 'label': STAGE_LABELS[st], 'count': n, 'conv': round(n * 100 / prev, 1) if prev else None,
                       'width': int(n * 100 / total) if total else 0})
        prev = n or prev
    m['funnel'] = funnel
    frt = Lead.objects.filter(owner_id__in=uids, first_response_seconds__isnull=False, assigned_at__gte=s_dt, assigned_at__lt=e_dt)
    avg = frt.aggregate(a=Avg('first_response_seconds'))['a']
    m['first_response_min'] = int(avg // 60) if avg else None
    m['slow_first_response'] = frt.filter(first_response_seconds__gt=FIRST_RESPONSE_OK_MINUTES * 60).count()

    # --- activity
    acts = LeadActivity.objects.filter(actor_id__in=uids, occurred_at__gte=s_dt, occurred_at__lt=e_dt)
    by_kind = {r['kind']: r['n'] for r in acts.values('kind').annotate(n=Count('id'))}
    m['calls'], m['meetings'] = by_kind.get('call', 0), by_kind.get('meeting', 0)
    m['messages'] = by_kind.get('email', 0) + by_kind.get('whatsapp', 0)
    m['notes'] = by_kind.get('note', 0)
    m['calls_connected'] = acts.filter(kind='call', outcome='connected').count()
    m['call_minutes'] = acts.filter(kind='call').aggregate(s=Sum('duration_minutes'))['s'] or 0

    # --- follow-ups
    fus = FollowUp.objects.filter(assignee_id__in=uids, due_at__gte=s_dt, due_at__lt=e_dt)
    system_closed = Q(status='cancelled', notes__in=['Lead won', 'Lead lost'])
    counted = fus.exclude(system_closed)
    done = counted.filter(status='done')
    on_time = [f for f in done.only('due_at', 'completed_at') if f.completed_at and f.completed_at <= f.due_at + ON_TIME_GRACE]
    skipped_cancelled = counted.filter(status='cancelled').count()
    skipped_pending = counted.filter(status='pending', due_at__lt=now - SKIP_AFTER).count()
    m['fu_due'] = counted.count()
    m['fu_done'] = done.count()
    m['fu_on_time'] = len(on_time)
    m['fu_late'] = m['fu_done'] - len(on_time)
    m['fu_skipped'] = skipped_cancelled + skipped_pending
    m['fu_overdue'] = counted.filter(status='pending', due_at__lt=now, due_at__gte=now - SKIP_AFTER).count()
    m['fu_postponed'] = counted.aggregate(s=Sum('postpone_count'))['s'] or 0
    m['fu_compliance'] = _pct(m['fu_on_time'], m['fu_due'])

    # --- tasks and deliverables
    tasks = Task.objects.filter(assignee_id__in=uids, due_date__gte=p.start, due_date__lte=p.end)
    m['tasks'] = tasks.exclude(status='cancelled').count()
    m['tasks_done'] = tasks.filter(status='done').count()
    m['tasks_overdue'] = tasks.exclude(status__in=['done', 'cancelled']).filter(due_date__lt=today).count()
    m['tasks_cancelled'] = tasks.filter(status='cancelled').count()
    m['tasks_late'] = sum(1 for t in tasks.filter(status='done', completed_at__isnull=False).only('due_date', 'completed_at')
                          if timezone.localtime(t.completed_at).date() > t.due_date)
    dl = Deliverable.objects.filter(assignee_id__in=uids, due_date__gte=p.start, due_date__lte=p.end).exclude(status='cancelled')
    m['dl'] = dl.count()
    delivered = dl.filter(status='delivered')
    m['dl_done'] = delivered.count()
    m['dl_late'] = sum(1 for d in delivered.only('delivered_at', 'original_due_date') if d.was_late_against_contract)
    m['dl_skipped'] = dl.filter(status='skipped').count()
    m['dl_missed'] = dl.filter(missed_at__isnull=False).count()
    m['dl_rescheduled'] = dl.filter(reschedule_count__gt=0).count()
    m['dl_overdue'] = sum(1 for d in dl.filter(status__in=['upcoming', 'in_progress', 'in_review', 'awaiting_client']) if d.is_overdue)

    from crm.modules.clients import health
    m['quiet_clients'] = len(health.quiet_clients(uids))

    # --- nightly facts
    stats = StaffDayStat.objects.filter(user_id__in=uids, day__gte=p.start, day__lte=p.end)
    m['auto_scheduled'] = stats.aggregate(s=Sum('auto_scheduled'))['s'] or 0

    m.update(time_and_presence(uids, p, now))
    m['targets'] = targets_for(uids, p, team_ids)
    m['rows'] = achievement_rows(m, p, today)
    return m


def time_and_presence(uids, p: Period, now: datetime) -> dict:
    """Working time, breaks, days not logged in, leave and punctuality."""
    from crm.modules.worktime import services as wt
    today = india.today()
    last = min(p.end, today)
    data = wt.range_summaries(uids, p.start, p.end, now)
    worked = brk = days = late = breaks_n = 0
    longest = 0
    logins, logouts = [], []
    absent_days: list[date] = []
    leave_days = Decimal('0')
    leave_by_type: dict[str, Decimal] = defaultdict(Decimal)
    emps = {e.user_id: e for e in Employee.objects.filter(user_id__in=uids).select_related('shift')}
    for uid in uids:
        mine = {d: s for d, s in data.get(uid, {}).items() if s['periods']}
        emp = emps.get(uid)
        for d, s in mine.items():
            worked += s['worked']
            brk += s['break_seconds']
            breaks_n += s['break_count']
            longest = max(longest, s['longest_break'])
            if s['first_in']:
                logins.append(timezone.localtime(s['first_in']))
            if s['last_out']:
                logouts.append(timezone.localtime(s['last_out']))
            if emp and emp.shift_id and s['first_in']:
                limit = datetime.combine(d, emp.shift.start_time) + timedelta(minutes=emp.shift.grace_minutes)
                if timezone.localtime(s['first_in']).replace(tzinfo=None) > limit:
                    late += 1
        days += len(mine)
        # days that should have been worked
        start = max(p.start, emp.joining_date) if emp else p.start
        should = india.working_days_list(start, last) if last >= start else []
        covered = set(mine)
        if emp:
            covered |= set(Attendance.objects.filter(employee=emp, att_date__range=(start, max(last, start)),
                                                     status__in=['present', 'half_day', 'wfh', 'leave', 'holiday', 'weekly_off'])
                           .values_list('att_date', flat=True))
            for lr in LeaveRequest.objects.filter(employee=emp, status='approved', start_date__lte=p.end, end_date__gte=p.start).select_related('leave_type'):
                span = india.working_days_list(max(lr.start_date, p.start), min(lr.end_date, p.end))
                covered |= set(span)
                n = Decimal(len(span))
                leave_days += n
                leave_by_type[lr.leave_type.name] += n
        absent_days += [d for d in should if d not in covered and d < today]
    absent_days = sorted(set(absent_days))
    spells = sum(1 for i, d in enumerate(absent_days) if i == 0 or (d - absent_days[i - 1]).days > (3 if absent_days[i - 1].weekday() == 4 else 1))

    def avg_clock(ts):
        if not ts:
            return None
        secs = int(sum(t.hour * 3600 + t.minute * 60 for t in ts) / len(ts))
        return f'{secs // 3600:02d}:{secs % 3600 // 60:02d}'

    return {
        'days_worked': days, 'worked_seconds': worked, 'worked_text': wt.fmt_hm(worked), 'break_seconds': brk, 'break_text': wt.fmt_hm(brk),
        'avg_worked': wt.fmt_hm(worked // days) if days else None, 'avg_break_min': int(brk / 60 / days) if days else 0,
        'breaks': breaks_n, 'longest_break': wt.fmt_hm(longest), 'usual_login': avg_clock(logins), 'usual_logout': avg_clock(logouts),
        'late_logins': late if any(e.shift_id for e in emps.values()) else None, 'absent_days': absent_days, 'absent': len(absent_days),
        'absence_spells': spells, 'leave_days': leave_days, 'leave_by_type': dict(leave_by_type),
        'working_days': len(india.working_days_list(p.start, last)) if last >= p.start else 0,
    }


def achievement_rows(m: dict, p: Period, today: date) -> list[dict]:
    """Target against actual for each measure: the gap, and for a running period where they should be by now."""
    t = m['targets']
    elapsed = len(india.working_days_list(p.start, min(p.end, today)))
    total = len(india.working_days_list(p.start, p.end)) or 1
    ratio = Decimal(elapsed) / Decimal(total) if p.is_current else Decimal(1)
    spec = [('leads', 'Leads', m['leads'], False), ('quotations', 'Quotations sent', m['quotes'], False),
            ('clients', 'Clients onboarded', m['clients'], False), ('calls', 'Calls', m['calls'], False),
            ('meetings', 'Meetings', m['meetings'], False), ('won', 'Won revenue', m['won_value'], True),
            ('collected', 'Collected', m['collected'], True)]
    rows = []
    for key, label, actual, money in spec:
        target = t[key]
        expected = target * ratio
        pct = _pct(actual, target)
        pace = _pct(actual, expected) if expected else None
        tone = ''
        if target:
            ref = pace if (p.is_current and pace is not None) else pct
            tone = 'good' if ref is not None and ref >= 100 else 'warn' if ref is not None and ref >= 70 else 'bad'
        rows.append({'key': key, 'label': label, 'actual': actual, 'target': target, 'money': money, 'diff': actual - target if target else None,
                     'pct': pct, 'expected': expected if p.is_current else None, 'pace': pace, 'tone': tone, 'has': bool(target)})
    return rows


def flags(m: dict, p: Period) -> list[dict]:
    """Plain-language reasons to look at this person, worst first. Each one names the number behind it."""
    out = []
    rows = {r['key']: r for r in m['rows']}
    for key, noun, level in (('collected', 'collected', 'bad'), ('won', 'won', 'warn')):
        r = rows[key]
        if r['has'] and r['pace'] is not None and Decimal(r['pace']) < BEHIND_PACE * 100 and p.is_current:
            out.append({'level': level, 'text': f"{noun.capitalize()} {_money(r['actual'])} against {_money(r['expected'])} expected by now ({r['pace']}% of pace)."})
        elif r['has'] and not p.is_current and r['pct'] is not None and r['pct'] < 70:
            out.append({'level': level, 'text': f"{noun.capitalize()} {_money(r['actual'])} of a {_money(r['target'])} target ({r['pct']}%)."})
    if m['fu_skipped'] >= SKIPPED_BAD:
        out.append({'level': 'bad', 'text': f"{m['fu_skipped']} follow-ups skipped."})
    elif m['fu_skipped']:
        out.append({'level': 'warn', 'text': f"{m['fu_skipped']} follow-up{'s' if m['fu_skipped'] != 1 else ''} skipped."})
    if m['fu_due'] >= 5 and m['fu_compliance'] is not None and m['fu_compliance'] < 70:
        out.append({'level': 'warn', 'text': f"Only {m['fu_compliance']}% of follow-ups were done on time."})
    if m['absent']:
        out.append({'level': 'bad' if m['absent'] >= 3 else 'warn', 'text': f"{m['absent']} working day{'s' if m['absent'] != 1 else ''} not logged in."})
    if m['stale']:
        out.append({'level': 'warn', 'text': f"{m['stale']} open lead{'s' if m['stale'] != 1 else ''} untouched for {STALE_DAYS}+ days."})
    if m['first_response_min'] is not None and m['first_response_min'] > FIRST_RESPONSE_OK_MINUTES:
        out.append({'level': 'warn', 'text': f"First response averages {m['first_response_min']} minutes."})
    if m['auto_scheduled']:
        out.append({'level': 'warn', 'text': f"{m['auto_scheduled']} lead{'s' if m['auto_scheduled'] != 1 else ''} had no follow-up and the system had to set one."})
    if m.get('quiet_clients'):
        out.append({'level': 'warn', 'text': f"{m['quiet_clients']} live customer{'s' if m['quiet_clients'] != 1 else ''} with no contact for 30+ days."})
    if m['tasks_overdue'] >= 3:
        out.append({'level': 'warn', 'text': f"{m['tasks_overdue']} tasks overdue."})
    if m['dl_missed']:
        out.append({'level': 'bad', 'text': f"{m['dl_missed']} client deliverable{'s' if m['dl_missed'] != 1 else ''} missed."})
    if m['avg_break_min'] > LONG_BREAK_MINUTES:
        out.append({'level': 'warn', 'text': f"Breaks average {m['avg_break_min']} minutes a day."})
    if m['late_logins']:
        out.append({'level': 'warn' if m['late_logins'] < 3 else 'bad', 'text': f"{m['late_logins']} late login{'s' if m['late_logins'] != 1 else ''}."})
    if not m['targets']['has']:
        out.append({'level': 'info', 'text': 'No target is set for this period, so results cannot be judged against one.'})
    hit = [r for r in m['rows'] if r['has'] and r['pct'] is not None and r['pct'] >= 100]
    if hit:
        out.append({'level': 'good', 'text': 'Target reached: ' + ', '.join(r['label'].lower() for r in hit) + '.'})
    order = {'bad': 0, 'warn': 1, 'info': 2, 'good': 3}
    return sorted(out, key=lambda f: order[f['level']])


def _money(v) -> str:
    return f'₹{Decimal(v):,.0f}'


# ---------------------------------------------------------------- graphs

def trend(uids, end: date) -> dict:
    """Twelve months of revenue against target and twelve weeks of activity, for the graphs."""
    months = []
    d = end.replace(day=1)
    for i in range(11, -1, -1):
        months.append(india.add_months(d, -i))
    first, last = months[0], india.add_months(months[-1], 1)
    won = defaultdict(lambda: ZERO)
    leads = defaultdict(int)
    for l in Lead.objects.filter(stage='won', won_at__date__gte=first, won_at__date__lt=last).filter(_credited('', uids)).only('won_value', 'won_at'):
        won[timezone.localtime(l.won_at).date().replace(day=1)] += l.won_value or ZERO
    for l in Lead.objects.filter(owner_id__in=uids, assigned_at__date__gte=first, assigned_at__date__lt=last).only('assigned_at'):
        leads[timezone.localtime(l.assigned_at).date().replace(day=1)] += 1
    collected = defaultdict(lambda: ZERO)
    for pay in Payment.objects.filter(status__in=attribution.COUNTED, payment_date__gte=first, payment_date__lt=last).select_related('client__source_lead'):
        if attribution.payment_credit_id(pay) in uids:
            collected[pay.payment_date.replace(day=1)] += pay.amount
    targets = defaultdict(lambda: ZERO)
    for t in SalesTarget.objects.filter(user_id__in=uids, month__gte=first, month__lt=last):
        targets[t.month] += t.won_value
    monthly = {'labels': [m.strftime('%b %y') for m in months], 'won': [float(won[m]) for m in months],
               'collected': [float(collected[m]) for m in months], 'target': [float(targets[m]) for m in months],
               'leads': [leads[m] for m in months]}
    # weeks
    monday = end - timedelta(days=end.weekday())
    weeks = [monday - timedelta(weeks=i) for i in range(11, -1, -1)]
    w_first = timezone.make_aware(datetime.combine(weeks[0], time.min))
    w_last = timezone.make_aware(datetime.combine(weeks[-1] + timedelta(days=7), time.min))
    act = defaultdict(int)
    for a in LeadActivity.objects.filter(actor_id__in=uids, occurred_at__gte=w_first, occurred_at__lt=w_last, kind__in=['call', 'meeting', 'email', 'whatsapp']).only('occurred_at'):
        d0 = timezone.localtime(a.occurred_at).date()
        act[d0 - timedelta(days=d0.weekday())] += 1
    hours = defaultdict(float)
    for wp in WorkPeriod.objects.filter(user_id__in=uids, day__gte=weeks[0], day__lt=weeks[-1] + timedelta(days=7)).prefetch_related('breaks'):
        hours[wp.day - timedelta(days=wp.day.weekday())] += wp.worked_seconds() / 3600
    weekly = {'labels': [w.strftime('%d %b') for w in weeks], 'activity': [act[w] for w in weeks], 'hours': [round(hours[w], 1) for w in weeks]}
    stats = list(StaffDayStat.objects.filter(user_id__in=uids, day__gte=end - timedelta(days=29), day__lte=end).values('day')
                 .annotate(overdue=Sum('overdue_followups'), pipeline=Sum('open_pipeline')).order_by('day'))
    daily = {'labels': [s['day'].strftime('%d %b') for s in stats], 'overdue': [s['overdue'] for s in stats],
             'pipeline': [float(s['pipeline']) for s in stats]}
    return {'monthly': monthly, 'weekly': weekly, 'daily': daily}


def calendar_days(uid: int, p: Period, now: datetime | None = None) -> list[dict]:
    """One cell per day for the attendance calendar: worked, leave, not logged in, holiday or weekend."""
    from crm.modules.worktime import services as wt
    now = now or timezone.now()
    today = india.today()
    start = max(p.start, p.end - timedelta(days=61))
    data = wt.range_summaries([uid], start, p.end, now).get(uid, {})
    emp = Employee.objects.filter(user_id=uid).first()
    on_leave = set()
    if emp:
        for lr in LeaveRequest.objects.filter(employee=emp, status='approved', start_date__lte=p.end, end_date__gte=start):
            on_leave |= set(india.working_days_list(max(lr.start_date, start), min(lr.end_date, p.end)))
    working = set(india.working_days_list(start, p.end))
    out, d = [], start
    while d <= p.end:
        s = data.get(d)
        if d > today:
            kind = 'future'
        elif s and s['periods']:
            kind = 'worked'
        elif d in on_leave:
            kind = 'leave'
        elif d in working:
            kind = 'absent' if not (emp and emp.joining_date > d) else 'off'
        else:
            kind = 'off'
        out.append({'day': d, 'kind': kind, 'text': s['worked_text'] if s and s['periods'] else ''})
        d += timedelta(days=1)
    return out
