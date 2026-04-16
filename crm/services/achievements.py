from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from crm.models import Achievement, EmployeeProfile, MonthlyTarget


@dataclass
class MonthlyPerformance:
    total_achieved: Decimal
    target: Decimal
    remaining: Decimal
    days_left: int
    per_day_required: Decimal
    per_week_required: Decimal


def _month_bounds(month: date) -> tuple[date, date]:
    """Return (first_day, first_day_of_next_month) for the given month date."""
    first = month.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1, day=1)
    else:
        nxt = first.replace(month=first.month + 1, day=1)
    return first, nxt


def _days_left_in_month(month_start: date) -> int:
    """Number of days (inclusive of today) remaining in the month from local today."""
    today = timezone.localdate()
    first, nxt = _month_bounds(month_start)
    if today < first:
        # Whole month left
        return (nxt - first).days
    if today >= nxt:
        return 0
    # Today is within the month: from today (inclusive) to end of month
    return (nxt - today).days


def get_monthly_performance(user, month: date, package=None) -> MonthlyPerformance:
    """
    Compute monthly performance metrics for a user.

    Returns:
        MonthlyPerformance: dataclass with totals and per-period requirements.
    """
    if not isinstance(month, date):
        raise TypeError('month must be a datetime.date instance')

    month_start, month_end = _month_bounds(month)

    qs = Achievement.objects.filter(
        employee=user,
        achieved_date__gte=month_start,
        achieved_date__lt=month_end,
    )
    if package is not None:
        qs = qs.filter(package=package)

    total_achieved = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    # Target resolution: MonthlyTarget takes precedence, fall back to EmployeeProfile
    try:
        mt = MonthlyTarget.objects.get(employee=user, month=month_start)
        target = mt.target_amount or Decimal('0')
    except MonthlyTarget.DoesNotExist:
        # Backward compatibility: if month wasn't normalized to day=1, match by year/month.
        mt = (
            MonthlyTarget.objects.filter(
                employee=user,
                month__year=month_start.year,
                month__month=month_start.month,
            )
            .order_by('month', '-id')
            .first()
        )
        if mt:
            target = mt.target_amount or Decimal('0')
        else:
            try:
                profile = EmployeeProfile.objects.get(user=user)
                target = profile.target_amount or Decimal('0')
            except EmployeeProfile.DoesNotExist:
                target = Decimal('0')

    remaining = target - total_achieved
    if remaining < Decimal('0'):
        remaining = Decimal('0')

    days_left = _days_left_in_month(month_start)
    if days_left > 0 and remaining > 0:
        per_day_required = (remaining / Decimal(days_left)).quantize(Decimal('0.01'))
    else:
        per_day_required = Decimal('0')

    if days_left > 0 and remaining > 0:
        weeks_left = (days_left + 6) // 7
        weeks_left = max(weeks_left, 1)
        per_week_required = (remaining / Decimal(weeks_left)).quantize(Decimal('0.01'))
    else:
        per_week_required = Decimal('0')

    return MonthlyPerformance(
        total_achieved=total_achieved,
        target=target,
        remaining=remaining,
        days_left=days_left,
        per_day_required=per_day_required,
        per_week_required=per_week_required,
    )

