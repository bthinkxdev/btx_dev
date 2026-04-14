import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import TruncDate, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .services.whatsapp import handle_message, is_duplicate_event, mask_phone
from .forms import (
    ExcelImportForm,
    FollowUpForm,
    LeadForm,
    PackageForm,
    QuickFollowUpForm,
    QuickNoteForm,
    RescheduleFollowUpForm,
    TaskForm,
)
from .models import ActivityLog, EmployeeProfile, FollowUp, Lead, Package, Task
from .utils import (
    get_report_data,
    import_leads_from_excel,
    log_activity,
    recalc_lead_next_followup,
)

logger = logging.getLogger(__name__)


def _profile(user):
    return EmployeeProfile.objects.get_or_create(user=user)[0]


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


def _followups_queue_context(user):
    start, end, _ = _local_today_bounds()
    base = FollowUp.objects.filter(employee=user).select_related('lead')
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
    WhatsApp Cloud API webhook endpoint:
    - GET: verification handshake (hub.mode / hub.verify_token / hub.challenge)
    - POST: incoming message events
    """
    if request.method == 'GET':
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge', '')
        verify_token = getattr(settings, 'WHATSAPP_VERIFY_TOKEN', '')

        if mode == 'subscribe' and verify_token and token == verify_token:
            return HttpResponse(challenge, content_type='text/plain')
        return JsonResponse({'error': 'Invalid verify token'}, status=403)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (TypeError, ValueError, UnicodeDecodeError):
        logger.exception('Invalid WhatsApp webhook payload')
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
    logger.info('WhatsApp webhook received: entries=%s', len(payload.get('entry', [])))

    try:
        entries = payload.get('entry')
        if not isinstance(entries, list) or not entries:
            return JsonResponse({'status': 'ignored', 'reason': 'missing_entry'})
        processed = 0
        duplicates = 0
        ignored = 0
        for entry in entries:
            changes = (entry or {}).get('changes') or []
            if not isinstance(changes, list):
                continue
            for change in changes:
                value = (change or {}).get('value', {})
                messages = value.get('messages', [])
                if not isinstance(messages, list):
                    continue
                for message in messages:
                    message = message or {}
                    message_type = message.get('type')
                    phone = message.get('from')
                    message_id = message.get('id')
                    text = ''

                    if message_type == 'text':
                        text = ((message.get('text') or {}).get('body') or '').strip()
                    elif message_type == 'interactive':
                        interactive = message.get('interactive') or {}
                        button_reply = interactive.get('button_reply') or {}
                        list_reply = interactive.get('list_reply') or {}
                        text = (
                            button_reply.get('id')
                            or list_reply.get('id')
                            or button_reply.get('title')
                            or list_reply.get('title')
                            or ''
                        ).strip()
                    else:
                        ignored += 1
                        continue

                    if not phone or not text:
                        ignored += 1
                        continue
                    if is_duplicate_event(message_id):
                        duplicates += 1
                        logger.info(
                            'Ignored duplicate WhatsApp message id=%s phone=%s',
                            message_id,
                            mask_phone(phone),
                        )
                        continue

                    logger.info(
                        'Processing WhatsApp message id=%s phone=%s type=%s',
                        message_id,
                        mask_phone(phone),
                        message_type,
                    )
                    handle_message(phone, text)
                    processed += 1
    except Exception:
        logger.exception('Failed to process WhatsApp webhook event')
        return JsonResponse({'status': 'error'}, status=500)

    if processed == 0 and duplicates == 0:
        return JsonResponse({'status': 'ignored', 'reason': 'no_messages'})
    return JsonResponse(
        {
            'status': 'ok',
            'processed': processed,
            'duplicates': duplicates,
            'ignored': ignored,
        }
    )


# GET ?sort=… for leads list (validated keys)
LEAD_SORT_EXEC = 'exec'
LEAD_SORT_DEFAULT = 'created_new'  # Newest created first unless ?sort=…
LEAD_SORT_CHOICES = (
    ('created_new', 'Created: newest first'),
    (LEAD_SORT_EXEC, 'Execution priority (FU)'),
    ('fu_soon', 'Follow-up: soonest first'),
    ('fu_late', 'Follow-up: latest first'),
    ('created_old', 'Created: oldest'),
    ('updated_new', 'Updated: newest'),
    ('updated_old', 'Updated: oldest'),
    ('status_az', 'Status A→Z'),
    ('status_za', 'Status Z→A'),
    ('deal_high', 'Deal value: high → low'),
    ('deal_low', 'Deal value: low → high'),
    ('name_az', 'Name A→Z'),
    ('name_za', 'Name Z→A'),
)
_LEAD_SORT_DB = {
    'created_new': ('-created_at', '-id'),
    'created_old': ('created_at', 'id'),
    'updated_new': ('-updated_at', '-id'),
    'updated_old': ('updated_at', 'id'),
    'status_az': ('status', 'name'),
    'status_za': ('-status', 'name'),
    'deal_high': ('-deal_value', '-updated_at'),
    'deal_low': ('deal_value', '-updated_at'),
    'name_az': ('name', 'id'),
    'name_za': ('-name', 'id'),
}

LEADS_PER_PAGE = 20


def _exec_bucket_expression(fu_start, fu_end):
    """
    Execution-priority bucket for ordering (mirrors _sales_sort_leads tiers).
    Tie-break: next_followup (nulls first), then updated_at desc.
    """
    return Case(
        When(status__in=TERMINAL_STATUSES, then=Value(5)),
        When(
            ~Q(status__in=TERMINAL_STATUSES)
            & (Q(next_followup__lt=fu_start) | Q(next_followup__isnull=True)),
            then=Value(0),
        ),
        When(
            ~Q(status__in=TERMINAL_STATUSES)
            & Q(next_followup__gte=fu_start, next_followup__lt=fu_end),
            then=Value(1),
        ),
        When(status__in=INTERESTED_SORT_STATUSES, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )


def _lead_for_exec(user, pk):
    """Single lead with activity + task counts for execution board."""
    start, end, local_date = _local_today_bounds()
    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    return (
        Lead.objects.filter(pk=pk, employee=user)
        .select_related('package')
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
        'status_choices': Lead.Status.choices,
        'packages': Package.objects.filter(employee=user),
        'fu_start': start,
        'fu_end': end,
        'fu_bounds': (start, end),
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
            pkg = Package.objects.filter(pk=pid, employee=user).first()
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

    latest = ActivityLog.objects.filter(lead_id=OuterRef('pk')).order_by('-created_at')
    qs = (
        Lead.objects.filter(employee=user)
        .select_related('package')
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

    st = request.GET.get('status')
    if st in dict(Lead.Status.choices):
        qs = qs.filter(status=st)

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

    has_tasks_filter = request.GET.get('has_tasks', '').strip()
    if has_tasks_filter == '1':
        qs = qs.filter(task_open_count__gt=0)

    min_deal_s = request.GET.get('min_deal', '').strip()
    if min_deal_s:
        try:
            _min_deal = Decimal(str(min_deal_s))
            if _min_deal > 0:
                qs = qs.filter(deal_value__gte=_min_deal)
        except Exception:
            min_deal_s = ''

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
            qs = qs.filter(status=STATUS_CLOSED, updated_at__date=d)
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
                status=STATUS_CLOSED, updated_at__year=y, updated_at__month=m
            )
        except ValueError:
            pass

    date_scope = request.GET.get('date_scope', '').strip()
    date_basis = request.GET.get('date_basis', 'created').strip()
    if date_basis not in ('fu', 'created'):
        date_basis = 'created'
    if not date_scope and date_basis == 'fu':
        date_basis = 'created'
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
            if date_basis == 'created':
                qs = qs.filter(created_at__gte=ds, created_at__lt=de)
            else:
                qs = qs.filter(
                    next_followup__isnull=False,
                    next_followup__gte=ds,
                    next_followup__lt=de,
                )

    sort_key = request.GET.get('sort', LEAD_SORT_DEFAULT).strip()
    valid_sorts = {k for k, _ in LEAD_SORT_CHOICES}
    if sort_key not in valid_sorts:
        sort_key = LEAD_SORT_DEFAULT

    if sort_key == LEAD_SORT_EXEC:
        qs = qs.annotate(_exec_b=_exec_bucket_expression(start, end))
        qs = qs.order_by(
            '_exec_b',
            F('next_followup').asc(nulls_first=True),
            '-updated_at',
            '-id',
        )
    elif sort_key == 'fu_soon':
        qs = qs.order_by(F('next_followup').asc(nulls_last=True), '-updated_at', '-id')
    elif sort_key == 'fu_late':
        qs = qs.order_by(F('next_followup').desc(nulls_last=True), '-updated_at', '-id')
    else:
        qs = qs.order_by(*_LEAD_SORT_DB[sort_key])

    filters_ctx = {
        'q': q,
        'status': st or '',
        'high_hope': high_hope_filter,
        'fu': fu_filter,
        'package': pkg or '',
        'created_day': created_day,
        'closed_day': closed_day,
        'created_month': created_month,
        'closed_month': closed_month,
        'sort': sort_key,
        'date_scope': date_scope,
        'date_basis': date_basis,
        'date_start': date_start_s,
        'date_end': date_end_s,
        'has_tasks': has_tasks_filter,
        'min_deal': min_deal_s,
    }

    has_active_filters = bool(
        q
        or st
        or high_hope_filter
        or fu_filter
        or package_filter
        or created_day
        or closed_day
        or created_month
        or closed_month
        or date_scope
        or sort_key != LEAD_SORT_DEFAULT
        or has_tasks_filter
        or min_deal_s,
    )

    return {
        'qs': qs,
        'start': start,
        'end': end,
        'local_date': local_date,
        'sort_key': sort_key,
        'filters_ctx': filters_ctx,
        'package_filter': package_filter,
        'has_active_filters': has_active_filters,
        'pkg': pkg,
    }


@login_required
def leads_list(request):
    user = request.user
    meta = _leads_list_qs_and_meta(request, user)
    qs = meta['qs']
    start, end = meta['start'], meta['end']
    sort_key = meta['sort_key']
    filters_ctx = meta['filters_ctx']
    package_filter = meta['package_filter']
    has_active_filters = meta['has_active_filters']

    packages = Package.objects.filter(employee=user)

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
        'basis_fu': _lq(date_basis='fu'),
        'basis_created': _lq(date_basis='created'),
        'date_all': _lq(date_scope='', date_start='', date_end=''),
        'date_today': _lq(date_scope='today', date_start='', date_end=''),
        'date_yesterday': _lq(date_scope='yesterday', date_start='', date_end=''),
        'date_week': _lq(date_scope='this_week', date_start='', date_end=''),
        'date_month': _lq(date_scope='this_month', date_start='', date_end=''),
        'fu_all': _lq(fu='', has_tasks=''),
        'fu_overdue': _lq(fu='overdue', has_tasks=''),
        'fu_today': _lq(fu='today', has_tasks=''),
        'fu_hot': _lq(fu='hot', has_tasks=''),
        'has_tasks_on': _lq(fu='', has_tasks='1'),
        'has_tasks_off': _lq(has_tasks=''),
        'high_hope_all': _lq(high_hope=''),
        'high_hope_on': _lq(high_hope='1'),
    }
    status_pills = [{'val': '', 'label': 'All statuses', 'qs': _lq(status='')}]
    for _sv, _sl in Lead.Status.choices:
        status_pills.append({'val': _sv, 'label': _sl, 'qs': _lq(status=_sv)})

    # Global summary strip counts (always reflect full pipeline, not current filters)
    _all_leads = Lead.objects.filter(employee=user)
    all_leads_total = _all_leads.count()
    _active_q = ~Q(status__in=TERMINAL_STATUSES)
    overdue_count = _all_leads.filter(
        _active_q & (Q(next_followup__lt=start) | Q(next_followup__isnull=True))
    ).count()
    today_fu_count = _all_leads.filter(
        _active_q, next_followup__gte=start, next_followup__lt=end
    ).count()
    pending_tasks_count = Task.objects.filter(employee=user, is_completed=False).count()
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
            'status_choices': Lead.Status.choices,
            'form': form,
            'import_form': import_form,
            'fu_start': start,
            'fu_end': end,
            'fu_bounds': (start, end),
            'filters': filters_ctx,
            'leads_base': leads_base,
            'lqs': lqs,
            'status_pills': status_pills,
            'package_filter': package_filter,
            'has_active_filters': has_active_filters,
            'sort_choices': LEAD_SORT_CHOICES,
            'sort_current': sort_key,
            'sort_label': dict(LEAD_SORT_CHOICES).get(
                sort_key, dict(LEAD_SORT_CHOICES)[LEAD_SORT_DEFAULT]
            ),
            'overdue_count': overdue_count,
            'today_fu_count': today_fu_count,
            'pending_tasks_count': pending_tasks_count,
            'hot_leads_count': hot_leads_count,
        },
    )


@login_required
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
    packages = Package.objects.filter(employee=user)
    base_ctx = {
        'fu_start': start,
        'fu_end': end,
        'fu_bounds': (start, end),
        'status_choices': Lead.Status.choices,
        'packages': packages,
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
def lead_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 1:
        return JsonResponse({'results': []})
    rows = (
        Lead.objects.filter(employee=request.user)
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
@require_POST
def lead_patch(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
@require_POST
def lead_high_hope_toggle(request, pk):
    """
    Toggle lead.high_hope and return the matching partial for HTMX targets.
    """
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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


@login_required
@require_POST
def lead_quick_followup(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
    form = QuickFollowUpForm(request.POST)
    err = None
    if form.is_valid():
        dt = form.cleaned_data['fu_datetime']
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        fu = FollowUp.objects.create(
            lead=lead,
            employee=user,
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
        if not err:
            _hx_toast(resp, 'Scheduled')
        return resp
    return HttpResponse(status=204)


@login_required
@require_POST
def lead_quick_note(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
@require_POST
def lead_notes_save(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
@require_POST
def lead_contact_save(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
        'status_choices': Lead.Status.choices,
        'packages': Package.objects.filter(employee=user),
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
def lead_detail(request, pk):
    user = request.user
    lead = get_object_or_404(
        Lead.objects.filter(employee=user).select_related('package'), pk=pk
    )
    activities = lead.activities.all()[:100]
    followups = lead.followups.all().order_by('-datetime')[:50]
    tasks = lead.tasks.all()
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
            'fu_form': fu_form,
            'task_form': task_form,
            'status_choices': Lead.Status.choices,
            'packages': Package.objects.filter(employee=user),
            'fu_start': start,
            'fu_end': end,
        },
    )


@login_required
@require_POST
def lead_status_detail(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
                'status_choices': Lead.Status.choices,
                'packages': Package.objects.filter(employee=user),
                'fu_start': _local_today_bounds()[0],
                'fu_end': _local_today_bounds()[1],
            },
        )
        _hx_toast(resp, 'Updated')
        return resp
    return HttpResponse(status=204)


@login_required
@require_POST
def followup_add(request, lead_pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=lead_pk, employee=user)
    form = FollowUpForm(request.POST)
    fu_error = None
    if form.is_valid():
        fu = form.save(commit=False)
        fu.lead = lead
        fu.employee = user
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
                'status_choices': Lead.Status.choices,
                'packages': Package.objects.filter(employee=user),
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
        Task.objects.filter(lead_id=lead_pk, employee=user)
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
def lead_tasks_panel(request, pk):
    user = request.user
    get_object_or_404(Lead, pk=pk, employee=user)
    return render(
        request,
        'crm/partials/lead_tasks_panel_inner.html',
        _tasks_panel_ctx(user, pk),
    )


@login_required
@require_POST
def task_add(request, lead_pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=lead_pk, employee=user)
    form = TaskForm(request.POST)
    task_error = None
    if form.is_valid():
        t = form.save(commit=False)
        t.lead = lead
        t.employee = user
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
                'status_choices': Lead.Status.choices,
                'packages': Package.objects.filter(employee=user),
            },
        )
    return HttpResponse(status=204)


@login_required
@require_POST
def task_update(request, pk):
    user = request.user
    task = get_object_or_404(Task, pk=pk, employee=user)
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
@require_POST
def task_toggle(request, pk):
    user = request.user
    task = get_object_or_404(Task, pk=pk, employee=user)
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
@require_POST
def lead_log_call(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
    log_activity(lead, 'call', '')
    if request.headers.get('HX-Request'):
        r = HttpResponse(status=204)
        _hx_toast(r, 'Call logged')
        return r
    return HttpResponse(status=204)


@login_required
@require_POST
def lead_log_whatsapp(request, pk):
    user = request.user
    lead = get_object_or_404(Lead, pk=pk, employee=user)
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
def followups_page(request):
    user = request.user
    ctx = _followups_queue_context(user)
    return render(request, 'crm/followups.html', ctx)


@login_required
@require_POST
def followup_done(request, pk):
    user = request.user
    fu = get_object_or_404(FollowUp, pk=pk, employee=user)
    fu.is_done = True
    fu.save(update_fields=['is_done'])
    recalc_lead_next_followup(fu.lead)
    log_activity(fu.lead, 'follow_up_done', fu.note[:200])
    if request.headers.get('HX-Request'):
        from_queue = request.POST.get('_from') == 'followups_queue'
        if from_queue:
            ctx = _followups_queue_context(user)
            ctx['hx_oob'] = True
            resp = render(request, 'crm/partials/followups_list.html', ctx)
            _hx_toast(resp, 'Done')
            return resp
        r = HttpResponse(status=200)
        r['HX-Trigger'] = json.dumps({'crmToast': 'Done', 'crmFuDone': fu.pk})
        return r
    return HttpResponse(status=204)


@login_required
@require_POST
def followup_reschedule(request, pk):
    user = request.user
    fu = get_object_or_404(
        FollowUp.objects.filter(employee=user).select_related('lead'), pk=pk
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
            },
        )
    return HttpResponse(status=204)


@login_required
def packages_page(request):
    user = request.user
    packages = Package.objects.filter(employee=user)
    form = PackageForm()
    edit_id = request.GET.get('edit')
    edit_obj = None
    if edit_id and edit_id.isdigit():
        edit_obj = Package.objects.filter(pk=int(edit_id), employee=user).first()
    edit_form = PackageForm(instance=edit_obj) if edit_obj else None
    return render(
        request,
        'crm/packages.html',
        {
            'packages': packages,
            'form': form,
            'edit_obj': edit_obj,
            'edit_form': edit_form,
        },
    )


@login_required
@require_http_methods(['GET', 'POST'])
def package_create(request):
    user = request.user
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
@require_POST
def package_update(request, pk):
    user = request.user
    pkg = get_object_or_404(Package, pk=pk, employee=user)
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
@require_POST
def package_delete(request, pk):
    user = request.user
    Package.objects.filter(pk=pk, employee=user).delete()
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
def performance_report_card(request):
    """HTMX fragment: Sales Report Card for Daily / Weekly / Monthly."""
    period = (request.GET.get('period') or 'daily').strip().lower()
    return render(
        request,
        'crm/partials/sales_report_card_inner.html',
        {'r': get_report_data(request.user, period)},
    )
