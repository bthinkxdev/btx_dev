"""Project ticket board: create, move, attach files, links."""

from django.db import transaction
from django.db.models import Max

from ..models import Project, ProjectMember, ProjectTicket, ProjectTicketAttachment, ProjectTicketLink

TICKET_STATUSES = ProjectTicket.Status.choices


def get_tickets_for_user(user):
    """Tickets on projects the user can access."""
    from .project import get_projects_for_user

    project_ids = get_projects_for_user(user).values_list('pk', flat=True)
    return (
        ProjectTicket.objects.filter(project_id__in=project_ids)
        .select_related('project', 'project__client', 'assigned_to', 'created_by')
        .prefetch_related('attachments', 'links')
    )


def tickets_for_project(project):
    return (
        ProjectTicket.objects.filter(project=project)
        .select_related('assigned_to', 'created_by')
        .prefetch_related('attachments', 'links')
    )


def tickets_by_status(project):
    buckets = {code: [] for code, _ in TICKET_STATUSES}
    for ticket in tickets_for_project(project):
        buckets.setdefault(ticket.status, []).append(ticket)
    return buckets


def ticket_columns(project):
    buckets = tickets_by_status(project)
    return [(code, label, buckets.get(code, [])) for code, label in TICKET_STATUSES]


def ticket_columns_from_queryset(qs):
    """Kanban columns from a ticket queryset (must be ordered; slice before calling)."""
    buckets = {code: [] for code, _ in TICKET_STATUSES}
    for ticket in qs:
        buckets.setdefault(ticket.status, []).append(ticket)
    return [(code, label, buckets.get(code, [])) for code, label in TICKET_STATUSES]


def count_open_tickets_for_user(user):
    """Non-done tickets the user can access (sidebar notification)."""
    return get_tickets_for_user(user).exclude(
        status=ProjectTicket.Status.DONE
    ).count()


def project_member_users(project):
    ids = list(project.memberships.values_list('user_id', flat=True))
    if not ids and project.assigned_to_id:
        ids = [project.assigned_to_id]
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if not ids:
        return User.objects.none()
    return User.objects.filter(pk__in=ids, is_active=True).order_by(
        'first_name', 'username'
    )


def _validate_assignee(project, user):
    if user is None:
        return
    if not project_member_users(project).filter(pk=user.pk).exists():
        raise ValueError('Assignee must be a member of this project team.')


@transaction.atomic
def create_ticket(
    project,
    *,
    title,
    description='',
    status=ProjectTicket.Status.BACKLOG,
    priority=ProjectTicket.Priority.MEDIUM,
    assigned_to=None,
    created_by,
    links=None,
) -> ProjectTicket:
    title = (title or '').strip()[:300]
    if not title:
        raise ValueError('Heading is required.')
    if status not in dict(TICKET_STATUSES):
        raise ValueError('Invalid status.')
    _validate_assignee(project, assigned_to)
    pos = (
        ProjectTicket.objects.filter(project=project, status=status).aggregate(
            m=Max('position')
        )['m']
        or 0
    ) + 1
    ticket = ProjectTicket.objects.create(
        project=project,
        title=title,
        description=(description or '').strip(),
        status=status,
        priority=priority,
        assigned_to=assigned_to,
        created_by=created_by,
        position=pos,
    )
    sync_links(ticket, links or [])
    return ticket


@transaction.atomic
def sync_links(ticket, links_data):
    """Replace ticket links. links_data: list of dicts with label, url, note."""
    ticket.links.all().delete()
    for idx, row in enumerate(links_data):
        url = (row.get('url') or '').strip()
        if not url:
            continue
        label = (row.get('label') or url).strip()[:200]
        note = (row.get('note') or '').strip()
        ProjectTicketLink.objects.create(
            ticket=ticket,
            label=label,
            url=url[:500],
            note=note,
            sort_order=idx,
        )


@transaction.atomic
def move_ticket(
    ticket,
    *,
    new_status,
    position=None,
) -> ProjectTicket:
    if new_status not in dict(TICKET_STATUSES):
        raise ValueError('Invalid status.')
    old_status = ticket.status
    if old_status != new_status:
        ticket.status = new_status
    if position is None:
        position = (
            ProjectTicket.objects.filter(
                project=ticket.project, status=new_status
            )
            .exclude(pk=ticket.pk)
            .aggregate(m=Max('position'))['m']
            or 0
        ) + 1
    ticket.position = max(0, int(position))
    ticket.save(update_fields=['status', 'position', 'updated_at'])
    return ticket


@transaction.atomic
def update_ticket_fields(ticket, **fields) -> ProjectTicket:
    allowed = {'title', 'description', 'priority', 'assigned_to', 'status'}
    update_fields = ['updated_at']
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key == 'title':
            val = (val or '').strip()[:300]
            if not val:
                raise ValueError('Heading is required.')
        if key == 'assigned_to':
            _validate_assignee(ticket.project, val)
            if val == '':
                val = None
        if key == 'status' and val not in dict(TICKET_STATUSES):
            raise ValueError('Invalid status.')
        setattr(ticket, key, val)
        update_fields.append(
            'assigned_to_id' if key == 'assigned_to' else key
        )
    ticket.save(update_fields=update_fields)
    return ticket


def add_attachment(
    ticket,
    *,
    uploaded_file,
    uploaded_by,
    visibility=None,
    save_to_desk=False,
    is_reference_screenshot=False,
) -> ProjectTicketAttachment:
    name = getattr(uploaded_file, 'name', '') or 'file'
    vis = visibility or ProjectTicketAttachment.Visibility.TEAM
    if vis not in dict(ProjectTicketAttachment.Visibility.choices):
        vis = ProjectTicketAttachment.Visibility.TEAM
    return ProjectTicketAttachment.objects.create(
        ticket=ticket,
        file=uploaded_file,
        original_name=name[:255],
        uploaded_by=uploaded_by,
        visibility=vis,
        save_to_desk=bool(save_to_desk),
        is_reference_screenshot=bool(is_reference_screenshot),
    )


def save_uploaded_files(
    ticket,
    *,
    file_list,
    uploaded_by,
    visibility='team',
    save_to_desk=False,
    is_reference_screenshot=False,
):
    saved = []
    for f in file_list:
        if f:
            saved.append(
                add_attachment(
                    ticket,
                    uploaded_file=f,
                    uploaded_by=uploaded_by,
                    visibility=visibility,
                    save_to_desk=save_to_desk,
                    is_reference_screenshot=is_reference_screenshot,
                )
            )
    return saved


def parse_links_from_post(post):
    """Build link dicts from POST keys link_label_N, link_url_N, link_note_N."""
    indices = set()
    for key in post:
        if key.startswith('link_url_'):
            try:
                indices.add(int(key.split('_', 2)[2]))
            except ValueError:
                pass
    rows = []
    for i in sorted(indices):
        url = (post.get(f'link_url_{i}') or '').strip()
        if not url:
            continue
        rows.append(
            {
                'label': (post.get(f'link_label_{i}') or '').strip(),
                'url': url,
                'note': (post.get(f'link_note_{i}') or '').strip(),
            }
        )
    return rows


def user_can_view_attachment(user, attachment):
    ticket = attachment.ticket
    project = ticket.project
    from .project import get_projects_for_user

    if not get_projects_for_user(user).filter(pk=project.pk).exists():
        return False
    vis = attachment.visibility
    if vis == ProjectTicketAttachment.Visibility.TEAM:
        return True
    if vis == ProjectTicketAttachment.Visibility.ASSIGNEE:
        return ticket.assigned_to_id == user.pk or user.is_staff or user.is_superuser
    if vis == ProjectTicketAttachment.Visibility.LEAD:
        lead_id = (
            project.memberships.filter(role=ProjectMember.Role.LEAD)
            .values_list('user_id', flat=True)
            .first()
        )
        if not lead_id:
            lead_id = project.assigned_to_id
        return user.pk == lead_id or user.is_staff or user.is_superuser
    return True
