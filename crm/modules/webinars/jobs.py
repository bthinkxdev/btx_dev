"""Webinar timers: reminders to people who paid, and a safety net for payments the browser never reported."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from pages import webinar, webinar_mail
from pages.models import Webinar, WebinarRegistration


def reminders() -> int:
    """24 hours and 1 hour before the start, once each, to every paid registrant."""
    now = timezone.now()
    sent = 0
    for w in Webinar.objects.filter(is_active=True, starts_at__gt=now, starts_at__lte=now + timedelta(hours=25)):
        left = w.starts_at - now
        for kind, field, window in (('24h', 'reminder_24h_sent_at', timedelta(hours=24)),
                                    ('1h', 'reminder_1h_sent_at', timedelta(hours=1, minutes=15))):
            if left > window:
                continue
            if kind == '24h' and left <= timedelta(hours=3):
                continue      # too close for a "tomorrow" reminder; the 1-hour one covers it
            for reg in w.registrations.filter(status='paid', **{f'{field}__isnull': True}).select_related('webinar'):
                if webinar_mail.send_reminder(reg, kind):
                    WebinarRegistration.objects.filter(pk=reg.pk).update(**{field: now})
                    sent += 1
    return sent


def reconcile_pending() -> int:
    """Ask Cashfree about checkouts that are still pending after a few minutes: the visitor may have paid and closed
    the tab before the return page loaded, and the webhook may not have reached us. Returns how many became paid."""
    now = timezone.now()
    paid = 0
    for reg in WebinarRegistration.objects.filter(status='pending', created_at__lte=now - timedelta(minutes=4),
                                                  created_at__gte=now - timedelta(days=2)).select_related('webinar')[:60]:
        if webinar.reconcile(reg) == 'paid':
            paid += 1
    return paid


def meta_retry() -> int:
    """Send Meta Conversions API events that failed the first time (network or Meta errors). Returns how many went."""
    from pages import meta
    return meta.retry_pending()
