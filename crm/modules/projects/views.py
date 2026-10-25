"""Project, task, timesheet, blocker and ticket pages (Part 10.2-10.5)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, india, permissions
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import (AuditLog, Blocker, ChangeRequest, Client, Contract, Milestone, Project, ProjectMember, Quotation, Task,
                        Ticket, TimeEntry)
from crm.models.delivery import PROJECT_STATUSES, TASK_PRIORITIES, TASK_STATUSES, TICKET_STATUSES

from . import services as svc
from .forms import (AcceptMilestoneForm, ChangeRequestForm, DeadlineForm, MemberForm, MilestoneForm, ProjectForm, TaskForm,
                    TicketForm, TimeForm)


# ---------------------------------------------------------------- portfolio

class ProjectList(ListPage):
    model = Project
    module = 'projects'
    template_name = 'crm/projects/list.html'
    title = 'Projects'
    search_fields = ('code__icontains', 'name__icontains', 'client__display_name__icontains')
    select_related = ('client', 'manager')
    sort_fields = {'due': 'due_date', 'start': 'start_date'}
    filters = [Filter('status', 'Status', PROJECT_STATUSES)]
    quick_filters = {'active': ('Active', lambda qs, r: qs.filter(status='active')),
                     'mine': ('I manage', lambda qs, r: qs.filter(manager=r.user)),
                     'late': ('Past due', lambda qs, r: qs.filter(status__in=['planned', 'active'], due_date__lt=india.today()))}
    export_name = 'projects'
    export_columns = [('Code', 'code'), ('Name', 'name'), ('Client', 'client'), ('PM', 'manager'), ('Status', lambda p: p.get_status_display()),
                      ('Start', 'start_date'), ('Due', 'due_date'), ('Budget hours', 'budget_hours'), ('Hours logged', 'hours_logged')]

    def base_queryset(self):
        return super().base_queryset().distinct().prefetch_related('milestones')

    def extra_context(self, qs):
        return {'module_code': 'projects', 'can_create': permissions.has_perm(self.request.user, 'projects', 'create'),
                'search_placeholder': 'Search code, name, client', 'today': india.today()}


def get_project(request, pk, action='view'):
    return get_scoped(request, Project, pk, 'projects', action,
                      queryset=Project.objects.select_related('client', 'manager', 'contract', 'quotation', 'service').distinct())


def _clients(request):
    return permissions.scoped(Client.objects.filter(archived_at__isnull=True), request.user, 'clients', 'view').order_by('display_name')


@staff_required
def project_create(request):
    permissions.require(request.user, 'projects', 'create', request)
    initial = {'manager': request.user.pk, 'start_date': india.today(), 'due_date': india.today() + timedelta(days=30)}
    contract = quotation = None
    if request.GET.get('contract') or request.POST.get('src_contract'):
        contract = get_scoped(request, Contract, request.GET.get('contract') or request.POST.get('src_contract'), 'contracts', 'view')
        initial.update(client=contract.client_id, name=contract.title, scope=contract.scope,
                       budget_amount=contract.value, start_date=max(contract.start_date, india.today()))
        if contract.end_date:
            initial['due_date'] = contract.end_date
        svc_line = contract.lines.filter(service__isnull=False).first()
        if svc_line:
            initial['service'] = svc_line.service_id
    elif request.GET.get('quotation') or request.POST.get('src_quotation'):
        quotation = get_scoped(request, Quotation, request.GET.get('quotation') or request.POST.get('src_quotation'), 'quotations', 'view')
        if quotation.status not in ('accepted', 'converted'):
            messages.error(request, 'Only an accepted quotation can start a project.')
            return redirect('crm:quotation_detail', pk=quotation.pk)
        client = quotation.client
        if client is None and quotation.lead_id:
            from crm.modules.clients.services import ensure_client_for_lead
            client = ensure_client_for_lead(quotation.lead, request.user)
        line = quotation.lines.filter(service__isnull=False).first()
        initial.update(client=client.pk if client else None, name=quotation.title or f'Project for {client}', scope=quotation.deliverables,
                       budget_amount=quotation.taxable_total, service=line.service_id if line else None,
                       budget_hours=line.service.estimated_hours if line else 0)
    elif request.GET.get('client'):
        initial['client'] = get_scoped(request, Client, request.GET['client'], 'clients', 'view').pk
    form = ProjectForm(request.POST or None, initial=initial, clients=_clients(request))
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        if contract and c['budget_amount'] > contract.value and not request.POST.get('budget_note'):
            form.add_error('budget_amount', 'The budget is above the contract value. Add a note to explain.')
        else:
            try:
                p = svc.create_project(request.user, client=c['client'], name=c['name'], manager=c['manager'], start_date=c['start_date'],
                                       due_date=c['due_date'], budget_hours=c['budget_hours'], budget_amount=c['budget_amount'],
                                       scope=c['scope'], acceptance_criteria=c['acceptance_criteria'], contract=contract,
                                       quotation=quotation, lead=quotation.lead if quotation else None, service=c.get('service'),
                                       use_template=bool(request.POST.get('use_template')))
                messages.success(request, f'Project {p.code} created.')
                return redirect('crm:project_detail', pk=p.pk)
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
    elif request.method == 'POST':
        messages.error(request, 'Please fix the highlighted problems.')
    return render(request, 'crm/projects/form.html', {'form': form, 'contract': contract, 'quotation': quotation, 'is_new': True})


@staff_required
def project_edit(request, pk):
    p = get_project(request, pk, 'edit')
    form = ProjectForm(request.POST or None, instance=p, clients=_clients(request))
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        audit.log('project.saved', obj, changes={'fields': form.changed_data})
        messages.success(request, 'Project saved.')
        return redirect('crm:project_detail', pk=p.pk)
    return render(request, 'crm/projects/form.html', {'form': form, 'p': p, 'is_new': False})


def _detail_ctx(request, p, forms=None, open_modal=''):
    user = request.user
    show_cost = permissions.can_see(user, 'cost_price')
    forms = forms or {}
    defaults = {'task': TaskForm(prefix='task', project=p, instance=Task(project=p)), 'milestone': MilestoneForm(prefix='ms'),
                'member': MemberForm(prefix='member', show_cost=show_cost), 'deadline': DeadlineForm(prefix='deadline', initial={'new_date': p.due_date}),
                'accept': AcceptMilestoneForm(prefix='accept'), 'cr': ChangeRequestForm(prefix='cr')}
    defaults.update(forms)
    tab = request.GET.get('tab', 'overview')
    tasks = p.tasks.select_related('assignee', 'milestone').prefetch_related('depends_on')
    by_status = defaultdict(int)
    for t in tasks:
        by_status[t.status] += 1
    cost = rev = None
    if show_cost:
        from crm.modules.reports.services import project_cost, project_revenue
        cost = project_cost(p)
        rev = project_revenue(p)
    members = [{'m': m, 'total': svc.total_allocation(m.user)} for m in p.members.select_related('user')]
    pending = approvals.pending_for(p)
    checklist = svc.close_checklist(p)
    return {
        'close_ready': all(i['ok'] for i in checklist),
        'p': p, 'tab': tab, 'tasks': tasks, 'by_status': dict(by_status), 'milestones': p.milestones.select_related('accepted_by', 'invoice'),
        'members': members, 'forms': defaults, 'open_modal': open_modal, 'show_cost': show_cost, 'cost': cost, 'revenue': rev,
        'margin': (rev - cost['total']) if cost and rev is not None else None, 'is_pm': svc.is_pm(user, p),
        'can_edit': permissions.in_scope(user, 'projects', 'edit', p), 'can_task': permissions.has_perm(user, 'tasks', 'create'),
        'time': p.time_entries.select_related('user', 'task')[:100],
        'hours': p.hours_logged, 'change_requests': p.change_requests.select_related('invoice'),
        'blockers': Blocker.objects.filter(task__project=p).select_related('task', 'owner').exclude(status='resolved'),
        'tickets': p.tickets.all()[:20], 'invoices': p.invoices.all(), 'checklist': checklist, 'pending': pending,
        'activity': AuditLog.objects.filter(object_type='project', object_id=str(p.pk))[:40],
        'statuses': TASK_STATUSES, 'today': india.today(),
    }


@staff_required
def project_detail(request, pk):
    p = get_project(request, pk)
    return render(request, 'crm/projects/detail.html', _detail_ctx(request, p, open_modal=request.GET.get('modal', '')))


@staff_required
@require_POST
def project_action(request, pk, action):
    p = get_project(request, pk)
    user = request.user
    back = reverse('crm:project_detail', args=[p.pk])

    def bad(key, form):
        return render(request, 'crm/projects/detail.html', _detail_ctx(request, p, {key: form}, key), status=400)
    try:
        if action == 'task':
            permissions.require(user, 'tasks', 'create', request)
            form = TaskForm(request.POST, prefix='task', project=p, instance=Task(project=p))
            if not form.is_valid():
                return bad('task', form)
            t = form.save(commit=False)
            t.project = p
            svc.save_task(t, user, deps=form.cleaned_data['depends_on'], is_new=True)
            messages.success(request, f'Task "{t.title}" added.')
            back += '?tab=tasks'
        elif action == 'milestone':
            permissions.require_object(user, 'projects', 'edit', p, request)
            form = MilestoneForm(request.POST, prefix='ms')
            if not form.is_valid():
                return bad('milestone', form)
            m = form.save(commit=False)
            m.project = p
            m.save()
            audit.log('milestone.created', p, actor=user, changes={'milestone': m.name})
            messages.success(request, 'Milestone added.')
            back += '?tab=milestones'
        elif action == 'member':
            permissions.require_object(user, 'projects', 'edit', p, request)
            form = MemberForm(request.POST, prefix='member', show_cost=permissions.can_see(user, 'cost_price'))
            if not form.is_valid():
                return bad('member', form)
            c = form.cleaned_data
            _, total = svc.add_member(p, user, user=c['user'], role=c['role'], allocation_percent=c['allocation_percent'],
                                      start_date=c.get('start_date'), end_date=c.get('end_date'), cost_rate=c.get('cost_rate') or 0)
            if total > 100:
                messages.warning(request, f'{c["user"].get_full_name() or c["user"]} is now allocated {total}% across projects.')
            else:
                messages.success(request, 'Team member saved.')
            back += '?tab=team'
        elif action == 'remove_member':
            permissions.require_object(user, 'projects', 'edit', p, request)
            m = get_object_or_404(ProjectMember, pk=request.POST.get('member'), project=p)
            if m.user_id == p.manager_id:
                raise ValidationError('The project manager cannot be removed.')
            if p.tasks.filter(assignee=m.user).exclude(status__in=['done', 'cancelled']).exists():
                raise ValidationError('Reassign their open tasks first.')
            m.delete()
            audit.log('project.member_removed', p, actor=user, changes={'user': str(m.user)})
            messages.success(request, 'Member removed.')
            back += '?tab=team'
        elif action == 'deadline':
            permissions.require_object(user, 'projects', 'edit', p, request)
            form = DeadlineForm(request.POST, prefix='deadline')
            if not form.is_valid():
                return bad('deadline', form)
            svc.change_deadline(p, user, new_date=form.cleaned_data['new_date'], reason=form.cleaned_data['reason'])
            messages.success(request, 'Deadline changed.')
        elif action in ('activate', 'hold', 'cancel', 'reopen'):
            permissions.require_object(user, 'projects', 'edit', p, request)
            svc.set_status(p, user, {'activate': 'active', 'hold': 'on_hold', 'cancel': 'cancelled', 'reopen': 'active'}[action],
                           request.POST.get('reason', ''))
            messages.success(request, 'Project status updated.')
        elif action == 'close':
            svc.request_close(p, user, request.POST.get('reason', ''))
            messages.success(request, 'Close requested. Finance signs off before the project is closed.')
        elif action in ('ms_ready', 'ms_accept', 'ms_reject'):
            m = get_object_or_404(Milestone, pk=request.POST.get('milestone'), project=p)
            if action == 'ms_ready':
                svc.mark_ready(m, user)
                messages.success(request, 'Marked ready for review.')
            elif action == 'ms_accept':
                form = AcceptMilestoneForm(request.POST, prefix='accept')
                if not form.is_valid():
                    raise ValidationError('Record the acceptance evidence.')
                inv = svc.accept_milestone(m, user, evidence=form.cleaned_data['evidence'], invoice_action=form.cleaned_data['invoice_action'])
                messages.success(request, 'Milestone accepted.' + (' A draft invoice was created.' if inv else ''))
                if inv:
                    return redirect('crm:invoice_detail', pk=inv.pk)
            else:
                svc.reject_milestone(m, user, request.POST.get('reason', ''))
                messages.success(request, 'Milestone sent back.')
            back += '?tab=milestones'
        elif action == 'cr':
            form = ChangeRequestForm(request.POST, prefix='cr')
            if not form.is_valid():
                return bad('cr', form)
            cr = form.save(commit=False)
            cr.project = p
            cr.created_by = user
            cr.save()
            svc.submit_change(cr, user)
            messages.success(request, 'Change request submitted for approval. The original price is unchanged.')
            back += '?tab=changes'
        elif action == 'cr_bill':
            permissions.require(user, 'invoices', 'create', request)
            cr = get_object_or_404(ChangeRequest, pk=request.POST.get('cr'), project=p)
            inv = svc.bill_change(cr, user)
            messages.success(request, 'Draft invoice created for the change request.')
            return redirect('crm:invoice_detail', pk=inv.pk)
        elif action == 'blocker_resolve':
            b = get_object_or_404(Blocker, pk=request.POST.get('blocker'), task__project=p)
            svc.resolve_blocker(b, user, request.POST.get('reason', ''))
            messages.success(request, 'Blocker resolved.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect(back)


# ---------------------------------------------------------------- tasks

def task_scope_q(user):
    """Tasks: the user's own/team scope plus every task on projects they manage (Part 10.2)."""
    return Q(project__manager=user)


class TaskList(ListPage):
    model = Task
    module = 'tasks'
    template_name = 'crm/projects/task_list.html'
    title = 'Tasks'
    search_fields = ('title__icontains', 'project__code__icontains', 'project__name__icontains')
    select_related = ('project', 'assignee')
    default_sort = 'due_date'
    sort_fields = {'due': 'due_date', 'priority': 'priority'}
    filters = [Filter('status', 'Status', TASK_STATUSES), Filter('priority', 'Priority', TASK_PRIORITIES),
               Filter('project', 'Project', lambda r: [(p.pk, str(p)) for p in Project.objects.filter(status__in=['planned', 'active', 'on_hold'])],
                      lookup='project_id')]
    quick_filters = {'mine': ('Assigned to me', lambda qs, r: qs.filter(assignee=r.user)),
                     'open': ('Open', lambda qs, r: qs.exclude(status__in=['done', 'cancelled'])),
                     'overdue': ('Overdue', lambda qs, r: qs.exclude(status__in=['done', 'cancelled']).filter(due_date__lt=india.today())),
                     'review': ('Waiting for my review', lambda qs, r: qs.filter(status='in_review', reviewer=r.user))}
    export_name = 'tasks'
    export_columns = [('Project', 'project'), ('Task', 'title'), ('Assignee', 'assignee'), ('Status', lambda t: t.get_status_display()),
                      ('Priority', 'priority'), ('Due', 'due_date'), ('Estimate', 'estimate_hours')]

    def extra_scope_q(self):
        return task_scope_q(self.request.user)

    def base_queryset(self):
        return super().base_queryset().distinct()

    def get(self, request, *args, **kwargs):
        if request.GET.get('view') == 'board':
            return task_board(request)
        if not request.GET.get('quick') and not request.GET.get('status') and not request.GET.get('q'):
            request.GET = request.GET.copy()
            request.GET['quick'] = 'open'
        return super().get(request, *args, **kwargs)

    def extra_context(self, qs):
        return {'module_code': 'tasks', 'today': india.today(), 'search_placeholder': 'Search task or project'}


def _visible_tasks(request):
    return permissions.scoped(Task.objects.all(), request.user, 'tasks', 'view', extra_q=task_scope_q(request.user)).distinct()


def task_board(request):
    qs = _visible_tasks(request).select_related('project', 'assignee').exclude(status='cancelled')
    pid = request.GET.get('project')
    if pid and pid.isdigit():
        qs = qs.filter(project_id=pid)
    if request.GET.get('mine'):
        qs = qs.filter(assignee=request.user)
    qs = qs.exclude(status='done', completed_at__lt=timezone.now() - timedelta(days=14))
    cols = [{'status': s, 'label': l, 'cards': [t for t in qs if t.status == s]} for s, l in TASK_STATUSES if s != 'cancelled']
    projects = Project.objects.filter(pk__in=_visible_tasks(request).values('project_id'), status__in=['planned', 'active', 'on_hold'])
    return render(request, 'crm/projects/task_board.html', {'columns': cols, 'projects': projects, 'today': india.today(),
                                                            'statuses': TASK_STATUSES})


def get_task(request, pk, action='view'):
    return get_scoped(request, Task, pk, 'tasks', action, extra_q=task_scope_q(request.user),
                      queryset=Task.objects.select_related('project__client', 'project__manager', 'assignee', 'reviewer', 'milestone').distinct())


@staff_required
def task_detail(request, pk):
    t = get_task(request, pk)
    can_edit = permissions.in_scope(request.user, 'tasks', 'edit', t, extra_q=task_scope_q(request.user))
    form = TaskForm(request.POST or None, instance=t, prefix='task', project=t.project)
    if request.method == 'POST':
        if not can_edit:
            permissions.denied(request.user, 'tasks', 'edit', request)
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                svc.save_task(obj, request.user, deps=form.cleaned_data['depends_on'], is_new=False)
                messages.success(request, 'Task saved.')
                return redirect('crm:task_detail', pk=t.pk)
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
    time_form = TimeForm(prefix='time', user=request.user, initial={'project': t.project_id, 'task': t.pk})
    return render(request, 'crm/projects/task_detail.html', {
        't': t, 'form': form, 'can_edit': can_edit, 'statuses': TASK_STATUSES, 'next': svc.TRANSITIONS.get(t.status, ()),
        'blockers': t.blockers.select_related('owner'), 'time': t.time_entries.select_related('user')[:50], 'time_form': time_form,
        'activity': AuditLog.objects.filter(object_type='task', object_id=str(t.pk))[:30],
        'deps': t.depends_on.all(), 'dependents': t.dependents.all(), 'open_modal': 'edit' if request.method == 'POST' else request.GET.get('modal', ''),
    })


@staff_required
@require_POST
def task_status(request, pk):
    t = get_task(request, pk)
    wants_json = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if not permissions.in_scope(request.user, 'tasks', 'edit', t, extra_q=task_scope_q(request.user)):
        if wants_json:
            return JsonResponse({'ok': False, 'error': 'You cannot change this task.'}, status=403)
        permissions.denied(request.user, 'tasks', 'edit', request)
    to = request.POST.get('to', '')
    if to == 'blocked' and not request.POST.get('reason'):
        if wants_json:
            return JsonResponse({'ok': False, 'redirect': reverse('crm:task_detail', args=[t.pk]) + '?modal=block'})
    try:
        svc.set_task_status(t, request.user, to, reason=request.POST.get('reason', ''), blocker_kind=request.POST.get('kind', 'other'))
    except ValidationError as exc:
        if wants_json:
            return JsonResponse({'ok': False, 'error': ' '.join(exc.messages)})
        messages.error(request, ' '.join(exc.messages))
        return redirect('crm:task_detail', pk=t.pk)
    if wants_json:
        return JsonResponse({'ok': True, 'message': f'Moved to {dict(TASK_STATUSES)[to]}.'})
    messages.success(request, f'Task moved to {dict(TASK_STATUSES)[to]}.')
    nxt = request.POST.get('next')
    return redirect(nxt if nxt and nxt.startswith('/crm/') else reverse('crm:task_detail', args=[t.pk]))


# ---------------------------------------------------------------- timesheets

@staff_required
def timesheet(request):
    permissions.require(request.user, 'tasks', 'view', request)
    user = request.user
    try:
        ws = svc.week_start(date.fromisoformat(request.GET['week'])) if request.GET.get('week') else svc.week_start(india.today())
    except (ValueError, AttributeError):
        ws = svc.week_start(india.today())
    days = [ws + timedelta(days=i) for i in range(7)]
    form = TimeForm(request.POST or None, prefix='time', user=user, initial={'entry_date': min(india.today(), days[-1])})
    if request.method == 'POST':
        action = request.POST.get('action', 'log')
        try:
            if action == 'log':
                if not form.is_valid():
                    raise ValidationError(' '.join(e for errs in form.errors.values() for e in errs))
                c = form.cleaned_data
                svc.log_time(user, project=c['project'], task=c.get('task'), entry_date=c['entry_date'], hours=c['hours'],
                             billable=c['billable'], note=c['note'])
                messages.success(request, f'{c["hours"].normalize():f} h logged.')
            elif action == 'delete':
                svc.delete_time(get_object_or_404(TimeEntry, pk=request.POST.get('entry'), user=user), user)
                messages.success(request, 'Entry deleted.')
            elif action == 'submit':
                n = svc.submit_week(user, ws)
                messages.success(request, f'Week submitted ({n} entries). It is locked until approved or sent back.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        nxt = request.POST.get('next', '')
        if nxt.startswith('/crm/tasks/'):
            return redirect(nxt)
        return redirect(f'{reverse("crm:timesheet")}?week={ws}')
    entries = TimeEntry.objects.filter(user=user, entry_date__range=(ws, days[-1])).select_related('project', 'task').order_by('entry_date')
    grid = defaultdict(lambda: [Decimal('0')] * 7)
    totals = [Decimal('0')] * 7
    for e in entries:
        key = (e.project, e.task)
        grid[key][(e.entry_date - ws).days] += e.hours
        totals[(e.entry_date - ws).days] += e.hours
    rows = [{'project': k[0], 'task': k[1], 'hours': v, 'total': sum(v, Decimal('0'))} for k, v in grid.items()]
    return render(request, 'crm/projects/timesheet.html', {
        'ws': ws, 'days': days, 'rows': rows, 'totals': totals, 'week_total': sum(totals, Decimal('0')), 'entries': entries, 'form': form,
        'locked': svc.week_locked(user, ws), 'prev': ws - timedelta(days=7), 'next': ws + timedelta(days=7),
        'has_draft': entries.filter(status__in=['draft', 'rejected']).exists(), 'today': india.today()})


@staff_required
def timesheet_approvals(request):
    permissions.require(request.user, 'tasks', 'approve', request)
    user = request.user
    qs = TimeEntry.objects.filter(status='submitted').exclude(user=user).select_related('user', 'project', 'task')
    managed = [e for e in qs if svc.is_pm(user, e.project)]
    if request.method == 'POST':
        ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()]
        chosen = [e for e in managed if e.pk in ids]
        try:
            if not chosen:
                raise ValidationError('Select at least one entry.')
            n = svc.decide_entries(chosen, user, approve=request.POST.get('decision') == 'approve', comment=request.POST.get('reason', ''))
            messages.success(request, f'{n} entr{"y" if n == 1 else "ies"} {"approved" if request.POST.get("decision") == "approve" else "sent back"}.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:timesheet_approvals')
    groups = defaultdict(list)
    for e in managed:
        groups[(e.user, svc.week_start(e.entry_date))].append(e)
    data = [{'user': k[0], 'week': k[1], 'entries': v, 'total': sum((e.hours for e in v), Decimal('0'))} for k, v in sorted(groups.items(), key=lambda x: x[0][1])]
    return render(request, 'crm/projects/timesheet_approvals.html', {'groups': data})


@staff_required
def blockers(request):
    permissions.require(request.user, 'tasks', 'view', request)
    visible = _visible_tasks(request)
    open_blockers = Blocker.objects.filter(task__in=visible).exclude(status='resolved').select_related('task__project', 'owner')
    overdue = visible.exclude(status__in=['done', 'cancelled']).filter(due_date__lt=india.today()).select_related('project', 'assignee')
    return render(request, 'crm/projects/blockers.html', {'blockers': open_blockers, 'overdue': overdue[:200], 'today': india.today()})


@staff_required
@require_POST
def blocker_action(request, pk):
    b = get_object_or_404(Blocker.objects.select_related('task__project'), pk=pk)
    get_task(request, b.task_id, 'edit')
    try:
        svc.resolve_blocker(b, request.user, request.POST.get('reason', ''))
        messages.success(request, 'Blocker resolved.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:blockers')


# ---------------------------------------------------------------- tickets

class TicketList(ListPage):
    model = Ticket
    module = 'tickets'
    template_name = 'crm/projects/ticket_list.html'
    title = 'Support tickets'
    search_fields = ('number__icontains', 'subject__icontains', 'client__display_name__icontains')
    select_related = ('client', 'assignee', 'project', 'contract')
    filters = [Filter('status', 'Status', TICKET_STATUSES), Filter('priority', 'Priority', TASK_PRIORITIES)]
    quick_filters = {'mine': ('Assigned to me', lambda qs, r: qs.filter(assignee=r.user)),
                     'open': ('Open', lambda qs, r: qs.filter(status__in=['open', 'in_progress', 'waiting'])),
                     'breached': ('SLA breached', lambda qs, r: qs.filter(status__in=['open', 'in_progress'], sla_due_at__lt=timezone.now()))}
    export_name = 'tickets'
    export_columns = [('Number', 'number'), ('Subject', 'subject'), ('Client', 'client'), ('Priority', 'priority'),
                      ('Status', lambda t: t.get_status_display()), ('Assignee', 'assignee'), ('SLA due', 'sla_due_at')]

    def extra_context(self, qs):
        return {'module_code': 'tickets', 'can_create': permissions.has_perm(self.request.user, 'tickets', 'create')}


@staff_required
def ticket_create(request):
    permissions.require(request.user, 'tickets', 'create', request)
    initial = {}
    if request.GET.get('client'):
        initial['client'] = request.GET['client']
    if request.GET.get('project'):
        initial['project'] = request.GET['project']
    form = TicketForm(request.POST or None, initial=initial, clients=_clients(request))
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        t = svc.create_ticket(request.user, client=c['client'], subject=c['subject'], description=c['description'], priority=c['priority'],
                              assignee=c.get('assignee'), project=c.get('project'))
        messages.success(request, f'Ticket {t.number} created. SLA due {timezone.localtime(t.sla_due_at):%d-%b %H:%M}.')
        return redirect('crm:ticket_detail', pk=t.pk)
    return render(request, 'crm/projects/ticket_form.html', {'form': form})


@staff_required
def ticket_detail(request, pk):
    t = get_scoped(request, Ticket, pk, 'tickets', 'view', queryset=Ticket.objects.select_related('client', 'project', 'contract', 'assignee'))
    can_edit = permissions.in_scope(request.user, 'tickets', 'edit', t)
    if request.method == 'POST':
        if not can_edit:
            permissions.denied(request.user, 'tickets', 'edit', request)
        try:
            if request.POST.get('action') == 'assign':
                permissions.require(request.user, 'tickets', 'assign', request)
                from django.contrib.auth import get_user_model
                u = get_object_or_404(get_user_model(), pk=request.POST.get('assignee'), is_active=True)
                Ticket.objects.filter(pk=t.pk).update(assignee=u)
                audit.log('ticket.assigned', t, actor=request.user, changes={'to': str(u)})
                messages.success(request, f'Assigned to {u.get_full_name() or u}.')
            else:
                svc.set_ticket_status(t, request.user, request.POST.get('to', ''), request.POST.get('reason', ''))
                messages.success(request, 'Ticket updated.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('crm:ticket_detail', pk=t.pk)
    from .forms import staff_users
    return render(request, 'crm/projects/ticket_detail.html', {
        't': t, 'can_edit': can_edit, 'next': svc.TICKET_FLOW.get(t.status, ()), 'statuses': dict(TICKET_STATUSES),
        'users': staff_users(), 'can_assign': permissions.has_perm(request.user, 'tickets', 'assign'),
        'activity': AuditLog.objects.filter(object_type='ticket', object_id=str(t.pk))[:30]})


@staff_required
def task_create(request):
    """Quick add: choose the project, then the task form for that project."""
    permissions.require(request.user, 'tasks', 'create', request)
    projects = Project.objects.filter(status__in=['planned', 'active', 'on_hold']).filter(
        Q(manager=request.user) | Q(members__user=request.user)).distinct().order_by('code')
    pid = request.GET.get('project') or request.POST.get('project')
    p = projects.filter(pk=pid).first() if pid and str(pid).isdigit() else None
    form = None
    if p is not None:
        form = TaskForm(request.POST or None, prefix='task', project=p, instance=Task(project=p),
                        initial={'assignee': request.user.pk})
        if request.method == 'POST' and form.is_valid():
            t = form.save(commit=False)
            t.project = p
            try:
                svc.save_task(t, request.user, deps=form.cleaned_data['depends_on'], is_new=True)
                messages.success(request, f'Task "{t.title}" added to {p.code}.')
                return redirect('crm:task_detail', pk=t.pk)
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
    return render(request, 'crm/projects/task_create.html', {'projects': projects, 'p': p, 'form': form})
