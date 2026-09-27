"""
Daily Workbench: the Activity-based work queue (counts strip, tabs, a
"Work" modal that completes the current activity via crm.services.pipeline)
plus a TL/Manager team view. Additive — the existing Leads/Follow-ups pages
are untouched and keep working exactly as they do today.
"""

from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Activity, Cadence, Lead
from .rbac import can_view_all_sales_data
from .services.pipeline import PipelineError, complete_activity
from .views import (
    _lead_scope_qs,
    _local_today_bounds,
    _sales_scope_user_ids,
    _sales_team_users_qs,
    sales_pipeline_required,
)


def _workbench_scope_ids(user):
    if can_view_all_sales_data(user):
        return _sales_scope_user_ids(user)
    return {user.id}


def _active_leads_qs(user):
    return (
        _lead_scope_qs(user)
        .filter(lifecycle_state=Lead.LifecycleState.ACTIVE)
        .select_related('next_activity', 'package', 'employee')
    )


def _bucket_counts(user):
    start, end, _ = _local_today_bounds()
    qs = _active_leads_qs(user)
    return {
        'overdue': qs.filter(next_activity__scheduled_at__lt=start).count(),
        'today': qs.filter(next_activity__scheduled_at__gte=start, next_activity__scheduled_at__lt=end).count(),
        'new': qs.filter(created_at__gte=start, created_at__lt=end).count(),
        'upcoming': qs.filter(next_activity__scheduled_at__gte=end).count(),
        'no_action': qs.filter(next_activity__isnull=True).count(),
    }


def _tab_qs(user, tab):
    start, end, _ = _local_today_bounds()
    qs = _active_leads_qs(user)
    if tab == 'overdue':
        return qs.filter(next_activity__scheduled_at__lt=start).order_by('next_activity__scheduled_at')
    if tab == 'today':
        return qs.filter(
            next_activity__scheduled_at__gte=start, next_activity__scheduled_at__lt=end
        ).order_by('next_activity__scheduled_at')
    if tab == 'new':
        return qs.filter(created_at__gte=start, created_at__lt=end).order_by('-created_at')
    if tab == 'upcoming':
        return qs.filter(next_activity__scheduled_at__gte=end).order_by('next_activity__scheduled_at')
    if tab == 'no_action':
        return qs.filter(next_activity__isnull=True).order_by('-updated_at')
    # 'all' — priority order: overdue first (most overdue first), then today,
    # then new, then hot, then high value, then upcoming, no-action last.
    now = timezone.now()
    from django.db.models import Case, IntegerField, Value, When

    return qs.annotate(
        _rank=Case(
            When(next_activity__isnull=True, then=Value(6)),
            When(next_activity__scheduled_at__lt=start, then=Value(0)),
            When(next_activity__scheduled_at__lt=end, then=Value(1)),
            When(created_at__gte=start, then=Value(2)),
            When(high_hope=True, then=Value(3)),
            default=Value(5),
            output_field=IntegerField(),
        )
    ).order_by('_rank', 'next_activity__scheduled_at', '-deal_value')


@login_required
@sales_pipeline_required
def workbench(request):
    user = request.user
    tab = (request.GET.get('tab') or 'overdue').strip()
    if tab not in ('overdue', 'today', 'new', 'upcoming', 'no_action', 'all'):
        tab = 'overdue'
    leads = list(_tab_qs(user, tab)[:200])
    counts = _bucket_counts(user)
    return render(
        request,
        'crm/workbench/workbench.html',
        {
            'leads': leads,
            'counts': counts,
            'total_active': _active_leads_qs(user).count(),
            'tab': tab,
            'outcome_choices': Activity.Outcome.choices,
            'type_choices': Activity.Type.choices,
        },
    )


@login_required
@sales_pipeline_required
def workbench_lead_panel(request, pk):
    """The Work modal body for one lead's current activity."""
    user = request.user
    lead = get_object_or_404(_active_leads_qs(user), pk=pk)
    return render(
        request,
        'crm/workbench/partials/work_modal.html',
        {
            'lead': lead,
            'activity': lead.next_activity,
            'outcome_choices': Activity.Outcome.choices,
            'type_choices': Activity.Type.choices,
        },
    )


@login_required
@sales_pipeline_required
@require_POST
def workbench_complete_activity(request, pk):
    user = request.user
    lead = get_object_or_404(_active_leads_qs(user), pk=pk)
    activity = lead.next_activity
    if not activity:
        return HttpResponse(status=409)

    outcome = request.POST.get('outcome')
    next_type = request.POST.get('next_type') or None
    next_dt_raw = (request.POST.get('next_scheduled_at') or '').strip()
    next_scheduled_at = None
    if next_dt_raw:
        try:
            next_scheduled_at = datetime.strptime(next_dt_raw, '%Y-%m-%dT%H:%M')
            if timezone.is_naive(next_scheduled_at):
                next_scheduled_at = timezone.make_aware(next_scheduled_at)
        except ValueError:
            next_scheduled_at = None

    try:
        complete_activity(
            activity,
            outcome=outcome,
            user=user,
            notes=request.POST.get('notes', ''),
            next_type=next_type,
            next_scheduled_at=next_scheduled_at,
            lost_reason=request.POST.get('lost_reason', ''),
            lost_note=request.POST.get('lost_note', ''),
        )
    except PipelineError as e:
        if request.headers.get('HX-Request'):
            return render(request, 'crm/workbench/partials/work_modal_error.html', {'error': str(e)})
        return JsonResponse({'error': str(e)}, status=400)

    if request.headers.get('HX-Request'):
        resp = HttpResponse(status=200)
        resp['HX-Trigger'] = '{"crmToast": "Saved", "crmWorkbenchRefresh": true}'
        return resp
    return JsonResponse({'ok': True})


@login_required
@sales_pipeline_required
def workbench_team(request):
    user = request.user
    if not can_view_all_sales_data(user):
        return HttpResponse(status=403)
    start, end, _ = _local_today_bounds()
    rows = []
    execs = list(_sales_team_users_qs())
    for e in execs:
        qs = Lead.objects.filter(employee=e, lifecycle_state=Lead.LifecycleState.ACTIVE)
        rows.append({
            'user': e,
            'total': Lead.objects.filter(employee=e).count(),
            'today': qs.filter(
                next_activity__scheduled_at__gte=start, next_activity__scheduled_at__lt=end
            ).count(),
            'overdue': qs.filter(next_activity__scheduled_at__lt=start).count(),
            'new': qs.filter(created_at__gte=start, created_at__lt=end).count(),
            'no_action': qs.filter(next_activity__isnull=True).count(),
            'completed_today': Activity.objects.filter(
                owner=e, status=Activity.Status.COMPLETED, completed_at__gte=start, completed_at__lt=end
            ).count(),
        })
    team_ids = _sales_scope_user_ids(user)
    team_all = Lead.objects.filter(employee_id__in=team_ids)
    team_active = team_all.filter(lifecycle_state=Lead.LifecycleState.ACTIVE)
    totals = {
        'total': team_all.count(),
        'active': team_active.count(),
        'overdue': team_active.filter(next_activity__scheduled_at__lt=start).count(),
        'today': team_active.filter(
            next_activity__scheduled_at__gte=start, next_activity__scheduled_at__lt=end
        ).count(),
        'new': team_active.filter(created_at__gte=start, created_at__lt=end).count(),
        'no_action': team_active.filter(next_activity__isnull=True).count(),
        'won': team_all.filter(lifecycle_state=Lead.LifecycleState.WON).count(),
        'lost': team_all.filter(lifecycle_state=Lead.LifecycleState.LOST).count(),
        'pipeline_value': team_active.aggregate(s=Sum('deal_value'))['s'] or 0,
    }
    funnel = [
        {'label': label, 'count': team_active.filter(status=value).count()}
        for value, label in Lead.PIPELINE_STAGES
    ]
    return render(
        request,
        'crm/workbench/team.html',
        {'rows': rows, 'totals': totals, 'funnel': funnel},
    )


@login_required
@sales_pipeline_required
def pipeline_board(request):
    """Visual kanban: one column per pipeline stage, active leads only."""
    user = request.user
    leads = (
        _lead_scope_qs(user)
        .filter(lifecycle_state=Lead.LifecycleState.ACTIVE)
        .select_related('package', 'employee', 'next_activity')
        .order_by('-deal_value', '-updated_at')
    )
    columns = []
    for value, label in Lead.PIPELINE_STAGES:
        stage_leads = [l for l in leads if l.status == value]
        columns.append({
            'value': value,
            'label': label,
            'leads': stage_leads,
            'count': len(stage_leads),
            'value_sum': sum((l.deal_value for l in stage_leads), start=0),
        })
    return render(request, 'crm/workbench/pipeline.html', {'columns': columns})
