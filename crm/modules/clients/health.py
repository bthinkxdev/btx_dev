"""Customer health: a customer should never go quiet without somebody noticing.

A "live" customer is one we are working for: an active or suspended contract, an open project, an open deliverable, or an
invoice in the last 90 days. Contact counts when something real happened with them: a lead activity, a deliverable
sent, approved or delivered, a task finished, a support ticket, or a payment received. If nothing has for 30 days, the
owner and their manager are told, once a month, until it is dealt with."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from django.db.models import Max, Q
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify

QUIET_DAYS = 30
LIVE_INVOICE_DAYS = 90
DELIVERABLE_TOUCH = ['note', 'sent_to_client', 'client_approved', 'client_changes', 'delivered', 'review_requested']


def live_clients(uids=None):
    from crm.models import Client
    today = india.today()
    qs = Client.objects.filter(status='active', archived_at__isnull=True).filter(
        Q(contracts__status__in=['active', 'suspended']) | Q(projects__status__in=['planned', 'active', 'on_hold']) |
        Q(deliverables__status__in=['upcoming', 'in_progress', 'in_review', 'awaiting_client']) |
        Q(invoices__invoice_date__gte=today - timedelta(days=LIVE_INVOICE_DAYS))).distinct()
    if uids is not None:
        qs = qs.filter(owner_id__in=list(uids))
    return qs.select_related('owner')


def last_touch(client) -> datetime | None:
    """The most recent real contact with this customer, from every place one is recorded."""
    from crm.models import DeliverableEvent, LeadActivity, Payment, Task, Ticket
    stamps = [
        LeadActivity.objects.filter(lead__client=client).aggregate(m=Max('occurred_at'))['m'],
        DeliverableEvent.objects.filter(deliverable__client=client, kind__in=DELIVERABLE_TOUCH).aggregate(m=Max('created_at'))['m'],
        Task.objects.filter(project__client=client, status='done').aggregate(m=Max('completed_at'))['m'],
        Ticket.objects.filter(client=client).aggregate(m=Max('updated_at'))['m'],
    ]
    pay = Payment.objects.filter(client=client, status__in=['verified', 'partially_refunded']).aggregate(m=Max('payment_date'))['m']
    if pay:
        stamps.append(timezone.make_aware(datetime.combine(pay, time.min)))
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def quiet_days(client, now=None) -> int | None:
    """Days since the last contact, or since the customer was created if there never was one. None when not quiet enough to matter."""
    now = now or timezone.now()
    touch = last_touch(client) or client.created_at
    return max(0, (now - touch).days)


def quiet_clients(uids=None, days: int = QUIET_DAYS, now=None) -> list[tuple]:
    now = now or timezone.now()
    out = []
    for c in live_clients(uids):
        d = quiet_days(c, now)
        if d is not None and d >= days:
            out.append((c, d))
    out.sort(key=lambda t: -t[1])
    return out


def notify_quiet(now=None) -> int:
    """Job: tell the owner and their manager about customers that have gone quiet. Once a month per customer."""
    now = now or timezone.now()
    n = 0
    for client, days in quiet_clients(now=now):
        owner = client.owner if client.owner.is_active else None
        people = [owner, notify.manager_of(owner)] if owner else notify.owners()
        made = notify.notify(people, f'{client.display_name} has had no contact for {days} days',
                             body='Call or message them, deliver something, or log what is happening, so they are not forgotten.',
                             url=reverse('crm:client_detail', args=[client.pk]), event_type='client_quiet', level='warning',
                             dedupe_key=f'quiet:{client.pk}:{now:%Y%m}')
        if made:
            audit.log('client.went_quiet', client, changes={'days': days, 'owner': str(client.owner)})
            n += 1
    return n
