"""Pipeline board (Part 5.2) and sales targets / performance / funnel (Part 5.3)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import audit, india, permissions
from crm.core.forms import CrmModelForm, MoneyFormField
from crm.core.money import ZERO, q2
from crm.core.views import staff_required
from crm.models import (Lead, LeadSource, Payment, PaymentLink, Quotation, SalesTarget, Service, Team)
from crm.models.leads import OPEN_STAGES, PIPELINE_STAGES, STAGES
from crm.modules.leads.forms import assignable_users
from crm.modules.leads.views import scoped_leads

STAGE_LABELS = dict(STAGES)


@staff_required
def pipeline(request):
    permissions.require(request.user, 'leads', 'view', request)
    qs = scoped_leads(request).filter(archived_at__isnull=True).select_related('owner', 'client').prefetch_related('quotations')
    g = request.GET
    if g.get('owner', '').isdigit():
        qs = qs.filter(owner_id=int(g['owner']))
    if g.get('service', '').isdigit():
        qs = qs.filter(services__id=int(g['service'])).distinct()
    if g.get('source', '').isdigit():
        qs = qs.filter(source_id=int(g['source']))
    try:
        if g.get('min'):
            qs = qs.filter(value__gte=Decimal(g['min']))
        if g.get('max'):
            qs = qs.filter(value__lte=Decimal(g['max']))
    except Exception:
        pass
    if g.get('close'):
        try:
            y, m = map(int, g['close'].split('-'))
            qs = qs.filter(expected_close_date__year=y, expected_close_date__month=m)
        except ValueError:
            pass
    if g.get('stale') == '1':
        qs = qs.filter(Q(last_activity_at__lt=timezone.now() - timedelta(days=3)) | Q(last_activity_at__isnull=True))
    recent = timezone.now() - timedelta(days=30)
    columns = []
    can_move_any = permissions.has_perm(request.user, 'leads', 'edit')
    for stage in PIPELINE_STAGES:
        stage_qs = qs.filter(stage=stage)
        if stage in ('won', 'lost'):
            stage_qs = stage_qs.filter(stage_changed_at__gte=recent)
        cards = list(stage_qs.order_by('next_follow_up_at', '-value')[:60])
        total = sum((c.value for c in cards), ZERO)
        weighted = sum((c.weighted_value for c in cards), ZERO)
        columns.append({'stage': stage, 'label': STAGE_LABELS[stage], 'cards': cards, 'count': stage_qs.count(),
                        'total': total, 'weighted': weighted})
    open_total = sum((c['weighted'] for c in columns if c['stage'] in OPEN_STAGES), ZERO)
    return render(request, 'crm/sales/pipeline.html', {
        'columns': columns, 'now': timezone.now(), 'weighted_open': open_total,
        'owners': assignable_users(request.user) if permissions.has_perm(request.user, 'leads', 'assign') else [],
        'services': Service.objects.filter(is_active=True), 'sources': LeadSource.objects.filter(is_active=True),
        'move_targets': [(s, STAGE_LABELS[s]) for s in PIPELINE_STAGES if s not in ('new',)],
        'can_move_any': can_move_any, 'can_quote': permissions.has_perm(request.user, 'quotations', 'create'),
    })


class TargetForm(CrmModelForm):
    won_value = MoneyFormField()
    collected_value = MoneyFormField()
    month = forms.DateField(widget=forms.DateInput(attrs={'type': 'month'}, format='%Y-%m'), input_formats=['%Y-%m', '%Y-%m-%d'])

    class Meta:
        model = SalesTarget
        fields = ['month', 'user', 'team', 'lead_count', 'quotation_count', 'won_value', 'collected_value']

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = assignable_users(user)
        self.fields['user'].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields['user'].required = False
        self.fields['team'].required = False

    def clean_month(self):
        return self.cleaned_data['month'].replace(day=1)

    def clean(self):
        c = super().clean()
        if bool(c.get('user')) == bool(c.get('team')):
            raise forms.ValidationError('Set a target for either one person or one team.')
        return c


@staff_required
def targets(request):
    permissions.require(request.user, 'deals', 'view', request)
    today = india.today()
    try:
        month = date.fromisoformat(request.GET['month'] + '-01') if request.GET.get('month') else today.replace(day=1)
    except ValueError:
        month = today.replace(day=1)
    nxt = india.add_months(month, 1)
    User = get_user_model()
    scope = permissions.get_scope(request.user, 'deals', 'view')
    people = User.objects.filter(is_active=True, crm_profile__receives_leads=True)
    if scope == 'own':
        people = people.filter(pk=request.user.pk)
    elif scope in ('team', 'department'):
        people = people.filter(pk__in=permissions.team_member_ids(request.user))
    rows = []
    for u in people.order_by('first_name'):
        t = SalesTarget.objects.filter(user=u, month=month).first()
        leads_in = Lead.objects.filter(owner=u, created_at__date__gte=month, created_at__date__lt=nxt).count()
        quotes = Quotation.objects.filter(owner=u, sent_at__date__gte=month, sent_at__date__lt=nxt, version=1).count()
        won = Lead.objects.filter(owner=u, stage='won', won_at__date__gte=month, won_at__date__lt=nxt).aggregate(s=Sum('won_value'))['s'] or ZERO
        collected = Payment.objects.filter(status__in=['verified', 'partially_refunded'], client__owner=u,
                                           payment_date__gte=month, payment_date__lt=nxt).aggregate(s=Sum('amount'))['s'] or ZERO
        weighted = sum((l.weighted_value for l in Lead.objects.filter(owner=u, stage__in=OPEN_STAGES, archived_at__isnull=True,
                                                                      expected_close_date__gte=month, expected_close_date__lt=nxt)), ZERO)
        target_c = t.collected_value if t else ZERO
        rows.append({
            'user': u, 'target': t, 'leads': leads_in, 'quotes': quotes, 'won': won, 'collected': collected,
            'forecast': collected + weighted, 'gap': max(target_c - collected, ZERO) if t else None,
            'pct': int(collected * 100 / target_c) if t and target_c else None,
        })
    # Funnel over the month's cohort
    scoped = scoped_leads(request)
    cohort = scoped.filter(created_at__date__gte=month, created_at__date__lt=nxt)
    from crm.models import StageHistory
    reached = {}
    order = ['new', 'contacted', 'qualified', 'quotation_prepared', 'quotation_sent', 'negotiation', 'won']
    total = cohort.count()
    for s in order:
        if s == 'new':
            reached[s] = total
        else:
            reached[s] = cohort.filter(Q(stage_history__to_stage=s) | Q(stage=s)).distinct().count()
    funnel = []
    prev = None
    for s in order:
        n = reached[s]
        conv = round(n * 100 / prev, 1) if prev else None
        funnel.append({'stage': s, 'label': STAGE_LABELS[s], 'count': n, 'conv': conv,
                       'width': int(n * 100 / total) if total else 0})
        prev = n or prev
    # Activity quality
    from crm.models import LeadActivity
    quality = []
    for u in people.order_by('first_name'):
        acts = LeadActivity.objects.filter(actor=u, occurred_at__date__gte=month, occurred_at__date__lt=nxt)
        days = max(1, min((today - month).days + 1, (nxt - month).days))
        frt = Lead.objects.filter(owner=u, first_response_seconds__isnull=False, created_at__date__gte=month,
                                  created_at__date__lt=nxt).aggregate(a=Avg('first_response_seconds'))['a']
        quality.append({
            'user': u, 'calls_per_day': round(acts.filter(kind='call').count() / days, 1),
            'first_response_min': int(frt // 60) if frt else None,
            'overdue': u.crm_follow_ups.filter(status='pending', due_at__lt=timezone.now()).count(),
            'quotes': Quotation.objects.filter(owner=u, sent_at__date__gte=month, sent_at__date__lt=nxt).count(),
            'links_paid': PaymentLink.objects.filter(owner=u, paid_at__date__gte=month, paid_at__date__lt=nxt).count(),
        })
    form = TargetForm(user=request.user, initial={'month': month.strftime('%Y-%m')})
    return render(request, 'crm/sales/targets.html', {
        'rows': rows, 'month': month, 'prev': india.add_months(month, -1), 'next': nxt, 'funnel': funnel, 'quality': quality,
        'form': form, 'can_configure': permissions.has_perm(request.user, 'deals', 'configure'),
    })


@staff_required
@require_POST
def target_save(request):
    permissions.require(request.user, 'deals', 'configure', request)
    form = TargetForm(request.POST, user=request.user)
    if form.is_valid():
        c = form.cleaned_data
        lookup = {'month': c['month'], 'user': c.get('user'), 'team': c.get('team')}
        existing = SalesTarget.objects.filter(**{k: v for k, v in lookup.items() if v is not None}).first()
        before = {f: str(getattr(existing, f)) for f in ('lead_count', 'quotation_count', 'won_value', 'collected_value')} if existing else {}
        obj = existing or SalesTarget(month=c['month'], user=c.get('user'), team=c.get('team'), created_by=request.user)
        for f in ('lead_count', 'quotation_count', 'won_value', 'collected_value'):
            setattr(obj, f, c[f])
        obj.updated_by = request.user
        obj.save()
        audit.log('target.saved', obj, changes=audit.diff(before, {f: str(getattr(obj, f)) for f in before or
                                                                  ('lead_count', 'quotation_count', 'won_value', 'collected_value')}))
        messages.success(request, 'Target saved.')
    else:
        from crm.core.views import form_error_text
        messages.error(request, form_error_text(form))
    return redirect(f"/crm/targets/?month={request.POST.get('month', '')[:7]}")
