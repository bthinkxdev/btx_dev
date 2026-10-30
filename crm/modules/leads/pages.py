"""Lead engine pages (Part 4.10-4.11): My Day, follow-up calendar, sources, inbox, duplicates, stale, lost analysis."""
from __future__ import annotations

import calendar as pycal
import json
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, india, permissions
from crm.core.money import ZERO
from crm.core.views import Filter, ListPage, back, form_error_text, get_scoped, staff_required
from crm.models import (ApprovalRequest, Campaign, DuplicateCandidate, FollowUp, Lead, LeadInbox, LeadSource,
                        Payment, PaymentLink, Quotation, SalesTarget)
from crm.models.leads import LOST_REASONS, OPEN_STAGES

from . import services as svc
from .forms import CampaignForm, CompleteFollowUpForm, PostponeForm, SourceForm
from .views import lead_scope_extra, scoped_leads


# ---------------------------------------------------------------- My Day (4.10)

@staff_required
def my_day(request):
    u = request.user
    now = timezone.now()
    today = timezone.localdate()
    end_today = timezone.make_aware(datetime.combine(today, time.max))
    ctx = {'now': now, 'today': today}
    if permissions.has_perm(u, 'leads', 'view'):
        fus = FollowUp.objects.filter(assignee=u, status='pending', lead__archived_at__isnull=True) \
            .select_related('lead', 'lead__owner').order_by('due_at')
        ctx['overdue'] = list(fus.filter(due_at__lt=now)[:50])
        ctx['today_list'] = list(fus.filter(due_at__gte=now, due_at__lte=end_today)[:50])
        ctx['waiting'] = list(Lead.objects.filter(owner=u, stage='new', first_contacted_at__isnull=True,
                                                  archived_at__isnull=True).order_by('assigned_at')[:20])
        for l in ctx['waiting']:
            due = svc.sla_due(l.assigned_at or l.created_at)
            l.sla_seconds = int((due - now).total_seconds())
        ctx['postpone_form'] = PostponeForm(prefix='postpone')
        ctx['complete_form'] = CompleteFollowUpForm(prefix='complete')
    if permissions.has_perm(u, 'quotations', 'view'):
        ctx['quotes_to_chase'] = list(Quotation.objects.filter(owner=u, status__in=['sent', 'viewed'])
                                      .select_related('client', 'lead').order_by('valid_until')[:20])
        ctx['soon'] = today + timedelta(days=3)
    if permissions.has_perm(u, 'payment_links', 'view'):
        links = PaymentLink.objects.filter(owner=u, status__in=['active', 'partially_paid']).filter(
            Q(open_count__gt=0) | Q(expires_at__lte=now + timedelta(hours=48))).select_related('client').order_by('expires_at')
        ctx['links_to_nudge'] = list(links[:20])
    pending = ApprovalRequest.objects.filter(status='pending').exclude(requested_by=u)
    from crm.core import approvals as appr
    ctx['approvals_waiting'] = [r for r in pending.select_related('requested_by')[:100] if appr.can_decide(u, r)[0]][:10]
    month = today.replace(day=1)
    target = SalesTarget.objects.filter(user=u, month=month).first()
    collected = Payment.objects.filter(status__in=['verified', 'partially_refunded'], payment_date__gte=month,
                                       client__owner=u).aggregate(s=Sum('amount'))['s'] or ZERO
    ctx['target'] = target
    ctx['collected'] = collected
    if target and target.collected_value:
        ctx['target_pct'] = min(100, int(collected * 100 / target.collected_value))
        ctx['target_gap'] = max(target.collected_value - collected, ZERO)
    from crm.models import Task
    ctx['my_tasks'] = list(Task.objects.filter(assignee=u, status__in=['todo', 'in_progress', 'blocked', 'in_review'])
                           .select_related('project').order_by('due_date')[:15]) if permissions.has_perm(u, 'tasks', 'view') else []
    return render(request, 'crm/leads/my_day.html', ctx)


@staff_required
@require_POST
def follow_up_quick(request, pk, action):
    """Complete / postpone straight from My Day or the calendar."""
    fu = get_object_or_404(FollowUp.objects.select_related('lead'), pk=pk)
    get_scoped(request, Lead, fu.lead_id, 'leads', 'edit', extra_q=lead_scope_extra(request.user))
    # The follow-up is identified by the URL, not by the form's hidden field (the quick modal is
    # shared by every row, so that field is never filled in client-side).
    data = request.POST.copy()
    data[f'{action}-follow_up'] = str(fu.pk)
    try:
        if action == 'postpone':
            form = PostponeForm(data, prefix='postpone')
            if not form.is_valid():
                raise ValidationError(form_error_text(form))
            reason = dict(form.fields['reason'].choices).get(form.cleaned_data['reason'])
            svc.postpone_follow_up(fu, new_due=form.cleaned_data['new_due'], reason=reason, actor=request.user)
            messages.success(request, 'Follow-up postponed.')
        elif action == 'complete':
            form = CompleteFollowUpForm(data, prefix='complete')
            if not form.is_valid():
                raise ValidationError(form_error_text(form))
            c = form.cleaned_data
            svc.complete_follow_up(fu, outcome=c['outcome'], notes=c.get('notes', ''), next_due=c.get('next_due'),
                                   next_kind=c.get('next_kind') or 'call', next_purpose=c.get('next_purpose', ''),
                                   actor=request.user)
            messages.success(request, 'Follow-up completed.')
        else:
            raise ValidationError('Unknown action.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return back(request, reverse('crm:my_day'))


# ---------------------------------------------------------------- calendar (4.10)

@staff_required
def followup_calendar(request):
    permissions.require(request.user, 'leads', 'view', request)
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        current = date(year, month, 1)
    except (ValueError, TypeError):
        current = today.replace(day=1)
    # Phones get the agenda-style week view by default: a 7-column month grid does not fit.
    is_phone = 'Mobi' in request.META.get('HTTP_USER_AGENT', '')
    view = request.GET.get('view', 'week' if is_phone else 'month')
    owner = request.GET.get('owner', '')
    leads = scoped_leads(request)
    qs = FollowUp.objects.filter(lead__in=leads, status='pending').select_related('lead', 'assignee')
    if owner.isdigit() and permissions.has_perm(request.user, 'leads', 'assign'):
        qs = qs.filter(assignee_id=int(owner))
    elif not permissions.has_perm(request.user, 'leads', 'assign'):
        qs = qs.filter(assignee=request.user)
    if view == 'week':
        start = today - timedelta(days=today.weekday())
        if request.GET.get('start'):
            try:
                start = date.fromisoformat(request.GET['start'])
            except ValueError:
                pass
        days = [start + timedelta(days=i) for i in range(7)]
        rng = (timezone.make_aware(datetime.combine(days[0], time.min)), timezone.make_aware(datetime.combine(days[-1], time.max)))
        items = list(qs.filter(due_at__range=rng).order_by('due_at'))
        by_day = {d: [] for d in days}
        for fu in items:
            by_day.setdefault(timezone.localtime(fu.due_at).date(), []).append(fu)
        ctx = {'view': 'week', 'week': [(d, by_day.get(d, [])) for d in days], 'prev_start': (start - timedelta(days=7)).isoformat(),
               'next_start': (start + timedelta(days=7)).isoformat(), 'start': start}
    else:
        cal = pycal.Calendar(firstweekday=0)
        weeks = cal.monthdatescalendar(current.year, current.month)
        rng = (timezone.make_aware(datetime.combine(weeks[0][0], time.min)),
               timezone.make_aware(datetime.combine(weeks[-1][-1], time.max)))
        by_day = {}
        for fu in qs.filter(due_at__range=rng).order_by('due_at'):
            by_day.setdefault(timezone.localtime(fu.due_at).date(), []).append(fu)
        prev_m = (current - timedelta(days=1)).replace(day=1)
        next_m = india.add_months(current, 1)
        ctx = {'view': 'month', 'weeks': [[(d, by_day.get(d, [])) for d in w] for w in weeks], 'current': current,
               'prev': prev_m, 'next': next_m}
    from .forms import assignable_users
    ctx.update({'today': today, 'owner': owner, 'owners': assignable_users(request.user)
                if permissions.has_perm(request.user, 'leads', 'assign') else [], 'postpone_form': PostponeForm(prefix='postpone'),
                'now': timezone.now()})
    return render(request, 'crm/leads/calendar.html', ctx)


# ---------------------------------------------------------------- sources & campaigns (4.11)

@staff_required
def source_list(request):
    permissions.require(request.user, 'leads', 'configure', request)
    since = timezone.now() - timedelta(days=30)
    sources = LeadSource.objects.annotate(
        leads_30=Count('leads', filter=Q(leads__created_at__gte=since)),
        leads_total=Count('leads'),
        won=Count('leads', filter=Q(leads__stage='won')),
        revenue=Sum('leads__won_value', filter=Q(leads__stage='won')),
    ).order_by('name')
    rows = []
    for s in sources:
        cpl = (s.monthly_cost / s.leads_30) if s.leads_30 and s.monthly_cost else None
        conv = round(s.won * 100 / s.leads_total, 1) if s.leads_total else 0
        rows.append({'s': s, 'cpl': cpl, 'conv': conv})
    return render(request, 'crm/leads/sources.html', {
        'rows': rows, 'campaigns': Campaign.objects.select_related('source').annotate(n=Count('leads'))[:100],
        'inbound_base': request.build_absolute_uri('/crm/api/leads/inbound/'),
    })


@staff_required
def source_edit(request, pk=None):
    permissions.require(request.user, 'leads', 'configure', request)
    obj = get_object_or_404(LeadSource, pk=pk) if pk else None
    form = SourceForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        s = form.save(commit=False)
        if not s.pk:
            s.created_by = request.user
        s.updated_by = request.user
        s.save()
        audit.log('source.saved', s, changes={'fields': form.changed_data})
        messages.success(request, f'Source {s} saved.')
        return redirect('crm:source_list')
    return render(request, 'crm/leads/source_form.html', {'form': form, 'obj': obj})


@staff_required
@require_POST
def source_action(request, pk, action):
    permissions.require(request.user, 'leads', 'configure', request)
    s = get_object_or_404(LeadSource, pk=pk)
    if action == 'rotate':
        s.rotate_key()
        s.save(update_fields=['source_key', 'secret'])
        audit.log('source.key_rotated', s, reason=request.POST.get('reason', ''))
        messages.success(request, f'New key issued for {s}. Update the form or integration with the new URL.')
    elif action == 'pause':
        s.is_paused = not s.is_paused
        s.save(update_fields=['is_paused'])
        audit.log('source.paused' if s.is_paused else 'source.resumed', s)
        messages.success(request, f'{s} {"paused" if s.is_paused else "resumed"}. '
                                  f'{"Inbound payloads are still stored, but not processed until resumed." if s.is_paused else ""}')
    elif action == 'test':
        item = LeadInbox.objects.create(source=s, payload={'name': 'Test Lead', 'phone': '+919000000000',
                                                           'email': 'test.lead@example.com', 'message': 'Source test'},
                                        raw_body='{"test": true}', content_type='application/json', signature_valid=True,
                                        status='received')
        item = svc.ingest(item)
        messages.info(request, f'Test payload stored as inbox #{item.pk}: {item.get_status_display()}.')
    return redirect('crm:source_list')


@staff_required
def campaign_edit(request, pk=None):
    permissions.require(request.user, 'leads', 'configure', request)
    obj = get_object_or_404(Campaign, pk=pk) if pk else None
    form = CampaignForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        c = form.save(commit=False)
        c.created_by = c.created_by or request.user
        c.save()
        messages.success(request, f'Campaign {c.name} saved.')
        return redirect('crm:source_list')
    return render(request, 'crm/leads/campaign_form.html', {'form': form, 'obj': obj})


# ---------------------------------------------------------------- inbound inbox (4.11)

class InboxList(ListPage):
    model = LeadInbox
    module = 'leads'
    action = 'assign'
    template_name = 'crm/leads/inbox.html'
    title = 'Inbound inbox'
    select_related = ('source', 'lead')
    default_sort = '-received_at'
    search_fields = ('raw_body__icontains', 'external_id__icontains', 'last_error__icontains')
    quick_filters = {
        'attention': ('Needs attention', lambda qs, r: qs.filter(status__in=['failed', 'review'])),
        'today': ('Today', lambda qs, r: qs.filter(received_at__date=timezone.localdate())),
    }
    filters = [Filter('status', 'Status', LeadInbox.STATUSES),
               Filter('source', 'Source', lambda r: LeadSource.objects.values_list('pk', 'name'), lookup='source_id')]

    def base_queryset(self):
        permissions.require(self.request.user, 'leads', 'assign', self.request)
        return LeadInbox.objects.select_related('source', 'lead')

    def extra_context(self, qs):
        return {'module_code': 'inbox'}


@staff_required
def inbox_detail(request, pk):
    permissions.require(request.user, 'leads', 'assign', request)
    item = get_object_or_404(LeadInbox.objects.select_related('source', 'lead'), pk=pk)
    pretty = json.dumps(item.payload, indent=2, ensure_ascii=False)
    return render(request, 'crm/leads/inbox_detail.html', {'item': item, 'pretty': pretty,
                                                           'parsed': svc.parse_payload(item.source, item.payload)})


@staff_required
@require_POST
def inbox_action(request, pk, action):
    permissions.require(request.user, 'leads', 'assign', request)
    item = get_object_or_404(LeadInbox, pk=pk)
    if action == 'reprocess':
        if item.status in ('processed', 'duplicate'):
            messages.info(request, 'This payload was already processed; nothing was repeated.')
        else:
            LeadInbox.objects.filter(pk=item.pk).update(status='received', next_retry_at=None)
            item = svc.ingest(item)
            audit.log('inbox.reprocessed', item, changes={'status': item.status})
            if item.status in ('processed', 'duplicate'):
                messages.success(request, f'Processed: {item.get_status_display()}.')
            else:
                messages.error(request, f'Still failing: {item.last_error[:200]}')
    elif action == 'resolve':
        note = request.POST.get('reason', '').strip()
        if len(note) < 5:
            messages.error(request, 'Add a resolution note.')
        else:
            item.status = 'resolved'
            item.resolved_by = request.user
            item.resolution_note = note[:300]
            item.save(update_fields=['status', 'resolved_by', 'resolution_note'])
            audit.log('inbox.resolved', item, reason=note)
            messages.success(request, 'Marked resolved.')
    return redirect('crm:inbox_detail', pk=item.pk)


# ---------------------------------------------------------------- duplicates (4.11)

@staff_required
def duplicate_list(request):
    permissions.require(request.user, 'leads', 'assign', request)
    status = request.GET.get('status', 'open')
    qs = DuplicateCandidate.objects.select_related('lead_a', 'lead_b', 'lead_a__owner', 'lead_b__owner')
    qs = qs.filter(status__in=['open', 'later']) if status == 'open' else qs.filter(status=status)
    # Also surface leads flagged at creation.
    return render(request, 'crm/leads/duplicates.html', {'pairs': qs[:200], 'status': status})


@staff_required
def duplicate_scan(request):
    """Find pairs with the same phone or email that are not recorded yet."""
    permissions.require(request.user, 'leads', 'assign', request)
    found = 0
    base = Lead.objects.filter(archived_at__isnull=True, merged_into__isnull=True)
    for field in ('phone', 'email'):
        dup_values = base.exclude(**{field: ''}).values(field).annotate(n=Count('id')).filter(n__gt=1).values_list(field, flat=True)[:500]
        for value in dup_values:
            ids = list(base.filter(**{field: value}).values_list('pk', flat=True)[:10])
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = Lead.objects.get(pk=ids[i]), Lead.objects.get(pk=ids[j])
                    _, created = DuplicateCandidate.objects.get_or_create(
                        lead_a_id=min(a.pk, b.pk), lead_b_id=max(a.pk, b.pk),
                        defaults={'reason': f'Same {field}', 'score': 95 if field == 'phone' else 90})
                    found += created
    messages.success(request, f'Scan complete: {found} new suspected pair{"s" if found != 1 else ""}.')
    return redirect('crm:duplicate_list')


@staff_required
def duplicate_compare(request, pk):
    permissions.require(request.user, 'leads', 'assign', request)
    pair = get_object_or_404(DuplicateCandidate.objects.select_related('lead_a', 'lead_b'), pk=pk)
    a, b = pair.lead_a, pair.lead_b
    rows = []
    for f in svc.MERGE_FIELDS:
        va, vb = getattr(a, f), getattr(b, f)
        rows.append({'field': f, 'label': Lead._meta.get_field(f).verbose_name.capitalize(), 'a': va, 'b': vb,
                     'differs': str(va or '') != str(vb or '')})
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('not_duplicate', 'later'):
            pair.status = action
            pair.decided_by = request.user
            pair.decided_at = timezone.now()
            pair.note = request.POST.get('note', '')[:300]
            pair.save()
            audit.log('lead.duplicate_' + action, a, changes={'other': b.pk})
            messages.success(request, 'Saved.')
            return redirect('crm:duplicate_list')
        if action in ('merge', 'request_merge'):
            primary_id = int(request.POST.get('primary', a.pk))
            primary, secondary = (a, b) if primary_id == a.pk else (b, a)
            choices = {}
            for f in svc.MERGE_FIELDS:
                pick = request.POST.get(f'keep_{f}', 'a')
                choices[f] = 'primary' if (pick == 'a') == (primary.pk == a.pk) else 'secondary'
            try:
                if action == 'merge' and permissions.has_perm(request.user, 'leads', 'approve'):
                    svc.merge(primary, secondary, request.user, choices)
                    messages.success(request, f'Merged into {primary}. The other record is archived with a pointer.')
                    return redirect('crm:lead_detail', pk=primary.pk)
                from crm.core import approvals
                approvals.submit('lead_merge', primary, title=f'Merge lead #{secondary.pk} into #{primary.pk} ({primary})',
                                 module='leads', requested_by=request.user, level='manager',
                                 reasons=[pair.reason], payload={'secondary': secondary.pk, 'choices': choices},
                                 before={'secondary': str(secondary)}, after={'kept': str(primary)})
                messages.success(request, 'Merge requested. A sales manager must approve it.')
                return redirect('crm:duplicate_list')
            except ValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
    return render(request, 'crm/leads/duplicate_compare.html', {'pair': pair, 'a': a, 'b': b, 'rows': rows,
                                                                'can_merge_now': permissions.has_perm(request.user, 'leads', 'approve')})


# ---------------------------------------------------------------- stale & unassigned, lost analysis

@staff_required
def stale_leads(request):
    permissions.require(request.user, 'leads', 'assign', request)
    from crm.models import CompanySettings
    company = CompanySettings.get()
    cutoff = timezone.now() - timedelta(days=company.stale_days)
    leads = scoped_leads(request).filter(archived_at__isnull=True)
    stale = leads.filter(stage__in=OPEN_STAGES).filter(Q(last_activity_at__lt=cutoff) | Q(last_activity_at__isnull=True)) \
        .select_related('owner', 'source').order_by('last_activity_at')[:200]
    unassigned = leads.filter(owner__isnull=True, stage__in=OPEN_STAGES).select_related('source').order_by('created_at')[:200]
    from .forms import AssignForm
    return render(request, 'crm/leads/stale.html', {'stale': stale, 'unassigned': unassigned, 'company': company,
                                                    'assign_form': AssignForm(user=request.user, prefix='assign'),
                                                    'now': timezone.now()})


@staff_required
def lost_analysis(request):
    permissions.require(request.user, 'leads', 'view', request)
    leads = scoped_leads(request).filter(stage='lost')
    try:
        days = int(request.GET.get('days', 90))
    except ValueError:
        days = 90
    leads = leads.filter(lost_at__gte=timezone.now() - timedelta(days=days))
    reasons = dict(LOST_REASONS)
    by_reason = [(reasons.get(r['lost_reason'], r['lost_reason'] or 'Not given'), r['n'], r['v'] or ZERO)
                 for r in leads.values('lost_reason').annotate(n=Count('id'), v=Sum('value')).order_by('-n')]
    by_source = leads.values('source__name').annotate(n=Count('id'), v=Sum('value')).order_by('-n')
    by_owner = leads.values('owner__first_name', 'owner__last_name', 'owner__username').annotate(n=Count('id'), v=Sum('value')).order_by('-n')
    by_service = leads.values('services__name').annotate(n=Count('id', distinct=True)).order_by('-n')
    competitors = leads.exclude(lost_competitor='').values('lost_competitor').annotate(n=Count('id')).order_by('-n')[:10]
    winback = scoped_leads(request).filter(stage='lost', win_back_date__isnull=False, archived_at__isnull=True) \
        .order_by('win_back_date').select_related('owner')[:50]
    return render(request, 'crm/leads/lost_analysis.html', {
        'by_reason': by_reason, 'by_source': by_source, 'by_owner': by_owner, 'by_service': by_service,
        'competitors': competitors, 'winback': winback, 'days': days, 'total': leads.count(),
        'total_value': leads.aggregate(s=Sum('value'))['s'] or ZERO,
        'chart': {'labels': [r[0] for r in by_reason], 'values': [r[1] for r in by_reason]},
    })


@staff_required
@require_POST
def winback_task(request, pk):
    lead = get_scoped(request, Lead, pk, 'leads', 'edit', extra_q=lead_scope_extra(request.user))
    if lead.stage != 'lost':
        messages.error(request, 'Only lost leads have win-back tasks.')
        return redirect('crm:lead_detail', pk=lead.pk)
    due = india.next_working_day_at()
    FollowUp.objects.create(lead=lead, kind='call', due_at=due, assignee=lead.owner or request.user,
                            purpose='Win-back campaign', created_by=request.user)
    audit.log('lead.winback_task', lead)
    messages.success(request, f'Win-back call scheduled for {timezone.localtime(due):%d-%b-%Y %H:%M}.')
    return back(request, reverse('crm:lost_analysis'))
