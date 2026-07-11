"""CRM-wide template context (header bell, etc.)."""

from datetime import datetime

from django.utils import timezone

from .models import EmployeeProfile, FollowUp, Task, WhatsAppBotExcludePhone
from .services import project_tickets as ticket_service
from .rbac import (
    can_access_billing,
    can_access_finance,
    can_access_sales_pipeline,
    can_view_financial_data,
    is_crm_developer,
)


def crm_header(request):
    out = {
        'crm_overdue_fu_count': 0,
        'crm_tasks_overdue_n': 0,
        'crm_tasks_today_n': 0,
        'crm_tasks_open_n': 0,
        'crm_tasks_undated_n': 0,
        'crm_tickets_open_n': 0,
        'crm_employee_profile': None,
        'crm_wa_excludes': [],
        'crm_is_developer': False,
        'crm_can_view_financial_data': True,
        'crm_can_access_sales_pipeline': True,
        'crm_can_access_billing': False,
        'crm_can_access_finance': False,
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
    try:
        out['crm_tickets_open_n'] = ticket_service.count_open_tickets_for_user(user)
    except Exception:
        out['crm_tickets_open_n'] = 0
    out['crm_employee_profile'] = EmployeeProfile.objects.filter(user=user).first()
    prof = out['crm_employee_profile']
    out['crm_whatsapp_bot_enabled'] = bool(prof and prof.whatsapp_bot_enabled)
    out['crm_wa_excludes'] = list(WhatsAppBotExcludePhone.objects.filter(executive=user)[:50])
    out['crm_is_developer'] = is_crm_developer(user)
    out['crm_can_view_financial_data'] = can_view_financial_data(user)
    out['crm_can_access_sales_pipeline'] = can_access_sales_pipeline(user)
    out['crm_can_access_billing'] = can_access_billing(user)
    out['crm_can_access_finance'] = can_access_finance(user)
    return out
