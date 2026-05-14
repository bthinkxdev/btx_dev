"""Tenant handover portal access (tokenized, separate from onboarding token)."""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import (
    AuditEntry,
    HandoverPortalAccess,
    OnboardingSubmission,
    Project,
    ProjectCredential,
    ProjectHandover,
    RenewalTracker,
)
from ..rbac import client_portal_credential_projection
from ..utils import log_activity
from . import audit as audit_service
from .onboarding import get_onboarding_summary


def get_portal_by_token(token: str) -> HandoverPortalAccess | None:
    """
    Fetches HandoverPortalAccess by access_token UUID.
    Returns None if not found or not active.
    Updates last_accessed_at and increments access_count.
    """
    try:
        u = uuid.UUID(str(token).strip())
    except (ValueError, TypeError, AttributeError):
        return None
    portal = (
        HandoverPortalAccess.objects.filter(access_token=u, is_active=True)
        .select_related(
            'project',
            'project__client',
            'project__package',
            'project__onboarding',
            'project__handover',
        )
        .first()
    )
    if portal is None:
        return None
    HandoverPortalAccess.objects.filter(pk=portal.pk).update(
        last_accessed_at=timezone.now(),
        access_count=F('access_count') + 1,
    )
    return (
        HandoverPortalAccess.objects.select_related(
            'project',
            'project__client',
            'project__package',
            'project__onboarding',
            'project__handover',
        ).get(pk=portal.pk)
    )


@transaction.atomic
def activate_portal(project: Project, *, activated_by) -> HandoverPortalAccess:
    """
    Creates or updates HandoverPortalAccess for the project.
    Raises ValueError if ProjectHandover.completed_at is not set.
    """
    ho = ProjectHandover.objects.filter(project=project).first()
    if not ho or not ho.completed_at:
        raise ValueError(
            'Handover must be marked complete before activating the tenant portal.'
        )
    portal, _ = HandoverPortalAccess.objects.get_or_create(project=project)
    portal.is_active = True
    portal.activated_at = timezone.now()
    portal.activated_by = activated_by
    portal.save(
        update_fields=['is_active', 'activated_at', 'activated_by'],
    )
    lead = project.client.lead
    if lead is not None:
        log_activity(lead, 'portal_activated', '')
    audit_service.log_event(
        category=AuditEntry.EventCategory.PORTAL,
        action='activated',
        object_type='HandoverPortalAccess',
        object_id=portal.pk,
        object_repr=f'Portal {portal.project_id}'[:200],
        actor=activated_by,
        project=project,
        after_state={'is_active': True},
    )
    return portal


@transaction.atomic
def deactivate_portal(project: Project, *, deactivated_by) -> HandoverPortalAccess:
    """
    Sets is_active=False. Does not delete the token.
    Raises ValueError if portal does not exist for this project.
    """
    _ = deactivated_by
    portal = HandoverPortalAccess.objects.filter(project=project).first()
    if not portal:
        raise ValueError('No portal record exists for this project.')
    portal.is_active = False
    portal.save(update_fields=['is_active'])
    lead = project.client.lead
    if lead is not None:
        log_activity(lead, 'portal_deactivated', '')
    return portal


def get_portal_payload(portal: HandoverPortalAccess) -> dict:
    """
    Sanitized data for the client portal (no decryption).
    Credentials list uses SHARED visibility only via client_portal_credential_projection.
    """
    project = portal.project
    client = project.client
    ho = ProjectHandover.objects.filter(project=project).first()
    submission = OnboardingSubmission.objects.filter(project=project).first()

    login_types = (
        ProjectCredential.CredentialType.ADMIN_LOGIN,
        ProjectCredential.CredentialType.DELIVERY_LOGIN,
        ProjectCredential.CredentialType.MAILBOX_LOGIN,
    )
    credentials = []
    for c in project.credentials.filter(
        visibility_level=ProjectCredential.VisibilityLevel.SHARED,
        credential_type__in=login_types,
    ).order_by('label', 'pk'):
        row = client_portal_credential_projection(c)
        if not row:
            continue
        credentials.append(
            {
                **row,
                'id': c.pk,
                'has_password': bool(c.password_encrypted),
                'has_secret': bool(c.secret_key_encrypted),
            }
        )

    sub = (
        project.renewals.filter(
            status=RenewalTracker.Status.ACTIVE,
            subject_type=RenewalTracker.SubjectType.SUBSCRIPTION,
        )
        .order_by('expires_at')
        .first()
    )
    if not sub:
        sub = (
            project.renewals.filter(status=RenewalTracker.Status.ACTIVE)
            .order_by('expires_at')
            .first()
        )

    subscription = {}
    if sub:
        subscription = {
            'title': sub.title,
            'expires_at': sub.expires_at,
            'status': sub.status,
            'status_display': sub.get_status_display(),
            'renewal_url': sub.renewal_url,
            'subject_type': sub.subject_type,
        }

    handover = {}
    if ho:
        handover = {
            'completed_at': ho.completed_at,
            'handover_notes': ho.handover_notes,
            'live_site_url': ho.live_site_url,
            'admin_site_url': ho.admin_site_url,
            'support_contact': ho.support_contact,
            'sla_summary': ho.sla_summary,
        }

    onboarding_summary = (
        get_onboarding_summary(submission)
        if submission
        else {'sections': [], 'overall': 0, 'is_submitted': False}
    )

    return {
        'project': {
            'business_name': client.business_name,
            'status': project.get_status_display(),
            'status_code': project.status,
            'package_name': project.package.name if project.package else '',
        },
        'client': {
            'business_name': client.business_name,
            'contact_person': client.contact_person,
            'phone': client.phone,
            'email': client.email,
        },
        'credentials': credentials,
        'subscription': subscription,
        'handover': handover,
        'onboarding_summary': onboarding_summary,
    }
