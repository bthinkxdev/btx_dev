"""Central CRM audit trail (AuditEntry)."""

from __future__ import annotations

import logging
from typing import Optional

from django.db.models import Q, QuerySet

from ..models import AuditEntry, Project, ProjectCredential

logger = logging.getLogger(__name__)


def log_event(
    *,
    category: str,
    action: str,
    object_type: str,
    object_id,
    object_repr: str,
    actor=None,
    project=None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    ip_address: str | None = None,
    note: str = '',
) -> Optional[AuditEntry]:
    """
    Central function for writing audit entries.
    Sets actor_label from actor.get_username() at call time.
    Never raises — if the write fails, log the exception and return None.
    """
    try:
        oid = str(object_id)[:40]
        actor_label = ''
        if actor is not None:
            try:
                actor_label = (actor.get_username() or '')[:100]
            except Exception:
                actor_label = ''
        return AuditEntry.objects.create(
            category=category,
            action=action[:100],
            object_type=object_type[:60],
            object_id=oid,
            object_repr=(object_repr or '')[:200],
            actor=actor if getattr(actor, 'is_authenticated', False) else None,
            actor_label=actor_label,
            project=project if isinstance(project, Project) and project.pk else None,
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
            note=note or '',
        )
    except Exception:
        logger.exception('AuditEntry write failed category=%s action=%s', category, action)
        return None


def get_project_audit_trail(project, limit=50) -> QuerySet:
    """
    Audit entries for a project: project FK set, or ProjectCredential rows
    belonging to this project.
    """
    cred_ids = list(
        ProjectCredential.objects.filter(project=project).values_list('pk', flat=True)
    )
    cred_ids_str = [str(i) for i in cred_ids]
    q = Q(project=project)
    if cred_ids_str:
        q |= Q(object_type='ProjectCredential', object_id__in=cred_ids_str)
    return (
        AuditEntry.objects.filter(q)
        .select_related('actor', 'project')
        .order_by('-created_at')[:limit]
    )


def get_actor_audit_trail(user, limit=50) -> QuerySet:
    return (
        AuditEntry.objects.filter(actor=user)
        .select_related('actor', 'project')
        .order_by('-created_at')[:limit]
    )


def get_audit_trail_for_object(object_type: str, object_id) -> QuerySet:
    return (
        AuditEntry.objects.filter(
            object_type=object_type,
            object_id=str(object_id),
        )
        .select_related('actor', 'project')
        .order_by('-created_at')
    )
