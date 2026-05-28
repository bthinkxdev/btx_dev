"""Dedicated CRM Tickets section (project delivery tasks)."""

from django.contrib import messages
from django.db.models import Q
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .forms import ProjectTicketFilterForm, ProjectTicketForm
from .models import Project, ProjectTicket, ProjectTicketAttachment
from .services.project import get_projects_for_user
from .services import project_tickets as ticket_service

User = get_user_model()


def _apply_ticket_filters(request, user):
    filt = ProjectTicketFilterForm(request.GET, user=user)
    qs = ticket_service.get_tickets_for_user(user)
    project = None
    if filt.is_valid():
        cd = filt.cleaned_data
        if cd.get('project'):
            project = cd['project']
            qs = qs.filter(project=project)
        if cd.get('status'):
            qs = qs.filter(status=cd['status'])
        if cd.get('assigned_to'):
            qs = qs.filter(assigned_to=cd['assigned_to'])
        q = (cd.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    return qs.order_by('position', '-updated_at'), project, filt


def _ticket_board_ctx(request, project, qs):
    columns = ticket_service.ticket_columns_from_queryset(qs[:200])
    return {
        'project': project,
        'ticket_columns': columns,
        'ticket_form': ProjectTicketForm(project=project) if project else None,
        'board_mode': True,
        'filter_query': request.GET.urlencode(),
    }


@login_required
def tickets_hub(request):
    """Tickets home: Kanban board (default) or list."""
    user = request.user
    qs, project, filt = _apply_ticket_filters(request, user)
    view = (request.GET.get('view') or 'board').strip()
    ctx = {
        'filter_form': filt,
        'tickets': qs[:200],
        'project': project,
        'view': view,
        'projects': get_projects_for_user(user).order_by('-updated_at')[:100],
    }
    if view == 'board':
        ctx.update(_ticket_board_ctx(request, project, qs))
    return render(request, 'crm/tickets/hub.html', ctx)


@login_required
@require_http_methods(['GET', 'POST'])
def ticket_create(request):
    user = request.user
    project = None
    preselect = request.GET.get('project') or request.POST.get('project')
    if preselect:
        project = get_object_or_404(get_projects_for_user(user), pk=preselect)

    if request.method == 'POST':
        proj_id = request.POST.get('project')
        if proj_id:
            project = get_object_or_404(get_projects_for_user(user), pk=proj_id)
        form = ProjectTicketForm(request.POST, project=project, user=user, show_project=True)
        if form.is_valid():
            if not project and form.cleaned_data.get('project'):
                project = form.cleaned_data['project']
            cd = form.cleaned_data
            try:
                ticket = ticket_service.create_ticket(
                    project,
                    title=cd['title'],
                    description=cd.get('description') or '',
                    status=cd.get('status') or ProjectTicket.Status.BACKLOG,
                    priority=cd.get('priority') or ProjectTicket.Priority.MEDIUM,
                    assigned_to=cd.get('assigned_to'),
                    created_by=user,
                    links=ticket_service.parse_links_from_post(request.POST),
                )
                ref_vis = request.POST.get('ref_visibility') or 'team'
                ref_desk = request.POST.get('ref_save_desk') in ('1', 'on', 'true')
                ticket_service.save_uploaded_files(
                    ticket,
                    file_list=request.FILES.getlist('reference_screenshots'),
                    uploaded_by=user,
                    visibility=ref_vis,
                    save_to_desk=ref_desk,
                    is_reference_screenshot=True,
                )
                ticket_service.save_uploaded_files(
                    ticket,
                    file_list=request.FILES.getlist('files'),
                    uploaded_by=user,
                    visibility=request.POST.get('file_visibility') or 'team',
                    save_to_desk=request.POST.get('file_save_desk') in ('1', 'on', 'true'),
                    is_reference_screenshot=False,
                )
                messages.success(request, 'Ticket created.')
                return redirect('crm:ticket_detail', pk=ticket.pk)
            except ValueError as exc:
                form.add_error(None, str(exc))
    else:
        form = ProjectTicketForm(project=project, user=user, show_project=True)
        if project:
            form.fields['project'].initial = project.pk

    link_rows = [{'label': '', 'url': '', 'note': ''} for _ in range(5)]
    return render(
        request,
        'crm/tickets/create.html',
        {
            'form': form,
            'project': project,
            'link_rows': link_rows,
            'visibility_choices': ProjectTicketAttachment.Visibility.choices,
        },
    )


@login_required
@require_http_methods(['GET', 'POST'])
def ticket_detail(request, pk):
    user = request.user
    ticket = get_object_or_404(
        ticket_service.get_tickets_for_user(user),
        pk=pk,
    )
    project = ticket.project
    if request.method == 'POST':
        action = (request.POST.get('action') or 'update').strip()
        if action == 'delete':
            ticket.delete()
            messages.success(request, 'Ticket deleted.')
            return redirect('crm:tickets')
        form = ProjectTicketForm(request.POST, project=project)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                ticket_service.update_ticket_fields(
                    ticket,
                    title=cd['title'],
                    description=cd.get('description') or '',
                    status=cd.get('status'),
                    priority=cd.get('priority'),
                    assigned_to=cd.get('assigned_to'),
                )
                ticket_service.sync_links(
                    ticket, ticket_service.parse_links_from_post(request.POST)
                )
                ticket_service.save_uploaded_files(
                    ticket,
                    file_list=request.FILES.getlist('reference_screenshots'),
                    uploaded_by=user,
                    visibility=request.POST.get('ref_visibility') or 'team',
                    save_to_desk=request.POST.get('ref_save_desk') in ('1', 'on', 'true'),
                    is_reference_screenshot=True,
                )
                ticket_service.save_uploaded_files(
                    ticket,
                    file_list=request.FILES.getlist('files'),
                    uploaded_by=user,
                    visibility=request.POST.get('file_visibility') or 'team',
                    save_to_desk=request.POST.get('file_save_desk') in ('1', 'on', 'true'),
                    is_reference_screenshot=False,
                )
                messages.success(request, 'Ticket saved.')
                return redirect('crm:ticket_detail', pk=ticket.pk)
            except ValueError as exc:
                form.add_error(None, str(exc))
    else:
        form = ProjectTicketForm(
            initial={
                'title': ticket.title,
                'description': ticket.description,
                'status': ticket.status,
                'priority': ticket.priority,
                'assigned_to': ticket.assigned_to_id,
            },
            project=project,
        )
    existing_links = list(ticket.links.all())
    link_rows = [
        {'label': l.label, 'url': l.url, 'note': l.note} for l in existing_links
    ]
    while len(link_rows) < 5:
        link_rows.append({'label': '', 'url': '', 'note': ''})
    visible_attachments = [
        a
        for a in ticket.attachments.all()
        if ticket_service.user_can_view_attachment(user, a) and not a.is_reference_screenshot
    ]
    reference_screenshots = [
        a
        for a in ticket.attachments.all()
        if a.is_reference_screenshot and ticket_service.user_can_view_attachment(user, a)
    ]
    return render(
        request,
        'crm/tickets/detail.html',
        {
            'ticket': ticket,
            'project': project,
            'form': form,
            'link_rows': link_rows,
            'visible_attachments': visible_attachments,
            'reference_screenshots': reference_screenshots,
            'visibility_choices': ProjectTicketAttachment.Visibility.choices,
            'team_users': ticket_service.project_member_users(project),
        },
    )


@login_required
def ticket_member_select(request):
    """HTMX: assignee dropdown for selected project."""
    user = request.user
    project_pk = request.GET.get('project')
    project = get_object_or_404(get_projects_for_user(user), pk=project_pk)
    return render(
        request,
        'crm/tickets/partials/member_select.html',
        {
            'members': ticket_service.project_member_users(project),
            'selected': request.GET.get('assigned_to'),
        },
    )


@login_required
@require_POST
def ticket_move(request, pk):
    ticket = get_object_or_404(ticket_service.get_tickets_for_user(request.user), pk=pk)
    new_status = (request.POST.get('status') or '').strip()
    try:
        position = int(request.POST.get('position', 0))
    except (TypeError, ValueError):
        position = None
    try:
        ticket_service.move_ticket(ticket, new_status=new_status, position=position)
    except ValueError:
        pass
    if request.headers.get('HX-Request'):
        qs, proj, _ = _apply_ticket_filters(request, request.user)
        return render(
            request,
            'crm/tickets/partials/board.html',
            _ticket_board_ctx(request, proj, qs),
        )
    return redirect('crm:tickets' + ('?' + request.GET.urlencode() if request.GET else '?view=board'))


@login_required
def tickets_board_partial(request):
    """HTMX: kanban board fragment for current filters."""
    qs, project, _ = _apply_ticket_filters(request, request.user)
    return render(
        request,
        'crm/tickets/partials/board.html',
        _ticket_board_ctx(request, project, qs),
    )


@login_required
@require_POST
def ticket_upload(request, pk):
    ticket = get_object_or_404(
        ticket_service.get_tickets_for_user(request.user), pk=pk
    )
    is_ref = request.POST.get('is_reference') in ('1', 'on', 'true')
    uploads = request.FILES.getlist('file') or []
    single = request.FILES.get('file')
    if single and single not in uploads:
        uploads.append(single)
    for uploaded in uploads:
        if uploaded:
            ticket_service.add_attachment(
                ticket,
                uploaded_file=uploaded,
                uploaded_by=request.user,
                visibility=request.POST.get('visibility') or 'team',
                save_to_desk=request.POST.get('save_to_desk') in ('1', 'on', 'true'),
                is_reference_screenshot=is_ref,
            )
    if request.headers.get('HX-Request'):
        if request.headers.get('HX-Board-Refresh'):
            qs, project, _ = _apply_ticket_filters(request, request.user)
            return render(
                request,
                'crm/tickets/partials/board.html',
                _ticket_board_ctx(request, project, qs),
            )
        ticket.refresh_from_db()
        user = request.user
        visible = [
            a
            for a in ticket.attachments.all()
            if ticket_service.user_can_view_attachment(user, a)
            and not a.is_reference_screenshot
        ]
        refs = [
            a
            for a in ticket.attachments.all()
            if a.is_reference_screenshot
            and ticket_service.user_can_view_attachment(user, a)
        ]
        if is_ref:
            return render(
                request,
                'crm/tickets/partials/reference_gallery.html',
                {'reference_screenshots': refs},
            )
        return render(
            request,
            'crm/tickets/partials/attachments.html',
            {'attachments': visible},
        )
    return redirect('crm:ticket_detail', pk=pk)
