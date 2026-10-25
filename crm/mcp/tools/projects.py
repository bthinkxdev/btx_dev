"""Project, task and ticket tools."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.projects import services as svc

PROJECT_FIELDS = ('name', 'client', 'manager', 'status', 'start_date', 'due_date', 'created_at')
TASK_FIELDS = ('project', 'title', 'assignee', 'status', 'due_date', 'created_at')
TICKET_FIELDS = ('client', 'subject', 'status', 'priority', 'assignee', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_projects(status: str = '', limit: int = 50) -> list[dict]:
        """List projects, optionally filtered by status (planned/active/on_hold/closed/cancelled)."""
        from crm.models import Project
        user = context.current_user()
        qs = permissions.scoped(Project.objects.all(), user, 'projects', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), PROJECT_FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def create_project(client_id: int, name: str, manager_username: str, start_date_iso: str,
                       due_date_iso: str = '') -> dict:
        """Create a project for a client. manager_username must be an existing CRM user."""
        from crm.models import Client
        user = context.current_user()
        permissions.require(user, 'projects', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        manager = get_user_model().objects.get(username=manager_username)
        p = svc.create_project(user, client=client, name=name, manager=manager,
                               start_date=date.fromisoformat(start_date_iso),
                               due_date=date.fromisoformat(due_date_iso) if due_date_iso else None)
        return serialize.row(p, PROJECT_FIELDS)

    @helpers.sync_tool(mcp)
    def add_project_member(project_id: int, username: str, allocation_percent: int = 100) -> dict:
        """Add a team member to a project."""
        from crm.models import Project
        user = context.current_user()
        p = helpers.get_scoped(user, Project, project_id, 'projects', 'edit')
        member = get_user_model().objects.get(username=username)
        svc.add_member(p, user, user=member, allocation_percent=allocation_percent)
        return {'ok': True}

    @helpers.sync_tool(mcp)
    def list_tasks(project_id: int = 0, status: str = '', limit: int = 100) -> list[dict]:
        """List tasks, optionally filtered by project and/or status."""
        from crm.models import Task
        user = context.current_user()
        qs = permissions.scoped(Task.objects.all(), user, 'tasks', 'view')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), TASK_FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def create_task(project_id: int, title: str, assignee_username: str = '', due_date_iso: str = '') -> dict:
        """Create a task on a project. assignee_username, if given, must already be a project member."""
        from crm.models import Project, Task
        user = context.current_user()
        project = helpers.get_scoped(user, Project, project_id, 'projects', 'edit')
        assignee = get_user_model().objects.get(username=assignee_username) if assignee_username else None
        task = Task(project=project, title=title, assignee=assignee,
                   due_date=date.fromisoformat(due_date_iso) if due_date_iso else None)
        try:
            task = svc.save_task(task, user, is_new=True)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(task, TASK_FIELDS)

    @helpers.sync_tool(mcp)
    def set_task_status(task_id: int, status: str, reason: str = '') -> dict:
        """Move a task to a new status (todo/in_progress/blocked/in_review/done/cancelled)."""
        from crm.models import Task
        user = context.current_user()
        task = helpers.get_scoped(user, Task, task_id, 'tasks', 'edit')
        try:
            svc.set_task_status(task, user, status, reason=reason)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        task.refresh_from_db()
        return serialize.row(task, TASK_FIELDS)

    @helpers.sync_tool(mcp)
    def log_time(project_id: int, hours: str, entry_date_iso: str = '', task_id: int = 0) -> dict:
        """Log a timesheet entry for the current period against a project (and optionally a task)."""
        from crm.core import india
        from crm.models import Project, Task
        user = context.current_user()
        project = helpers.get_scoped(user, Project, project_id, 'projects', 'view')
        task = helpers.get_scoped(user, Task, task_id, 'tasks', 'view') if task_id else None
        entry = svc.log_time(user, project=project, task=task,
                             entry_date=date.fromisoformat(entry_date_iso) if entry_date_iso else india.today(),
                             hours=Decimal(hours))
        return {'ok': True, 'entry_id': entry.pk}

    @helpers.sync_tool(mcp)
    def list_tickets(status: str = '', limit: int = 50) -> list[dict]:
        """List support tickets, optionally filtered by status."""
        from crm.models import Ticket
        user = context.current_user()
        qs = permissions.scoped(Ticket.objects.all(), user, 'tickets', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), TICKET_FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def create_ticket(client_id: int, subject: str, description: str = '', priority: str = 'medium',
                      assignee_username: str = '', project_id: int = 0) -> dict:
        """Raise a support ticket for a client."""
        from crm.models import Client, Project
        user = context.current_user()
        permissions.require(user, 'tickets', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        assignee = get_user_model().objects.get(username=assignee_username) if assignee_username else None
        project = helpers.get_scoped(user, Project, project_id, 'projects', 'view') if project_id else None
        t = svc.create_ticket(user, client=client, subject=subject, description=description, priority=priority,
                              assignee=assignee, project=project)
        return serialize.row(t, TICKET_FIELDS)

    @helpers.sync_tool(mcp)
    def set_ticket_status(ticket_id: int, status: str, note: str = '') -> dict:
        """Update a support ticket's status (open/in_progress/waiting/resolved/closed)."""
        from crm.models import Ticket
        user = context.current_user()
        t = helpers.get_scoped(user, Ticket, ticket_id, 'tickets', 'edit')
        try:
            svc.set_ticket_status(t, user, status, note)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        t.refresh_from_db()
        return serialize.row(t, TICKET_FIELDS)
