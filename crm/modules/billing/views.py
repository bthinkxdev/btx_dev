"""Invoice pages (Part 7.3-7.9): list, builder, detail with popups, credit notes, receivables, advances, TDS, statements."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, india, notify, pdf, permissions
from crm.core.money import D, ZERO
from crm.core.views import Filter, ListPage, back, get_scoped, staff_required
from crm.models import (AuditLog, Client, CompanySettings, CreditNote, DeliveryLog, Invoice, InvoiceLine, PaymentLink, Quotation,
                        Service, TdsReceivable)
from crm.models.billing import INV_OPEN, INVOICE_STATUSES

from . import services as svc
from .forms import (CreditNoteForm, DisputeForm, InvoiceForm, InvoiceLineFormSet, PromiseForm, SendInvoiceForm, StatementForm,
                    TdsUpdateForm)


def _client_choices(request):
    return [(c.pk, c.display_name) for c in permissions.scoped(Client.objects.filter(archived_at__isnull=True), request.user,
                                                               'clients', 'view').order_by('display_name')[:300]]


def _overdue(lo, hi=None):
    def f(qs, value, request):
        today = india.today()
        qs = qs.filter(status__in=INV_OPEN, balance__gt=0, due_date__lte=today - timedelta(days=lo))
        return qs.filter(due_date__gt=today - timedelta(days=hi)) if hi else qs
    return f


BUCKETS = {'1-30': (1, 31), '31-60': (31, 61), '61-90': (61, 91), '90+': (91, None)}


class InvoiceList(ListPage):
    model = Invoice
    module = 'invoices'
    template_name = 'crm/billing/invoice_list.html'
    title = 'Invoices'
    search_fields = ('number__icontains', 'client__display_name__icontains', 'customer_name__icontains', 'po_number__icontains')
    select_related = ('client', 'owner')
    default_sort = '-id'
    sort_fields = {'date': 'invoice_date', 'due': 'due_date', 'total': 'grand_total', 'balance': 'balance'}
    filters = [Filter('status', 'Status', INVOICE_STATUSES), Filter('client', 'Client', _client_choices, lookup='client_id'),
               Filter('age', 'Overdue', [(k, f'{k} days') for k in BUCKETS],
                      apply=lambda qs, v, r: _overdue(*BUCKETS[v])(qs, v, r))]
    quick_filters = {
        'mine': ('Mine', lambda qs, r: qs.filter(owner=r.user)),
        'drafts': ('Drafts', lambda qs, r: qs.filter(status__in=['draft', 'pending_approval'])),
        'unpaid': ('Unpaid', lambda qs, r: qs.filter(status__in=INV_OPEN, balance__gt=0)),
        'overdue': ('Overdue', lambda qs, r: qs.filter(status__in=INV_OPEN, balance__gt=0, due_date__lt=india.today())),
        'due_week': ('Due in 7 days', lambda qs, r: qs.filter(status__in=INV_OPEN, balance__gt=0, due_date__gte=india.today(),
                                                             due_date__lte=india.today() + timedelta(days=7))),
        'disputed': ('Disputed', lambda qs, r: qs.filter(disputed=True)),
        'recurring': ('Recurring', lambda qs, r: qs.filter(billing_schedule__isnull=False)),
    }
    export_name = 'invoices'
    export_columns = [('Number', 'display_number'), ('Client', 'client'), ('Issue date', 'invoice_date'), ('Due date', 'due_date'),
                      ('Taxable', 'taxable_total'), ('Tax', 'tax_total'), ('Total', 'grand_total'), ('Paid', 'paid_amount'),
                      ('Advance adjusted', 'advance_adjusted'), ('TDS withheld', 'tds_withheld'), ('Credit noted', 'credit_noted'),
                      ('Balance', 'balance'), ('Status', lambda i: i.get_status_display()), ('Age', 'age_bucket'), ('Owner', 'owner')]

    def extra_context(self, qs):
        user = self.request.user
        open_qs = qs.filter(status__in=INV_OPEN)
        return {'module_code': 'invoices', 'can_create': permissions.has_perm(user, 'invoices', 'create'),
                'can_remind': permissions.has_perm(user, 'invoices', 'edit'),
                'search_placeholder': 'Search number, client, PO',
                'sum_balance': open_qs.aggregate(s=Sum('balance'))['s'] or ZERO}


def get_invoice(request, pk, action='view'):
    return get_scoped(request, Invoice, pk, 'invoices', action,
                      queryset=Invoice.objects.select_related('client', 'owner', 'quotation', 'contract', 'project'))


# ---------------------------------------------------------------- builder (7.4)

def _catalog(on_date):
    data = {}
    for s in Service.objects.filter(is_active=True):
        taxability, rate = s.tax_for(on_date)
        data[s.pk] = {'description': s.name, 'sac': s.sac_code, 'unit': s.unit, 'price': str(s.default_price),
                      'taxability': taxability, 'rate': f'{rate.normalize():f}', 'floor': '0'}
    return data


def _builder(request, inv: Invoice, is_new: bool, initial_lines=None):
    user = request.user
    can_override = permissions.has_perm(user, 'invoices', 'override')
    form = InvoiceForm(request.POST or None, instance=inv, user=user,
                       can_change_owner=permissions.has_perm(user, 'invoices', 'approve'))
    on_date = inv.invoice_date
    if request.method == 'GET' and initial_lines:
        from django.forms import inlineformset_factory
        from crm.modules.quotations.forms import BaseLineFormSet
        from .forms import InvoiceLineForm
        factory = inlineformset_factory(Invoice, InvoiceLine, form=InvoiceLineForm, formset=BaseLineFormSet,
                                        extra=len(initial_lines), can_delete=True)
        lines = factory(instance=inv, prefix='lines', can_override=can_override, on_date=on_date, initial=initial_lines)
    else:
        lines = InvoiceLineFormSet(request.POST or None, instance=inv, prefix='lines', can_override=can_override, on_date=on_date)
    if request.method == 'POST':
        if form.is_valid() and lines.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False)
                    if obj.pk:
                        locked = Invoice.objects.select_for_update().get(pk=obj.pk)
                        if locked.status != 'draft':
                            raise ValidationError('This invoice is no longer a draft, so it cannot be edited.')
                    else:
                        obj.created_by = user
                    obj.updated_by = user
                    obj.save()
                    lines.instance = obj
                    for i, f in enumerate(lines.forms):
                        if f.cleaned_data and not f.cleaned_data.get('DELETE'):
                            f.instance.sort = i
                    lines.save()
                    svc.recalculate(obj)
                    audit.log('invoice.created' if is_new else 'invoice.saved', obj, changes={'fields': form.changed_data})
                messages.success(request, f'Saved draft at {timezone.localtime():%H:%M}. Issue it when ready.')
                if request.POST.get('next') == 'detail':
                    return redirect('crm:invoice_detail', pk=obj.pk)
                return redirect('crm:invoice_edit', pk=obj.pk)
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        else:
            messages.error(request, 'Please fix the highlighted problems. Nothing was saved.')
    return render(request, 'crm/billing/builder.html', {
        'form': form, 'lines': lines, 'inv': inv, 'is_new': is_new, 'catalog': _catalog(on_date), 'can_override': can_override,
        'advances': svc.open_advances(inv.client) if inv.client_id else [],
    })


@staff_required
def invoice_create(request):
    permissions.require(request.user, 'invoices', 'create', request)
    quotation = None
    src_q = request.GET.get('quotation') or request.POST.get('src_quotation')
    src_c = request.GET.get('client') or request.POST.get('src_client')
    if src_q:
        quotation = get_scoped(request, Quotation, src_q, 'quotations', 'view', queryset=Quotation.objects.select_related('client', 'lead'))
        if quotation.status not in ('accepted', 'converted'):
            messages.error(request, 'Only an accepted quotation can be invoiced.')
            return redirect('crm:quotation_detail', pk=quotation.pk)
        client = quotation.client
        if client is None and quotation.lead_id:
            from crm.modules.clients.services import ensure_client_for_lead
            client = ensure_client_for_lead(quotation.lead, request.user)
            quotation.refresh_from_db()
        if client is None:
            messages.error(request, 'Link the quotation to a client first.')
            return redirect('crm:quotation_detail', pk=quotation.pk)
    elif src_c:
        client = get_scoped(request, Client, src_c, 'clients', 'view')
    else:
        messages.info(request, 'Start an invoice from a client, an accepted quotation or a contract.')
        return redirect('crm:client_list')
    if client.is_restricted:
        messages.error(request, 'This client is restricted. New invoices are blocked.')
        return redirect('crm:client_detail', pk=client.pk)
    inv = svc.prefill(client=client, owner=request.user if not quotation else quotation.owner, quotation=quotation)
    if not permissions.has_perm(request.user, 'invoices', 'approve'):
        inv.owner = request.user
    initial = []
    if quotation is not None:
        for ql in quotation.lines.all():
            initial.append({'service': ql.service_id, 'description': ql.description, 'sac_code': ql.sac_code, 'quantity': ql.quantity,
                            'unit': ql.unit, 'unit_price': ql.unit_price, 'discount_mode': ql.discount_mode,
                            'discount_value': ql.discount_value, 'taxability': ql.taxability,
                            'tax_rate': f'{ql.tax_rate.normalize():f}'})
    if client.credit_limit:
        from crm.modules.clients.services import outstanding
        if outstanding(client) > client.credit_limit:
            messages.warning(request, f'Outstanding is above the credit limit of {pdf._money(client.credit_limit)}.')
    return _builder(request, inv, is_new=True, initial_lines=initial)


@staff_required
def invoice_edit(request, pk):
    inv = get_invoice(request, pk, 'edit')
    if inv.status != 'draft':
        messages.info(request, 'An issued invoice cannot change. Use a credit note to reduce it.')
        return redirect('crm:invoice_detail', pk=inv.pk)
    return _builder(request, inv, is_new=False)


@staff_required
@require_POST
def invoice_preview_totals(request):
    permissions.require(request.user, 'invoices', 'create', request)
    from crm.modules.quotations.views import preview_totals
    return preview_totals(request, round_off=CompanySettings.get().round_off_invoice, module='invoices')


# ---------------------------------------------------------------- detail and actions

def _detail_ctx(request, inv, forms=None, open_modal=''):
    user = request.user
    forms = forms or {}
    contact = inv.client.billing_contact
    defaults = {
        'send': SendInvoiceForm(prefix='send', initial={'to': (contact.email if contact and contact.email else inv.client.email)}),
        'credit': CreditNoteForm(prefix='credit', invoice=inv, initial={'taxable': inv.taxable_total}),
        'promise': PromiseForm(prefix='promise', initial={'amount': inv.balance, 'date': india.today() + timedelta(days=7)}),
        'dispute': DisputeForm(prefix='dispute'),
    }
    defaults.update(forms)
    can_edit = permissions.in_scope(user, 'invoices', 'edit', inv)
    blockers = svc.issue_blockers(inv) if inv.status == 'draft' else []
    reasons = svc.issue_approval_reasons(inv, user) if inv.status == 'draft' else []
    pending = approvals.pending_for(inv)
    return {
        'inv': inv, 'lines': inv.lines.select_related('service'), 'company': CompanySettings.get(), 'forms': defaults,
        'open_modal': open_modal, 'can_edit': can_edit, 'blockers': blockers, 'reasons': reasons, 'pending': pending,
        'can_decide': approvals.can_decide(user, pending)[0] if pending else False,
        'allocations': inv.allocations.select_related('payment').order_by('id'),
        'credit_notes': inv.credit_notes.all(), 'links': PaymentLink.objects.filter(invoice=inv),
        'payments_pending': inv.direct_payments.filter(status__in=['pending_verification', 'review']),
        'advances': svc.open_advances(inv.client) if inv.status in INV_OPEN + ['draft'] else [],
        'deliveries': DeliveryLog.objects.filter(object_type='invoice', object_id=str(inv.pk))[:20],
        'activity': AuditLog.objects.filter(object_type='invoice', object_id=str(inv.pk))[:40],
        'can_cancel': permissions.has_perm(user, 'invoices', 'cancel') and permissions.has_perm(user, 'accounting', 'approve'),
        'can_credit': permissions.has_perm(user, 'invoices', 'edit'), 'can_link': permissions.has_perm(user, 'payment_links', 'create'),
        'can_record': permissions.has_perm(user, 'payments', 'create'), 'show_internal': permissions.can_see(user, 'internal_notes'),
        'history': approvals.history_for(inv), 'reminder_blocked': svc.reminder_blocked(inv) if inv.status in INV_OPEN else '',
    }


@staff_required
def invoice_detail(request, pk):
    inv = get_invoice(request, pk)
    return render(request, 'crm/billing/invoice_detail.html', _detail_ctx(request, inv, open_modal=request.GET.get('modal', '')))


ACTION_FORMS = {'send': SendInvoiceForm, 'promise': PromiseForm, 'dispute': DisputeForm}


@staff_required
@require_POST
def invoice_action(request, pk, action):
    inv = get_invoice(request, pk)
    user = request.user
    perm = {'cancel': 'cancel', 'write_off': 'edit'}.get(action, 'edit')
    permissions.require(user, 'invoices', perm, request)
    permissions.require_object(user, 'invoices', 'edit', inv, request)
    form = None
    if action in ACTION_FORMS:
        form = ACTION_FORMS[action](request.POST, prefix=action)
    elif action == 'credit':
        form = CreditNoteForm(request.POST, prefix='credit', invoice=inv)
    if form is not None and not form.is_valid():
        return render(request, 'crm/billing/invoice_detail.html', _detail_ctx(request, inv, {action: form}, action), status=400)
    c = form.cleaned_data if form else {}
    try:
        if action == 'issue':
            reasons = svc.issue_approval_reasons(inv, user)
            if reasons:
                raise ValidationError('Needs approval first: ' + '; '.join(reasons) + '.')
            selections = []
            for p, held in svc.open_advances(inv.client):
                raw = request.POST.get(f'adv_{p.pk}')
                if raw:
                    amt = D(raw)
                    if amt < 0 or amt > held:
                        raise ValidationError(f'Advance from {p.receipt_number} must be between 0 and {held}.')
                    selections.append((p, amt))
            with transaction.atomic():
                inv = svc.issue(inv, user, request)
                adjusted = svc.apply_advances(inv, user, selections) if selections else ZERO
            inv.refresh_from_db()
            msg = f'Invoice {inv.number} issued. Tax frozen, PDF stored, ledger posting queued.'
            if adjusted:
                msg += f' {pdf._money(adjusted)} adjusted from advances.'
            messages.success(request, msg)
        elif action == 'approval':
            svc.submit_issue_approval(inv, user, request.POST.get('message', ''))
            messages.success(request, 'Sent for approval. The invoice is frozen until a decision.')
        elif action == 'cancel_approval':
            pend = approvals.pending_for(inv)
            if pend and pend.requested_by_id == user.pk:
                approvals.cancel(pend, user, 'Cancelled by requester')
            messages.success(request, 'Approval request cancelled. The invoice is a draft again.')
        elif action == 'send':
            svc.send(inv, user, to=c['to'], cc=c.get('cc'), message=c.get('message', ''), include_link=c.get('include_link', False),
                     request=request)
            messages.success(request, f'Invoice {inv.number} sent to {c["to"]}.')
        elif action == 'remind':
            if svc.send_reminder(inv, 'manual', user):
                messages.success(request, 'Reminder sent.')
            else:
                messages.warning(request, f'Reminder not sent: {svc.reminder_blocked(inv)}.')
        elif action == 'credit':
            cn = svc.create_credit_note(inv, user, taxable=c['taxable'], reason=c['reason'], notes=c.get('notes', ''),
                                        treatment=c['treatment'])
            if cn.status == 'issued':
                messages.success(request, f'Credit note {cn.number} issued for {pdf._money(cn.total)}.')
            else:
                messages.warning(request, f'Credit note for {pdf._money(cn.total)} needs Accountant approval.')
        elif action == 'cancel':
            svc.cancel(inv, user, request.POST.get('reason', ''))
            messages.success(request, f'Invoice {inv.number} cancelled. The number is kept as Cancelled; a reversal is posted.')
        elif action == 'write_off':
            svc.request_write_off(inv, user, request.POST.get('reason', ''))
            messages.success(request, 'Write-off requested. The Owner must approve it.')
        elif action == 'promise':
            svc.record_promise(inv, user, date=c['date'], amount=c['amount'], note=c.get('note', ''))
            messages.success(request, f'Promise to pay recorded. Reminders pause until {c["date"]:%d-%b-%Y}.')
        elif action == 'dispute':
            svc.set_dispute(inv, user, True, c['reason'])
            messages.success(request, 'Marked disputed. Reminders are paused.')
        elif action == 'resolve_dispute':
            svc.set_dispute(inv, user, False, request.POST.get('reason', ''))
            messages.success(request, 'Dispute resolved. Reminders resume.')
        elif action == 'apply_advance':
            selections = []
            for p, held in svc.open_advances(inv.client):
                raw = request.POST.get(f'adv_{p.pk}')
                if raw:
                    amt = D(raw)
                    if amt < 0 or amt > held:
                        raise ValidationError(f'Advance from {p.receipt_number} must be between 0 and {held}.')
                    selections.append((p, amt))
            if not selections:
                raise ValidationError('Enter an amount against at least one advance.')
            with transaction.atomic():
                adjusted = svc.apply_advances(inv, user, selections)
            messages.success(request, f'{pdf._money(adjusted)} adjusted from customer advances.')
        else:
            raise Http404
    except (ValidationError, ArithmeticError) as exc:
        messages.error(request, ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Enter numbers only.')
    return redirect('crm:invoice_detail', pk=inv.pk)


@staff_required
def invoice_pdf_view(request, pk):
    inv = get_invoice(request, pk)
    if inv.pdf_file:
        inv.pdf_file.open('rb')
        data = inv.pdf_file.read()
        inv.pdf_file.close()
    else:
        data = pdf.invoice_pdf(inv)
    resp = HttpResponse(data, content_type='application/pdf')
    disp = 'attachment' if request.GET.get('download') else 'inline'
    resp['Content-Disposition'] = f'{disp}; filename="{(inv.number or "draft-" + str(inv.pk)).replace("/", "-")}.pdf"'
    return resp


@staff_required
@require_POST
def invoice_bulk_remind(request):
    permissions.require(request.user, 'invoices', 'edit', request)
    ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()][:200]
    qs = permissions.scoped(Invoice.objects.filter(pk__in=ids).select_related('client'), request.user, 'invoices', 'edit')
    sent, skipped = 0, []
    for inv in qs:
        if svc.send_reminder(inv, 'manual', request.user):
            sent += 1
        else:
            skipped.append(f'{inv.display_number} ({svc.reminder_blocked(inv)})')
    if sent:
        messages.success(request, f'Reminders sent for {sent} invoice(s).')
    if skipped:
        messages.warning(request, 'Skipped: ' + '; '.join(skipped[:10]) + ('…' if len(skipped) > 10 else ''))
    return back(request, reverse('crm:invoice_list'))


# ---------------------------------------------------------------- credit notes

class CreditNoteList(ListPage):
    model = CreditNote
    module = 'invoices'
    template_name = 'crm/billing/credit_note_list.html'
    title = 'Credit notes'
    owner_fields = ('invoice__owner',)
    search_fields = ('number__icontains', 'invoice__number__icontains', 'invoice__client__display_name__icontains')
    select_related = ('invoice', 'invoice__client')
    filters = [Filter('status', 'Status', CreditNote.STATUSES)]
    export_name = 'credit-notes'
    export_columns = [('Number', 'number'), ('Date', 'note_date'), ('Invoice', 'invoice'), ('Client', 'invoice.client'),
                      ('Reason', lambda c: c.get_reason_display()), ('Taxable', 'taxable_value'), ('CGST', 'cgst'), ('SGST', 'sgst'),
                      ('IGST', 'igst'), ('Total', 'total'), ('Status', lambda c: c.get_status_display())]

    def extra_context(self, qs):
        return {'module_code': 'credit_notes'}


@staff_required
def credit_note_pdf_view(request, pk):
    cn = get_object_or_404(CreditNote.objects.select_related('invoice'), pk=pk)
    get_invoice(request, cn.invoice_id)
    resp = HttpResponse(pdf.credit_note_pdf(cn), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{(cn.number or "draft").replace("/", "-")}.pdf"'
    return resp


# ---------------------------------------------------------------- receivables (7.7)

@staff_required
def receivables(request):
    permissions.require(request.user, 'invoices', 'view', request)
    qs = permissions.scoped(Invoice.objects.all(), request.user, 'invoices', 'view')
    owner = request.GET.get('owner')
    if owner and owner.isdigit():
        qs = qs.filter(owner_id=owner)
    rows, totals, buckets = svc.ageing(qs)
    from django.contrib.auth import get_user_model
    owners = get_user_model().objects.filter(pk__in=Invoice.objects.filter(status__in=INV_OPEN).values('owner')).order_by('first_name')
    promises = qs.filter(status__in=INV_OPEN, promise_date__isnull=False, promise_date__gte=india.today()).select_related('client')
    broken = qs.filter(status__in=INV_OPEN, balance__gt=0, promise_date__lt=india.today()).select_related('client')
    disputed = qs.filter(status__in=INV_OPEN, disputed=True).select_related('client')
    return render(request, 'crm/billing/receivables.html', {
        'rows': rows, 'totals': totals, 'buckets': buckets, 'owners': owners, 'owner': owner, 'promises': promises[:50],
        'broken': broken[:50], 'disputed': disputed[:50],
        'chart': {'labels': buckets, 'values': [float(totals[b]) for b in buckets]},
    })


@staff_required
def advances(request):
    permissions.require(request.user, 'payments', 'view', request)
    from crm.modules.payments.services import customer_advances
    rows = customer_advances()
    allowed = set(permissions.scoped(Client.objects.all(), request.user, 'clients', 'view').values_list('pk', flat=True))
    rows = [r for r in rows if r['payment'].client_id in allowed]
    return render(request, 'crm/billing/advances.html', {'rows': rows, 'total': sum((r['held'] for r in rows), ZERO)})


# ---------------------------------------------------------------- TDS receivable (7.6)

class TdsList(ListPage):
    model = TdsReceivable
    module = 'payments'
    template_name = 'crm/billing/tds_list.html'
    title = 'TDS receivable'
    owner_fields = ('client__owner',)
    search_fields = ('client__display_name__icontains', 'certificate_number__icontains', 'deductor_tan__icontains')
    select_related = ('client', 'payment')
    filters = [Filter('status', 'Status', TdsReceivable.STATUSES),
               Filter('quarter', 'Quarter', [('Q1', 'Q1'), ('Q2', 'Q2'), ('Q3', 'Q3'), ('Q4', 'Q4')]),
               Filter('fiscal_year', 'FY', lambda r: [(f, f) for f in TdsReceivable.objects.values_list('fiscal_year', flat=True).distinct()])]
    quick_filters = {'old': ('Older than 90 days, no certificate',
                             lambda qs, r: qs.filter(status='expected', created_at__lte=timezone.now() - timedelta(days=90)))}
    export_name = 'tds-receivable'
    export_columns = [('Client', 'client'), ('TAN', 'deductor_tan'), ('Section', 'section'), ('FY', 'fiscal_year'), ('Quarter', 'quarter'),
                      ('Amount', 'amount'), ('Receipt', 'payment.receipt_number'), ('Status', lambda t: t.get_status_display()),
                      ('Form 16A', 'certificate_number'), ('Certificate date', 'certificate_date')]

    def extra_context(self, qs):
        return {'module_code': 'tds', 'form': TdsUpdateForm(prefix='tds'),
                'can_edit': permissions.has_perm(self.request.user, 'payments', 'approve'),
                'sum_expected': qs.filter(status='expected').aggregate(s=Sum('amount'))['s'] or ZERO}


@staff_required
@require_POST
def tds_action(request, pk, action):
    permissions.require(request.user, 'payments', 'approve', request)
    t = get_object_or_404(TdsReceivable, pk=pk)
    try:
        if action == 'update':
            form = TdsUpdateForm(request.POST, prefix='tds')
            if not form.is_valid():
                raise ValidationError(' '.join(e for errs in form.errors.values() for e in errs))
            svc.update_tds(t, request.user, **form.cleaned_data)
            messages.success(request, 'TDS entry updated.')
        elif action == 'write_off':
            svc.request_tds_write_off(t, request.user, request.POST.get('reason', ''))
            messages.success(request, 'Write-off requested; an Accountant must approve it.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return back(request, reverse('crm:tds_receivable'))


# ---------------------------------------------------------------- statement of account (7.7)

@staff_required
def client_statement(request, pk):
    client = get_scoped(request, Client, pk, 'clients', 'view')
    permissions.require(request.user, 'invoices', 'view', request)
    start, end = india.fy_bounds(india.today())
    form = StatementForm(request.GET or None, initial={'start': start, 'end': min(end, india.today())})
    if request.GET and form.is_valid():
        start, end = form.cleaned_data['start'], form.cleaned_data['end']
    else:
        end = min(end, india.today())
    rows, closing = svc.statement_rows(client, start, end)
    if request.GET.get('format') == 'pdf':
        resp = HttpResponse(pdf.statement_pdf(client, rows, closing, start, end), content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="statement-{client.pk}-{start:%Y%m%d}-{end:%Y%m%d}.pdf"'
        audit.log('client.statement_downloaded', client, changes={'from': start, 'to': end})
        return resp
    if request.method == 'POST':
        to = request.POST.get('to', '').strip()
        from django.core.validators import validate_email
        try:
            validate_email(to)
        except ValidationError:
            messages.error(request, 'Enter a valid email address.')
        else:
            data = pdf.statement_pdf(client, rows, closing, start, end)
            notify.send_email(to, f'Statement of account {start:%d-%b-%Y} to {end:%d-%b-%Y}',
                              f'Dear {client.legal_name},\n\nPlease find your statement attached. Closing balance: {pdf._money(closing)}.\n\n'
                              f'{CompanySettings.get().legal_name}', sent_by=request.user, obj=client, template_code='statement',
                              attachments=[('statement.pdf', data, 'application/pdf')])
            audit.log('client.statement_sent', client, changes={'to': to, 'from': start, 'to_date': end})
            messages.success(request, f'Statement emailed to {to}.')
        return redirect(f'{reverse("crm:client_statement", args=[client.pk])}?start={start}&end={end}')
    contact = client.billing_contact
    return render(request, 'crm/billing/statement.html', {'client': client, 'rows': rows, 'closing': closing, 'start': start,
                                                          'end': end, 'form': form,
                                                          'to': (contact.email if contact and contact.email else client.email)})
