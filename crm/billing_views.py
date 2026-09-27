"""
Simple billing: pick a lead (optional), add line items, enter received
amount, get a Bill + PDF payment receipt. No ledger, no accounts.
"""

from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .models import Bill, BillPayment, Lead, Package
from .services.simple_billing import create_quick_bill

BILLS_PER_PAGE = 20


@login_required
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
            return redirect('crm:bill_detail', pk=bill.pk)

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

    return render(
        request,
        'crm/billing/bill_create.html',
        {
            'packages': Package.objects.all().order_by('name'),
            'posted_items': [],
            'payment_method_choices': BillPayment.PaymentMethod.choices,
        },
    )


@login_required
def bill_detail(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related('lead').prefetch_related('line_items', 'payments'),
        pk=pk,
    )
    return render(request, 'crm/billing/bill_detail.html', {'bill': bill})


@login_required
def bill_pdf(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    if not bill.pdf_file:
        raise Http404('Receipt PDF not available.')
    return FileResponse(
        bill.pdf_file.open('rb'),
        as_attachment=False,
        filename=f'{bill.bill_number.replace("/", "-")}.pdf',
    )
