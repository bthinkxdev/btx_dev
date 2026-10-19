"""
Simple billing: pick a lead (optional), add line items, enter received
amount, get a Bill + PDF payment receipt. No ledger, no accounts.
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Bill, BillPayment, Lead, Package
from .rbac import admin_required
from .services.simple_billing import add_payment_to_bill, create_quick_bill

BILLS_PER_PAGE = 20


@login_required
@admin_required
def billing_dashboard(request):
    bills = Bill.objects.select_related('lead').order_by('-bill_date', '-created_at')
    paginator = Paginator(bills, BILLS_PER_PAGE)
    page_raw = request.GET.get('page') or '1'
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)
    return render(
        request,
        'crm/billing/dashboard.html',
        {'bills': page_obj, 'page_obj': page_obj},
    )


@login_required
@admin_required
def bill_create(request):
    if request.method == 'POST':
        lead_id = (request.POST.get('lead_id') or '').strip()
        lead = None
        if lead_id.isdigit():
            lead = Lead.objects.filter(pk=int(lead_id)).first()

        descriptions = request.POST.getlist('item_description')
        quantities = request.POST.getlist('item_quantity')
        unit_prices = request.POST.getlist('item_unit_price')

        line_items = []
        errors = []
        for desc, qty_raw, price_raw in zip(descriptions, quantities, unit_prices):
            desc = (desc or '').strip()
            if not desc:
                continue
            try:
                qty = Decimal(qty_raw or '1')
                price = Decimal(price_raw or '0')
            except InvalidOperation:
                errors.append(f'Invalid quantity/price for "{desc}".')
                continue
            line_items.append({'description': desc, 'quantity': qty, 'unit_price': price})

        if not line_items:
            errors.append('Add at least one item with a description.')

        received_raw = (request.POST.get('received_amount') or '0').strip()
        try:
            received_amount = Decimal(received_raw or '0')
        except InvalidOperation:
            received_amount = Decimal('0')
            errors.append('Invalid received amount.')

        payment_method = request.POST.get('payment_method') or BillPayment.PaymentMethod.CASH
        if payment_method not in dict(BillPayment.PaymentMethod.choices):
            payment_method = BillPayment.PaymentMethod.CASH

        notes = (request.POST.get('notes') or '').strip()

        if not errors:
            bill = create_quick_bill(
                created_by=request.user,
                lead=lead,
                line_items=line_items,
                received_amount=received_amount,
                payment_method=payment_method,
                notes=notes,
            )
            return redirect(reverse('crm:bill_detail', kwargs={'pk': bill.pk}) + '?preview=1')

        posted_items = [
            {'description': d, 'quantity': q, 'unit_price': p}
            for d, q, p in zip(descriptions, quantities, unit_prices)
            if (d or '').strip()
        ]
        return render(
            request,
            'crm/billing/bill_create.html',
            {
                'errors': errors,
                'packages': Package.objects.all().order_by('name'),
                'posted_items': posted_items,
                'payment_method_choices': BillPayment.PaymentMethod.choices,
                'form_lead_id': lead_id,
                'form_lead_name': lead.name if lead else '',
                'form_notes': notes,
                'form_received_amount': received_raw,
                'form_payment_method': payment_method,
            },
        )

    prefill_lead = None
    lead_param = (request.GET.get('lead') or '').strip()
    if lead_param.isdigit():
        prefill_lead = Lead.objects.filter(pk=int(lead_param)).first()
    return render(
        request,
        'crm/billing/bill_create.html',
        {
            'packages': Package.objects.all().order_by('name'),
            'posted_items': [],
            'payment_method_choices': BillPayment.PaymentMethod.choices,
            'form_lead_id': prefill_lead.pk if prefill_lead else '',
            'form_lead_name': prefill_lead.name if prefill_lead else '',
        },
    )


@login_required
@admin_required
def bill_detail(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related('lead').prefetch_related('line_items', 'payments'),
        pk=pk,
    )
    return render(
        request,
        'crm/billing/bill_detail.html',
        {
            'bill': bill,
            'payment_method_choices': BillPayment.PaymentMethod.choices,
            'auto_preview': request.GET.get('preview') == '1',
        },
    )


@login_required
@admin_required
def bill_pdf(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    if not bill.pdf_file:
        raise Http404('Receipt PDF not available.')
    return FileResponse(
        bill.pdf_file.open('rb'),
        as_attachment=False,
        filename=f'{bill.bill_number.replace("/", "-")}.pdf',
    )


@login_required
@admin_required
@require_POST
def bill_payment_create(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    if bill.balance_due <= 0:
        messages.error(request, 'This bill is already fully paid.')
        return redirect('crm:bill_detail', pk=bill.pk)

    amount_raw = (request.POST.get('amount') or '').strip()
    try:
        amount = Decimal(amount_raw)
    except InvalidOperation:
        amount = Decimal('0')
    if amount <= 0:
        messages.error(request, 'Enter a payment amount greater than zero.')
        return redirect('crm:bill_detail', pk=bill.pk)

    payment_method = request.POST.get('payment_method') or BillPayment.PaymentMethod.CASH
    if payment_method not in dict(BillPayment.PaymentMethod.choices):
        payment_method = BillPayment.PaymentMethod.CASH

    add_payment_to_bill(
        bill,
        amount=amount,
        payment_method=payment_method,
        notes=(request.POST.get('notes') or '').strip(),
        transaction_id=(request.POST.get('transaction_id') or '').strip(),
        created_by=request.user,
    )
    messages.success(request, 'Payment recorded.')
    return redirect('crm:bill_detail', pk=bill.pk)


@login_required
@admin_required
def bill_payment_receipt(request, pk):
    payment = get_object_or_404(BillPayment, pk=pk)
    if not payment.receipt_pdf:
        raise Http404('Receipt PDF not available.')
    filename = f'{(payment.receipt_number or str(payment.pk)).replace("/", "-")}.pdf'
    return FileResponse(payment.receipt_pdf.open('rb'), as_attachment=False, filename=filename)
