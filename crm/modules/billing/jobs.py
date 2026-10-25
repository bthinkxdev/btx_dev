"""Billing jobs: dunning and TDS certificate reminders (Part 7.6, 7.7). Idempotent per step."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from crm.core import india, notify
from crm.models import Invoice, TdsReceivable
from crm.models.billing import INV_OPEN

from . import services as svc


def dunning() -> int:
    """Send the next due reminder step once per invoice; stops when paid, disputed, paused or promised."""
    today = india.today()
    order = [s for s, _ in svc.REMINDER_STEPS]
    sent = 0
    for inv in Invoice.objects.filter(status__in=INV_OPEN, balance__gt=0).select_related('client'):
        due_steps = [s for s, offset in svc.REMINDER_STEPS if today >= inv.due_date + timedelta(days=offset)]
        if not due_steps:
            continue
        step = due_steps[-1]
        last = inv.last_reminder_step
        if last in order and order.index(last) >= order.index(step):
            continue
        if inv.promise_date and inv.promise_date < today and not inv.disputed:
            notify.notify([inv.owner], f'Promise to pay broken on {inv.number}', body=f'Promised {inv.promise_date:%d-%b-%Y}',
                          url=svc.inv_url(inv), event_type='promise_broken', level='warning',
                          dedupe_key=f'promise-broken:{inv.pk}:{inv.promise_date}')
            Invoice.objects.filter(pk=inv.pk).update(promise_date=None)
            inv.promise_date = None
        if svc.send_reminder(inv, step):
            sent += 1
    return sent


def tds_certificate_reminders() -> int:
    """Monthly: Finance task for TDS entries older than 90 days without a Form 16A."""
    cutoff = timezone.now() - timedelta(days=90)
    month_ago = timezone.now() - timedelta(days=28)
    qs = TdsReceivable.objects.filter(status='expected', created_at__lte=cutoff).exclude(reminded_at__gte=month_ago)
    n = qs.count()
    if n:
        from crm.modules.payments.services import finance_users
        from django.urls import reverse
        notify.notify(finance_users(), f'{n} TDS certificate(s) overdue', body='Follow up with the customers for Form 16A.',
                      url=reverse('crm:tds_receivable') + '?quick=old', event_type='tds_reminder', level='warning',
                      dedupe_key=f'tds-remind:{india.today():%Y-%m}')
        qs.update(reminded_at=timezone.now())
    return n
