"""
Approval engine (Part 3.4).

Rules of engagement enforced here:
 1. Requester cannot approve their own request (maker-checker), even with the permission.
 2. A request freezes the object; the owning service cancels and restarts it on edit.
 3. Approvers see before/after, requester, reason.
 4. Approve and Reject both write audit events; Reject needs a comment of at least 10 characters.
 5. Delegation for a date range; a delegate can never approve the delegator's own request.
 6. Pending longer than the policy window -> Expired, requester and approver's manager notified.

Approval *levels* map to permissions, never to job titles.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Callable

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import audit, notify, permissions

LEVELS = {
    'manager': None,                       # <module>.approve
    'finance': ('payments', 'approve'),
    'accountant': ('accounting', 'approve'),
    'owner': ('settings', 'configure'),
    'hr': ('leave', 'approve'),
    'project': ('projects', 'approve'),
}
LEVEL_LABELS = {'manager': 'Manager', 'finance': 'Finance', 'accountant': 'Accountant', 'owner': 'Owner',
                'hr': 'HR', 'project': 'Project manager'}
DEFAULT_EXPIRY_HOURS = {'quotation': 72, 'payment_link': 48, 'refund': 72, 'expense': 168, 'leave': 96,
                        'lead_merge': 168, 'lead_export': 48, 'lead_lost': 72, 'payment_link_extension': 48}

_HANDLERS: dict[str, tuple[Callable, Callable | None]] = {}


class ApprovalError(ValidationError):
    pass


def register(kind: str, on_approve: Callable, on_reject: Callable | None = None):
    _HANDLERS[kind] = (on_approve, on_reject)


def level_perm(level: str, module: str) -> tuple[str, str]:
    perm = LEVELS.get(level)
    return perm if perm else (module, 'approve')


def _user_has_level(user, level: str, module: str) -> bool:
    m, a = level_perm(level, module)
    return permissions.has_perm(user, m, a)


def approvers_for(req) -> list:
    m, a = level_perm(req.required_level, req.module)
    return [u for u in notify.users_with_perm(m, a) if u.pk != req.requested_by_id]


def can_decide(user, req) -> tuple[bool, str]:
    if req.status != 'pending':
        return False, 'This request is no longer pending.'
    if user.pk == req.requested_by_id:
        return False, 'You cannot approve your own request.'
    if _user_has_level(user, req.required_level, req.module):
        return True, ''
    # Delegation: user acts for someone who holds the level, inside the date range.
    from crm.models import UserProfile
    today = timezone.localdate()
    for p in UserProfile.objects.filter(delegate_to=user, delegate_from__lte=today, delegate_until__gte=today) \
            .select_related('user'):
        if p.user_id == req.requested_by_id:
            continue
        if _user_has_level(p.user, req.required_level, req.module):
            return True, ''
    return False, f'Needs {LEVEL_LABELS.get(req.required_level, req.required_level)} approval.'


def pending_for(obj, kind: str | None = None):
    from crm.models import ApprovalRequest
    ct = ContentType.objects.get_for_model(type(obj))
    qs = ApprovalRequest.objects.filter(content_type=ct, object_id=str(obj.pk), status='pending')
    if kind:
        qs = qs.filter(kind=kind)
    return qs.first()


def history_for(obj):
    from crm.models import ApprovalRequest
    ct = ContentType.objects.get_for_model(type(obj))
    return ApprovalRequest.objects.filter(content_type=ct, object_id=str(obj.pk)).select_related(
        'requested_by', 'decided_by')


def submit(kind: str, obj, *, title: str, module: str, requested_by, level: str = 'manager', amount=None,
           reasons: list | None = None, before: dict | None = None, after: dict | None = None,
           payload: dict | None = None, message: str = '', expires_hours: int | None = None):
    from crm.models import ApprovalRequest
    if pending_for(obj, kind):
        raise ApprovalError('An approval request is already pending for this record.')
    hours = expires_hours if expires_hours is not None else DEFAULT_EXPIRY_HOURS.get(kind)
    req = ApprovalRequest.objects.create(
        kind=kind, content_type=ContentType.objects.get_for_model(type(obj)), object_id=str(obj.pk),
        title=title[:200], module=module, amount=amount, reasons=audit._clean(reasons or []),
        before=audit._clean(before or {}), after=audit._clean(after or {}), payload=audit._clean(payload or {}),
        message=message, required_level=level, requested_by=requested_by,
        expires_at=timezone.now() + timedelta(hours=hours) if hours else None,
    )
    audit.log('approval.requested', obj, actor=requested_by,
              changes={'kind': kind, 'level': level, 'reasons': reasons or []}, reason=message)
    url = reverse('crm:approval_detail', args=[req.pk])
    transaction.on_commit(lambda: notify.notify(
        approvers_for(req), f'Approval needed: {req.title}', body=f'Requested by {requested_by.get_full_name() or requested_by}',
        url=url, event_type='approval_requested', level='warning', dedupe_key=f'approval:{req.pk}', email=True))
    return req


def _lock(req_id):
    from crm.models import ApprovalRequest
    return ApprovalRequest.objects.select_for_update().get(pk=req_id)


def approve(req, user, comment: str = ''):
    with transaction.atomic():
        req = _lock(req.pk)
        ok, why = can_decide(user, req)
        if not ok:
            audit.log('approval.approve_blocked', req.target, actor=user, result='denied', reason=why)
            raise ApprovalError(why)
        req.status = 'approved'
        req.decided_by = user
        req.decided_at = timezone.now()
        req.comment = comment
        req.save(update_fields=['status', 'decided_by', 'decided_at', 'comment'])
        handler = _HANDLERS.get(req.kind)
        if handler:
            handler[0](req, user)
        audit.log('approval.approved', req.target, actor=user, changes={'kind': req.kind, 'request': req.pk},
                  reason=comment)
    notify.notify([req.requested_by], f'Approved: {req.title}', body=comment, url=_target_url(req),
                  event_type='approval_decided', level='success')
    return req


def reject(req, user, comment: str):
    if len((comment or '').strip()) < 10:
        raise ApprovalError('Add a comment of at least 10 characters explaining the rejection.')
    with transaction.atomic():
        req = _lock(req.pk)
        ok, why = can_decide(user, req)
        if not ok:
            raise ApprovalError(why)
        req.status = 'rejected'
        req.decided_by = user
        req.decided_at = timezone.now()
        req.comment = comment.strip()
        req.save(update_fields=['status', 'decided_by', 'decided_at', 'comment'])
        handler = _HANDLERS.get(req.kind)
        if handler and handler[1]:
            handler[1](req, user)
        audit.log('approval.rejected', req.target, actor=user, changes={'kind': req.kind, 'request': req.pk},
                  reason=comment)
    notify.notify([req.requested_by], f'Rejected: {req.title}', body=comment, url=_target_url(req),
                  event_type='approval_decided', level='warning')
    return req


def cancel(req, user, reason: str = 'Cancelled by requester', run_reject_handler: bool = True):
    with transaction.atomic():
        req = _lock(req.pk)
        if req.status != 'pending':
            return req
        req.status = 'cancelled'
        req.decided_by = user
        req.decided_at = timezone.now()
        req.comment = reason
        req.save(update_fields=['status', 'decided_by', 'decided_at', 'comment'])
        handler = _HANDLERS.get(req.kind)
        if run_reject_handler and handler and handler[1]:
            handler[1](req, user)
        audit.log('approval.cancelled', req.target, actor=user, reason=reason)
    return req


def expire_overdue() -> int:
    """Job: mark expired requests, notify requester and approver's manager (rule 6)."""
    from crm.models import ApprovalRequest
    count = 0
    for req in ApprovalRequest.objects.filter(status='pending', expires_at__lt=timezone.now()):
        with transaction.atomic():
            r = _lock(req.pk)
            if r.status != 'pending':
                continue
            r.status = 'expired'
            r.decided_at = timezone.now()
            r.save(update_fields=['status', 'decided_at'])
            handler = _HANDLERS.get(r.kind)
            if handler and handler[1]:
                handler[1](r, None)
            audit.log('approval.expired', r.target, actor_type='job', reason='Pending longer than the policy window')
        recipients = [r.requested_by] + [notify.manager_of(u) for u in approvers_for(r)]
        notify.notify(recipients, f'Approval expired: {r.title}', url=_target_url(r), event_type='approval_expired',
                      level='warning')
        count += 1
    return count


def remind_and_escalate() -> int:
    """Job: reminder at 4 h, escalate to owners at 24 h (Part 3.4)."""
    from crm.models import ApprovalRequest
    now = timezone.now()
    count = 0
    for r in ApprovalRequest.objects.filter(status='pending', requested_at__lt=now - timedelta(hours=4),
                                            reminded_at__isnull=True):
        notify.notify(approvers_for(r), f'Reminder: approval waiting — {r.title}',
                      url=reverse('crm:approval_detail', args=[r.pk]), event_type='approval_requested',
                      level='warning', dedupe_key=f'approval-remind:{r.pk}')
        ApprovalRequest.objects.filter(pk=r.pk).update(reminded_at=now)
        count += 1
    for r in ApprovalRequest.objects.filter(status='pending', requested_at__lt=now - timedelta(hours=24),
                                            escalated_at__isnull=True):
        notify.notify(notify.owners(), f'Escalated approval: {r.title}',
                      url=reverse('crm:approval_detail', args=[r.pk]), event_type='approval_requested',
                      level='critical', dedupe_key=f'approval-escalate:{r.pk}', email=True)
        ApprovalRequest.objects.filter(pk=r.pk).update(escalated_at=now)
        count += 1
    return count


def _target_url(req) -> str:
    target = req.target
    if target is not None and hasattr(target, 'get_crm_url'):
        return target.get_crm_url()
    return reverse('crm:approval_detail', args=[req.pk])
