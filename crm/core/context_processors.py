"""Context for CRM pages only: navigation, notifications, company, test-mode banner."""
from __future__ import annotations


def crm(request):
    if not request.path.startswith('/crm/'):
        return {}
    from django.conf import settings
    ctx = {'crm_section': True, 'vapid_public_key': settings.VAPID_PUBLIC_KEY}
    user = getattr(request, 'user', None)
    try:
        from crm.models import CompanySettings
        company = CompanySettings.get()
    except Exception:  # pragma: no cover - before migrate
        return ctx
    ctx['company'] = company
    if user is None or not user.is_authenticated:
        return ctx
    from crm.models import Notification
    from .nav import build_nav, mobile_tabs
    match = getattr(request, 'resolver_match', None)
    url_name = match.url_name if match else ''
    ctx['nav_groups'] = build_nav(user, url_name or '')
    ctx['nav_current'] = url_name
    ctx['mobile_tabs'] = mobile_tabs(user, url_name or '')
    unread = Notification.objects.filter(user=user, read_at__isnull=True)
    ctx['unread_count'] = unread.count()
    ctx['recent_notifications'] = list(unread.order_by('-id')[:8])
    ctx['gateway_test_mode'] = company.gateway_provider != 'none' and not company.gateway_live
    return ctx
