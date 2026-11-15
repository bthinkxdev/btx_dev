"""Working time: login, breaks and logout, and what they add up to each day.

Rules, so nobody is surprised by a number:

* Administrators (the owner and super admins) are never tracked. Everyone else is.
* Working time = from login to logout, minus breaks. Being idle is not subtracted: a person is signed out only after
  12 hours without any activity, and then their logout is recorded at their last activity, not 12 hours later.
* A day never runs past midnight. A person still signed in at midnight is closed at their last activity and a new day
  starts with their first activity after midnight.
* Several devices share one running day. Logging out on one device does not end it while another was used recently.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_tz

from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.core import india, permissions
from crm.models import BreakPeriod, WorkPeriod
from crm.models.worktime import fmt_hm

logger = logging.getLogger('crm.worktime')

MCP_USERNAME = 'business_manager_ai'
DEFAULT_IDLE_MINUTES = 720


NOTICE_VERSION = 1       # raise this when the wording changes and everyone is asked again
NOTICE_TEXT = ('Your login and logout times, your breaks and your work in the CRM (calls, follow-ups, tasks, deliverables and the leads you '
               'handle) are recorded. They are used to measure attendance and performance. Administrators, HR and your sales manager can see them.')


def notice_pending(user) -> bool:
    """Tracked people are told, once, that their time and activity are recorded."""
    if not tracked(user):
        return False
    profile = getattr(user, 'crm_profile', None)
    return profile is not None and profile.work_notice_version < NOTICE_VERSION


def acknowledge_notice(user) -> None:
    from crm.models import UserProfile
    UserProfile.objects.filter(user=user).update(work_notice_version=NOTICE_VERSION, work_notice_at=timezone.now())


def idle_limit() -> timedelta:
    """How long a person may be inactive before they are signed out (12 hours unless an administrator changed it)."""
    try:
        from crm.models import CompanySettings
        return timedelta(minutes=CompanySettings.get().idle_timeout_minutes or DEFAULT_IDLE_MINUTES)
    except Exception:  # database not migrated yet
        return timedelta(minutes=DEFAULT_IDLE_MINUTES)


def local_day(moment: datetime | None = None) -> date:
    return timezone.localtime(moment or timezone.now()).date()


def tracked(user) -> bool:
    """Everyone except administrators and the AI service account."""
    if user is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return False
    if user.username == MCP_USERNAME or permissions.is_super_admin(user):
        return False
    return True


def tracked_users(qs=None) -> list:
    from django.contrib.auth import get_user_model
    qs = qs if qs is not None else get_user_model().objects.filter(is_active=True)
    qs = qs.exclude(username=MCP_USERNAME).select_related('crm_profile').prefetch_related('crm_profile__roles')
    return [u for u in qs.order_by('first_name', 'username') if tracked(u)]


def current_break(user) -> BreakPeriod | None:
    """The break a person is on right now, if any. While one is open the CRM is locked for them."""
    if not tracked(user):
        return None
    return BreakPeriod.objects.filter(period__user=user, period__ended_at__isnull=True, ended_at__isnull=True).select_related('period').first()


def open_period(user) -> WorkPeriod | None:
    return WorkPeriod.objects.filter(user=user, ended_at__isnull=True).first()


# ---------------------------------------------------------------- opening and closing

def _start(user, at: datetime, source: str) -> WorkPeriod:
    try:
        with transaction.atomic():
            return WorkPeriod.objects.create(user=user, day=local_day(at), started_at=at, last_activity_at=at, start_source=source)
    except IntegrityError:                    # another request opened it a moment ago
        return open_period(user)


def _close(period: WorkPeriod, at: datetime, reason: str) -> None:
    """End the period, and any break still running inside it, at the same moment."""
    at = max(at, period.started_at)
    with transaction.atomic():
        for b in period.breaks.filter(ended_at__isnull=True):
            BreakPeriod.objects.filter(pk=b.pk, ended_at__isnull=True).update(ended_at=max(at, b.started_at), end_reason=reason)
        WorkPeriod.objects.filter(pk=period.pk, ended_at__isnull=True).update(ended_at=at, end_reason=reason,
                                                                              last_activity_at=max(period.last_activity_at, at))
    try:
        from crm.modules.performance import daily
        daily.generate(period.user, period.day)
    except Exception:   # a reporting fault must never stop someone signing out
        logger.exception('Daily report for %s could not be made', period.user_id)


def _other_recent_session(user, session_key: str, now: datetime) -> bool:
    from crm.models import UserSession
    return UserSession.objects.filter(user=user, ended_at__isnull=True, last_seen_at__gte=now - idle_limit()).exclude(
        session_key=session_key or '').exists()


# ---------------------------------------------------------------- events from the sign-in flow

def on_login(user, now: datetime | None = None) -> None:
    if not tracked(user):
        return
    now = now or timezone.now()
    period = open_period(user)
    if period is not None and period.day == local_day(now):
        WorkPeriod.objects.filter(pk=period.pk).update(last_activity_at=now)
        return
    if period is not None:                    # left open from an earlier day
        _close(period, period.last_activity_at, 'day_end')
    _start(user, now, 'login')


def on_logout(user, session_key: str = '', now: datetime | None = None) -> None:
    if not tracked(user):
        return
    now = now or timezone.now()
    period = open_period(user)
    if period is None:
        return
    if _other_recent_session(user, session_key, now):
        WorkPeriod.objects.filter(pk=period.pk).update(last_activity_at=now)      # still signed in on another device
        return
    _close(period, now, 'logout')


def on_idle_logout(user, last_activity_ts: float | None, session_key: str = '') -> None:
    """The session ran out after the idle limit. The day ends when the person last did something."""
    if not tracked(user):
        return
    period = open_period(user)
    if period is None:
        return
    last = datetime.fromtimestamp(float(last_activity_ts), dt_tz.utc) if last_activity_ts else period.last_activity_at
    if _other_recent_session(user, session_key, timezone.now()):
        return
    _close(period, last, 'idle')


def heartbeat(user, now: datetime | None = None) -> None:
    """Called about once a minute while someone uses the CRM. Keeps their day open, rolls it over at midnight,
    and brings it back if another device had ended it."""
    if not tracked(user):
        return
    now = now or timezone.now()
    period = open_period(user)
    if period is None:
        _start(user, now, 'resume')
        return
    if period.day != local_day(now):
        _close(period, period.last_activity_at, 'day_end')
        _start(user, now, 'day_start')
        return
    WorkPeriod.objects.filter(pk=period.pk).update(last_activity_at=now)


# ---------------------------------------------------------------- breaks

class WorkTimeError(Exception):
    pass


@transaction.atomic
def start_break(user, now: datetime | None = None) -> BreakPeriod:
    if not tracked(user):
        raise WorkTimeError('Your time is not tracked.')
    now = now or timezone.now()
    period = open_period(user)
    if period is None or period.day != local_day(now):
        heartbeat(user, now)
        period = open_period(user)
    if period.breaks.filter(ended_at__isnull=True).exists():
        raise WorkTimeError('You are already on a break.')
    WorkPeriod.objects.filter(pk=period.pk).update(last_activity_at=now)
    return BreakPeriod.objects.create(period=period, started_at=now)


@transaction.atomic
def end_break(user, now: datetime | None = None) -> BreakPeriod:
    if not tracked(user):
        raise WorkTimeError('Your time is not tracked.')
    now = now or timezone.now()
    period = open_period(user)
    brk = period.breaks.select_for_update().filter(ended_at__isnull=True).first() if period else None
    if brk is None:
        raise WorkTimeError('You are not on a break.')
    brk.ended_at, brk.end_reason = max(now, brk.started_at), 'back'
    brk.save(update_fields=['ended_at', 'end_reason'])
    WorkPeriod.objects.filter(pk=period.pk).update(last_activity_at=now)
    return brk


# ---------------------------------------------------------------- the job

def close_stale(now: datetime | None = None) -> int:
    """Close days nobody is using any more: 12 hours with no activity, or a day that rolled past midnight."""
    now = now or timezone.now()
    limit, today, n = idle_limit(), local_day(now), 0
    for p in WorkPeriod.objects.filter(ended_at__isnull=True).select_related('user'):
        if now - p.last_activity_at >= limit:
            _close(p, p.last_activity_at, 'idle')
            n += 1
        elif p.day < today:
            _close(p, p.last_activity_at, 'day_end')
            n += 1
    return n


# ---------------------------------------------------------------- reading

def _summary(periods: list[WorkPeriod], now: datetime) -> dict:
    breaks = [b for p in periods for b in p.breaks.all()]
    worked = sum(p.worked_seconds(now) for p in periods)
    brk = sum(int(((b.ended_at or now) - b.started_at).total_seconds()) for b in breaks)
    open_p = next((p for p in periods if p.is_open), None)
    on_break = open_p is not None and any(b.ended_at is None for b in open_p.breaks.all())
    return {
        'periods': periods, 'breaks': breaks, 'first_in': periods[0].started_at if periods else None,
        'last_out': periods[-1].ended_at if periods and periods[-1].ended_at else None,
        'worked': worked, 'break_seconds': brk, 'break_count': len(breaks),
        'longest_break': max((int(((b.ended_at or now) - b.started_at).total_seconds()) for b in breaks), default=0),
        'state': ('break' if on_break else 'working') if open_p else ('off' if periods else 'none'),
        'worked_text': fmt_hm(worked), 'break_text': fmt_hm(brk), 'open': open_p,
    }


def day_summary(user, day: date, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    periods = list(WorkPeriod.objects.filter(user=user, day=day).prefetch_related('breaks').order_by('started_at'))
    return _summary(periods, now)


def range_summaries(users, start: date, end: date, now: datetime | None = None) -> dict:
    """{user_id: {day: summary}} for everyone in one pass."""
    now = now or timezone.now()
    grouped: dict = {}
    qs = WorkPeriod.objects.filter(user__in=users, day__range=(start, end)).prefetch_related('breaks').order_by('started_at')
    for p in qs:
        grouped.setdefault(p.user_id, {}).setdefault(p.day, []).append(p)
    return {uid: {d: _summary(ps, now) for d, ps in days.items()} for uid, days in grouped.items()}


def widget(user, now: datetime | None = None) -> dict | None:
    """What the header shows: working, on break, or nothing for people who are not tracked."""
    if not tracked(user):
        return None
    now = now or timezone.now()
    s = day_summary(user, local_day(now), now)
    open_p = s['open']
    brk = next((b for b in open_p.breaks.all() if b.ended_at is None), None) if open_p else None
    return {
        'state': s['state'], 'worked': s['worked'], 'worked_text': s['worked_text'], 'break_text': s['break_text'],
        'break_seconds': int((now - brk.started_at).total_seconds()) if brk else 0,
        'since': open_p.started_at if open_p else None, 'now_ms': int(now.timestamp() * 1000),
    }


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def end_of(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.max))


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}'


def report(users, start: date, end: date, now: datetime | None = None) -> dict:
    """One line per person for a period: days worked, working time, breaks, typical login and logout, and the
    working days (Monday to Friday unless HR changed it, minus holidays) they never logged in."""
    now = now or timezone.now()
    today = local_day(now)
    last = min(end, today)
    working_days = india.working_days_list(start, last) if last >= start else []
    data = range_summaries(users, start, end, now)
    rows, total_worked, total_break = [], 0, 0
    for u in users:
        days = {d: s for d, s in data.get(u.pk, {}).items() if s['periods']}
        worked = sum(s['worked'] for s in days.values())
        brk = sum(s['break_seconds'] for s in days.values())
        ins = [timezone.localtime(s['first_in']) for s in days.values() if s['first_in']]
        outs = [timezone.localtime(s['last_out']) for s in days.values() if s['last_out']]
        n = len(days)
        absent = [d for d in working_days if d not in days]
        rows.append({
            'user': u, 'days': n, 'worked': worked, 'worked_text': fmt_hm(worked), 'break_seconds': brk, 'break_text': fmt_hm(brk),
            'avg_worked': fmt_hm(worked // n) if n else '—', 'avg_break': fmt_hm(brk // n) if n else '—',
            'avg_in': _clock(sum(t.hour * 3600 + t.minute * 60 for t in ins) / len(ins)) if ins else '—',
            'avg_out': _clock(sum(t.hour * 3600 + t.minute * 60 for t in outs) / len(outs)) if outs else '—',
            'absent': len(absent), 'absent_days': absent, 'breaks': sum(s['break_count'] for s in days.values()),
            'longest_break': fmt_hm(max((s['longest_break'] for s in days.values()), default=0)),
        })
        total_worked += worked
        total_break += brk
    rows.sort(key=lambda r: (-r['worked'], r['user'].get_username()))
    return {'rows': rows, 'start': start, 'end': end, 'working_days': len(working_days), 'total_worked': fmt_hm(total_worked),
            'total_break': fmt_hm(total_break)}
