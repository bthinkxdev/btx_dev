"""
Projects, milestones, tasks, timesheets, blockers, change requests and tickets (Part 10).

Status changes go through the functions here so the rules hold everywhere (board drag, list,
API): Blocked needs a reason and opens a blocker; Done respects the reviewer; time cannot pass
24 hours a day or land in a submitted week; a project closes only when its checklist is green.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, india, notify, numbering, permissions
from crm.core.money import ZERO, q2
from crm.models import (Blocker, ChangeRequest, Milestone, Project, ProjectMember, Task, Ticket, TimeEntry)


class ProjectError(ValidationError):
    pass


def project_url(p) -> str:
    return reverse('crm:project_detail', args=[p.pk])


def is_pm(user, project) -> bool:
    return project.manager_id == user.pk or permissions.get_scope(user, 'projects', 'approve') in ('team', 'department', 'company')


# ---------------------------------------------------------------- projects

def create_project(actor, *, client, name, manager, start_date, due_date, budget_hours=Decimal('0'), budget_amount=ZERO,
                   scope='', acceptance_criteria='', contract=None, quotation=None, lead=None, service=None,
                   use_template=True) -> Project:
    if due_date < start_date:
        raise ProjectError('The due date must be after the start date.')
    contract_value = ZERO
    if contract is not None:
        contract_value = contract.value
    elif quotation is not None:
        contract_value = quotation.taxable_total
    with transaction.atomic():
        p = Project.objects.create(code=numbering.allocate_simple('PRJ'), name=name, client=client, contract=contract,
                                   quotation=quotation, lead=lead, service=service, manager=manager, start_date=start_date,
                                   due_date=due_date, budget_hours=budget_hours or 0, budget_amount=budget_amount or ZERO,
                                   contract_value=contract_value, scope=scope, acceptance_criteria=acceptance_criteria, created_by=actor)
        ProjectMember.objects.create(project=p, user=manager, role='Project manager', allocation_percent=0, start_date=start_date)
        if use_template and service is not None and service.task_template:
            for i, title in enumerate(t.strip() for t in service.task_template.splitlines()):
                if title:
                    Task.objects.create(project=p, title=title[:200], sort=i, created_by=actor)
        if quotation is not None:
            for m in quotation.milestones.all():
                Milestone.objects.create(project=p, name=m.name[:150], due_date=due_date,
                                         amount=q2(quotation.taxable_total * m.percent / 100))
        audit.log('project.created', p, actor=actor, changes={'code': p.code, 'client': str(client), 'manager': str(manager)})
        if quotation is not None:
            from crm.modules.quotations.services import mark_converted
            mark_converted(quotation, actor, 'project')
    notify.notify([manager], f'You manage new project {p}', url=project_url(p), event_type='project_assigned')
    return p


def set_status(p: Project, actor, status: str, reason: str = ''):
    allowed = {'planned': ('active', 'cancelled', 'on_hold'), 'active': ('on_hold', 'cancelled'),
               'on_hold': ('active', 'cancelled'), 'completed': ('active',)}
    if status not in allowed.get(p.status, ()):
        raise ProjectError(f'A {p.get_status_display().lower()} project cannot move to {status.replace("_", " ")}.')
    if status in ('on_hold', 'cancelled') and len(reason.strip()) < 10:
        raise ProjectError('Give a reason of at least 10 characters.')
    Project.objects.filter(pk=p.pk).update(status=status, hold_reason=reason[:300] if status == 'on_hold' else '')
    audit.log('project.status', p, actor=actor, changes={'from': p.status, 'to': status}, reason=reason)


def close_checklist(p: Project) -> list[dict]:
    items = []
    open_ms = [m.name for m in p.milestones.exclude(status='accepted')]
    items.append({'label': 'All milestones accepted', 'ok': not open_ms, 'detail': ', '.join(open_ms)})
    open_tasks = p.tasks.exclude(status__in=['done', 'cancelled']).count()
    items.append({'label': 'No open tasks', 'ok': open_tasks == 0, 'detail': f'{open_tasks} open' if open_tasks else ''})
    unbilled = p.change_requests.filter(status='approved').count()
    items.append({'label': 'No unbilled approved scope changes', 'ok': unbilled == 0, 'detail': f'{unbilled} to bill' if unbilled else ''})
    pending_time = p.time_entries.filter(status__in=['draft', 'submitted']).count()
    items.append({'label': 'Timesheets approved', 'ok': pending_time == 0, 'detail': f'{pending_time} entries' if pending_time else ''})
    open_blockers = Blocker.objects.filter(task__project=p).exclude(status='resolved').count()
    items.append({'label': 'No open blockers', 'ok': open_blockers == 0, 'detail': f'{open_blockers} open' if open_blockers else ''})
    return items


def request_close(p: Project, actor, lessons: str = ''):
    if p.status not in ('active', 'completed', 'on_hold'):
        raise ProjectError('Only an active or completed project can be closed.')
    if not is_pm(actor, p):
        raise ProjectError('Only the project manager can close the project.')
    red = [i['label'] + (f' ({i["detail"]})' if i.get('detail') else '') for i in close_checklist(p) if not i['ok']]
    if red:
        raise ProjectError('Not ready to close: ' + '; '.join(red) + '.')
    from crm.modules.reports.services import project_cost
    cost = project_cost(p)
    with transaction.atomic():
        Project.objects.filter(pk=p.pk).update(status='completed', lessons=lessons)
        approvals.submit('project_close', p, title=f'Close project {p}', module='projects', requested_by=actor, level='finance',
                         reasons=['PM and finance sign-off'], after={'final cost': str(cost['total'])}, message=lessons)


def _close_approved(req, user):
    Project.objects.filter(pk=req.object_id).update(status='closed', closed_at=timezone.now())
    audit.log('project.closed', Project.objects.get(pk=req.object_id), actor=user)


def _close_rejected(req, user):
    Project.objects.filter(pk=req.object_id, status='completed').update(status='active')


approvals.register('project_close', _close_approved, _close_rejected)


def add_member(p: Project, actor, *, user, role='', allocation_percent=100, start_date=None, end_date=None, cost_rate=ZERO):
    m, created = ProjectMember.objects.update_or_create(project=p, user=user, defaults={
        'role': role, 'allocation_percent': allocation_percent, 'start_date': start_date, 'end_date': end_date, 'cost_rate': cost_rate})
    audit.log('project.member_saved', p, actor=actor, changes={'user': str(user), 'allocation': allocation_percent})
    return m, total_allocation(user)


def total_allocation(user, on: date | None = None) -> int:
    on = on or india.today()
    qs = ProjectMember.objects.filter(user=user, project__status__in=['planned', 'active'])
    total = 0
    for m in qs:
        if (m.start_date and m.start_date > on) or (m.end_date and m.end_date < on):
            continue
        total += m.allocation_percent
    return total


def change_deadline(p: Project, actor, *, new_date: date, reason: str):
    if len(reason.strip()) < 10:
        raise ProjectError('Give a reason of at least 10 characters.')
    if new_date < p.start_date:
        raise ProjectError('The due date cannot be before the start date.')
    old = p.due_date
    Project.objects.filter(pk=p.pk).update(due_date=new_date)
    late_ms = p.milestones.exclude(status='accepted').filter(due_date__lt=new_date)
    audit.log('project.deadline_changed', p, actor=actor, changes={'due': [old, new_date]}, reason=reason)
    if late_ms.exists():
        notify.notify([p.manager], f'{p}: deadline moved past {late_ms.count()} milestone date(s)', body=reason, url=project_url(p),
                      event_type='project_risk', level='warning')


# ---------------------------------------------------------------- tasks

def _member_ids(p: Project) -> set[int]:
    return set(p.members.values_list('user_id', flat=True)) | {p.manager_id}


def check_dependencies(task: Task, deps) -> None:
    """A task cannot depend on itself through any chain."""
    targets = {d.pk for d in deps}
    if task.pk in targets:
        raise ProjectError('A task cannot depend on itself.')
    seen, stack = set(), list(targets)
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        for nxt in Task.objects.filter(pk=t).values_list('depends_on', flat=True):
            if nxt is None:
                continue
            if nxt == task.pk:
                raise ProjectError('This dependency would create a loop between tasks.')
            stack.append(nxt)


def save_task(task: Task, actor, *, deps=None, is_new: bool):
    p = task.project
    if task.assignee_id and task.assignee_id not in _member_ids(p):
        raise ProjectError('The assignee must be a member of the project.')
    if task.reviewer_id and task.reviewer_id == task.assignee_id:
        raise ProjectError('The reviewer cannot be the assignee.')
    with transaction.atomic():
        if not task.pk:
            task.created_by = actor
        task.updated_by = actor
        task.save()
        if deps is not None:
            check_dependencies(task, deps)
            task.depends_on.set(deps)
        audit.log('task.created' if is_new else 'task.saved', task, actor=actor, changes={'assignee': str(task.assignee or '')})
    if is_new and task.assignee_id and task.assignee_id != actor.pk:
        notify.notify([task.assignee], f'New task: {task.title}', body=str(p), url=reverse('crm:task_detail', args=[task.pk]),
                      event_type='task_assigned')
    return task


TRANSITIONS = {
    'todo': ('in_progress', 'blocked', 'cancelled', 'done'),
    'in_progress': ('todo', 'blocked', 'in_review', 'done', 'cancelled'),
    'blocked': ('in_progress', 'todo', 'cancelled'),
    'in_review': ('in_progress', 'done'),
    'done': ('in_progress',),
    'cancelled': ('todo',),
}


def set_task_status(task: Task, actor, status: str, *, reason: str = '', blocker_kind: str = 'other', blocker_owner=None):
    if status not in TRANSITIONS.get(task.status, ()):
        raise ProjectError(f'A task cannot move from {task.get_status_display()} to {dict(Task._meta.get_field("status").choices).get(status, status)}.')
    if status == 'blocked' and len(reason.strip()) < 5:
        raise ProjectError('Say what is blocking the task.')
    if status in ('in_progress', 'in_review', 'done'):
        waiting = [d.title for d in task.depends_on.exclude(status__in=['done', 'cancelled'])]
        if waiting:
            raise ProjectError('Finish the tasks this depends on first: ' + ', '.join(waiting[:5]) + '.')
    if status == 'done' and task.reviewer_id:
        if task.status != 'in_review':
            raise ProjectError('This task has a reviewer: move it to In review first.')
        if actor.pk != task.reviewer_id and not is_pm(actor, task.project):
            raise ProjectError('Only the reviewer or the project manager can mark it done.')
    with transaction.atomic():
        t = Task.objects.select_for_update().get(pk=task.pk)
        old = t.status
        t.status = status
        t.blocked_reason = reason[:300] if status == 'blocked' else ''
        t.completed_at = timezone.now() if status == 'done' else None
        t.updated_by = actor
        t.save(update_fields=['status', 'blocked_reason', 'completed_at', 'updated_by', 'updated_at'])
        if status == 'blocked':
            Blocker.objects.create(task=t, kind=blocker_kind, description=reason[:500], owner=blocker_owner or t.project.manager)
        if old == 'blocked' and status != 'blocked':
            Blocker.objects.filter(task=t).exclude(status='resolved').update(status='resolved', resolved_at=timezone.now(),
                                                                              resolution=f'Task moved to {status}')
        audit.log('task.status', t, actor=actor, changes={'from': old, 'to': status}, reason=reason)
    if status == 'in_review' and t.reviewer_id:
        notify.notify([t.reviewer], f'Review: {t.title}', url=reverse('crm:task_detail', args=[t.pk]), event_type='task_review')
    if status == 'blocked':
        notify.notify([t.project.manager], f'Blocked: {t.title}', body=reason, url=reverse('crm:task_detail', args=[t.pk]),
                      event_type='task_blocked', level='warning')
    return t


def resolve_blocker(b: Blocker, actor, resolution: str):
    if len(resolution.strip()) < 5:
        raise ProjectError('Describe how it was resolved.')
    Blocker.objects.filter(pk=b.pk).update(status='resolved', resolved_at=timezone.now(), resolution=resolution[:300])
    audit.log('blocker.resolved', b.task, actor=actor, reason=resolution)


def escalate_blockers() -> int:
    """Open more than 2 days escalates to the PM (Part 10.1)."""
    n = 0
    for b in Blocker.objects.filter(status='open', created_at__lte=timezone.now() - timedelta(days=2)).select_related('task__project'):
        Blocker.objects.filter(pk=b.pk).update(status='escalated')
        p = b.task.project
        notify.notify([p.manager, notify.manager_of(p.manager)], f'Blocker open 2+ days: {b.task.title}', body=b.description,
                      url=reverse('crm:task_detail', args=[b.task_id]), event_type='blocker_escalated', level='warning',
                      dedupe_key=f'blocker-esc:{b.pk}')
        n += 1
    return n


# ---------------------------------------------------------------- milestones

def mark_ready(m: Milestone, actor):
    if m.status not in ('pending', 'in_progress', 'rejected'):
        raise ProjectError('This milestone cannot be marked ready.')
    Milestone.objects.filter(pk=m.pk).update(status='ready_for_review')
    audit.log('milestone.ready', m.project, actor=actor, changes={'milestone': m.name})
    notify.notify([m.project.manager], f'Milestone ready for review: {m.name}', url=project_url(m.project), event_type='milestone_ready')


def accept_milestone(m: Milestone, actor, *, evidence: str, invoice_action: str = 'none'):
    if m.status != 'ready_for_review':
        raise ProjectError('Mark the milestone ready for review first.')
    if not permissions.has_perm(actor, 'projects', 'approve'):
        permissions.denied(actor, 'projects', 'approve', detail='accept milestone')
    if m.tasks.filter(assignee=actor).exists():
        raise ProjectError('You worked on this milestone, so someone else must accept it.')
    if len(evidence.strip()) < 5:
        raise ProjectError('Record the acceptance evidence (who accepted and how).')
    inv = None
    with transaction.atomic():
        Milestone.objects.filter(pk=m.pk).update(status='accepted', accepted_by=actor, accepted_at=timezone.now(), evidence=evidence)
        audit.log('milestone.accepted', m.project, actor=actor, changes={'milestone': m.name, 'invoice': invoice_action})
        row = m.billing_rows.filter(status__in=['scheduled', 'failed']).first()
        if row is not None:
            from crm.modules.contracts.services import invoice_for_row
            inv = invoice_for_row(row, actor, issue=False)
        elif invoice_action == 'draft_invoice' and m.amount > 0:
            inv = _milestone_invoice(m, actor)
        if inv is not None:
            Milestone.objects.filter(pk=m.pk).update(invoice=inv)
    return inv


def _milestone_invoice(m: Milestone, actor):
    from crm.models import InvoiceLine
    from crm.modules.billing import services as bs
    p = m.project
    inv = bs.prefill(client=p.client, owner=actor, quotation=None)
    inv.project = p
    inv.contract = p.contract
    inv.milestone_ref = f'{p.code} milestone: {m.name}'[:120]
    inv.created_by = actor
    inv.save()
    svc = p.service
    rate = svc.tax_for(inv.invoice_date) if svc else ('taxable', Decimal('18'))
    InvoiceLine.objects.create(invoice=inv, service=svc, description=inv.milestone_ref, sac_code=svc.sac_code if svc else '998314',
                               quantity=1, unit_price=m.amount, taxability=rate[0], tax_rate=rate[1])
    bs.recalculate(inv)
    return inv


def reject_milestone(m: Milestone, actor, comment: str):
    if m.status != 'ready_for_review':
        raise ProjectError('Only a milestone in review can be rejected.')
    if len(comment.strip()) < 10:
        raise ProjectError('Give a comment of at least 10 characters.')
    Milestone.objects.filter(pk=m.pk).update(status='rejected', rejection_comment=comment[:300])
    audit.log('milestone.rejected', m.project, actor=actor, changes={'milestone': m.name}, reason=comment)


# ---------------------------------------------------------------- timesheets

def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_locked(user, d: date) -> bool:
    ws = week_start(d)
    return TimeEntry.objects.filter(user=user, entry_date__range=(ws, ws + timedelta(days=6)), status__in=['submitted', 'approved']).exists()


def log_time(user, *, project: Project, entry_date: date, hours: Decimal, task: Task | None = None, billable: bool = True,
             note: str = '', entry: TimeEntry | None = None) -> TimeEntry:
    hours = Decimal(hours)
    if hours <= 0:
        raise ProjectError('Enter hours above 0.')
    if entry_date > india.today():
        raise ProjectError('Time cannot be logged for a future date.')
    if user.pk not in _member_ids(project):
        raise ProjectError('You are not a member of this project.')
    if project.status not in ('planned', 'active', 'on_hold'):
        raise ProjectError('This project is not open for time.')
    if task is not None and task.project_id != project.pk:
        raise ProjectError('The task belongs to another project.')
    if week_locked(user, entry_date):
        raise ProjectError('That week is already submitted. Ask your manager to reject it to make changes.')
    with transaction.atomic():
        others = TimeEntry.objects.select_for_update().filter(user=user, entry_date=entry_date)
        if entry is not None:
            others = others.exclude(pk=entry.pk)
        day = (others.aggregate(s=Sum('hours'))['s'] or Decimal('0')) + hours
        if day > 24:
            raise ProjectError(f'That makes {day.normalize():f} hours on {entry_date:%d-%b-%Y}. A day has at most 24 hours.')
        if entry is None:
            entry = TimeEntry(user=user)
        entry.project, entry.task, entry.entry_date, entry.hours, entry.billable, entry.note = project, task, entry_date, hours, billable, note[:300]
        entry.status = 'draft'
        entry.save()
    return entry


def delete_time(entry: TimeEntry, user):
    if entry.user_id != user.pk or entry.status != 'draft':
        raise ProjectError('Only your own draft time can be deleted.')
    entry.delete()


def submit_week(user, ws: date) -> int:
    ws = week_start(ws)
    n = TimeEntry.objects.filter(user=user, entry_date__range=(ws, ws + timedelta(days=6)), status__in=['draft', 'rejected']) \
        .update(status='submitted', reject_comment='')
    if not n:
        raise ProjectError('There is no draft time in this week.')
    audit.log('timesheet.submitted', actor=user, object_type='timesheet', object_id=f'{user.pk}:{ws}', changes={'entries': n})
    managers = {e.project.manager for e in TimeEntry.objects.filter(user=user, entry_date__range=(ws, ws + timedelta(days=6))).select_related('project__manager')}
    notify.notify([m for m in managers if m.pk != user.pk], f'Timesheet submitted: {user.get_full_name() or user} week of {ws:%d-%b}',
                  url=reverse('crm:timesheet_approvals'), event_type='timesheet_submitted')
    return n


def decide_entries(entries, approver, *, approve: bool, comment: str = '') -> int:
    if not approve and len(comment.strip()) < 10:
        raise ProjectError('Give a comment of at least 10 characters when rejecting.')
    n = 0
    for e in entries:
        if e.user_id == approver.pk:
            raise ProjectError('You cannot approve your own time.')
        if e.status != 'submitted':
            continue
        if not is_pm(approver, e.project):
            raise ProjectError(f'You do not manage {e.project}.')
        TimeEntry.objects.filter(pk=e.pk).update(status='approved' if approve else 'rejected', approved_by=approver,
                                                 decided_at=timezone.now(), reject_comment='' if approve else comment[:300])
        n += 1
    audit.log('timesheet.approved' if approve else 'timesheet.rejected', actor=approver, object_type='timesheet', object_id='',
              changes={'entries': n}, reason=comment)
    return n


# ---------------------------------------------------------------- change requests (9.4, 10.1)

def submit_change(cr: ChangeRequest, actor):
    if cr.status != 'draft':
        raise ProjectError('Only a draft change request can be submitted.')
    if cr.price < 0:
        raise ProjectError('The price cannot be negative.')
    with transaction.atomic():
        ChangeRequest.objects.filter(pk=cr.pk).update(status='submitted')
        approvals.submit('change_request', cr, title=f'Scope change on {cr.project}: {cr.title}', module='projects', requested_by=actor,
                         level='manager', amount=cr.price, reasons=['Additional scope'],
                         after={'price': str(cr.price), 'effort hours': str(cr.effort_hours)}, message=cr.description[:500])


def _cr_approved(req, user):
    ChangeRequest.objects.filter(pk=req.object_id, status='submitted').update(status='approved', decided_by=user)


def _cr_rejected(req, user):
    ChangeRequest.objects.filter(pk=req.object_id, status='submitted').update(status='rejected', decided_by=user)


approvals.register('change_request', _cr_approved, _cr_rejected)


def bill_change(cr: ChangeRequest, actor):
    """A new priced invoice line; the original contract and project value are untouched."""
    from crm.models import InvoiceLine
    from crm.modules.billing import services as bs
    if cr.status != 'approved':
        raise ProjectError('Only an approved change request can be billed.')
    with transaction.atomic():
        cr = ChangeRequest.objects.select_for_update().get(pk=cr.pk)
        if cr.invoice_id:
            raise ProjectError('This change request is already billed.')
        p = cr.project
        inv = bs.prefill(client=p.client, owner=actor)
        inv.project, inv.contract = p, p.contract
        inv.milestone_ref = f'Change request: {cr.title}'[:120]
        inv.created_by = actor
        inv.save()
        sac = p.service.sac_code if p.service_id else '998314'
        InvoiceLine.objects.create(invoice=inv, service=p.service, description=f'Change request: {cr.title}'[:500], sac_code=sac,
                                   quantity=1, unit_price=cr.price, tax_rate=cr.tax_rate)
        bs.recalculate(inv)
        cr.status, cr.invoice = 'billed', inv
        cr.save(update_fields=['status', 'invoice'])
        audit.log('change_request.billed', p, actor=actor, changes={'cr': cr.pk, 'invoice': inv.pk})
    return inv


# ---------------------------------------------------------------- tickets (10.5)

SLA_HOURS = {'urgent': 4, 'high': 8, 'medium': 24, 'low': 72}


def create_ticket(actor, *, client, subject, description='', priority='medium', assignee=None, project=None, contract=None) -> Ticket:
    if contract is None and project is not None and project.contract_id:
        contract = project.contract
    if contract is None:
        from crm.models import Contract
        contract = Contract.objects.filter(client=client, status='active', lines__billing_model='amc').first()
    with transaction.atomic():
        t = Ticket.objects.create(number=numbering.allocate_simple('TKT'), client=client, subject=subject, description=description,
                                  priority=priority, assignee=assignee, project=project, contract=contract,
                                  sla_due_at=timezone.now() + timedelta(hours=SLA_HOURS.get(priority, 24)), created_by=actor)
        audit.log('ticket.created', t, actor=actor, changes={'priority': priority})
    if assignee and assignee != actor:
        notify.notify([assignee], f'Ticket {t.number}: {subject}', url=reverse('crm:ticket_detail', args=[t.pk]), event_type='ticket_assigned')
    return t


TICKET_FLOW = {'open': ('in_progress', 'waiting', 'resolved'), 'in_progress': ('waiting', 'resolved', 'open'),
               'waiting': ('in_progress', 'resolved'), 'resolved': ('closed', 'in_progress'), 'closed': ('in_progress',)}


def set_ticket_status(t: Ticket, actor, status: str, note: str = ''):
    if status not in TICKET_FLOW.get(t.status, ()):
        raise ProjectError('That status change is not allowed.')
    if status == 'resolved' and len(note.strip()) < 5:
        raise ProjectError('Describe the resolution.')
    Ticket.objects.filter(pk=t.pk).update(status=status, resolved_at=timezone.now() if status == 'resolved' else t.resolved_at,
                                          updated_by=actor)
    audit.log('ticket.status', t, actor=actor, changes={'from': t.status, 'to': status}, reason=note)


def ticket_sla() -> int:
    n = 0
    for t in Ticket.objects.filter(status__in=['open', 'in_progress'], sla_due_at__lt=timezone.now(), breach_alerted=False) \
            .select_related('project__manager', 'assignee'):
        Ticket.objects.filter(pk=t.pk).update(breach_alerted=True)
        users = [t.assignee, t.project.manager if t.project_id else None] + list(notify.users_with_perm('tickets', 'assign'))[:3]
        notify.notify([u for u in users if u], f'SLA breached: {t.number} {t.subject}', url=reverse('crm:ticket_detail', args=[t.pk]),
                      event_type='sla_breach', level='error', dedupe_key=f'ticket-sla:{t.pk}')
        n += 1
    return n
