"""
The one periodic job behind every time-based CRM push: overdue/due-today,
30-minute and 5-minute reminders for FollowUp, Activity (the Workbench
engine) and Task, SLA escalation to sales managers/admins for overdue
high-priority work, and the daily 11am check-in / 9pm sign-off nudges.

Meant to run every minute (cron / Windows Task Scheduler / Celery beat —
see crm/tasks.py for the Celery wrapper around the same `run()` this command
calls, so both scheduling paths stay in sync with one implementation).

Idempotent by design:
- Each FollowUp/Activity/Task has its own `reminder_30_sent_at`,
  `reminder_sent_at` (5-min), `overdue_notified_at` and `escalated_at`
  timestamps, so running this every minute never double-sends.
- The daily reminders are deduped by checking today's NotificationEvent log
  rather than a dedicated flag, since they aren't tied to one row.
- `crm.push.send_push_to_user` returns None (not a NotificationEvent) when a
  send is suppressed for quiet hours — every per-row flag below is only set
  when a send was actually attempted, so a quiet-hours miss is retried on
  the first run after quiet hours end rather than silently lost forever.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, FollowUp, NotificationEvent, Task
from crm.push import send_push_to_managers, send_push_to_user

WINDOW_30_MIN = timezone.timedelta(minutes=30)
WINDOW_5_MIN = timezone.timedelta(minutes=5)


def _lead_url(lead):
    return reverse('crm:lead_detail', args=[lead.pk])


def _escalation_sla():
    from django.conf import settings

    return timezone.timedelta(minutes=settings.NOTIFICATION_ESCALATION_SLA_MINUTES)


def _maybe_escalate(*, now, obj, lead, owner, overdue_since, kind_label):
    """High-priority (high_hope) work overdue beyond the SLA gets escalated
    to sales managers/admins, once, on top of the owner's own reminder."""
    if not lead.high_hope or obj.escalated_at:
        return False
    if now - overdue_since < _escalation_sla():
        return False
    sent = send_push_to_managers(
        kind=NotificationEvent.Kind.ESCALATION,
        title=f'Overdue: {lead.name}',
        body=f'{kind_label} for {lead.name} is overdue, owned by {owner.get_full_name() or owner.username}.',
        url=_lead_url(lead),
        lead=lead,
    )
    if sent is not None:
        obj.escalated_at = now
        return True
    return False


def _process_followups(now, counts):
    qs = FollowUp.objects.filter(is_done=False).select_related('lead', 'employee')

    for fu in qs.filter(datetime__lte=now, overdue_notified_at__isnull=True):
        sent = send_push_to_user(
            fu.employee, kind=NotificationEvent.Kind.FOLLOWUP_OVERDUE,
            title='Follow-up overdue',
            body=f'{fu.lead.name} — was due {timezone.localtime(fu.datetime):%d %b, %H:%M}',
            url=_lead_url(fu.lead), lead=fu.lead,
        )
        update_fields = []
        if sent is not None:
            fu.overdue_notified_at = now
            update_fields.append('overdue_notified_at')
            counts['followup_overdue'] += 1
        if _maybe_escalate(now=now, obj=fu, lead=fu.lead, owner=fu.employee, overdue_since=fu.datetime, kind_label='A follow-up'):
            update_fields.append('escalated_at')
            counts['escalated'] += 1
        if update_fields:
            fu.save(update_fields=update_fields)

    for fu in qs.filter(reminder_30_sent_at__isnull=True, datetime__gt=now, datetime__lte=now + WINDOW_30_MIN):
        sent = send_push_to_user(
            fu.employee, kind=NotificationEvent.Kind.FOLLOWUP_REMINDER_30,
            title='Follow-up in 30 minutes',
            body=f'{fu.lead.name} at {timezone.localtime(fu.datetime):%H:%M}',
            url=_lead_url(fu.lead), lead=fu.lead,
        )
        if sent is not None:
            fu.reminder_30_sent_at = now
            fu.save(update_fields=['reminder_30_sent_at'])
            counts['followup_r30'] += 1

    for fu in qs.filter(reminder_sent_at__isnull=True, datetime__gt=now, datetime__lte=now + WINDOW_5_MIN):
        sent = send_push_to_user(
            fu.employee, kind=NotificationEvent.Kind.FOLLOWUP_REMINDER_5,
            title='Follow-up in 5 minutes',
            body=f'{fu.lead.name} at {timezone.localtime(fu.datetime):%H:%M}',
            url=_lead_url(fu.lead), lead=fu.lead,
        )
        if sent is not None:
            fu.reminder_sent_at = now
            fu.save(update_fields=['reminder_sent_at'])
            counts['followup_r5'] += 1


def _process_activities(now, counts):
    qs = Activity.objects.filter(status=Activity.Status.PENDING).select_related('lead', 'owner')

    for act in qs.filter(scheduled_at__lte=now, overdue_notified_at__isnull=True):
        sent = send_push_to_user(
            act.owner, kind=NotificationEvent.Kind.ACTIVITY_OVERDUE,
            title='Activity overdue',
            body=f'{act.get_activity_type_display()} for {act.lead.name} — was due {timezone.localtime(act.scheduled_at):%d %b, %H:%M}',
            url=_lead_url(act.lead), lead=act.lead,
        )
        update_fields = []
        if sent is not None:
            act.overdue_notified_at = now
            update_fields.append('overdue_notified_at')
            counts['activity_overdue'] += 1
        if _maybe_escalate(now=now, obj=act, lead=act.lead, owner=act.owner, overdue_since=act.scheduled_at, kind_label=f'An {act.get_activity_type_display().lower()}'):
            update_fields.append('escalated_at')
            counts['escalated'] += 1
        if update_fields:
            act.save(update_fields=update_fields)

    for act in qs.filter(reminder_30_sent_at__isnull=True, scheduled_at__gt=now, scheduled_at__lte=now + WINDOW_30_MIN):
        sent = send_push_to_user(
            act.owner, kind=NotificationEvent.Kind.ACTIVITY_REMINDER_30,
            title='Activity in 30 minutes',
            body=f'{act.get_activity_type_display()} — {act.lead.name} at {timezone.localtime(act.scheduled_at):%H:%M}',
            url=_lead_url(act.lead), lead=act.lead,
        )
        if sent is not None:
            act.reminder_30_sent_at = now
            act.save(update_fields=['reminder_30_sent_at'])
            counts['activity_r30'] += 1

    for act in qs.filter(reminder_sent_at__isnull=True, scheduled_at__gt=now, scheduled_at__lte=now + WINDOW_5_MIN):
        sent = send_push_to_user(
            act.owner, kind=NotificationEvent.Kind.ACTIVITY_REMINDER_5,
            title='Activity in 5 minutes',
            body=f'{act.get_activity_type_display()} — {act.lead.name} at {timezone.localtime(act.scheduled_at):%H:%M}',
            url=_lead_url(act.lead), lead=act.lead,
        )
        if sent is not None:
            act.reminder_sent_at = now
            act.save(update_fields=['reminder_sent_at'])
            counts['activity_r5'] += 1


def _process_tasks(now, counts):
    """Task only has due_date+due_time (both optional) rather than one
    DateTimeField, so candidates are prefiltered loosely in the DB (by date)
    and checked precisely in Python via Task.due_at."""
    window_end_date = (now + WINDOW_30_MIN).date()
    candidates = (
        Task.objects.filter(
            is_completed=False, due_date__isnull=False, due_time__isnull=False,
            due_date__lte=window_end_date,
        ).select_related('lead', 'employee')
    )
    for task in candidates:
        due_at = task.due_at
        if due_at is None:
            continue

        if due_at <= now and task.overdue_notified_at is None:
            sent = send_push_to_user(
                task.employee, kind=NotificationEvent.Kind.TASK_OVERDUE,
                title='Task overdue',
                body=f'{task.title} — was due {timezone.localtime(due_at):%d %b, %H:%M}',
                url=_lead_url(task.lead), lead=task.lead,
            )
            update_fields = []
            if sent is not None:
                task.overdue_notified_at = now
                update_fields.append('overdue_notified_at')
                counts['task_overdue'] += 1
            if _maybe_escalate(now=now, obj=task, lead=task.lead, owner=task.employee, overdue_since=due_at, kind_label=f'The task "{task.title}"'):
                update_fields.append('escalated_at')
                counts['escalated'] += 1
            if update_fields:
                task.save(update_fields=update_fields)
            continue

        if now < due_at <= now + WINDOW_30_MIN and task.reminder_30_sent_at is None:
            sent = send_push_to_user(
                task.employee, kind=NotificationEvent.Kind.TASK_REMINDER_30,
                title='Task due in 30 minutes',
                body=f'{task.title} at {timezone.localtime(due_at):%H:%M}',
                url=_lead_url(task.lead), lead=task.lead,
            )
            if sent is not None:
                task.reminder_30_sent_at = now
                task.save(update_fields=['reminder_30_sent_at'])
                counts['task_r30'] += 1

        if now < due_at <= now + WINDOW_5_MIN and task.reminder_sent_at is None:
            sent = send_push_to_user(
                task.employee, kind=NotificationEvent.Kind.TASK_REMINDER_5,
                title='Task due in 5 minutes',
                body=f'{task.title} at {timezone.localtime(due_at):%H:%M}',
                url=_lead_url(task.lead), lead=task.lead,
            )
            if sent is not None:
                task.reminder_sent_at = now
                task.save(update_fields=['reminder_sent_at'])
                counts['task_r5'] += 1


def _already_sent_today(user, kind, now):
    local_today = timezone.localtime(now).date()
    return NotificationEvent.objects.filter(
        recipient=user, kind=kind, created_at__date=local_today,
    ).exists()


def _process_daily_reminders(now, counts):
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from crm.rbac import is_notification_eligible

    local_now = timezone.localtime(now)
    User = get_user_model()
    eligible_users = [u for u in User.objects.filter(is_active=True) if is_notification_eligible(u)]

    if local_now.hour == settings.NOTIFICATION_LOGIN_REMINDER_HOUR:
        today = local_now.date()
        for user in eligible_users:
            if user.last_login and timezone.localtime(user.last_login).date() >= today:
                continue  # already active today
            if _already_sent_today(user, NotificationEvent.Kind.LOGIN_REMINDER, now):
                continue
            send_push_to_user(
                user, kind=NotificationEvent.Kind.LOGIN_REMINDER,
                title="Don't forget to check in",
                body="You haven't logged into the CRM yet today.",
                url='/crm/', bypass_quiet_hours=True,
            )
            counts['login_reminder'] += 1

    if local_now.hour == settings.NOTIFICATION_LOGOUT_REMINDER_HOUR:
        today = local_now.date()
        for user in eligible_users:
            if not (user.last_login and timezone.localtime(user.last_login).date() >= today):
                continue  # wasn't active today — nothing to sign off from
            if _already_sent_today(user, NotificationEvent.Kind.LOGOUT_REMINDER, now):
                continue
            send_push_to_user(
                user, kind=NotificationEvent.Kind.LOGOUT_REMINDER,
                title='Wrapping up for the day?',
                body='Log any pending updates before you sign off.',
                url='/crm/', bypass_quiet_hours=True,
            )
            counts['logout_reminder'] += 1


def run(*, now=None, stdout=None):
    now = now or timezone.now()
    counts = {
        'followup_overdue': 0, 'followup_r30': 0, 'followup_r5': 0,
        'activity_overdue': 0, 'activity_r30': 0, 'activity_r5': 0,
        'task_overdue': 0, 'task_r30': 0, 'task_r5': 0,
        'escalated': 0, 'login_reminder': 0, 'logout_reminder': 0,
    }

    _process_followups(now, counts)
    _process_activities(now, counts)
    _process_tasks(now, counts)
    _process_daily_reminders(now, counts)

    if stdout:
        stdout.write(', '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


class Command(BaseCommand):
    help = (
        'Send overdue/due-today, 30-min/5-min reminders (FollowUp, Activity, Task), '
        'SLA escalations, and the daily 11am/9pm check-in reminders. Idempotent; run every minute.'
    )

    def handle(self, *args, **options):
        counts = run(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(f'Done — {counts}'))
