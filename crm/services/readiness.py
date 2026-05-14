"""Operational readiness: onboarding + provisioning + credentials + handover + portal."""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from ..models import (
    HandoverPortalAccess,
    OnboardingSubmission,
    Project,
    ProjectCredential,
    ProjectHandover,
    ProjectProvisioning,
    ProvisioningStep,
)


def get_operational_readiness(project: Project) -> dict[str, Any]:
    submission = OnboardingSubmission.objects.filter(project=project).first()
    onboard_ok = bool(submission and submission.is_fully_submitted())

    prov = ProjectProvisioning.objects.filter(project=project).first()
    if not onboard_ok or prov is None:
        prov_ok = False
    else:
        steps = list(prov.steps.all())
        if not steps:
            prov_ok = False
        else:
            prov_ok = all(
                s.status == ProvisioningStep.Status.COMPLETED for s in steps
            )

    login_types = (
        ProjectCredential.CredentialType.ADMIN_LOGIN,
        ProjectCredential.CredentialType.DELIVERY_LOGIN,
        ProjectCredential.CredentialType.MAILBOX_LOGIN,
    )
    cred_ok = project.credentials.filter(
        visibility_level=ProjectCredential.VisibilityLevel.SHARED,
        credential_type__in=login_types,
    ).exclude(username='').exists()

    ho = ProjectHandover.objects.filter(project=project).first()
    handover_ok = bool(ho and ho.completed_at)

    portal_row = HandoverPortalAccess.objects.filter(project=project).first()
    portal_active = bool(portal_row and portal_row.is_active)

    flags = (onboard_ok, prov_ok, cred_ok, handover_ok, portal_active)
    pct = int(round(sum(1 for f in flags if f) / len(flags) * 100))

    return {
        'onboarding_complete': onboard_ok,
        'provisioning_complete': prov_ok,
        'credentials_ready': cred_ok,
        'handover_complete': handover_ok,
        'portal_active': portal_active,
        'readiness_percent': pct,
        'operationally_ready': all(flags),
    }


def missing_client_visible_logins(project: Project) -> bool:
    """True if no SHARED portal login credentials are stored for this project."""
    return not project.credentials.filter(
        Q(credential_type=ProjectCredential.CredentialType.ADMIN_LOGIN)
        | Q(credential_type=ProjectCredential.CredentialType.DELIVERY_LOGIN),
        visibility_level=ProjectCredential.VisibilityLevel.SHARED,
    ).exclude(username='').exists()
