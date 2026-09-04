import json
import logging
import re
from functools import wraps
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Sum, Value, When
from django.db.models.functions import TruncDate, TruncMonth
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .services.whatsapp import (
    handle_message,
    is_duplicate_event,
    mask_phone,
    get_active_wa_number,
)
from .services.achievements import get_monthly_performance
from .forms import (
    AchievementForm,
    AuditFilterForm,
    ChangeRequestFilterForm,
    ChangeRequestPortalForm,
    ChangeRequestStaffForm,
    CompleteForm,
    ExcelImportForm,
    FollowUpForm,
    LeadConvertForm,
    LeadForm,
    OnboardingAgreementForm,
    OnboardingBrandingForm,
    OnboardingBusinessInfoForm,
    OnboardingContactForm,
    OnboardingContentForm,
    OnboardingDocumentForm,
    OnboardingPaymentKycForm,
    OnboardingRequirementsForm,
    PackageForm,
    PackageScopeForm,
    PortalActivateForm,
    ProjectCredentialForm,
    ProjectAssigneesForm,
    ProjectForm,
    ProjectHandoverForm,
    ProjectTicketForm,
    ProvisioningStepStatusForm,
    QuickFollowUpForm,
    QuickNoteForm,
    QuoteForm,
    RejectForm,
    RenewalFilterForm,
    RenewalTrackerForm,
    RescheduleFollowUpForm,
    TaskForm,
    TriageForm,
)
from .models import (
    Achievement,
    ActivityLog,
    AuditEntry,
    ChangeRequest,
    Client,
    CredentialAuditLog,
    EmployeeProfile,
    FollowUp,
    HandoverPortalAccess,
    Lead,
    MonthlyTarget,
    OnboardingSubmission,
    Package,
    Project,
    ProjectCredential,
    ProjectTicket,
    ProjectHandover,
    ProjectProvisioning,
    ProvisioningStep,
    RenewalReminderLog,
    RenewalTracker,
    Task,
    WhatsAppBotExcludePhone,
)
from .services import change_requests as change_request_service
from .services import onboarding as onboarding_service
from .services import credentials as credential_service
from .services import portal as portal_service
from .services import provisioning as provisioning_service
from .services import readiness as readiness_service
from .services import renewals as renewals_service
from .services import scope as scope_service
from .services import audit as audit_service
from .rbac import (
    can_access_audit_trail,
    can_access_change_requests,
    can_access_operations_dashboard,
    can_access_renewals_dashboard,
    can_access_sales_pipeline,
    can_complete_handover,
    can_edit_credentials,
    can_edit_package_scope,
    can_manage_portal,
    can_manage_provisioning,
    can_send_renewal_reminder_manual,
    can_view_all_sales_data,
    can_view_credential_audit,
    credential_allowed_for_role,
    get_crm_role,
    is_sales_manager,
    ROLE_SALES,
)
from .services.project import (
    convert_lead_to_project,
    create_project,
    get_or_create_client_for_lead,
    get_projects_for_user,
    set_project_assignees,
    update_project_status,
)
from .services import project_tickets as ticket_service
from .crypto import decrypt_ciphertext
from .utils import (
    get_report_data,
    import_leads_from_excel,
    log_activity,
    recalc_lead_next_followup,
)

User = get_user_model()

logger = logging.getLogger(__name__)


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


# Lead status values (match crm.models.Lead.Status enum values).
# We use explicit strings in conditions so we don't depend on enum member names.
STATUS_NEW = 'new'
STATUS_CLOSED = 'closed'
STATUS_LOST = 'lost'
STATUS_LOST_AFTER_PROPOSAL = 'lost_after_proposal'

STATUS_WHATSAPP_CONNECTED = 'whatsapp_connected'
STATUS_CALL_CONNECTED = 'call_connected'
STATUS_CLOSING_ONGOING = 'closing_ongoing'

STATUS_PROPOSAL_SENT = 'proposal_sent'
STATUS_NEGOTIATION_AFTER_PROPOSAL = 'negotiation_after_proposal'
STATUS_FAILED_RETRY = 'failed_retry'

TERMINAL_STATUSES = (STATUS_CLOSED, STATUS_LOST, STATUS_LOST_AFTER_PROPOSAL)
INTERESTED_SORT_STATUSES = (
    STATUS_CLOSING_ONGOING,
    STATUS_PROPOSAL_SENT,
    STATUS_NEGOTIATION_AFTER_PROPOSAL,
    STATUS_FAILED_RETRY,
)
HOT_ACTIVE_FILTER_EXCLUDE_STATUSES = (STATUS_NEW,) + TERMINAL_STATUSES


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp_webhook(request):
    """
    WhatsApp inbound webhook endpoint (transport-agnostic).

    Meta Cloud API transport was removed; we now expect a WhatsApp Web.js bridge
    to POST inbound messages here.
    """
    if request.method == 'GET':
        return JsonResponse({'status': 'ok'})

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (TypeError, ValueError, UnicodeDecodeError):
        logger.exception('Invalid WhatsApp webhook payload')
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    # Expected Web.js payload (bridge -> Django):
    # {
    #   "account_id": "exec1",           # maps to WhatsAppNumber.phone_number_id (temporary)
    #   "from": "9198....",             # customer phone digits (or +E164; we normalize downstream)
    #   "text": "hi",
    #   "message_id": "provider-id",    # optional but recommended for dedupe
    #   "timestamp": 1716...,           # optional epoch seconds
    #   "raw": {...}                    # optional provider payload
    # }
    account_id = str(payload.get('account_id') or '').strip()
    phone = payload.get('from') or payload.get('phone') or payload.get('customer_phone')
    text = payload.get('text') or payload.get('message') or ''
    message_id = str(payload.get('message_id') or payload.get('id') or '').strip()
    ts = payload.get('timestamp')

    if not phone or not str(text).strip():
        return JsonResponse({'status': 'ignored', 'reason': 'missing_phone_or_text'})

    wa_number = get_active_wa_number(account_id) if account_id else None
    provider_dt = None
    try:
        if ts:
            provider_dt = timezone.datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        provider_dt = None

    if message_id and is_duplicate_event(message_id):
        logger.info('Ignored duplicate WhatsApp message id=%s phone=%s', message_id, mask_phone(phone))
        return JsonResponse({'status': 'ok', 'processed': 0, 'duplicates': 1})

    logger.info('Processing WA(webjs) message id=%s phone=%s account_id=%s', message_id, mask_phone(phone), account_id)
    handle_message(
        phone,
        text,
        wa_number=wa_number,
        message_id=message_id,
        raw_payload=(payload.get('raw') if isinstance(payload.get('raw'), dict) else payload),
        provider_timestamp=provider_dt,
    )
    return JsonResponse({'status': 'ok', 'processed': 1, 'duplicates': 0})


# Fixed leads-list order (newest created first) — no user-facing sort picker.
LEAD_DEFAULT_ORDER = ('-created_at', '-id')

LEADS_PER_PAGE = 20


def _lead_for_exec(user, pk):
    """Single lead with activity + task counts for execution board."""
    start, end, local_date = _local_today_bounds()
    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    return (
        _lead_scope_qs(user).filter(pk=pk)
        .select_related('package', 'employee')
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
            pkg = Package.objects.filter(pk=pid, employee_id=lead.employee_id).first()
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
        status__in=INTERESTED_SORT_STATUSES
    ).count()
    closed_won = leads.filter(status=STATUS_CLOSED).count()
    revenue = (
        leads.filter(status=STATUS_CLOSED).aggregate(s=Sum('deal_value'))['s']
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

    won_count = leads.filter(status=STATUS_CLOSED).count()
    conv_pct = round((won_count / total_leads * 100), 1) if total_leads else 0

    six_mo = timezone.now() - timedelta(days=185)
    rev_monthly = list(
        leads.filter(status=STATUS_CLOSED, updated_at__gte=six_mo)
        .annotate(m=TruncMonth('updated_at'))
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
    active_q = ~Q(status__in=TERMINAL_STATUSES)
    scope = _sales_scope_context(request, user)

    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    qs = (
        Lead.objects.filter(employee_id__in=scope['scope_ids'])
        .select_related('package', 'employee')
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

    fu_filter = request.GET.get('fu', '')
    if fu_filter == 'overdue':
        qs = qs.filter(
            active_q & (Q(next_followup__lt=start) | Q(next_followup__isnull=True))
        )
    elif fu_filter == 'today':
        qs = qs.filter(
            active_q,
            next_followup__gte=start,
            next_followup__lt=end,
        )
    elif fu_filter == 'hot':
        qs = qs.filter(active_q).exclude(status=STATUS_NEW).filter(deal_value__gt=0)

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

    closed_statuses = Lead.STAGE_TO_STATUSES['closed']

    closed_day = request.GET.get('closed_day', '').strip()
    if closed_day:
        try:
            d = datetime.strptime(closed_day, '%Y-%m-%d').date()
            qs = qs.filter(status__in=closed_statuses, updated_at__date=d)
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
                status__in=closed_statuses, updated_at__year=y, updated_at__month=m
            )
        except ValueError:
            pass

    date_scope = request.GET.get('date_scope', '').strip()
    date_start_s = request.GET.get('date_start', '').strip()
    date_end_s = request.GET.get('date_end', '').strip()
    if date_scope in (
        'today',
        'yesterday',
        'this_week',
        'this_month',
        'custom',
    ):
        bounds = _date_scope_bounds(date_scope, date_start_s, date_end_s)
        if bounds:
            ds, de = bounds
            qs = qs.filter(created_at__gte=ds, created_at__lt=de)

    qs = qs.order_by(*LEAD_DEFAULT_ORDER)

    filters_ctx = {
        'q': q,
        'stage': stage,
        'high_hope': high_hope_filter,
        'fu': fu_filter,
        'package': pkg or '',
        'created_day': created_day,
        'closed_day': closed_day,
        'created_month': created_month,
        'closed_month': closed_month,
        'date_scope': date_scope,
        'date_start': date_start_s,
        'date_end': date_end_s,
        'employee': str(scope['selected_employee'].id) if scope['selected_employee'] else '',
    }

    has_active_filters = bool(
        q
        or stage
        or high_hope_filter
        or fu_filter
        or package_filter
        or created_day
        or closed_day
        or created_month
        or closed_month
        or date_scope
    )

    return {
        'qs': qs,
        'start': start,
        'end': end,
        'local_date': local_date,
        'filters_ctx': filters_ctx,
        'package_filter': package_filter,
        'has_active_filters': has_active_filters,
        'pkg': pkg,
        'manager_mode': scope['manager_mode'],
        'scope_employees': scope['scope_employees'],
        'selected_employee': scope['selected_employee'],
        'scope_ids': scope['scope_ids'],
    }


@login_required
@sales_pipeline_required
def leads_list(request):
    user = request.user
    meta = _leads_list_qs_and_meta(request, user)
    qs = meta['qs']
    start, end = meta['start'], meta['end']
    filters_ctx = meta['filters_ctx']
    package_filter = meta['package_filter']
    has_active_filters = meta['has_active_filters']
    manager_mode = meta['manager_mode']
    scope_employees = meta['scope_employees']
    selected_employee = meta['selected_employee']
    scope_ids = meta['scope_ids']

    packages = Package.objects.all()

    # Full document always starts at batch 1 (ignore ?page=). Infinite scroll uses /leads/more/?page=…
    page_raw = '1'
    paginator = Paginator(qs, LEADS_PER_PAGE)
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        last = paginator.num_pages or 1
        page_obj = paginator.page(last)

    leads_page = list(page_obj.object_list)

    form = LeadForm(employee=user)
    import_form = ExcelImportForm()

    _lq = lambda **kw: _leads_url_query(filters_ctx, **kw)
    leads_base = reverse('crm:leads')
    lqs = {
        'date_all': _lq(date_scope='', date_start='', date_end=''),
        'date_today': _lq(date_scope='today', date_start='', date_end=''),
        'date_yesterday': _lq(date_scope='yesterday', date_start='', date_end=''),
        'date_week': _lq(date_scope='this_week', date_start='', date_end=''),
        'date_month': _lq(date_scope='this_month', date_start='', date_end=''),
        'fu_all': _lq(fu=''),
        'fu_overdue': _lq(fu='overdue'),
        'fu_today': _lq(fu='today'),
        'fu_hot': _lq(fu='hot'),
        'high_hope_all': _lq(high_hope=''),
        'high_hope_on': _lq(high_hope='1'),
    }

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

    _active_q = ~Q(status__in=TERMINAL_STATUSES)
    overdue_count = _all_leads.filter(
        _active_q & (Q(next_followup__lt=start) | Q(next_followup__isnull=True))
    ).count()
    today_fu_count = _all_leads.filter(
        _active_q, next_followup__gte=start, next_followup__lt=end
    ).count()
    pending_tasks_count = Task.objects.filter(employee_id__in=scope_ids, is_completed=False).count()
    hot_leads_count = _all_leads.filter(
        _active_q,
        # Exclude brand-new leads, keep active pipeline + deal value.
        ~Q(status=STATUS_NEW),
        deal_value__gt=0,
    ).count()

    leads_more_url = reverse('crm:leads_more')
    pagination_next_qs = (
        _leads_url_query(filters_ctx, page=page_obj.next_page_number())
        if page_obj.has_next()
        else ''
    )

    return render(
        request,
        'crm/leads.html',
        {
            'leads': leads_page,
            'paginator': paginator,
            'page_obj': page_obj,
            'all_leads_total': all_leads_total,
            'leads_more_url': leads_more_url,
            'pagination_next_qs': pagination_next_qs,
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
            'stage_pills': stage_pills,
            'package_filter': package_filter,
            'has_active_filters': has_active_filters,
            'overdue_count': overdue_count,
            'today_fu_count': today_fu_count,
            'pending_tasks_count': pending_tasks_count,
            'hot_leads_count': hot_leads_count,
            'manager_mode': manager_mode,
            'scope_employees': scope_employees,
            'selected_employee': selected_employee,
            'employee_pills': employee_pills,
            'package_pills': package_pills,
        },
    )


@login_required
@sales_pipeline_required
def leads_more_json(request):
    """JSON chunk for infinite scroll (next page of lead rows)."""
    user = request.user
    meta = _leads_list_qs_and_meta(request, user)
    qs = meta['qs']
    start, end = meta['start'], meta['end']
    filters_ctx = meta['filters_ctx']

    page_raw = (request.GET.get('page') or '1').strip()
    paginator = Paginator(qs, LEADS_PER_PAGE)
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        return JsonResponse(
            {
                'desktop_html': '',
                'mobile_html': '',
                'has_more': False,
                'next_querystring': '',
            }
        )

    leads_page = list(page_obj.object_list)
    packages = Package.objects.all()
    base_ctx = {
        'fu_start': start,
        'fu_end': end,
        'fu_bounds': (start, end),
        'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
        'packages': packages,
        'manager_mode': meta['manager_mode'],
    }
    desk_parts = []
    mob_parts = []
    for lead in leads_page:
        ctx = {**base_ctx, 'lead': lead}
        desk_parts.append(
            render_to_string('crm/partials/lead_exec_board.html', ctx, request=request)
        )
        mob_parts.append(
            render_to_string('crm/partials/lead_mobile_card.html', ctx, request=request)
        )

    next_qs = ''
    if page_obj.has_next():
        next_qs = _leads_url_query(filters_ctx, page=page_obj.next_page_number())

    resp = JsonResponse(
        {
            'desktop_html': ''.join(desk_parts),
            'mobile_html': ''.join(mob_parts),
            'has_more': page_obj.has_next(),
            'next_querystring': next_qs,
        }
    )
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp['Pragma'] = 'no-cache'
    return resp


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
        resp = render(request, 'crm/partials/lead_detail_sticky.html', ctx)
    elif tpl == 'mobile_card':
        resp = render(request, 'crm/partials/lead_mobile_card.html', ctx)
    else:
        resp = render(request, 'crm/partials/lead_exec_board.html', ctx)
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
            resp = render(request, 'crm/partials/lead_detail_sticky.html', ctx)
        elif tpl == 'mobile_card':
            resp = render(request, 'crm/partials/lead_mobile_card.html', ctx)
        else:
            resp = render(request, 'crm/partials/lead_exec_board.html', ctx)
        _hx_toast(resp, 'Updated')
        return resp

    return HttpResponse(status=204)


def _whatsapp_bot_panel_context(user):
    profile, _ = EmployeeProfile.objects.get_or_create(user=user)
    return {
        'crm_whatsapp_bot_enabled': profile.whatsapp_bot_enabled,
        'crm_employee_profile': profile,
        'crm_wa_excludes': WhatsAppBotExcludePhone.objects.filter(executive=user),
    }


@login_required
@sales_pipeline_required
@require_POST
def whatsapp_bot_toggle(request):
    """Toggle CRM master switch for WhatsApp auto-replies (per executive)."""
    profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
    profile.whatsapp_bot_enabled = not profile.whatsapp_bot_enabled
    profile.save(update_fields=['whatsapp_bot_enabled'])

    if request.headers.get('HX-Request'):
        resp = render(
            request,
            'crm/partials/whatsapp_bot_panel.html',
            _whatsapp_bot_panel_context(request.user),
        )
        state = 'ON — bot will reply on WhatsApp' if profile.whatsapp_bot_enabled else 'OFF — messages stored only'
        _hx_toast(resp, state)
        return resp

    return redirect('crm:leads')


@login_required
@sales_pipeline_required
@require_POST
def whatsapp_bot_exclude_add(request):
    """Add a phone number to this executive's bot exclude list."""
    raw = str(request.POST.get('phone') or '').strip()
    label = str(request.POST.get('label') or '').strip()[:120]
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) < 10:
        resp = render(
            request,
            'crm/partials/whatsapp_bot_panel.html',
            {**_whatsapp_bot_panel_context(request.user), 'wa_exclude_error': 'Enter a valid phone with country code.'},
        )
        _hx_toast(resp, 'Invalid phone')
        return resp

    WhatsAppBotExcludePhone.objects.get_or_create(
        executive=request.user,
        phone=digits,
        defaults={'label': label},
    )
    resp = render(
        request,
        'crm/partials/whatsapp_bot_panel.html',
        _whatsapp_bot_panel_context(request.user),
    )
    _hx_toast(resp, 'Number excluded from bot')
    return resp


@login_required
@sales_pipeline_required
@require_POST
def whatsapp_bot_exclude_remove(request, pk):
    """Remove a phone from the bot exclude list."""
    WhatsAppBotExcludePhone.objects.filter(pk=pk, executive=request.user).delete()
    resp = render(
        request,
        'crm/partials/whatsapp_bot_panel.html',
        _whatsapp_bot_panel_context(request.user),
    )
    _hx_toast(resp, 'Removed from exclude list')
    return resp


def _wa_gateway_config():
    base = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_URL', '') or '').strip().rstrip('/')
    token = str(getattr(settings, 'WHATSAPP_WEBJS_BRIDGE_TOKEN', '') or '').strip()
    return base, token


def _wa_gateway_sessions():
    import requests

    base, token = _wa_gateway_config()
    if not base or not token:
        return None, 'WhatsApp gateway is not configured (bridge URL or token missing).'
    try:
        resp = requests.get(
            f'{base}/sessions',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return list(data.get('sessions') or []), ''
    except Exception as exc:
        logger.exception('WA gateway sessions fetch failed')
        return None, f'Gateway unreachable: {exc}'


def _wa_gateway_qr_html(session_id: str):
    import requests

    base, token = _wa_gateway_config()
    if not base or not token:
        return '', 'WhatsApp gateway is not configured (bridge URL or token missing).'
    sid = str(session_id or '').strip()
    if not sid or not re.fullmatch(r'[a-zA-Z0-9_-]+', sid):
        return '', 'Invalid session id.'
    try:
        resp = requests.get(
            f'{base}/qr/{sid}',
            params={'token': token},
            timeout=15,
        )
        if resp.status_code == 401:
            return '', 'Gateway rejected the bridge token. Check WHATSAPP_WEBJS_BRIDGE_TOKEN matches wa_gateway/.env.'
        resp.raise_for_status()
        html = resp.text or ''
        m = re.search(r'<div class="wrap">\s*(.*?)\s*</div>\s*</body>', html, re.S | re.I)
        body = (m.group(1).strip() if m else '')
        if not body:
            return '', 'No QR available — session may already be linked. Refresh in a few seconds.'
        return body, ''
    except Exception as exc:
        logger.exception('WA gateway QR fetch failed session=%s', sid)
        return '', f'Gateway unreachable: {exc}'


@login_required
@sales_pipeline_required
@require_http_methods(['GET'])
def whatsapp_qr_list(request):
    """Staff CRM page: list WA gateway sessions (login required; bridge token stays server-side)."""
    sessions, err = _wa_gateway_sessions()
    return render(
        request,
        'crm/wa_qr_list.html',
        {'sessions': sessions or [], 'wa_qr_error': err},
    )


@login_required
@sales_pipeline_required
@require_http_methods(['GET'])
def whatsapp_qr_session(request, session_id):
    """Staff CRM page: show QR for one gateway session (admin1, admin2, …)."""
    qr_html, err = _wa_gateway_qr_html(session_id)
    return render(
        request,
        'crm/wa_qr_session.html',
        {
            'session_id': session_id,
            'qr_html': qr_html,
            'wa_qr_error': err,
        },
    )


@login_required
@sales_pipeline_required
@require_http_methods(['GET'])
def whatsapp_session_health(request):
    """Production dashboard: live gateway session vs CRM mapping + bot toggle."""
    from crm.services.wa_gateway_sync import build_session_health_rows

    gateway_sessions, err = _wa_gateway_sessions()
    rows = build_session_health_rows(gateway_sessions)
    return render(
        request,
        'crm/wa_session_health.html',
        {
            'rows': rows,
            'wa_qr_error': err,
        },
    )


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
        tpl = request.POST.get('_tpl', 'exec_row')
        lead_ann = _lead_for_exec(user, pk)
        ctx = _exec_board_ctx(lead_ann, user, quick_fu_error=err)
        tmpl = (
            'crm/partials/lead_mobile_card.html'
            if tpl == 'mobile_card'
            else 'crm/partials/lead_exec_board.html'
        )
        resp = render(request, tmpl, ctx)
        # Scheduled via the global "Schedule Followup" modal (desktop + mobile).
        if err:
            resp['HX-Trigger'] = json.dumps({'crmFuModalError': err})
        else:
            resp['HX-Trigger'] = json.dumps({'crmToast': 'Scheduled', 'crmCloseFuModal': True})
        return resp
    return HttpResponse(status=204)


@login_required
@sales_pipeline_required
@require_POST
def lead_quick_note(request, pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=pk)
    form = QuickNoteForm(request.POST)
    err = None
    if form.is_valid():
        line = (form.cleaned_data.get('quick_note') or '').strip()
        if line:
            ts = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')
            lead.notes = (lead.notes + f'\n[{ts}] {line}').strip()[:10000]
            lead.save(update_fields=['notes', 'updated_at'])
            log_activity(lead, 'note', line[:200])
        lead.refresh_from_db()
    else:
        err = 'Too long or empty'

    if request.headers.get('HX-Request'):
        tpl = request.POST.get('_tpl', 'exec_row')
        lead_ann = _lead_for_exec(user, pk)
        ctx = _exec_board_ctx(lead_ann, user, quick_note_error=err)
        tmpl = (
            'crm/partials/lead_mobile_card.html'
            if tpl == 'mobile_card'
            else 'crm/partials/lead_exec_board.html'
        )
        resp = render(request, tmpl, ctx)
        if not err:
            _hx_toast(resp, 'Updated')
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
        _lead_scope_qs(user).select_related('package', 'employee'), pk=pk
    )
    activities = lead.activities.all()[:100]
    followups = lead.followups.all().order_by('-datetime')[:50]
    tasks = lead.tasks.all()
    fu_form = FollowUpForm()
    task_form = TaskForm()
    start, end, _ = _local_today_bounds()
    client_for_lead = Client.objects.filter(lead=lead).first()
    has_client = client_for_lead is not None
    linked_project = None
    if client_for_lead:
        linked_project = client_for_lead.projects.order_by('-created_at').first()
    show_convert_to_project = (
        lead.status == Lead.Status.ADVANCE_RECEIVED_PROJECT_STARTED or not has_client
    )
    convert_form = (
        LeadConvertForm(employee=lead.employee)
        if show_convert_to_project and not has_client
        else None
    )
    return render(
        request,
        'crm/lead_detail.html',
        {
            'lead': lead,
            'activities': activities,
            'followups': followups,
            'tasks': tasks,
            'fu_form': fu_form,
            'task_form': task_form,
            'status_choices': Lead.PRIMARY_STATUS_CHOICES,
            'status_to_stage': Lead.STATUS_TO_STAGE,
            'detailed_status_choices': Lead.Status.choices,
            'packages': Package.objects.all(),
            'fu_start': start,
            'fu_end': end,
            'show_convert_to_project': show_convert_to_project,
            'convert_form': convert_form,
            'lead_has_client': has_client,
            'linked_project': linked_project,
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
    if title:
        task.title = title
    if due:
        try:
            task.due_date = datetime.strptime(due, '%Y-%m-%d').date()
        except ValueError:
            pass
    elif 'due_date' in request.POST and not due:
        task.due_date = None
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


@login_required
@sales_pipeline_required
def followups_page(request):
    user = request.user
    scope = _sales_scope_context(request, user)
    ctx = _followups_queue_context(user, scope_ids=scope['scope_ids'])
    ctx['manager_mode'] = scope['manager_mode']
    ctx['scope_employees'] = scope['scope_employees']
    ctx['selected_employee'] = scope['selected_employee']
    return render(request, 'crm/followups.html', ctx)


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
        fu.save(update_fields=['datetime', 'reminder_sent_at'])
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
    won = leads.filter(status=STATUS_CLOSED).count()
    revenue = (
        leads.filter(status=STATUS_CLOSED).aggregate(s=Sum('deal_value'))['s']
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
        leads.filter(status=STATUS_CLOSED, updated_at__gte=since)
        .annotate(d=TruncDate('updated_at'))
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
                status=STATUS_CLOSED,
                updated_at__year=mm.year,
                updated_at__month=mm.month,
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
def achievements_dashboard(request):
    user = request.user
    # Month filter: ?month=YYYY-MM (defaults to current month)
    month_param = (request.GET.get('month') or '').strip()
    today = timezone.localdate()
    if month_param and len(month_param) >= 7:
        try:
            y, m = int(month_param[:4]), int(month_param[5:7])
            month_date = date(y, m, 1)
        except ValueError:
            month_date = today.replace(day=1)
    else:
        month_date = today.replace(day=1)

    # Employee scope
    can_scope_employees = can_view_all_sales_data(user)
    if user.is_superuser:
        emp_id_s = (request.GET.get('employee') or '').strip()
        if emp_id_s.isdigit():
            employee = User.objects.filter(pk=int(emp_id_s), is_active=True).first()
        else:
            employee = user
        if not employee:
            employee = user
        available_employees = User.objects.filter(is_active=True).order_by('username')
    elif can_scope_employees:
        emp_id_s = (request.GET.get('employee') or '').strip()
        available_employees = _sales_team_users_qs()
        if emp_id_s.isdigit():
            employee = next(
                (e for e in available_employees if e.id == int(emp_id_s)), user
            )
        else:
            employee = user
    else:
        employee = user
        available_employees = None

    # Package filter
    pkg_id_s = (request.GET.get('package') or '').strip()
    package_obj = None
    if pkg_id_s.isdigit():
        package_obj = Package.objects.filter(pk=int(pkg_id_s)).first()

    perf = get_monthly_performance(employee, month_date, package=package_obj)
    month_start, month_end = month_date.replace(day=1), (
        month_date.replace(year=month_date.year + 1, month=1, day=1)
        if month_date.month == 12
        else month_date.replace(month=month_date.month + 1, day=1)
    )

    achievements = (
        Achievement.objects.filter(
            employee=employee,
            achieved_date__gte=month_start,
            achieved_date__lt=month_end,
        )
        .select_related('employee', 'lead', 'package', 'created_by')
        .order_by('-achieved_date', '-id')
    )
    if package_obj:
        achievements = achievements.filter(package=package_obj)

    packages = Package.objects.all().order_by('name')
    # Latest / upcoming monthly target for info
    mt = (
        MonthlyTarget.objects.filter(employee=employee, month=month_start)
        .order_by('-month')
        .first()
    )

    form_employee = employee if can_scope_employees else user
    form = AchievementForm(employee=form_employee, initial={'achieved_date': today})

    ctx = {
        'scope_employee': employee,
        'is_admin': user.is_superuser,
        'can_scope_employees': can_scope_employees,
        'employees': available_employees,
        'current_month': month_date,
        'package_filter': package_obj,
        'packages': packages,
        'achievements': achievements,
        'monthly_target': mt,
        'perf': perf,
        'form': form,
    }
    return render(request, 'crm/achievements/dashboard.html', ctx)


@login_required
@sales_pipeline_required
@require_http_methods(['POST'])
def achievement_create(request):
    user = request.user
    target_employee = user
    if user.is_superuser:
        emp_id = (request.POST.get('employee') or '').strip()
        if emp_id.isdigit():
            target_employee = (
                User.objects.filter(pk=int(emp_id), is_active=True).first() or user
            )
    elif is_sales_manager(user):
        emp_id = (request.POST.get('employee') or '').strip()
        if emp_id.isdigit() and int(emp_id) in _sales_scope_user_ids(user):
            target_employee = User.objects.filter(pk=int(emp_id), is_active=True).first() or user

    form = AchievementForm(request.POST or None, employee=target_employee)
    if form.is_valid():
        ach = form.save(commit=False)
        ach.employee = target_employee
        ach.created_by = user
        ach.save()
        if request.headers.get('HX-Request'):
            r = HttpResponse()
            r['HX-Location'] = json.dumps({
                'path': reverse('crm:achievements_dashboard'),
                'target': '#crm-main-content',
                'select': '#crm-main-content',
                'swap': 'outerHTML',
            })
            return r
        return redirect('crm:achievements_dashboard')
    if request.headers.get('HX-Request'):
        # On error, re-render the small form area.
        month_date = timezone.localdate().replace(day=1)
        packages = Package.objects.all().order_by('name')
        ctx = {
            'form': form,
            'scope_employee': target_employee,
            'is_admin': user.is_superuser,
            'can_scope_employees': can_view_all_sales_data(user),
            'current_month': month_date,
            'package_filter': None,
            'packages': packages,
        }
        return render(request, 'crm/achievements/_achievement_form.html', ctx)
    return redirect('crm:achievements_dashboard')


@login_required
@sales_pipeline_required
@require_http_methods(['GET', 'POST'])
def achievement_update(request, pk):
    user = request.user
    ach = get_object_or_404(Achievement, pk=pk)
    if not _can_manage_employee_data(user, ach.employee_id):
        return HttpResponse(status=403)

    if request.method == 'POST':
        form = AchievementForm(request.POST, instance=ach, employee=ach.employee)
        if form.is_valid():
            form.save()
            if request.headers.get('HX-Request'):
                r = HttpResponse()
                r['HX-Location'] = json.dumps({
                    'path': reverse('crm:achievements_dashboard'),
                    'target': '#crm-main-content',
                    'select': '#crm-main-content',
                    'swap': 'outerHTML',
                })
                return r
            return redirect('crm:achievements_dashboard')
    else:
        form = AchievementForm(instance=ach, employee=ach.employee)

    return render(
        request,
        'crm/achievements/edit.html',
        {'form': form, 'achievement': ach},
    )


@login_required
@sales_pipeline_required
@require_POST
def achievement_delete(request, pk):
    user = request.user
    ach = get_object_or_404(Achievement, pk=pk)
    if not _can_manage_employee_data(user, ach.employee_id):
        return HttpResponse(status=403)
    ach.delete()
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': reverse('crm:achievements_dashboard'),
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return redirect('crm:achievements_dashboard')


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


@login_required
def projects_list(request):
    user = request.user
    raw_status = (request.GET.get('status') or '').strip()
    current_status = ''
    if raw_status in dict(Project.Status.choices):
        current_status = raw_status
    qs = get_projects_for_user(user)
    if current_status:
        qs = qs.filter(status=current_status)
    projects_base = reverse('crm:projects')
    status_pills = [{'val': '', 'label': 'All', 'url': projects_base}]
    for val, label in Project.Status.choices:
        url = f'{projects_base}?{urlencode({"status": val})}'
        status_pills.append({'val': val, 'label': label, 'url': url})
    return render(
        request,
        'crm/projects/list.html',
        {
            'projects': qs,
            'status_choices': Project.Status.choices,
            'current_status': current_status,
            'status_pills': status_pills,
        },
    )


@login_required
@sales_pipeline_required
@require_http_methods(['GET', 'POST'])
def project_create(request):
    user = request.user
    if request.method == 'POST':
        form = ProjectForm(request.POST, user=user)
        if form.is_valid():
            cd = form.cleaned_data
            lead = cd['lead']
            client, client_created = get_or_create_client_for_lead(
                lead, created_by=user
            )
            project = create_project(
                client,
                package=cd['package'],
                deal_value=cd['deal_value'],
                advance_received=cd['advance_received'],
                assignees=list(cd.get('assignees') or []),
                notes=cd.get('notes') or '',
                created_by=user,
            )
            if client_created:
                log_activity(
                    lead,
                    'lead_converted_to_project',
                    f'Client "{client.business_name}", project #{project.pk}',
                )
                if lead.status == Lead.Status.CLOSED:
                    lead.status = Lead.Status.ADVANCE_RECEIVED_PROJECT_STARTED
                    lead.save(update_fields=['status', 'updated_at'])
            messages.success(request, 'Project created.')
            return redirect('crm:project_detail', pk=project.pk)
        messages.error(request, form.errors.as_text())
    else:
        form = ProjectForm(user=user)
    return render(request, 'crm/projects/create.html', {'form': form})


@login_required
def project_detail(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    client_obj = project.client
    lead_obj = client_obj.lead
    submission = OnboardingSubmission.objects.filter(project=project).first()
    onboarding_pct = submission.overall_completion_percent() if submission else 0
    onboarding_public_url = request.build_absolute_uri(
        reverse('crm:onboarding_form', args=[project.onboarding_token])
    )
    readiness = readiness_service.get_operational_readiness(project)
    team_users = [
        m.user for m in project.memberships.select_related('user').order_by('-role', 'added_at')
    ]
    return render(
        request,
        'crm/projects/detail.html',
        {
            'project': project,
            'client': client_obj,
            'lead': lead_obj,
            'project_status_choices': Project.Status.choices,
            'onboarding_submission': submission,
            'onboarding_pct': onboarding_pct,
            'onboarding_public_url': onboarding_public_url,
            'readiness': readiness,
            'team_users': team_users,
            'assignees_form': ProjectAssigneesForm(project=project),
        },
    )


@login_required
@require_POST
def project_status_update(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    new_status = (request.POST.get('status') or '').strip()
    try:
        update_project_status(
            project, new_status=new_status, updated_by=user
        )
    except ValueError:
        project.refresh_from_db()
        resp = render(
            request,
            'crm/projects/partials/status_badge.html',
            {
                'project': project,
                'project_status_choices': Project.Status.choices,
                'status_error': 'Invalid status.',
            },
        )
        return resp
    project.refresh_from_db()
    return render(
        request,
        'crm/projects/partials/status_badge.html',
        {
            'project': project,
            'project_status_choices': Project.Status.choices,
        },
    )


@login_required
@sales_pipeline_required
@require_POST
def lead_convert_to_project(request, lead_pk):
    user = request.user
    lead = get_object_or_404(_lead_scope_qs(user), pk=lead_pk)
    if Client.objects.filter(lead=lead).exists():
        msg = 'This lead already has a client.'
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/partials/lead_convert_feedback.html',
                {'error': msg, 'form': LeadConvertForm(employee=lead.employee)},
            )
        messages.error(request, msg)
        return redirect('crm:lead_detail', pk=lead_pk)

    form = LeadConvertForm(request.POST, employee=lead.employee)
    if not form.is_valid():
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/partials/lead_convert_feedback.html',
                {'error': None, 'form': form},
            )
        messages.error(request, form.errors.as_text())
        return redirect('crm:lead_detail', pk=lead_pk)

    pkg = form.cleaned_data['package']

    try:
        _, project = convert_lead_to_project(
            lead,
            package=pkg,
            deal_value=form.cleaned_data['deal_value'],
            advance_received=form.cleaned_data['advance_received'],
            assignees=list(form.cleaned_data.get('assignees') or []),
            notes=form.cleaned_data.get('notes') or '',
            created_by=user,
        )
    except ValueError as exc:
        msg = str(exc) or 'Could not convert this lead.'
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/partials/lead_convert_feedback.html',
                {'error': msg, 'form': LeadConvertForm(employee=lead.employee)},
            )
        messages.error(request, msg)
        return redirect('crm:lead_detail', pk=lead_pk)

    messages.success(request, 'Lead converted to project.')
    detail_url = reverse('crm:project_detail', args=[project.pk])
    if request.headers.get('HX-Request'):
        r = HttpResponse()
        r['HX-Location'] = json.dumps({
            'path': detail_url,
            'target': '#crm-main-content',
            'select': '#crm-main-content',
            'swap': 'outerHTML',
        })
        return r
    return redirect('crm:project_detail', pk=project.pk)


def _submission_by_token(token):
    return get_object_or_404(
        OnboardingSubmission.objects.select_related(
            'project', 'project__client', 'project__package'
        ),
        project__onboarding_token=token,
    )


def _onboarding_form_redirect(request, token):
    """Full-page redirect; HTMX needs HX-Redirect or it may mishandle 302 and duplicate layout."""
    url = reverse('crm:onboarding_form', kwargs={'token': token})
    resp = redirect(url)
    if request.headers.get('HX-Request'):
        resp['HX-Redirect'] = url
    return resp


def _onboarding_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR') or '').strip()[:45] or None


def _onboarding_ip_for_db(ip_str):
    if not ip_str:
        return None
    if len(ip_str) > 39:
        return ip_str[:39]
    return ip_str


# Public onboarding — WhatsApp support (country code + number, no + for wa.me).
ONBOARDING_SUPPORT_WHATSAPP_E164 = '919544196763'


def _onboarding_whatsapp_support_url(project) -> str:
    client = getattr(project, 'client', None)
    client_name = (client.business_name or 'Client').strip() if client else 'Client'
    pkg = getattr(project, 'package', None)
    pkg_label = str(pkg) if pkg else '—'
    body = (
        'I found some issues while submitting the form. Please assist me.\n\n'
        f'Client / business: {client_name}\n'
        f'Package: {pkg_label}'
    )
    return f'https://wa.me/{ONBOARDING_SUPPORT_WHATSAPP_E164}?text={quote(body)}'


ONBOARDING_PUBLIC_FORM_MAP = {
    'business_info': OnboardingBusinessInfoForm,
    'contact': OnboardingContactForm,
    'branding': OnboardingBrandingForm,
    'requirements': OnboardingRequirementsForm,
    'content': OnboardingContentForm,
    'documents': OnboardingDocumentForm,
    'payment_kyc': OnboardingPaymentKycForm,
}


def onboarding_form(request, token):
    submission = _submission_by_token(token)
    scope_check = {'violations': [], 'warnings': [], 'clean': True}
    if not submission.is_fully_submitted():
        scope_check = scope_service.validate_onboarding_requirements(submission)
    steps, first_open = onboarding_service.get_public_onboarding_step_meta(submission)
    if first_open is None and steps and all(s['saved'] for s in steps):
        first_open = steps[-1]['slug']
    step_meta = {s['slug']: s for s in steps}
    flow_slugs_json = json.dumps([s[0] for s in onboarding_service.PUBLIC_ONBOARDING_FLOW])
    if submission.is_fully_submitted():
        return render(
            request,
            'crm/onboarding/form.html',
            {
                'submission': submission,
                'project': submission.project,
                'readonly_done': True,
                'overall_pct': submission.overall_completion_percent(),
                'scope_violations': [],
                'scope_feature_labels': scope_service.FEATURE_LABELS,
                'onboarding_whatsapp_url': _onboarding_whatsapp_support_url(
                    submission.project
                ),
                'onboarding_step_meta': step_meta,
                'onboarding_first_open': first_open,
                'onboarding_flow_slugs_json': flow_slugs_json,
            },
        )
    business_form = OnboardingBusinessInfoForm(instance=submission)
    contact_form = OnboardingContactForm(instance=submission)
    branding_form = OnboardingBrandingForm(instance=submission)
    requirements_form = OnboardingRequirementsForm(instance=submission)
    content_form = OnboardingContentForm(instance=submission)
    document_form = OnboardingDocumentForm(instance=submission)
    payment_kyc_form = OnboardingPaymentKycForm(instance=submission)
    agreement_form = OnboardingAgreementForm()
    return render(
        request,
        'crm/onboarding/form.html',
        {
            'submission': submission,
            'project': submission.project,
            'token': token,
            'readonly_done': False,
            'overall_pct': submission.overall_completion_percent(),
            'business_form': business_form,
            'contact_form': contact_form,
            'branding_form': branding_form,
            'requirements_form': requirements_form,
            'content_form': content_form,
            'document_form': document_form,
            'payment_kyc_form': payment_kyc_form,
            'agreement_form': agreement_form,
            'scope_violations': scope_check.get('violations') or [],
            'scope_feature_labels': scope_service.FEATURE_LABELS,
            'onboarding_whatsapp_url': _onboarding_whatsapp_support_url(
                submission.project
            ),
            'onboarding_step_meta': step_meta,
            'onboarding_first_open': first_open,
            'onboarding_flow_slugs_json': flow_slugs_json,
        },
    )


@require_POST
def onboarding_section_save(request, token):
    submission = _submission_by_token(token)
    if submission.is_fully_submitted():
        return HttpResponse(status=403)
    section = (request.POST.get('section') or '').strip()
    form_cls = ONBOARDING_PUBLIC_FORM_MAP.get(section)
    if not form_cls:
        return HttpResponse('Invalid section', status=400)
    try:
        onboarding_service.assert_section_save_allowed(submission, section)
    except ValueError as exc:
        return HttpResponse(str(exc), status=403)
    if section in ('documents', 'payment_kyc'):
        form = form_cls(request.POST, request.FILES, instance=submission)
    else:
        form = form_cls(request.POST, instance=submission)
    if not form.is_valid():
        return render(
            request,
            'crm/onboarding/partials/section_saved.html',
            {
                'section': section,
                'ok': False,
                'errors': form.errors,
                'overall_pct': submission.overall_completion_percent(),
            },
        )
    field_names = onboarding_service.SECTION_MODEL_FIELDS[section]
    data = {}
    for name in field_names:
        if name in form.cleaned_data:
            data[name] = form.cleaned_data[name]
    if section in ('documents', 'payment_kyc'):
        for name in field_names:
            if name in request.FILES:
                data[name] = request.FILES[name]
    onboarding_service.save_onboarding_section(
        submission, section, data, updated_by=None
    )
    submission.refresh_from_db()
    return render(
        request,
        'crm/onboarding/partials/section_saved.html',
        {
            'section': section,
            'ok': True,
            'errors': None,
            'overall_pct': submission.overall_completion_percent(),
        },
    )


@require_POST
def onboarding_final_submit(request, token):
    submission = _submission_by_token(token)
    if submission.is_fully_submitted():
        return render(request, 'crm/onboarding/partials/already_submitted.html')
    form = OnboardingAgreementForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'crm/onboarding/partials/agreement_errors.html',
            {'agreement_form': form, 'onboarding_token': token},
        )
    tv = form.cleaned_data.get('terms_version') or '1.0'
    OnboardingSubmission.objects.filter(pk=submission.pk).update(terms_version=tv[:20])
    submission.refresh_from_db()
    if not submission.is_payment_kyc_complete():
        reasons = submission.payment_kyc_incomplete_reasons()
        msg = (
            'Cannot submit yet. Open Payment & owner KYC, fix the items below, then click '
            '“Save section” again: '
            + ' '.join(reasons)
        )
        messages.error(request, msg)
        return _onboarding_form_redirect(request, token)
    try:
        onboarding_service.submit_onboarding(
            submission,
            ip_address=_onboarding_ip_for_db(_onboarding_client_ip(request)),
        )
    except ValueError:
        return render(request, 'crm/onboarding/partials/already_submitted.html')
    submission.refresh_from_db()
    if request.headers.get('HX-Request'):
        return render(request, 'crm/onboarding/partials/thank_you.html')
    return render(
        request,
        'crm/onboarding/form.html',
        {
            'submission': submission,
            'project': submission.project,
            'readonly_done': True,
            'overall_pct': submission.overall_completion_percent(),
            'onboarding_whatsapp_url': _onboarding_whatsapp_support_url(
                submission.project
            ),
        },
    )


@login_required
def project_onboarding_detail(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    submission = get_object_or_404(OnboardingSubmission, project=project)
    summary = onboarding_service.get_onboarding_summary(submission)
    return render(
        request,
        'crm/projects/onboarding_detail.html',
        {
            'project': project,
            'submission': submission,
            'summary': summary,
            'section_status_choices': OnboardingSubmission.SectionStatus.choices,
        },
    )


@login_required
@require_POST
def onboarding_section_verify(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    submission = get_object_or_404(OnboardingSubmission, project=project)
    section = (request.POST.get('section') or '').strip()
    new_status = (request.POST.get('new_status') or '').strip()
    try:
        onboarding_service.verify_section(
            submission, section, new_status, updated_by=user
        )
    except ValueError:
        submission.refresh_from_db()
        summary = onboarding_service.get_onboarding_summary(submission)
        sec_meta = next(
            (s for s in summary['sections'] if s['name'] == section),
            None,
        )
        if sec_meta is None:
            sec_meta = {
                'name': section,
                'label': section,
                'status': '',
                'status_label': section,
                'completion': 0,
            }
        ctx = {
            'submission': submission,
            'project': project,
            'sec': sec_meta,
            'error': 'Could not update status.',
            'section_status_choices': OnboardingSubmission.SectionStatus.choices,
            'summary': summary,
        }
        return render(request, 'crm/projects/partials/section_verify_response.html', ctx)
    submission.refresh_from_db()
    summary = onboarding_service.get_onboarding_summary(submission)
    sec_meta = next(s for s in summary['sections'] if s['name'] == section)
    ctx = {
        'submission': submission,
        'project': project,
        'sec': sec_meta,
        'error': None,
        'section_status_choices': OnboardingSubmission.SectionStatus.choices,
        'summary': summary,
    }
    return render(request, 'crm/projects/partials/section_verify_response.html', ctx)


@login_required
@require_POST
def onboarding_internal_notes_save(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    submission = get_object_or_404(OnboardingSubmission, project=project)
    notes = request.POST.get('internal_notes', '')
    submission.internal_notes = notes[:20000]
    submission.save(update_fields=['internal_notes', 'updated_at'])
    return render(
        request,
        'crm/projects/partials/onboarding_notes.html',
        {'submission': submission, 'project': project},
    )


def _crm_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR') or '').strip()[:45] or None


@login_required
@require_POST
def onboarding_client_notes_save(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    submission = get_object_or_404(OnboardingSubmission, project=project)
    notes = request.POST.get('client_notes', '')
    submission.client_notes = notes[:20000]
    submission.save(update_fields=['client_notes', 'updated_at'])
    return render(
        request,
        'crm/projects/partials/onboarding_notes.html',
        {'submission': submission, 'project': project},
    )


@login_required
def operations_dashboard(request):
    if not can_access_operations_dashboard(request.user):
        return HttpResponse(status=403)
    flt = (request.GET.get('filter') or '').strip()
    now = timezone.now()
    today = timezone.localtime(now).date()
    renewals_expiring_7d = RenewalTracker.objects.filter(
        status=RenewalTracker.Status.ACTIVE,
        expires_at__date__gte=today,
        expires_at__date__lte=today + timedelta(days=7),
    ).count()
    renewals_expired_or_overdue = RenewalTracker.objects.filter(
        Q(status=RenewalTracker.Status.EXPIRED)
        | Q(
            status=RenewalTracker.Status.ACTIVE,
            expires_at__date__lt=today,
        )
    ).count()
    qs = (
        get_projects_for_user(request.user)
        .select_related('client', 'onboarding')
        .order_by('-updated_at')[:300]
    )
    rows = []
    for p in qs:
        readiness = readiness_service.get_operational_readiness(p)
        ps = provisioning_service.get_project_provisioning_summary(p)
        miss_cred = readiness_service.missing_client_visible_logins(p)
        exp_ren = RenewalTracker.objects.filter(
            Q(project=p, status=RenewalTracker.Status.EXPIRED)
            | Q(
                project=p,
                status=RenewalTracker.Status.ACTIVE,
                expires_at__date__lt=today,
            )
        ).exists()
        exp_cred = ProjectCredential.objects.filter(
            project=p, expires_at__isnull=False, expires_at__lt=now
        ).exists()
        rows.append(
            {
                'project': p,
                'readiness': readiness,
                'prov': ps,
                'blocked': (ps.get('blocked') or 0) + (ps.get('failed') or 0),
                'missing_cred': miss_cred,
                'expired': exp_ren or exp_cred,
            }
        )
    if flt == 'onboarding_pending':
        rows = [r for r in rows if not r['readiness']['onboarding_complete']]
    elif flt == 'provisioning_pending':
        rows = [
            r
            for r in rows
            if r['readiness']['onboarding_complete']
            and not r['readiness']['provisioning_complete']
        ]
    elif flt == 'handover_pending':
        rows = [
            r
            for r in rows
            if r['readiness']['onboarding_complete']
            and r['readiness']['provisioning_complete']
            and not r['readiness']['handover_complete']
        ]
    elif flt == 'expired':
        rows = [r for r in rows if r['expired']]
    return render(
        request,
        'crm/operations/dashboard.html',
        {
            'rows': rows,
            'active_filter': flt,
            'renewals_expiring_7d': renewals_expiring_7d,
            'renewals_expired_or_overdue': renewals_expired_or_overdue,
        },
    )


def _operations_context(request, project):
    user = request.user
    readiness = readiness_service.get_operational_readiness(project)
    prov_summary = provisioning_service.get_project_provisioning_summary(project)
    credentials_list = credential_service.queryset_for_user(user, project)
    handover = ProjectHandover.objects.filter(project=project).first()
    if handover is None:
        handover = ProjectHandover(project=project)
    handover_form = ProjectHandoverForm(instance=handover)
    cred_form = ProjectCredentialForm()
    renewal_form = RenewalTrackerForm()
    renewals = list(project.renewals.order_by('expires_at')[:48])
    audit_logs = CredentialAuditLog.objects.none()
    if can_view_credential_audit(user):
        audit_logs = (
            CredentialAuditLog.objects.filter(credential__project=project)
            .select_related('user', 'credential')
            .order_by('-timestamp')[:50]
        )
    portal_access = HandoverPortalAccess.objects.filter(project=project).first()
    portal_url = ''
    if portal_access and portal_access.is_active:
        portal_url = request.build_absolute_uri(
            reverse('crm:client_portal', args=[portal_access.access_token])
        )
    return {
        'project': project,
        'readiness': readiness,
        'prov_summary': prov_summary,
        'credentials_list': credentials_list,
        'handover': handover,
        'handover_form': handover_form,
        'cred_form': cred_form,
        'renewal_form': renewal_form,
        'renewals': renewals,
        'audit_logs': audit_logs,
        'provisioning_status_choices': ProvisioningStep.Status.choices,
        'can_edit_credentials': can_edit_credentials(user),
        'can_manage_provisioning': can_manage_provisioning(user),
        'can_complete_handover': can_complete_handover(user),
        'portal_access': portal_access,
        'portal_url': portal_url,
        'portal_activate_form': PortalActivateForm(),
        'can_manage_portal': can_manage_portal(user),
        'cr_staff_form': ChangeRequestStaffForm(),
        'cr_staff_error': False,
        'cr_staff_success': False,
        'new_cr_id': None,
    }


@login_required
def project_operations(request, pk):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    ctx = _operations_context(request, project)
    return render(request, 'crm/projects/operations.html', ctx)


@login_required
@require_POST
def project_provisioning_step_update(request, pk):
    user = request.user
    if not can_manage_provisioning(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    form = ProvisioningStepStatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    step_key = form.cleaned_data['step_key']
    status = form.cleaned_data['status']
    notes = form.cleaned_data.get('notes') or ''
    try:
        if status == ProvisioningStep.Status.COMPLETED:
            step = provisioning_service.complete_provisioning_step(
                project=project,
                step_key=step_key,
                user=user,
                notes=notes or None,
            )
        else:
            step = provisioning_service.update_provisioning_status(
                project=project,
                step_key=step_key,
                status=status,
                user=user,
                notes=notes or None,
            )
    except (ProjectProvisioning.DoesNotExist, ProvisioningStep.DoesNotExist):
        return HttpResponse(status=404)
    lead = project.client.lead
    if lead is not None:
        log_activity(lead, 'provisioning_updated', f'{step_key}:{status}')
    return render(
        request,
        'crm/projects/partials/provisioning_step_row.html',
        {
            'step': step,
            'project': project,
            'provisioning_status_choices': ProvisioningStep.Status.choices,
            'can_manage_provisioning': can_manage_provisioning(request.user),
        },
    )


@login_required
@require_POST
def project_credential_save(request, pk):
    user = request.user
    if not can_edit_credentials(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    cred_id = (request.POST.get('credential_id') or '').strip()
    instance = None
    if cred_id.isdigit():
        instance = get_object_or_404(
            ProjectCredential, pk=int(cred_id), project=project
        )
        if not credential_allowed_for_role(user, instance):
            return HttpResponse('Forbidden', status=403)
    form = ProjectCredentialForm(request.POST, instance=instance)
    if not form.is_valid():
        ctx = _operations_context(request, project)
        ctx['cred_form'] = form
        ctx['cred_form_error'] = True
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/projects/partials/credentials_block.html',
                ctx,
                status=400,
            )
        return render(request, 'crm/projects/operations.html', ctx, status=400)
    pwd_in = (form.cleaned_data.get('password_plain') or '').strip()
    sec_in = (form.cleaned_data.get('secret_plain') or '').strip()
    plain_password = None
    plain_secret = None
    if instance is None:
        inst = form.save(commit=False)
        inst.project = project
        plain_password = pwd_in
        plain_secret = sec_in
    else:
        inst = form.save(commit=False)
        if pwd_in:
            plain_password = pwd_in
        if sec_in:
            plain_secret = sec_in
    credential_service.save_credential(
        project=project,
        user=user,
        ip=_crm_client_ip(request),
        instance=inst,
        plain_password=plain_password,
        plain_secret=plain_secret,
    )
    if request.headers.get('HX-Request'):
        ctx = _operations_context(request, project)
        return render(
            request,
            'crm/projects/partials/credentials_block.html',
            ctx,
        )
    return redirect('crm:project_operations', pk=pk)


@login_required
def credential_reveal(request, pk, cred_id):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    cred = get_object_or_404(ProjectCredential, pk=cred_id, project=project)
    if not credential_allowed_for_role(user, cred):
        return HttpResponse('Forbidden', status=403)
    field = (request.GET.get('field') or 'password').strip()
    if field == 'secret':
        val = credential_service.decrypt_secret_for_user(cred, user=user)
    else:
        val = credential_service.decrypt_password_for_user(cred, user=user)
    credential_service.record_view(cred, user=user, ip=_crm_client_ip(request))
    return render(
        request,
        'crm/projects/partials/credential_reveal.html',
        {'value': val, 'field': field},
    )


@login_required
@require_POST
def credential_copy_logged(request, pk, cred_id):
    user = request.user
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    cred = get_object_or_404(ProjectCredential, pk=cred_id, project=project)
    if not credential_allowed_for_role(user, cred):
        return HttpResponse('Forbidden', status=403)
    field = (request.POST.get('field') or 'password').strip()
    if field == 'secret':
        _ = credential_service.decrypt_secret_for_user(cred, user=user)
    else:
        _ = credential_service.decrypt_password_for_user(cred, user=user)
    credential_service.record_copy(cred, user=user, ip=_crm_client_ip(request))
    r = HttpResponse('')
    return _hx_toast(r, 'Copy logged for audit.')


@login_required
@require_POST
def project_handover_save(request, pk):
    user = request.user
    if not can_complete_handover(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    ho, _ = ProjectHandover.objects.get_or_create(project=project)
    form = ProjectHandoverForm(request.POST, request.FILES, instance=ho)
    if not form.is_valid():
        ctx = _operations_context(request, project)
        ctx['handover_form'] = form
        ctx['handover_error'] = True
        ctx['handover'] = form.instance
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/projects/partials/handover_block.html',
                ctx,
                status=400,
            )
        return render(request, 'crm/projects/operations.html', ctx, status=400)
    ho = form.save(commit=False)
    if request.POST.get('mark_complete'):
        ho.completed_at = timezone.now()
        ho.completed_by = user
    ho.save()
    if request.headers.get('HX-Request'):
        ctx = _operations_context(request, project)
        return render(
            request,
            'crm/projects/partials/handover_block.html',
            ctx,
        )
    return redirect('crm:project_operations', pk=pk)


@login_required
@require_POST
def project_renewal_add(request, pk):
    user = request.user
    if not can_manage_provisioning(user) or not can_access_renewals_dashboard(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    form = RenewalTrackerForm(request.POST)
    if not form.is_valid():
        ctx = _operations_context(request, project)
        ctx['renewal_form'] = form
        ctx['renewal_error'] = True
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/projects/partials/renewals_block.html',
                ctx,
                status=400,
            )
        return render(request, 'crm/projects/operations.html', ctx, status=400)
    rt = form.save(commit=False)
    rt.project = project
    rt.save()
    if request.headers.get('HX-Request'):
        ctx = _operations_context(request, project)
        return render(
            request,
            'crm/projects/partials/renewals_block.html',
            ctx,
        )
    return redirect('crm:project_operations', pk=pk)


def client_portal(request, token):
    import uuid as uuid_mod

    try:
        u = uuid_mod.UUID(str(token).strip())
    except (ValueError, TypeError, AttributeError):
        return render(request, 'crm/portal/not_found.html', status=404)
    access = (
        HandoverPortalAccess.objects.filter(access_token=u)
        .select_related(
            'project',
            'project__client',
            'project__package',
            'project__onboarding',
            'project__handover',
        )
        .first()
    )
    if access is None:
        return render(request, 'crm/portal/not_found.html', status=404)
    if not access.is_active:
        return render(request, 'crm/portal/inactive.html', {'access': access}, status=200)
    portal = portal_service.get_portal_by_token(str(token))
    if portal is None:
        return render(request, 'crm/portal/inactive.html', {'access': access}, status=200)
    payload = portal_service.get_portal_payload(portal)
    portal_credentials = []
    for row in payload['credentials']:
        cred = (
            ProjectCredential.objects.filter(
                pk=row['id'],
                project_id=portal.project_id,
                visibility_level=ProjectCredential.VisibilityLevel.SHARED,
            )
            .first()
        )
        if not cred:
            continue
        portal_credentials.append(
            {
                **row,
                'password_value': (
                    decrypt_ciphertext(cred.password_encrypted)
                    if cred.password_encrypted
                    else ''
                ),
                'secret_value': (
                    decrypt_ciphertext(cred.secret_key_encrypted)
                    if cred.secret_key_encrypted
                    else ''
                ),
            }
        )
    return render(
        request,
        'crm/portal/dashboard.html',
        {
            'portal': portal,
            'payload': payload,
            'portal_credentials': portal_credentials,
            'portal_cr_form': ChangeRequestPortalForm(),
            'portal_token': str(portal.access_token),
        },
    )


@login_required
@require_POST
def portal_activate(request, pk):
    user = request.user
    if not can_manage_portal(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    form = PortalActivateForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        portal_service.activate_portal(project, activated_by=user)
    except ValueError as exc:
        ctx = _operations_context(request, project)
        ctx['portal_error'] = str(exc)
        return render(
            request,
            'crm/projects/partials/portal_status.html',
            ctx,
            status=400,
        )
    ctx = _operations_context(request, project)
    return render(request, 'crm/projects/partials/portal_status.html', ctx)


@login_required
@require_POST
def portal_deactivate(request, pk):
    user = request.user
    if not can_manage_portal(user):
        return HttpResponse('Forbidden', status=403)
    project = get_object_or_404(get_projects_for_user(user), pk=pk)
    form = PortalActivateForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        portal_service.deactivate_portal(project, deactivated_by=user)
    except ValueError as exc:
        ctx = _operations_context(request, project)
        ctx['portal_error'] = str(exc)
        return render(
            request,
            'crm/projects/partials/portal_status.html',
            ctx,
            status=400,
        )
    ctx = _operations_context(request, project)
    return render(request, 'crm/projects/partials/portal_status.html', ctx)


@login_required
def renewals_dashboard(request):
    if not can_access_renewals_dashboard(request.user):
        return HttpResponse(status=403)
    form = RenewalFilterForm(request.GET)
    form.is_valid()
    st = (form.cleaned_data.get('status') or '').strip()
    win = (form.cleaned_data.get('window') or '').strip()
    today = timezone.localtime(timezone.now()).date()
    qs = RenewalTracker.objects.select_related(
        'project', 'project__client', 'project__package'
    ).annotate(log_count=Count('reminder_logs'))
    if st == 'active':
        qs = qs.filter(status=RenewalTracker.Status.ACTIVE)
    elif st == 'expired':
        qs = qs.filter(status=RenewalTracker.Status.EXPIRED)
    elif st == 'suspended':
        qs = qs.filter(project__status=Project.Status.SUSPENDED)
    if win == '30d':
        qs = qs.filter(
            status=RenewalTracker.Status.ACTIVE,
            expires_at__date__gte=today,
            expires_at__date__lte=today + timedelta(days=30),
        )
    elif win == '7d':
        qs = qs.filter(
            status=RenewalTracker.Status.ACTIVE,
            expires_at__date__gte=today,
            expires_at__date__lte=today + timedelta(days=7),
        )
    elif win == 'overdue':
        qs = qs.filter(
            Q(status=RenewalTracker.Status.EXPIRED)
            | Q(
                status=RenewalTracker.Status.ACTIVE,
                expires_at__date__lt=today,
            )
        )
    qs = qs.order_by('expires_at')
    rows = []
    for r in qs:
        expd = timezone.localtime(r.expires_at).date()
        rows.append({'renewal': r, 'days_remaining': (expd - today).days})
    return render(
        request,
        'crm/renewals/list.html',
        {'rows': rows, 'filter_form': form},
    )


@login_required
def renewal_detail(request, pk):
    if not can_access_renewals_dashboard(request.user):
        return HttpResponse(status=403)
    renewal = get_object_or_404(
        RenewalTracker.objects.select_related(
            'project', 'project__client', 'project__package'
        ),
        pk=pk,
    )
    get_object_or_404(get_projects_for_user(request.user), pk=renewal.project_id)
    logs = list(renewal.reminder_logs.order_by('-sent_at'))
    return render(
        request,
        'crm/renewals/detail.html',
        {
            'renewal': renewal,
            'logs': logs,
            'reminder_types': RenewalReminderLog.ReminderType.choices,
            'can_manual_reminder': can_send_renewal_reminder_manual(request.user),
        },
    )


@login_required
@require_POST
def renewal_send_reminder_manual(request, pk):
    user = request.user
    if not can_send_renewal_reminder_manual(user):
        return HttpResponse('Forbidden', status=403)
    renewal = get_object_or_404(
        RenewalTracker.objects.select_related('project', 'project__client'),
        pk=pk,
    )
    get_object_or_404(get_projects_for_user(user), pk=renewal.project_id)
    reminder_type = (request.POST.get('reminder_type') or '').strip()
    valid = {c.value for c in RenewalReminderLog.ReminderType}
    if reminder_type not in valid:
        return HttpResponse(status=400)
    RenewalReminderLog.objects.filter(
        renewal=renewal, reminder_type=reminder_type
    ).delete()
    if reminder_type == RenewalReminderLog.ReminderType.INTERNAL:
        recipient = (getattr(settings, 'RENEWAL_INTERNAL_ALERT_EMAIL', '') or '').strip()
    else:
        recipient = (renewal.project.client.email or '').strip()
    renewals_service.send_renewal_reminder(
        renewal, reminder_type, recipient_email=recipient
    )
    renewal.refresh_from_db()
    logs = list(renewal.reminder_logs.order_by('-sent_at'))
    if request.headers.get('HX-Request'):
        return render(
            request,
            'crm/renewals/partials/reminder_log.html',
            {'renewal': renewal, 'logs': logs},
        )
    return redirect('crm:renewal_detail', pk=renewal.pk)


# ---------------------------------------------------------------------------
# Phase 5 — audit trail, change requests, package scope
# ---------------------------------------------------------------------------


def _portal_session_key(token) -> str:
    return f'portal_cr_submissions_{token}'


@require_POST
def portal_submit_change_request(request, token):
    import uuid as uuid_mod

    try:
        u = uuid_mod.UUID(str(token).strip())
    except (ValueError, TypeError, AttributeError):
        return render(request, 'crm/portal/partials/change_request_submitted.html', {'ok': False, 'error': 'Invalid link.'}, status=404)
    access = (
        HandoverPortalAccess.objects.filter(access_token=u, is_active=True)
        .select_related('project')
        .first()
    )
    if access is None:
        return render(request, 'crm/portal/partials/change_request_submitted.html', {'ok': False, 'error': 'Portal not available.'}, status=404)
    sk = _portal_session_key(str(u))
    count = int(request.session.get(sk, 0))
    if count >= 3:
        return render(
            request,
            'crm/portal/partials/change_request_submitted.html',
            {'ok': False, 'error': 'You have reached the maximum number of requests for this session.'},
            status=429,
        )
    form = ChangeRequestPortalForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            'crm/portal/partials/change_request_submitted.html',
            {'ok': False, 'error': 'Please check the form fields.', 'form': form},
            status=400,
        )
    change_request_service.submit_change_request(
        access.project,
        title=form.cleaned_data['title'],
        description=form.cleaned_data['description'],
        request_type=form.cleaned_data['request_type'],
        submitted_via_portal=True,
    )
    request.session[sk] = count + 1
    request.session.modified = True
    return render(request, 'crm/portal/partials/change_request_submitted.html', {'ok': True})


def portal_change_requests(request, token):
    import uuid as uuid_mod

    try:
        u = uuid_mod.UUID(str(token).strip())
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(status=404)
    access = (
        HandoverPortalAccess.objects.filter(access_token=u, is_active=True)
        .select_related('project')
        .first()
    )
    if access is None:
        return HttpResponse(status=404)
    qs = change_request_service.get_change_requests_for_project(access.project)[:50]
    rows = []
    for cr in qs:
        rows.append(
            {
                'title': cr.title,
                'request_type': cr.get_request_type_display(),
                'status': cr.get_status_display(),
                'resolution_note': cr.resolution_note
                if cr.status == ChangeRequest.Status.COMPLETED
                else '',
            }
        )
    return render(
        request,
        'crm/portal/partials/change_requests_list.html',
        {'rows': rows},
    )


@login_required
def audit_trail(request):
    if not can_access_audit_trail(request.user):
        return HttpResponse(status=403)
    form = AuditFilterForm(request.GET)
    form.is_valid()
    qs = AuditEntry.objects.select_related('actor', 'project').order_by('-created_at')
    cat = (form.cleaned_data.get('category') or '').strip()
    if cat:
        qs = qs.filter(category=cat)
    actor = form.cleaned_data.get('actor')
    if actor:
        qs = qs.filter(actor=actor)
    df = form.cleaned_data.get('date_from')
    dt = form.cleaned_data.get('date_to')
    if df:
        start = timezone.make_aware(datetime.combine(df, datetime.min.time()))
        qs = qs.filter(created_at__gte=start)
    if dt:
        end = timezone.make_aware(datetime.combine(dt, time(23, 59, 59)))
        qs = qs.filter(created_at__lte=end)
    paginator = Paginator(qs, 50)
    page = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return render(
        request,
        'crm/audit/list.html',
        {'page_obj': page_obj, 'filter_form': form},
    )


@login_required
def project_audit_trail(request, pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    entries = list(audit_service.get_project_audit_trail(project, limit=20))
    return render(
        request,
        'crm/projects/partials/audit_trail.html',
        {'project': project, 'entries': entries},
    )


@login_required
def change_requests_list(request):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    pq = get_projects_for_user(request.user)
    form = ChangeRequestFilterForm(request.GET, project_qs=pq)
    form.is_valid()
    qs = (
        ChangeRequest.objects.filter(project__in=pq)
        .select_related('project', 'project__client', 'assigned_to')
        .order_by('-created_at')
    )
    st = (form.cleaned_data.get('status') or '').strip()
    if st:
        qs = qs.filter(status=st)
    rt = (form.cleaned_data.get('request_type') or '').strip()
    if rt:
        qs = qs.filter(request_type=rt)
    proj = form.cleaned_data.get('project')
    if proj:
        qs = qs.filter(project=proj)
    paginator = Paginator(qs, 50)
    page = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return render(
        request,
        'crm/change_requests/list.html',
        {'page_obj': page_obj, 'filter_form': form},
    )


@login_required
def change_request_detail(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(
        ChangeRequest.objects.select_related(
            'project', 'project__client', 'project__package', 'assigned_to'
        ),
        pk=pk,
    )
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    timeline = list(
        audit_service.get_audit_trail_for_object('ChangeRequest', cr.pk)[:100]
    )
    scope_summary = scope_service.get_scope_summary(cr.project.package)
    in_scope_features = []
    out_of_scope_features = []
    if cr.project.package:
        try:
            sc = cr.project.package.scope
            info = sc.check_request_in_scope(cr.requested_features or [])
            in_scope_features = info['in_scope']
            out_of_scope_features = info['out_of_scope']
        except Exception:
            pass
    triage_form = TriageForm(initial={'requested_features': cr.requested_features or []})
    quote_form = QuoteForm()
    reject_form = RejectForm()
    complete_form = CompleteForm()
    return render(
        request,
        'crm/change_requests/detail.html',
        {
            'cr': cr,
            'timeline': timeline,
            'scope_summary': scope_summary,
            'in_scope_features': in_scope_features,
            'out_of_scope_features': out_of_scope_features,
            'triage_form': triage_form,
            'quote_form': quote_form,
            'reject_form': reject_form,
            'complete_form': complete_form,
        },
    )


@login_required
@require_POST
def change_request_triage(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    form = TriageForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        change_request_service.triage_change_request(
            cr,
            requested_features=form.cleaned_data.get('requested_features') or [],
            updated_by=request.user,
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    scope_summary = scope_service.get_scope_summary(cr.project.package)
    in_scope = []
    out_of_scope = []
    if cr.project.package:
        try:
            sc = cr.project.package.scope
            info = sc.check_request_in_scope(cr.requested_features or [])
            in_scope = info['in_scope']
            out_of_scope = info['out_of_scope']
        except Exception:
            pass
    return render(
        request,
        'crm/change_requests/partials/cr_triage_oob.html',
        {
            'cr': cr,
            'scope_summary': scope_summary,
            'in_scope_features': in_scope,
            'out_of_scope_features': out_of_scope,
        },
    )


@login_required
@require_POST
def change_request_quote(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    form = QuoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        change_request_service.quote_change_request(
            cr,
            quoted_amount=form.cleaned_data['quoted_amount'],
            quote_notes=form.cleaned_data.get('quote_notes') or '',
            updated_by=request.user,
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    return render(
        request,
        'crm/change_requests/partials/cr_status.html',
        {'cr': cr},
    )


@login_required
@require_POST
def change_request_approve(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    try:
        change_request_service.approve_change_request(cr, approved_by=request.user)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    return render(
        request,
        'crm/change_requests/partials/cr_status.html',
        {'cr': cr},
    )


@login_required
@require_POST
def change_request_start(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    try:
        change_request_service.start_change_request(cr, started_by=request.user)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    return render(
        request,
        'crm/change_requests/partials/cr_status.html',
        {'cr': cr},
    )


@login_required
@require_POST
def change_request_reject(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    form = RejectForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        change_request_service.reject_change_request(
            cr,
            rejected_by=request.user,
            reason=form.cleaned_data['reason'],
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    return render(
        request,
        'crm/change_requests/partials/cr_status.html',
        {'cr': cr},
    )


@login_required
@require_POST
def change_request_complete(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    cr = get_object_or_404(ChangeRequest, pk=pk)
    get_object_or_404(get_projects_for_user(request.user), pk=cr.project_id)
    form = CompleteForm(request.POST)
    if not form.is_valid():
        return HttpResponse(status=400)
    try:
        change_request_service.complete_change_request(
            cr,
            resolution_note=form.cleaned_data['resolution_note'],
            completed_by=request.user,
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    cr.refresh_from_db()
    return render(
        request,
        'crm/change_requests/partials/cr_status.html',
        {'cr': cr},
    )


@login_required
@sales_pipeline_required
@require_http_methods(['GET', 'POST'])
def package_scope_edit(request, pk):
    if not can_edit_package_scope(request.user):
        return HttpResponse(status=403)
    package = get_object_or_404(Package, pk=pk)
    try:
        scope = package.scope
    except Exception:
        return HttpResponse(
            'Scope record is being created — save the package again or contact support.',
            status=400,
        )
    if request.method == 'POST':
        form = PackageScopeForm(request.POST, instance=scope)
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
            return redirect('crm:packages')
    else:
        form = PackageScopeForm(instance=scope)
    return render(
        request,
        'crm/packages/scope_edit.html',
        {'form': form, 'package': package},
    )


@login_required
@require_POST
def change_request_staff_create(request, pk):
    if not can_access_change_requests(request.user):
        return HttpResponse(status=403)
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    form = ChangeRequestStaffForm(request.POST)
    if not form.is_valid():
        ctx = _operations_context(request, project)
        ctx['cr_staff_form'] = form
        ctx['cr_staff_error'] = True
        if request.headers.get('HX-Request'):
            return render(
                request,
                'crm/projects/partials/change_request_staff_block.html',
                ctx,
                status=400,
            )
        return redirect('crm:project_operations', pk=pk)
    data = form.cleaned_data
    cr = change_request_service.submit_change_request(
        project,
        title=data['title'],
        description=data['description'],
        request_type=data['request_type'],
        submitted_via_portal=False,
        submitted_by_staff=request.user,
        assigned_to=data.get('assigned_to'),
    )
    if request.headers.get('HX-Request'):
        ctx = _operations_context(request, project)
        ctx['cr_staff_form'] = ChangeRequestStaffForm()
        ctx['cr_staff_success'] = True
        ctx['new_cr_id'] = cr.pk
        return render(
            request,
            'crm/projects/partials/change_request_staff_block.html',
            ctx,
        )
    return redirect('crm:change_request_detail', pk=cr.pk)


def _ticket_board_ctx(project):
    return {
        'project': project,
        'ticket_columns': ticket_service.ticket_columns(project),
        'ticket_form': ProjectTicketForm(project=project),
    }


@login_required
@require_POST
def project_assignees_update(request, pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    form = ProjectAssigneesForm(request.POST, project=project)
    if form.is_valid():
        set_project_assignees(
            project, form.cleaned_data.get('assignees') or [], updated_by=request.user
        )
        project.refresh_from_db()
        messages.success(request, 'Team updated.')
    else:
        messages.error(request, form.errors.as_text())
    if request.headers.get('HX-Request'):
        team_users = [
            m.user
            for m in project.memberships.select_related('user').order_by('-role', 'added_at')
        ]
        return render(
            request,
            'crm/projects/partials/assignees_block.html',
            {
                'project': project,
                'team_users': team_users,
                'assignees_form': form if not form.is_valid() else ProjectAssigneesForm(project=project),
            },
        )
    return redirect('crm:project_detail', pk=pk)


@login_required
def project_tickets_board(request, pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    return render(
        request,
        'crm/projects/partials/ticket_board.html',
        _ticket_board_ctx(project),
    )


@login_required
@require_POST
def project_ticket_create(request, pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    form = ProjectTicketForm(request.POST, project=project)
    if form.is_valid():
        cd = form.cleaned_data
        try:
            ticket_service.create_ticket(
                project,
                title=cd['title'],
                description=cd.get('description') or '',
                status=cd.get('status') or ProjectTicket.Status.BACKLOG,
                priority=cd.get('priority') or ProjectTicket.Priority.MEDIUM,
                assigned_to=cd.get('assigned_to'),
                created_by=request.user,
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
    if request.headers.get('HX-Request'):
        ctx = _ticket_board_ctx(project)
        ctx['ticket_form'] = form
        ctx['ticket_create_error'] = not form.is_valid()
        return render(request, 'crm/projects/partials/ticket_board.html', ctx)
    return redirect('crm:project_detail', pk=pk)


@login_required
@require_POST
def project_ticket_move(request, pk, ticket_pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    ticket = get_object_or_404(ProjectTicket, pk=ticket_pk, project=project)
    new_status = (request.POST.get('status') or '').strip()
    try:
        position = int(request.POST.get('position', 0))
    except (TypeError, ValueError):
        position = None
    try:
        ticket_service.move_ticket(
            ticket, new_status=new_status, position=position
        )
    except ValueError:
        pass
    if request.headers.get('HX-Request'):
        return render(
            request,
            'crm/projects/partials/ticket_board.html',
            _ticket_board_ctx(project),
        )
    return redirect('crm:project_detail', pk=pk)


@login_required
@require_http_methods(['GET', 'POST'])
def project_ticket_detail(request, pk, ticket_pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    ticket = get_object_or_404(
        ProjectTicket.objects.prefetch_related('attachments'),
        pk=ticket_pk,
        project=project,
    )
    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        if action == 'update':
            try:
                aid = request.POST.get('assigned_to')
                assignee = None
                if aid:
                    assignee = get_object_or_404(User, pk=aid)
                ticket_service.update_ticket_fields(
                    ticket,
                    title=request.POST.get('title'),
                    description=request.POST.get('description', ''),
                    priority=request.POST.get('priority'),
                    assigned_to=assignee,
                )
                if request.headers.get('HX-Request'):
                    board = render_to_string(
                        'crm/projects/partials/ticket_board.html',
                        _ticket_board_ctx(project),
                        request=request,
                    )
                    resp = HttpResponse(board)
                    resp['HX-Trigger'] = json.dumps({
                        'crmCloseTicketDrawer': True,
                        'crmToast': 'Ticket saved',
                    })
                    return resp
            except ValueError as exc:
                messages.error(request, str(exc))
        elif action == 'delete':
            ticket.delete()
            if request.headers.get('HX-Request'):
                board = render_to_string(
                    'crm/projects/partials/ticket_board.html',
                    _ticket_board_ctx(project),
                    request=request,
                )
                resp = HttpResponse(board)
                resp['HX-Trigger'] = json.dumps({'crmCloseTicketDrawer': True})
                return resp
            return redirect('crm:project_detail', pk=pk)
        ticket.refresh_from_db()
    team_qs = User.objects.filter(
        pk__in=project.memberships.values_list('user_id', flat=True),
        is_active=True,
    ).order_by('username')
    return render(
        request,
        'crm/projects/partials/ticket_drawer.html',
        {
            'project': project,
            'ticket': ticket,
            'team_users': team_qs,
            'priority_choices': ProjectTicket.Priority.choices,
        },
    )


@login_required
@require_POST
def project_ticket_upload(request, pk, ticket_pk):
    project = get_object_or_404(get_projects_for_user(request.user), pk=pk)
    ticket = get_object_or_404(ProjectTicket, pk=ticket_pk, project=project)
    uploaded = request.FILES.get('file')
    if uploaded:
        ticket_service.add_attachment(
            ticket, uploaded_file=uploaded, uploaded_by=request.user
        )
    if request.headers.get('HX-Request'):
        ticket.refresh_from_db()
        return render(
            request,
            'crm/projects/partials/ticket_attachments.html',
            {'ticket': ticket},
        )
    return redirect('crm:project_detail', pk=pk)
