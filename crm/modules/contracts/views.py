"""Contract pages: list, builder, detail with lifecycle popups, renewals calendar, billing schedule (Part 9.4-9.7, 7.10)."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.http import Http404

from crm.core import approvals, audit, india, permissions
from crm.core.money import ZERO
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import AuditLog, BillingSchedule, Client, Contract, Quotation
from crm.models.delivery import CONTRACT_STATUSES

from . import services as svc
from .forms import AmendForm, ContractForm, ContractLineFormSet, RenewForm, ScheduleRowForm, TerminateForm


class ContractList(ListPage):
    date_fields = {'end': 'end_date', 'start': 'start_date'}
    model = Contract
    module = 'contracts'
    template_name = 'crm/contracts/list.html'
    title = 'Contracts'
    search_fields = ('number__icontains', 'title__icontains', 'client__display_name__icontains')
    select_related = ('client', 'owner')
    sort_fields = {'end': 'end_date', 'start': 'start_date'}
    filters = [Filter('status', 'Status', CONTRACT_STATUSES)]
    quick_filters = {
        'active': ('Active', lambda qs, r: qs.filter(status='active')),
        'renewal': ('Renewal due (60 days)', lambda qs, r: qs.filter(status='active', end_date__lte=india.today() + timedelta(days=60))),
        'mine': ('Mine', lambda qs, r: qs.filter(owner=r.user)),
        'drafts': ('Drafts', lambda qs, r: qs.filter(status__in=['draft', 'pending_approval'])),
    }
    export_name = 'contracts'
    export_columns = [('Number', 'number'), ('Version', 'version'), ('Client', 'client'), ('Title', 'title'), ('Start', 'start_date'),
                      ('End', 'end_date'), ('Value', 'value'), ('Status', lambda c: c.get_status_display()), ('Owner', 'owner')]

    def base_queryset(self):
        return super().base_queryset().prefetch_related('lines')

    def extra_context(self, qs):
        return {'module_code': 'contracts', 'mrr': svc.mrr(), 'search_placeholder': 'Search number, title, client'}


def get_contract(request, pk, action='view'):
    return get_scoped(request, Contract, pk, 'contracts', action,
                      queryset=Contract.objects.select_related('client', 'owner', 'quotation', 'previous'))


def _builder(request, c, is_new, initial_lines=None):
    user = request.user
    form = ContractForm(request.POST or None, instance=c, user=user, can_change_owner=permissions.has_perm(user, 'contracts', 'approve'))
    if request.method == 'GET' and initial_lines:
        from django.forms import inlineformset_factory
        from crm.models import ContractLine
        from .forms import BaseContractLines, ContractLineForm
        lines = inlineformset_factory(Contract, ContractLine, form=ContractLineForm, formset=BaseContractLines,
                                      extra=len(initial_lines), can_delete=True)(instance=c, prefix='lines', initial=initial_lines)
    else:
        lines = ContractLineFormSet(request.POST or None, instance=c, prefix='lines')
    if request.method == 'POST':
        if form.is_valid() and lines.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False)
                    if obj.pk and Contract.objects.select_for_update().get(pk=obj.pk).status != 'draft':
                        raise ValidationError('Only a draft contract can be edited. Amend an active contract instead.')
                    if not obj.pk:
                        obj.created_by = user
                    obj.updated_by = user
                    obj.save()
                    lines.instance = obj
                    lines.save()
                    audit.log('contract.saved', obj, changes={'fields': form.changed_data})
                messages.success(request, 'Contract saved as draft.')
                return redirect('crm:contract_detail', pk=obj.pk)
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        else:
            messages.error(request, 'Please fix the highlighted problems. Nothing was saved.')
    return render(request, 'crm/contracts/form.html', {'form': form, 'lines': lines, 'c': c, 'is_new': is_new})


@staff_required
def contract_create(request):
    permissions.require(request.user, 'contracts', 'create', request)
    q = None
    qid = request.GET.get('quotation') or request.POST.get('src_quotation')
    cid = request.GET.get('client') or request.POST.get('src_client')
    if qid:
        q = get_scoped(request, Quotation, qid, 'quotations', 'view', queryset=Quotation.objects.select_related('client', 'lead'))
        if q.status not in ('accepted', 'converted'):
            messages.error(request, 'Only an accepted quotation can become a contract.')
            return redirect('crm:quotation_detail', pk=q.pk)
        client = q.client
        if client is None and q.lead_id:
            from crm.modules.clients.services import ensure_client_for_lead
            client = ensure_client_for_lead(q.lead, request.user)
    elif cid:
        client = get_scoped(request, Client, cid, 'clients', 'view')
    else:
        messages.info(request, 'Create a contract from a client or an accepted quotation.')
        return redirect('crm:client_list')
    c = svc.prefill(client=client, owner=request.user, quotation=q)
    initial = svc.lines_from_quotation(q) if q else []
    for row in initial:
        row['start_date'] = c.start_date
    return _builder(request, c, True, initial)


@staff_required
def contract_edit(request, pk):
    c = get_contract(request, pk, 'edit')
    if c.status != 'draft':
        messages.info(request, 'Only a draft can be edited. Use Amend for an active contract.')
        return redirect('crm:contract_detail', pk=c.pk)
    return _builder(request, c, False)


def _detail_ctx(request, c, forms=None, open_modal=''):
    user = request.user
    forms = forms or {}
    defaults = {'amend': AmendForm(prefix='amend', initial={'effective_from': india.today()}),
                'renew': RenewForm(prefix='renew', initial={'new_end': india.add_months(c.end_date, 12) if c.end_date else None}),
                'terminate': TerminateForm(prefix='terminate', initial={'on': india.today()})}
    defaults.update(forms)
    rows = BillingSchedule.objects.filter(contract_line__contract=c).select_related('contract_line').order_by('invoice_date', 'id')
    invoices = c.invoices.all() if hasattr(c, 'invoices') else []
    versions = []
    v = c
    while v is not None and len(versions) < 20:
        versions.insert(0, v)
        v = v.previous
    nxt = getattr(c, 'amendment', None)
    while nxt is not None and len(versions) < 40:
        versions.append(nxt)
        nxt = getattr(nxt, 'amendment', None)
    pending = approvals.pending_for(c)
    return {'c': c, 'lines': c.lines.select_related('service'), 'rows': rows, 'invoices': invoices, 'versions': versions,
            'forms': defaults, 'open_modal': open_modal, 'pending': pending,
            'can_decide': approvals.can_decide(user, pending)[0] if pending else False,
            'can_edit': permissions.in_scope(user, 'contracts', 'edit', c), 'blockers': svc.validate_ready(c) if c.status == 'draft' else [],
            'reasons': svc.activation_reasons(c, user) if c.status == 'draft' else [],
            'can_invoice': permissions.has_perm(user, 'invoices', 'create'),
            'projects': c.projects.all(), 'tickets': c.tickets.all()[:10],
            'activity': AuditLog.objects.filter(object_type='contract', object_id=str(c.pk))[:30],
            'row_form': ScheduleRowForm(prefix='row'), 'today': india.today()}


@staff_required
def contract_detail(request, pk):
    c = get_contract(request, pk)
    return render(request, 'crm/contracts/detail.html', _detail_ctx(request, c, open_modal=request.GET.get('modal', '')))


FORMS = {'amend': AmendForm, 'renew': RenewForm, 'terminate': TerminateForm}


@staff_required
@require_POST
def contract_action(request, pk, action):
    c = get_contract(request, pk)
    user = request.user
    permissions.require(user, 'contracts', 'edit', request)
    permissions.require_object(user, 'contracts', 'edit', c, request)
    form = None
    if action in FORMS:
        form = FORMS[action](request.POST, prefix=action)
        if not form.is_valid():
            return render(request, 'crm/contracts/detail.html', _detail_ctx(request, c, {action: form}, action), status=400)
    d = form.cleaned_data if form else {}
    try:
        if action == 'activate':
            res = svc.submit_or_activate(c, user, request.POST.get('message', ''))
            messages.success(request, f'Contract {res.number} is active; its billing schedule is generated.' if res else
                             'Sent for manager approval.')
        elif action == 'amend':
            new = svc.amend(c, user, effective_from=d['effective_from'], reason=d['reason'])
            messages.success(request, f'Version {new.version} created as a draft. Edit the lines, then activate it.')
            return redirect('crm:contract_edit', pk=new.pk)
        elif action == 'renew':
            new = svc.renew(c, user, new_end=d['new_end'], price_change_percent=d.get('price_change_percent') or ZERO, note=d.get('note', ''))
            messages.success(request, f'Renewed until {new.end_date:%d-%b-%Y} as version {new.version}.')
            return redirect('crm:contract_detail', pk=new.pk)
        elif action == 'terminate':
            svc.terminate(c, user, on=d['on'], reason=d['reason'])
            messages.success(request, 'Contract terminated; later billing rows are skipped.')
        elif action == 'suspend':
            svc.suspend(c, user, request.POST.get('reason', ''))
            messages.success(request, 'Contract suspended. Billing pauses.')
        elif action == 'resume':
            svc.resume(c, user)
            messages.success(request, 'Contract resumed.')
        elif action == 'complete':
            svc.complete(c, user)
            messages.success(request, 'Contract completed.')
        elif action == 'lost_renewal':
            svc.record_lost_renewal(c, user, request.POST.get('reason', ''))
            messages.success(request, 'Recorded. Auto-renew is off.')
        elif action == 'regenerate':
            n = svc.generate_schedule(c)
            messages.success(request, f'{n} new schedule row(s).')
        elif action in ('row_edit', 'row_invoice', 'row_retry'):
            row = get_object_or_404(BillingSchedule, pk=request.POST.get('row'), contract_line__contract=c)
            if action == 'row_edit':
                f = ScheduleRowForm(request.POST, prefix='row')
                if not f.is_valid():
                    raise ValidationError(' '.join(e for errs in f.errors.values() for e in errs))
                svc.edit_row(row, user, invoice_date=f.cleaned_data['invoice_date'], amount=f.cleaned_data['amount'])
                messages.success(request, 'Schedule row updated.')
            else:
                permissions.require(user, 'invoices', 'create', request)
                inv = svc.retry_row(row, user) if action == 'row_retry' else svc.invoice_for_row(row, user, issue=False)
                messages.success(request, f'Draft invoice created for {row.period_start:%d-%b-%Y}. Review and issue it.')
                return redirect('crm:invoice_detail', pk=inv.pk)
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:contract_detail', pk=c.pk)


@staff_required
def renewals(request):
    permissions.require(request.user, 'contracts', 'view', request)
    qs = permissions.scoped(Contract.objects.filter(status='active', end_date__isnull=False), request.user, 'contracts', 'view') \
        .select_related('client', 'owner').prefetch_related('lines').order_by('end_date')
    today = india.today()
    months = defaultdict(list)
    for c in qs.filter(end_date__lte=india.add_months(today, 12)):
        months[india.month_start(c.end_date)].append(c)
    cal = [{'month': m, 'contracts': cs, 'value': sum((c.value for c in cs), ZERO),
            'mrr': sum((ln.monthly_value for c in cs for ln in c.lines.all()), ZERO)} for m, cs in sorted(months.items())]
    year_ago = india.add_months(today, -12)
    ended = Contract.objects.filter(end_date__range=(year_ago, today)).exclude(status='draft')
    renewed = sum(1 for c in ended if getattr(c, 'amendment', None) is not None and c.amendment.amendment_reason == 'Renewal')
    rate = round(renewed * 100 / ended.count()) if ended.count() else None
    return render(request, 'crm/contracts/renewals.html', {'cal': cal, 'overdue': qs.filter(end_date__lt=today), 'rate': rate,
                                                           'mrr': svc.mrr(), 'moves': svc.mrr_movements(india.month_start(today), today)})


@staff_required
def billing_schedule(request):
    permissions.require(request.user, 'contracts', 'view', request)
    status = request.GET.get('status', 'upcoming')
    qs = BillingSchedule.objects.select_related('contract_line__contract__client').order_by('invoice_date')
    allowed = permissions.scoped(Contract.objects.all(), request.user, 'contracts', 'view').values('pk')
    qs = qs.filter(contract_line__contract__in=allowed)
    today = india.today()
    if status == 'upcoming':
        qs = qs.filter(status='scheduled', invoice_date__lte=today + timedelta(days=7), contract_line__contract__status='active')
    elif status in dict(BillingSchedule.STATUSES):
        qs = qs.filter(status=status)
    return render(request, 'crm/contracts/schedule.html', {'rows': qs[:300], 'status': status, 'statuses': BillingSchedule.STATUSES,
                                                           'today': today})
