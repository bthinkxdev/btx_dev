"""Quotation timers (Part 5.8): expiry, 3-day pre-expiry task, accepted-but-unpaid reminder."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from crm.core import audit, india, notify
from crm.models import FollowUp, Quotation

from .services import q_url


def expire_and_remind() -> int:
    today = india.today()
    n = 0
    for q in Quotation.objects.filter(status__in=['sent', 'viewed', 'approved'], valid_until__lt=today):
        with transaction.atomic():
            locked = Quotation.objects.select_for_update().get(pk=q.pk)
            if locked.status not in ('sent', 'viewed', 'approved'):
                continue
            locked.status = 'expired'
            locked.expired_at = timezone.now()
            locked.save(update_fields=['status', 'expired_at'])
            audit.log('quotation.expired', locked, actor_type='job')
        if q.lead_id:
            FollowUp.objects.create(lead_id=q.lead_id, kind='call', due_at=india.next_working_day_at(),
                                    assignee=q.lead.owner or q.owner,
                                    purpose=f'{q.display_number} expired: extend with a new version or close')
        notify.notify([q.owner], f'Quotation {q.display_number} expired', body='Create a new version or record the outcome.',
                      url=q_url(q), event_type='quotation_expired', level='warning', dedupe_key=f'qexp:{q.pk}')
        n += 1
    soon = today + timedelta(days=3)
    for q in Quotation.objects.filter(status__in=['sent', 'viewed'], valid_until__lte=soon, valid_until__gte=today,
                                      reminder_sent_at__isnull=True):
        if q.lead_id:
            FollowUp.objects.create(lead_id=q.lead_id, kind='call', due_at=india.next_working_day_at(),
                                    assignee=q.lead.owner or q.owner, purpose=f'{q.display_number} expires {q.valid_until:%d-%b}')
        notify.notify([q.owner], f'{q.display_number} expires on {q.valid_until:%d-%b-%Y}', url=q_url(q),
                      event_type='quotation_expiring', level='warning')
        Quotation.objects.filter(pk=q.pk).update(reminder_sent_at=timezone.now())
        n += 1
    # Accepted but no payment in 3 days -> owner and finance.
    cutoff = timezone.now() - timedelta(days=3)
    for q in Quotation.objects.filter(status='accepted', accepted_at__lt=cutoff):
        if q.payments.filter(status__in=['verified', 'partially_refunded']).exists():
            continue
        notify.notify([q.owner] + notify.users_with_perm('payments', 'approve'),
                      f'Accepted 3+ days ago, no payment yet: {q.display_number}', url=q_url(q),
                      event_type='accepted_unpaid', level='warning', dedupe_key=f'acc-unpaid:{q.pk}')
        n += 1
    return n
