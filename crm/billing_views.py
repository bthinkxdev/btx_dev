"""Admin-only billing & accounts views."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .billing_forms import RecordPaymentForm, StatementFilterForm
from .models import Bill, BillPayment, LedgerEntry, Project
from .rbac import can_access_billing
from .services import billing as billing_service


def billing_required(view_fn):
    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not can_access_billing(request.user):
            return HttpResponseForbidden(
                'Billing & Accounts is restricted to administrators.'
            )
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _default_payment_description(project: Project) -> str:
    if project.package:
        return f'{project.package.name} — professional services'
    return f'{project.client.business_name} — professional services'


def _fresh_payment_form(project: Project) -> RecordPaymentForm:
    suggested = project.balance_due if project.balance_due > 0 else project.deal_value
    return RecordPaymentForm(
        initial={
            'amount': suggested,
            'description': _default_payment_description(project),
            'payment_date': timezone.localdate(),
            'send_email': True,
        }
    )


def _billing_redirect(request, url_name: str, *, pk: int | None = None, msg: str | None = None):
    """Redirect with HTMX support when triggered from billing pages."""
    if msg:
        messages.success(request, msg)
    url = reverse(url_name, kwargs={'pk': pk}) if pk is not None else reverse(url_name)
    if request.headers.get('HX-Request'):
        response = HttpResponse(status=200)
        response['HX-Redirect'] = url
        return response
    return redirect(url)


def _render_billing_project(
    request,
    project,
    *,
    form,
    statement_form=None,
    open_pdf_url: str | None = None,
    show_payment_modal: bool = False,
):
    client = project.client
    snapshot = billing_service.get_project_billing_snapshot(
        project, actor=request.user
    )
    payments = (
        project.bill_payments.select_related('bill', 'recorded_by')
        .order_by('-payment_date', '-created_at')
    )
    ledger = project.ledger_entries.order_by('entry_date', 'created_at', 'pk')
    if statement_form is None:
        statement_form = StatementFilterForm()
    response = render(
        request,
        'crm/billing/project.html',
        {
            'project': project,
            'client': client,
            'form': form,
            'payments': payments,
            'ledger': ledger,
            'snapshot': snapshot,
            'statement_form': statement_form,
            'open_pdf_url': open_pdf_url,
            'show_payment_modal': show_payment_modal,
            'page_title': f'Billing — Project #{project.pk}',
        },
    )
    if request.headers.get('HX-Request'):
        triggers = {}
        if open_pdf_url:
            triggers['billingOpenPdf'] = open_pdf_url
        if show_payment_modal:
            triggers['billingPaymentModalOpen'] = True
            if form.errors:
                triggers['billingFormInvalid'] = True
                triggers['crmToast'] = 'Could not save — check the highlighted fields in the form.'
        elif request.method == 'POST':
            triggers['billingPaymentModalClose'] = True
        if triggers:
            response['HX-Trigger'] = json.dumps(triggers)
    return response


@login_required
@billing_required
def billing_dashboard(request):
    summary = billing_service.get_billing_summary()
    return render(
        request,
        'crm/billing/dashboard.html',
        {**summary, 'page_title': 'Billing & Accounts'},
    )


@login_required
@billing_required
@require_http_methods(['GET', 'POST'])
def billing_project(request, pk):
    project = get_object_or_404(
        Project.objects.select_related('client', 'client__lead', 'onboarding', 'package'),
        pk=pk,
    )

    open_pdf_url = request.GET.get('open_pdf')
    if open_pdf_url and request.method == 'GET':
        try:
            bill_pk = int(open_pdf_url)
            open_pdf_url = reverse('crm:bill_pdf', kwargs={'pk': bill_pk})
        except (TypeError, ValueError):
            open_pdf_url = None

    statement_form = StatementFilterForm(request.GET if request.method == 'GET' else None)

    if request.method == 'POST':
        form = RecordPaymentForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            pdf_url = None
            bill = None
            if data.get('invoice_only'):
                bill = billing_service.create_bill(
                    project=project,
                    line_items=[{
                        'description': data['description'],
                        'quantity': Decimal('1'),
                        'unit_price': data['amount'],
                    }],
                    bill_date=data['payment_date'],
                    gst_percent=data.get('gst_percent') or Decimal('0'),
                    description=data.get('notes') or '',
                    issue=True,
                    send_email=data.get('send_email', True),
                    actor=request.user,
                )
                messages.success(
                    request,
                    f'Invoice {bill.bill_number} issued. Record payment when received.',
                )
                if request.POST.get('download_pdf'):
                    pdf_url = reverse('crm:bill_pdf', kwargs={'pk': bill.pk})
            else:
                _payment, bill = billing_service.record_payment_with_receipt(
                    project=project,
                    amount=data['amount'],
                    description=data['description'],
                    proof_file=data['proof_file'],
                    transaction_id=data['transaction_id'],
                    payment_method=data['payment_method'],
                    payment_date=data['payment_date'],
                    notes=data.get('notes') or '',
                    gst_percent=data.get('gst_percent') or Decimal('0'),
                    send_email=data.get('send_email', True),
                    actor=request.user,
                )
                emailed = bill.email_sent_to
                msg = f'Payment recorded · Receipt {bill.bill_number}'
                if emailed:
                    msg += f' · Emailed to {emailed}'
                messages.success(request, msg)
                if request.POST.get('download_pdf'):
                    pdf_url = reverse('crm:bill_pdf', kwargs={'pk': bill.pk})

            project.refresh_from_db()
            if request.headers.get('HX-Request'):
                return _render_billing_project(
                    request,
                    project,
                    form=_fresh_payment_form(project),
                    open_pdf_url=pdf_url,
                )
            if pdf_url and bill:
                return redirect(
                    reverse('crm:billing_project', kwargs={'pk': pk})
                    + f'?open_pdf={bill.pk}'
                )
            return redirect('crm:billing_project', pk=pk)
        show_payment_modal = True
    else:
        form = _fresh_payment_form(project)
        show_payment_modal = request.GET.get('record') in ('1', 'true', 'yes')

    return _render_billing_project(
        request,
        project,
        form=form,
        statement_form=statement_form,
        open_pdf_url=open_pdf_url,
        show_payment_modal=show_payment_modal,
    )


@login_required
@billing_required
def bill_create(request, pk):
    return redirect('crm:billing_project', pk=pk)


@login_required
@billing_required
@require_POST
def bill_quick_create(request, pk):
    return redirect('crm:billing_project', pk=pk)


@login_required
@billing_required
def payment_create(request, pk):
    return redirect('crm:billing_project', pk=pk)


@login_required
@billing_required
def bill_detail(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related('project', 'project__package', 'client', 'created_by').prefetch_related(
            'line_items', 'payments'
        ),
        pk=pk,
    )
    payments = list(bill.payments.order_by('-payment_date', '-pk'))
    payment = payments[0] if payments else None
    return render(
        request,
        'crm/billing/bill_detail.html',
        {
            'bill': bill,
            'project': bill.project,
            'payment': payment,
            'payments': payments,
            'page_title': bill.bill_number,
        },
    )


@login_required
@billing_required
@require_GET
def bill_pdf(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related('project', 'client').prefetch_related('payments'),
        pk=pk,
    )
    payment = bill.payments.order_by('-pk').first()
    pdf_bytes = billing_service.render_bill_pdf(bill, payment=payment)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = (
        f'inline; filename="{bill.bill_number.replace("/", "-")}.pdf"'
    )
    return response


@login_required
@billing_required
@require_GET
def payment_receipt_pdf(request, payment_pk):
    payment = get_object_or_404(
        BillPayment.objects.select_related('bill', 'project', 'project__client'),
        pk=payment_pk,
    )
    if payment.bill_id:
        return redirect('crm:bill_pdf', pk=payment.bill_id)
    return HttpResponse('No receipt for this payment.', status=404)


@login_required
@billing_required
@require_POST
def bill_resend_email(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related('project', 'client').prefetch_related('payments'),
        pk=pk,
    )
    recipient = billing_service.get_billing_email_for_project(bill.project)
    if not recipient:
        messages.warning(request, 'No client or onboarding email on file.')
    elif bill.kind == Bill.Kind.RECEIPT:
        payment = bill.payments.order_by('-pk').first()
        if payment and billing_service.send_payment_receipt_email(bill, payment, recipient):
            messages.success(request, f'Receipt emailed to {recipient}.')
        else:
            messages.error(request, 'Failed to send email.')
    elif billing_service.send_bill_email(bill, recipient):
        messages.success(request, f'Invoice emailed to {recipient}.')
    else:
        messages.error(request, 'Failed to send email. Check mail settings.')

    if request.headers.get('HX-Request'):
        return _billing_redirect(
            request, 'crm:billing_project', pk=bill.project_id
        )
    return redirect('crm:billing_project', pk=bill.project_id)


@login_required
@billing_required
@require_POST
def payment_resend_email(request, payment_pk):
    payment = get_object_or_404(
        BillPayment.objects.select_related('bill', 'project'),
        pk=payment_pk,
    )
    if not payment.bill_id:
        messages.error(request, 'No receipt bill linked to this payment.')
        return redirect('crm:billing_project', pk=payment.project_id)
    return redirect('crm:bill_resend_email', pk=payment.bill_id)


@login_required
@billing_required
@require_POST
def payment_verify(request, payment_pk):
    payment = get_object_or_404(BillPayment.objects.select_related('project'), pk=payment_pk)
    billing_service.verify_payment(payment, actor=request.user)
    messages.success(request, 'Payment verified.')
    return redirect('crm:billing_project', pk=payment.project_id)


@login_required
@billing_required
@require_GET
def ledger_list(request):
    project_id = request.GET.get('project')
    if not project_id:
        messages.info(
            request,
            'Select a project to view its ledger. Balances are tracked per project.',
        )
        return redirect('crm:billing_dashboard')

    project = get_object_or_404(
        Project.objects.select_related('client', 'client__lead', 'onboarding', 'package'),
        pk=project_id,
    )
    entries = (
        LedgerEntry.objects.filter(project=project)
        .select_related('bill', 'payment')
        .order_by('-entry_date', '-created_at', '-pk')[:300]
    )
    snapshot = billing_service.get_project_billing_snapshot(
        project, actor=request.user
    )
    projects = Project.objects.select_related('client').order_by('-updated_at')[:100]
    return render(
        request,
        'crm/billing/ledger.html',
        {
            'entries': entries,
            'projects': projects,
            'project': project,
            'snapshot': snapshot,
            'filter_project': str(project.pk),
            'page_title': f'Ledger — Project #{project.pk}',
        },
    )


@login_required
@billing_required
@require_GET
def statement_download(request, pk):
    project = get_object_or_404(
        Project.objects.select_related('client', 'client__lead', 'onboarding', 'package'),
        pk=pk,
    )
    form = StatementFilterForm(request.GET)
    if not form.is_valid():
        for err in form.non_field_errors():
            messages.error(request, err)
        for field in form:
            for err in field.errors:
                messages.error(request, f'{field.label}: {err}')
        return redirect('crm:billing_project', pk=pk)

    date_from = form.cleaned_data.get('date_from')
    date_to = form.cleaned_data.get('date_to')
    pdf_bytes = billing_service.render_statement_pdf(
        project, date_from=date_from, date_to=date_to
    )
    fname = billing_service.statement_filename(
        project, date_from=date_from, date_to=date_to
    )
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response
