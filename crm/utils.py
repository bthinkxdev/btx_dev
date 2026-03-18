"""Activity logging, next_followup sync, Excel import, sales report metrics."""

import io
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import ActivityLog, FollowUp, Lead, Package, Task

User = get_user_model()

# Flexible Excel column headers -> Lead field
COLUMN_ALIASES = {
    'name': ('name', 'lead name', 'full name', 'customer', 'client'),
    'phone': ('phone', 'mobile', 'tel', 'contact', 'phone number'),
    'email': ('email', 'e-mail', 'mail'),
    'source': ('source', 'channel', 'referral', 'origin'),
    'deal_value': ('deal value', 'value', 'amount', 'price', 'deal'),
    'package': ('package', 'product', 'plan'),
    'notes': ('notes', 'note', 'remarks', 'comments'),
}


def normalize_header(h):
    if h is None:
        return ''
    return re.sub(r'\s+', ' ', str(h).strip().lower())


def map_headers(row_headers):
    """Return dict field_name -> column index."""
    normalized = [normalize_header(c) for c in row_headers]
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for i, h in enumerate(normalized):
            if not h:
                continue
            if h in aliases or any(a in h for a in aliases if len(a) > 3):
                if field not in mapping:
                    mapping[field] = i
                break
            if field == 'name' and h == 'name':
                mapping[field] = i
    return mapping


def cell_str(row, idx):
    if idx is None or idx >= len(row):
        return ''
    v = row[idx]
    if v is None:
        return ''
    return str(v).strip()


def parse_decimal(s):
    if not s:
        return Decimal('0')
    s = str(s).replace(',', '').strip()
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal('0')


def log_activity(lead, action, note=''):
    ActivityLog.objects.create(lead=lead, action=action, note=note or '')


def recalc_lead_next_followup(lead):
    nxt = (
        lead.followups.filter(is_done=False)
        .order_by('datetime')
        .values_list('datetime', flat=True)
        .first()
    )
    Lead.objects.filter(pk=lead.pk).update(
        next_followup=nxt,
        updated_at=timezone.now(),
    )


def import_leads_from_excel(file, employee: User):
    """
    Parse .xlsx; create Lead for each valid row. All leads assigned to employee.
    Returns dict: created, skipped, errors (list of str).
    """
    try:
        import openpyxl
    except ImportError:
        return {
            'created': 0,
            'skipped': 0,
            'errors': ['openpyxl is required. pip install openpyxl'],
        }

    raw = file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        return {'created': 0, 'skipped': 0, 'errors': [f'Invalid Excel file: {e}']}

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return {'created': 0, 'skipped': 0, 'errors': ['Empty sheet']}

    header_row = rows[0]
    mapping = map_headers(header_row)
    if 'name' not in mapping:
        return {
            'created': 0,
            'skipped': 0,
            'errors': ['No "name" column found. Use headers: name, phone, email, source, package, deal value, notes'],
        }

    created = 0
    skipped = 0
    errors = []
    package_cache = {}

    def get_package_by_name(name):
        if not name:
            return None
        key = name.strip().lower()
        if key in package_cache:
            return package_cache[key]
        pkg = Package.objects.filter(employee=employee, name__iexact=name.strip()).first()
        package_cache[key] = pkg
        return pkg

    for line_num, row in enumerate(rows[1:], start=2):
        if not row or all(v is None or str(v).strip() == '' for v in row):
            skipped += 1
            continue
        name = cell_str(row, mapping.get('name'))
        if not name:
            skipped += 1
            continue
        phone = cell_str(row, mapping.get('phone'))
        email = cell_str(row, mapping.get('email'))[:254]
        source = cell_str(row, mapping.get('source'))[:120]
        deal_value = parse_decimal(cell_str(row, mapping.get('deal_value')))
        pkg_name = cell_str(row, mapping.get('package'))
        notes = cell_str(row, mapping.get('notes'))
        pkg = get_package_by_name(pkg_name)

        try:
            with transaction.atomic():
                lead = Lead.objects.create(
                    employee=employee,
                    name=name[:200],
                    phone=phone[:40],
                    email=email,
                    source=source,
                    status=Lead.Status.NEW,
                    package=pkg,
                    deal_value=deal_value,
                    notes=notes,
                )
                log_activity(lead, 'imported', 'Excel import')
                created += 1
        except Exception as e:
            errors.append(f'Row {line_num}: {e}')

    return {'created': created, 'skipped': skipped, 'errors': errors[:50]}


CRM_REPORT_BRAND = 'BThinkX CRM'


def _aware_day_start(d):
    """Local calendar date d → start of day (aware)."""
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


def get_report_data(user, period):
    """
    Sales report metrics for the logged-in user.

    period: 'daily' | 'weekly' | 'monthly'
    Window is local timezone: daily = today; weekly = Mon→today; monthly = 1st→today.

    Period metrics:
    - total_leads: leads created in window
    - contacted_leads: those not still NEW
    - followups_done: ActivityLog follow_up_done in window (accurate completion time)
    - interested_leads: leads in INTERESTED/NEGOTIATION touched in window (updated_at)
    - closed_deals / revenue: WON with updated_at in window
    - conversion_rate: closed_deals / total_leads * 100

    Snapshot (current): overdue_followups, pending_tasks, hot_leads
    """
    period = (period or 'daily').lower()
    if period not in ('daily', 'weekly', 'monthly'):
        period = 'daily'

    now = timezone.now()
    local = timezone.localtime(now).date() if timezone.is_aware(now) else now.date()
    today_end = _aware_day_start(local + timedelta(days=1))

    if period == 'daily':
        start = _aware_day_start(local)
        period_title = 'Daily'
        date_line = local.strftime('%Y-%m-%d')
    elif period == 'weekly':
        monday = local - timedelta(days=local.weekday())
        start = _aware_day_start(monday)
        period_title = 'Weekly'
        date_line = f'{monday.isoformat()} → {local.isoformat()}'
    else:
        first = local.replace(day=1)
        start = _aware_day_start(first)
        period_title = 'Monthly'
        date_line = f'{first.strftime("%Y-%m-%d")} → {local.isoformat()}'

    leads = Lead.objects.filter(employee=user)
    not_new = [s for s, _ in Lead.Status.choices if s != Lead.Status.NEW]

    total_leads = leads.filter(
        created_at__gte=start, created_at__lt=today_end
    ).count()
    contacted_leads = leads.filter(
        created_at__gte=start,
        created_at__lt=today_end,
        status__in=not_new,
    ).count()
    followups_done = ActivityLog.objects.filter(
        lead__employee=user,
        action='follow_up_done',
        created_at__gte=start,
        created_at__lt=today_end,
    ).count()
    interested_leads = leads.filter(
        updated_at__gte=start,
        updated_at__lt=today_end,
        status__in=(Lead.Status.INTERESTED, Lead.Status.NEGOTIATION),
    ).count()
    closed_qs = leads.filter(
        status=Lead.Status.WON,
        updated_at__gte=start,
        updated_at__lt=today_end,
    )
    closed_deals = closed_qs.count()
    revenue = closed_qs.aggregate(s=Sum('deal_value'))['s'] or Decimal('0')
    conversion_rate = (
        round((closed_deals / total_leads * 100), 1) if total_leads else 0.0
    )

    day_start = _aware_day_start(local)
    overdue_followups = FollowUp.objects.filter(
        employee=user, is_done=False, datetime__lt=day_start
    ).count()
    pending_tasks = Task.objects.filter(
        employee=user, is_completed=False
    ).count()
    active_q = ~Q(status__in=(Lead.Status.WON, Lead.Status.LOST))
    hot_leads = leads.filter(
        active_q,
        status__in=(
            Lead.Status.INTERESTED,
            Lead.Status.NEGOTIATION,
            Lead.Status.QUALIFIED,
            Lead.Status.PROPOSAL,
        ),
        deal_value__gt=0,
    ).count()

    rev_whole = revenue == revenue.quantize(Decimal('1'))
    rev_str = f'{int(revenue):,}' if rev_whole else f'{revenue:,.2f}'
    report_text = (
        f'📊 {CRM_REPORT_BRAND} — {period_title} Report ({date_line})\n\n'
        f'Leads: {total_leads}\n'
        f'Contacted: {contacted_leads}\n'
        f'Follow-ups Done: {followups_done}\n'
        f'Interested: {interested_leads}\n'
        f'Closed: {closed_deals}\n'
        f'Revenue: ₹{rev_str}\n\n'
        f'Overdue Follow-ups: {overdue_followups}\n'
        f'Pending Tasks: {pending_tasks}\n'
        f'Hot Leads: {hot_leads}\n\n'
        f'Conversion Rate: {conversion_rate}%'
    )

    return {
        'period': period,
        'period_title': period_title,
        'date_line': date_line,
        'total_leads': total_leads,
        'contacted_leads': contacted_leads,
        'followups_done': followups_done,
        'interested_leads': interested_leads,
        'closed_deals': closed_deals,
        'revenue': revenue,
        'revenue_display': rev_str,
        'conversion_rate': conversion_rate,
        'overdue_followups': overdue_followups,
        'pending_tasks': pending_tasks,
        'hot_leads': hot_leads,
        'report_text': report_text,
    }
