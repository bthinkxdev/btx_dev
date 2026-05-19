"""Client and project lifecycle: lead conversion, project CRUD helpers, visibility."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from ..models import AuditEntry, Client, Lead, Project
from ..utils import log_activity
from . import audit as audit_service
from . import billing as billing_service
from . import scope as scope_service


@transaction.atomic
def convert_lead_to_client(lead, *, created_by) -> Client:
    """
    Creates a Client from a Lead. Raises ValueError if the lead
    already has a client. Does not change lead.status — caller does that.
    """
    if Client.objects.filter(lead=lead).exists():
        raise ValueError('This lead already has a client.')
    return Client.objects.create(
        lead=lead,
        business_name=(lead.name or 'Unknown')[:200],
        contact_person=(lead.name or 'Unknown')[:200],
        phone=(lead.phone or '')[:40],
        email=(lead.email or '')[:254],
        created_by=created_by,
    )


def get_or_create_client_for_lead(lead, *, created_by) -> tuple[Client, bool]:
    """
    Returns (client, created). Reuses the Client row linked to this lead when it
    already exists; otherwise creates one via convert_lead_to_client.
    """
    existing = Client.objects.filter(lead=lead).first()
    if existing:
        return existing, False
    return convert_lead_to_client(lead, created_by=created_by), True


@transaction.atomic
def create_project(
    client,
    *,
    package,
    deal_value,
    advance_received=0,
    assigned_to=None,
    notes='',
    created_by,
) -> Project:
    """
    Creates a Project for a Client. Calculates balance_due automatically.
    Logs an ActivityLog entry on the originating lead if client.lead exists.
    """
    advance = advance_received or Decimal('0')
    project = Project(
        client=client,
        package=package,
        deal_value=deal_value,
        advance_received=Decimal('0'),
        assigned_to=assigned_to,
        notes=notes or '',
        created_by=created_by,
    )
    project.save()
    scope_service.enforce_scope_on_project_create(project)
    billing_service.ensure_project_contract_ledger(project, actor=created_by)
    if advance > 0:
        billing_service.record_opening_advance(
            project,
            advance,
            actor=created_by,
            notes='Advance recorded at project creation',
        )
    lead = client.lead
    if lead is not None:
        pkg_label = str(package) if package else '—'
        log_activity(
            lead,
            'project_created',
            f'Project #{project.pk}: {pkg_label}, deal {deal_value}, advance {advance_received}',
        )
    audit_service.log_event(
        category=AuditEntry.EventCategory.PROJECT,
        action='created',
        object_type='Project',
        object_id=project.pk,
        object_repr=str(project)[:200],
        actor=created_by,
        project=project,
        after_state={
            'status': project.status,
            'package_id': project.package_id,
        },
    )
    return project


@transaction.atomic
def convert_lead_to_project(
    lead,
    *,
    package,
    deal_value,
    advance_received=0,
    assigned_to=None,
    notes='',
    created_by,
):
    """
    Atomic: converts lead -> client -> project in one transaction.
    Sets lead.status to ADVANCE_RECEIVED_PROJECT_STARTED after creation.
    Returns (client, project) tuple.
    """
    if Client.objects.filter(lead=lead).exists():
        raise ValueError('This lead already has a client.')
    client = convert_lead_to_client(lead, created_by=created_by)
    project = create_project(
        client,
        package=package,
        deal_value=deal_value,
        advance_received=advance_received,
        assigned_to=assigned_to,
        notes=notes,
        created_by=created_by,
    )
    log_activity(
        lead,
        'lead_converted_to_project',
        f'Client "{client.business_name}", project #{project.pk}',
    )
    lead.status = Lead.Status.ADVANCE_RECEIVED_PROJECT_STARTED
    lead.save(update_fields=['status', 'updated_at'])
    return client, project


@transaction.atomic
def update_project_status(project, *, new_status, updated_by) -> Project:
    """
    Updates project status. Logs the change to ActivityLog on the
    related lead if it exists.
    """
    if new_status not in dict(Project.Status.choices):
        raise ValueError('Invalid project status.')
    old = project.status
    if old == new_status:
        return project
    project.status = new_status
    project.save()
    lead = project.client.lead
    if lead is not None:
        log_activity(
            lead,
            'project_status_change',
            f'Project #{project.pk}: {old} → {new_status}',
        )
    audit_service.log_event(
        category=AuditEntry.EventCategory.PROJECT,
        action='status_changed',
        object_type='Project',
        object_id=project.pk,
        object_repr=str(project)[:200],
        actor=updated_by,
        project=project,
        before_state={'status': {'from': old, 'to': new_status}},
        after_state={'status': new_status},
    )
    return project


def get_projects_for_user(user):
    """
    Superusers and staff see all projects.
    Regular users see only projects they created or are assigned_to.
    Returns queryset with select_related('client', 'package', 'assigned_to').
    """
    qs = Project.objects.select_related('client', 'package', 'assigned_to')
    if user.is_superuser or user.is_staff:
        return qs
    return qs.filter(Q(created_by=user) | Q(assigned_to=user))
