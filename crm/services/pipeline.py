"""
Activity-based lead engine: every Activity that gets completed must either
produce a next Activity or move the Lead to a terminal lifecycle_state
(WON / LOST) or NURTURE with a future review date. A Lead should never be
left ACTIVE with next_activity=None except transiently, while it's waiting
to be picked up in the "No Next Action" queue.

This module is additive and does not touch the existing FollowUp/Task/
ActivityLog models or views — it is a second, parallel layer.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import Activity, Cadence, Lead
from ..utils import log_activity

# Outcomes that require the caller to supply the next step explicitly
# (the "ask next action" flow from the spec) rather than being decided
# automatically.
MANUAL_NEXT_ACTION_OUTCOMES = {
    Activity.Outcome.INTERESTED,
    Activity.Outcome.FOLLOW_UP_REQUIRED,
    Activity.Outcome.DEMO_SCHEDULED,
    Activity.Outcome.PROPOSAL_REQUESTED,
    Activity.Outcome.NOT_INTERESTED,
}
AUTOMATIC_RETRY_OUTCOMES = {Activity.Outcome.NO_ANSWER}
TERMINAL_OUTCOMES = {Activity.Outcome.WON, Activity.Outcome.LOST}


class PipelineError(Exception):
    """Raised when an outcome/transition can't be applied as requested."""


def start_lead_activity(lead, *, activity_type=Activity.Type.CALL, scheduled_at=None, user=None):
    """Fresh-lead protection: give a brand-new (or reactivated) lead an
    immediate pending activity so it never sits with no next action."""
    activity = Activity.objects.create(
        lead=lead,
        owner=lead.employee,
        activity_type=activity_type,
        scheduled_at=scheduled_at or timezone.now(),
        source=Activity.Source.AUTOMATIC,
        created_by=user,
    )
    Lead.objects.filter(pk=lead.pk).update(next_activity=activity)
    lead.next_activity = activity
    return activity


def _cancel_other_pending(lead, keep=None):
    qs = Activity.objects.filter(lead=lead, status=Activity.Status.PENDING)
    if keep is not None:
        qs = qs.exclude(pk=keep.pk)
    qs.update(status=Activity.Status.CANCELLED)


def _default_cadence():
    return Cadence.objects.filter(is_default=True).order_by('id').first()


def _next_retry_activity(activity, user):
    """NO_ANSWER: create the next step from the lead's default cadence,
    falling back to +1 day same activity type if no cadence is configured."""
    cadence = _default_cadence()
    delay_days = 1
    next_type = activity.activity_type
    if cadence:
        steps = list(cadence.steps.all())
        current_offsets = [s.day_offset for s in steps]
        later = [s for s in steps if s.day_offset > 0]
        if later:
            step = min(later, key=lambda s: s.day_offset)
            delay_days = step.day_offset
            next_type = step.activity_type
    scheduled_at = activity.scheduled_at + timezone.timedelta(days=delay_days)
    return Activity.objects.create(
        lead=activity.lead,
        owner=activity.owner,
        activity_type=next_type,
        scheduled_at=scheduled_at,
        source=Activity.Source.AUTOMATIC,
        created_by=user,
    )


@transaction.atomic
def complete_activity(
    activity,
    *,
    outcome,
    user,
    notes='',
    next_type=None,
    next_scheduled_at=None,
    lost_reason='',
    lost_note='',
    nurture_reason='',
    nurture_next_review=None,
):
    """
    Completes `activity` with `outcome`, then either creates the next
    Activity or moves the lead to a terminal/nurture state. Returns
    (next_activity_or_None, lead).
    """
    if outcome not in Activity.Outcome.values:
        raise PipelineError(f'Unknown outcome: {outcome}')

    activity.status = Activity.Status.COMPLETED
    activity.outcome = outcome
    activity.completed_at = timezone.now()
    activity.completed_by = user
    if notes:
        activity.notes = notes
    activity.save(update_fields=['status', 'outcome', 'completed_at', 'completed_by', 'notes', 'updated_at'])

    lead = activity.lead
    next_activity = None

    if outcome == Activity.Outcome.WON:
        next_activity = None
        mark_won(lead, user=user)
    elif outcome == Activity.Outcome.LOST:
        if not lost_reason:
            raise PipelineError('Lost requires a reason.')
        mark_lost(lead, reason=lost_reason, note=lost_note, user=user)
    elif outcome in AUTOMATIC_RETRY_OUTCOMES:
        next_activity = _next_retry_activity(activity, user)
    elif outcome in MANUAL_NEXT_ACTION_OUTCOMES:
        if not next_scheduled_at:
            raise PipelineError('This outcome needs a next action date/time.')
        next_activity = Activity.objects.create(
            lead=lead,
            owner=activity.owner,
            activity_type=next_type or Activity.Type.CALL,
            scheduled_at=next_scheduled_at,
            source=Activity.Source.MANUAL,
            created_by=user,
        )
    else:
        raise PipelineError(f'Outcome {outcome} has no dispatch rule.')

    if next_activity is not None:
        Lead.objects.filter(pk=lead.pk).update(next_activity=next_activity)
        lead.refresh_from_db(fields=['next_activity'])

    log_activity(lead, 'pipeline_outcome', f'{activity.get_activity_type_display()} → {activity.get_outcome_display()}')
    return next_activity, lead


def mark_won(lead, *, user=None):
    if lead.deal_value <= 0:
        raise PipelineError('Won requires a deal value greater than zero.')
    lead.lifecycle_state = Lead.LifecycleState.WON
    lead.status = Lead.Status.WON
    lead.won_at = timezone.now()
    lead.next_activity = None
    lead.save(update_fields=['lifecycle_state', 'status', 'won_at', 'next_activity', 'updated_at'])
    _cancel_other_pending(lead)
    log_activity(lead, 'lead_won', '')
    return lead


def mark_lost(lead, *, reason, note='', user=None):
    if not reason:
        raise PipelineError('Lost requires a reason.')
    lead.lifecycle_state = Lead.LifecycleState.LOST
    lead.lost_reason = reason
    lead.lost_note = note
    lead.lost_at = timezone.now()
    lead.next_activity = None
    lead.save(update_fields=[
        'lifecycle_state', 'lost_reason', 'lost_note', 'lost_at', 'next_activity', 'updated_at',
    ])
    _cancel_other_pending(lead)
    log_activity(lead, 'lead_lost', reason)
    return lead


def move_to_nurture(lead, *, reason, next_review, user=None):
    if not reason or not next_review:
        raise PipelineError('Nurture requires a reason and a next-review date.')
    lead.lifecycle_state = Lead.LifecycleState.NURTURE
    lead.nurture_reason = reason
    lead.nurture_next_review = next_review
    lead.next_activity = None
    lead.save(update_fields=[
        'lifecycle_state', 'nurture_reason', 'nurture_next_review', 'next_activity', 'updated_at',
    ])
    _cancel_other_pending(lead)
    log_activity(lead, 'lead_nurture', reason)
    return lead


def wake_from_nurture(lead, *, user=None):
    """NURTURE → ACTIVE with a fresh activity, checked at request time (no cron)."""
    lead.lifecycle_state = Lead.LifecycleState.ACTIVE
    lead.save(update_fields=['lifecycle_state', 'updated_at'])
    return start_lead_activity(lead, user=user)


def due_nurture_leads_qs():
    """Nurture leads whose review date has arrived — surfaced, never auto-flipped silently."""
    return Lead.objects.filter(
        lifecycle_state=Lead.LifecycleState.NURTURE,
        nurture_next_review__lte=timezone.localdate(),
    )
