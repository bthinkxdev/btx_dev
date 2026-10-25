"""Scheduled jobs for payment links and the gateway (Part 6.1, 6.8, 13.4). All idempotent."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from crm.core import audit, notify
from crm.models import GatewayEvent, PaymentAttempt, PaymentLink
from crm.models.payments import PL_EXPIRED, PL_PAYABLE

from . import services as svc


def expire_links() -> int:
    """Active links past expiry become Expired (open attempts are checked with the gateway first)."""
    n = 0
    for link in PaymentLink.objects.filter(status__in=PL_PAYABLE, expires_at__lte=timezone.now()):
        for a in link.attempts.filter(status__in=['initiated', 'pending', 'authorised']):
            svc.sync_attempt(a)
        with transaction.atomic():
            l2 = PaymentLink.objects.select_for_update().get(pk=link.pk)
            if l2.status not in PL_PAYABLE or l2.expires_at > timezone.now():
                continue
            l2.status = PL_EXPIRED
            l2.expired_at = timezone.now()
            l2.last_event = 'Expired'
            l2.save(update_fields=['status', 'expired_at', 'last_event'])
            audit.log('payment_link.expired', l2, changes={'balance': str(l2.balance)})
        notify.notify([link.owner], f'Payment link {link.code} expired unpaid', body=f'{link.client} · balance ₹{link.balance:,.2f}',
                      url=svc.link_url(link), event_type='payment_link_expired', level='warning', dedupe_key=f'pl-exp:{link.pk}')
        n += 1
    return n


def abandoned_and_unpaid() -> int:
    """Initiated attempts left for 30 minutes are Abandoned; links opened but unpaid for 24h alert the owner once."""
    n = 0
    cutoff = timezone.now() - timedelta(minutes=30)
    for a in PaymentAttempt.objects.filter(status='initiated', created_at__lte=cutoff).select_related('link'):
        svc.sync_attempt(a)
        PaymentAttempt.objects.filter(pk=a.pk, status='initiated').update(status='abandoned', failure_reason='Customer did not finish')
        n += 1
    day = timezone.now() - timedelta(hours=24)
    for link in PaymentLink.objects.filter(status__in=PL_PAYABLE, first_opened_at__lte=day, abandoned_alerted_at__isnull=True,
                                           amount_paid=0):
        PaymentLink.objects.filter(pk=link.pk).update(abandoned_alerted_at=timezone.now())
        notify.notify([link.owner], f'{link.client} opened {link.code} but has not paid', url=svc.link_url(link),
                      event_type='payment_link_abandoned', dedupe_key=f'pl-aband:{link.pk}')
        n += 1
    return n


def gateway_backstop() -> int:
    """Every 15 minutes: ask the gateway about recent open attempts; once a day sweep the last 48 hours."""
    now = timezone.now()
    window = timedelta(hours=48) if now.hour == 2 and now.minute < 15 else timedelta(hours=3)
    n = 0
    qs = PaymentAttempt.objects.filter(created_at__gte=now - window).exclude(status='captured').select_related('link')
    for a in qs[:500]:
        n += len(svc.sync_attempt(a))
    return n


def reprocess_failed_events() -> int:
    n = 0
    for ev in GatewayEvent.objects.filter(status__in=['failed', 'received'], attempts__lt=10,
                                          received_at__lte=timezone.now() - timedelta(minutes=2)).order_by('id')[:200]:
        svc.process_event(ev.pk)
        n += 1
    return n
