"""Dashboards and the report catalogue (Part 13.1-13.2)."""
from __future__ import annotations

import csv
import io

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import staff_required

from . import catalogue, dashboards


def _export_log(user, module, filters, columns, rows):
    from crm.models import ExportLog
    ExportLog.objects.create(user=user, module=module, filters=filters, columns=columns, row_count=rows, file_format='csv')
    audit.log('export.created', actor=user, object_type=module, object_id='', changes={'rows': rows, 'filters': filters})


def _dashboard(request, key):
    title, func, (module, action) = dashboards.DASHBOARDS[key]
    permissions.require(request.user, module, action, request)
    if not dashboards.can_open(request.user, key):
        permissions.denied(request.user, module, action, request, detail=f'dashboard:{key} needs company-wide access')
    rng = dashboards.date_range(request)
    data = func(request, rng)
    shown, all_cards = dashboards.apply_prefs(request.user, key, data['cards'])
    if request.GET.get('export') == 'csv':
        permissions.require(request.user, 'reports', 'export', request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Card', 'Value', 'Covers', 'As of'])
        stamp = timezone.localtime().strftime('%d-%b-%Y %H:%M')
        for c in shown:
            w.writerow([c.label, c.value, c.note, stamp])
        _export_log(request.user, f'dashboard:{key}', {'from': rng.start.isoformat(), 'to': rng.end.isoformat()}, ['Card', 'Value'], len(shown))
        resp = HttpResponse(('﻿' + buf.getvalue()).encode('utf-8'), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{key}-dashboard-{rng.end:%Y%m%d}.csv"'
        return resp
    others = [(k, t, reverse(_URL[k])) for k, (t, _, (m, a)) in dashboards.DASHBOARDS.items()
              if k != key and dashboards.can_open(request.user, k)]
    profile = getattr(request.user, 'crm_profile', None)
    prefs = (profile.dashboard_prefs or {}).get(key, {}) if profile else {}
    ctx = {**data, 'key': key, 'title': title, 'cards': shown, 'all_cards': all_cards, 'hidden': set(prefs.get('hidden', [])),
           'rng': rng, 'presets': dashboards.PRESETS, 'as_of': timezone.localtime(), 'others': others,
           'can_export': permissions.has_perm(request.user, 'reports', 'export')}
    return render(request, f'crm/reports/dashboard_{key}.html', ctx)


_URL = {'owner': 'crm:dashboard_owner', 'sales': 'crm:dashboard_sales', 'finance': 'crm:dashboard_finance',
        'delivery': 'crm:dashboard_delivery', 'hr': 'crm:hr_dashboard'}


@staff_required
def dashboard_owner(request):
    return _dashboard(request, 'owner')


@staff_required
def dashboard_sales(request):
    return _dashboard(request, 'sales')


@staff_required
def dashboard_finance(request):
    return _dashboard(request, 'finance')


@staff_required
def dashboard_delivery(request):
    return _dashboard(request, 'delivery')


@staff_required
def hr_dashboard(request):
    return _dashboard(request, 'hr')


@staff_required
@require_POST
def dashboard_configure(request, key):
    if key not in dashboards.DASHBOARDS:
        raise Http404
    _, _, (module, action) = dashboards.DASHBOARDS[key]
    permissions.require(request.user, module, action, request)
    if not dashboards.can_open(request.user, key):
        raise Http404
    profile = request.user.crm_profile
    if request.POST.get('reset'):
        prefs = dict(profile.dashboard_prefs or {})
        prefs.pop(key, None)
    else:
        allowed = [k for k in request.POST.getlist('order') if k.replace('_', '').isalnum()][:60]
        shown = set(request.POST.getlist('show'))
        prefs = dict(profile.dashboard_prefs or {})
        prefs[key] = {'order': allowed, 'hidden': [k for k in allowed if k not in shown]}
    profile.dashboard_prefs = prefs
    profile.save(update_fields=['dashboard_prefs'])
    messages.success(request, 'Dashboard layout saved.' if not request.POST.get('reset') else 'Dashboard reset to the default layout.')
    return redirect(_URL[key])


# ---------------------------------------------------------------- report catalogue

@staff_required
def report_index(request):
    permissions.require(request.user, 'reports', 'view', request)
    from crm.models import ReportSchedule
    groups: dict = {}
    for r in catalogue.visible(request.user):
        groups.setdefault(r.group, []).append(r)
    return render(request, 'crm/reports/index.html', {
        'groups': groups, 'schedules': ReportSchedule.objects.filter(user=request.user),
        'by_slug': catalogue.BY_SLUG})


@staff_required
def report_view(request, slug):
    permissions.require(request.user, 'reports', 'view', request)
    report = catalogue.BY_SLUG.get(slug)
    if report is None or report not in catalogue.visible(request.user):
        raise Http404
    if report.url_name:
        return redirect(report.url_name)
    params = catalogue.params_from(request.GET)
    try:
        table = catalogue.run(report, request.user, params)
    except permissions.PermissionDenied as exc:
        return render(request, 'crm/errors/403.html', {'message': str(exc)}, status=403)
    if request.GET.get('export') == 'csv':
        permissions.require(request.user, 'reports', 'export', request)
        data = catalogue.to_csv(report, table, params)
        _export_log(request.user, f'report:{slug}', dict(request.GET.lists()), [c[0] for c in table.columns], len(table.rows))
        resp = HttpResponse(data, content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{slug}-{params.end:%Y%m%d}.csv"'
        return resp
    filters = []
    for name in report.filters:
        if name in report.choices:
            filters.append((name, name.capitalize(), report.choices[name]))
        elif name in catalogue.FILTERS:
            label, fn = catalogue.FILTERS[name]
            choices = [(str(k), v) for k, v in fn(request.user)]
            if choices:
                filters.append((name, label, choices))
    from crm.modules.accounting.reports import previous_period
    ps, pe = previous_period(params.start, params.end)
    prev_q = request.GET.copy()
    prev_q['from'], prev_q['to'] = ps.isoformat(), pe.isoformat()
    from crm.models import ReportSchedule
    return render(request, 'crm/reports/report.html', {
        'report': report, 'table': table, 'params': params, 'filters': filters, 'active': params.filters,
        'prev_url': f'?{prev_q.urlencode()}', 'prev_label': f'{ps:%d %b} – {pe:%d %b %Y}',
        'can_export': permissions.has_perm(request.user, 'reports', 'export'),
        'schedules': ReportSchedule.objects.filter(user=request.user, report=slug),
        'frequencies': ReportSchedule.FREQUENCIES, 'querystring': request.GET.urlencode(),
        'presets': _report_presets(),
    })


def _report_presets():
    from crm.core import india
    from datetime import timedelta
    today = india.today()
    fy0, _ = india.fy_bounds(today)
    last_end = india.month_start(today) - timedelta(days=1)
    return [('This month', india.month_start(today), today), ('Last month', india.month_start(last_end), last_end),
            ('Last 90 days', today - timedelta(days=89), today), ('This financial year', fy0, today)]


@staff_required
@require_POST
def report_schedule(request, slug):
    from crm.models import ReportSchedule
    permissions.require(request.user, 'reports', 'export', request)
    report = catalogue.BY_SLUG.get(slug)
    if report is None or report.func is None or report not in catalogue.visible(request.user):
        raise Http404
    if not request.user.email:
        messages.error(request, 'Add an email address to your profile before scheduling a report.')
    else:
        freq = request.POST.get('frequency', 'weekly')
        try:
            hour = min(max(int(request.POST.get('hour', 8)), 0), 23)
        except ValueError:
            hour = 8
        if freq not in dict(ReportSchedule.FREQUENCIES):
            freq = 'weekly'
        qs = request.POST.get('querystring', '')[:1000]
        s = ReportSchedule.objects.create(user=request.user, report=slug, querystring=qs, frequency=freq, hour=hour)
        audit.log('report.scheduled', actor=request.user, object_type='reportschedule', object_id=str(s.pk),
                  changes={'report': slug, 'frequency': freq, 'hour': hour})
        messages.success(request, f'{report.title} will be emailed to {request.user.email} {s.get_frequency_display().lower()} from {hour:02d}:00.')
    return redirect(reverse('crm:report_view', args=[slug]) + ('?' + request.POST.get('querystring', '') if request.POST.get('querystring') else ''))


@staff_required
@require_POST
def report_schedule_delete(request, pk):
    from crm.models import ReportSchedule
    s = get_object_or_404(ReportSchedule, pk=pk, user=request.user)
    audit.log('report.schedule_removed', actor=request.user, object_type='reportschedule', object_id=str(s.pk), changes={'report': s.report})
    s.delete()
    messages.success(request, 'Schedule removed.')
    return redirect('crm:report_index')

