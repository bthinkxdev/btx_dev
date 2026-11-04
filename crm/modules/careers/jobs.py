"""Hiring timers: remind the candidate and the interviewers about interviews in the next 24 hours.
Idempotent: reminder_sent_at stops repeats."""
from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from crm.core import notify
from pages.models import Interview

from . import mail


def interview_reminders() -> int:
    now = timezone.now()
    due = (Interview.objects
           .filter(status=Interview.STATUS_SCHEDULED, scheduled_at__gt=now, scheduled_at__lte=now + timedelta(hours=24),
                   reminder_sent_at__isnull=True)
           .select_related('application', 'application__job', 'created_by').prefetch_related('interviewers'))
    sent = 0
    for iv in due:
        # Skip the candidate email when HR chose not to email them, or the invitation went out only moments ago.
        if iv.candidate_notified_at and iv.candidate_notified_at <= now - timedelta(hours=3):
            mail.send_interview_reminder(iv)
        people = {u.pk: u for u in iv.interviewers.all()}
        if iv.created_by_id and iv.created_by:
            people[iv.created_by_id] = iv.created_by
        when = timezone.localtime(iv.scheduled_at).strftime('%I:%M %p').lstrip('0')
        notify.notify(people.values(), f'Interview soon: {iv.application.full_name}',
                      body=f'{iv.round_name} at {when} ({iv.get_mode_display()})',
                      url=reverse('crm:career_application_detail', args=[iv.application_id]),
                      event_type='career_interview', dedupe_key=f'career-iv-reminder:{iv.pk}:{iv.sequence}')
        Interview.objects.filter(pk=iv.pk).update(reminder_sent_at=now)
        sent += 1
    return sent
