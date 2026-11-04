"""Hiring workflow: move a candidate through the pipeline and schedule, change, cancel and close interviews.

Views stay thin; every rule lives here so it can be tested without a browser. Candidate emails are sent through
crm.modules.careers.mail and are only ever sent when HR asks for them."""
from __future__ import annotations

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.core import audit, notify
from pages.models import Interview, JobApplication

from . import mail

OPEN_FOR_INTERVIEW = ('new', 'reviewing', 'shortlisted')
TIME_FIELDS = {'scheduled_at', 'duration_minutes', 'mode', 'meeting_link', 'location'}


def _application_url(app) -> str:
    return reverse('crm:career_application_detail', args=[app.pk])


def apply_action(app: JobApplication, action: str, *, actor, send_email: bool = False, note: str = '') -> dict:
    """review / shortlist / reject / hire. Returns {'status': ..., 'emailed': bool}."""
    target = {'review': 'reviewing', 'shortlist': 'shortlisted', 'reject': 'rejected', 'hire': 'hired'}[action]
    old = app.status
    app.status = target
    app.save(update_fields=['status'])
    emailed = False
    if send_email and action == 'shortlist':
        emailed = mail.send_shortlisted(app, note, actor=actor)
    elif send_email and action == 'reject':
        emailed = mail.send_rejected(app, note, actor=actor)
    if action == 'reject':
        # an interview still on the calendar for a rejected candidate would be a mistake: close it
        for iv in app.interviews.filter(status=Interview.STATUS_SCHEDULED):
            iv.status = 'cancelled'
            iv.sequence += 1
            iv.save(update_fields=['status', 'sequence', 'updated_at'])
    audit.log(f'career.application_{action}', app, actor=actor,
              changes={'from': old, 'to': target, 'emailed': emailed})
    return {'status': target, 'emailed': emailed}


@transaction.atomic
def save_interview(form, application: JobApplication, *, actor) -> Interview:
    """Create or update an interview from a valid InterviewForm and email the candidate when asked."""
    is_new = form.instance.pk is None
    time_changed = bool(TIME_FIELDS & set(form.changed_data))
    iv = form.save(commit=False)
    iv.application = application
    if is_new:
        iv.created_by = actor
    elif time_changed:
        iv.sequence += 1                      # calendars treat a higher sequence as an update to the same event
    iv.save()
    form.save_m2m()
    if application.status in OPEN_FOR_INTERVIEW:
        application.status = 'interview'
        application.save(update_fields=['status'])
    audit.log('career.interview_scheduled' if is_new else 'career.interview_updated', application, actor=actor,
              changes={'round': iv.round_name, 'at': iv.scheduled_at.isoformat(), 'mode': iv.mode})

    emailed = False
    wants_email = form.cleaned_data.get('notify_candidate')
    if wants_email and (is_new or time_changed or not iv.candidate_notified_at):
        emailed = mail.send_interview_invite(iv, updated=not is_new and bool(iv.candidate_notified_at), actor=actor)
    iv._emailed = emailed

    when = timezone.localtime(iv.scheduled_at).strftime('%d %b, %I:%M %p').replace(' 0', ' ')
    recipients = [u for u in iv.interviewers.all() if u != actor]
    if recipients:
        notify.notify(recipients, f'Interview {"scheduled" if is_new else "updated"}: {application.full_name}',
                      body=f'{iv.round_name} on {when} ({iv.get_mode_display()})', url=_application_url(application),
                      event_type='career_interview', dedupe_key=f'career-iv:{iv.pk}:{iv.sequence}')
    return iv


@transaction.atomic
def close_interview(iv: Interview, action: str, *, actor, note: str = '', send_email: bool = False) -> bool:
    """complete / no_show / cancel. Returns True when the candidate was emailed (cancel only)."""
    emailed = False
    if action == 'cancel':
        iv.status = 'cancelled'
        iv.sequence += 1
        iv.save(update_fields=['status', 'sequence', 'updated_at'])
        if send_email and iv.candidate_notified_at:
            emailed = mail.send_interview_cancelled(iv, note, actor=actor)
        app = iv.application
        still_planned = app.interviews.exclude(pk=iv.pk).filter(status__in=['scheduled', 'completed']).exists()
        if app.status == 'interview' and not still_planned:
            app.status = 'shortlisted'
            app.save(update_fields=['status'])
    else:
        iv.status = 'completed' if action == 'complete' else 'no_show'
        if note:
            iv.feedback = (iv.feedback + '\n\n' if iv.feedback else '') + note
        iv.save(update_fields=['status', 'feedback', 'updated_at'])
    audit.log(f'career.interview_{action}', iv.application, actor=actor, changes={'round': iv.round_name})
    return emailed
