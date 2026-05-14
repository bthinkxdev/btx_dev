"""Change request lifecycle and triage."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import QuerySet

from ..models import AuditEntry, ChangeRequest, Project
from . import audit as audit_service


def _cr_repr(cr: ChangeRequest) -> str:
    return f'{cr.title}'[:200]


@transaction.atomic
def submit_change_request(
    project: Project,
    *,
    title: str,
    description: str,
    request_type: str,
    submitted_via_portal: bool = False,
    submitted_by_staff=None,
    assigned_to=None,
) -> ChangeRequest:
    cr = ChangeRequest.objects.create(
        project=project,
        title=title[:200],
        description=description,
        request_type=request_type,
        status=ChangeRequest.Status.SUBMITTED,
        submitted_via_portal=submitted_via_portal,
        submitted_by_staff=submitted_by_staff
        if getattr(submitted_by_staff, 'pk', None)
        else None,
        assigned_to=assigned_to if getattr(assigned_to, 'pk', None) else None,
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='submitted',
        object_type='ChangeRequest',
        object_id=cr.pk,
        object_repr=_cr_repr(cr),
        actor=submitted_by_staff,
        project=project,
        after_state={'status': ChangeRequest.Status.SUBMITTED},
    )
    return cr


@transaction.atomic
def triage_change_request(
    change_request: ChangeRequest,
    *,
    requested_features: list,
    updated_by,
) -> ChangeRequest:
    if change_request.status not in (
        ChangeRequest.Status.SUBMITTED,
        ChangeRequest.Status.IN_TRIAGE,
    ):
        raise ValueError('Triage is only valid for submitted or in-triage requests.')
    pkg = change_request.project.package
    if not pkg:
        raise ValueError('Project has no package.')
    try:
        scope = pkg.scope
    except Exception as exc:
        raise ValueError('Package has no scope defined.') from exc

    feats = list(requested_features or [])
    verdict_info = scope.check_request_in_scope(feats)
    change_request.requested_features = feats
    change_request.scope_verdict = verdict_info['verdict']
    change_request.status = ChangeRequest.Status.IN_TRIAGE
    change_request.save(
        update_fields=[
            'requested_features',
            'scope_verdict',
            'status',
            'updated_at',
        ]
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='triaged',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=updated_by,
        project=change_request.project,
        after_state={
            'scope_verdict': change_request.scope_verdict,
            'in_scope': verdict_info['in_scope'],
            'out_of_scope': verdict_info['out_of_scope'],
        },
    )
    return change_request


@transaction.atomic
def quote_change_request(
    change_request: ChangeRequest,
    *,
    quoted_amount,
    quote_notes: str,
    updated_by,
) -> ChangeRequest:
    if change_request.status != ChangeRequest.Status.IN_TRIAGE:
        raise ValueError('Quote is only valid while the request is in triage.')
    if change_request.scope_verdict not in ('partial', 'out_of_scope'):
        raise ValueError('Quote applies only to partial or out-of-scope requests.')
    amt = quoted_amount
    if isinstance(amt, (int, float)):
        amt = Decimal(str(amt))
    change_request.quoted_amount = amt
    change_request.quote_notes = (quote_notes or '')[:20000]
    change_request.status = ChangeRequest.Status.QUOTED
    change_request.save(
        update_fields=['quoted_amount', 'quote_notes', 'status', 'updated_at']
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='quoted',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=updated_by,
        project=change_request.project,
        after_state={
            'quoted_amount': str(change_request.quoted_amount),
            'status': change_request.status,
        },
    )
    return change_request


@transaction.atomic
def approve_change_request(
    change_request: ChangeRequest, *, approved_by
) -> ChangeRequest:
    ok = False
    if change_request.status == ChangeRequest.Status.QUOTED:
        ok = True
    elif (
        change_request.status == ChangeRequest.Status.IN_TRIAGE
        and change_request.scope_verdict == 'in_scope'
    ):
        ok = True
    if not ok:
        raise ValueError(
            'Approve is only valid from Quoted, or from In Triage when fully in scope.'
        )
    change_request.status = ChangeRequest.Status.APPROVED
    change_request.save(update_fields=['status', 'updated_at'])
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='approved',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=approved_by,
        project=change_request.project,
        after_state={'status': change_request.status},
    )
    return change_request


@transaction.atomic
def start_change_request(
    change_request: ChangeRequest, *, started_by
) -> ChangeRequest:
    """Approved → in progress (work started)."""
    if change_request.status != ChangeRequest.Status.APPROVED:
        raise ValueError('Start work is only valid after approval.')
    change_request.status = ChangeRequest.Status.IN_PROGRESS
    change_request.save(update_fields=['status', 'updated_at'])
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='started',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=started_by,
        project=change_request.project,
        after_state={'status': change_request.status},
    )
    return change_request


@transaction.atomic
def reject_change_request(
    change_request: ChangeRequest, *, rejected_by, reason: str
) -> ChangeRequest:
    if change_request.status in (
        ChangeRequest.Status.IN_PROGRESS,
        ChangeRequest.Status.COMPLETED,
        ChangeRequest.Status.CANCELLED,
        ChangeRequest.Status.REJECTED,
    ):
        raise ValueError('Cannot reject this change request in its current state.')
    change_request.status = ChangeRequest.Status.REJECTED
    change_request.resolution_note = (reason or '')[:20000]
    change_request.save(
        update_fields=['status', 'resolution_note', 'updated_at'],
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='rejected',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=rejected_by,
        project=change_request.project,
        after_state={'status': change_request.status},
        note=change_request.resolution_note[:500],
    )
    return change_request


@transaction.atomic
def complete_change_request(
    change_request: ChangeRequest,
    *,
    resolution_note: str,
    completed_by,
) -> ChangeRequest:
    if change_request.status != ChangeRequest.Status.IN_PROGRESS:
        raise ValueError('Complete is only valid while work is in progress.')
    change_request.status = ChangeRequest.Status.COMPLETED
    change_request.resolution_note = (resolution_note or '')[:20000]
    change_request.save(
        update_fields=['status', 'resolution_note', 'updated_at'],
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.CHANGE_REQUEST,
        action='completed',
        object_type='ChangeRequest',
        object_id=change_request.pk,
        object_repr=_cr_repr(change_request),
        actor=completed_by,
        project=change_request.project,
        after_state={'status': change_request.status},
    )
    return change_request


def get_change_requests_for_project(project: Project) -> QuerySet:
    return (
        ChangeRequest.objects.filter(project=project)
        .select_related('assigned_to')
        .order_by('-created_at')
    )
