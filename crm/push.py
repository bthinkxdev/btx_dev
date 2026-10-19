"""
Web Push delivery — the one place that actually talks to the push service.
Every trigger (new lead, follow-up reminders, lead assigned, lead won, the
manual test button, and whatever gets added later) goes through
`send_push_to_user` / `send_push_to_users` so delivery, failure logging,
quiet hours and dead-subscription cleanup are handled in exactly one spot.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.utils import timezone

from .models import NotificationEvent, PushSubscription

logger = logging.getLogger(__name__)


def vapid_configured() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY)


def in_quiet_hours(now=None) -> bool:
    """Quiet hours wrap past midnight (e.g. 21:00 -> 09:00), so this is an
    'outside the day window' check, not a simple start<now<end range."""
    now = now or timezone.now()
    local_hour = timezone.localtime(now).hour
    start = settings.NOTIFICATION_QUIET_HOURS_START
    end = settings.NOTIFICATION_QUIET_HOURS_END
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def send_push_to_user(user, *, kind, title, body='', url='', lead=None, bypass_quiet_hours=False):
    """Send one notification to every device `user` has subscribed from.

    Returns the created NotificationEvent (success or failure — both are
    logged so the in-app history and admin show what was attempted), or
    None if the send was skipped entirely for quiet hours. Callers that use
    a per-row 'already notified' flag (reminder jobs) should only set that
    flag when this returns non-None — None means 'try again on the next run'.
    """
    is_urgent = kind in NotificationEvent.URGENT_KINDS or bypass_quiet_hours
    if not is_urgent and in_quiet_hours():
        return None

    if not vapid_configured():
        logger.warning('VAPID keys not configured — skipping push "%s" to %s', title, user)
        return NotificationEvent.objects.create(
            kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
            is_success=False, error='VAPID keys not configured',
        )

    subs = list(PushSubscription.objects.filter(user=user))
    if not subs:
        return NotificationEvent.objects.create(
            kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
            is_success=False, error='No push subscription for user',
        )

    from pywebpush import WebPushException, webpush

    # One NotificationEvent represents the logical notification (used for
    # dedup, history and click tracking); if the user has several devices we
    # still send to every one of them under that same event's id.
    event = NotificationEvent.objects.create(
        kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
        is_success=False,
    )
    payload = json.dumps({'id': event.pk, 'title': title, 'body': body, 'url': url, 'tag': kind})

    delivered = False
    last_error = ''
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': settings.VAPID_CLAIM_EMAIL},
            )
            delivered = True
        except WebPushException as exc:
            status = getattr(exc.response, 'status_code', None)
            if status in (404, 410):
                # The browser/push service says this subscription is dead — stop
                # trying it instead of failing silently forever.
                sub.delete()
            last_error = str(exc)
            logger.warning('Push failed for %s (%s): %s', user, kind, exc)

    event.is_success = delivered
    event.error = '' if delivered else last_error
    if delivered:
        event.delivered_at = timezone.now()
    event.save(update_fields=['is_success', 'error', 'delivered_at'])
    return event


def send_push_to_users(users, **kwargs):
    for user in users:
        send_push_to_user(user, **kwargs)


def send_push_to_admins(**kwargs):
    """New-website-lead notifications go to every CRM admin, not one specific
    owner — there's no lead owner yet at that point."""
    from django.contrib.auth import get_user_model

    from .rbac import is_crm_admin

    User = get_user_model()
    admins = [u for u in User.objects.filter(is_active=True) if is_crm_admin(u)]
    send_push_to_users(admins, **kwargs)


def send_push_to_managers(**kwargs):
    """Escalations go to sales managers + admins (the people with
    can_view_all_sales_data), not a specific 1:1 'reports to' manager —
    this CRM's roles don't model that hierarchy."""
    from django.contrib.auth import get_user_model

    from .rbac import can_view_all_sales_data

    User = get_user_model()
    managers = [u for u in User.objects.filter(is_active=True) if can_view_all_sales_data(u)]
    send_push_to_users(managers, **kwargs)
