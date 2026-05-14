"""Expiry / renewal tracking, reminder emails, and expiry enforcement."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditEntry,
    Project,
    ProjectCredential,
    RenewalReminderLog,
    RenewalTracker,
)
from . import audit as audit_service


def get_expiring_renewals(*, within_days: int = 30):
    now = timezone.now()
    end = now + timedelta(days=within_days)
    return (
        RenewalTracker.objects.filter(
            status=RenewalTracker.Status.ACTIVE,
            expires_at__gte=now,
            expires_at__lte=end,
        )
        .select_related('project', 'project__client')
        .order_by('expires_at')
    )


def get_expired_renewals():
    now = timezone.now()
    return (
        RenewalTracker.objects.filter(
            status=RenewalTracker.Status.ACTIVE,
            expires_at__lt=now,
        )
        .select_related('project', 'project__client')
        .order_by('expires_at')
    )


def credentials_expiring_soon(*, within_days: int = 30):
    now = timezone.now()
    end = now + timedelta(days=within_days)
    return ProjectCredential.objects.filter(
        expires_at__isnull=False,
        expires_at__gte=now,
        expires_at__lte=end,
    ).select_related('project', 'project__client')


def mark_internal_notified(tracker: RenewalTracker) -> None:
    tracker.notified_internal_at = timezone.now()
    tracker.save(update_fields=['notified_internal_at', 'updated_at'])


def mark_client_notified(tracker: RenewalTracker) -> None:
    tracker.notified_client_at = timezone.now()
    tracker.save(update_fields=['notified_client_at', 'updated_at'])


def _local_today() -> date:
    return timezone.localtime(timezone.now()).date()


def _expiry_local_date(renewal: RenewalTracker) -> date:
    return timezone.localtime(renewal.expires_at).date()


def _start_of_local_day(d: date):
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


def send_renewal_reminder(
    renewal: RenewalTracker,
    reminder_type: str,
    *,
    recipient_email: str,
) -> bool:
    """
    Sends a renewal reminder email using Django's send_mail.
    Idempotent: if a log entry already exists for renewal + reminder_type,
    does nothing and returns False.
    Creates RenewalReminderLog on send attempt (success or failure).
    Returns True if mail was sent successfully, False if skipped or failed.
    """
    if RenewalReminderLog.objects.filter(
        renewal=renewal, reminder_type=reminder_type
    ).exists():
        return False
    recipient_email = (recipient_email or '').strip()
    if not recipient_email:
        return False

    subject = f'Renewal reminder ({reminder_type}) — {renewal.title}'
    body = (
        f'Hello,\n\n'
        f'This is a reminder regarding: {renewal.title}\n'
        f'Project: {renewal.project.client.business_name}\n'
        f'Expiry (local): {_expiry_local_date(renewal)}\n'
        f'Reminder type: {reminder_type}\n\n'
        f'If you have questions, reply to this email.\n'
    )
    from_addr = getattr(settings, 'RENEWAL_FROM_EMAIL', settings.DEFAULT_FROM_EMAIL)
    ok = False
    err = ''
    try:
        send_mail(
            subject,
            body,
            from_addr,
            [recipient_email.strip()],
            fail_silently=False,
        )
        ok = True
    except Exception as exc:
        err = str(exc)[:5000]

    RenewalReminderLog.objects.create(
        renewal=renewal,
        reminder_type=reminder_type,
        sent_to=recipient_email,
        success=ok,
        error_message=err,
    )
    return ok


def get_renewals_due_for_reminder() -> dict[str, list[RenewalTracker]]:
    """
    Renewals grouped by reminder type that need action today.
    Uses calendar dates in the project timezone.
    """
    today = _local_today()
    buckets: dict[str, list[RenewalTracker]] = {
        RenewalReminderLog.ReminderType.DAYS_30: [],
        RenewalReminderLog.ReminderType.DAYS_7: [],
        RenewalReminderLog.ReminderType.DAYS_1: [],
        RenewalReminderLog.ReminderType.EXPIRED: [],
        RenewalReminderLog.ReminderType.INTERNAL: [],
    }

    active = RenewalTracker.objects.filter(
        status=RenewalTracker.Status.ACTIVE,
    ).select_related('project', 'project__client')

    def has_log(r: RenewalTracker, rtype: str) -> bool:
        return RenewalReminderLog.objects.filter(
            renewal=r, reminder_type=rtype
        ).exists()

    for r in active:
        exp = _expiry_local_date(r)
        if exp == today + timedelta(days=30) and not has_log(
            r, RenewalReminderLog.ReminderType.DAYS_30
        ):
            buckets[RenewalReminderLog.ReminderType.DAYS_30].append(r)
        if exp == today + timedelta(days=7) and not has_log(
            r, RenewalReminderLog.ReminderType.DAYS_7
        ):
            buckets[RenewalReminderLog.ReminderType.DAYS_7].append(r)
        if exp == today + timedelta(days=1) and not has_log(
            r, RenewalReminderLog.ReminderType.DAYS_1
        ):
            buckets[RenewalReminderLog.ReminderType.DAYS_1].append(r)
        if exp < today and not has_log(r, RenewalReminderLog.ReminderType.EXPIRED):
            buckets[RenewalReminderLog.ReminderType.EXPIRED].append(r)
        if exp <= today + timedelta(days=7) and not has_log(
            r, RenewalReminderLog.ReminderType.INTERNAL
        ):
            buckets[RenewalReminderLog.ReminderType.INTERNAL].append(r)

    return buckets


@transaction.atomic
def process_expired_renewals() -> int:
    """
    Marks renewals expired when expiry date is before today (local),
    and suspends projects unless already completed or suspended.
    """
    today = _local_today()
    start_today = _start_of_local_day(today)
    qs = (
        RenewalTracker.objects.select_for_update()
        .filter(expires_at__lt=start_today)
        .exclude(status=RenewalTracker.Status.EXPIRED)
    )
    count = 0
    for r in qs:
        r.status = RenewalTracker.Status.EXPIRED
        r.save(update_fields=['status', 'updated_at'])
        count += 1
        proj = Project.objects.select_for_update().get(pk=r.project_id)
        audit_service.log_event(
            category=AuditEntry.EventCategory.RENEWAL,
            action='expired',
            object_type='RenewalTracker',
            object_id=r.pk,
            object_repr=(r.title or '')[:200],
            actor=None,
            project=proj,
            after_state={'renewal_status': RenewalTracker.Status.EXPIRED},
        )
        if proj.status not in (
            Project.Status.COMPLETED,
            Project.Status.SUSPENDED,
        ):
            proj.status = Project.Status.SUSPENDED
            proj.save(update_fields=['status', 'updated_at'])
    return count
