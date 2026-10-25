"""
Invoicing, allocation, credit notes, write-offs (Part 7).

Locks are always taken in the fixed order payment link -> payment -> invoice -> journal
(Part 14.3) to avoid deadlocks. Issued invoices are immutable: the only changes after
issue are allocations, credit notes, cancellation (with reversal) and write-off.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from crm.core import approvals, audit, documents, gst, india, notify, numbering, pdf, permissions, posting
from crm.core.money import D, ZERO, q2
from crm.models import (CompanySettings, CreditNote, Invoice, InvoiceLine, Payment, PaymentAllocation)
from crm.models.billing import INV_CANCELLED, INV_CREDIT_NOTED, INV_DRAFT, INV_ISSUED, INV_OPEN, INV_PAID, INV_PARTIAL, INV_PENDING, INV_WRITTEN_OFF


class BillingError(ValidationError):
    pass


def inv_url(inv) -> str:
    return reverse('crm:invoice_detail', args=[inv.pk])


# ---------------------------------------------------------------- calculation

def recalculate(inv: Invoice, save: bool = True):
    company = CompanySettings.get()
    lines = list(inv.lines.all())
    result = documents.compute(inv, lines, company.round_off_invoice)
    documents.apply(inv, lines, result, save_lines=save)
    inv.balance = inv.grand_total
    if save:
        inv.save(update_fields=['place_of_supply', 'supply_type', 'subtotal', 'discount_total', 'taxable_total', 'cgst_total',
                                'sgst_total', 'igst_total', 'tax_total', 'round_off', 'grand_total', 'tax_snapshot', 'balance'])
    return result


def refresh_balance(inv: Invoice) -> Invoice:
    """Recompute paid/credited/balance/status from allocations and credit notes. Caller holds the invoice lock."""
    allocs = inv.allocations.filter(reversed_at__isnull=True)
    adv = allocs.filter(is_advance_adjustment=True).aggregate(a=Sum('amount'), t=Sum('tds_amount'))
    cash = allocs.filter(is_advance_adjustment=False).aggregate(a=Sum('amount'), t=Sum('tds_amount'))
    inv.advance_adjusted = (adv['a'] or ZERO) + (adv['t'] or ZERO)
    inv.paid_amount = cash['a'] or ZERO
    inv.tds_withheld = cash['t'] or ZERO
    inv.credit_noted = inv.credit_notes.filter(status='issued').aggregate(s=Sum('total'))['s'] or ZERO
    settled = inv.advance_adjusted + inv.paid_amount + inv.tds_withheld + inv.credit_noted + inv.written_off
    balance = inv.grand_total - settled
    if balance < 0:
        raise BillingError(f'Allocations and credits ({settled}) cannot exceed the invoice total ({inv.grand_total}).')
    inv.balance = balance
    if inv.status in (INV_ISSUED, INV_PARTIAL, INV_PAID, INV_CREDIT_NOTED):
        if inv.credit_noted and inv.credit_noted >= inv.grand_total:
            inv.status = INV_CREDIT_NOTED
        elif balance == 0:
            if inv.status != INV_PAID:
                inv.paid_at = timezone.now()
            inv.status = INV_PAID if not inv.credit_noted or inv.paid_amount + inv.advance_adjusted else INV_CREDIT_NOTED
        elif settled > 0:
            inv.status = INV_PARTIAL
        else:
            inv.status = INV_ISSUED
    inv.save(update_fields=['advance_adjusted', 'paid_amount', 'tds_withheld', 'credit_noted', 'balance', 'status', 'paid_at'])
    return inv


# ---------------------------------------------------------------- creation

def prefill(*, client, owner, quotation=None) -> Invoice:
    c = CompanySettings.get()
    today = india.today()
    inv = Invoice(client=client, owner=owner, quotation=quotation, invoice_date=today,
                  due_date=today + timedelta(days=client.payment_terms_days or c.invoice_due_days),
                  customer_name=client.legal_name, customer_gstin=client.gstin, customer_state=client.billing_state,
                  customer_type=client.customer_type, billing_address=client.billing_address,
                  shipping_address=client.shipping_address, place_of_supply=client.billing_state,
                  terms=c.invoice_terms)
    if quotation is not None:
        inv.price_mode = quotation.price_mode
        inv.discount_mode = quotation.discount_mode
        inv.discount_value = quotation.discount_value
        inv.place_of_supply = quotation.place_of_supply or inv.place_of_supply
    return inv


def copy_quotation_lines(inv: Invoice, quotation):
    for i, ql in enumerate(quotation.lines.all()):
        InvoiceLine.objects.create(invoice=inv, sort=i, service=ql.service, description=ql.description, sac_code=ql.sac_code,
                                   quantity=ql.quantity, unit=ql.unit, unit_price=ql.unit_price, discount_mode=ql.discount_mode,
                                   discount_value=ql.discount_value, taxability=ql.taxability, tax_rate=ql.tax_rate,
                                   catalogue_price=ql.catalogue_price)


def issue_blockers(inv: Invoice) -> list[str]:
    errs = []
    lines = list(inv.lines.all())
    if not lines:
        errs.append('Add at least one line')
    if inv.customer_type == 'registered' and not inv.customer_gstin:
        errs.append('A registered (B2B) customer needs a GSTIN')
    if not (inv.place_of_supply or inv.customer_type == 'overseas'):
        errs.append('Add the place of supply')
    if inv.due_date < inv.invoice_date:
        errs.append('The due date cannot be before the invoice date')
    if posting.period_status(inv.invoice_date) == 'closed':
        errs.append(f'The invoice date {inv.invoice_date:%d-%b-%Y} is in a closed period')
    if inv.grand_total <= 0:
        errs.append('The invoice total must be more than ₹0')
    if any(l.taxability == 'taxable' and not l.sac_code for l in lines):
        errs.append('Every taxable line needs a SAC or HSN code')
    return errs


def issue(inv: Invoice, actor, request=None, approved: bool = False) -> Invoice:
    """Allocate the number, freeze tax, store the PDF, queue the ledger posting (Part 7.4)."""
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.status not in (INV_DRAFT, INV_PENDING):
            raise BillingError('This invoice is already issued.')
        if inv.status == INV_PENDING and not approved:
            raise BillingError('This invoice is waiting for approval.')
        recalculate(inv)
        errs = issue_blockers(inv)
        if errs:
            raise BillingError('Cannot issue: ' + '; '.join(errs) + '.')
        if not approved:
            reasons = issue_approval_reasons(inv, actor)
            if reasons:
                raise BillingError('Needs approval first: ' + '; '.join(reasons) + '.')
        posting.ensure_period_open(inv.invoice_date, actor)
        inv.number = numbering.allocate('INV', inv.invoice_date)
        inv.fiscal_year = india.fy_label(inv.invoice_date)
        inv.status = INV_ISSUED
        inv.issued_at = timezone.now()
        inv.issued_by = actor
        inv.balance = inv.grand_total
        inv.row_version += 1
        inv.save()
        data = pdf.invoice_pdf(inv)
        inv.pdf_sha256 = pdf.sha256(data)
        inv.pdf_file.save(f'{inv.number.replace("/", "-")}.pdf', ContentFile(data), save=False)
        inv.save(update_fields=['pdf_file', 'pdf_sha256'])
        posting.enqueue('invoice_issued', inv, 'issued', {'total': str(inv.grand_total)})
        audit.log('invoice.issued', inv, actor=actor, changes={'number': inv.number, 'total': str(inv.grand_total),
                                                               'pdf_sha256': inv.pdf_sha256})
        if inv.quotation_id:
            from crm.modules.quotations.services import mark_converted
            mark_converted(inv.quotation, actor, 'invoice')
        from crm.modules.payroll.services import accrue_commission_for_invoice
        accrue_commission_for_invoice(inv)
    return inv


def regenerate_pdf(inv: Invoice) -> bytes:
    """Status changes (paid/cancelled) never alter the stored issued PDF; this renders the current view."""
    return pdf.invoice_pdf(inv)


def send(inv: Invoice, actor, *, to: str, cc=None, message: str = '', include_link: bool = True, request=None):
    from crm.models import MessageTemplate, PaymentLink
    if inv.status not in INV_OPEN + [INV_PAID]:
        raise BillingError('Only an issued invoice can be sent.')
    inv.pdf_file.open('rb')
    data = inv.pdf_file.read()
    inv.pdf_file.close()
    link_url = ''
    if include_link:
        link = PaymentLink.objects.filter(invoice=inv, status__in=['active', 'partially_paid']).order_by('-id').first()
        if link:
            from crm.modules.payments.services import public_url
            link_url = public_url(link, request)
    company = CompanySettings.get()
    tpl = MessageTemplate.objects.filter(code='invoice_send', is_active=True).order_by('-version').first()
    ctx = {'name': inv.client.billing_contact.name if inv.client.billing_contact else str(inv.client),
           'number': inv.number, 'total': pdf._money(inv.grand_total), 'due_date': inv.due_date.strftime('%d-%b-%Y'),
           'link': link_url, 'sender': actor.get_full_name() or actor.username, 'company': company.legal_name}
    subject = notify.render(tpl.subject, ctx) if tpl else f'Invoice {inv.number}'
    body = (message + '\n\n' if message else '') + (notify.render(tpl.body, ctx) if tpl else '')
    notify.send_email(to, subject, body, cc=cc, sent_by=actor, obj=inv, template_code='invoice_send',
                      attachments=[(f'{inv.number.replace("/", "-")}.pdf', data, 'application/pdf')])
    Invoice.objects.filter(pk=inv.pk).update(sent_at=timezone.now())
    audit.log('invoice.sent', inv, actor=actor, changes={'to': to})


# ---------------------------------------------------------------- allocation (7.9)

def allocate(payment: Payment, rows: list[tuple], actor, *, after_receipt: bool = True) -> list[PaymentAllocation]:
    """rows = [(invoice, cash_amount, tds_amount)]. Atomic with row locks on payment then invoices (sorted)."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status not in ('verified', 'partially_refunded'):
            raise BillingError('Only a verified payment can be allocated.')
        invoices = {i.pk: i for i in Invoice.objects.select_for_update().filter(pk__in=[r[0].pk for r in rows]).order_by('pk')}
        total = sum((D(r[1]) + D(r[2]) for r in rows), ZERO)
        if total <= 0:
            raise BillingError('Enter an amount to allocate.')
        available = payment.unallocated - payment.refunded_amount
        if total > available:
            raise BillingError(f'Total allocation ({total}) cannot exceed the unallocated amount ({available}).')
        tds_total = sum((D(r[2]) for r in rows), ZERO)
        used_tds = payment.allocations.filter(reversed_at__isnull=True).aggregate(s=Sum('tds_amount'))['s'] or ZERO
        if tds_total > payment.tds_amount - used_tds:
            raise BillingError('TDS allocated cannot exceed the TDS recorded on the payment.')
        made = []
        for inv_ref, cash, tds in rows:
            inv = invoices[inv_ref.pk]
            cash, tds = q2(cash), q2(tds)
            if cash + tds <= 0:
                continue
            if inv.client_id != payment.client_id:
                raise BillingError(f'{inv} belongs to a different client.')
            if inv.status not in INV_OPEN:
                raise BillingError(f'{inv} is not open for payment.')
            if cash + tds > inv.balance:
                raise BillingError(f'Allocation to {inv} ({cash + tds}) is more than its balance ({inv.balance}).')
            a = PaymentAllocation.objects.create(payment=payment, invoice=inv, amount=cash, tds_amount=tds,
                                                 is_advance_adjustment=after_receipt, created_by=actor)
            refresh_balance(inv)
            made.append(a)
            if after_receipt:
                posting.enqueue('advance_allocated', a, 'allocated')
            audit.log('payment.allocated', inv, actor=actor, changes={'payment': str(payment), 'amount': str(cash), 'tds': str(tds)})
        payment.allocated_amount = (payment.allocated_amount or ZERO) + sum((a.total for a in made), ZERO)
        payment.save(update_fields=['allocated_amount'])
    return made


def auto_allocation_plan(payment: Payment) -> list[tuple]:
    """Oldest invoices first; exact-match invoice wins (Part 7.9 auto-suggest)."""
    remaining = payment.unallocated
    open_inv = list(Invoice.objects.filter(client=payment.client, status__in=INV_OPEN, balance__gt=0).order_by('due_date', 'id'))
    exact = [i for i in open_inv if i.balance == remaining]
    if exact:
        return [(exact[0], remaining, ZERO)]
    plan = []
    for inv in open_inv:
        if remaining <= 0:
            break
        amt = min(inv.balance, remaining)
        plan.append((inv, amt, ZERO))
        remaining -= amt
    return plan


def reverse_allocation(allocation: PaymentAllocation, actor, reason: str):
    if not permissions.has_perm(actor, 'accounting', 'approve'):
        permissions.denied(actor, 'accounting', 'approve', detail='allocation reversal')
    if len(reason.strip()) < 10:
        raise BillingError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=allocation.payment_id)
        inv = Invoice.objects.select_for_update().get(pk=allocation.invoice_id)
        a = PaymentAllocation.objects.select_for_update().get(pk=allocation.pk)
        if a.reversed_at:
            raise BillingError('This allocation is already reversed.')
        a.reversed_at = timezone.now()
        a.reversed_by = actor
        a.reversal_reason = reason
        a.save(update_fields=['reversed_at', 'reversed_by', 'reversal_reason'])
        payment.allocated_amount -= a.total
        payment.save(update_fields=['allocated_amount'])
        refresh_balance(inv)
        posting.enqueue('allocation_reversed', a, 'reversed')
        audit.log('payment.allocation_reversed', inv, actor=actor, reason=reason, changes={'payment': str(payment), 'amount': str(a.total)})
    return a


# ---------------------------------------------------------------- credit notes (7.8)

def credit_note_amounts(inv: Invoice, taxable: Decimal) -> dict:
    """Tax recomputed proportionally to the invoice's own tax split."""
    taxable = q2(taxable)
    if inv.taxable_total <= 0:
        raise BillingError('This invoice has no taxable value to credit.')
    tax = q2(inv.tax_total * taxable / inv.taxable_total)
    cgst, sgst, igst = gst.split_tax(tax, inv.cgst_total, inv.sgst_total, inv.igst_total)
    return {'taxable_value': taxable, 'cgst': cgst, 'sgst': sgst, 'igst': igst, 'total': taxable + cgst + sgst + igst}


def create_credit_note(inv: Invoice, actor, *, taxable: Decimal, reason: str, notes: str = '', treatment: str = 'reduce_balance') -> CreditNote:
    if reason == 'other' and len(notes.strip()) < 5:
        raise BillingError('Add a note when the reason is Other.')
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.status not in (INV_ISSUED, INV_PARTIAL, INV_PAID):
            raise BillingError('Credit notes need an issued, uncancelled invoice.')
        amounts = credit_note_amounts(inv, taxable)
        already = inv.credit_notes.exclude(status='cancelled').aggregate(s=Sum('total'))['s'] or ZERO
        if amounts['total'] + already > inv.grand_total:
            raise BillingError(f'Credit notes ({amounts["total"] + already}) cannot exceed the invoice total ({inv.grand_total}).')
        if treatment == 'reduce_balance' and amounts['total'] > inv.balance:
            raise BillingError(f'The credit ({amounts["total"]}) is more than the open balance ({inv.balance}). '
                               'Choose "Refund money" to credit paid amounts.')
        cn = CreditNote.objects.create(invoice=inv, note_date=india.today(), reason=reason, notes=notes, treatment=treatment,
                                       created_by=actor, **amounts)
        company = CompanySettings.get()
        needs = amounts['total'] > company.credit_note_approval_above or not permissions.has_perm(actor, 'invoices', 'approve')
        if needs:
            cn.status = 'pending_approval'
            cn.save(update_fields=['status'])
            approvals.submit('credit_note', cn, title=f'Credit note {pdf._money(cn.total)} on {inv.number}', module='invoices',
                             requested_by=actor, level='accountant', amount=cn.total, reasons=[cn.get_reason_display()],
                             before={'invoice balance': str(inv.balance)}, after={'credit': str(cn.total)}, message=notes)
        else:
            issue_credit_note(cn, actor)
        audit.log('credit_note.created', inv, actor=actor, changes={'total': str(cn.total), 'reason': reason})
    return cn


def issue_credit_note(cn: CreditNote, actor):
    with transaction.atomic():
        cn = CreditNote.objects.select_for_update().get(pk=cn.pk)
        if cn.status == 'issued':
            return cn
        inv = Invoice.objects.select_for_update().get(pk=cn.invoice_id)
        posting.ensure_period_open(cn.note_date, actor)
        if cn.treatment == 'refund':
            # Free up paid money: unwind allocations (newest first) by the credit amount; it becomes a refundable advance.
            need = cn.total - min(cn.total, inv.balance)
            for a in inv.allocations.filter(reversed_at__isnull=True).order_by('-id'):
                if need <= 0:
                    break
                keep = max(a.total - need, ZERO)
                need -= a.total - keep
                payment = Payment.objects.select_for_update().get(pk=a.payment_id)
                a.reversed_at = timezone.now()
                a.reversed_by = actor
                a.reversal_reason = f'Credit note {cn.pk} (refund)'
                a.save(update_fields=['reversed_at', 'reversed_by', 'reversal_reason'])
                payment.allocated_amount -= a.total
                posting.enqueue('allocation_reversed', a, 'reversed')
                if keep > 0:
                    tds_keep = min(a.tds_amount, keep)
                    na = PaymentAllocation.objects.create(payment=payment, invoice=inv, amount=keep - tds_keep, tds_amount=tds_keep,
                                                          is_advance_adjustment=True, created_by=actor)
                    payment.allocated_amount += na.total
                    posting.enqueue('advance_allocated', na, 'allocated')
                payment.save(update_fields=['allocated_amount'])
        cn.number = numbering.allocate('CN', cn.note_date)
        cn.status = 'issued'
        cn.issued_at = timezone.now()
        cn.issued_by = actor
        cn.save(update_fields=['number', 'status', 'issued_at', 'issued_by'])
        refresh_balance(inv)
        posting.enqueue('credit_note_issued', cn, 'issued')
        audit.log('credit_note.issued', inv, actor=actor, changes={'number': cn.number, 'total': str(cn.total)})
        from crm.modules.payroll.services import clawback_for_credit_note
        clawback_for_credit_note(cn)
    return cn


def _cn_approved(req, user):
    issue_credit_note(CreditNote.objects.get(pk=req.object_id), req.requested_by)


def _cn_rejected(req, user):
    CreditNote.objects.filter(pk=req.object_id, status='pending_approval').update(status='cancelled')


approvals.register('credit_note', _cn_approved, _cn_rejected)


# ---------------------------------------------------------------- cancellation and write-off

def cancel(inv: Invoice, actor, reason: str):
    """Cancel an issued invoice with no money against it; posts a reversal. The number is kept as Cancelled."""
    if len(reason.strip()) < 10:
        raise BillingError('Give a reason of at least 10 characters.')
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.status == INV_DRAFT:
            raise BillingError('A draft is not issued; just leave it or delete it.')
        if inv.status not in (INV_ISSUED,):
            raise BillingError('Only an issued invoice with no payments or credit notes can be cancelled. Use a credit note instead.')
        if inv.allocations.filter(reversed_at__isnull=True).exists() or inv.credit_notes.exclude(status='cancelled').exists():
            raise BillingError('This invoice has payments or credit notes. Use a credit note instead.')
        posting.ensure_period_open(timezone.localdate(), actor)
        inv.status = INV_CANCELLED
        inv.cancelled_at = timezone.now()
        inv.cancel_reason = reason
        inv.balance = ZERO
        inv.save(update_fields=['status', 'cancelled_at', 'cancel_reason', 'balance'])
        posting.enqueue('invoice_cancelled', inv, 'cancelled')
        audit.log('invoice.cancelled', inv, actor=actor, reason=reason)
    return inv


def request_write_off(inv: Invoice, actor, reason: str):
    if len(reason.strip()) < 10:
        raise BillingError('Give a reason of at least 10 characters.')
    if inv.status not in INV_OPEN or inv.balance <= 0:
        raise BillingError('Only an unpaid balance can be written off.')
    return approvals.submit('write_off', inv, title=f'Write off {pdf._money(inv.balance)} on {inv.number}', module='invoices',
                            requested_by=actor, level='owner', amount=inv.balance, reasons=['Bad-debt write-off'],
                            before={'balance': str(inv.balance)}, after={'balance': '0.00', 'status': 'Written off'},
                            message=reason)


def _write_off_approved(req, user):
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=req.object_id)
        if inv.status not in INV_OPEN or inv.balance <= 0:
            return
        amount = inv.balance
        inv.written_off = amount
        inv.written_off_at = timezone.now()
        inv.write_off_reason = req.message
        inv.balance = ZERO
        inv.status = INV_WRITTEN_OFF
        inv.save(update_fields=['written_off', 'written_off_at', 'write_off_reason', 'balance', 'status'])
        posting.enqueue('invoice_written_off', inv, 'written_off', {'amount': str(amount)})
        audit.log('invoice.written_off', inv, actor=user, changes={'amount': str(amount)}, reason=req.message)


approvals.register('write_off', _write_off_approved)


def record_promise(inv: Invoice, actor, *, date, amount, note: str):
    if date < india.today():
        raise BillingError('The promise date cannot be in the past.')
    Invoice.objects.filter(pk=inv.pk).update(promise_date=date, promise_amount=amount, promise_note=note[:300])
    audit.log('invoice.promise_to_pay', inv, actor=actor, changes={'date': date, 'amount': str(amount)})


def set_dispute(inv: Invoice, actor, disputed: bool, reason: str = ''):
    if disputed and len(reason.strip()) < 5:
        raise BillingError('Describe the dispute.')
    Invoice.objects.filter(pk=inv.pk).update(disputed=disputed, dispute_reason=reason[:300] if disputed else '')
    audit.log('invoice.disputed' if disputed else 'invoice.dispute_resolved', inv, actor=actor, reason=reason)
    if disputed:
        notify.notify([inv.owner], f'Invoice {inv.number} disputed', body=reason, url=inv_url(inv), event_type='invoice_disputed',
                      level='warning')


# ---------------------------------------------------------------- statements and ageing (7.7)

def statement_rows(client, start, end):
    from crm.models import CreditNote
    events = []
    for inv in Invoice.objects.filter(client=client, number__gt='').exclude(status=INV_DRAFT):
        events.append((inv.invoice_date, f'Invoice {inv.number}', inv.grand_total, ZERO))
        if inv.status == INV_CANCELLED:
            events.append((inv.cancelled_at.date(), f'Cancellation {inv.number}', ZERO, inv.grand_total))
        if inv.written_off:
            events.append((inv.written_off_at.date(), f'Write-off {inv.number}', ZERO, inv.written_off))
    for cn in CreditNote.objects.filter(invoice__client=client, status='issued'):
        events.append((cn.note_date, f'Credit note {cn.number}', ZERO, cn.total))
    for p in Payment.objects.filter(client=client, status__in=['verified', 'partially_refunded', 'refunded']):
        events.append((p.payment_date, f'Payment {p.receipt_number} ({p.get_method_display()})', ZERO, p.gross_settled))
        for r in p.refunds.filter(status='processed'):
            events.append((r.processed_at.date(), f'Refund on {p.receipt_number}', r.amount, ZERO))
    events.sort(key=lambda e: e[0])
    bal = ZERO
    opening = ZERO
    rows = []
    for d, label, dr, cr in events:
        if d < start:
            opening += dr - cr
            continue
        if d > end:
            continue
        if not rows:
            bal = opening
            if opening:
                rows.append({'date': start, 'label': 'Opening balance', 'debit': ZERO, 'credit': ZERO, 'balance': opening})
        bal += dr - cr
        rows.append({'date': d, 'label': label, 'debit': dr, 'credit': cr, 'balance': bal})
    closing = bal if rows else opening
    return rows, closing


def ageing(qs=None, as_of=None):
    as_of = as_of or india.today()
    qs = qs if qs is not None else Invoice.objects.all()
    qs = qs.filter(status__in=INV_OPEN, balance__gt=0, invoice_date__lte=as_of).select_related('client', 'owner')
    buckets = ['Current', '1-30', '31-60', '61-90', '90+']
    by_client = {}
    totals = {b: ZERO for b in buckets}
    for inv in qs:
        days = (as_of - inv.due_date).days
        b = 'Current' if days <= 0 else '1-30' if days <= 30 else '31-60' if days <= 60 else '61-90' if days <= 90 else '90+'
        row = by_client.setdefault(inv.client_id, {'client': inv.client, **{x: ZERO for x in buckets}, 'total': ZERO})
        row[b] += inv.balance
        row['total'] += inv.balance
        totals[b] += inv.balance
    totals['total'] = sum((totals[b] for b in buckets), ZERO)
    return sorted(by_client.values(), key=lambda r: -r['total']), totals, buckets


# ---------------------------------------------------------------- issue approval and advances

def issue_approval_reasons(inv: Invoice, actor) -> list[str]:
    c = CompanySettings.get()
    reasons = []
    disc = documents.effective_discount_percent(inv)
    if disc > c.exec_discount_limit and not permissions.has_perm(actor, 'invoices', 'approve'):
        reasons.append(f'Discount of {disc}% is above the {c.exec_discount_limit}% limit')
    if inv.gst_overridden:
        reasons.append('GST treatment was overridden')
    return reasons


def submit_issue_approval(inv: Invoice, actor, message: str = ''):
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=inv.pk)
        if inv.status != INV_DRAFT:
            raise BillingError('Only a draft can be sent for approval.')
        recalculate(inv)
        errs = issue_blockers(inv)
        if errs:
            raise BillingError('Fix first: ' + '; '.join(errs) + '.')
        reasons = issue_approval_reasons(inv, actor)
        inv.status = INV_PENDING
        inv.save(update_fields=['status'])
        approvals.submit('invoice_issue', inv, title=f'Issue invoice for {inv.client} ({pdf._money(inv.grand_total)})',
                         module='invoices', requested_by=actor, level='accountant' if inv.gst_overridden else 'manager',
                         amount=inv.grand_total, reasons=reasons or ['Approval requested'], message=message)


def _issue_approved(req, user):
    inv = Invoice.objects.get(pk=req.object_id)
    if inv.status == INV_PENDING:
        issue(inv, req.requested_by, approved=True)


def _issue_rejected(req, user):
    Invoice.objects.filter(pk=req.object_id, status=INV_PENDING).update(status=INV_DRAFT)


approvals.register('invoice_issue', _issue_approved, _issue_rejected)


def open_advances(client):
    from crm.modules.payments.services import held_advance
    out = []
    for p in Payment.objects.filter(client=client, status__in=['verified', 'partially_refunded']).order_by('payment_date', 'id'):
        h = held_advance(p)
        if h > 0:
            out.append((p, h))
    return out


def apply_advances(inv: Invoice, actor, selections: list[tuple]):
    """selections = [(payment, amount)]: adjust held customer advances against an issued invoice (Part 7.5 step 3)."""
    total = ZERO
    for payment, amount in selections:
        amount = q2(amount)
        if amount <= 0:
            continue
        inv.refresh_from_db()
        take = min(amount, inv.balance)
        if take <= 0:
            break
        allocate(payment, [(inv, take, ZERO)], actor, after_receipt=True)
        total += take
    return total


# ---------------------------------------------------------------- reminders (7.7)

REMINDER_STEPS = [('before', -3), ('due', 0), ('d3', 3), ('d7', 7), ('d15', 15)]


def reminder_blocked(inv: Invoice) -> str:
    if inv.status not in INV_OPEN or inv.balance <= 0:
        return 'Not open'
    if inv.disputed:
        return 'Disputed'
    if inv.client.dunning_paused:
        return 'Reminders paused for this client'
    if inv.promise_date and inv.promise_date >= india.today():
        return f'Promise to pay on {inv.promise_date:%d-%b-%Y}'
    if not (inv.client.billing_contact and inv.client.billing_contact.email) and not inv.client.email:
        return 'No email address'
    return ''


def send_reminder(inv: Invoice, step: str, actor=None) -> bool:
    from crm.models import MessageTemplate, PaymentLink
    if reminder_blocked(inv):
        return False
    contact = inv.client.billing_contact
    to = (contact.email if contact and contact.email else '') or inv.client.email
    link = PaymentLink.objects.filter(invoice=inv, status__in=['active', 'partially_paid']).order_by('-id').first()
    from crm.modules.payments.services import public_url
    company = CompanySettings.get()
    tpl = MessageTemplate.objects.filter(code='invoice_reminder', is_active=True).order_by('-version').first()
    ctx = {'name': contact.name if contact else str(inv.client), 'number': inv.number, 'balance': pdf._money(inv.balance),
           'due_date': inv.due_date.strftime('%d-%b-%Y'), 'days': inv.days_overdue, 'link': public_url(link) if link else '',
           'company': company.legal_name}
    subject = notify.render(tpl.subject, ctx) if tpl else f'Payment reminder: invoice {inv.number}'
    body = notify.render(tpl.body, ctx) if tpl else (
        f'Dear {ctx["name"]},\n\nThis is a reminder that {ctx["balance"]} is due on invoice {inv.number} '
        f'(due {ctx["due_date"]}).' + (f'\nPay securely: {ctx["link"]}' if ctx['link'] else '') + f'\n\nThank you,\n{company.legal_name}')
    notify.send_email(to, subject, body, sent_by=actor, obj=inv, template_code='invoice_reminder')
    Invoice.objects.filter(pk=inv.pk).update(last_reminder_step=step, last_reminder_at=timezone.now())
    audit.log('invoice.reminder_sent', inv, actor=actor, changes={'step': step, 'to': to})
    return True


# ---------------------------------------------------------------- TDS receivable (7.6)

def update_tds(t, actor, *, status: str, certificate_number: str = '', certificate_date=None, note: str = ''):
    from crm.models import TdsReceivable
    if t.status == 'written_off':
        raise BillingError('This entry was written off.')
    TdsReceivable.objects.filter(pk=t.pk).update(status=status, certificate_number=certificate_number, certificate_date=certificate_date,
                                                 note=note[:300])
    audit.log('tds_receivable.updated', t.payment, actor=actor, changes={'status': status, 'certificate': certificate_number})


def request_tds_write_off(t, actor, reason: str):
    if len(reason.strip()) < 10:
        raise BillingError('Give a reason of at least 10 characters.')
    if t.status in ('claimed', 'written_off'):
        raise BillingError('This TDS was already claimed or written off.')
    return approvals.submit('tds_write_off', t, title=f'Write off TDS {pdf._money(t.amount)} from {t.client}', module='accounting',
                            requested_by=actor, level='accountant', amount=t.amount, reasons=['Unclaimable TDS'], message=reason)


def _tds_write_off_approved(req, user):
    from crm.models import TdsReceivable
    with transaction.atomic():
        t = TdsReceivable.objects.select_for_update().get(pk=req.object_id)
        if t.status == 'written_off':
            return
        t.status = 'written_off'
        t.note = req.message[:300]
        t.save(update_fields=['status', 'note'])
        posting.enqueue('tds_written_off', t, 'written_off')
        audit.log('tds_receivable.written_off', t.payment, actor=user, reason=req.message)


approvals.register('tds_write_off', _tds_write_off_approved)
