"""In-app notifications and email delivery with a delivery log (Part 13.3)."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.utils import timezone

logger = logging.getLogger('crm.notify')

CRITICAL_EVENTS = {'sla_breach', 'payment_failed', 'approval_requested', 'unassigned_lead', 'gateway_review'}


def notify(users: Iterable, title: str, *, body: str = '', url: str = '', event_type: str = 'info',
           level: str = 'info', dedupe_key: str = '', email: bool = False) -> int:
    """Create in-app notifications; optionally email. Returns number created. Never raises."""
    from crm.models import Notification
    created = 0
    seen = set()
    for user in users:
        if user is None or not getattr(user, 'is_active', False) or user.pk in seen:
            continue
        seen.add(user.pk)
        try:
            with transaction.atomic():
                Notification.objects.create(user=user, title=title[:200], body=body[:500], url=url[:300],
                                            event_type=event_type, level=level, dedupe_key=dedupe_key[:120])
            created += 1
        except IntegrityError:
            continue  # same dedupe key already notified
        except Exception:  # pragma: no cover
            logger.exception('notification failed')
            continue
        profile = getattr(user, 'crm_profile', None)
        link = (settings.SITE_BASE_URL + url) if url.startswith('/') else url
        if email and user.email:
            if profile is None or profile.notify_email or event_type in CRITICAL_EVENTS:
                send_email(user.email, title, f'{body}\n\n{link}'.strip(), template_code=f'notify:{event_type}')
        if profile is None or profile.notify_push or event_type in CRITICAL_EVENTS:
            send_push(user, title, body=body, url=url, tag=event_type)
    return created


def send_push(user, title: str, *, body: str = '', url: str = '', tag: str = '') -> None:
    """Deliver a Web Push notification to every device the user has enabled (PWA).

    Fire-and-forget, off the request path, same pattern as email dispatch — a slow or
    unreachable push service must never add latency to the request that triggered it.
    Never raises; a dead subscription (the browser/OS dropped it) is cleaned up quietly.
    """
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        return  # push not configured in this environment (e.g. a fresh local checkout)
    from crm.models import PushSubscription
    subs = list(PushSubscription.objects.filter(user=user))
    if not subs:
        return
    import threading

    def run():
        from django.db import connection
        try:
            _deliver_push(subs, title, body, url, tag)
        finally:
            connection.close()
    threading.Thread(target=run, daemon=True, name=f'crm-push-{user.pk}').start()


def _deliver_push(subs, title: str, body: str, url: str, tag: str) -> None:
    import json

    from pywebpush import WebPushException, webpush

    from crm.models import PushSubscription
    link = (settings.SITE_BASE_URL + url) if url.startswith('/') else url
    payload = json.dumps({'title': title[:150], 'body': body[:300], 'url': link or settings.SITE_BASE_URL,
                          'tag': tag or 'crm'})
    for sub in subs:
        subscription_info = {'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}}
        try:
            webpush(subscription_info=subscription_info, data=payload,
                   vapid_private_key=settings.VAPID_PRIVATE_KEY,
                   vapid_claims={'sub': settings.VAPID_CLAIM_EMAIL}, ttl=259200)
        except WebPushException as exc:
            status = getattr(exc.response, 'status_code', None)
            if status in (404, 410):  # the browser/OS unsubscribed this endpoint
                PushSubscription.objects.filter(pk=sub.pk).delete()
            else:
                logger.warning('Push to subscription %s failed: %s', sub.pk, exc)
        except Exception:  # pragma: no cover
            logger.exception('Push delivery failed')


def render(template_text: str, context: dict) -> str:
    return Template(template_text).render(Context(context, autoescape=False))


def send_email(to: str, subject: str, body: str, *, cc: list[str] | None = None, attachments=None,
               sent_by=None, obj=None, template_code: str = '', html: str = '') -> 'object':
    """Send after commit and record a DeliveryLog row. Returns the log row."""
    from crm.models import DeliveryLog
    log = DeliveryLog.objects.create(
        channel='email', recipient=to[:254], cc=', '.join(cc or [])[:500], subject=subject[:250],
        template_code=template_code[:60], status='queued', sent_by=sent_by,
        object_type=obj._meta.model_name if obj is not None else '', object_id=str(obj.pk) if obj is not None else '',
        body=body[:20000], attachment_name=', '.join(a[0] for a in (attachments or []))[:200],
    )

    def _send():
        dispatch(log.pk, attachments=attachments, html=html)

    transaction.on_commit(_send)
    return log


def dispatch(log_id: int, attachments=None, html: str = ''):
    """Deliver off the request path: Celery if configured, else a daemon thread; sync when CRM_SYNC_EMAIL."""
    if getattr(settings, 'CRM_SYNC_EMAIL', False):
        deliver(log_id, attachments=attachments, html=html)
        return
    if getattr(settings, 'CELERY_BROKER_URL', ''):
        try:
            from crm.tasks import deliver_email
            if not attachments:
                deliver_email.delay(log_id)
                return
        except Exception:  # broker down: fall back to a thread
            logger.warning('Celery unavailable, sending email %s in a thread', log_id)
    import threading

    def run():
        from django.db import connection
        try:
            deliver(log_id, attachments=attachments, html=html)
        finally:
            connection.close()
    threading.Thread(target=run, daemon=True, name=f'crm-email-{log_id}').start()


def retry_failed_emails(limit: int = 50) -> int:
    """Job: retry queued/failed messages up to 3 attempts (attachments are regenerated by their senders)."""
    from crm.models import DeliveryLog
    n = 0
    for log in DeliveryLog.objects.filter(status__in=['queued', 'failed'], attempts__lt=3, attachment_name='',
                                          created_at__gte=timezone.now() - timedelta(days=2))[:limit]:
        deliver(log.pk)
        n += 1
    return n


def deliver(log_id: int, attachments=None, html: str = ''):
    from crm.models import DeliveryLog
    log = DeliveryLog.objects.get(pk=log_id)
    log.attempts += 1
    try:
        msg = EmailMessage(subject=log.subject, body=log.body, from_email=settings.DEFAULT_FROM_EMAIL,
                           to=[log.recipient], cc=[c.strip() for c in log.cc.split(',') if c.strip()])
        for name, content, mime in (attachments or []):
            msg.attach(name, content, mime)
        if html:
            msg.content_subtype = 'plain'
        msg.send(fail_silently=False)
        log.status = 'sent'
        log.error = ''
    except Exception as exc:  # network / SMTP failure is recorded, never raised to the user
        log.status = 'failed'
        log.error = str(exc)[:500]
        logger.warning('Email to %s failed: %s', log.recipient, exc)
    log.save(update_fields=['status', 'error', 'attempts'])
    return log


def users_with_perm(module: str, action: str):
    """Active users holding a permission (for routing alerts and approvals)."""
    from django.contrib.auth import get_user_model
    from . import permissions
    User = get_user_model()
    out = []
    for u in User.objects.filter(is_active=True).select_related('crm_profile').prefetch_related('crm_profile__roles'):
        if permissions.has_perm(u, module, action):
            out.append(u)
    return out


def owners():
    from django.contrib.auth import get_user_model
    from . import permissions
    User = get_user_model()
    return [u for u in User.objects.filter(is_active=True) if u.is_superuser or permissions.has_role(u, 'owner')]


def manager_of(user):
    profile = getattr(user, 'crm_profile', None)
    if profile is None:
        return None
    if profile.manager_id:
        return profile.manager
    if profile.team_id and profile.team.manager_id and profile.team.manager_id != user.pk:
        return profile.team.manager
    return None


def mark_all_read(user):
    from crm.models import Notification
    return Notification.objects.filter(user=user, read_at__isnull=True).update(read_at=timezone.now())
