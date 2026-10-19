import calendar as calendar_module
import json
import re
from functools import wraps
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Case, Count, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import (
    ExcelImportForm,
    FollowUpForm,
    LeadForm,
    PackageForm,
    QuickFollowUpForm,
    RescheduleFollowUpForm,
    TaskForm,
)
from .models import (
    Activity,
    ActivityLog,
    EmployeeProfile,
    FollowUp,
    Lead,
    Package,
    Task,
)
from .rbac import (
    can_access_sales_pipeline,
    can_view_all_sales_data,
    is_sales_manager,
    ROLE_SALES,
)
from .utils import (
    get_report_data,
    import_leads_from_excel,
    log_activity,
    recalc_lead_next_followup,
)

User = get_user_model()

def sales_pipeline_required(view_fn):
    """Block CRM *dev* role from sales, pipeline, packages, and financial summary screens."""

    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not can_access_sales_pipeline(request.user):
            return HttpResponseForbidden(
                'This area is not available for your account role.'
            )
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _profile(user):
    return EmployeeProfile.objects.get_or_create(user=user)[0]


def _sales_team_users_qs():
    """Active users on the sales team (crm_role='sales'), for manager scoping + dropdowns."""
    return (
        User.objects.filter(
            is_active=True, crm_profile__crm_role=ROLE_SALES
        )
        .order_by('username')
    )


def _sales_scope_user_ids(user):
    """User ids a sales manager (or admin) may view/edit sales data for — the sales team + self."""
    ids = set(_sales_team_users_qs().values_list('id', flat=True))
    ids.add(user.id)
    return ids


def _sales_scope_context(request, user):
    """
    Shared employee-scope state for leads / follow-ups / achievements list views.
    Returns manager_mode, the employee ids in scope, the selected employee (None = "all"),
    and the queryset of employees to show in the picker.
    """
    manager_mode = can_view_all_sales_data(user)
    if not manager_mode:
        return {
            'manager_mode': False,
            'scope_ids': {user.id},
            'selected_employee': None,
            'scope_employees': None,
        }
    scope_employees = _sales_team_users_qs()
    scope_ids = _sales_scope_user_ids(user)
    emp_param = (request.GET.get('employee') or '').strip()
    selected_employee = None
    if emp_param.isdigit():
        selected_employee = next(
            (e for e in scope_employees if e.id == int(emp_param)), None
        )
    return {
        'manager_mode': True,
        'scope_ids': {selected_employee.id} if selected_employee else scope_ids,
        'selected_employee': selected_employee,
        'scope_employees': scope_employees,
    }


def _lead_scope_qs(user):
    """Leads a user may view/edit — own leads, or (sales managers/admins) the whole sales team."""
    if can_view_all_sales_data(user):
        return Lead.objects.filter(employee_id__in=_sales_scope_user_ids(user))
    return Lead.objects.filter(employee=user)


def _followup_scope_qs(user):
    if can_view_all_sales_data(user):
        return FollowUp.objects.filter(employee_id__in=_sales_scope_user_ids(user))
    return FollowUp.objects.filter(employee=user)


def _task_scope_qs(user):
    if can_view_all_sales_data(user):
        return Task.objects.filter(employee_id__in=_sales_scope_user_ids(user))
    return Task.objects.filter(employee=user)


def _can_manage_employee_data(user, target_employee_id):
    """Superuser, the employee themself, or a sales manager acting within their team."""
    if user.is_superuser or target_employee_id == user.id:
        return True
    return is_sales_manager(user) and target_employee_id in _sales_scope_user_ids(user)


def _local_today_bounds():
    now = timezone.now()
    if timezone.is_aware(now):
        local = timezone.localtime(now).date()
    else:
        local = now.date()
    start = timezone.make_aware(datetime.combine(local, datetime.min.time()))
    end = start + timedelta(days=1)
    return start, end, local


def _aware_day_bounds(d):
    """Local calendar date d → [start, next_day) as aware datetimes."""
    start = timezone.make_aware(datetime.combine(d, datetime.min.time()))
    return start, start + timedelta(days=1)


def _date_scope_bounds(scope, date_start_str, date_end_str):
    """
    Return (start_aware, end_aware) for filtering created_at or next_followup.
    end is exclusive. None if scope invalid / custom incomplete.
    """
    now = timezone.now()
    local = timezone.localtime(now).date() if timezone.is_aware(now) else now.date()

    if scope == 'today':
        return _aware_day_bounds(local)
    if scope == 'yesterday':
        d = local - timedelta(days=1)
        return _aware_day_bounds(d)
    if scope == 'this_week':
        monday = local - timedelta(days=local.weekday())
        s, _ = _aware_day_bounds(monday)
        return s, s + timedelta(days=7)
    if scope == 'this_month':
        first = local.replace(day=1)
        if first.month == 12:
            nxt = first.replace(year=first.year + 1, month=1, day=1)
        else:
            nxt = first.replace(month=first.month + 1, day=1)
        s = timezone.make_aware(datetime.combine(first, datetime.min.time()))
        e = timezone.make_aware(datetime.combine(nxt, datetime.min.time()))
        return s, e
    if scope == 'custom':
        try:
            ds = datetime.strptime((date_start_str or '').strip(), '%Y-%m-%d').date()
            de = datetime.strptime((date_end_str or '').strip(), '%Y-%m-%d').date()
        except ValueError:
            return None
        if de < ds:
            ds, de = de, ds
        s, _ = _aware_day_bounds(ds)
        _, e = _aware_day_bounds(de)
        return s, e + timedelta(days=1)
    return None


def _leads_url_query(base_filters, **overrides):
    """Merge filter dict + overrides; omit empty values for clean query strings."""
    d = {**base_filters, **overrides}
    p = d.get('page')
    try:
        if p is None or str(p).strip() == '' or int(p) <= 1:
            d.pop('page', None)
    except (TypeError, ValueError):
        pass
    return urlencode({k: str(v) for k, v in d.items() if v not in (None, '')})


def _hx_toast(response, message):
    """Attach toast trigger for HTMX requests (does not alter body)."""
    if isinstance(response, HttpResponse):
        response['HX-Trigger'] = json.dumps({'crmToast': message})
    return response


def _followups_queue_context(user, scope_ids=None):
    start, end, _ = _local_today_bounds()
    if scope_ids is None:
        scope_ids = (
            _sales_scope_user_ids(user) if can_view_all_sales_data(user) else {user.id}
        )
    base = FollowUp.objects.filter(employee_id__in=scope_ids).select_related('lead', 'employee')
    today = list(
        base.filter(is_done=False, datetime__gte=start, datetime__lt=end).order_by(
            'datetime'
        )
    )
    upcoming = list(
        base.filter(is_done=False, datetime__gte=end).order_by('datetime')[:80]
    )
    overdue = list(
        base.filter(is_done=False, datetime__lt=start).order_by('datetime')[:80]
    )
    return {
        'today': today,
        'upcoming': upcoming,
        'overdue': overdue,
    }


# Won/Lost/Nurture live on Lead.lifecycle_state (set via crm.services.pipeline),
# not on `status` — `status` is purely the 7-stage pipeline position now.
INTERESTED_SORT_STATUSES = (
    Lead.Status.QUALIFIED,
    Lead.Status.DEMO,
    Lead.Status.PROPOSAL,
    Lead.Status.NEGOTIATION,
)


# Default leads-list order (newest created first) when no column sort is chosen.
LEAD_DEFAULT_ORDER = ('-created_at', '-id')

# Leads-table column sort: query-param key -> queryset field(s) to order by.
# 'status' sorts by pipeline position (Status.choices order), not alphabetically.
LEAD_SORT_FIELDS = {
    'name': ('name',),
    'phone': ('phone',),
    'package': ('package__name',),
    'value': ('deal_value',),
    'last_activity': ('last_act_at',),
    'next_followup': ('next_activity__scheduled_at',),
    'status': ('_status_order',),
}

LEADS_PER_PAGE = 10


def _flag_lead_rows(leads_page, bounds):
    """
    Tag each lead with why it belongs in the active period's list, for the row
    badges: is_new_in_period (created within the period) and
    is_followup_in_period (follow-up scheduled within the period — exact
    window only, no overdue/unscheduled folding). `bounds` is (ds, de) for the
    active date_scope, or None when viewing "All time" (no period, no badges).
    Cheap Python-side flags on an already-paginated page — not a DB annotation.
    """
    for lead in leads_page:
        if bounds:
            ds, de = bounds
            lead.is_new_in_period = ds <= lead.created_at < de
            next_at = lead.next_activity.scheduled_at if lead.next_activity_id else None
            lead.is_followup_in_period = bool(next_at) and ds <= next_at < de
        else:
            lead.is_new_in_period = False
            lead.is_followup_in_period = False
    return leads_page


def _lead_for_exec(user, pk):
    """Single lead with activity + task counts for execution board."""
    start, end, local_date = _local_today_bounds()
    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    return (
        _lead_scope_qs(user).filter(pk=pk)
        .select_related('package', 'employee', 'next_activity')
        .annotate(
            last_act=Subquery(latest.values('action')[:1]),
            last_act_at=Subquery(latest.values('created_at')[:1]),
            task_open_count=Count(
                'tasks', filter=Q(tasks__is_completed=False)
            ),
            task_overdue_count=Count(
                'tasks',
                filter=Q(
                    tasks__is_completed=False,
                    tasks__due_date__isnull=False,
                    tasks__due_date__lt=local_date,
                ),
            ),
            task_done_count=Count('tasks', filter=Q(tasks__is_completed=True)),
        )
        .first()
    )


def _exec_board_ctx(lead, user, **extra):
    start, end, _ = _local_today_bounds()
    # No request/date_scope context available on this single-row re-render path
    # (POST body only carries _tpl) — fall back to today's window, the default
    # and overwhelmingly common case for whichever list this row lives on.
    _flag_lead_rows([lead], (start, end))
    ctx = {
        'lead': lead,
        'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
        'detailed_status_choices': Lead.Status.choices,
        'packages': Package.objects.all(),
        'fu_start': start,
        'fu_end': end,
        'fu_bounds': (start, end),
        'manager_mode': can_view_all_sales_data(user),
    }
    ctx.update(extra)
    return ctx


def _patch_lead_from_post(lead, user, request):
    """Apply POST fields to lead; returns list of changes for logging."""
    old_status = lead.status
    if 'status' in request.POST:
        new_st = request.POST.get('status')
        if new_st in dict(Lead.Status.choices):
            lead.status = new_st
            if old_status != new_st:
                log_activity(lead, 'status_change', f'{old_status} → {new_st}')
    pkg_changed = False
    if 'package' in request.POST:
        old_pkg_id = lead.package_id
        pid = request.POST.get('package') or ''
        if pid == '':
            lead.package = None
        else:
            # Packages are a shared, platform-wide catalog (see packages.html) — not
            # scoped to whichever manager created them — so any package can be applied
            # to any lead regardless of who owns it.
            pkg = Package.objects.filter(pk=pid).first()
            if pkg:
                lead.package = pkg
        if old_pkg_id != lead.package_id:
            pkg_changed = True
            log_activity(
                lead,
                'package_change',
                str(lead.package) if lead.package else '—',
            )
            if lead.package_id:
                lead.deal_value = lead.package.price
    if 'deal_value' in request.POST:
        try:
            lead.deal_value = Decimal(str(request.POST.get('deal_value', '0') or '0'))
        except Exception:
            pass

    if 'name' in request.POST:
        new_name = (request.POST.get('name') or '').strip()[:200]
        if new_name and new_name != lead.name:
            log_activity(lead, 'contact_updated', f'name → {new_name[:80]}')
        if new_name:
            lead.name = new_name
    if 'phone' in request.POST:
        lead.phone = (request.POST.get('phone') or '').strip()[:40]
    if 'email' in request.POST:
        lead.email = (request.POST.get('email') or '').strip()[:254]
    if 'source' in request.POST:
        lead.source = (request.POST.get('source') or '').strip()[:120]

    lead.save()


@login_required
def dashboard(request):
    user = request.user
    if not can_access_sales_pipeline(user):
        return render(request, 'crm/dashboard.html', {'show_sales_dashboard': False})
    leads = Lead.objects.filter(employee=user)
    followups = FollowUp.objects.filter(employee=user)
    start, end, today = _local_today_bounds()

    today_fu = list(
        followups.filter(is_done=False, datetime__gte=start, datetime__lt=end)
        .select_related('lead')
        .order_by('datetime')
    )
    overdue_fu = list(
        followups.filter(is_done=False, datetime__lt=start)
        .select_related('lead')
        .order_by('datetime')[:40]
    )

    total_leads = leads.count()
    interested = leads.filter(
        status__in=INTERESTED_SORT_STATUSES, lifecycle_state=Lead.LifecycleState.ACTIVE,
    ).count()
    closed_won = leads.filter(lifecycle_state=Lead.LifecycleState.WON).count()
    revenue = (
        leads.filter(lifecycle_state=Lead.LifecycleState.WON).aggregate(s=Sum('deal_value'))['s']
        or Decimal('0')
    )
    profile = _profile(user)
    target = profile.target_amount or Decimal('0')

    since = timezone.now() - timedelta(days=30)
    per_day = list(
        leads.filter(created_at__gte=since)
        .annotate(d=TruncDate('created_at'))
        .values('d')
        .annotate(c=Count('id'))
        .order_by('d')
    )
    leads_chart_labels = [x['d'].isoformat() if x['d'] else '' for x in per_day]
    leads_chart_data = [x['c'] for x in per_day]

    won_count = closed_won
    conv_pct = round((won_count / total_leads * 100), 1) if total_leads else 0

    six_mo = timezone.now() - timedelta(days=185)
    rev_monthly = list(
        leads.filter(lifecycle_state=Lead.LifecycleState.WON, won_at__gte=six_mo)
        .annotate(m=TruncMonth('won_at'))
        .values('m')
        .annotate(total=Sum('deal_value'))
        .order_by('m')
    )
    rev_labels = [x['m'].strftime('%Y-%m') if x['m'] else '' for x in rev_monthly]
    rev_values = [float(x['total'] or 0) for x in rev_monthly]

    ctx = {
        'show_sales_dashboard': True,
        'today_followups': today_fu,
        'overdue_followups': overdue_fu,
        'total_leads': total_leads,
        'interested': interested,
        'closed_won': closed_won,
        'revenue': revenue,
        'target': target,
        'leads_chart_labels': json.dumps(leads_chart_labels),
        'leads_chart_data': json.dumps(leads_chart_data),
        'conversion_pct': conv_pct,
        'rev_chart_labels': json.dumps(rev_labels),
        'rev_chart_values': json.dumps(rev_values),
    }
    return render(request, 'crm/dashboard.html', ctx)


def _leads_list_qs_and_meta(request, user):
    """
    Build filtered + ordered Lead queryset and filter state for leads_list / infinite scroll.
    """
    start, end, local_date = _local_today_bounds()
    active_q = Q(lifecycle_state=Lead.LifecycleState.ACTIVE)
    scope = _sales_scope_context(request, user)

    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    qs = (
        Lead.objects.filter(employee_id__in=scope['scope_ids'])
        .select_related('package', 'employee', 'next_activity')
        .annotate(
            last_act=Subquery(latest.values('action')[:1]),
            last_act_at=Subquery(latest.values('created_at')[:1]),
            task_open_count=Count(
                'tasks', filter=Q(tasks__is_completed=False)
            ),
            task_overdue_count=Count(
                'tasks',
                filter=Q(
                    tasks__is_completed=False,
                    tasks__due_date__isnull=False,
                    tasks__due_date__lt=local_date,
                ),
            ),
            task_done_count=Count('tasks', filter=Q(tasks__is_completed=True)),
        )
    )

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    stage = request.GET.get('stage', '').strip()
    if stage in Lead.STAGE_TO_STATUSES:
        qs = qs.filter(status__in=Lead.STAGE_TO_STATUSES[stage])

    high_hope_filter = request.GET.get('high_hope', '').strip()
    if high_hope_filter == '1':
        qs = qs.filter(high_hope=True)

    pkg = request.GET.get('package')
    package_filter = int(pkg) if pkg and pkg.isdigit() else None
    if package_filter:
        qs = qs.filter(package_id=package_filter)

    created_day = request.GET.get('created_day', '').strip()
    if created_day:
        try:
            d = datetime.strptime(created_day, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date=d)
        except ValueError:
            pass

    closed_day = request.GET.get('closed_day', '').strip()
    if closed_day:
        try:
            d = datetime.strptime(closed_day, '%Y-%m-%d').date()
            qs = qs.filter(lifecycle_state=Lead.LifecycleState.WON, won_at__date=d)
        except ValueError:
            pass

    created_month = request.GET.get('created_month', '').strip()
    if created_month and len(created_month) >= 7:
        try:
            y, m = int(created_month[:4]), int(created_month[5:7])
            qs = qs.filter(created_at__year=y, created_at__month=m)
        except ValueError:
            pass

    closed_month = request.GET.get('closed_month', '').strip()
    if closed_month and len(closed_month) >= 7:
        try:
            y, m = int(closed_month[:4]), int(closed_month[5:7])
            qs = qs.filter(
                lifecycle_state=Lead.LifecycleState.WON, won_at__year=y, won_at__month=m,
            )
        except ValueError:
            pass

    # Default landing (no ?date_scope= at all) is "All time" — every lead in
    # scope, newest first. Picking a period — Today / Yesterday / This week /
    # This month — uses the exact same rule: leads created within the
    # window, OR leads with a follow-up scheduled exactly within the window.
    # No overdue-folding, no "no follow-up set" catch-all — those cases
    # would otherwise leak months-old, never-touched leads into every single
    # period forever. Overdue tracking is the dedicated Follow-ups page's
    # job, not this one.
    date_scope = request.GET.get('date_scope', None)
    if date_scope is None:
        date_scope = 'all'
    date_start_s = request.GET.get('date_start', '').strip()
    date_end_s = request.GET.get('date_end', '').strip()

    sort_by_time = False
    period_bounds = None
    if date_scope in ('today', 'yesterday', 'this_week', 'this_month', 'custom'):
        bounds = _date_scope_bounds(date_scope, date_start_s, date_end_s)
        if bounds:
            ds, de = bounds
            qs = qs.filter(
                Q(created_at__gte=ds, created_at__lt=de)
                | (active_q & Q(next_activity__scheduled_at__gte=ds, next_activity__scheduled_at__lt=de))
            )
            sort_by_time = True
            period_bounds = (ds, de)
    # 'all' (or anything else) — no date filter at all, show everyone in scope.

    # Explicit column sort (click a table header) always wins over the
    # default/period ordering below — it's a view preference the user just
    # asked for, not part of the filtered result set.
    sort = request.GET.get('sort', '').strip()
    sort_dir = request.GET.get('dir', '').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort not in LEAD_SORT_FIELDS:
        sort = ''

    if sort:
        if sort == 'status':
            status_order = Case(
                *[When(status=code, then=Value(i)) for i, (code, _label) in enumerate(Lead.Status.choices)],
                default=Value(len(Lead.Status.choices)),
                output_field=IntegerField(),
            )
            qs = qs.annotate(_status_order=status_order)
        order_fields = LEAD_SORT_FIELDS[sort]
        qs = qs.order_by(*(f'-{f}' if sort_dir == 'desc' else f for f in order_fields), '-id')
    elif sort_by_time:
        # One mixed list of leads + follow-ups: sort by whichever timestamp is
        # this row's actual moment — its scheduled follow-up, or (if it has none)
        # when the lead itself was created.
        qs = qs.annotate(_sort_time=Coalesce('next_activity__scheduled_at', 'created_at')).order_by('_sort_time')
    else:
        qs = qs.order_by(*LEAD_DEFAULT_ORDER)

    filters_ctx = {
        'q': q,
        'stage': stage,
        'high_hope': high_hope_filter,
        'package': pkg or '',
        'created_day': created_day,
        'closed_day': closed_day,
        'created_month': created_month,
        'closed_month': closed_month,
        'date_scope': date_scope,
        'date_start': date_start_s,
        'date_end': date_end_s,
        'employee': str(scope['selected_employee'].id) if scope['selected_employee'] else '',
        'sort': sort,
        'dir': sort_dir if sort else '',
    }

    has_active_filters = bool(
        q
        or stage
        or high_hope_filter
        or package_filter
        or created_day
        or closed_day
        or created_month
        or closed_month
        or (date_scope and date_scope != 'all')  # 'all' is the default, not a user-applied filter
    )

    return {
        'qs': qs,
        'start': start,
        'end': end,
        'local_date': local_date,
        'period_bounds': period_bounds,
        'filters_ctx': filters_ctx,
        'package_filter': package_filter,
        'has_active_filters': has_active_filters,
        'pkg': pkg,
        'manager_mode': scope['manager_mode'],
        'scope_employees': scope['scope_employees'],
        'selected_employee': scope['selected_employee'],
        'scope_ids': scope['scope_ids'],
        'sort': sort,
        'sort_dir': sort_dir,
    }


@login_required
@sales_pipeline_required
def leads_list(request):
    user = request.user
    meta = _leads_list_qs_and_meta(request, user)
    qs = meta['qs']
    start, end = meta['start'], meta['end']
    period_bounds = meta['period_bounds']
    filters_ctx = meta['filters_ctx']
    package_filter = meta['package_filter']
    has_active_filters = meta['has_active_filters']
    manager_mode = meta['manager_mode']
    scope_employees = meta['scope_employees']
    selected_employee = meta['selected_employee']
    scope_ids = meta['scope_ids']
    sort = meta['sort']
    sort_dir = meta['sort_dir']

    packages = Package.objects.all()

    page_raw = (request.GET.get('page') or '1').strip()
    paginator = Paginator(qs, LEADS_PER_PAGE)
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        last = paginator.num_pages or 1
        page_obj = paginator.page(last)

    leads_page = _flag_lead_rows(list(page_obj.object_list), period_bounds)
    for i, lead in enumerate(leads_page):
        lead.row_number = page_obj.start_index() + i

    form = LeadForm(employee=user)
    import_form = ExcelImportForm()

    _lq = lambda **kw: _leads_url_query(filters_ctx, **kw)
    leads_base = reverse('crm:leads')
    lqs = {
        'date_all': _lq(date_scope='all', date_start='', date_end=''),
        'date_today': _lq(date_scope='today', date_start='', date_end=''),
        'date_yesterday': _lq(date_scope='yesterday', date_start='', date_end=''),
        'date_week': _lq(date_scope='this_week', date_start='', date_end=''),
        'date_month': _lq(date_scope='this_month', date_start='', date_end=''),
        'high_hope_all': _lq(high_hope=''),
        'high_hope_on': _lq(high_hope='1'),
    }

    # Sortable column headers: clicking a column sorts ascending; clicking the
    # already-active column flips direction. Any other active filter/page is
    # preserved in the link via filters_ctx.
    sort_links = {}
    for key in LEAD_SORT_FIELDS:
        next_dir = 'desc' if (sort == key and sort_dir == 'asc') else 'asc'
        sort_links[key] = _lq(sort=key, dir=next_dir, page='')

    page_items = []
    for n in paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1):
        if isinstance(n, int):
            page_items.append({
                'num': n,
                'qs': _lq(page=n) if n != 1 else _lq(page=''),
                'active': n == page_obj.number,
            })
        else:
            page_items.append(None)  # ellipsis

    employee_pills = []
    if manager_mode and scope_employees:
        employee_pills = [{'id': None, 'label': 'All leads', 'qs': _lq(employee='')}]
        for e in scope_employees:
            employee_pills.append({
                'id': e.id,
                'label': e.get_full_name() or e.username,
                'qs': _lq(employee=e.id),
            })

    package_pills = [{'id': None, 'label': 'All packages', 'qs': _lq(package='')}]
    for p in packages:
        package_pills.append({'id': p.id, 'label': p.name, 'qs': _lq(package=p.id)})

    # Global summary strip counts (always reflect full pipeline, not current filters, but honor employee scope)
    _all_leads = Lead.objects.filter(employee_id__in=scope_ids)
    all_leads_total = _all_leads.count()

    # Pipeline stage tabs, each with a live count of the full scoped pipeline.
    stage_pills = [{'key': '', 'label': 'All', 'count': all_leads_total, 'qs': _lq(stage='')}]
    for _stage_key, _stage_label in Lead.PIPELINE_STAGES:
        stage_pills.append({
            'key': _stage_key,
            'label': _stage_label,
            'count': _all_leads.filter(status__in=Lead.STAGE_TO_STATUSES[_stage_key]).count(),
            'qs': _lq(stage=_stage_key),
        })

    pending_tasks_count = Task.objects.filter(employee_id__in=scope_ids, is_completed=False).count()

    prev_qs = _lq(page=page_obj.previous_page_number()) if page_obj.has_previous() else ''
    next_qs = _lq(page=page_obj.next_page_number()) if page_obj.has_next() else ''

    return render(
        request,
        'crm/leads.html',
        {
            'leads': leads_page,
            'paginator': paginator,
            'page_obj': page_obj,
            'page_items': page_items,
            'prev_qs': prev_qs,
            'next_qs': next_qs,
            'all_leads_total': all_leads_total,
            'packages': packages,
            'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
            'form': form,
            'import_form': import_form,
            'fu_start': start,
            'fu_end': end,
            'fu_bounds': (start, end),
            'filters': filters_ctx,
            'leads_base': leads_base,
            'lqs': lqs,
            'sort_links': sort_links,
            'current_sort': sort,
            'current_dir': sort_dir,
            'stage_pills': stage_pills,
            'package_filter': package_filter,
            'has_active_filters': has_active_filters,
            'pending_tasks_count': pending_tasks_count,
            'manager_mode': manager_mode,
            'scope_employees': scope_employees,
            'selected_employee': selected_employee,
            'employee_pills': employee_pills,
            'package_pills': package_pills,
        },
    )


@login_required
@sales_pipeline_required
def lead_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 1:
        return JsonResponse({'results': []})
    rows = (
        _lead_scope_qs(request.user)
        .filter(Q(name__icontains=q) | Q(phone__icontains=q))
        .order_by('-updated_at')[:15]
    )
    return JsonResponse(
        {
            'results': [
                {'id': L.pk, 'name': L.name, 'phone': L.phone or ''} for L in rows
            ]
        }
    )


@login_required
@sales_pipeline_required
@require_POST
def lead_quick_add(request):
    """Minimal create: name + phone only."""
    user = request.user
    name = (request.POST.get('name') or '').strip()
    phone = (request.POST.get('phone') or '').strip()
    if not name:
        return HttpResponse(status=204)
    lead = Lead.objects.create(
        employee=user,
        name=name[:200],
        phone=phone[:40],
    )
    log_activity(lead, 'created', 'Quick add')
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:leads'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_create(request):
    user = request.user
    form = LeadForm(request.POST, employee=user)
    if form.is_valid():
        if request.POST.get('confirm_duplicate') != '1':
            existing = form.find_duplicate()
            if existing:
                return render(
                    request,
                    'crm/partials/lead_dup_warning.html',
                    {'existing': existing},
                )
        lead = form.save(commit=False)
        lead.employee = user
        lead.save()
        if lead.package_id and lead.deal_value == Decimal('0'):
            lead.deal_value = lead.package.price
            lead.save(update_fields=['deal_value'])
        log_activity(lead, 'created', f'Status: {lead.get_status_display()}')
        messages.success(request, 'Lead created.')
        if request.headers.get('HX-Request'):
            r = HttpResponse()
            r['HX-Location'] = json.dumps({
                'path': reverse('crm:leads'),
                'target': '#crm-main-content',
                'select': '#crm-main-content',
                'swap': 'outerHTML',
            })
            return r
        return HttpResponse(status=204)
    messages.error(request, form.errors.as_text())
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:leads'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_patch(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    _patch_lead_from_post(lead, user, request)
    lead.refresh_from_db()

    if not request.headers.get('HX-Request'):
        return HttpResponse(status=204)

    tpl = request.POST.get('_tpl', 'exec_row')
    lead = _lead_for_exec(user, pk)
    ctx = _exec_board_ctx(lead, user)
    if tpl == 'sticky':
        body = render_to_string('crm/partials/lead_detail_sticky.html', ctx, request)
    else:
        body = render_to_string('crm/partials/lead_table_row.html', ctx, request)
    resp = HttpResponse(body)
    if request.headers.get('HX-Request'):
        _hx_toast(resp, 'Updated')
    return resp


@login_required
@sales_pipeline_required
@require_POST
def lead_high_hope_toggle(request, pk):
    """
    Toggle lead.high_hope and return the matching partial for HTMX targets.
    """
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    lead.high_hope = not lead.high_hope
    lead.save(update_fields=['high_hope'])

    # HTMX: re-render only the affected UI fragment.
    if request.headers.get('HX-Request'):
        tpl = request.POST.get('_tpl', 'exec_row')
        lead_ann = _lead_for_exec(user, pk)
        ctx = _exec_board_ctx(lead_ann, user)
        if tpl == 'sticky':
            body = render_to_string('crm/partials/lead_detail_sticky.html', ctx, request)
        else:
            body = render_to_string('crm/partials/lead_table_row.html', ctx, request)
        resp = HttpResponse(body)
        _hx_toast(resp, 'Updated')
        return resp

    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_quick_followup(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    form = QuickFollowUpForm(request.POST)
    err = None
    if form.is_valid():
        dt = form.cleaned_data['fu_datetime']
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        fu = FollowUp.objects.create(
            lead=lead,
            employee=lead.employee,
            datetime=dt,
            note=form.cleaned_data.get('fu_note') or '',
        )
        recalc_lead_next_followup(lead)
        log_activity(lead, 'follow_up_scheduled', form.cleaned_data.get('fu_note') or '')
        lead.refresh_from_db()
    else:
        err = 'Invalid date/time'

    if request.headers.get('HX-Request'):
        lead_ann = _lead_for_exec(user, pk)
        ctx = _exec_board_ctx(lead_ann, user, quick_fu_error=err)
        resp = render(request, 'crm/partials/lead_table_row.html', ctx)
        if err:
            resp['HX-Trigger'] = json.dumps({'crmFuModalError': err})
        else:
            resp['HX-Trigger'] = json.dumps({'crmToast': 'Scheduled', 'crmCloseFuModal': True})
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_notes_save(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    lead.notes = request.POST.get('notes', '')[:10000]
    lead.save(update_fields=['notes', 'updated_at'])
    log_activity(lead, 'notes_updated', '')
    if request.headers.get('HX-Request'):
        return render(
            request,
            'crm/partials/lead_notes_status.html',
            {'ok': True},
        )
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_contact_save(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    name = (request.POST.get('name') or '').strip()[:200]
    phone = (request.POST.get('phone') or '').strip()[:40]
    email = (request.POST.get('email') or '').strip()[:254]
    source = (request.POST.get('source') or '').strip()[:120]
    err = None
    fv = {'name': name, 'phone': phone, 'email': email, 'source': source}
    if not name:
        err = 'Name is required.'
    else:
        lead.name = name
        lead.phone = phone
        lead.email = email
        lead.source = source
        try:
            lead.full_clean()
        except ValidationError as e:
            err = '; '.join(
                m for msgs in (e.message_dict or {}).values() for m in msgs
            ) or str(e)
            lead.refresh_from_db()
        else:
            lead.save()
            log_activity(lead, 'contact_updated', 'Contact details')
    start, end, _ = _local_today_bounds()
    ctx = {
        'lead': lead,
        'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
        'detailed_status_choices': Lead.Status.choices,
        'packages': Package.objects.all(),
        'fu_start': start,
        'fu_end': end,
        'contact_save_error': err,
        'contact_saved_ok': not err and name,
        'contact_fv': fv if err else None,
    }
    if request.headers.get('HX-Request'):
        body = render_to_string('crm/partials/lead_contact_host.html', ctx, request)
        body += render_to_string('crm/partials/lead_sticky_oob.html', ctx, request)
        body += render_to_string('crm/partials/lead_crumb_oob.html', ctx, request)
        resp = HttpResponse(body)
        if not err:
            _hx_toast(resp, 'Contact saved')
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
def lead_detail(request, pk):
    user = request.user
    lead = get_object_or_404(
        _lead_scope_qs(user).select_related('package', 'employee', 'place', 'extraction_item'), pk=pk
    )
    activities = lead.activities.all()[:100]
    followups = lead.followups.all().order_by('-datetime')[:50]
    tasks = lead.tasks.all()
    bills = lead.bills.all().order_by('-bill_date', '-created_at')
    fu_form = FollowUpForm()
    task_form = TaskForm()
    start, end, _ = _local_today_bounds()
    return render(
        request,
        'crm/lead_detail.html',
        {
            'lead': lead,
            'activities': activities,
            'followups': followups,
            'tasks': tasks,
            'bills': bills,
            'fu_form': fu_form,
            'task_form': task_form,
            'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
            'detailed_status_choices': Lead.Status.choices,
            'packages': Package.objects.all(),
            'fu_start': start,
            'fu_end': end,
            'manager_mode': can_view_all_sales_data(user),
        },
    )


@login_required
@sales_pipeline_required
@require_POST
def lead_status_detail(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    old = lead.status
    st = request.POST.get('status')
    if st in dict(Lead.Status.choices):
        lead.status = st
        lead.save(update_fields=['status', 'updated_at'])
        if old != st:
            log_activity(lead, 'status_change', f'{old} → {st}')
        if st == Lead.Status.WON and lead.lifecycle_state != Lead.LifecycleState.WON:
            from .services.pipeline import PipelineError, mark_won
            try:
                mark_won(lead, user=user)
            except PipelineError:
                pass  # status still updated above; lifecycle stays Active until deal_value is set
    if request.headers.get('HX-Request'):
        resp = render(
            request,
            'crm/partials/lead_detail_sticky.html',
            {
                'lead': lead,
                'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
                'detailed_status_choices': Lead.Status.choices,
                'packages': Package.objects.all(),
                'fu_start': _local_today_bounds()[0],
                'fu_end': _local_today_bounds()[1],
            },
        )
        _hx_toast(resp, 'Updated')
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_mark_won(request, pk):
    """
    Centralized Won transition — same crm.services.pipeline.mark_won used
    by the Daily Workbench's outcome engine, reachable here too (Pipeline
    board, lead detail) so there is one single path to "Won" everywhere.
    """
    from .services.pipeline import PipelineError, mark_won

    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    try:
        mark_won(lead, user=user)
    except PipelineError as e:
        return HttpResponse(str(e), status=400)
    return HttpResponse(status=200)


@login_required
@sales_pipeline_required
@require_POST
def lead_mark_lost(request, pk):
    """Centralized Lost transition — same crm.services.pipeline.mark_lost
    the Daily Workbench's outcome engine uses."""
    from .services.pipeline import PipelineError, mark_lost

    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    reason = (request.POST.get('reason') or '').strip()
    note = (request.POST.get('note') or '').strip()
    try:
        mark_lost(lead, reason=reason, note=note, user=user)
    except PipelineError as e:
        return HttpResponse(str(e), status=400)
    return HttpResponse(status=200)


@login_required
@sales_pipeline_required
@require_POST
def followup_add(request, lead_pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=lead_pk)
    form = FollowUpForm(request.POST)
    fu_error = None
    if form.is_valid():
        fu = form.save(commit=False)
        fu.lead = lead
        fu.employee = lead.employee
        if timezone.is_naive(fu.datetime):
            fu.datetime = timezone.make_aware(fu.datetime)
        fu.save()
        recalc_lead_next_followup(lead)
        log_activity(lead, 'follow_up_scheduled', fu.note[:200])
        lead.refresh_from_db()
    else:
        fu_error = form.errors.as_text()

    if request.headers.get('HX-Request'):
        resp = render(
            request,
            'crm/partials/lead_detail_post_fu.html',
            {
                'lead': lead,
                'followups': lead.followups.all().order_by('-datetime')[:50],
                'tasks': lead.tasks.all(),
                'fu_form': FollowUpForm(),
                'task_form': TaskForm(),
                'fu_error': fu_error,
                'task_error': None,
                'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
                'packages': Package.objects.all(),
            },
        )
        if not fu_error:
            _hx_toast(resp, 'Scheduled')
        return resp
    return HttpResponse(status=204)


def _tasks_panel_ctx(user, lead_pk):
    _, _, local_date = _local_today_bounds()
    lead_ann = _lead_for_exec(user, lead_pk)
    tasks = (
        Task.objects.filter(lead_id=lead_pk)
        .order_by('is_completed', 'due_date', 'id')
    )
    return {
        'lead': lead_ann,
        'tasks': tasks,
        'task_form': TaskForm(),
        'task_error': None,
        'crm_local_date': local_date,
    }


@login_required
@sales_pipeline_required
def lead_tasks_panel(request, pk):
    user = request.user
    get_object_or_404(_lead_scope_qs(user), pk=pk)
    return render(
        request,
        'crm/partials/lead_tasks_panel_inner.html',
        _tasks_panel_ctx(user, pk),
    )


@login_required
@sales_pipeline_required
@require_POST
def task_add(request, lead_pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=lead_pk)
    form = TaskForm(request.POST)
    task_error = None
    if form.is_valid():
        t = form.save(commit=False)
        t.lead = lead
        t.employee = lead.employee
        t.save()
        log_activity(lead, 'task_added', t.title)
    else:
        task_error = form.errors.as_text()

    if request.headers.get('HX-Request'):
        if request.POST.get('_from') == 'exec_modal':
            lead_ann = _lead_for_exec(user, lead_pk)
            badge = render_to_string(
                'crm/partials/exec_task_badge_oob.html',
                {'lead': lead_ann},
                request=request,
            )
            if form.is_valid():
                resp = HttpResponse(badge)
                resp['HX-Trigger'] = json.dumps(
                    {
                        'crmToast': 'Task added',
                        'crmRefreshTaskHeader': True,
                        'crmCloseTaskModal': True,
                        'crmTaskPanelRefresh': lead_pk,
                    }
                )
                return resp
            err = render_to_string(
                'crm/partials/task_modal_err_oob.html',
                {'msg': (task_error or 'Invalid task').strip()},
                request=request,
            )
            return HttpResponse(err)
        if request.POST.get('_from') == 'exec':
            ctx = _tasks_panel_ctx(user, lead_pk)
            ctx['task_error'] = task_error
            inner = render_to_string(
                'crm/partials/lead_tasks_panel_inner.html',
                ctx,
                request=request,
            )
            lead_ann = _lead_for_exec(user, lead_pk)
            badge = render_to_string(
                'crm/partials/exec_task_badge_oob.html',
                {'lead': lead_ann},
                request=request,
            )
            resp = HttpResponse(inner + badge)
            trig = {}
            if not task_error:
                trig['crmToast'] = 'Task added'
                trig['crmRefreshTaskHeader'] = True
            if trig:
                resp['HX-Trigger'] = json.dumps(trig)
            return resp
        return render(
            request,
            'crm/partials/lead_detail_post_fu.html',
            {
                'lead': lead,
                'followups': lead.followups.all().order_by('-datetime')[:50],
                'tasks': lead.tasks.all(),
                'fu_form': FollowUpForm(),
                'task_form': TaskForm(),
                'fu_error': None,
                'task_error': task_error,
                'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
                'packages': Package.objects.all(),
            },
        )
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def task_update(request, pk):
    user = request.user
    task = get_object_or_404(_task_scope_qs(user), pk=pk)
    title = (request.POST.get('title') or '').strip()[:300]
    due = (request.POST.get('due_date') or '').strip()
    due_time = (request.POST.get('due_time') or '').strip()
    if title:
        task.title = title
    if due:
        try:
            task.due_date = datetime.strptime(due, '%Y-%m-%d').date()
        except ValueError:
            pass
    elif 'due_date' in request.POST and not due:
        task.due_date = None
    if due_time:
        try:
            task.due_time = datetime.strptime(due_time, '%H:%M').time()
        except ValueError:
            pass
    elif 'due_time' in request.POST and not due_time:
        task.due_time = None
    if 'due_date' in request.POST or 'due_time' in request.POST:
        # Due moment changed — let the reminder job re-evaluate from scratch.
        task.reminder_30_sent_at = None
        task.reminder_sent_at = None
        task.overdue_notified_at = None
        task.escalated_at = None
    task.save()
    log_activity(task.lead, 'task_updated', task.title[:200])
    if request.headers.get('HX-Request') and request.POST.get('_from') == 'exec':
        ctx = _tasks_panel_ctx(user, task.lead_id)
        inner = render_to_string(
            'crm/partials/lead_tasks_panel_inner.html',
            ctx,
            request=request,
        )
        lead_ann = _lead_for_exec(user, task.lead_id)
        badge = render_to_string(
            'crm/partials/exec_task_badge_oob.html',
            {'lead': lead_ann},
            request=request,
        )
        resp = HttpResponse(inner + badge)
        resp['HX-Trigger'] = json.dumps(
            {'crmToast': 'Saved', 'crmRefreshTaskHeader': True}
        )
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def task_toggle(request, pk):
    user = request.user
    task = get_object_or_404(_task_scope_qs(user), pk=pk)
    task.is_completed = not task.is_completed
    task.save(update_fields=['is_completed'])
    log_activity(
        task.lead,
        'task_updated',
        f'{"Done" if task.is_completed else "Reopened"}: {task.title}',
    )
    if request.headers.get('HX-Request'):
        if request.POST.get('_from') == 'exec':
            ctx = _tasks_panel_ctx(user, task.lead_id)
            inner = render_to_string(
                'crm/partials/lead_tasks_panel_inner.html',
                ctx,
                request=request,
            )
            lead_ann = _lead_for_exec(user, task.lead_id)
            badge = render_to_string(
                'crm/partials/exec_task_badge_oob.html',
                {'lead': lead_ann},
                request=request,
            )
            resp = HttpResponse(inner + badge)
            resp['HX-Trigger'] = json.dumps(
                {
                    'crmToast': 'Done' if task.is_completed else 'Updated',
                    'crmRefreshTaskHeader': True,
                }
            )
            return resp
        if request.POST.get('_from') == 'header' and task.is_completed:
            r = HttpResponse()
            r['HX-Trigger'] = json.dumps(
                {'crmToast': 'Done', 'crmRefreshTaskHeader': True}
            )
            return r
        resp = render(
            request, 'crm/partials/task_exec_row.html', {'task': task}
        )
        if task.is_completed:
            _hx_toast(resp, 'Done')
        else:
            _hx_toast(resp, 'Updated')
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_log_call(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    log_activity(lead, 'call', '')
    if request.headers.get('HX-Request'):
        r = HttpResponse(status=204)
        _hx_toast(r, 'Call logged')
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_log_whatsapp(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    log_activity(lead, 'whatsapp', '')
    if request.headers.get('HX-Request'):
        r = HttpResponse(status=204)
        _hx_toast(r, 'WhatsApp')
        return r
    return HttpResponse(status=204)


@login_required
def tasks_header_dropdown(request):
    user = request.user
    _, _, local_date = _local_today_bounds()
    tasks_overdue = list(
        Task.objects.filter(
            employee=user,
            is_completed=False,
            due_date__isnull=False,
            due_date__lt=local_date,
        )
        .select_related('lead')
        .order_by('due_date', 'id')[:30]
    )
    tasks_today = list(
        Task.objects.filter(
            employee=user,
            is_completed=False,
            due_date=local_date,
        )
        .select_related('lead')
        .order_by('due_date', 'id')[:30]
    )
    tasks_undated = list(
        Task.objects.filter(
            employee=user,
            is_completed=False,
            due_date__isnull=True,
        )
        .select_related('lead')
        .order_by('id')[:30]
    )
    return render(
        request,
        'crm/partials/tasks_header_dropdown.html',
        {
            'tasks_overdue': tasks_overdue,
            'tasks_today': tasks_today,
            'tasks_undated': tasks_undated,
        },
    )


@login_required
def tasks_header_badges(request):
    user = request.user
    _, _, local_date = _local_today_bounds()
    overdue_n = Task.objects.filter(
        employee=user,
        is_completed=False,
        due_date__isnull=False,
        due_date__lt=local_date,
    ).count()
    today_n = Task.objects.filter(
        employee=user,
        is_completed=False,
        due_date=local_date,
    ).count()
    open_n = Task.objects.filter(
        employee=user,
        is_completed=False,
    ).count()
    return render(
        request,
        'crm/partials/tasks_header_badges.html',
        {
            'tasks_overdue_n': overdue_n,
            'tasks_today_n': today_n,
            'tasks_open_n': open_n,
        },
    )


def _add_months(year, month, delta):
    m = month - 1 + delta
    y = year + m // 12
    m = m % 12 + 1
    return y, m


def _parse_calendar_ym(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get('y') or today.year)
        month = int(request.GET.get('m') or today.month)
        date(year, month, 1)  # validates the month is real
    except (TypeError, ValueError):
        year, month = today.year, today.month
    return year, month


def _parse_calendar_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _followup_calendar_ctx(scope_ids, year, month, selected_date):
    """Month grid (Sun-first, padded with adjacent-month days) with a
    pending-activity count + dominant type (for the badge color) per day.
    Activity (crm.services.pipeline) is the one source of truth for 'what's
    next' across the whole CRM — the same records the Daily Workbench and
    Pipeline board read — so this calendar is just another view onto them,
    not a separate schedule."""
    today = timezone.localdate()
    cal = calendar_module.Calendar(firstweekday=calendar_module.SUNDAY)
    month_weeks = cal.monthdatescalendar(year, month)
    grid_start, grid_end = month_weeks[0][0], month_weeks[-1][-1]

    rows = (
        Activity.objects.filter(
            owner_id__in=scope_ids,
            status=Activity.Status.PENDING,
            scheduled_at__date__gte=grid_start,
            scheduled_at__date__lte=grid_end,
        )
        .annotate(day=TruncDate('scheduled_at'))
        .values('day', 'activity_type')
        .annotate(n=Count('id'))
    )
    day_meta = {}
    for row in rows:
        meta = day_meta.setdefault(row['day'], {'count': 0, 'types': {}})
        meta['count'] += row['n']
        meta['types'][row['activity_type']] = meta['types'].get(row['activity_type'], 0) + row['n']
    for meta in day_meta.values():
        meta['dominant_type'] = max(meta['types'].items(), key=lambda kv: kv[1])[0]

    weeks = []
    for week in month_weeks:
        days = []
        for d in week:
            meta = day_meta.get(d)
            days.append({
                'date': d,
                'day': d.day,
                'in_month': d.month == month,
                'is_today': d == today,
                'is_selected': d == selected_date,
                'count': meta['count'] if meta else 0,
                'dominant_type': meta['dominant_type'] if meta else '',
            })
        weeks.append(days)

    prev_y, prev_m = _add_months(year, month, -1)
    next_y, next_m = _add_months(year, month, 1)
    return {
        'year': year,
        'month': month,
        'month_label': date(year, month, 1).strftime('%B %Y'),
        'weeks': weeks,
        'prev_y': prev_y, 'prev_m': prev_m,
        'next_y': next_y, 'next_m': next_m,
        'today_y': today.year, 'today_m': today.month, 'today_iso': today.isoformat(),
        'selected_date': selected_date,
    }


def _followup_day_panel_ctx(scope_ids, selected_date):
    start = timezone.make_aware(datetime.combine(selected_date, time.min))
    end = start + timedelta(days=1)
    day_activities = list(
        Activity.objects.filter(
            owner_id__in=scope_ids, status=Activity.Status.PENDING,
            scheduled_at__gte=start, scheduled_at__lt=end,
        )
        .select_related('lead', 'owner')
        .order_by('scheduled_at')
    )
    return {
        'selected_date': selected_date,
        'day_activities': day_activities,
    }


@login_required
@sales_pipeline_required
def followups_page(request):
    user = request.user
    scope = _sales_scope_context(request, user)
    today = timezone.localdate()
    year, month = _parse_calendar_ym(request)
    selected_date = _parse_calendar_date(request.GET.get('date'), today)

    ctx = _followup_calendar_ctx(scope['scope_ids'], year, month, selected_date)
    ctx.update(_followup_day_panel_ctx(scope['scope_ids'], selected_date))
    ctx['manager_mode'] = scope['manager_mode']
    ctx['scope_employees'] = scope['scope_employees']
    ctx['selected_employee'] = scope['selected_employee']
    return render(request, 'crm/followups.html', ctx)


@login_required
@sales_pipeline_required
def followups_calendar_month(request):
    """HTMX partial: the calendar grid only, for prev/next month navigation."""
    user = request.user
    scope = _sales_scope_context(request, user)
    year, month = _parse_calendar_ym(request)
    selected_date = _parse_calendar_date(request.GET.get('date'), date(year, month, 1))
    ctx = _followup_calendar_ctx(scope['scope_ids'], year, month, selected_date)
    return render(request, 'crm/partials/followups_calendar_grid.html', ctx)


@login_required
@sales_pipeline_required
def followups_day_panel(request):
    """HTMX partial: the selected day's pending activities. Completing one
    goes through the exact same Workbench 'Work' modal/endpoint
    (crm:workbench_lead_panel / crm:workbench_complete_activity) — one
    completion flow for the whole CRM, not a second copy of it here."""
    user = request.user
    scope = _sales_scope_context(request, user)
    selected_date = _parse_calendar_date(request.GET.get('date'), timezone.localdate())
    ctx = _followup_day_panel_ctx(scope['scope_ids'], selected_date)
    ctx['manager_mode'] = scope['manager_mode']
    return render(request, 'crm/partials/followups_day_panel.html', ctx)


@login_required
@sales_pipeline_required
@require_POST
def followup_done(request, pk):
    user = request.user
    fu = get_object_or_404(_followup_scope_qs(user), pk=pk)
    fu.is_done = True
    fu.save(update_fields=['is_done'])
    recalc_lead_next_followup(fu.lead)
    log_activity(fu.lead, 'follow_up_done', fu.note[:200])
    if request.headers.get('HX-Request'):
        from_queue = request.POST.get('_from') == 'followups_queue'
        if from_queue:
            ctx = _followups_queue_context(user)
            ctx['hx_oob'] = True
            ctx['manager_mode'] = can_view_all_sales_data(user)
            resp = render(request, 'crm/partials/followups_list.html', ctx)
            _hx_toast(resp, 'Done')
            return resp
        r = HttpResponse(status=200)
        r['HX-Trigger'] = json.dumps({'crmToast': 'Done', 'crmFuDone': fu.pk})
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def followup_reschedule(request, pk):
    user = request.user
    fu = get_object_or_404(
        _followup_scope_qs(user).select_related('lead'), pk=pk
    )
    form = RescheduleFollowUpForm(request.POST)
    if form.is_valid():
        ndt = form.cleaned_data['new_datetime']
        if timezone.is_naive(ndt):
            ndt = timezone.make_aware(ndt)
        fu.datetime = ndt
        fu.reminder_sent_at = None
        fu.reminder_30_sent_at = None
        fu.overdue_notified_at = None
        fu.escalated_at = None
        fu.save(update_fields=[
            'datetime', 'reminder_sent_at', 'reminder_30_sent_at', 'overdue_notified_at', 'escalated_at',
        ])
        recalc_lead_next_followup(fu.lead)
        log_activity(fu.lead, 'follow_up_rescheduled', str(ndt))
        if request.headers.get('HX-Request'):
            ctx = _followups_queue_context(user)
            ctx['hx_oob'] = True
            ctx['manager_mode'] = can_view_all_sales_data(user)
            resp = render(request, 'crm/partials/followups_list.html', ctx)
            _hx_toast(resp, 'Rescheduled')
            return resp
        return HttpResponse(status=204)

    start, end, _ = _local_today_bounds()
    if fu.datetime < start:
        bucket = 'overdue'
    elif fu.datetime < end:
        bucket = 'today'
    else:
        bucket = 'upcoming'
    err_list = form.errors.get('new_datetime', ['Check date/time'])
    reschedule_error = err_list[0] if isinstance(err_list, list) else str(err_list)

    if request.headers.get('HX-Request'):
        return render(
            request,
            'crm/partials/followup_exec_card.html',
            {
                'fu': fu,
                'bucket': bucket,
                'reschedule_error': reschedule_error,
                'hx_oob': True,
                'manager_mode': can_view_all_sales_data(user),
            },
        )
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
def packages_page(request):
    user = request.user
    can_manage = can_view_all_sales_data(user)
    packages = Package.objects.all().order_by('name')
    form = PackageForm()
    edit_obj = None
    if can_manage:
        edit_id = request.GET.get('edit')
        if edit_id and edit_id.isdigit():
            edit_obj = Package.objects.filter(pk=int(edit_id)).first()
    edit_form = PackageForm(instance=edit_obj) if edit_obj else None
    return render(
        request,
        'crm/packages.html',
        {
            'packages': packages,
            'form': form,
            'edit_obj': edit_obj,
            'edit_form': edit_form,
            'can_manage_packages': can_manage,
        },
    )


@login_required
@sales_pipeline_required
@require_http_methods(['GET', 'POST'])
def package_create(request):
    user = request.user
    if not can_view_all_sales_data(user):
        return HttpResponse(status=403)
    if request.method == 'POST':
        form = PackageForm(request.POST)
        if form.is_valid():
            p = form.save(commit=False)
            p.employee = user
            p.save()
        if request.headers.get('HX-Request'):
            r = HttpResponse()
            r['HX-Location'] = json.dumps({
                'path': reverse('crm:packages'),
                'target': '#crm-main-content',
                'select': '#crm-main-content',
                'swap': 'outerHTML',
            })
            return r
        return HttpResponse(status=204)
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def package_update(request, pk):
    user = request.user
    if not can_view_all_sales_data(user):
        return HttpResponse(status=403)
    pkg = get_object_or_404(Package, pk=pk)
    form = PackageForm(request.POST, instance=pkg)
    if form.is_valid():
        form.save()
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:packages'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def package_delete(request, pk):
    user = request.user
    if not can_view_all_sales_data(user):
        return HttpResponse(status=403)
    Package.objects.filter(pk=pk).delete()
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:packages'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def leads_import_excel(request):
    user = request.user
    form = ExcelImportForm(request.POST, request.FILES)
    if not form.is_valid():
        if request.headers.get('HX-Request'):
            r = HttpResponse()
            r['HX-Location'] = json.dumps({
                'path': reverse('crm:leads'),
                'target': '#crm-main-content',
                'select': '#crm-main-content',
                'swap': 'outerHTML',
            })
            return r
        return HttpResponse(status=204)
    result = import_leads_from_excel(form.cleaned_data['file'], user)
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:leads'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
def performance(request):
    user = request.user
    leads = Lead.objects.filter(employee=user)
    total = leads.count()
    won = leads.filter(lifecycle_state=Lead.LifecycleState.WON).count()
    revenue = (
        leads.filter(lifecycle_state=Lead.LifecycleState.WON).aggregate(s=Sum('deal_value'))['s']
        or Decimal('0')
    )
    conv = round((won / total * 100), 1) if total else 0

    since = timezone.now() - timedelta(days=60)
    daily_created = list(
        leads.filter(created_at__gte=since)
        .annotate(d=TruncDate('created_at'))
        .values('d')
        .annotate(c=Count('id'))
        .order_by('d')
    )
    daily_won = list(
        leads.filter(lifecycle_state=Lead.LifecycleState.WON, won_at__gte=since)
        .annotate(d=TruncDate('won_at'))
        .values('d')
        .annotate(c=Count('id'))
        .order_by('d')
    )

    perf_labels = [x['d'].isoformat() for x in daily_created]
    perf_created = [x['c'] for x in daily_created]
    won_map = {x['d']: x['c'] for x in daily_won}
    perf_won = [won_map.get(x['d'], 0) for x in daily_created]

    monthly = list(
        leads.annotate(m=TruncMonth('created_at'))
        .values('m')
        .annotate(created=Count('id'))
        .order_by('m')
    )
    last12 = monthly[-12:] if len(monthly) > 12 else monthly
    m_labels = [x['m'].strftime('%Y-%m') if x['m'] else '' for x in last12]
    m_created = [x['created'] for x in last12]
    m_won_list = []
    m_rev_list = []
    for x in last12:
        mm = x['m']
        if mm:
            won_q = leads.filter(
                lifecycle_state=Lead.LifecycleState.WON,
                won_at__year=mm.year,
                won_at__month=mm.month,
            )
            m_won_list.append(won_q.count())
            m_rev_list.append(
                float(won_q.aggregate(s=Sum('deal_value'))['s'] or 0)
            )
        else:
            m_won_list.append(0)
            m_rev_list.append(0.0)

    ctx = {
        'total_leads': total,
        'conversions': won,
        'conv_pct': conv,
        'revenue': revenue,
        'perf_labels': json.dumps(perf_labels),
        'perf_created': json.dumps(perf_created),
        'perf_won': json.dumps(perf_won),
        'm_labels': json.dumps(m_labels),
        'm_created': json.dumps(m_created),
        'm_won': json.dumps(m_won_list),
        'm_rev': json.dumps(m_rev_list),
        'sales_report': get_report_data(user, 'daily'),
    }
    return render(request, 'crm/performance.html', ctx)


@login_required
@sales_pipeline_required
def performance_report_card(request):
    """HTMX fragment: Sales Report Card for Daily / Weekly / Monthly."""
    period = (request.GET.get('period') or 'daily').strip().lower()
    return render(
        request,
        'crm/partials/sales_report_card_inner.html',
        {'r': get_report_data(request.user, period)},
    )


