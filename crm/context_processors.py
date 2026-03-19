"""CRM-wide template context (header bell, etc.)."""

from datetime import datetime

from django.utils import timezone

from .models import FollowUp, Task


def crm_header(request):
    out = {
        'crm_overdue_fu_count': 0,
        'crm_tasks_overdue_n': 0,
        'crm_tasks_today_n': 0,
        'crm_tasks_open_n': 0,
        'crm_tasks_undated_n': 0,
    }
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return out
    path = getattr(request, 'path', '') or ''
    if '/crm' not in path or '/crm/login' in path:
        return out
    now = timezone.now()
    if timezone.is_aware(now):
        local = timezone.localtime(now).date()
    else:
        local = now.date()
    start = timezone.make_aware(datetime.combine(local, datetime.min.time()))
    out['crm_overdue_fu_count'] = FollowUp.objects.filter(
        employee=user,
        is_done=False,
        datetime__lt=start,
    ).count()
    out['crm_tasks_overdue_n'] = Task.objects.filter(
        employee=user,
        is_completed=False,
        due_date__isnull=False,
        due_date__lt=local,
    ).count()
    out['crm_tasks_today_n'] = Task.objects.filter(
        employee=user,
        is_completed=False,
        due_date=local,
    ).count()
    out['crm_tasks_open_n'] = Task.objects.filter(
        employee=user,
        is_completed=False,
    ).count()
    out['crm_tasks_undated_n'] = Task.objects.filter(
        employee=user,
        is_completed=False,
        due_date__isnull=True,
    ).count()
    return out
