"""
Daily Workbench: the Activity-based work queue (counts strip, tabs, a
"Work" modal that completes the current activity via crm.services.pipeline)
plus a TL/Manager team view. Additive — the existing Leads/Follow-ups pages
are untouched and keep working exactly as they do today.
"""

from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import LeadForm
from .models import Activity, Cadence, Lead, Package
from .rbac import can_view_all_sales_data
from .services.pipeline import PipelineError, complete_activity
from .views import (
    _leads_url_query,
    _local_today_bounds,
    _sales_scope_context,
    _sales_scope_user_ids,
    _sales_team_users_qs,
    sales_pipeline_required,
)

# Pipeline board sort — a small, fixed set (not a full column-sort grid like
# the Leads table, since cards are already grouped by stage into columns).
PIPELINE_SORT_OPTIONS = [
    ('-created_at', 'Newest first'),
    ('created_at', 'Oldest first'),
    ('-deal_value', 'Highest value'),
    ('deal_value', 'Lowest value'),
]

WORKBENCH_PER_PAGE = 10
WORKBENCH_TABS = ('overdue', 'today', 'new', 'upcoming', 'no_action', 'all')
# Column sort on the Workbench table — opt-in, overrides the tab's priority
# ordering when present (same pattern as the Leads table's column sort).
WORKBENCH_SORT_FIELDS = {
    'package': ('package__name',),
    'value': ('deal_value',),
}


def _active_leads_qs(scope_ids):
    return (
        Lead.objects.filter(employee_id__in=scope_ids)
        .filter(lifecycle_state=Lead.LifecycleState.ACTIVE)
        .select_related('next_activity', 'package', 'employee')
    )


def _bucket_counts(scope_ids):
    start, end, _ = _local_today_bounds()
    qs = _active_leads_qs(scope_ids)
    return {
        'overdue': qs.filter(next_activity__scheduled_at__lt=start).count(),
        'today': qs.filter(next_activity__scheduled_at__gte=start, next_activity__scheduled_at__lt=end).count(),
        'new': qs.filter(created_at__gte=start, created_at__lt=end).count(),
        'upcoming': qs.filter(next_activity__scheduled_at__gte=end).count(),
        'no_action': qs.filter(next_activity__isnull=True).count(),
    }


def _tab_qs(scope_ids, tab):
    start, end, _ = _local_today_bounds()
    qs = _active_leads_qs(scope_ids)
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
    scope = _sales_scope_context(request, user)
    tab = (request.GET.get('tab') or 'overdue').strip()
    if tab not in WORKBENCH_TABS:
        tab = 'overdue'

    sort = request.GET.get('sort', '').strip()
    sort_dir = request.GET.get('dir', '').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort not in WORKBENCH_SORT_FIELDS:
        sort = ''

    qs = _tab_qs(scope['scope_ids'], tab)
    if sort:
        fields = WORKBENCH_SORT_FIELDS[sort]
        qs = qs.order_by(*(f'-{f}' if sort_dir == 'desc' else f for f in fields), '-id')

    counts = _bucket_counts(scope['scope_ids'])

    filters_ctx = {
        'tab': tab,
        'employee': str(scope['selected_employee'].id) if scope['selected_employee'] else '',
        'sort': sort,
        'dir': sort_dir if sort else '',
    }
    _wq = lambda **kw: _leads_url_query(filters_ctx, **kw)
    wb_base = reverse('crm:workbench')

    page_raw = (request.GET.get('page') or '1').strip()
    paginator = Paginator(qs, WORKBENCH_PER_PAGE)
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    leads = list(page_obj.object_list)

    tab_links = {t: _wq(tab=t, page='', sort='', dir='') for t in WORKBENCH_TABS}
    exec_picker_links = {
        'all': _wq(employee='', page=''),
    }
    if scope['scope_employees']:
        for e in scope['scope_employees']:
            exec_picker_links[e.id] = _wq(employee=e.id, page='')

    sort_links = {
        key: _wq(sort=key, dir=('desc' if (sort == key and sort_dir == 'asc') else 'asc'), page='')
        for key in WORKBENCH_SORT_FIELDS
    }

    page_items = []
    for n in paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1):
        if isinstance(n, int):
            page_items.append({
                'num': n,
                'qs': _wq(page=n) if n != 1 else _wq(page=''),
                'active': n == page_obj.number,
            })
        else:
            page_items.append(None)  # ellipsis

    prev_qs = _wq(page=page_obj.previous_page_number()) if page_obj.has_previous() else ''
    next_qs = _wq(page=page_obj.next_page_number()) if page_obj.has_next() else ''

    return render(
        request,
        'crm/workbench/workbench.html',
        {
            'leads': leads,
            'counts': counts,
            'total_active': _active_leads_qs(scope['scope_ids']).count(),
            'tab': tab,
            'wb_base': wb_base,
            'tab_links': tab_links,
            'exec_picker_links': exec_picker_links,
            'sort_links': sort_links,
            'current_sort': sort,
            'current_dir': sort_dir,
            'paginator': paginator,
            'page_obj': page_obj,
            'page_items': page_items,
            'prev_qs': prev_qs,
            'next_qs': next_qs,
            'outcome_choices': Activity.Outcome.choices,
            'type_choices': Activity.Type.choices,
            'manager_mode_scope': scope['manager_mode'],
            'scope_employees': scope['scope_employees'],
            'selected_employee': scope['selected_employee'],
        },
    )


@login_required
@sales_pipeline_required
def workbench_lead_panel(request, pk):
    """The Work modal body for one lead's current activity."""
    user = request.user
    scope = _sales_scope_context(request, user)
    lead = get_object_or_404(_active_leads_qs(scope['scope_ids']), pk=pk)
    return render(
        request,
        'crm/workbench/partials/work_modal.html',
        {
            'lead': lead,
            'activity': lead.next_activity,
            'outcome_choices': Activity.Outcome.choices,
            'type_choices': Activity.Type.choices,
            'next_step_default_date': timezone.localdate() + timezone.timedelta(days=1),
        },
    )


@login_required
@sales_pipeline_required
@require_POST
def workbench_complete_activity(request, pk):
    user = request.user
    scope = _sales_scope_context(request, user)
    lead = get_object_or_404(_active_leads_qs(scope['scope_ids']), pk=pk)
    activity = lead.next_activity
    if not activity:
        return HttpResponse(status=409)

    outcome = request.POST.get('outcome')
    next_type = request.POST.get('next_type') or None
    next_date_raw = (request.POST.get('next_date') or '').strip()
    next_time_raw = (request.POST.get('next_time') or '').strip()
    next_scheduled_at = None
    if next_date_raw:
        try:
            d = datetime.strptime(next_date_raw, '%Y-%m-%d').date()
            t = datetime.strptime(next_time_raw, '%H:%M').time() if next_time_raw else datetime.min.time()
            next_scheduled_at = timezone.make_aware(datetime.combine(d, t))
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
    """
    Visual kanban. One column per active pipeline stage, plus two terminal
    columns (Won / Lost) driven by lifecycle_state — the same single source
    of truth the Daily Workbench's outcome engine writes to
    (crm.services.pipeline), so a lead's status is the same everywhere.
    """
    user = request.user
    scope = _sales_scope_context(request, user)
    base = Lead.objects.filter(employee_id__in=scope['scope_ids']).select_related(
        'package', 'employee', 'next_activity',
    )

    q = (request.GET.get('q') or '').strip()
    if q:
        base = base.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    pkg = (request.GET.get('package') or '').strip()
    package_filter = int(pkg) if pkg.isdigit() else None
    if package_filter:
        base = base.filter(package_id=package_filter)

    sort_values = dict(PIPELINE_SORT_OPTIONS)
    sort = request.GET.get('sort', '').strip()
    if sort not in sort_values:
        sort = '-created_at'

    active_leads = base.filter(lifecycle_state=Lead.LifecycleState.ACTIVE).order_by(sort, '-id')

    columns = []
    move_options = []
    for value, label in Lead.PIPELINE_STAGES:
        if value == Lead.Status.WON:
            continue  # Won is a terminal lifecycle column below, not a progress stage
        move_options.append((value, label))
        stage_leads = [l for l in active_leads if l.status == value]
        columns.append({
            'value': value,
            'label': label,
            'leads': stage_leads,
            'count': len(stage_leads),
            'value_sum': sum((l.deal_value for l in stage_leads), start=0),
            'terminal': False,
        })

    move_options.append(('won', 'Won'))
    move_options.append(('lost', 'Lost'))

    won_leads = list(base.filter(lifecycle_state=Lead.LifecycleState.WON).order_by('-won_at')[:50])
    lost_leads = list(base.filter(lifecycle_state=Lead.LifecycleState.LOST).order_by('-lost_at')[:50])
    columns.append({
        'value': 'won', 'label': 'Won', 'leads': won_leads, 'count': len(won_leads),
        'value_sum': sum((l.deal_value for l in won_leads), start=0), 'terminal': True,
    })
    columns.append({
        'value': 'lost', 'label': 'Lost', 'leads': lost_leads, 'count': len(lost_leads),
        'value_sum': 0, 'terminal': True,
    })

    filters_ctx = {
        'q': q,
        'package': pkg,
        'employee': str(scope['selected_employee'].id) if scope['selected_employee'] else '',
        'sort': sort,
    }
    _pq = lambda **kw: _leads_url_query(filters_ctx, **kw)
    pipeline_base = reverse('crm:pipeline_board')

    exec_picker_links = {'all': _pq(employee='')}
    if scope['scope_employees']:
        for e in scope['scope_employees']:
            exec_picker_links[e.id] = _pq(employee=e.id)

    sort_links = {value: _pq(sort=value) for value, _label in PIPELINE_SORT_OPTIONS}

    package_pills = [{'id': None, 'label': 'All packages', 'qs': _pq(package='')}]
    for p in Package.objects.all():
        package_pills.append({'id': p.id, 'label': p.name, 'qs': _pq(package=p.id)})

    return render(
        request,
        'crm/workbench/pipeline.html',
        {
            'columns': columns,
            'move_options': move_options,
            'manager_mode_scope': scope['manager_mode'],
            'scope_employees': scope['scope_employees'],
            'selected_employee': scope['selected_employee'],
            'total_leads': base.count(),
            'pipeline_base': pipeline_base,
            'filters': filters_ctx,
            'exec_picker_links': exec_picker_links,
            'sort_options': PIPELINE_SORT_OPTIONS,
            'sort_links': sort_links,
            'current_sort': sort,
            'package_filter': package_filter,
            'package_pills': package_pills,
            'form': LeadForm(employee=user),
        },
    )
