"""Staff pages for payment links, payments, allocation, refunds, gateway events and settlements (Part 6, 7.9)."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, india, pdf, permissions
from crm.core.money import D, ZERO
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import (AuditLog, Client, CompanySettings, DeliveryLog, Dispute, GatewayEvent, Invoice, Payment,
                        PaymentLink, Quotation, Refund, Settlement)
from crm.models.payments import GATEWAY_METHODS, LINK_STATUSES, METHODS, PAYMENT_STATUSES, PL_PAYABLE

from . import gateway
from . import services as svc
from .forms import (DisputeForm, ExtensionForm, LinkForm, ManualPaymentForm, ManualRefundForm, RefundForm, SendLinkForm,
                    SettlementForm, default_expiry)


def _client_choices(request):
    return [(c.pk, c.display_name) for c in permissions.scoped(Client.objects.filter(archived_at__isnull=True), request.user,
                                                               'clients', 'view').order_by('display_name')[:300]]


# ---------------------------------------------------------------- payment links

class LinkList(ListPage):
    model = PaymentLink
    module = 'payment_links'
    template_name = 'crm/payments/link_list.html'
    title = 'Payment links'
    search_fields = ('code__icontains', 'client__display_name__icontains', 'description__icontains',
                     'quotation__number__icontains', 'invoice__number__icontains')
    select_related = ('client', 'owner', 'quotation', 'invoice')
    sort_fields = {'amount': 'amount', 'expires': 'expires_at', 'created': 'created_at'}
    filters = [Filter('status', 'Status', LINK_STATUSES), Filter('client', 'Client', _client_choices, lookup='client_id')]
    quick_filters = {
        'mine': ('Mine', lambda qs, r: qs.filter(owner=r.user)),
        'expiring': ('Expiring in 3 days', lambda qs, r: qs.filter(status__in=PL_PAYABLE, expires_at__lte=timezone.now() + timedelta(days=3))),
        'opened': ('Opened, not paid', lambda qs, r: qs.filter(status__in=PL_PAYABLE, open_count__gt=0)),
        'failed': ('Failed attempt', lambda qs, r: qs.filter(status__in=PL_PAYABLE, attempts__status='failed').distinct()),
        'pending': ('Pending approval', lambda qs, r: qs.filter(status='pending_approval')),
    }
    export_name = 'payment-links'
    export_columns = [('Link ID', 'code'), ('Client', 'client'), ('Source', 'source_label'), ('Purpose', lambda l: l.get_purpose_display()),
                      ('Amount', 'amount'), ('Collected', 'amount_paid'), ('Balance', 'balance'),
                      ('Status', lambda l: l.get_status_display()), ('Expires', 'expires_at'), ('Created by', 'owner'),
                      ('Last event', 'last_event'), ('Opened', 'open_count')]

    def extra_context(self, qs):
        return {'module_code': 'payment_links', 'search_placeholder': 'Search link, client, source',
                'soon': timezone.now() + timedelta(days=2), 'test_mode': gateway.is_test_mode()}


def get_link(request, pk, action='view'):
    return get_scoped(request, PaymentLink, pk, 'payment_links', action,
                      queryset=PaymentLink.objects.select_related('client', 'owner', 'quotation', 'invoice'))


def _source_from_request(request):
    qid = request.GET.get('quotation') or request.POST.get('src_quotation')
    iid = request.GET.get('invoice') or request.POST.get('src_invoice')
    if qid:
        q = get_scoped(request, Quotation, qid, 'quotations', 'view',
                       queryset=Quotation.objects.select_related('client', 'lead'))
        return q
    if iid:
        return get_scoped(request, Invoice, iid, 'invoices', 'view', queryset=Invoice.objects.select_related('client'))
    return None


@staff_required
def link_create(request):
    permissions.require(request.user, 'payment_links', 'create', request)
    source = _source_from_request(request)
    if source is None:
        messages.info(request, 'Generate a payment link from a quotation or an issued invoice.')
        return redirect('crm:link_list')
    back_url = reverse('crm:quotation_detail' if isinstance(source, Quotation) else 'crm:invoice_detail', args=[source.pk])
    errors = svc.eligibility_errors(source)
    company = CompanySettings.get()
    cap = svc.collectable(source)
    milestones = list(source.milestones.all()) if isinstance(source, Quotation) else []
    client = source.client
    contact = (client.billing_contact or client.primary_contact) if client else None
    lead = source.lead if isinstance(source, Quotation) and source.lead_id else None
    purpose = 'advance' if isinstance(source, Quotation) and source.advance_percent and source.status != 'accepted' else 'full'
    initial = {
        'purpose': purpose, 'amount': svc.suggested_amount(source, purpose),
        'description': (f'Advance for {source.title or "quotation " + source.display_number}' if purpose == 'advance'
                        else f'Payment for {source.display_number}')[:200],
        'expires_at': default_expiry(company.payment_link_default_expiry_days),
        'contact_name': (contact.name if contact else '') or (getattr(source, 'contact_name', '') or (lead.name if lead else '')),
        'contact_email': (contact.email if contact and contact.email else '') or getattr(source, 'contact_email', '') or (client.email if client else '') or (lead.email if lead else ''),
        'contact_phone': (contact.phone if contact and contact.phone else '') or getattr(source, 'contact_phone', '') or (client.phone if client else '') or (lead.phone if lead else ''),
        'min_partial_amount': company.min_partial_payment,
    }
    if isinstance(source, Quotation):
        max_exp = timezone.make_aware(datetime.combine(source.valid_until, time(23, 59)))
        if initial['expires_at'] > max_exp:
            initial['expires_at'] = max_exp
    form = LinkForm(request.POST or None, initial=initial, enabled_methods=company.gateway_methods or GATEWAY_METHODS,
                    milestones=milestones, collectable=cap, can_multi=permissions.has_perm(request.user, 'payment_links', 'approve'))
    if request.method == 'POST' and not errors and form.is_valid():
        c = form.cleaned_data
        ms = next((m for m in milestones if str(m.pk) == c.get('milestone')), None)
        try:
            link = svc.create_link(
                source, request.user, purpose=c['purpose'], amount=c['amount'], description=c['description'],
                expires_at=c['expires_at'], allow_partial=c['allow_partial'], min_partial_amount=c.get('min_partial_amount'),
                methods=c['methods'], contact_name=c['contact_name'], contact_email=c['contact_email'],
                contact_phone=c['contact_phone'], internal_note=c['internal_note'], milestone_name=ms.name if ms else '',
                allow_multiple=c.get('allow_multiple', False), request_message=c.get('request_message', ''))
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
        else:
            if link.status == 'pending_approval':
                messages.warning(request, f'Link {link.code} needs approval before the customer can pay. The approver was notified.')
            else:
                if c['send_now'] and c['contact_email']:
                    try:
                        svc.send_link(link, request.user, to=c['contact_email'], request=request)
                        messages.success(request, f'Link {link.code} generated and emailed to {c["contact_email"]}.')
                    except ValidationError as exc:
                        messages.warning(request, f'Link generated, but not sent: {" ".join(exc.messages)}')
                else:
                    messages.success(request, f'Link {link.code} generated. Copy it below.')
            return redirect(reverse('crm:link_detail', args=[link.pk]) + '?created=1')
    elif request.method == 'POST' and not errors:
        messages.error(request, 'Please fix the highlighted problems. No link was created.')
    return render(request, 'crm/payments/link_form.html', {
        'form': form, 'source': source, 'is_quotation': isinstance(source, Quotation), 'cap': cap, 'errors': errors,
        'back_url': back_url, 'company': company, 'milestones': milestones, 'test_mode': gateway.is_test_mode(),
        'tax_preview': svc.tax_breakup(source, D(form['amount'].value() or 0)) if not errors else None,
        'needs_approval_above': company.exec_payment_link_limit,
        'can_approve': permissions.has_perm(request.user, 'payment_links', 'approve'),
    })


@staff_required
@require_POST
def link_tax_preview(request):
    """Live tax breakup for the amount typed in the generate-link form (server maths, no floats in the browser)."""
    from django.http import JsonResponse
    from crm.core.money import format_inr
    permissions.require(request.user, 'payment_links', 'create', request)
    source = _source_from_request(request)
    if source is None:
        return JsonResponse({'ok': False, 'error': 'No source.'})
    try:
        amount = D(request.POST.get('amount') or 0)
    except (ValueError, ArithmeticError):
        return JsonResponse({'ok': False, 'error': 'Enter numbers only.'})
    cap = svc.collectable(source)
    t = svc.tax_breakup(source, amount)
    return JsonResponse({'ok': True, 'taxable': format_inr(D(t['taxable'])), 'cgst': format_inr(D(t['cgst'])),
                         'sgst': format_inr(D(t['sgst'])), 'igst': format_inr(D(t['igst'])), 'total': format_inr(D(t['total'])),
                         'over': amount > cap, 'cap': format_inr(cap), 'intra': t.get('supply_type') == 'intra'})


def _link_ctx(request, link, forms=None, open_modal=''):
    user = request.user
    is_finance = permissions.has_perm(user, 'payments', 'approve')
    forms = forms or {}
    defaults = {'send': SendLinkForm(prefix='send', initial={'to': link.contact_email}),
                'extend': ExtensionForm(prefix='extend', initial={'new_expiry': max(link.expires_at, timezone.now()) + timedelta(days=7)})}
    defaults.update(forms)
    payments = link.payments.select_related('verified_by').prefetch_related('allocations__invoice', 'refunds')
    can_edit = permissions.in_scope(user, 'payment_links', 'edit', link)
    return {
        'link': link, 'url': svc.public_url(link, request) if link.status in PL_PAYABLE else '',
        'attempts': link.attempts.all(), 'payments': payments, 'events': link.gateway_events.all()[:50] if is_finance else [],
        'deliveries': DeliveryLog.objects.filter(object_type='paymentlink', object_id=str(link.pk))[:30],
        'refunds': Refund.objects.filter(payment__link=link).select_related('payment'),
        'activity': AuditLog.objects.filter(object_type='paymentlink', object_id=str(link.pk))[:50],
        'tax': link.tax_snapshot, 'forms': defaults, 'open_modal': open_modal, 'is_finance': is_finance, 'can_edit': can_edit,
        'can_cancel': permissions.has_perm(user, 'payment_links', 'cancel') and can_edit,
        'can_record': permissions.has_perm(user, 'payments', 'create'),
        'can_refund': permissions.has_perm(user, 'refunds', 'create'),
        'pending': approvals.pending_for(link), 'history': approvals.history_for(link),
        'created': request.GET.get('created') == '1', 'test_mode': gateway.is_test_mode(),
    }


@staff_required
def link_detail(request, pk):
    link = get_link(request, pk)
    return render(request, 'crm/payments/link_detail.html', _link_ctx(request, link, open_modal=request.GET.get('modal', '')))


@staff_required
@require_POST
def link_action(request, pk, action):
    link = get_link(request, pk)
    user = request.user
    try:
        if action == 'send':
            permissions.require_object(user, 'payment_links', 'edit', link, request)
            form = SendLinkForm(request.POST, prefix='send')
            if not form.is_valid():
                return render(request, 'crm/payments/link_detail.html', _link_ctx(request, link, {'send': form}, 'send'), status=400)
            svc.send_link(link, user, to=form.cleaned_data['to'], message=form.cleaned_data['message'], request=request)
            messages.success(request, f'Link sent to {form.cleaned_data["to"]}.')
        elif action == 'cancel':
            permissions.require(user, 'payment_links', 'cancel', request)
            permissions.require_object(user, 'payment_links', 'edit', link, request)
            svc.cancel_link(link, user, request.POST.get('reason', ''))
            messages.success(request, f'Link {link.code} cancelled. The customer page now says it is no longer active.')
        elif action == 'extend':
            permissions.require_object(user, 'payment_links', 'edit', link, request)
            form = ExtensionForm(request.POST, prefix='extend')
            if not form.is_valid():
                return render(request, 'crm/payments/link_detail.html', _link_ctx(request, link, {'extend': form}, 'extend'), status=400)
            req = svc.request_extension(link, user, new_expiry=form.cleaned_data['new_expiry'], reason=form.cleaned_data['reason'])
            messages.success(request, 'Extension requested; the approver was notified.' if req else 'Expiry extended.')
        elif action == 'sync':
            permissions.require(user, 'payments', 'approve', request)
            n = sum(len(svc.sync_attempt(a, source='backstop')) for a in link.attempts.exclude(status__in=['captured']))
            messages.success(request, f'Checked with the gateway: {n} new event(s).')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:link_detail', pk=link.pk)


# ---------------------------------------------------------------- payments

class PaymentList(ListPage):
    model = Payment
    module = 'payments'
    template_name = 'crm/payments/payment_list.html'
    title = 'Payments'
    search_fields = ('receipt_number__icontains', 'client__display_name__icontains', 'reference_number__icontains',
                     'provider_payment_id__icontains')
    select_related = ('client', 'link', 'invoice', 'submitted_by', 'verified_by')
    default_sort = '-payment_date'
    sort_fields = {'date': 'payment_date', 'amount': 'amount'}
    filters = [Filter('status', 'Status', PAYMENT_STATUSES), Filter('method', 'Method', METHODS),
               Filter('kind', 'Type', [('gateway', 'Gateway'), ('manual', 'Manual')]),
               Filter('client', 'Client', _client_choices, lookup='client_id')]
    quick_filters = {
        'pending': ('Pending verification', lambda qs, r: qs.filter(status='pending_verification')),
        'review': ('Review queue', lambda qs, r: qs.filter(status='review')),
        'unallocated': ('Unallocated', lambda qs, r: qs.filter(status__in=['verified', 'partially_refunded'],
                                                               allocated_amount__lt=F('amount') + F('tds_amount') - F('refunded_amount'))),
        'tds': ('With TDS', lambda qs, r: qs.filter(tds_amount__gt=0)),
        'month': ('This month', lambda qs, r: qs.filter(payment_date__gte=india.month_start(india.today()))),
    }
    export_name = 'payments'
    export_columns = [('Receipt', 'receipt_number'), ('Date', 'payment_date'), ('Client', 'client'), ('Type', 'kind'),
                      ('Method', lambda p: p.get_method_display()), ('Amount', 'amount'), ('TDS', 'tds_amount'),
                      ('Allocated', 'allocated_amount'), ('Refunded', 'refunded_amount'), ('Status', lambda p: p.get_status_display()),
                      ('Reference', lambda p: p.reference_number or p.provider_payment_id), ('Link', 'link')]

    def extra_context(self, qs):
        return {'module_code': 'payments', 'search_placeholder': 'Search receipt, client, reference',
                'can_record': permissions.has_perm(self.request.user, 'payments', 'create'),
                'sum_amount': qs.filter(status__in=['verified', 'partially_refunded', 'refunded']).aggregate(s=Sum('amount'))['s'] or ZERO}


class VerifyQueue(PaymentList):
    template_name = 'crm/payments/verify_queue.html'
    action = 'approve'
    module = 'payments'
    title = 'Verify manual payments'
    quick_filters = {}
    filters = [Filter('client', 'Client', _client_choices, lookup='client_id')]

    def base_queryset(self):
        return super().base_queryset().filter(status__in=['pending_verification', 'review'])


def get_payment(request, pk, action='view'):
    return get_scoped(request, Payment, pk, 'payments', action,
                      queryset=Payment.objects.select_related('client', 'link', 'invoice', 'attempt', 'deposit_account',
                                                              'submitted_by', 'verified_by'))


def _payment_ctx(request, p, forms=None, open_modal=''):
    user = request.user
    forms = forms or {}
    gateway_ok = p.kind == 'gateway' and bool(p.attempt_id)
    refundable = svc.refundable_now(p)
    defaults = {'refund': RefundForm(prefix='refund', gateway_ok=gateway_ok, initial={'amount': refundable}),
                'dispute': DisputeForm(prefix='dispute')}
    defaults.update(forms)
    from crm.models import Attachment
    from django.contrib.contenttypes.models import ContentType
    is_finance = permissions.has_perm(user, 'payments', 'approve')
    return {
        'p': p, 'allocations': p.allocations.select_related('invoice', 'created_by', 'reversed_by'),
        'refunds': p.refunds.all(), 'disputes': p.disputes.all(), 'forms': defaults, 'open_modal': open_modal,
        'voucher': getattr(p, 'receipt_voucher', None), 'tds': getattr(p, 'tds_receivable', None),
        'is_finance': is_finance, 'can_verify': is_finance and p.status == 'pending_verification' and p.submitted_by_id != user.pk,
        'self_submitted': p.submitted_by_id == user.pk, 'refundable': refundable, 'held': svc.held_advance(p),
        'can_refund': permissions.has_perm(user, 'refunds', 'create') and refundable > 0,
        'can_allocate': permissions.has_perm(user, 'payments', 'create') and p.status in ('verified', 'partially_refunded') and svc.held_advance(p) > 0,
        'proofs': Attachment.objects.filter(content_type=ContentType.objects.get_for_model(Payment), object_id=str(p.pk), archived_at__isnull=True),
        'events': p.gateway_events.all() if is_finance else [],
        'activity': AuditLog.objects.filter(object_type='payment', object_id=str(p.pk))[:50],
        'can_reverse': permissions.has_perm(user, 'accounting', 'approve'),
    }


@staff_required
def payment_detail(request, pk):
    p = get_payment(request, pk)
    return render(request, 'crm/payments/payment_detail.html', _payment_ctx(request, p, open_modal=request.GET.get('modal', '')))


@staff_required
def payment_record(request):
    permissions.require(request.user, 'payments', 'create', request)
    clients = permissions.scoped(Client.objects.filter(archived_at__isnull=True), request.user, 'clients', 'view').order_by('display_name')
    initial = {}
    client = None
    link = None
    if request.GET.get('invoice'):
        inv = get_scoped(request, Invoice, request.GET['invoice'], 'invoices', 'view')
        client = inv.client
        initial.update(client=inv.client_id, invoice=inv.pk, amount=inv.balance)
    elif request.GET.get('client'):
        client = get_object_or_404(clients, pk=request.GET['client'])
        initial['client'] = client.pk
    lid = request.GET.get('link') or request.POST.get('link')
    if lid:
        link = get_link(request, lid)
        client = link.client
        initial.update(client=link.client_id, amount=link.balance, invoice=link.invoice_id)
    form = ManualPaymentForm(request.POST or None, request.FILES or None, initial=initial, clients=clients, client=client)
    if request.method == 'POST' and form.is_valid():
        c = form.cleaned_data
        try:
            p = svc.record_manual(request.user, client=c['client'], invoice=c.get('invoice'), link=link, amount=c['amount'],
                                  payment_date=c['payment_date'], method=c['method'], reference_number=c.get('reference_number', ''),
                                  deposit_account=c['deposit_account'], tds_amount=c.get('tds_amount') or ZERO,
                                  tds_section=c.get('tds_section', ''), deductor_tan=c.get('deductor_tan', ''), note=c.get('note', ''),
                                  proof=c.get('proof'))
        except ValidationError as exc:
            form.add_error(None, ' '.join(exc.messages))
        else:
            messages.success(request, f'Payment of {pdf._money(p.amount)} recorded as Pending verification. '
                                      'A different Finance user must verify it before it reduces the balance.')
            return redirect('crm:payment_detail', pk=p.pk)
    elif request.method == 'POST':
        messages.error(request, 'Please fix the highlighted problems. Nothing was saved.')
    return render(request, 'crm/payments/payment_record.html', {'form': form, 'link': link,
                                                                 'proof_above': CompanySettings.get().manual_payment_proof_above})


@staff_required
@require_POST
def payment_action(request, pk, action):
    p = get_payment(request, pk)
    user = request.user
    try:
        if action == 'verify':
            permissions.require(user, 'payments', 'approve', request)
            svc.verify_manual(p, user)
            p.refresh_from_db()
            messages.success(request, f'Verified. Receipt {p.receipt_number} issued and posted to the ledger.')
        elif action == 'reject':
            permissions.require(user, 'payments', 'approve', request)
            svc.reject_manual(p, user, request.POST.get('reason', ''))
            messages.success(request, 'Payment rejected; the submitter was told why.')
        elif action == 'accept_review':
            svc.accept_review(p, user, request.POST.get('reason', ''))
            messages.success(request, 'Payment accepted and counted.')
        elif action == 'refund':
            permissions.require(user, 'refunds', 'create', request)
            form = RefundForm(request.POST, prefix='refund', gateway_ok=p.kind == 'gateway' and bool(p.attempt_id))
            if not form.is_valid():
                return render(request, 'crm/payments/payment_detail.html', _payment_ctx(request, p, {'refund': form}, 'refund'), status=400)
            c = form.cleaned_data
            svc.request_refund(p, user, amount=c['amount'], reason=c['reason'], notes=c.get('notes', ''), method=c['method'])
            messages.success(request, 'Refund requested. Finance approval is needed before money goes back.')
        elif action == 'dispute':
            permissions.require(user, 'payments', 'approve', request)
            d = get_object_or_404(Dispute, pk=request.POST.get('dispute'), payment=p)
            form = DisputeForm(request.POST, prefix='dispute')
            if not form.is_valid():
                return render(request, 'crm/payments/payment_detail.html', _payment_ctx(request, p, {'dispute': form}, 'dispute'), status=400)
            svc.update_dispute(d, user, status=form.cleaned_data['status'], note=form.cleaned_data['note'])
            messages.success(request, 'Dispute updated.')
        elif action == 'reverse_allocation':
            from crm.models import PaymentAllocation
            from crm.modules.billing.services import reverse_allocation
            a = get_object_or_404(PaymentAllocation, pk=request.POST.get('allocation'), payment=p)
            reverse_allocation(a, user, request.POST.get('reason', ''))
            messages.success(request, 'Allocation reversed. The amount is held as an advance again.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:payment_detail', pk=p.pk)


@staff_required
def receipt_pdf_view(request, pk):
    p = get_payment(request, pk)
    if p.status not in ('verified', 'partially_refunded', 'refunded'):
        messages.info(request, 'A receipt is available once the payment is verified.')
        return redirect('crm:payment_detail', pk=p.pk)
    resp = HttpResponse(pdf.receipt_pdf(p), content_type='application/pdf')
    resp['Content-Disposition'] = f'{"attachment" if request.GET.get("download") else "inline"}; filename="{p.receipt_number.replace("/", "-")}.pdf"'
    return resp


# ---------------------------------------------------------------- allocation (7.9)

@staff_required
def allocation(request):
    permissions.require(request.user, 'payments', 'create', request)
    qs = permissions.scoped(Payment.objects.filter(status__in=['verified', 'partially_refunded']).select_related('client'),
                            request.user, 'payments', 'view')
    rows = [p for p in qs.order_by('payment_date') if svc.held_advance(p) > 0]
    clients = {p.client_id for p in rows}
    open_by_client = {}
    for inv in Invoice.objects.filter(client_id__in=clients, status__in=['issued', 'partially_paid'], balance__gt=0):
        open_by_client.setdefault(inv.client_id, []).append(inv)
    data = [{'p': p, 'held': svc.held_advance(p), 'open': open_by_client.get(p.client_id, [])} for p in rows]
    return render(request, 'crm/payments/allocation.html', {'rows': data, 'total_held': sum((r['held'] for r in data), ZERO)})


@staff_required
def allocate_payment(request, pk):
    permissions.require(request.user, 'payments', 'create', request)
    p = get_payment(request, pk)
    from crm.modules.billing import services as billing
    invoices = list(Invoice.objects.filter(client=p.client, status__in=['issued', 'partially_paid'], balance__gt=0).order_by('due_date', 'id'))
    held = svc.held_advance(p)
    used_tds = p.allocations.filter(reversed_at__isnull=True).aggregate(s=Sum('tds_amount'))['s'] or ZERO
    tds_left = p.tds_amount - used_tds
    plan = {}
    error = ''
    if request.method == 'POST':
        rows = []
        try:
            for inv in invoices:
                cash = D(request.POST.get(f'cash_{inv.pk}') or 0)
                tds = D(request.POST.get(f'tds_{inv.pk}') or 0)
                if cash < 0 or tds < 0:
                    raise ValidationError('Amounts cannot be negative.')
                plan[inv.pk] = (cash, tds)
                if cash or tds:
                    rows.append((inv, cash, tds))
            if not rows:
                raise ValidationError('Enter an amount against at least one invoice.')
            billing.allocate(p, rows, request.user, after_receipt=True)
        except (ValidationError, ArithmeticError, ValueError) as exc:
            error = ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Enter numbers only.'
        else:
            messages.success(request, 'Allocation saved. Invoice balances and the ledger are updated.')
            return redirect('crm:payment_detail', pk=p.pk)
    elif request.GET.get('auto'):
        for inv, cash, tds in billing.auto_allocation_plan(p):
            plan[inv.pk] = (min(cash, held), ZERO)
    rows = [{'inv': i, 'cash': plan.get(i.pk, (ZERO, ZERO))[0], 'tds': plan.get(i.pk, (ZERO, ZERO))[1]} for i in invoices]
    return render(request, 'crm/payments/allocate.html', {'p': p, 'rows': rows, 'held': held, 'tds_left': tds_left, 'error': error})


# ---------------------------------------------------------------- refunds

class RefundList(ListPage):
    model = Refund
    module = 'refunds'
    template_name = 'crm/payments/refund_list.html'
    title = 'Refunds'
    search_fields = ('payment__receipt_number__icontains', 'payment__client__display_name__icontains', 'gateway_refund_id__icontains')
    select_related = ('payment', 'payment__client', 'approved_by')
    filters = [Filter('status', 'Status', Refund.STATUSES), Filter('reason', 'Reason', Refund.REASONS)]
    export_name = 'refunds'
    export_columns = [('Payment', 'payment'), ('Client', 'payment.client'), ('Amount', 'amount'), ('Tax portion', 'tax_portion'),
                      ('Reason', lambda r: r.get_reason_display()), ('Method', 'method'), ('Status', lambda r: r.get_status_display()),
                      ('Processed', 'processed_at')]

    def extra_context(self, qs):
        return {'module_code': 'refunds', 'is_finance': permissions.has_perm(self.request.user, 'payments', 'approve'),
                'manual_form': ManualRefundForm(prefix='manual')}


@staff_required
@require_POST
def refund_action(request, pk, action):
    r = get_scoped(request, Refund, pk, 'refunds', 'view', queryset=Refund.objects.select_related('payment'))
    try:
        permissions.require(request.user, 'payments', 'approve', request)
        if action == 'execute':
            r = svc.execute_refund(r, request.user)
            messages.success(request, {'processed': 'Refund processed.', 'initiated': 'Refund initiated at the gateway.'}.get(
                r.status, f'Refund failed: {r.error}'))
        elif action == 'manual':
            form = ManualRefundForm(request.POST, prefix='manual')
            if not form.is_valid():
                raise ValidationError('Enter the bank or UPI reference of the refund.')
            svc.mark_manual_refunded(r, request.user, form.cleaned_data['reference'])
            messages.success(request, 'Refund marked as processed.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    from crm.core.views import back
    return back(request, reverse('crm:refund_list'))


# ---------------------------------------------------------------- gateway events and settlements

class GatewayEventList(ListPage):
    model = GatewayEvent
    module = 'payments'
    action = 'approve'
    template_name = 'crm/payments/event_list.html'
    title = 'Gateway events'
    search_fields = ('event_id__icontains', 'event_type__icontains', 'link__code__icontains', 'note__icontains')
    select_related = ('link', 'payment')
    filters = [Filter('status', 'Status', GatewayEvent.STATUSES),
               Filter('provider', 'Provider', [('cashfree', 'Cashfree'), ('sandbox', 'Sandbox')]),
               Filter('source', 'Source', [('webhook', 'Webhook'), ('backstop', 'Backstop poll'), ('return', 'Return URL')])]
    quick_filters = {'failed': ('Failed', lambda qs, r: qs.filter(status='failed')),
                     'review': ('Review', lambda qs, r: qs.filter(status='review')),
                     'dup': ('Duplicates', lambda qs, r: qs.filter(status='duplicate'))}
    export_columns = []

    def base_queryset(self):
        permissions.require(self.request.user, 'payments', 'approve', self.request)
        return GatewayEvent.objects.select_related('link', 'payment')


@staff_required
def event_detail(request, pk):
    permissions.require(request.user, 'payments', 'approve', request)
    ev = get_object_or_404(GatewayEvent.objects.select_related('link', 'payment'), pk=pk)
    import json
    return render(request, 'crm/payments/event_detail.html', {'ev': ev, 'pretty': json.dumps(ev.payload, indent=2, default=str)[:20000]})


@staff_required
@require_POST
def event_action(request, pk, action):
    permissions.require(request.user, 'payments', 'approve', request)
    ev = get_object_or_404(GatewayEvent, pk=pk)
    try:
        if action == 'reprocess':
            ev = svc.reprocess(ev, request.user)
            messages.success(request, f'Reprocessed: {ev.get_status_display()}. {ev.note}')
        elif action == 'ignore':
            svc.ignore_event(ev, request.user, request.POST.get('reason', ''))
            messages.success(request, 'Event marked ignored.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:event_detail', pk=ev.pk)


@staff_required
def settlement_list(request):
    if not (permissions.has_perm(request.user, 'payments', 'view') or permissions.has_perm(request.user, 'banking', 'view')):
        permissions.denied(request.user, 'banking', 'view', request)
    can_add = permissions.has_perm(request.user, 'accounting', 'create') or permissions.has_perm(request.user, 'payments', 'approve')
    form = SettlementForm(request.POST or None, prefix='st', initial={'settled_on': india.today()})
    unsettled = Payment.objects.filter(kind='gateway', settlement__isnull=True, status__in=['verified', 'partially_refunded', 'refunded']) \
        .select_related('client').order_by('payment_date')
    open_modal = ''
    if request.method == 'POST':
        if not can_add:
            permissions.denied(request.user, 'payments', 'approve', request)
        if form.is_valid():
            c = form.cleaned_data
            try:
                ids = [int(x) for x in request.POST.getlist('payments') if x.isdigit()]
                s = svc.record_settlement(request.user, provider=gateway.get_adapter().provider or 'cashfree',
                                          settlement_id=c['settlement_id'], settled_on=c['settled_on'], gross=c['gross'],
                                          fees=c['fees'], tax_on_fees=c['tax_on_fees'], tds=c['tds'], bank_account=c['bank_account'],
                                          payment_ids=ids)
                messages.success(request, f'Settlement {s.settlement_id} recorded: net {pdf._money(s.net)} posted to {s.bank_account}.')
                return redirect('crm:settlement_list')
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        open_modal = 'settlement'
    return render(request, 'crm/payments/settlement_list.html', {
        'settlements': Settlement.objects.select_related('bank_account')[:200], 'form': form, 'can_add': can_add,
        'unsettled': unsettled[:200], 'open_modal': open_modal,
        'unsettled_total': unsettled.aggregate(s=Sum('amount'))['s'] or ZERO})
