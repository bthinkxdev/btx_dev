"""Lead pages (Part 4.4-4.7): list, add/edit, detail with popups, bulk actions, board moves."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, permissions
from crm.core.views import Filter, ListPage, back, form_error_text, get_scoped, staff_required
from crm.models import (Attachment, FollowUp, Lead, LeadActivity, LeadSource, LeadTag, Service)
from crm.models.leads import CLOSED_STAGES, LOST_REASONS, OPEN_STAGES, PRIORITIES, STAGES

from . import services as svc
from .forms import (AssignForm, BulkForm, CancelFollowUpForm, CompleteFollowUpForm, DuplicateDecisionForm, LeadForm,
                    LogCallForm, LogMeetingForm, LostForm, MessageForm, NoteForm, PostponeForm, ReopenForm,
                    ScheduleFollowUpForm, StageForm, WonForm, assignable_users)


def lead_scope_extra(user):
    """People who assign leads also see the unassigned queue."""
    return Q(owner__isnull=True) if permissions.has_perm(user, 'leads', 'assign') else None


def scoped_leads(request, action='view'):
    return permissions.scoped(Lead.objects.all(), request.user, 'leads', action, extra_q=lead_scope_extra(request.user))


def get_lead(request, pk, action='view'):
    return get_scoped(request, Lead, pk, 'leads', action, extra_q=lead_scope_extra(request.user),
                      queryset=Lead.objects.select_related('owner', 'source', 'campaign', 'client', 'team'))


def _owner_choices(request):
    users = assignable_users(request.user) if permissions.has_perm(request.user, 'leads', 'assign') else []
    return [(u.pk, u.get_full_name() or u.username) for u in users] + ([('none', 'Unassigned')] if users else [])


def _owner_filter(qs, value, request):
    if value == 'none':
        return qs.filter(owner__isnull=True)
    return qs.filter(owner_id=int(value))


def _now():
    return timezone.now()


def _sla_breached(qs, request):
    """First response slower than the SLA, or still waiting past it (Part 4.9)."""
    from crm.models import CompanySettings
    limit = CompanySettings.get().first_response_minutes
    return qs.filter(Q(first_response_seconds__gt=limit * 60) |
                     Q(stage__in=OPEN_STAGES, first_contacted_at__isnull=True, created_at__lt=_now() - timedelta(minutes=limit)))


QUICK = {
    'mine': ('My leads', lambda qs, r: qs.filter(owner=r.user)),
    'unassigned': ('Unassigned', lambda qs, r: qs.filter(owner__isnull=True, stage__in=OPEN_STAGES)),
    'due_today': ('Due today', lambda qs, r: qs.filter(stage__in=OPEN_STAGES, next_follow_up_at__date=timezone.localdate())),
    'overdue': ('Overdue', lambda qs, r: qs.filter(stage__in=OPEN_STAGES, next_follow_up_at__lt=_now())),
    'no_activity': ('No activity 3 days', lambda qs, r: qs.filter(stage__in=OPEN_STAGES).filter(
        Q(last_activity_at__lt=_now() - timedelta(days=3)) | Q(last_activity_at__isnull=True))),
    'new_24h': ('New in last 24 h', lambda qs, r: qs.filter(created_at__gte=_now() - timedelta(hours=24))),
    'hot': ('High priority', lambda qs, r: qs.filter(priority='hot', stage__in=OPEN_STAGES)),
    'open': ('Open', lambda qs, r: qs.filter(stage__in=OPEN_STAGES)),
    'unworked': ('Not yet contacted', lambda qs, r: qs.filter(stage__in=OPEN_STAGES, first_contacted_at__isnull=True)),
    'sla_breached': ('First-response SLA breached', _sla_breached),
    'archived': ('Archived', lambda qs, r: qs.filter(archived_at__isnull=False)),
}


class LeadList(ListPage):
    date_fields = {'created': 'created_at', 'assigned': 'assigned_at', 'won': 'won_at', 'lost': 'lost_at', 'next': 'next_follow_up_at'}
    model = Lead
    module = 'leads'
    template_name = 'crm/leads/list.html'
    title = 'Leads'
    search_fields = ('name__icontains', 'company__icontains', 'email__icontains', 'phone__contains', 'city__icontains')
    select_related = ('owner', 'source')
    prefetch_related = ('services',)
    default_sort = 'next_follow_up_at'
    sort_fields = {'name': 'name', 'next': 'next_follow_up_at', 'value': 'value', 'created': 'created_at',
                   'last': 'last_activity_at', 'stage': 'stage'}
    quick_filters = QUICK
    export_name = 'leads'
    export_columns = [
        ('Name', 'name'), ('Company', 'company'), ('Phone', 'phone'), ('Email', 'email'), ('City', 'city'),
        ('Source', 'source.name'), ('Owner', lambda l: l.owner.get_full_name() if l.owner else ''),
        ('Stage', lambda l: l.get_stage_display()), ('Priority', lambda l: l.get_priority_display()),
        ('Value', 'value'), ('Next follow-up', 'next_follow_up_at'), ('Last activity', 'last_activity_at'),
        ('Created', 'created_at'),
    ]

    @property
    def filters(self):
        return [
            Filter('stage', 'Stage', STAGES),
            Filter('source', 'Source', lambda r: LeadSource.objects.values_list('pk', 'name'), lookup='source_id'),
            Filter('owner', 'Owner', _owner_choices, apply=_owner_filter),
            Filter('priority', 'Priority', PRIORITIES),
            Filter('service', 'Service', lambda r: Service.objects.filter(is_active=True).values_list('pk', 'name'),
                   apply=lambda qs, v, r: qs.filter(services__id=int(v)).distinct()),
            Filter('tag', 'Tag', lambda r: LeadTag.objects.values_list('pk', 'name'),
                   apply=lambda qs, v, r: qs.filter(tags__id=int(v)).distinct()),
        ]

    def base_queryset(self):
        qs = super().base_queryset()
        if self.request.GET.get('quick') != 'archived':
            qs = qs.filter(archived_at__isnull=True)
        return qs

    def extra_scope_q(self):
        return lead_scope_extra(self.request.user)

    def get(self, request, *args, **kwargs):
        view = request.GET.get('view')
        if view in ('table', 'board'):
            request.session['lead_view'] = view
        elif request.session.get('lead_view') == 'board' and not request.GET:
            return redirect(reverse('crm:pipeline') + '?scope=leads')
        if view == 'board':
            return redirect(reverse('crm:pipeline') + '?scope=leads')
        if view == 'calendar':
            return redirect('crm:followup_calendar')
        return super().get(request, *args, **kwargs)

    def check_export(self, count):
        """Executives exporting more than N rows need manager approval (Part 3.5, 4.12)."""
        from crm.models import ApprovalRequest, CompanySettings
        limit = CompanySettings.get().lead_export_approval_rows
        user = self.request.user
        if count <= limit or permissions.has_perm(user, 'leads', 'approve'):
            return None
        granted = ApprovalRequest.objects.filter(kind='lead_export', requested_by=user, status='approved',
                                                 decided_at__gte=timezone.now() - timedelta(hours=24)).exists()
        if granted:
            return None
        messages.error(self.request, f'Export not allowed above {limit} rows without approval. '
                                     'Use Request approval below, or narrow the filters.')
        q = self.request.GET.copy()
        q.pop('export', None)
        return redirect(f'{self.request.path}?{q.urlencode()}&export_blocked={count}')

    def extra_context(self, qs):
        user = self.request.user
        lead_ids = [l.pk for l in self.object_list_cache] if hasattr(self, 'object_list_cache') else []
        return {
            'module_code': 'leads', 'can_create': permissions.has_perm(user, 'leads', 'create'),
            'can_import': permissions.has_perm(user, 'leads', 'import'),
            'can_assign': permissions.has_perm(user, 'leads', 'assign'),
            'bulk_form': BulkForm(user=user), 'now': timezone.now(),
            'export_blocked': self.request.GET.get('export_blocked'),
            'search_placeholder': 'Search name, company, phone, email',
            'truly_empty': not Lead.objects.exists(),
        }


@staff_required
@require_POST
def lead_export_request(request):
    permissions.require(request.user, 'leads', 'export', request)
    rows = int(request.POST.get('rows') or 0)
    reason = request.POST.get('reason', '').strip()
    profile = request.user.crm_profile
    try:
        approvals.submit('lead_export', profile, title=f'Export {rows} leads ({request.user.get_full_name() or request.user})',
                         module='leads', requested_by=request.user, level='manager',
                         reasons=[f'Export of {rows} rows is above the limit for your role'],
                         payload={'rows': rows, 'query': request.POST.get('query', '')}, message=reason)
        messages.success(request, 'Export approval requested. You will be notified when your manager decides.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:lead_list')


approvals.register('lead_export', lambda req, user: None)


@staff_required
@require_POST
def lead_bulk(request):
    """Bulk actions report per-record results (Part 2.7). There is no bulk delete."""
    form = BulkForm(request.POST, user=request.user)
    ids = [int(i) for i in request.POST.getlist('ids') if i.isdigit()]
    if not ids:
        messages.error(request, 'Select at least one lead.')
        return back(request, reverse('crm:lead_list'))
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return back(request, reverse('crm:lead_list'))
    action = form.cleaned_data['action']
    perm = {'assign': 'assign', 'priority': 'edit', 'tag': 'edit'}[action]
    permissions.require(request.user, 'leads', perm, request)
    leads = list(scoped_leads(request, perm).filter(pk__in=ids))
    ok, failed = 0, []
    for lead in leads:
        try:
            if action == 'assign':
                owner = form.cleaned_data.get('owner')
                if owner is None:
                    raise ValidationError('Choose who to assign to.')
                if not lead.is_open:
                    raise ValidationError('closed leads cannot be reassigned')
                svc.assign(lead, owner, request.user, rule='manual', reason='Bulk assign', override_availability=True)
            elif action == 'priority':
                p = form.cleaned_data.get('priority')
                if not p:
                    raise ValidationError('Choose a priority.')
                Lead.objects.filter(pk=lead.pk).update(priority=p, updated_by=request.user)
                audit.log('lead.updated', lead, changes={'priority': [lead.priority, p]})
            elif action == 'tag':
                tag = form.cleaned_data.get('tag')
                if tag is None:
                    raise ValidationError('Choose a tag.')
                lead.tags.add(tag)
            ok += 1
        except ValidationError as exc:
            failed.append(f'{lead}: {" ".join(exc.messages)}')
    missing = len(ids) - len(leads)
    if missing:
        failed.append(f'{missing} lead(s) are outside your access and were skipped')
    if action == 'assign' and ok:
        owner = form.cleaned_data['owner']
        messages.success(request, f'{ok} lead{"s" if ok != 1 else ""} assigned to {owner.get_full_name() or owner}.')
    elif ok:
        messages.success(request, f'Updated {ok} lead{"s" if ok != 1 else ""}.')
    for f in failed[:10]:
        messages.error(request, f)
    return back(request, reverse('crm:lead_list'))


# ---------------------------------------------------------------- add / edit (4.5)

def _lead_input_from(form):
    c = form.cleaned_data
    return svc.LeadInput(
        name=c['name'], phone=c.get('phone', ''), email=c.get('email', ''), company=c.get('company', ''),
        website=c.get('website', ''), city=c.get('city', ''), state=c.get('state', ''), notes=c.get('notes', ''),
        source=c['source'], campaign=c.get('campaign'), owner=c.get('owner'), priority=c.get('priority') or 'warm',
        next_follow_up_at=c.get('next_follow_up_at'), budget_min=c.get('budget_min'), budget_max=c.get('budget_max'),
        expected_close_date=c.get('expected_close_date'), referrer=c.get('referrer', ''), value=c.get('value'),
    )


@staff_required
def lead_create(request):
    permissions.require(request.user, 'leads', 'create', request)
    form = LeadForm(request.POST or None, user=request.user)
    dup_form = None
    duplicates = []
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        duplicates = [d for d in svc.find_duplicates(c.get('phone', ''), c.get('email', ''))]
        decision = request.POST.get('duplicate_action')
        if duplicates and not decision:
            dup_form = DuplicateDecisionForm(initial={'target': duplicates[0].pk})
        else:
            if duplicates:
                dup_form = DuplicateDecisionForm(request.POST)
            if dup_form is not None and not dup_form.is_valid():
                pass
            else:
                try:
                    if dup_form is not None and dup_form.cleaned_data['duplicate_action'] == 'attach':
                        target = next((d for d in duplicates if d.pk == dup_form.cleaned_data.get('target')), duplicates[0])
                        target = get_lead(request, target.pk)
                        svc.attach_enquiry(target, f'Repeat enquiry entered by {request.user.get_full_name() or request.user}: '
                                                   f'{c.get("notes") or "no notes"}', source=c['source'], actor=request.user)
                        messages.success(request, f'Added as an interaction on the existing lead {target}.')
                        return redirect('crm:lead_detail', pk=target.pk)
                    reason = dup_form.cleaned_data.get('duplicate_reason', '') if dup_form else ''
                    lead = svc.create_lead(_lead_input_from(form), request.user, services=c['services'], tags=c.get('tags') or [],
                                           rule='creator' if c.get('owner') else 'round_robin', duplicate_reason=reason)
                    owner = lead.owner.get_full_name() if lead.owner else 'nobody yet (no executive available)'
                    messages.success(request, f'Lead {lead} saved and assigned to {owner}.')
                    if request.POST.get('another'):
                        return redirect('crm:lead_create')
                    return redirect('crm:lead_detail', pk=lead.pk)
                except ValidationError as exc:
                    form.add_error(None, ' '.join(exc.messages))
    return render(request, 'crm/leads/form.html', {'form': form, 'dup_form': dup_form, 'duplicates': duplicates,
                                                   'is_new': True})


@staff_required
def lead_edit(request, pk):
    lead = get_lead(request, pk, 'edit')
    if lead.archived_at:
        messages.error(request, 'This lead is archived and cannot be edited.')
        return redirect('crm:lead_detail', pk=lead.pk)
    form = LeadForm(request.POST or None, instance=lead, user=request.user)
    if request.method == 'POST' and form.is_valid():
        fields = [f for f in form.changed_data if f not in ('row_version',)]
        before = {f: getattr(lead, f) for f in fields if f not in ('services', 'tags', 'next_follow_up_at')}
        dupes = [d for d in svc.find_duplicates(form.cleaned_data.get('phone', ''), form.cleaned_data.get('email', ''),
                                                exclude_id=lead.pk)]
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            new_next = form.cleaned_data.get('next_follow_up_at')
            if 'next_follow_up_at' in form.changed_data and new_next:
                fu = obj.follow_ups.filter(status='pending').order_by('due_at').first()
                if fu:
                    fu.due_at = new_next
                    fu.save(update_fields=['due_at'])
                elif obj.is_open and obj.owner:
                    FollowUp.objects.create(lead=obj, kind='call', due_at=new_next, assignee=obj.owner, created_by=request.user)
            obj.save()
            form.save_m2m()
            svc.log_update(obj, request.user, before)
            for d in dupes:
                svc.record_duplicate_pair(obj, d, 'Same phone or email after edit', 85)
        messages.success(request, f'Lead {obj} updated.')
        if dupes:
            messages.warning(request, f'This lead now matches {len(dupes)} other lead(s). They are listed for merge review.')
        return redirect('crm:lead_detail', pk=obj.pk)
    return render(request, 'crm/leads/form.html', {'form': form, 'lead': lead, 'is_new': False})


@staff_required
def duplicate_check(request):
    """Async 'Checking…' duplicate lookup on blur (Part 4.5)."""
    permissions.require(request.user, 'leads', 'create', request)
    dupes = svc.find_duplicates(request.GET.get('phone', ''), request.GET.get('email', ''),
                                exclude_id=int(request.GET['exclude']) if request.GET.get('exclude', '').isdigit() else None)
    out = []
    for d in dupes[:3]:
        visible = permissions.in_scope(request.user, 'leads', 'view', d, extra_q=lead_scope_extra(request.user))
        out.append({'id': d.pk if visible else None, 'name': d.name if visible else 'A lead owned by another team',
                    'owner': (d.owner.get_full_name() if d.owner else 'Unassigned') if visible else '',
                    'stage': d.get_stage_display(), 'url': reverse('crm:lead_detail', args=[d.pk]) if visible else ''})
    return JsonResponse({'ok': True, 'duplicates': out})


# ---------------------------------------------------------------- detail (4.6) and popups (4.7)

ACTION_FORMS = {
    'log_call': LogCallForm, 'log_meeting': LogMeetingForm, 'add_note': NoteForm, 'log_message': MessageForm,
    'schedule': ScheduleFollowUpForm, 'complete': CompleteFollowUpForm, 'postpone': PostponeForm,
    'cancel_follow_up': CancelFollowUpForm, 'stage': StageForm, 'won': WonForm, 'lost': LostForm,
    'assign': AssignForm, 'reopen': ReopenForm,
}
ACTION_PERMS = {'assign': 'assign', 'won': 'edit', 'lost': 'edit', 'stage': 'edit', 'reopen': 'edit'}


def _build_forms(request, lead, bound_action=None):
    matches = list(svc.matching_clients(lead.phone, lead.email, name=lead.company)) if not lead.client_id else []
    kw = {
        'schedule': {'user': request.user, 'lead': lead}, 'assign': {'user': request.user},
        'stage': {'lead': lead}, 'won': {'lead': lead, 'matches': matches},
    }
    forms = {}
    for name, cls in ACTION_FORMS.items():
        data = request.POST if name == bound_action else None
        forms[name] = cls(data, prefix=name, **kw.get(name, {}))
    if bound_action is None and request.GET.get('to') in dict(STAGES):
        forms['stage'].initial['to'] = request.GET['to']
    fu = request.GET.get('fu', '')
    if fu.isdigit() and bound_action is None:
        for name in ('complete', 'postpone', 'cancel_follow_up'):
            forms[name].initial['follow_up'] = int(fu)
    return forms, matches


def _detail_context(request, lead, forms, matches, open_modal=''):
    from crm.models import PaymentLink
    user = request.user
    kind = request.GET.get('type', '')
    acts = lead.activities.select_related('actor')
    if kind:
        acts = acts.filter(kind=kind)
    if not (permissions.has_perm(user, 'leads', 'approve') or permissions.can_see(user, 'internal_notes')):
        acts = acts.exclude(visibility='private')
    can_edit = permissions.in_scope(user, 'leads', 'edit', lead, extra_q=lead_scope_extra(user))
    can_quote = permissions.has_perm(user, 'quotations', 'create')
    stage_rules = {s: svc.transition_rules(lead, s) for s, _ in STAGES if s not in CLOSED_STAGES}
    ct = ContentType.objects.get_for_model(Lead)
    return {
        'lead': lead, 'forms': forms, 'activities': acts[:200], 'activity_type': kind,
        'follow_ups': lead.follow_ups.select_related('assignee').order_by('status', 'due_at')[:30],
        'pending_follow_ups': lead.follow_ups.filter(status='pending').select_related('assignee').order_by('due_at'),
        'quotations': lead.quotations.order_by('-id')[:20],
        'payment_links': PaymentLink.objects.filter(quotation__lead=lead).order_by('-id')[:20],
        'stage_history': lead.stage_history.select_related('actor')[:30],
        'assignments': lead.assignments.select_related('from_user', 'to_user', 'actor')[:20],
        'can_edit': can_edit and lead.is_open, 'can_edit_any': can_edit,
        'can_assign': permissions.has_perm(user, 'leads', 'assign'),
        'can_archive': permissions.has_perm(user, 'leads', 'archive'),
        'can_quote': can_quote, 'can_merge': permissions.has_perm(user, 'leads', 'assign'),
        'quote_block_reason': '' if lead.stage in svc.QUALIFIED_PLUS else 'Qualify the lead first',
        'archive_blockers': svc.archive_blockers(lead), 'client_matches': matches,
        'stage_rules': stage_rules, 'open_modal': open_modal,
        'pending_approval': approvals.pending_for(lead),
        'duplicates': svc.find_duplicates(lead.phone, lead.email, exclude_id=lead.pk),
        'attachments': Attachment.objects.filter(content_type=ct, object_id=str(lead.pk), archived_at__isnull=True),
        'can_upload': can_edit, 'activity_kinds': LeadActivity.KINDS, 'now': timezone.now(),
        'wrong_number': request.GET.get('wrong') == '1',
        'lost_reason_label': dict(LOST_REASONS).get(lead.lost_reason, ''),
    }


@staff_required
def lead_detail(request, pk):
    lead = get_lead(request, pk)
    forms, matches = _build_forms(request, lead)
    return render(request, 'crm/leads/detail.html', _detail_context(request, lead, forms, matches,
                                                                     request.GET.get('modal', '')))


@staff_required
@require_POST
def lead_action(request, pk, action):
    if action not in ACTION_FORMS:
        return redirect('crm:lead_detail', pk=pk)
    perm = ACTION_PERMS.get(action, 'edit')
    lead = get_lead(request, pk, 'view')
    permissions.require(request.user, 'leads', perm, request)
    if perm != 'assign':
        permissions.require_object(request.user, 'leads', 'edit', lead, request, extra_q=lead_scope_extra(request.user))
    forms, matches = _build_forms(request, lead, bound_action=action)
    form = forms[action]
    if form.is_valid():
        c = form.cleaned_data
        u = request.user
        try:
            msg, extra = _run_action(request, lead, action, c, matches)
            if msg:
                messages.success(request, msg)
            return redirect(reverse('crm:lead_detail', args=[lead.pk]) + (extra or ''))
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
    lead.refresh_from_db()
    ctx = _detail_context(request, lead, forms, matches, open_modal=action)
    return render(request, 'crm/leads/detail.html', ctx, status=400)


def _run_action(request, lead, action, c, matches):
    u = request.user
    if action == 'log_call':
        svc.log_activity(lead, kind='call', summary=c.get('summary', ''), actor=u, direction=c['direction'],
                         outcome=c['outcome'], duration_minutes=c.get('duration_minutes'), next_due=c.get('next_due'),
                         next_kind=c.get('next_kind') or 'call', next_purpose=c.get('next_purpose', ''))
        return 'Call logged.', '?wrong=1' if c['outcome'] == 'wrong_number' else ''
    if action == 'log_meeting':
        summary = c['summary'] + (f'\nNext step: {c["next_step"]}' if c.get('next_step') else '')
        svc.log_activity(lead, kind='meeting', summary=summary, actor=u, occurred_at=c['occurred_at'],
                         attendees=c.get('attendees', ''), next_due=c.get('next_due'), next_kind=c.get('next_kind') or 'call',
                         next_purpose=c.get('next_purpose', ''))
        return 'Meeting logged.', ''
    if action == 'add_note':
        svc.log_activity(lead, kind='note', summary=c['summary'], actor=u, visibility=c['visibility'])
        return 'Note added.', ''
    if action == 'log_message':
        svc.log_activity(lead, kind=c['channel'], summary=c['summary'], actor=u, direction=c['direction'],
                         next_due=c.get('next_due'), next_kind=c.get('next_kind') or 'call',
                         next_purpose=c.get('next_purpose', ''))
        return f'{c["channel"].title()} logged.', ''
    if action == 'schedule':
        fu = svc.schedule_follow_up(lead, kind=c['kind'], due_at=c['due_at'], assignee=c['assignee'],
                                    purpose=c.get('purpose', ''), reminder_minutes=int(c['reminder_minutes']), actor=u)
        return f'{fu.get_kind_display()} scheduled for {timezone.localtime(fu.due_at):%d-%b-%Y %H:%M}.', ''
    if action in ('complete', 'postpone', 'cancel_follow_up'):
        fu = lead.follow_ups.filter(pk=c['follow_up']).first()
        if fu is None:
            raise ValidationError('That follow-up was not found on this lead.')
        if action == 'complete':
            svc.complete_follow_up(fu, outcome=c['outcome'], notes=c.get('notes', ''), next_due=c.get('next_due'),
                                   next_kind=c.get('next_kind') or 'call', next_purpose=c.get('next_purpose', ''), actor=u)
            return 'Follow-up completed.', ''
        if action == 'postpone':
            reason = dict(PostponeForm.base_fields['reason'].choices).get(c['reason'], c['reason'])
            svc.postpone_follow_up(fu, new_due=c['new_due'], reason=reason, actor=u)
            return 'Follow-up postponed.', ''
        svc.cancel_follow_up(fu, reason=c['reason'], actor=u)
        return 'Follow-up cancelled.', ''
    if action == 'stage':
        svc.change_stage(lead, c['to'], u, reason=c.get('reason', ''), next_due=c.get('next_due'),
                         next_kind=c.get('next_kind') or 'call', next_purpose=c.get('next_purpose', ''))
        return f'Stage changed to {dict(STAGES)[c["to"]]}.', ''
    if action == 'won':
        choice = c.get('client_choice')
        client = None
        if choice and choice != 'new':
            client = next((m for m in matches if str(m.pk) == choice), None)
        svc.mark_won(lead, u, won_value=c['won_value'], quotation=c.get('quotation'), note=c.get('note', ''),
                     expected_start=c.get('expected_start'), client=client, create_client=True)
        return f'🎉 {lead} marked won. Next: create a contract or project for the client.', '?won=1'
    if action == 'lost':
        _, req = svc.mark_lost(lead, u, reason=c['reason'], competitor=c.get('competitor', ''), price=c.get('price'),
                               notes=c.get('notes', ''), win_back_date=c.get('win_back_date'))
        if req:
            return 'This deal is above the approval threshold, so your manager must approve marking it lost.', ''
        return f'{lead} marked lost. A win-back task is scheduled.', ''
    if action == 'assign':
        svc.assign(lead, c['owner'], u, rule='manual', reason=c.get('reason', ''),
                   transfer_follow_ups=c.get('transfer_follow_ups', False), override_availability=c.get('override', False))
        return f'Assigned to {c["owner"].get_full_name() or c["owner"]}.', ''
    if action == 'reopen':
        svc.reopen(lead, u, reason=c['reason'], next_due=c['next_due'])
        return 'Lead re-opened.', ''
    return '', ''


@staff_required
@require_POST
def lead_archive(request, pk):
    lead = get_lead(request, pk, 'view')
    permissions.require(request.user, 'leads', 'archive', request)
    try:
        svc.archive(lead, request.user, request.POST.get('reason', ''))
        messages.success(request, f'{lead} archived. It stays searchable.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:lead_detail', pk=lead.pk)


@staff_required
@require_POST
def lead_delete(request, pk):
    """There is no delete for leads. Forced requests are rejected and audited (Part 4.12)."""
    lead = get_lead(request, pk, 'view')
    audit.log('lead.delete_attempt', lead, result='denied')
    permissions.denied(request.user, 'leads', 'archive', request, detail=f'hard delete lead:{lead.pk}')


@staff_required
@require_POST
def lead_mark_invalid(request, pk):
    lead = get_lead(request, pk, 'edit')
    try:
        svc.mark_lost(lead, request.user, reason='invalid', notes='Wrong number')
        messages.success(request, 'Lead marked invalid (lost: wrong number).')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:lead_detail', pk=lead.pk)


@staff_required
@require_POST
def lead_move(request, pk):
    """Board drag-and-drop / keyboard 'Move to' (Part 5.2). Invalid moves snap back with a message."""
    lead = get_lead(request, pk, 'view')
    if not permissions.in_scope(request.user, 'leads', 'edit', lead, extra_q=lead_scope_extra(request.user)):
        return JsonResponse({'ok': False, 'error': 'You can only move leads you own.'}, status=403)
    to = request.POST.get('to', '')
    detail = reverse('crm:lead_detail', args=[lead.pk])
    if to == 'won':
        return JsonResponse({'ok': False, 'redirect': detail + '?modal=won'})
    if to == 'lost':
        return JsonResponse({'ok': False, 'redirect': detail + '?modal=lost'})
    if to not in dict(STAGES):
        return JsonResponse({'ok': False, 'error': 'Unknown stage.'}, status=400)
    rules = svc.transition_rules(lead, to)
    if not rules['allowed']:
        return JsonResponse({'ok': False, 'error': rules['error']})
    has_future = lead.follow_ups.filter(status='pending', due_at__gt=timezone.now()).exists()
    if rules['needs_reason'] or not has_future:
        return JsonResponse({'ok': False, 'redirect': f'{detail}?modal=stage&to={to}'})
    try:
        svc.change_stage(lead, to, request.user)
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'error': ' '.join(exc.messages)})
    return JsonResponse({'ok': True, 'message': f'{lead} moved to {dict(STAGES)[to]}.'})


@staff_required
@require_POST
def assignment_undo(request, pk):
    from crm.models import LeadAssignment
    a = LeadAssignment.objects.select_related('lead').filter(pk=pk).first()
    if a is None:
        return redirect('crm:lead_list')
    permissions.require(request.user, 'leads', 'assign', request)
    try:
        svc.undo_assignment(a, request.user)
        messages.success(request, 'Assignment undone.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:lead_detail', pk=a.lead_id)
