"""Structured provisioning steps per project (initialized after onboarding submit)."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from ..models import AuditEntry, Project, ProjectProvisioning, ProvisioningStep
from . import audit as audit_service

# (domain, step_key, provider_type, provider_name) — vendor names live in provider_name / metadata only.
DEFAULT_STEP_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ('tenant', 'tenant_created', 'tenant', ''),
    ('tenant', 'tenant_domain_connected', 'dns', ''),
    ('tenant', 'tenant_admin_created', 'tenant', ''),
    ('payment_gateway', 'payment_gateway_requested', 'payment_gateway', ''),
    ('payment_gateway', 'payment_gateway_verified', 'payment_gateway', ''),
    ('payment_gateway', 'payment_gateway_live', 'payment_gateway', ''),
    ('delivery', 'delivery_platform_created', 'delivery', ''),
    ('delivery', 'delivery_api_configured', 'delivery', ''),
    ('email', 'system_email_created', 'email', ''),
    ('email', 'smtp_configured', 'smtp', ''),
    ('email', 'dkim_verified', 'dns', ''),
    ('email', 'spf_verified', 'dns', ''),
    ('infrastructure', 'ssl_active', 'tls', ''),
    ('infrastructure', 'custom_domain_connected', 'dns', ''),
)


@transaction.atomic
def create_default_provisioning(project: Project) -> ProjectProvisioning | None:
    """
    Idempotent: ensures ProjectProvisioning exists and all default steps are present.
    Call only after onboarding is fully submitted (signal / explicit).
    """
    if not project.pk:
        return None
    prov, _ = ProjectProvisioning.objects.get_or_create(project=project)
    existing = set(prov.steps.values_list('step_key', flat=True))
    to_create = [
        ProvisioningStep(
            provisioning=prov,
            step_key=key,
            domain=domain,
            provider_type=ptype,
            provider_name=pname,
            status=ProvisioningStep.Status.PENDING,
        )
        for domain, key, ptype, pname in DEFAULT_STEP_DEFS
        if key not in existing
    ]
    if to_create:
        ProvisioningStep.objects.bulk_create(to_create)
    return prov


def get_or_none(project: Project) -> ProjectProvisioning | None:
    return ProjectProvisioning.objects.filter(project=project).first()


@transaction.atomic
def update_provisioning_status(
    *,
    project: Project,
    step_key: str,
    status: str,
    user,
    notes: str | None = None,
    assigned_to=None,
) -> ProvisioningStep:
    prov = ProjectProvisioning.objects.select_for_update().get(project=project)
    step = ProvisioningStep.objects.select_for_update().get(
        provisioning=prov, step_key=step_key
    )
    old_status = step.status
    step.status = status
    if notes is not None:
        step.notes = notes
    if assigned_to is not None:
        step.assigned_to = assigned_to
    step.save(update_fields=['status', 'notes', 'assigned_to', 'updated_at'])
    audit_service.log_event(
        category=AuditEntry.EventCategory.PROVISIONING,
        action='step_updated',
        object_type='ProvisioningStep',
        object_id=step.pk,
        object_repr=f'{step_key} ({step.get_status_display()})'[:200],
        actor=user,
        project=project,
        after_state={
            'step_key': step_key,
            'status': {'from': old_status, 'to': status},
        },
    )
    return step


@transaction.atomic
def complete_provisioning_step(
    *,
    project: Project,
    step_key: str,
    user,
    notes: str | None = None,
) -> ProvisioningStep:
    prov = ProjectProvisioning.objects.select_for_update().get(project=project)
    step = ProvisioningStep.objects.select_for_update().get(
        provisioning=prov, step_key=step_key
    )
    old_status = step.status
    now = timezone.now()
    step.status = ProvisioningStep.Status.COMPLETED
    step.completed_by = user
    step.completed_at = now
    if notes is not None:
        step.notes = notes
    step.save(
        update_fields=[
            'status',
            'completed_by',
            'completed_at',
            'notes',
            'updated_at',
        ]
    )
    audit_service.log_event(
        category=AuditEntry.EventCategory.PROVISIONING,
        action='step_updated',
        object_type='ProvisioningStep',
        object_id=step.pk,
        object_repr=f'{step_key} (completed)'[:200],
        actor=user,
        project=project,
        after_state={
            'step_key': step_key,
            'status': {'from': old_status, 'to': ProvisioningStep.Status.COMPLETED},
        },
    )
    return step


def get_project_provisioning_summary(project: Project) -> dict[str, Any]:
    prov = get_or_none(project)
    if not prov:
        return {
            'exists': False,
            'total_steps': 0,
            'completed': 0,
            'blocked': 0,
            'failed': 0,
            'percent': 0,
            'steps': [],
        }
    steps = list(prov.steps.order_by('domain', 'step_key'))
    total = len(steps)
    completed = sum(1 for s in steps if s.status == ProvisioningStep.Status.COMPLETED)
    blocked = sum(1 for s in steps if s.status == ProvisioningStep.Status.BLOCKED)
    failed = sum(1 for s in steps if s.status == ProvisioningStep.Status.FAILED)
    pct = int(round((completed / total) * 100)) if total else 0
    return {
        'exists': True,
        'total_steps': total,
        'completed': completed,
        'blocked': blocked,
        'failed': failed,
        'percent': pct,
        'steps': steps,
        'provisioning': prov,
    }
