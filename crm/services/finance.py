"""Finance income & expense tracking helpers (not accounting)."""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from ..models import (
    AuditEntry,
    Expense,
    ExpenseCategory,
    Income,
    IncomeCategory,
    Project,
)
from . import audit as audit_service

DEFAULT_INCOME_CATEGORIES = (
    'Project Payment',
    'Advance',
    'Renewal',
    'Change Request',
    'Support / AMC',
    'Other',
)

DEFAULT_EXPENSE_CATEGORIES = (
    'Hosting',
    'Salary',
    'Marketing',
    'Office',
    'Internet',
    'Travel',
    'Fuel',
    'Food',
    'Software',
    'Domain',
    'Misc',
)

# Back-compat alias
DEFAULT_CATEGORIES = DEFAULT_INCOME_CATEGORIES


def ensure_default_categories() -> None:
    for name in DEFAULT_INCOME_CATEGORIES:
        IncomeCategory.objects.get_or_create(name=name, defaults={'active': True})


def ensure_default_expense_categories() -> None:
    for name in DEFAULT_EXPENSE_CATEGORIES:
        ExpenseCategory.objects.get_or_create(name=name, defaults={'active': True})


def _money(value) -> Decimal:
    if value is None:
        return Decimal('0')
    return Decimal(value)


def month_bounds(ref: date | None = None) -> tuple[date, date]:
    today = ref or timezone.localdate()
    start = today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    end = today.replace(day=last_day)
    return start, end


def week_bounds(ref: date | None = None) -> tuple[date, date]:
    today = ref or timezone.localdate()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def resolve_period(
    period: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    ref: date | None = None,
) -> tuple[date, date, str]:
    """
    Return (start, end, label) for dashboard/list filters.
    KPI cards always use current month separately.
    """
    today = ref or timezone.localdate()
    period = (period or 'month').strip().lower()
    if period == 'today':
        return today, today, 'Today'
    if period == 'week':
        start, end = week_bounds(today)
        return start, end, 'This Week'
    if period == 'custom' and date_from and date_to:
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        return date_from, date_to, 'Custom'
    start, end = month_bounds(today)
    return start, end, 'This Month'


def incomes_between(start: date, end: date) -> QuerySet[Income]:
    return Income.objects.filter(payment_date__gte=start, payment_date__lte=end)


def expenses_between(start: date, end: date) -> QuerySet[Expense]:
    return Expense.objects.filter(expense_date__gte=start, expense_date__lte=end)


def sum_amount(qs: QuerySet) -> Decimal:
    return _money(qs.aggregate(total=Sum('amount'))['total'])


def _prev_month_bounds(ref: date) -> tuple[date, date]:
    first = ref.replace(day=1)
    last_prev = first - timedelta(days=1)
    return month_bounds(last_prev)


def _growth_pct(current: Decimal, previous: Decimal) -> float:
    if previous == 0:
        if current == 0:
            return 0.0
        return 100.0
    return float(((current - previous) / previous) * Decimal('100'))


def get_management_dashboard(*, ref: date | None = None) -> dict[str, Any]:
    """
    Real-time management dashboard from Income + Expense tables.
    Always computed live — no cache; updates as soon as rows change.
    Uses aggregated annotations to minimise round-trips.
    """
    today = ref or timezone.localdate()
    month_start, month_end = month_bounds(today)
    prev_start, prev_end = _prev_month_bounds(today)
    days_elapsed = max(today.day, 1)
    # Chart window: month start → today (not future days)
    chart_end = min(today, month_end)

    # ── Core aggregates (few queries) ───────────────────────────────
    month_income = sum_amount(incomes_between(month_start, month_end))
    month_expense = sum_amount(expenses_between(month_start, month_end))
    today_income = sum_amount(incomes_between(today, today))
    today_expense = sum_amount(expenses_between(today, today))
    prev_income = sum_amount(incomes_between(prev_start, prev_end))
    prev_expense = sum_amount(expenses_between(prev_start, prev_end))

    month_profit = month_income - month_expense
    today_profit = today_income - today_expense

    cash_in = sum_amount(
        incomes_between(month_start, month_end).filter(
            payment_type=Income.PaymentType.CASH
        )
    )
    cash_out = sum_amount(
        expenses_between(month_start, month_end).filter(
            payment_method=Expense.PaymentMethod.CASH
        )
    )

    avg_daily_revenue = (month_income / days_elapsed).quantize(Decimal('0.01'))
    avg_daily_expense = (month_expense / days_elapsed).quantize(Decimal('0.01'))

    pending_collections = _money(
        Project.objects.filter(balance_due__gt=0).aggregate(total=Sum('balance_due'))['total']
    )

    # Top client / project (current month income)
    top_client_row = (
        incomes_between(month_start, month_end)
        .filter(client__isnull=False)
        .values('client_id', 'client__business_name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
        .first()
    )
    top_project_row = (
        incomes_between(month_start, month_end)
        .filter(project__isnull=False)
        .values(
            'project_id',
            'project__client__business_name',
        )
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
        .first()
    )
    highest_expense_cat = (
        expenses_between(month_start, month_end)
        .values('category_id', 'category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
        .first()
    )

    # ── Daily series (current month through today) ──────────────────
    daily_labels, daily_revenue = _daily_series(
        incomes_between(month_start, chart_end), 'payment_date', month_start, chart_end
    )
    _, daily_expense = _daily_series(
        expenses_between(month_start, chart_end), 'expense_date', month_start, chart_end
    )
    daily_profit = [
        round(daily_revenue[i] - daily_expense[i], 2) for i in range(len(daily_labels))
    ]

    # ── Monthly comparison (last 6 calendar months) — TruncMonth aggregates ─
    monthly_labels: list[str] = []
    monthly_revenue: list[float] = []
    monthly_expense: list[float] = []
    monthly_profit: list[float] = []
    cursor = today.replace(day=1)
    months_back: list[date] = []
    for _ in range(6):
        months_back.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months_back.reverse()

    range_start = months_back[0]
    _, range_end = month_bounds(months_back[-1])

    rev_by_month = {
        row['month']: _money(row['total'])
        for row in (
            Income.objects.filter(
                payment_date__gte=range_start,
                payment_date__lte=range_end,
            )
            .annotate(month=TruncMonth('payment_date'))
            .values('month')
            .annotate(total=Sum('amount'))
        )
        if row['month']
    }
    exp_by_month = {
        row['month']: _money(row['total'])
        for row in (
            Expense.objects.filter(
                expense_date__gte=range_start,
                expense_date__lte=range_end,
            )
            .annotate(month=TruncMonth('expense_date'))
            .values('month')
            .annotate(total=Sum('amount'))
        )
        if row['month']
    }

    def _month_key_match(store: dict, key: date) -> Decimal:
        for k, v in store.items():
            kd = k.date() if hasattr(k, 'date') else k
            if kd.year == key.year and kd.month == key.month:
                return v
        return Decimal('0')

    for m in months_back:
        key = m.replace(day=1)
        rev = _month_key_match(rev_by_month, key)
        exp = _month_key_match(exp_by_month, key)
        monthly_labels.append(m.strftime('%b %Y'))
        monthly_revenue.append(float(rev))
        monthly_expense.append(float(exp))
        monthly_profit.append(float(rev - exp))

    # ── Breakdown tables (annotated) ────────────────────────────────
    top_clients = list(
        incomes_between(month_start, month_end)
        .filter(client__isnull=False)
        .values('client_id', 'client__business_name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')[:8]
    )
    top_projects = list(
        incomes_between(month_start, month_end)
        .filter(project__isnull=False)
        .values('project_id', 'project__client__business_name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')[:8]
    )
    top_expense_categories = list(
        expenses_between(month_start, month_end)
        .values('category_id', 'category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')[:8]
    )
    client_revenue_rows = top_clients[:8]
    project_revenue_rows = top_projects[:8]
    expense_dist_rows = top_expense_categories

    recent_income = list(
        Income.objects.select_related('client', 'project', 'category')
        .order_by('-payment_date', '-created_at')[:10]
    )
    recent_expenses = list(
        Expense.objects.select_related('category', 'project', 'employee')
        .order_by('-expense_date', '-created_at')[:10]
    )

    return {
        # KPI metrics
        'month_revenue': month_income,
        'month_expense': month_expense,
        'month_profit': month_profit,
        'today_revenue': today_income,
        'today_expense': today_expense,
        'today_profit': today_profit,
        'cash_in': cash_in,
        'cash_out': cash_out,
        'avg_daily_revenue': avg_daily_revenue,
        'avg_daily_expense': avg_daily_expense,
        'pending_collections': pending_collections,
        'revenue_growth': _growth_pct(month_income, prev_income),
        'expense_growth': _growth_pct(month_expense, prev_expense),
        'prev_month_revenue': prev_income,
        'prev_month_expense': prev_expense,
        'days_elapsed': days_elapsed,
        'month_start': month_start,
        'month_end': month_end,
        'today': today,
        # Highlights
        'top_client': top_client_row,
        'top_project': top_project_row,
        'highest_expense_category': highest_expense_cat,
        # Chart JSON
        'daily_labels': json.dumps(daily_labels),
        'daily_revenue_values': json.dumps(daily_revenue),
        'daily_expense_values': json.dumps(daily_expense),
        'daily_profit_values': json.dumps(daily_profit),
        'monthly_labels': json.dumps(monthly_labels),
        'monthly_revenue_values': json.dumps(monthly_revenue),
        'monthly_expense_values': json.dumps(monthly_expense),
        'monthly_profit_values': json.dumps(monthly_profit),
        'client_revenue_labels': json.dumps([
            r['client__business_name'] or '—' for r in client_revenue_rows
        ]),
        'client_revenue_values': json.dumps([
            float(_money(r['total'])) for r in client_revenue_rows
        ]),
        'project_revenue_labels': json.dumps([
            f"#{r['project_id']} {r['project__client__business_name']}"
            for r in project_revenue_rows
        ]),
        'project_revenue_values': json.dumps([
            float(_money(r['total'])) for r in project_revenue_rows
        ]),
        'expense_dist_labels': json.dumps([
            r['category__name'] or '—' for r in expense_dist_rows
        ]),
        'expense_dist_values': json.dumps([
            float(_money(r['total'])) for r in expense_dist_rows
        ]),
        # Tables
        'recent_income': recent_income,
        'recent_expenses': recent_expenses,
        'top_clients': top_clients,
        'top_projects': top_projects,
        'top_categories': top_expense_categories,
        # Back-compat aliases used by older dashboard bits
        'today_income': today_income,
        'yesterday_income': sum_amount(
            incomes_between(today - timedelta(days=1), today - timedelta(days=1))
        ),
        'month_income': month_income,
        'month_advance': sum_amount(
            incomes_between(month_start, month_end).filter(
                payment_status=Income.PaymentStatus.ADVANCE
            )
        ),
        'pending_collection': pending_collections,
        'month_transactions': incomes_between(month_start, month_end).count(),
        'period_income_total': month_income,
        'period_expense_total': month_expense,
    }


def get_dashboard_metrics(*, ref: date | None = None) -> dict[str, Any]:
    """Lightweight current-month KPI cards (income-focused helpers)."""
    today = ref or timezone.localdate()
    yesterday = today - timedelta(days=1)
    month_start, month_end = month_bounds(today)

    month_qs = incomes_between(month_start, month_end)
    return {
        'today_income': sum_amount(incomes_between(today, today)),
        'yesterday_income': sum_amount(incomes_between(yesterday, yesterday)),
        'month_income': sum_amount(month_qs),
        'month_advance': sum_amount(
            month_qs.filter(payment_status=Income.PaymentStatus.ADVANCE)
        ),
        'pending_collection': _money(
            Project.objects.filter(balance_due__gt=0).aggregate(total=Sum('balance_due'))[
                'total'
            ]
        ),
        'month_transactions': month_qs.count(),
        'today_expense': sum_amount(expenses_between(today, today)),
        'month_expense': sum_amount(expenses_between(month_start, month_end)),
        'month_start': month_start,
        'month_end': month_end,
        'today': today,
    }


def _daily_series(qs, date_field: str, start: date, end: date) -> tuple[list[str], list[float]]:
    """Build a contiguous daily total series.

    DateFields are already calendar dates — do not use TruncDate (breaks on
    SQLite with USE_TZ / Asia/Kolkata via django's tz UDF).
    """
    daily_map: dict[date, Decimal] = {}
    day = start
    while day <= end:
        daily_map[day] = Decimal('0')
        day += timedelta(days=1)

    for row in (
        qs.values(date_field)
        .annotate(total=Sum('amount'))
        .order_by(date_field)
    ):
        d = row[date_field]
        if d is None:
            continue
        if hasattr(d, 'date') and not isinstance(d, date):
            d = d.date()
        if d in daily_map:
            daily_map[d] = _money(row['total'])

    return (
        [d.strftime('%d %b') for d in daily_map],
        [float(v) for v in daily_map.values()],
    )


def get_chart_payload(start: date, end: date) -> dict[str, Any]:
    qs = incomes_between(start, end)
    daily_labels, daily_values = _daily_series(qs, 'payment_date', start, end)

    by_category = list(
        qs.values('category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )
    by_type = list(
        qs.values('payment_type')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )
    type_labels = dict(Income.PaymentType.choices)

    exp_qs = expenses_between(start, end)
    _, daily_expense_values = _daily_series(exp_qs, 'expense_date', start, end)
    exp_by_category = list(
        exp_qs.values('category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )

    return {
        'daily_labels': json.dumps(daily_labels),
        'daily_values': json.dumps(daily_values),
        'daily_expense_values': json.dumps(daily_expense_values),
        'category_labels': json.dumps([r['category__name'] or '—' for r in by_category]),
        'category_values': json.dumps([float(_money(r['total'])) for r in by_category]),
        'type_labels': json.dumps([
            type_labels.get(r['payment_type'], r['payment_type']) for r in by_type
        ]),
        'type_values': json.dumps([float(_money(r['total'])) for r in by_type]),
        'expense_category_labels': json.dumps(
            [r['category__name'] or '—' for r in exp_by_category]
        ),
        'expense_category_values': json.dumps(
            [float(_money(r['total'])) for r in exp_by_category]
        ),
        'top_expense_categories': exp_by_category[:5],
        'period_income_total': sum_amount(qs),
        'period_expense_total': sum_amount(exp_qs),
    }





def income_snapshot(income: Income) -> dict[str, Any]:
    return {
        'id': income.pk,
        'amount': str(income.amount),
        'category': income.category.name if income.category_id else None,
        'payment_type': income.payment_type,
        'payment_status': income.payment_status,
        'payment_date': income.payment_date.isoformat() if income.payment_date else None,
        'client_id': income.client_id,
        'project_id': income.project_id,
        'bank_account': income.bank_account,
        'reference': income.reference,
        'notes': income.notes,
    }


def expense_snapshot(expense: Expense) -> dict[str, Any]:
    return {
        'id': expense.pk,
        'amount': str(expense.amount),
        'category': expense.category.name if expense.category_id else None,
        'vendor': expense.vendor,
        'payment_method': expense.payment_method,
        'paid_from': expense.paid_from,
        'funding_bucket_id': expense.funding_bucket_id,
        'funding_bucket': (
            expense.funding_bucket.name if expense.funding_bucket_id else 'Other'
        ),
        'expense_date': expense.expense_date.isoformat() if expense.expense_date else None,
        'project_id': expense.project_id,
        'employee_id': expense.employee_id,
        'has_receipt': bool(expense.receipt),
        'notes': expense.notes,
    }


def log_income_event(
    *,
    action: str,
    income: Income,
    actor,
    before_state: dict | None = None,
    after_state: dict | None = None,
    ip_address: str | None = None,
    note: str = '',
):
    return audit_service.log_event(
        category=AuditEntry.EventCategory.FINANCE,
        action=action,
        object_type='Income',
        object_id=income.pk,
        object_repr=str(income)[:200],
        actor=actor,
        project=income.project,
        before_state=before_state,
        after_state=after_state,
        ip_address=ip_address,
        note=note,
    )


def log_expense_event(
    *,
    action: str,
    expense: Expense,
    actor,
    before_state: dict | None = None,
    after_state: dict | None = None,
    ip_address: str | None = None,
    note: str = '',
):
    return audit_service.log_event(
        category=AuditEntry.EventCategory.FINANCE,
        action=action,
        object_type='Expense',
        object_id=expense.pk,
        object_repr=str(expense)[:200],
        actor=actor,
        project=expense.project,
        before_state=before_state,
        after_state=after_state,
        ip_address=ip_address,
        note=note,
    )


def create_income(*, actor, ip_address: str | None = None, **fields) -> Income:
    from . import revenue_allocation as alloc_service

    income = Income.objects.create(created_by=actor, **fields)
    log_income_event(
        action='income_created',
        income=income,
        actor=actor,
        after_state=income_snapshot(income),
        ip_address=ip_address,
    )
    alloc_service.sync_income_allocations(
        income, actor=actor, ip_address=ip_address, note='Auto-allocate on create'
    )
    return income


def update_income(
    income: Income,
    *,
    actor,
    ip_address: str | None = None,
    **fields,
) -> Income:
    from . import revenue_allocation as alloc_service

    before = income_snapshot(income)
    for key, value in fields.items():
        setattr(income, key, value)
    income.save()
    log_income_event(
        action='income_updated',
        income=income,
        actor=actor,
        before_state=before,
        after_state=income_snapshot(income),
        ip_address=ip_address,
    )
    alloc_service.sync_income_allocations(
        income, actor=actor, ip_address=ip_address, note='Recalculate on income update'
    )
    return income


def delete_income(
    income: Income,
    *,
    actor,
    ip_address: str | None = None,
) -> None:
    from . import revenue_allocation as alloc_service

    before = income_snapshot(income)
    pk = income.pk
    project = income.project
    repr_ = str(income)[:200]
    alloc_before = [
        {
            'bucket_id': a.bucket_id,
            'bucket': a.bucket.name,
            'amount': str(a.amount),
            'percentage': str(a.percentage),
        }
        for a in income.allocations.select_related('bucket')
    ]
    alloc_service.clear_income_allocations(
        pk,
        actor=actor,
        project=project,
        income_repr=repr_,
        before_state={'allocations': alloc_before},
        ip_address=ip_address,
    )
    income.delete()
    audit_service.log_event(
        category=AuditEntry.EventCategory.FINANCE,
        action='income_deleted',
        object_type='Income',
        object_id=pk,
        object_repr=repr_,
        actor=actor,
        project=project,
        before_state=before,
        ip_address=ip_address,
    )


def create_expense(*, actor, ip_address: str | None = None, **fields) -> Expense:
    from django.core.exceptions import ValidationError

    from .fund_management import available_bucket_balance, _money

    amount = _money(fields.get('amount'))
    bucket = fields.get('funding_bucket')
    if bucket and (getattr(bucket, 'code', None) == 'founder_pool' or getattr(bucket, 'name', None) == 'Founder Pool'):
        raise ValidationError(
            'Founder Pool payouts require a founder account. '
            'Use Funds → Founder Withdraw instead.'
        )
    if bucket:
        available = available_bucket_balance(bucket)
        if amount > available:
            raise ValidationError(
                f'Insufficient balance in {bucket.name}. '
                f'Available Rs. {available:.2f}, requested Rs. {amount:.2f}.'
            )
    expense = Expense.objects.create(created_by=actor, **fields)
    log_expense_event(
        action='expense_created',
        expense=expense,
        actor=actor,
        after_state=expense_snapshot(expense),
        ip_address=ip_address,
    )
    return expense


def update_expense(
    expense: Expense,
    *,
    actor,
    ip_address: str | None = None,
    **fields,
) -> Expense:
    from django.core.exceptions import ValidationError

    from .fund_management import available_bucket_balance, _money

    before = expense_snapshot(expense)
    new_amount = _money(fields.get('amount', expense.amount))
    new_bucket = fields.get('funding_bucket', expense.funding_bucket)
    if new_bucket and (
        getattr(new_bucket, 'code', None) == 'founder_pool'
        or getattr(new_bucket, 'name', None) == 'Founder Pool'
    ):
        raise ValidationError(
            'Founder Pool payouts require a founder account. '
            'Use Funds → Founder Withdraw instead.'
        )
    if new_bucket:
        available = available_bucket_balance(new_bucket, exclude_expense=expense)
        if new_amount > available:
            raise ValidationError(
                f'Insufficient balance in {new_bucket.name}. '
                f'Available Rs. {available:.2f}, requested Rs. {new_amount:.2f}.'
            )
    for key, value in fields.items():
        setattr(expense, key, value)
    expense.save()
    log_expense_event(
        action='expense_updated',
        expense=expense,
        actor=actor,
        before_state=before,
        after_state=expense_snapshot(expense),
        ip_address=ip_address,
    )
    return expense


def delete_expense(
    expense: Expense,
    *,
    actor,
    ip_address: str | None = None,
) -> None:
    before = expense_snapshot(expense)
    pk = expense.pk
    project = expense.project
    repr_ = str(expense)[:200]
    if expense.receipt:
        expense.receipt.delete(save=False)
    expense.delete()
    audit_service.log_event(
        category=AuditEntry.EventCategory.FINANCE,
        action='expense_deleted',
        object_type='Expense',
        object_id=pk,
        object_repr=repr_,
        actor=actor,
        project=project,
        before_state=before,
        ip_address=ip_address,
    )


def report_by_category(start: date, end: date) -> dict[str, Any]:
    qs = expenses_between(start, end)
    rows = list(
        qs.values('category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )
    return {
        'rows': rows,
        'grand_total': sum_amount(qs),
        'chart_labels': json.dumps([r['category__name'] or '—' for r in rows]),
        'chart_values': json.dumps([float(_money(r['total'])) for r in rows]),
    }


def report_by_vendor(start: date, end: date) -> dict[str, Any]:
    qs = expenses_between(start, end)
    rows = list(
        qs.exclude(vendor='')
        .values('vendor')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )
    blank = qs.filter(vendor='')
    blank_total = sum_amount(blank)
    if blank_total or blank.exists():
        rows.append({
            'vendor': '(No vendor)',
            'total': blank_total,
            'count': blank.count(),
        })
        rows.sort(key=lambda r: _money(r['total']), reverse=True)
    return {
        'rows': rows,
        'grand_total': sum_amount(qs),
        'chart_labels': json.dumps([r['vendor'] or '—' for r in rows[:12]]),
        'chart_values': json.dumps([float(_money(r['total'])) for r in rows[:12]]),
    }


def report_by_project(start: date, end: date) -> dict[str, Any]:
    qs = expenses_between(start, end)
    rows = list(
        qs.filter(project__isnull=False)
        .values(
            'project_id',
            'project__client__business_name',
        )
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )
    unlinked = qs.filter(project__isnull=True)
    unlinked_total = sum_amount(unlinked)
    if unlinked_total or unlinked.exists():
        rows.append({
            'project_id': None,
            'project__client__business_name': '(No project)',
            'total': unlinked_total,
            'count': unlinked.count(),
        })
    return {
        'rows': rows,
        'grand_total': sum_amount(qs),
        'chart_labels': json.dumps([
            (
                f"#{r['project_id']} {r['project__client__business_name']}"
                if r['project_id']
                else r['project__client__business_name']
            )
            for r in rows[:12]
        ]),
        'chart_values': json.dumps([float(_money(r['total'])) for r in rows[:12]]),
    }


# ── Billing → Income sync ─────────────────────────────────────────────────────

_BILL_PAYMENT_TYPE_MAP = {
    'bank_transfer': 'bank',
    'upi': 'upi',
    'cheque': 'cheque',
    'cash': 'cash',
    'other': 'other',
}


def create_income_from_bill_payment(payment, *, actor=None) -> Income | None:
    """
    Idempotent: when a BillPayment is verified, mirror it as an Income row.
    Skips if already linked or payment is not verified.
    """
    from ..models import BillPayment

    if payment is None or not getattr(payment, 'pk', None):
        return None
    if payment.status != BillPayment.Status.VERIFIED:
        return None

    existing = Income.objects.filter(bill_payment=payment).first()
    if existing:
        return existing

    ensure_default_categories()
    project = payment.project
    client = project.client if project else None

    txn = (payment.transaction_id or '').strip()
    if txn.startswith('OPENING-'):
        cat_name = 'Advance'
        pay_status = Income.PaymentStatus.ADVANCE
    else:
        cat_name = 'Project Payment'
        if project and project.balance_due > 0:
            pay_status = Income.PaymentStatus.PARTIAL
        elif project and project.deal_value and project.advance_received < project.deal_value:
            pay_status = Income.PaymentStatus.PARTIAL
        else:
            pay_status = Income.PaymentStatus.FULL

    category, _ = IncomeCategory.objects.get_or_create(
        name=cat_name, defaults={'active': True}
    )
    pay_type = _BILL_PAYMENT_TYPE_MAP.get(
        payment.payment_method, Income.PaymentType.OTHER
    )
    ref = txn or (payment.bill.bill_number if payment.bill_id else f'PAY-{payment.pk}')
    note_bits = ['Auto-created from verified bill payment.']
    if payment.notes:
        note_bits.append(payment.notes)

    income = Income.objects.create(
        client=client,
        project=project,
        category=category,
        amount=payment.amount,
        payment_type=pay_type,
        payment_status=pay_status,
        payment_date=payment.payment_date,
        bank_account='',
        reference=str(ref)[:120],
        notes='\n'.join(note_bits)[:2000],
        bill_payment=payment,
        created_by=(
            actor
            if getattr(actor, 'is_authenticated', False)
            else payment.verified_by
        ),
    )
    log_income_event(
        action='income_auto_from_bill_payment',
        income=income,
        actor=actor or payment.verified_by,
        after_state={
            **income_snapshot(income),
            'bill_payment_id': payment.pk,
        },
        note=f'Synced from BillPayment #{payment.pk}',
    )
    from . import revenue_allocation as alloc_service

    alloc_service.sync_income_allocations(
        income,
        actor=actor or payment.verified_by,
        note='Auto-allocate from bill payment income',
    )
    return income


def get_project_finance(project: Project) -> dict[str, Any]:
    """Aggregated finance snapshot for a single project (live Income + Expense)."""
    income_qs = Income.objects.filter(project=project)
    expense_qs = Expense.objects.filter(project=project)
    revenue = sum_amount(income_qs)
    expenses = sum_amount(expense_qs)
    profit = revenue - expenses
    margin = float((profit / revenue) * Decimal('100')) if revenue > 0 else 0.0
    pending = _money(project.balance_due)

    expense_breakdown = list(
        expense_qs.values('category__name')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('-total')
    )

    timeline: list[dict[str, Any]] = []
    for row in income_qs.select_related('category').order_by(
        '-payment_date', '-created_at'
    )[:50]:
        timeline.append({
            'kind': 'income',
            'date': row.payment_date,
            'label': row.category.name,
            'detail': row.reference or (row.notes[:80] if row.notes else 'Income'),
            'amount': row.amount,
            'pk': row.pk,
        })
    for row in expense_qs.select_related('category').order_by(
        '-expense_date', '-created_at'
    )[:50]:
        timeline.append({
            'kind': 'expense',
            'date': row.expense_date,
            'label': row.category.name,
            'detail': row.vendor or (row.notes[:80] if row.notes else 'Expense'),
            'amount': row.amount,
            'pk': row.pk,
        })
    timeline.sort(key=lambda x: (x['date'], x['kind']), reverse=True)

    return {
        'revenue': revenue,
        'expenses': expenses,
        'profit': profit,
        'profit_margin': margin,
        'pending_payments': pending,
        'expense_breakdown': expense_breakdown,
        'timeline': timeline[:40],
        'income_count': income_qs.count(),
        'expense_count': expense_qs.count(),
        'recent_income': list(
            income_qs.select_related('category').order_by(
                '-payment_date', '-created_at'
            )[:10]
        ),
        'recent_expenses': list(
            expense_qs.select_related('category').order_by(
                '-expense_date', '-created_at'
            )[:10]
        ),
        'breakdown_labels': json.dumps(
            [r['category__name'] or '—' for r in expense_breakdown]
        ),
        'breakdown_values': json.dumps(
            [float(_money(r['total'])) for r in expense_breakdown]
        ),
    }


def get_client_finance(client) -> dict[str, Any]:
    """Aggregated finance snapshot for a client across all projects."""
    projects = Project.objects.filter(client=client)
    project_ids = list(projects.values_list('pk', flat=True))
    project_count = len(project_ids)
    total_revenue = _money(projects.aggregate(t=Sum('deal_value'))['t'])

    income_filter = Q(client=client)
    if project_ids:
        income_filter |= Q(project_id__in=project_ids)
    income_qs = Income.objects.filter(income_filter)
    total_received = sum_amount(income_qs)

    outstanding = _money(
        projects.filter(balance_due__gt=0).aggregate(t=Sum('balance_due'))['t']
    )
    avg_project_value = (
        (total_revenue / project_count).quantize(Decimal('0.01'))
        if project_count
        else Decimal('0')
    )
    last_payment = income_qs.order_by('-payment_date', '-created_at').first()
    payment_timeline = list(
        income_qs.select_related('project', 'category').order_by(
            '-payment_date', '-created_at'
        )[:40]
    )

    project_rows = []
    for p in projects.select_related('package').order_by('-created_at'):
        rev = sum_amount(Income.objects.filter(project=p))
        exp = sum_amount(Expense.objects.filter(project=p))
        project_rows.append({
            'project': p,
            'revenue': rev,
            'expenses': exp,
            'profit': rev - exp,
            'pending': p.balance_due,
        })

    return {
        'total_revenue': total_revenue,
        'total_received': total_received,
        'outstanding': outstanding,
        'total_projects': project_count,
        'average_project_value': avg_project_value,
        'last_payment': last_payment,
        'payment_timeline': payment_timeline,
        'project_rows': project_rows,
    }
