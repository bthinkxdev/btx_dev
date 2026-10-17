"""
Periodic follow-up push notifications: overdue/due-today, plus a 30-minute
and a 5-minute reminder before each scheduled follow-up. Meant to run every
minute (cron / Windows Task Scheduler / Celery beat — see crm/tasks.py for
the Celery wrapper that calls the same `run()` function this command calls,
so both paths stay in sync with one implementation).

Idempotent: each FollowUp has its own `reminder_30_sent_at`,
`reminder_sent_at` (5-min) and `overdue_notified_at` timestamps, so running
this every minute never double-sends. Safe to run with no Redis/Celery at
all — a plain OS-level cron calling `manage.py send_followup_reminders` is
enough to get real reminders working.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import FollowUp, NotificationEvent
from crm.push import send_push_to_user


def run(*, now=None, stdout=None):
    """Returns (overdue_count, reminder_30_count, reminder_5_count)."""
    now = now or timezone.now()

    def log(msg):
        if stdout:
            stdout.write(msg)

    # --- Overdue / due today: one notification the first time a pending
    # follow-up's time has passed. ---
    overdue_qs = FollowUp.objects.filter(
        is_done=False, datetime__lte=now, overdue_notified_at__isnull=True,
    ).select_related('lead', 'employee')
    overdue_count = 0
    for fu in overdue_qs:
        send_push_to_user(
            fu.employee,
            kind=NotificationEvent.Kind.FOLLOWUP_OVERDUE,
            title='Follow-up overdue',
            body=f'{fu.lead.name} — was due {timezone.localtime(fu.datetime):%d %b, %H:%M}',
            url=f'/crm/leads/{fu.lead_id}/',
            lead=fu.lead,
        )
        fu.overdue_notified_at = now
        fu.save(update_fields=['overdue_notified_at'])
        overdue_count += 1
    log(f'Overdue notified: {overdue_count}')

    # --- 30-minute-before reminder. ---
    window_30_start = now
    window_30_end = now + timezone.timedelta(minutes=30)
    r30_qs = FollowUp.objects.filter(
        is_done=False,
        reminder_30_sent_at__isnull=True,
        datetime__gt=window_30_start,
        datetime__lte=window_30_end,
    ).select_related('lead', 'employee')
    r30_count = 0
    for fu in r30_qs:
        send_push_to_user(
            fu.employee,
            kind=NotificationEvent.Kind.FOLLOWUP_REMINDER_30,
            title='Follow-up in 30 minutes',
            body=f'{fu.lead.name} at {timezone.localtime(fu.datetime):%H:%M}',
            url=f'/crm/leads/{fu.lead_id}/',
            lead=fu.lead,
        )
        fu.reminder_30_sent_at = now
        fu.save(update_fields=['reminder_30_sent_at'])
        r30_count += 1
    log(f'30-min reminders: {r30_count}')

    # --- 5-minute-before reminder. ---
    window_5_end = now + timezone.timedelta(minutes=5)
    r5_qs = FollowUp.objects.filter(
        is_done=False,
        reminder_sent_at__isnull=True,
        datetime__gt=now,
        datetime__lte=window_5_end,
    ).select_related('lead', 'employee')
    r5_count = 0
    for fu in r5_qs:
        send_push_to_user(
            fu.employee,
            kind=NotificationEvent.Kind.FOLLOWUP_REMINDER_5,
            title='Follow-up in 5 minutes',
            body=f'{fu.lead.name} at {timezone.localtime(fu.datetime):%H:%M}',
            url=f'/crm/leads/{fu.lead_id}/',
            lead=fu.lead,
        )
        fu.reminder_sent_at = now
        fu.save(update_fields=['reminder_sent_at'])
        r5_count += 1
    log(f'5-min reminders: {r5_count}')

    return overdue_count, r30_count, r5_count


class Command(BaseCommand):
    help = 'Send overdue/due-today and 30-min/5-min follow-up push reminders (idempotent; run every minute).'

    def handle(self, *args, **options):
        overdue, r30, r5 = run(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(
            f'Done — overdue: {overdue}, 30-min: {r30}, 5-min: {r5}'
        ))
