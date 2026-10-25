"""Administration pages (Part 13.5-13.6)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, jobs, notify, permissions
from crm.core.views import form_error_text, staff_required
from crm.models import (AuditCheckpoint, AuditLog, CompanySettings, DeliveryLog, Department, DocumentSequence, ExpenseCategory,
                        GatewayEvent, JobRun, MessageTemplate, PostingOutbox, Role, SecurityEvent, SequenceSeries, Team, UserProfile)

from . import services as svc
from .forms import (CategoryTdsForm, CompanyForm, DepartmentForm, GatewayForm, PolicyForm, ResetAccessForm, ReasonForm,
                    RoleCreateForm, SeriesForm, TaxSettingsForm, TeamForm, TemplateForm, UserAccessForm, UserCreateForm)


def _admin(request):
    permissions.require(request.user, 'settings', 'configure', request)


def _err(request, exc):
    messages.error(request, ' '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))


def _date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------- company, policy, tax

def _settings_form_view(request, form_cls, template, *, sensitive=(), title='', success='Settings saved.', extra=None):
    _admin(request)
    company = CompanySettings.get()
    form = form_cls(request.POST or None, instance=company)
    if request.method == 'POST':
        if form.is_valid():
            try:
                changes = svc.save_settings(company, form, request.user, reason=form.cleaned_data.get('reason', ''), sensitive=sensitive)
                messages.success(request, success if changes else 'Nothing changed.')
                return redirect(request.path)
            except ValidationError as exc:
                form.add_error('reason', exc)
        messages.error(request, form_error_text(form))
    ctx = {'form': form, 'company': company, 'title': title,
           'history': AuditLog.objects.filter(object_type='companysettings', action__startswith='settings.')[:15]}
    ctx.update(extra(request) if extra else {})
    return render(request, template, ctx, status=400 if request.method == 'POST' else 200)


@staff_required
def settings_company(request):
    return _settings_form_view(request, CompanyForm, 'crm/administration/company.html', sensitive=svc.SENSITIVE_COMPANY_FIELDS,
                               title='Company settings', success='Company settings saved. New documents use them from now on.')


POLICY_GROUPS = [
    ('Lead response and SLA (Part 4.9)', ['first_response_minutes', 'unassigned_manager_alert_minutes', 'unassigned_owner_alert_minutes',
                                          'overdue_manager_hours', 'overdue_owner_hours', 'work_start_hour', 'work_end_hour', 'default_open_lead_cap']),
    ('Stale and ageing leads', ['stale_days', 'stale_escalate_days', 'aging_qualified_days', 'aging_quotation_sent_days',
                                'aging_negotiation_days', 'postpone_review_count', 'lost_approval_threshold', 'win_back_days']),
    ('Approval limits (Part 3.4)', ['exec_discount_limit', 'manager_discount_limit', 'margin_floor_percent', 'special_terms_days',
                                    'exec_payment_link_limit', 'credit_note_approval_above', 'journal_approval_above',
                                    'expense_manager_limit', 'lead_export_approval_rows']),
    ('Payments and proofs', ['payment_link_default_expiry_days', 'payment_link_max_expiry_days', 'min_partial_payment',
                             'manual_payment_proof_above', 'expense_proof_above']),
    ('Security', ['idle_timeout_minutes']),
]


@staff_required
def settings_policy(request):
    return _settings_form_view(request, PolicyForm, 'crm/administration/policy.html', sensitive=tuple(PolicyForm.Meta.fields),
                               title='Approval and SLA policy', success='Policy saved. It applies to new requests from now on.',
                               extra=lambda r: {'groups': POLICY_GROUPS})


def _tax_extra(request):
    from crm.core.india import fy_label
    fy = fy_label(timezone.localdate())
    series = []
    for s in SequenceSeries.objects.all():
        current = DocumentSequence.objects.filter(doc_type=s.doc_type, fiscal_year__in=[fy, 'ALL']).first()
        series.append({'s': s, 'current': current, 'issued': bool(current and current.next_number > 1),
                       'form': SeriesForm(instance=s, prefix=f's{s.pk}'),
                       'sample': f'{current.prefix if current else s.prefix}/{fy}/{1:0{current.padding if current else s.padding}d}'})
    from crm.models import Service
    return {'series': series, 'fy': fy, 'categories': [(c, CategoryTdsForm(instance=c, prefix=f'c{c.pk}')) for c in ExpenseCategory.objects.all()],
            'services': Service.objects.prefetch_related('tax_rules').order_by('name')[:200]}


@staff_required
def settings_tax(request):
    return _settings_form_view(request, TaxSettingsForm, 'crm/administration/tax.html', sensitive=tuple(TaxSettingsForm.Meta.fields),
                               title='Tax and numbering', success='Tax settings saved.', extra=_tax_extra)


@staff_required
@require_POST
def settings_series(request, pk):
    """Prefix and padding apply to a financial year's series only before its first number (Part 13.6)."""
    _admin(request)
    s = get_object_or_404(SequenceSeries, pk=pk)
    form = SeriesForm(request.POST, instance=s, prefix=f's{s.pk}')
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect('crm:settings_tax')
    before = {'prefix': SequenceSeries.objects.get(pk=pk).prefix, 'padding': SequenceSeries.objects.get(pk=pk).padding}
    with transaction.atomic():
        form.save()
        from crm.core.india import fy_label
        fy = fy_label(timezone.localdate())
        # Untouched sequences (no number issued yet) pick up the new format at once.
        updated = DocumentSequence.objects.filter(doc_type=s.doc_type, next_number=1).update(prefix=s.prefix, padding=s.padding)
        audit.log('settings.numbering_changed', s, actor=request.user,
                  changes=audit.diff(before, {'prefix': s.prefix, 'padding': s.padding}))
    issued = DocumentSequence.objects.filter(doc_type=s.doc_type, fiscal_year__in=[fy, 'ALL'], next_number__gt=1).exists()
    if issued:
        messages.success(request, f'{s.name}: saved. Numbers already issued this year keep their format; the new one starts next financial year.')
    else:
        messages.success(request, f'{s.name}: saved. {updated} unused series updated.')
    return redirect('crm:settings_tax')


@staff_required
@require_POST
def settings_category_tds(request, pk):
    _admin(request)
    c = get_object_or_404(ExpenseCategory, pk=pk)
    before = {f: getattr(c, f) for f in CategoryTdsForm.Meta.fields}
    form = CategoryTdsForm(request.POST, instance=c, prefix=f'c{c.pk}')
    if not form.is_valid():
        messages.error(request, f'{c.name}: {form_error_text(form)}')
        return redirect('crm:settings_tax')
    form.save()
    audit.log('settings.tds_rule_changed', c, actor=request.user, changes=audit.diff(before, {f: getattr(c, f) for f in CategoryTdsForm.Meta.fields}),
              reason=form.cleaned_data['reason'])
    messages.success(request, f'{c.name}: TDS rule saved. It applies to bills entered from now on.')
    return redirect('crm:settings_tax')


@staff_required
def settings_gateway(request):
    _admin(request)
    company = CompanySettings.get()
    form = GatewayForm(request.POST or None, initial={'provider': company.gateway_provider, 'app_id': company.gateway_app_id,
                                                      'methods': company.gateway_methods or ['upi', 'card', 'netbanking', 'wallet']})
    action = request.POST.get('action', 'save') if request.method == 'POST' else ''
    if action == 'test':
        ok, msg = svc.test_gateway()
        audit.log('settings.gateway_tested', company, actor=request.user, result='success' if ok else 'failure', changes={'message': msg})
        (messages.success if ok else messages.error)(request, msg)
        return redirect('crm:settings_gateway')
    if action in ('save', 'live', 'test_mode'):
        if form.is_valid():
            d = form.cleaned_data
            live = True if action == 'live' else False if action == 'test_mode' else None
            try:
                changes = svc.save_gateway(company, request.user, provider=d['provider'], app_id=d['app_id'].strip(), methods=d['methods'],
                                           secret=d['secret'].strip(), webhook_secret=d['webhook_secret'].strip(), live=live, reason=d['reason'])
                messages.success(request, 'Gateway settings saved.' if changes else 'Nothing changed.')
                return redirect('crm:settings_gateway')
            except ValidationError as exc:
                _err(request, exc)
        else:
            messages.error(request, form_error_text(form))
    return render(request, 'crm/administration/gateway.html', {
        'form': form, 'company': company, 'has_secret': bool(company.gateway_secret_enc),
        'has_webhook_secret': bool(company.gateway_webhook_secret_enc),
        'webhook_url': request.build_absolute_uri(reverse('crm:gateway_webhook', args=['cashfree'])),
        'is_owner': permissions.has_role(request.user, 'owner') or request.user.is_superuser,
        'recent_events': GatewayEvent.objects.order_by('-id')[:10],
        'history': AuditLog.objects.filter(action__startswith='settings.gateway')[:10],
    })


# ---------------------------------------------------------------- users

@staff_required
def user_list(request):
    _admin(request)
    User = get_user_model()
    qs = User.objects.filter(crm_profile__isnull=False).select_related('crm_profile__team', 'crm_profile__department') \
        .prefetch_related('crm_profile__roles').order_by('-is_active', 'first_name', 'username')
    term = request.GET.get('q', '').strip()
    if term:
        qs = qs.filter(Q(username__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(email__icontains=term))
    status = request.GET.get('status', '')
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'locked':
        qs = qs.filter(crm_profile__locked_until__gt=timezone.now())
    if request.GET.get('role', '').isdigit():
        qs = qs.filter(crm_profile__roles__id=int(request.GET['role']))
    form = UserCreateForm(request.POST or None, prefix='new')
    if request.method == 'POST':
        if form.is_valid():
            d = form.cleaned_data
            try:
                user, password = svc.create_user(request.user, username=d['username'], first_name=d['first_name'], last_name=d['last_name'],
                                                 email=d['email'], roles=list(d['roles']), team=d['team'], department=d['department'],
                                                 manager=d['manager'], designation=d['designation'], receives_leads=d['receives_leads'])
                request.session['crm_temp_password'] = {'user': user.pk, 'password': password}
                messages.success(request, f'{user.get_full_name() or user.username} created.')
                return redirect('crm:user_detail', pk=user.pk)
            except ValidationError as exc:
                form.add_error(None, exc)
        messages.error(request, form_error_text(form))
    page = Paginator(qs.distinct(), 50).get_page(request.GET.get('page'))
    return render(request, 'crm/administration/users.html', {
        'page_obj': page, 'form': form, 'roles': Role.objects.all(), 'q': term, 'status': status, 'now': timezone.now(),
        'open_modal': 'new-user' if request.method == 'POST' else ''}, status=400 if request.method == 'POST' else 200)


@staff_required
def user_detail(request, pk):
    _admin(request)
    from crm.models import UserSession
    User = get_user_model()
    user = get_object_or_404(User.objects.select_related('crm_profile'), pk=pk, crm_profile__isnull=False)
    p = user.crm_profile
    access = UserAccessForm(prefix='acc', initial={'roles': p.roles.all(), 'team': p.team, 'department': p.department, 'manager': p.manager,
                                                   'designation': p.designation, 'receives_leads': p.receives_leads,
                                                   'open_lead_cap': p.open_lead_cap})
    temp = request.session.pop('crm_temp_password', None)
    if temp and temp.get('user') != user.pk:
        temp = None
    from crm.models import Lead
    from crm.models.leads import OPEN_STAGES
    return render(request, 'crm/administration/user_detail.html', {
        'u': user, 'p': p, 'access': access, 'temp_password': temp['password'] if temp else '',
        'reason_form': ReasonForm(prefix='r'), 'reset_form': ResetAccessForm(prefix='rs'),
        'sessions': UserSession.objects.filter(user=user, ended_at__isnull=True)[:20],
        'events': SecurityEvent.objects.filter(user=user)[:15],
        'history': AuditLog.objects.filter(object_type='user', object_id=str(user.pk))[:20],
        'open_leads': Lead.objects.filter(owner=user, stage__in=OPEN_STAGES, archived_at__isnull=True).count(),
        'locked': bool(p.locked_until and p.locked_until > timezone.now()), 'is_self': user.pk == request.user.pk,
        'open_modal': request.GET.get('modal', ''),
    })


@staff_required
@require_POST
def user_action(request, pk, action):
    _admin(request)
    User = get_user_model()
    user = get_object_or_404(User, pk=pk, crm_profile__isnull=False)
    try:
        if action == 'access':
            f = UserAccessForm(request.POST, prefix='acc')
            if not f.is_valid():
                raise ValidationError(form_error_text(f))
            d = f.cleaned_data
            changes = svc.update_access(user, request.user, roles=list(d['roles']), team=d['team'], department=d['department'],
                                        manager=d['manager'], designation=d['designation'], receives_leads=d['receives_leads'],
                                        open_lead_cap=d['open_lead_cap'], reason=d['reason'])
            messages.success(request, 'Access saved. It applies from the user\'s next click.' if changes else 'Nothing changed.')
        elif action == 'deactivate':
            f = ReasonForm(request.POST, prefix='r')
            if not f.is_valid():
                raise ValidationError('Give a reason of at least 10 characters.')
            r = svc.deactivate(user, request.user, reason=f.cleaned_data['reason'], reassign=request.POST.get('reassign') == '1')
            messages.success(request, f'Deactivated and signed out of {r["sessions"]} session(s). {r["moved"]} open lead(s) reassigned'
                                      + (f', {r["unassigned"]} left unassigned for a manager.' if r['unassigned'] else '.'))
        elif action == 'reactivate':
            f = ReasonForm(request.POST, prefix='r')
            if not f.is_valid():
                raise ValidationError('Give a reason of at least 10 characters.')
            svc.reactivate(user, request.user, f.cleaned_data['reason'])
            messages.success(request, 'Reactivated. Review their roles before they sign in.')
        elif action == 'reset':
            f = ResetAccessForm(request.POST, prefix='rs')
            if not f.is_valid():
                raise ValidationError('Give a reason of at least 10 characters.')
            password = svc.reset_access(user, request.user, reset_two_factor=f.cleaned_data['reset_two_factor'], reason=f.cleaned_data['reason'])
            request.session['crm_temp_password'] = {'user': user.pk, 'password': password}
            messages.success(request, 'Access reset. Share the one-time password privately; it is shown once.')
        elif action == 'signout':
            from crm.modules.hr.services import end_sessions
            n = end_sessions(user)
            audit.log('user.sessions_ended', user, actor=request.user, changes={'sessions': n})
            audit.security_event('sessions_revoked', user=user, request=request, detail=f'{n} sessions, by {request.user}')
            messages.success(request, f'Signed out of {n} session(s).')
        else:
            raise Http404
    except ValidationError as exc:
        _err(request, exc)
    return redirect('crm:user_detail', pk=user.pk)


# ---------------------------------------------------------------- roles and permission matrix

@staff_required
def role_list(request):
    _admin(request)
    roles = list(Role.objects.annotate(users=Count('profiles', filter=Q(profiles__user__is_active=True))).order_by('name'))
    role = None
    if request.GET.get('role', '').isdigit():
        role = next((r for r in roles if r.pk == int(request.GET['role'])), None)
    role = role or (roles[0] if roles else None)
    comparison, other = [], None
    if role and request.GET.get('compare', '').isdigit():
        other = next((r for r in roles if r.pk == int(request.GET['compare'])), None)
        if other:
            comparison = svc.compare(role, other)
    if request.GET.get('export') == 'csv' and role:
        permissions.require(request.user, 'settings', 'export', request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Module'] + list(permissions.ACTIONS.values()))
        for row in svc.matrix(role):
            w.writerow([row['label']] + [scope for _, scope in row['cells']])
        w.writerow([])
        w.writerow(['Field groups', ', '.join(role.field_groups or [])])
        w.writerow(['Denied field groups', ', '.join(role.denied_field_groups or [])])
        audit.log('export.created', role, actor=request.user, changes={'what': 'permission matrix'})
        resp = HttpResponse(('﻿' + buf.getvalue()).encode('utf-8'), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="role-{role.code}.csv"'
        return resp
    return render(request, 'crm/administration/roles.html', {
        'roles': roles, 'role': role, 'matrix': svc.matrix(role) if role else [], 'actions': permissions.ACTIONS,
        'scopes': permissions.SCOPES, 'field_groups': permissions.FIELD_GROUPS, 'comparison': comparison, 'other': other,
        'create_form': RoleCreateForm(prefix='nr'),
        'history': AuditLog.objects.filter(object_type='role', object_id=str(role.pk))[:10] if role else [],
        'is_owner_role': role is not None and role.code == 'owner',
    })


@staff_required
@require_POST
def role_save(request, pk):
    _admin(request)
    role = get_object_or_404(Role, pk=pk)
    grants: dict = {}
    for m in permissions.MODULES:
        for a in permissions.ACTIONS:
            v = request.POST.get(f'g__{m}__{a}', '')
            if v:
                grants.setdefault(m, {})[a] = v
    try:
        changes = svc.save_role_grants(role, request.user, grants=grants, field_groups=request.POST.getlist('field_groups'),
                                       denied_groups=request.POST.getlist('denied_groups'), reason=request.POST.get('reason', ''))
        n = role.profiles.filter(user__is_active=True).count()
        messages.success(request, f'{role.name} saved. {n} user(s) get the change on their next click.' if changes else 'Nothing changed.')
    except ValidationError as exc:
        _err(request, exc)
    return redirect(f"{reverse('crm:role_list')}?role={role.pk}")


@staff_required
@require_POST
def role_create(request):
    _admin(request)
    f = RoleCreateForm(request.POST, prefix='nr')
    if not f.is_valid():
        messages.error(request, form_error_text(f))
        return redirect('crm:role_list')
    try:
        role = svc.create_role(request.user, name=f.cleaned_data['name'], code=f.cleaned_data['code'],
                               description=f.cleaned_data['description'], copy_from=f.cleaned_data['copy_from'])
        messages.success(request, f'Role {role.name} created. Set its permissions below.')
        return redirect(f"{reverse('crm:role_list')}?role={role.pk}")
    except ValidationError as exc:
        _err(request, exc)
        return redirect('crm:role_list')


# ---------------------------------------------------------------- teams and departments

@staff_required
def team_list(request):
    _admin(request)
    kind = request.POST.get('kind', '')
    pk = request.POST.get('pk', '')
    team_form = TeamForm(prefix='t')
    dept_form = DepartmentForm(prefix='d')
    if request.method == 'POST':
        if kind == 'team':
            inst = get_object_or_404(Team, pk=pk) if pk.isdigit() else None
            before = {f: str(getattr(inst, f)) for f in TeamForm.Meta.fields} if inst else {}
            team_form = TeamForm(request.POST, instance=inst, prefix='t')
            if team_form.is_valid():
                t = team_form.save()
                audit.log('team.saved', t, actor=request.user, changes=audit.diff(before, {f: str(getattr(t, f)) for f in TeamForm.Meta.fields}))
                messages.success(request, f'Team {t.name} saved.')
                return redirect('crm:team_list')
            messages.error(request, form_error_text(team_form))
        elif kind == 'department':
            inst = get_object_or_404(Department, pk=pk) if pk.isdigit() else None
            before = {f: str(getattr(inst, f)) for f in DepartmentForm.Meta.fields} if inst else {}
            dept_form = DepartmentForm(request.POST, instance=inst, prefix='d')
            if dept_form.is_valid():
                d = dept_form.save()
                audit.log('department.saved', d, actor=request.user, changes=audit.diff(before, {f: str(getattr(d, f)) for f in DepartmentForm.Meta.fields}))
                messages.success(request, f'Department {d.name} saved.')
                return redirect('crm:team_list')
            messages.error(request, form_error_text(dept_form))
    departments = Department.objects.select_related('parent', 'head').annotate(n=Count('members', distinct=True))
    tree = []

    def walk(parent_id, depth):
        for d in [x for x in departments if x.parent_id == parent_id]:
            tree.append((d, depth))
            walk(d.pk, depth + 1)
    walk(None, 0)
    teams = Team.objects.select_related('department', 'manager').annotate(n=Count('members', filter=Q(members__user__is_active=True)))
    edit_teams = {t.pk: TeamForm(instance=t, prefix='t') for t in teams}
    edit_depts = {d.pk: DepartmentForm(instance=d, prefix='d') for d in departments}
    # A failed edit re-opens its own popup with the errors.
    if kind == 'team' and pk.isdigit() and int(pk) in edit_teams:
        edit_teams[int(pk)], team_form = team_form, TeamForm(prefix='t')
    if kind == 'department' and pk.isdigit() and int(pk) in edit_depts:
        edit_depts[int(pk)], dept_form = dept_form, DepartmentForm(prefix='d')
    return render(request, 'crm/administration/teams.html', {
        'tree': tree, 'teams': teams, 'team_form': team_form, 'dept_form': dept_form,
        'edit_teams': edit_teams, 'edit_depts': edit_depts,
        'open_modal': ('new-team' if kind == 'team' and not pk else f'team-{pk}' if kind == 'team' else
                       'new-dept' if kind == 'department' and not pk else f'dept-{pk}' if kind == 'department' else ''),
    }, status=400 if request.method == 'POST' else 200)


# ---------------------------------------------------------------- message templates and delivery log

SAMPLE = {'name': 'Anu Thomas', 'company': 'BThinkX', 'number': 'INV/2026-27/0042', 'total': '₹1,18,000.00', 'amount': '₹59,000.00',
          'balance': '₹59,000.00', 'link': 'https://example.com/pay/sample', 'valid_until': '30-Nov-2026', 'due_date': '15-Nov-2026',
          'expires': '30-Nov-2026 18:00', 'description': 'Website development, first milestone', 'sender': 'Rahul (BThinkX)'}


@staff_required
def template_list(request):
    _admin(request)
    tab = request.GET.get('tab', 'templates')
    if tab == 'log':
        qs = DeliveryLog.objects.all()
        if request.GET.get('status') in dict(DeliveryLog.STATUSES):
            qs = qs.filter(status=request.GET['status'])
        if request.GET.get('q'):
            qs = qs.filter(Q(recipient__icontains=request.GET['q']) | Q(subject__icontains=request.GET['q']))
        page = Paginator(qs, 50).get_page(request.GET.get('page'))
        return render(request, 'crm/administration/templates.html', {'tab': tab, 'page_obj': page, 'statuses': DeliveryLog.STATUSES})
    latest = {}
    for t in MessageTemplate.objects.all():
        key = (t.code, t.channel)
        if key not in latest or (t.is_active and not latest[key].is_active) or (t.is_active == latest[key].is_active and t.version > latest[key].version):
            latest[key] = t
    return render(request, 'crm/administration/templates.html', {'tab': tab, 'templates': sorted(latest.values(), key=lambda t: t.name)})


@staff_required
def template_edit(request, pk):
    _admin(request)
    tpl = get_object_or_404(MessageTemplate, pk=pk)
    form = TemplateForm(request.POST or None, instance=MessageTemplate(name=tpl.name, subject=tpl.subject, body=tpl.body), prefix='tp')
    preview = None
    if request.method == 'POST' and form.is_valid():
        if request.POST.get('action') == 'preview':
            preview = {'subject': notify.render(form.cleaned_data['subject'], SAMPLE), 'body': notify.render(form.cleaned_data['body'], SAMPLE)}
        else:
            with transaction.atomic():
                last = MessageTemplate.objects.select_for_update().filter(code=tpl.code, channel=tpl.channel).order_by('-version').first()
                new = MessageTemplate.objects.create(code=tpl.code, channel=tpl.channel, name=form.cleaned_data['name'],
                                                     subject=form.cleaned_data['subject'], body=form.cleaned_data['body'],
                                                     version=last.version + 1, customer_facing=tpl.customer_facing, created_by=request.user)
                MessageTemplate.objects.filter(code=tpl.code, channel=tpl.channel).exclude(pk=new.pk).update(is_active=False)
                audit.log('template.changed', new, actor=request.user, reason=form.cleaned_data['reason'],
                          changes=audit.diff({'subject': last.subject, 'body': last.body, 'version': last.version},
                                             {'subject': new.subject, 'body': new.body, 'version': new.version}))
            messages.success(request, f'{new.name} v{new.version} is now live. Earlier versions are kept for the record.')
            return redirect('crm:template_edit', pk=new.pk)
    elif request.method == 'POST':
        messages.error(request, form_error_text(form))
    elif request.GET.get('restore', '').isdigit():
        old = get_object_or_404(MessageTemplate, pk=int(request.GET['restore']), code=tpl.code, channel=tpl.channel)
        form = TemplateForm(instance=MessageTemplate(name=old.name, subject=old.subject, body=old.body), prefix='tp')
        messages.info(request, f'Loaded v{old.version}. Give a reason and save to make it live again.')
    if preview is None:
        preview = {'subject': notify.render(tpl.subject, SAMPLE), 'body': notify.render(tpl.body, SAMPLE)}
    return render(request, 'crm/administration/template_edit.html', {
        'tpl': tpl, 'form': form, 'preview': preview, 'sample': SAMPLE,
        'versions': MessageTemplate.objects.filter(code=tpl.code, channel=tpl.channel).select_related('created_by').order_by('-version'),
    })


@staff_required
@require_POST
def delivery_retry(request, pk):
    _admin(request)
    log = get_object_or_404(DeliveryLog, pk=pk)
    if log.status not in ('failed', 'queued', 'bounced'):
        messages.info(request, 'Only failed or queued messages can be retried.')
    elif log.attachment_name:
        messages.error(request, 'This message had an attachment. Send it again from its record so the attachment is regenerated.')
    else:
        notify.deliver(log.pk)
        log.refresh_from_db()
        audit.log('email.retried', log, actor=request.user, changes={'status': log.status})
        (messages.success if log.status == 'sent' else messages.error)(request, 'Sent.' if log.status == 'sent' else f'Still failing: {log.error}')
    return redirect(reverse('crm:template_list') + '?tab=log')


# ---------------------------------------------------------------- jobs and health

def _health():
    last_integrity = JobRun.objects.filter(job='integrity.nightly').exclude(status='running').order_by('-id').first()
    day = timezone.now() - timedelta(hours=24)
    runs = JobRun.objects.filter(created_at__gte=day)
    return {
        'posting_pending': PostingOutbox.objects.filter(status='pending').count(),
        'posting_failed': PostingOutbox.objects.filter(status='failed').count(),
        'events_failed': GatewayEvent.objects.filter(status='failed').count(),
        'email_failed': DeliveryLog.objects.filter(status='failed', created_at__gte=day).count(),
        'last_checkpoint': AuditCheckpoint.objects.order_by('-id').first(),
        'last_integrity': last_integrity, 'runs_24h': runs.count(), 'failed_24h': runs.filter(status='failed').count(),
        'error_rate': round(runs.filter(status='failed').count() * 100 / runs.count(), 1) if runs.count() else 0,
        'integrity': _integrity_results(last_integrity),
    }


def _integrity_results(run):
    """Check results stored on the last integrity run (passed or failed)."""
    value = ((run.result or {}).get('value') if run is not None else None)
    return value['checks'] if isinstance(value, dict) and 'checks' in value else None


@staff_required
def job_list(request):
    permissions.require(request.user, 'automation', 'view', request)
    qs = JobRun.objects.all()
    status = request.GET.get('status', '')
    if status in dict(JobRun.STATUSES):
        qs = qs.filter(status=status)
    if request.GET.get('job') in jobs.JOBS:
        qs = qs.filter(job=request.GET['job'])
    last = {}
    for name in jobs.JOBS:
        last[name] = JobRun.objects.filter(job=name).order_by('-id').first()
    registry = [{'name': n, 'group': g, 'minutes': m, 'last': last[n]} for n, (_, g, m) in jobs.JOBS.items()]
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'crm/administration/jobs.html', {
        'page_obj': page, 'registry': registry, 'status': status, 'statuses': JobRun.STATUSES, 'health': _health(),
        'can_run': permissions.has_perm(request.user, 'automation', 'configure'), 'job_names': list(jobs.JOBS)})


@staff_required
@require_POST
def job_action(request, action):
    permissions.require(request.user, 'automation', 'configure', request)
    if action == 'run':
        name = request.POST.get('job', '')
        if name not in jobs.JOBS:
            raise Http404
        r = jobs.run_job(name, force=True)
        audit.log('job.run_manually', actor=request.user, object_type='job', object_id=name, changes=r)
        (messages.success if r['status'] == 'succeeded' else messages.error)(request, f'{name}: {r["status"]}.')
    elif action in ('retry', 'resolve'):
        run = get_object_or_404(JobRun, pk=request.POST.get('pk', 0) or 0, status='failed')
        if action == 'retry':
            # Jobs are idempotent: financial records and messages are keyed, so a retry cannot duplicate them.
            r = jobs.run_job(run.job, force=True)
            if r['status'] == 'succeeded':
                JobRun.objects.filter(pk=run.pk).update(status='resolved', resolved_reason=f'Retried successfully by {request.user}')
            audit.log('job.retried', actor=request.user, object_type='jobrun', object_id=str(run.pk), changes=r)
            (messages.success if r['status'] == 'succeeded' else messages.error)(request, f'{run.job}: retry {r["status"]}.')
        else:
            reason = request.POST.get('reason', '').strip()
            if len(reason) < 10:
                messages.error(request, 'Give a reason of at least 10 characters.')
            else:
                JobRun.objects.filter(pk=run.pk).update(status='resolved', resolved_reason=reason[:300])
                audit.log('job.resolved', actor=request.user, object_type='jobrun', object_id=str(run.pk), reason=reason)
                messages.success(request, 'Marked resolved.')
    elif action == 'integrity':
        r = jobs.run_job('integrity.nightly', force=True)
        audit.log('job.run_manually', actor=request.user, object_type='job', object_id='integrity.nightly', changes=r)
        if r['status'] == 'succeeded':
            messages.success(request, 'All integrity checks passed.')
        else:
            messages.error(request, 'Some integrity checks failed. See the results below.')
        return redirect(reverse('crm:job_list') + '#integrity')
    else:
        raise Http404
    return redirect(request.POST.get('next') if (request.POST.get('next') or '').startswith('/crm/') else reverse('crm:job_list'))


# ---------------------------------------------------------------- audit log and security events

def _audit_filtered(request):
    qs = AuditLog.objects.all()
    g = request.GET
    if g.get('user', '').isdigit():
        qs = qs.filter(actor_id=int(g['user']))
    if g.get('object_type'):
        qs = qs.filter(object_type=g['object_type'])
    if g.get('object_id'):
        qs = qs.filter(object_id=g['object_id'][:64])
    if g.get('action'):
        qs = qs.filter(action__icontains=g['action'][:80])
    if g.get('result') in ('success', 'failure', 'denied'):
        qs = qs.filter(result=g['result'])
    start, end = _date(g.get('from')), _date(g.get('to'))
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min)))
    if end:
        qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(end, time.max)))
    return qs


@staff_required
def audit_log(request):
    permissions.require(request.user, 'audit', 'view', request)
    qs = _audit_filtered(request)
    filters = {k: v for k, v in request.GET.items() if k not in ('page', 'export')}
    if request.GET.get('export') == 'csv':
        permissions.require(request.user, 'audit', 'export', request)
        n = qs.count()
        audit.log('audit.exported', actor=request.user, object_type='auditlog', changes={'rows': n, 'filters': filters})
        from crm.models import ExportLog
        ExportLog.objects.create(user=request.user, module='audit', filters=filters, columns=['time', 'actor', 'action', 'object'], row_count=n)

        def rows():
            yield '﻿'
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['Time (UTC)', 'Actor type', 'Actor', 'Action', 'Object type', 'Object id', 'Object', 'Result', 'Changes', 'Reason',
                        'Request id', 'Job id', 'IP', 'Channel'])
            yield buf.getvalue()
            for a in qs.iterator(chunk_size=1000):
                buf = io.StringIO()
                csv.writer(buf).writerow([a.created_at.isoformat(), a.actor_type, a.actor_name, a.action, a.object_type, a.object_id,
                                          _cell(a.object_repr), a.result, _cell(str(a.changes)), _cell(a.reason), a.request_id, a.job_id,
                                          a.ip or '', a.channel])
                yield buf.getvalue()
        from django.http import StreamingHttpResponse
        resp = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="audit-{timezone.localdate():%Y%m%d}.csv"'
        return resp
    # Every view of the log is itself logged (Part 13.5).
    audit.log('audit.viewed', actor=request.user, object_type='auditlog', changes={'filters': filters})
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    User = get_user_model()
    return render(request, 'crm/administration/audit.html', {
        'page_obj': page, 'users': User.objects.filter(crm_profile__isnull=False).order_by('first_name'),
        'object_types': AuditLog.objects.order_by().values_list('object_type', flat=True).distinct()[:100],
        'can_export': permissions.has_perm(request.user, 'audit', 'export'), 'f': request.GET,
    })


def _cell(v: str) -> str:
    v = v or ''
    return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


@staff_required
def security_events(request):
    permissions.require(request.user, 'audit', 'view', request)
    qs = SecurityEvent.objects.select_related('user')
    g = request.GET
    if g.get('kind') in dict(SecurityEvent.KINDS):
        qs = qs.filter(kind=g['kind'])
    if g.get('q'):
        qs = qs.filter(Q(username__icontains=g['q']) | Q(ip__startswith=g['q']) | Q(detail__icontains=g['q']))
    start, end = _date(g.get('from')), _date(g.get('to'))
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min)))
    if end:
        qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(end, time.max)))
    if g.get('export') == 'csv':
        permissions.require(request.user, 'audit', 'export', request)
        n = qs.count()
        audit.log('security.exported', actor=request.user, object_type='securityevent', changes={'rows': n})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Time (UTC)', 'Event', 'User', 'IP', 'Path', 'Detail', 'User agent'])
        for e in qs[:50000]:
            w.writerow([e.created_at.isoformat(), e.get_kind_display(), e.username, e.ip or '', e.path, _cell(e.detail), _cell(e.user_agent)])
        resp = HttpResponse(('﻿' + buf.getvalue()).encode('utf-8'), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="security-events-{timezone.localdate():%Y%m%d}.csv"'
        return resp
    audit.log('security.viewed', actor=request.user, object_type='securityevent')
    day = timezone.now() - timedelta(hours=24)
    summary = SecurityEvent.objects.filter(created_at__gte=day).values('kind').annotate(n=Count('id')).order_by('-n')
    kinds = dict(SecurityEvent.KINDS)
    return render(request, 'crm/administration/security.html', {
        'page_obj': Paginator(qs, 50).get_page(g.get('page')), 'kinds': SecurityEvent.KINDS, 'f': g,
        'summary': [(kinds.get(r['kind'], r['kind']), r['n'], r['kind']) for r in summary],
        'can_export': permissions.has_perm(request.user, 'audit', 'export'),
        'locked': UserProfile.objects.filter(locked_until__gt=timezone.now()).select_related('user'),
    })


@staff_required
@require_POST
def security_unlock(request, pk):
    _admin(request)
    p = get_object_or_404(UserProfile, pk=pk)
    p.locked_until = None
    p.failed_logins = 0
    p.save(update_fields=['locked_until', 'failed_logins'])
    audit.log('user.unlocked', p.user, actor=request.user)
    audit.security_event('lockout', user=p.user, request=request, detail=f'unlocked by {request.user}')
    messages.success(request, f'{p.user.get_full_name() or p.user.username} unlocked.')
    return redirect('crm:security_events')
