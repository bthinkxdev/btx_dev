"""
Web Push delivery — the one place that actually talks to the push service.
Every trigger (new lead, follow-up reminders, lead assigned, lead won, the
manual test button, and whatever gets added later) goes through
`send_push_to_user` / `send_push_to_users` so delivery, failure logging and
dead-subscription cleanup are handled in exactly one spot.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings

from .models import NotificationEvent, PushSubscription

logger = logging.getLogger(__name__)


def vapid_configured() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY)


def send_push_to_user(user, *, kind, title, body='', url='', lead=None):
    """Send one notification to every device `user` has subscribed from.
    Always logs a NotificationEvent, success or failure, so the admin can see
    what was (or wasn't) actually delivered."""
    if not vapid_configured():
        logger.warning('VAPID keys not configured — skipping push "%s" to %s', title, user)
        NotificationEvent.objects.create(
            kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
            is_success=False, error='VAPID keys not configured',
        )
        return

    subs = list(PushSubscription.objects.filter(user=user))
    if not subs:
        NotificationEvent.objects.create(
            kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
            is_success=False, error='No push subscription for user',
        )
        return

    from pywebpush import WebPushException, webpush

    payload = json.dumps({'title': title, 'body': body, 'url': url, 'tag': kind})
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
            NotificationEvent.objects.create(
                kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
                is_success=True,
            )
        except WebPushException as exc:
            status = getattr(exc.response, 'status_code', None)
            if status in (404, 410):
                # The browser/push service says this subscription is dead — stop
                # trying it instead of failing silently forever.
                sub.delete()
            NotificationEvent.objects.create(
                kind=kind, recipient=user, title=title, body=body, url=url, lead=lead,
                is_success=False, error=str(exc),
            )
            logger.warning('Push failed for %s (%s): %s', user, kind, exc)


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
