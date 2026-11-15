"""Monthly deliverables pages: my work, the team board, one deliverable, the client plan, standard service plans,
team load, the client delivery summary and the asset repository."""
from __future__ import annotations

from datetime import date, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from crm.core import audit, india, notify, permissions
from crm.core.views import Filter, ListPage, back, form_error_text, get_scoped, staff_required
from crm.models import (CalendarFeedToken, Client, Contract, Deliverable, DeliverableAsset, DeliveryPlan, PlanItem,
                        Service, ServiceDeliverable)
from crm.models.deliverables import CATEGORIES, DELIVERABLE_STATUSES, OPEN_STATUSES, TEAMS

from . import services as svc
from .forms import (AdhocForm, PlanItemForm, PreferencesForm, ReassignForm, TemplateItemForm, staff_users)

MODULE = 'deliverables'


def _name(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else 'Unassigned'


def _get_d(request, pk, action='view') -> Deliverable:
    qs = Deliverable.objects.select_related('client', 'plan', 'plan__account_owner', 'plan__contract', 'assignee', 'reviewer',
                                            'delivered_by', 'reviewed_by')
    return get_scoped(request, Deliverable, pk, MODULE, action, queryset=qs)


def _get_plan(request, pk, action='view') -> DeliveryPlan:
    qs = DeliveryPlan.objects.select_related('client', 'contract', 'account_owner', 'default_assignee')
    return get_scoped(request, DeliveryPlan, pk, MODULE, action, owner_fields=('account_owner', 'default_assignee'), queryset=qs)


def _assignable(user):
    if permissions.has_perm(user, MODULE, 'assign'):
        qs = staff_users()
        if not permissions.has_scope(user, MODULE, 'assign', 'company'):
            qs = qs.filter(pk__in=permissions.team_member_ids(user))
        return qs
    return get_user_model().objects.none()


def primary_step(d: Deliverable, user) -> dict | None:
    """The one thing the person should do next, so a phone screen needs a single tap."""
    if d.status == 'upcoming':
        return {'label': 'Start', 'verb': 'start', 'modal': ''}
    if d.status == 'in_progress':
        if d.requires_review and d.review_state != 'approved':
            return {'label': 'Send for review' if d.review_state != 'changes' else 'Fix and resubmit', 'verb': 'submit_review',
                    'modal': 'dl_review_submit'}
        if d.needs_client_approval and d.client_state != 'approved':
            return {'label': 'Sent to client', 'verb': 'send_client', 'modal': 'dl_send_client'}
        return {'label': 'Mark delivered', 'verb': 'deliver', 'modal': 'dl_deliver'}
    if d.status == 'in_review':
        if svc.can_review(user, d):
            return {'label': 'Review', 'verb': 'open', 'modal': ''}
        return None
    if d.status == 'awaiting_client':
        return {'label': 'Client replied', 'verb': 'open', 'modal': ''}
    return None


def _decorate(rows, user):
    rows = list(rows)
    for d in rows:
        d.step = primary_step(d, user)
    return rows


# ---------------------------------------------------------------- my deliverables

@staff_required
def mine(request):
    permissions.require(request.user, MODULE, 'view', request)
    user = request.user
    people = _assignable(user)
    target = user
    who = request.GET.get('who', '')
    if who.isdigit() and people.filter(pk=int(who)).exists():
        target = people.get(pk=int(who))
    today = india.today()
    base = Deliverable.objects.filter(assignee=target).select_related('client', 'plan', 'plan__account_owner', 'reviewer', 'assignee')
    open_qs = base.filter(status__in=OPEN_STATUSES)
    tabs = {
        'today': ('Today', open_qs.filter(due_date__lte=today)),
        'week': ('This week', open_qs.filter(due_date__gt=today, due_date__lte=today + timedelta(days=7))),
        'later': ('Later', open_qs.filter(due_date__gt=today + timedelta(days=7))),
        'waiting': ('Waiting', open_qs.filter(status__in=['in_review', 'awaiting_client'])),
        'done': ('Done', base.filter(status='delivered', delivered_at__gte=timezone.now() - timedelta(days=21)).order_by('-delivered_at')),
    }
    tab = request.GET.get('tab', 'today')
    tab = tab if tab in tabs else 'today'
    counts = {k: v[1].count() for k, v in tabs.items()}
    rows = _decorate(tabs[tab][1][:150], user)
    reviews = []
    if permissions.has_perm(user, MODULE, 'view'):
        reviews = _decorate(Deliverable.objects.filter(status='in_review').filter(Q(reviewer=user) | Q(assignee__crm_profile__manager=user))
                            .exclude(assignee=user).select_related('client', 'assignee', 'plan')[:20], user)
    feed = CalendarFeedToken.for_user(user)
    feed_url = request.build_absolute_uri(reverse('crm:calendar_feed', args=[feed.token]))
    return render(request, 'crm/deliverables/mine.html', {
        'rows': rows, 'tab': tab, 'tabs': [(k, v[0], counts[k]) for k, v in tabs.items()], 'target': target, 'people': people,
        'reviews': reviews, 'today': today, 'feed_url': feed_url, 'now': timezone.now(),
        'overdue_count': open_qs.filter(due_date__lt=today).count(),
        'can_add': permissions.has_perm(user, MODULE, 'create'), 'categories': CATEGORIES, 'teams': TEAMS})


# ---------------------------------------------------------------- the board (everyone I can see)

def _client_choices(request):
    qs = permissions.scoped(Deliverable.objects.all(), request.user, MODULE, 'view')
    return list(Client.objects.filter(pk__in=qs.values('client_id')).order_by('display_name').values_list('pk', 'display_name'))


def _month_choices(request):
    today = india.today().replace(day=1)
    out = []
    for i in range(-2, 4):
        m = india.add_months(today, i)
        out.append((m.isoformat(), m.strftime('%B %Y')))
    return out


def _owner_choices(request):
    return [(u.pk, _name(u)) for u in staff_users()]


class DeliverableList(ListPage):
    model = Deliverable
    template_name = 'crm/deliverables/list.html'
    module = MODULE
    title = 'Deliverables'
    select_related = ('client', 'assignee', 'plan', 'plan__account_owner', 'reviewer')
    search_fields = ('title', 'client__display_name', 'delivery_note')
    default_sort = 'due_date'
    sort_fields = {'due': 'due_date', 'client': 'client__display_name', 'status': 'status'}
    export_name = 'deliverables'
    export_columns = [('Client', 'client.display_name'), ('Deliverable', 'title'), ('Promised for', 'original_due_date'),
                      ('Due now', 'due_date'), ('Status', lambda d: d.get_status_display()), ('Assignee', lambda d: _name(d.assignee)),
                      ('Account owner', lambda d: _name(d.plan.account_owner)), ('Quantity', 'quantity'), ('Done', 'quantity_done'),
                      ('Rescheduled', 'reschedule_count'), ('Delivered at', 'delivered_at'), ('Delivery note', 'delivery_note')]
    filters = [
        Filter('assignee', 'Assignee', _owner_choices, 'assignee_id'),
        Filter('client', 'Client', _client_choices, 'client_id'),
        Filter('status', 'Status', DELIVERABLE_STATUSES),
        Filter('month', 'Month', _month_choices, apply=lambda qs, v, r: qs.filter(period=date.fromisoformat(v))),
        Filter('review', 'Review', [('pending', 'Waiting for review'), ('changes', 'Changes requested'), ('approved', 'Approved')], 'review_state'),
        Filter('category', 'Type', [(c, c) for c in CATEGORIES], 'category'),
    ]
    quick_filters = {
        'overdue': ('Overdue', lambda qs, r: qs.filter(status__in=OPEN_STATUSES, due_date__lt=india.today())),
        'today': ('Due today', lambda qs, r: qs.filter(status__in=OPEN_STATUSES, due_date=india.today())),
        'week': ('Next 7 days', lambda qs, r: qs.filter(status__in=OPEN_STATUSES, due_date__gte=india.today(),
                                                       due_date__lte=india.today() + timedelta(days=7))),
        'review': ('In review', lambda qs, r: qs.filter(status='in_review')),
        'client': ('Awaiting client', lambda qs, r: qs.filter(status='awaiting_client')),
        'missed': ('Missed', lambda qs, r: qs.filter(missed_at__isnull=False)),
        'unassigned': ('Unassigned', lambda qs, r: qs.filter(assignee__isnull=True, status__in=OPEN_STATUSES)),
        'rescheduled': ('Rescheduled', lambda qs, r: qs.filter(reschedule_count__gt=0)),
    }

    def extra_context(self, qs):
        user = self.request.user
        base = permissions.scoped(Deliverable.objects.all(), user, MODULE, 'view')
        today = india.today()
        open_qs = base.filter(status__in=OPEN_STATUSES)
        return {'now': timezone.now(), 'today': today,
                'stats': {'overdue': open_qs.filter(due_date__lt=today).count(), 'today': open_qs.filter(due_date=today).count(),
                          'review': base.filter(status='in_review').count(), 'client': base.filter(status='awaiting_client').count(),
                          'missed': base.filter(missed_at__isnull=False, period=today.replace(day=1)).count()},
                'can_assign': permissions.has_perm(user, MODULE, 'assign')}


# ---------------------------------------------------------------- one deliverable

@staff_required
def detail(request, pk):
    d = _get_d(request, pk)
    user = request.user
    can_edit = permissions.in_scope(user, MODULE, 'edit', d)
    return render(request, 'crm/deliverables/detail.html', {
        'd': d, 'plan': d.plan, 'client': d.client, 'step': primary_step(d, user), 'now': timezone.now(),
        'events': d.events.select_related('actor')[:80], 'assets': d.assets.select_related('added_by'),
        'can_edit': can_edit, 'open': d.is_open, 'can_review': d.status == 'in_review' and svc.can_review(user, d),
        'can_assign': permissions.has_perm(user, MODULE, 'assign') and d.is_open, 'people': list(_assignable(user)) or list(staff_users()),
        'is_owner': d.plan.account_owner_id == user.pk})


def _int(post, key, default=None):
    raw = (post.get(key) or '').strip()
    if raw == '':
        return default
    return int(raw)


def _date(post, key):
    try:
        return date.fromisoformat((post.get(key) or '').strip())
    except ValueError:
        raise ValidationError('Choose a valid date.')


def _time(post, key):
    raw = (post.get(key) or '').strip()
    if not raw:
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        raise ValidationError('Choose a valid time.')


@staff_required
@require_POST
def action(request, pk, verb):
    reviewing = verb in ('review_approve', 'review_changes')
    needs = 'assign' if verb == 'reassign' else 'edit'
    d = _get_d(request, pk, 'view' if reviewing or verb in ('client_approved', 'client_changes') else needs)
    if not reviewing and verb not in ('client_approved', 'client_changes') and not permissions.in_scope(request.user, MODULE, needs, d):
        raise PermissionDenied('You cannot change this deliverable.')
    user, post = request.user, request.POST
    target = reverse('crm:deliverable_detail', args=[d.pk])
    try:
        if verb == 'start':
            svc.start(d.pk, user)
            messages.success(request, 'Started.')
        elif verb == 'progress':
            svc.log_progress(d.pk, user, done=_int(post, 'done', 0), note=post.get('note', ''))
            messages.success(request, 'Progress saved.')
        elif verb == 'submit_review':
            svc.submit_review(d.pk, user, note=post.get('note', ''))
            messages.success(request, 'Sent for review.')
        elif verb in ('review_approve', 'review_changes'):
            svc.review(d.pk, user, approve=verb == 'review_approve', note=post.get('note', ''))
            messages.success(request, 'Approved.' if verb == 'review_approve' else 'Sent back with your comments.')
        elif verb == 'send_client':
            svc.send_to_client(d.pk, user, note=post.get('note', ''))
            messages.success(request, 'Marked as sent to the client. The wait is being timed.')
        elif verb in ('client_approved', 'client_changes'):
            if not permissions.in_scope(user, MODULE, 'edit', d):
                raise PermissionDenied('You cannot change this deliverable.')
            svc.client_answer(d.pk, user, approved=verb == 'client_approved', note=post.get('note', ''))
            messages.success(request, 'Client answer recorded.')
        elif verb == 'deliver':
            svc.deliver(d.pk, user, note=post.get('note', ''), done=_int(post, 'done'), shortfall_reason=post.get('shortfall_reason', ''))
            messages.success(request, 'Delivered.')
        elif verb == 'reschedule':
            svc.reschedule(d.pk, user, new_date=_date(post, 'new_date'), reason=post.get('reason', ''), new_time=_time(post, 'new_time'))
            messages.success(request, 'Rescheduled. The promised date is kept on record.')
        elif verb in ('skip', 'cancel'):
            svc.close_out(d.pk, user, skip=verb == 'skip', reason=post.get('reason', ''))
            messages.success(request, 'Skipped.' if verb == 'skip' else 'Cancelled.')
        elif verb == 'reassign':
            to = get_object_or_404(get_user_model().objects.filter(is_active=True), pk=_int(post, 'to', 0))
            if not _assignable(user).filter(pk=to.pk).exists():
                raise PermissionDenied('You cannot give work to that person.')
            svc.reassign(d.pk, user, to, note=post.get('note', ''))
            messages.success(request, f'Now with {_name(to)}.')
        elif verb == 'instructions':
            svc.set_instructions(d.pk, user, post.get('instructions', ''))
            messages.success(request, 'Saved.')
        elif verb == 'note':
            svc.add_note(d.pk, user, post.get('note', ''))
            messages.success(request, 'Note added.')
        elif verb == 'asset':
            svc.add_asset(d.pk, user, name=post.get('name', ''), kind=post.get('kind', 'file'), url=(post.get('url') or '').strip(),
                          upload=request.FILES.get('file'))
            messages.success(request, 'Added to the repository.')
        elif verb == 'asset_remove':
            asset = get_object_or_404(DeliverableAsset, pk=_int(post, 'asset', 0), deliverable=d)
            if asset.added_by_id != user.pk and not permissions.has_perm(user, MODULE, 'approve'):
                raise PermissionDenied('Only the person who added it, or a manager, can remove it.')
            svc._event(d, 'asset_removed', f'Removed {asset.name}.', user)
            asset.file.delete(save=False)
            asset.delete()
            messages.success(request, 'Removed.')
        else:
            raise Http404('Unknown action')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    except (ValueError, TypeError):
        messages.error(request, 'Check the number you entered.')
    return back(request, target)


@staff_required
def asset_download(request, pk, asset_id):
    d = _get_d(request, pk)
    asset = get_object_or_404(DeliverableAsset, pk=asset_id, deliverable=d)
    if asset.url and not asset.file:
        return redirect(asset.url)
    if not asset.file:
        raise Http404
    resp = FileResponse(asset.file.open('rb'), as_attachment=True, filename=asset.file.name.rsplit('/', 1)[-1])
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


# ---------------------------------------------------------------- the client's plan

@staff_required
def plan_detail(request, pk):
    plan = _get_plan(request, pk)
    user = request.user
    today = india.today()
    period = today.replace(day=1)
    try:
        if request.GET.get('month'):
            period = date.fromisoformat(request.GET['month']).replace(day=1)
    except ValueError:
        pass
    items = list(plan.items.filter(is_removed=False).select_related('assignee', 'reviewer'))
    upcoming = _decorate(Deliverable.objects.filter(plan=plan, status__in=OPEN_STATUSES, due_date__gte=today - timedelta(days=30))
                         .select_related('assignee', 'client', 'plan', 'reviewer')[:60], user)
    can_edit = permissions.in_scope(user, MODULE, 'edit', plan, owner_fields=('account_owner', 'default_assignee')) and plan.status != 'closed'
    return render(request, 'crm/deliverables/plan.html', {
        'plan': plan, 'items': items, 'removed': plan.items.filter(is_removed=True)[:20], 'upcoming': upcoming, 'period': period,
        'prev_month': india.add_months(period, -1), 'next_month': india.add_months(period, 1),
        'summary': svc.month_summary(plan.client, period), 'revisions': plan.revisions.select_related('changed_by')[:12],
        'needs_schedule': [i for i in items if i.needs_schedule and i.is_paused], 'can_edit': can_edit,
        'can_assign': permissions.has_perm(user, MODULE, 'assign') and plan.status != 'closed',
        'can_freeze': permissions.has_perm(user, MODULE, 'approve') and plan.status != 'closed',
        'pref_form': PreferencesForm(initial={'client_preferences': plan.client_preferences}),
        'reassign_form': ReassignForm(initial={'to': plan.assignee.pk if plan.assignee else None}),
        'adhoc_form': AdhocForm(initial={'due': today + timedelta(days=1), 'quantity': 1}),
        'categories': CATEGORIES, 'teams': TEAMS, 'today': today})


@staff_required
@require_POST
def plan_action(request, pk, verb):
    plan = _get_plan(request, pk, 'assign' if verb == 'reassign' else 'approve' if verb in ('freeze', 'unfreeze') else 'edit')
    user, post = request.user, request.POST
    target = reverse('crm:delivery_plan', args=[plan.pk])
    try:
        if verb == 'generate':
            n = svc.generate(plan)
            messages.success(request, f'Created {n} upcoming deliverable{"s" if n != 1 else ""}.' if n else 'Everything for the next 30 days already exists.')
        elif verb == 'preferences':
            form = PreferencesForm(post)
            if form.is_valid():
                svc.set_preferences(plan, user, form.cleaned_data['client_preferences'])
                messages.success(request, 'Preferences saved and applied to upcoming deliverables.')
            else:
                messages.error(request, form_error_text(form))
        elif verb == 'reassign':
            form = ReassignForm(post)
            if not form.is_valid():
                raise ValidationError(form_error_text(form))
            to = form.cleaned_data['to']
            if not _assignable(user).filter(pk=to.pk).exists():
                raise PermissionDenied('You cannot give work to that person.')
            moved = svc.reassign_plan(plan, to, user, account_owner=form.cleaned_data['account_owner'], note=form.cleaned_data['note'])
            messages.success(request, f'Moved to {_name(to)}: {moved} open deliverable{"s" if moved != 1 else ""}.')
        elif verb == 'freeze':
            svc.freeze(plan, user, _date(post, 'through'))
            messages.success(request, 'Frozen. Plan edits will not change those dates.')
        elif verb == 'unfreeze':
            svc.freeze(plan, user, None)
            messages.success(request, 'Freeze lifted.')
        elif verb == 'adhoc':
            form = AdhocForm(post)
            if not form.is_valid():
                raise ValidationError(form_error_text(form))
            c = form.cleaned_data
            svc.add_adhoc(plan, user, title=c['title'], due=c['due'], quantity=c['quantity'], assignee=c['assignee'],
                          instructions=c['instructions'], category=c['category'], due_time=c['due_time'], handled_by=c['handled_by'])
            messages.success(request, 'One-off deliverable added.')
        else:
            raise Http404('Unknown action')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return back(request, target)


@staff_required
def plan_item_form(request, pk=None, plan_id=None):
    if pk:
        item = get_object_or_404(PlanItem.objects.select_related('plan'), pk=pk)
        plan = _get_plan(request, item.plan_id, 'edit')
    else:
        item, plan = None, _get_plan(request, plan_id, 'edit')
    form = PlanItemForm(request.POST or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = form.save(commit=False)
            svc.save_item(plan, saved, request.user, creating=item is None)
            messages.success(request, f'"{saved.title}" saved. Upcoming dates are updated.')
            return redirect('crm:delivery_plan', pk=plan.pk)
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
    return render(request, 'crm/deliverables/item_form.html', {'form': form, 'plan': plan, 'item': item, 'categories': CATEGORIES, 'teams': TEAMS,
                                                               'title': 'Edit item' if item else 'Add item'})


@staff_required
@require_POST
def plan_item_action(request, pk, verb):
    item = get_object_or_404(PlanItem.objects.select_related('plan', 'plan__client'), pk=pk)
    _get_plan(request, item.plan_id, 'edit')
    try:
        if verb == 'pause':
            svc.set_paused(item, request.user, True)
            messages.success(request, 'Paused. Its upcoming deliverables were removed from the schedule.')
        elif verb == 'resume':
            svc.set_paused(item, request.user, False)
            messages.success(request, 'Switched on. Upcoming dates were created.')
        elif verb == 'remove':
            svc.remove_item(item, request.user, request.POST.get('reason', ''))
            messages.success(request, 'Removed from the plan.')
        else:
            raise Http404('Unknown action')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:delivery_plan', pk=item.plan_id)


@staff_required
@require_POST
def contract_plan(request, pk):
    permissions.require(request.user, MODULE, 'create', request)
    contract = get_object_or_404(Contract, pk=pk)
    get_scoped(request, Client, contract.client_id, 'clients', 'view')        # sales roles reach contracts through their client
    if contract.status != 'active':
        messages.error(request, 'Only an active contract can have a delivery plan.')
        return redirect('crm:contract_detail', pk=contract.pk)
    plan = svc.create_plan(contract, request.user, force=True)
    if plan is None:
        messages.error(request, 'No plan could be created for this contract.')
        return back(request, reverse('crm:client_deliverables', args=[contract.client_id]))
    return redirect('crm:delivery_plan', pk=plan.pk)


# ---------------------------------------------------------------- standard plans on a service

@staff_required
def service_deliverables(request, service_id):
    permissions.require(request.user, MODULE, 'configure', request)
    service = get_object_or_404(Service, pk=service_id)
    return render(request, 'crm/deliverables/service_plan.html', {'service': service, 'items': service.deliverable_items.all(),
                                                                  'form': TemplateItemForm(), 'categories': CATEGORIES, 'teams': TEAMS})


@staff_required
def template_item_form(request, service_id=None, pk=None):
    permissions.require(request.user, MODULE, 'configure', request)
    item = get_object_or_404(ServiceDeliverable.objects.select_related('service'), pk=pk) if pk else None
    service = item.service if item else get_object_or_404(Service, pk=service_id)
    form = TemplateItemForm(request.POST or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        row = form.save(commit=False)
        row.service = service
        row.save()
        audit.log('deliverables.template_saved', service, actor=request.user, changes={'item': row.title})
        messages.success(request, f'"{row.title}" saved. It applies to contracts activated from now on.')
        return redirect('crm:service_deliverables', service_id=service.pk)
    return render(request, 'crm/deliverables/template_form.html', {'form': form, 'service': service, 'item': item, 'categories': CATEGORIES, 'teams': TEAMS})


@staff_required
@require_POST
def template_item_delete(request, pk):
    permissions.require(request.user, MODULE, 'configure', request)
    item = get_object_or_404(ServiceDeliverable.objects.select_related('service'), pk=pk)
    audit.log('deliverables.template_removed', item.service, actor=request.user, changes={'item': item.title})
    service_id = item.service_id
    item.delete()
    messages.success(request, 'Removed from the standard plan. Existing client plans are not changed.')
    return redirect('crm:service_deliverables', service_id=service_id)


# ---------------------------------------------------------------- team load, summary, repository

@staff_required
def workload(request):
    """Team load: who is carrying how many open deliverables, and which clients need attention."""
    permissions.require(request.user, MODULE, 'assign', request)
    rows = svc.team_load(list(_assignable(request.user)))
    unassigned = Deliverable.objects.filter(assignee__isnull=True, status__in=OPEN_STATUSES).count()
    return render(request, 'crm/deliverables/workload.html', {
        'rows': rows, 'unassigned': unassigned, 'health': svc.client_health()[:12], 'period': india.today().replace(day=1)})


@staff_required
def client_summary(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    client = get_scoped(request, Client, pk, 'clients', 'view')
    period = india.today().replace(day=1)
    try:
        if request.GET.get('month'):
            period = date.fromisoformat(request.GET['month']).replace(day=1)
    except ValueError:
        pass
    summary = svc.month_summary(client, period)
    return render(request, 'crm/deliverables/summary.html', {
        'client': client, 'period': period, 'summary': summary, 'prev_month': india.add_months(period, -1),
        'next_month': india.add_months(period, 1), 'plan': client.delivery_plans.exclude(status='closed').first(), 'plans': client.delivery_plans.exclude(status='closed').select_related('contract'),
        'can_send': permissions.has_perm(request.user, MODULE, 'edit') and bool(client.email or client.contacts.filter(email__gt='').exists()),
        'print': request.GET.get('print') == '1'})


def _summary_text(client, period, summary) -> str:
    from crm.models import CompanySettings
    co = CompanySettings.get()
    lines = [f'Dear {client.legal_name},', '', f'Here is what we delivered for you in {period:%B %Y}.', '']
    for r in summary['rows']:
        lines.append(f'- {r["title"]}: {r["delivered"]} of {r["promised"]} delivered' + (f', {r["open"]} still to come' if r['open'] else ''))
        for d in r['items']:
            if d.status == 'delivered':
                lines.append(f'    {d.delivered_at:%d %b}: {d.delivery_note}')
    lines += ['', f'Overall: {summary["delivered"]} of {summary["promised"]} ({summary["percent"]}%).', '', f'Regards,\n{co.legal_name}']
    return '\n'.join(lines)


@staff_required
@require_POST
def client_summary_send(request, pk):
    permissions.require(request.user, MODULE, 'edit', request)
    client = get_scoped(request, Client, pk, 'clients', 'view')
    try:
        period = date.fromisoformat(request.POST.get('month', '')).replace(day=1)
    except ValueError:
        period = india.today().replace(day=1)
    to = (client.contacts.filter(is_primary=True, email__gt='').values_list('email', flat=True).first() or client.email
          or client.contacts.filter(email__gt='').values_list('email', flat=True).first())
    if not to:
        messages.error(request, 'This client has no email address.')
    else:
        summary = svc.month_summary(client, period)
        notify.send_email(to, f'Your {period:%B %Y} delivery summary', _summary_text(client, period, summary), sent_by=request.user,
                          obj=client, template_code='delivery_summary')
        audit.log('deliverables.summary_sent', client, actor=request.user, changes={'month': period, 'to': to})
        messages.success(request, f'Summary emailed to {to}.')
    return back(request, reverse('crm:client_deliverables', args=[client.pk]))


@staff_required
def client_assets(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    client = get_scoped(request, Client, pk, 'clients', 'view')
    visible = permissions.scoped(Deliverable.objects.filter(client=client), request.user, MODULE, 'view')
    qs = DeliverableAsset.objects.filter(deliverable__in=visible).select_related('deliverable', 'added_by')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(deliverable__title__icontains=q))
    kind = request.GET.get('kind', '')
    if kind in dict(DeliverableAsset.KINDS):
        qs = qs.filter(kind=kind)
    return render(request, 'crm/deliverables/assets.html', {'client': client, 'assets': qs[:300], 'q': q, 'kind': kind,
                                                            'kinds': DeliverableAsset.KINDS})


# ---------------------------------------------------------------- calendar feed

@never_cache
@require_GET
def calendar_feed(request, token):
    row = CalendarFeedToken.objects.select_related('user').filter(token=token, user__is_active=True).first()
    if row is None:
        raise Http404
    resp = HttpResponse(svc.ics_feed(row.user), content_type='text/calendar; charset=utf-8')
    resp['Content-Disposition'] = 'inline; filename="bthinkx-work.ics"'
    resp['Cache-Control'] = 'private, no-store'
    return resp


@staff_required
@require_POST
def calendar_feed_reset(request):
    CalendarFeedToken.for_user(request.user, rotate=True)
    audit.log('deliverables.calendar_link_reset', actor=request.user, object_type='user', object_id=str(request.user.pk))
    messages.success(request, 'Your calendar link was changed. Subscribe again with the new link.')
    return back(request, reverse('crm:deliverable_mine'))
