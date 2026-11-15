"""Working time pages: my day, the team's day, one person's days, and the break buttons."""
from __future__ import annotations

import csv
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import back, staff_required
from crm.models.worktime import fmt_hm

from . import services as svc

MODULE = 'attendance'


def _name(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else ''


def visible_users(user) -> list:
    """The tracked people this viewer may see: everyone for company scope, their team for team scope, else only themselves."""
    scope = permissions.get_scope(user, MODULE, 'view')
    base = get_user_model().objects.filter(is_active=True)
    if scope == 'company':
        return svc.tracked_users(base)
    profile = getattr(user, 'crm_profile', None)
    q = Q(pk=user.pk)
    if scope == 'team' and profile and profile.team_id:
        q |= Q(crm_profile__team_id=profile.team_id)
    elif scope == 'department' and profile:
        if profile.department_id:
            q |= Q(crm_profile__department_id=profile.department_id)
        if profile.team_id:
            q |= Q(crm_profile__team_id=profile.team_id)
    return svc.tracked_users(base.filter(q).distinct())


def can_see_team(user) -> bool:
    return permissions.has_scope(user, MODULE, 'view', 'team')


def timeline(summary: dict) -> list[dict]:
    """The day as a list of moments: signed in, break, back, signed out."""
    out = []
    reasons = {'logout': 'Logged out', 'idle': 'Signed out after 12 hours without activity', 'day_end': 'Day ended at midnight',
               'admin': 'Closed by a manager'}
    for p in summary['periods']:
        out.append({'at': p.started_at, 'label': {'login': 'Logged in', 'resume': 'Back on another device', 'day_start': 'Started the day'}.get(p.start_source, 'Logged in'),
                    'kind': 'in'})
        for b in p.breaks.all():
            out.append({'at': b.started_at, 'label': 'Break started', 'kind': 'break'})
            if b.ended_at and b.end_reason == 'back':
                out.append({'at': b.ended_at, 'label': f'Back to work after {fmt_hm(b.seconds)}', 'kind': 'back'})
        if p.ended_at:
            out.append({'at': p.ended_at, 'label': reasons.get(p.end_reason, 'Logged out'), 'kind': 'out'})
    return sorted(out, key=lambda e: e['at'])


def _page(request, person, own: bool):
    today = svc.local_day()
    now = timezone.now()
    ctx = {'person': person, 'own': own, 'today': today, 'tracked': svc.tracked(person), 'notice_text': svc.NOTICE_TEXT}
    if ctx['tracked']:
        span = 30 if not own else 14
        data = svc.range_summaries([person], today - timedelta(days=span), today, now).get(person.pk, {})
        data[today] = svc.day_summary(person, today, now)
        rows = []
        for i in range(span + 1):
            d = today - timedelta(days=i)
            s = data.get(d)
            rows.append({'day': d, 's': s if s and s['periods'] else None})
        monday = svc.week_start(today)
        week = sum(s['worked'] for d, s in data.items() if d >= monday)
        month = sum(s['worked'] for d, s in data.items() if d.month == today.month and d.year == today.year)
        ctx.update({'now_day': data[today], 'timeline': timeline(data[today]), 'rows': rows, 'week_text': fmt_hm(week),
                    'month_text': fmt_hm(month), 'widget': svc.widget(person, now) if own else None})
    return render(request, 'crm/worktime/my_time.html', ctx)


@staff_required
def my_time(request):
    return _page(request, request.user, own=True)


@staff_required
def person_time(request, pk):
    person = get_object_or_404(get_user_model(), pk=pk)
    if person.pk == request.user.pk:
        return _page(request, person, own=True)
    if not can_see_team(request.user) or person.pk not in {u.pk for u in visible_users(request.user)}:
        permissions.denied(request.user, MODULE, 'view', request)
    return _page(request, person, own=False)


def _safe_path(request, path: str) -> str:
    """A same-site address to return to, never the break screens themselves."""
    skip = (reverse('crm:on_break'), reverse('crm:break_start'), reverse('crm:break_end'), reverse('crm:logout'))
    if path and path not in skip and url_has_allowed_host_and_scheme(path, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return path
    return ''


@staff_required
@require_POST
def break_start(request):
    try:
        svc.start_break(request.user)
    except svc.WorkTimeError as exc:
        messages.error(request, str(exc))
        return back(request, reverse('crm:my_time'))
    ref = request.META.get('HTTP_REFERER', '')
    from urllib.parse import urlparse
    request.session['crm_break_next'] = _safe_path(request, urlparse(ref).path) if ref else ''
    return redirect('crm:on_break')


@staff_required
@require_POST
def break_end(request):
    nxt = request.session.pop('crm_break_next', '')
    try:
        b = svc.end_break(request.user)
        messages.success(request, f'Welcome back. That break was {fmt_hm(b.seconds)}.')
    except svc.WorkTimeError as exc:
        messages.error(request, str(exc))
    return redirect(_safe_path(request, nxt) or reverse('crm:home'))


@staff_required
@require_POST
def notice_ack(request):
    svc.acknowledge_notice(request.user)
    audit.log('worktime.notice_acknowledged', actor=request.user, object_type='user', object_id=str(request.user.pk),
              changes={'version': svc.NOTICE_VERSION})
    return back(request, reverse('crm:home'))


@never_cache
@staff_required
def on_break(request):
    """The screen someone sees while on a break: everything blanked out, one button."""
    brk = svc.current_break(request.user)
    if brk is None:
        return redirect('crm:home')
    return render(request, 'crm/worktime/on_break.html', {
        'since': brk.started_at, 'seconds': brk.seconds, 'name': request.user.first_name or request.user.get_username()})


def _parse_day(raw, default: date) -> date:
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


STATE_TEXT = {'working': 'Working', 'break': 'On break', 'off': 'Logged out', 'none': 'No login'}


@staff_required
def team_time(request):
    user = request.user
    if not can_see_team(user):
        permissions.denied(user, MODULE, 'view', request)
    today = svc.local_day()
    people = visible_users(user)
    if request.GET.get('export') == 'csv':
        return _export(request, people, today)
    day = min(_parse_day(request.GET.get('day'), today), today)
    now = timezone.now()
    data = svc.range_summaries(people, day, day, now)
    rows, totals = [], {'worked': 0, 'break': 0, 'working_now': 0, 'on_break': 0, 'no_login': 0}
    for u in people:
        s = data.get(u.pk, {}).get(day)
        state = s['state'] if s else 'none'
        if state == 'working':
            totals['working_now'] += 1
        elif state == 'break':
            totals['on_break'] += 1
        elif state == 'none':
            totals['no_login'] += 1
        if s:
            totals['worked'] += s['worked']
            totals['break'] += s['break_seconds']
        long_break = bool(s and s['longest_break'] >= 3600)
        rows.append({'user': u, 's': s, 'state': state, 'state_text': STATE_TEXT[state] if day == today or state == 'none' else 'Worked',
                     'long_break': long_break})
    rows.sort(key=lambda r: ({'break': 0, 'working': 1, 'off': 2, 'none': 3}[r['state']], _name(r['user']).lower()))
    totals['worked_text'], totals['break_text'] = fmt_hm(totals['worked']), fmt_hm(totals['break'])
    return render(request, 'crm/worktime/team.html', {
        'rows': rows, 'day': day, 'today': today, 'prev_day': day - timedelta(days=1),
        'next_day': day + timedelta(days=1) if day < today else None, 'totals': totals, 'people_count': len(people),
        'week_ago': today - timedelta(days=6), 'is_today': day == today})


def hhmm(seconds: int) -> str:
    h, m = divmod(max(0, int(seconds)) // 60, 60)
    return f'{h}:{m:02d}'


def _period(request, today: date):
    """Choose the report period from a preset or from/to dates. Never more than 93 days, never in the future."""
    key = request.GET.get('range', '')
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    presets = {
        'today': (today, today), 'week': (svc.week_start(today), today),
        'last_week': (svc.week_start(today) - timedelta(days=7), svc.week_start(today) - timedelta(days=1)),
        'month': (first, today), 'last_month': (last_month_end.replace(day=1), last_month_end),
    }
    if key in presets:
        start, end = presets[key]
    else:
        end = min(_parse_day(request.GET.get('to'), today), today)
        start = min(_parse_day(request.GET.get('from'), first), end)
        key = 'custom' if request.GET.get('from') or request.GET.get('to') else 'month'
    if (end - start).days > 92:
        start = end - timedelta(days=92)
    return start, end, key


@staff_required
def team_report(request):
    """Who worked how much over a period: the report a manager reads at the end of a week or month."""
    user = request.user
    if not can_see_team(user):
        permissions.denied(user, MODULE, 'view', request)
    today = svc.local_day()
    start, end, key = _period(request, today)
    people = visible_users(user)
    rep = svc.report(people, start, end)
    if request.GET.get('export') == 'csv':
        permissions.require(user, MODULE, 'export', request)
        headers = ['Name', 'Username', 'Days worked', 'Days not logged in', 'Working time (h:mm)', 'Average per day', 'Break time',
                   'Breaks', 'Longest break', 'Usual login', 'Usual logout']
        from crm.models import ExportLog
        ExportLog.objects.create(user=user, module=MODULE, filters=dict(request.GET.lists()), columns=headers, row_count=len(rep['rows']),
                                 file_format='csv')
        audit.log('export.created', actor=user, object_type='worktime_report', object_id='', changes={'from': start, 'to': end})
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="working-time-report-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
        resp.write('\ufeff')
        w = csv.writer(resp)
        w.writerow(headers)
        for r in rep['rows']:
            w.writerow([_csv_safe(v) for v in (_name(r['user']), r['user'].get_username(), r['days'], r['absent'], hhmm(r['worked']),
                                               r['avg_worked'], r['break_text'], r['breaks'], r['longest_break'], r['avg_in'], r['avg_out'])])
        return resp
    return render(request, 'crm/worktime/report.html', {
        'rep': rep, 'start': start, 'end': end, 'key': key, 'today': today, 'people_count': len(people),
        'presets': [('today', 'Today'), ('week', 'This week'), ('last_week', 'Last week'), ('month', 'This month'),
                    ('last_month', 'Last month')]})


def _csv_safe(value):
    return "'" + value if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def _export(request, people, today: date):
    permissions.require(request.user, MODULE, 'export', request)
    end = min(_parse_day(request.GET.get('to'), today), today)
    start = min(_parse_day(request.GET.get('from'), end - timedelta(days=6)), end)
    if (end - start).days > 92:
        start = end - timedelta(days=92)
    now = timezone.now()
    data = svc.range_summaries(people, start, end, now)
    rows = []
    for u in people:
        for d in sorted(data.get(u.pk, {})):
            s = data[u.pk][d]
            last = s['periods'][-1]
            rows.append([_name(u), u.get_username(), d.strftime('%d-%b-%Y'),
                         timezone.localtime(s['first_in']).strftime('%H:%M') if s['first_in'] else '',
                         timezone.localtime(s['last_out']).strftime('%H:%M') if s['last_out'] else 'still working',
                         s['break_count'], round(s['break_seconds'] / 60), hhmm(s['worked']),
                         last.get_end_reason_display() if last.ended_at else 'Still working'])
    headers = ['Name', 'Username', 'Date', 'First login', 'Last logout', 'Breaks', 'Break minutes', 'Working time (h:mm)', 'Day ended']
    from crm.models import ExportLog
    ExportLog.objects.create(user=request.user, module=MODULE, filters=dict(request.GET.lists()), columns=headers, row_count=len(rows),
                             file_format='csv')
    audit.log('export.created', actor=request.user, object_type='worktime', object_id='', changes={'rows': len(rows), 'from': start, 'to': end})
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="working-time-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
    resp.write('﻿')
    w = csv.writer(resp)
    w.writerow(headers)
    for r in rows:
        w.writerow([_csv_safe(v) for v in r])
    return resp
