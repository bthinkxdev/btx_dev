"""Scheduled work for monthly deliverables. Every job here is safe to run twice.

Reminders claim their step with one conditional UPDATE (``reminder_level < n``) before sending, so two overlapping
runs, a retry after a crash, or the 15-minute schedule firing repeatedly can never send the same reminder twice.
Notifications also carry a dedupe key as a second guard."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from crm.core import audit, india, notify
from crm.models import Deliverable
from crm.models.deliverables import OPEN_STATUSES

from . import services

logger = logging.getLogger('crm.deliverables')

REMIND_DAYS_BEFORE = 2
FIRST_ESCALATION_HOURS = 24
SECOND_ESCALATION_HOURS = 72
REVIEW_NUDGE_HOURS = 24
CLIENT_NUDGE_DAYS = (2, 5)


def generate() -> int:
    """Daily: create the next 30 days of deliverables for every active plan."""
    return services.generate_all()


def _target_level(d: Deliverable, now, today) -> int:
    hours = d.overdue_hours
    if hours >= SECOND_ESCALATION_HOURS:
        return 4
    if hours >= FIRST_ESCALATION_HOURS:
        return 3
    local = timezone.localtime(now)
    if today >= d.due_date and local.hour >= services.MORNING_HOUR:
        return 2
    if today >= d.due_date - timedelta(days=REMIND_DAYS_BEFORE):
        return 1
    return 0


def _who(d: Deliverable):
    return d.assignee or d.plan.account_owner


def _send(d: Deliverable, level: int) -> None:
    url = services.deliverable_url(d)
    when = f'{d.due_date:%a %d %b} {d.due_time:%I:%M %p}'.replace(' 0', ' ')
    who = _who(d)
    key = f'dl:{d.pk}:{level}:{d.due_date:%Y%m%d}'
    if level == 1:
        notify.notify([who], f'Due soon: {d.title} for {d.client}', body=f'Due {when}.', url=url,
                      event_type='deliverable_due', dedupe_key=key)
    elif level == 2:
        notify.notify([who], f'Due today: {d.title} for {d.client}', body=f'By {d.due_time:%I:%M %p}.'.replace(' 0', ' '), url=url,
                      event_type='deliverable_due', level='warning', dedupe_key=key)
    elif level == 3:
        people = [who, notify.manager_of(who) if who else None, d.plan.account_owner]
        notify.notify(people, f'Overdue 24 hours: {d.title} for {d.client}',
                      body=f'Was due {when}. {_name(who)} has not delivered it.', url=url, event_type='deliverable_overdue',
                      level='warning', email=True, dedupe_key=key)
    elif level == 4:
        people = [who, notify.manager_of(who) if who else None, d.plan.account_owner, *notify.owners()]
        notify.notify(people, f'Still not delivered after 72 hours: {d.title} for {d.client}',
                      body=f'Promised {d.original_due_date:%d %b}, was due {when}. This now counts as missed.', url=url,
                      event_type='deliverable_overdue', level='critical', email=True, dedupe_key=key)


def _name(user) -> str:
    return (user.get_full_name() or user.get_username()) if user else 'Nobody'


def reminders(now=None) -> int:
    """Every 15 minutes: due-soon, due-today, 24-hour and 72-hour reminders, plus review and client-answer nudges."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    sent = 0
    qs = (Deliverable.objects.filter(status__in=OPEN_STATUSES, plan__status='active',
                                     due_date__lte=today + timedelta(days=REMIND_DAYS_BEFORE),
                                     due_date__gte=today - timedelta(days=60))
          .select_related('plan', 'plan__account_owner', 'client', 'assignee', 'assignee__crm_profile'))
    for d in qs:
        level = _target_level(d, now, today)
        if level <= d.reminder_level:
            continue
        # claim this step; only the run that wins the update sends
        if not Deliverable.objects.filter(pk=d.pk, reminder_level__lt=level).update(reminder_level=level):
            continue
        d.reminder_level = level
        try:
            _send(d, level)
            if level == 4 and Deliverable.objects.filter(pk=d.pk, missed_at__isnull=True).update(missed_at=now):
                services._event(d, 'missed', 'Not delivered 72 hours after the due time.')
                audit.log('deliverables.missed', d, changes={'client': str(d.client), 'due': d.due_date,
                                                             'contract_date': d.original_due_date})
            sent += 1
        except Exception:
            logger.exception('Reminder for deliverable %s failed', d.pk)
    sent += _review_nudges(now)
    sent += _client_nudges(now)
    return sent


def _review_nudges(now) -> int:
    n = 0
    cutoff = now - timedelta(hours=REVIEW_NUDGE_HOURS)
    for d in Deliverable.objects.filter(status='in_review', review_requested_at__lte=cutoff, review_nudged_at__isnull=True,
                                        plan__status='active').select_related('client', 'reviewer', 'assignee', 'plan__account_owner'):
        if not Deliverable.objects.filter(pk=d.pk, review_nudged_at__isnull=True).update(review_nudged_at=now):
            continue
        targets = [d.reviewer] if d.reviewer else (notify.owners() or [d.plan.account_owner])
        notify.notify(targets, f'Waiting a day for review: {d.title} for {d.client}', body=d.review_note,
                      url=services.deliverable_url(d), event_type='deliverable_review', level='warning',
                      dedupe_key=f'dl-review-nudge:{d.pk}:{d.review_requested_at:%Y%m%d%H%M}')
        n += 1
    return n


def _client_nudges(now) -> int:
    n = 0
    for d in Deliverable.objects.filter(status='awaiting_client', client_sent_at__isnull=False, plan__status='active') \
            .select_related('client', 'assignee', 'plan__account_owner'):
        days = (now - d.client_sent_at).days
        level = sum(1 for limit in CLIENT_NUDGE_DAYS if days >= limit)
        if level <= d.client_nudge_level:
            continue
        if not Deliverable.objects.filter(pk=d.pk, client_nudge_level__lt=level).update(client_nudge_level=level):
            continue
        notify.notify([_who(d), d.plan.account_owner], f'{d.client} has not answered for {days} days: {d.title}',
                      body='Follow up with the client, or the deadline may slip.', url=services.deliverable_url(d),
                      event_type='deliverable_client_wait', level='warning', dedupe_key=f'dl-client-nudge:{d.pk}:{level}:{d.client_sent_at:%Y%m%d%H%M}')
        n += 1
    return n


def digest() -> int:
    """Daily: one summary per person of what is due today and what is already late."""
    from django.contrib.auth import get_user_model
    today = india.today()
    users = get_user_model().objects.filter(is_active=True).filter(
        crm_deliverables__status__in=OPEN_STATUSES, crm_deliverables__due_date__lte=today).distinct()
    n = 0
    for u in users:
        mine = Deliverable.objects.filter(assignee=u, status__in=OPEN_STATUSES, plan__status='active')
        due = mine.filter(due_date=today).count()
        late = mine.filter(due_date__lt=today).count()
        week = mine.filter(due_date__gt=today, due_date__lte=today + timedelta(days=7)).count()
        if not (due or late):
            continue
        parts = [f'{due} due today' if due else '', f'{late} overdue' if late else '', f'{week} more this week' if week else '']
        n += notify.notify([u], 'Your deliverables for today', body=', '.join(p for p in parts if p),
                           url=reverse('crm:deliverable_mine'), event_type='deliverable_digest',
                           level='warning' if late else 'info', dedupe_key=f'dl-digest:{today:%Y%m%d}')
    return n
