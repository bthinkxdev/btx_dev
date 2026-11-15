"""Performance profile of a salesperson, and the board of a team.

Who sees it: administrators and HR for anyone; a sales manager for the people on their team (and themselves). A sales
executive has no profile page: their own results are on the Targets page. Every view is written to the audit log."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, india, permissions
from crm.core.views import staff_required
from crm.models import Employee

from . import daily, metrics
from .jobs import sales_staff

MODULE = 'employees'


def can_view(viewer, target) -> bool:
    if permissions.is_super_admin(viewer) or permissions.has_scope(viewer, MODULE, 'view', 'company'):
        return True
    if permissions.has_role(viewer, 'sales_manager'):
        return viewer.pk == target.pk or target.pk in {u.pk for u in metrics.team_members(viewer)}
    return False


def can_open_board(viewer) -> bool:
    return bool(permissions.is_super_admin(viewer) or permissions.has_scope(viewer, MODULE, 'view', 'company')
                or permissions.has_role(viewer, 'sales_manager'))


def _name(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else ''


def _money(v) -> str:
    return f'₹{v:,.0f}'


@staff_required
def staff_profile(request, pk):
    target = get_object_or_404(get_user_model().objects.select_related('crm_profile'), pk=pk)
    viewer = request.user
    if not can_view(viewer, target):
        permissions.denied(viewer, MODULE, 'view', request, detail='performance profile')
    members = metrics.team_members(target)
    mode = 'team' if request.GET.get('view') == 'team' and members else 'individual'
    uids = [m.pk for m in members] if mode == 'team' else [target.pk]
    team_ids = [t.pk for t in metrics.managed_teams(target)] if mode == 'team' else []
    period = metrics.period_from(request.GET, earliest=metrics.earliest_day(uids))
    m = metrics.compute(uids, period, team_ids=team_ids)
    prev = None
    if request.GET.get('compare') and period.previous():
        prev = metrics.compute(uids, period.previous(), team_ids=team_ids)
    today = india.today()
    ctx = {
        'target': target, 'employee': Employee.objects.filter(user=target).select_related('department', 'manager', 'shift').first(),
        'mode': mode, 'members': members, 'period': period, 'm': m, 'prev': prev, 'flags': metrics.flags(m, period),
        'presets': metrics.PRESETS, 'compare': bool(request.GET.get('compare')), 'today': today,
        'years': list(range(today.year, today.year - 6, -1)), 'month_value': period.start.strftime('%Y-%m') if period.key in ('month', 'pick_month', 'last_month') else '',
        'is_hr': permissions.has_scope(viewer, MODULE, 'view', 'company') or permissions.is_super_admin(viewer),
        'is_manager': bool(members),
    }
    trend = metrics.trend(uids, period.end)
    ctx['trend'] = trend
    if mode == 'individual':
        ctx['calendar'] = metrics.calendar_days(target.pk, period)
        from crm.models import DailyReport
        seen, reports = set(), []
        for r in DailyReport.objects.filter(user=target, day__gte=period.start, day__lte=period.end).order_by('-day', '-seq'):
            if r.day not in seen:
                seen.add(r.day)
                reports.append(r)
        ctx['daily_reports'] = reports[:62]
    else:
        rows = []
        for u in members[:30]:
            r = metrics.compute([u.pk], period)
            rows.append({'user': u, 'm': r, 'flags': [f for f in metrics.flags(r, period) if f['level'] in ('bad', 'warn')][:2]})
        rows.sort(key=lambda x: -float(x['m']['won_value'] + x['m']['collected']))
        ctx['team_rows'] = rows
        n = len(rows) or 1
        ctx['team_avg'] = {'leads': round(sum(r['m']['leads'] for r in rows) / n, 1), 'won_value': sum(r['m']['won_value'] for r in rows) / n,
                           'calls': round(sum(r['m']['calls'] for r in rows) / n, 1), 'fu_skipped': round(sum(r['m']['fu_skipped'] for r in rows) / n, 1)}
    audit.log('performance.viewed', actor=viewer, object_type='user', object_id=str(target.pk), object_repr=_name(target)[:200],
              changes={'period': period.label, 'view': mode})
    return render(request, 'crm/performance/profile.html', ctx)


@staff_required
def team_board(request):
    """The sales people a viewer may open, side by side for one period."""
    viewer = request.user
    if not can_open_board(viewer):
        permissions.denied(viewer, MODULE, 'view', request, detail='performance board')
    if permissions.is_super_admin(viewer) or permissions.has_scope(viewer, MODULE, 'view', 'company'):
        people = sales_staff()
    else:
        people = [viewer] + metrics.team_members(viewer)
    people.sort(key=lambda u: _name(u).lower())
    period = metrics.period_from(request.GET, earliest=metrics.earliest_day([u.pk for u in people]))
    rows = []
    for u in people[:60]:
        r = metrics.compute([u.pk], period)
        rows.append({'user': u, 'm': r, 'flags': [f for f in metrics.flags(r, period) if f['level'] in ('bad', 'warn')],
                     'role': 'Manager' if permissions.has_role(u, 'sales_manager') else 'Executive' if permissions.has_role(u, 'sales_executive') else 'Sales'})
    rows.sort(key=lambda x: (-float(x['m']['won_value'] + x['m']['collected']), _name(x['user']).lower()))
    audit.log('performance.board_viewed', actor=viewer, object_type='performance', object_id='', changes={'period': period.label, 'people': len(rows)})
    return render(request, 'crm/performance/board.html', {
        'rows': rows, 'period': period, 'presets': metrics.PRESETS, 'today': india.today(),
        'years': list(range(india.today().year, india.today().year - 6, -1)),
        'month_value': period.start.strftime('%Y-%m') if period.key in ('month', 'pick_month', 'last_month') else ''})


def board_people(viewer) -> list:
    if permissions.is_super_admin(viewer) or permissions.has_scope(viewer, MODULE, 'view', 'company'):
        return sales_staff()
    return [viewer] + metrics.team_members(viewer)


@staff_required
def daily_board(request):
    """Every report for one day, for the people the viewer may see, with a box to look a report up by its code."""
    from datetime import date, timedelta
    from crm.models import DailyReport
    viewer = request.user
    if not can_open_board(viewer):
        permissions.denied(viewer, MODULE, 'view', request, detail='daily reports')
    today = india.today()
    try:
        day = min(date.fromisoformat(request.GET['day']), today) if request.GET.get('day') else today
    except ValueError:
        day = today
    people = sorted(board_people(viewer), key=lambda u: _name(u).lower())
    latest = {}
    for r in DailyReport.objects.filter(user__in=people, day=day).order_by('seq'):
        latest[r.user_id] = r
    rows = [{'user': u, 'report': latest.get(u.pk)} for u in people]
    audit.log('dailyreport.board_viewed', actor=viewer, object_type='dailyreport', object_id='', changes={'day': day})
    return render(request, 'crm/performance/daily_board.html', {
        'rows': rows, 'day': day, 'today': today, 'prev_day': day - timedelta(days=1), 'next_day': day + timedelta(days=1) if day < today else None,
        'submitted': sum(1 for r in rows if r['report'])})


@staff_required
def daily_find(request):
    """Look a report up by the code printed on it."""
    from crm.models import DailyReport
    code = (request.GET.get('code') or '').strip().upper().replace(' ', '')
    report = DailyReport.objects.filter(code=code).select_related('user').first() if code else None
    if report is None:
        messages.error(request, 'No report has that code.' if code else 'Enter the code printed on the report.')
        return redirect('crm:daily_board')
    return redirect('crm:daily_report', pk=report.pk)


@staff_required
def daily_report(request, pk):
    from crm.models import DailyReport
    report = get_object_or_404(DailyReport.objects.select_related('user'), pk=pk)
    viewer = request.user
    if not can_view(viewer, report.user):
        permissions.denied(viewer, MODULE, 'view', request, detail='daily report')
    pasted = request.POST.get('pasted', '') if request.method == 'POST' else None
    audit.log('dailyreport.viewed', actor=viewer, object_type='dailyreport', object_id=str(report.pk), object_repr=report.code,
              changes={'person': _name(report.user), 'day': report.day})
    return render(request, 'crm/performance/daily_report.html', {
        'report': report, 'versions': DailyReport.objects.filter(user=report.user, day=report.day).order_by('seq'),
        'genuine': daily.verify(report), 'pasted': pasted, 'matches': daily.same_text(report, pasted) if pasted else None,
        'person': report.user})


@require_POST
def report_event(request):
    """The pop-up tells us it was copied or closed. Works from the sign-in page, so it is keyed by the signed token."""
    report = daily.report_from_token(request.POST.get('token', ''))
    if report is None:
        return JsonResponse({'ok': False}, status=400)
    from crm.models import DailyReport
    now = timezone.now()
    if request.POST.get('event') == 'copied':
        DailyReport.objects.filter(pk=report.pk).update(copied_at=now, shown_at=report.shown_at or now, copy_count=report.copy_count + 1)
    else:
        daily.mark_shown(report)
        DailyReport.objects.filter(user=report.user, day__lte=report.day, shown_at__isnull=True).update(shown_at=now)
    return JsonResponse({'ok': True})
