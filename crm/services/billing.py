"""Billing: bill numbers, ledger, PDF generation, client email."""

from __future__ import annotations

import io
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..billing_constants import ASSETS, COMPANY
from ..models import (
    Bill,
    BillLineItem,
    BillPayment,
    BillSequence,
    Client,
    LedgerEntry,
    OnboardingSubmission,
    Project,
)
from . import audit as audit_service

logger = logging.getLogger(__name__)


def fiscal_year_suffix(d: date | None = None) -> str:
    """Indian FY suffix e.g. 2627 for FY 2026–27."""
    if d is None:
        d = timezone.localdate()
    start_year = d.year if d.month >= 4 else d.year - 1
    return f'{start_year % 100:02d}{(start_year + 1) % 100:02d}'


def allocate_bill_number() -> str:
    """Thread-safe bill number: BTX/2627/00001."""
    fy = fiscal_year_suffix()
    with transaction.atomic():
        seq, _ = BillSequence.objects.select_for_update().get_or_create(
            fiscal_year=fy,
            defaults={'last_number': 0},
        )
        seq.last_number += 1
        seq.save(update_fields=['last_number'])
        return f'BTX/{fy}/{seq.last_number:05d}'


def get_billing_email_for_project(project: Project) -> str:
    """
    Email for billing receipts/invoices.
    Priority: client → onboarding contact → linked lead.
    """
    client = project.client
    lead_email = ''
    if client.lead_id:
        lead_email = (client.lead.email or '')

    onboarding_email = ''
    try:
        onboarding_email = project.onboarding.contact_email or ''
    except OnboardingSubmission.DoesNotExist:
        pass

    for candidate in (client.email, onboarding_email, lead_email):
        email = (candidate or '').strip()
        if email:
            return email
    return ''


def get_billing_email_source_for_project(project: Project) -> str:
    """Where get_billing_email_for_project found the address (for UI hints)."""
    client = project.client
    if (client.email or '').strip():
        return 'client'
    try:
        if (project.onboarding.contact_email or '').strip():
            return 'onboarding'
    except OnboardingSubmission.DoesNotExist:
        pass
    if client.lead_id and (client.lead.email or '').strip():
        return 'lead'
    return ''


def contract_ledger_reference(project: Project) -> str:
    return f'CONTRACT-{project.pk}'


def opening_advance_reference(project: Project) -> str:
    return f'OPENING-{project.pk}'


def _project_ledger_balance(project: Project) -> Decimal:
    """Amount the client owes on this project (last ledger running balance)."""
    last = (
        LedgerEntry.objects.filter(project=project)
        .order_by('-entry_date', '-created_at', '-pk')
        .values_list('balance_after', flat=True)
        .first()
    )
    return last if last is not None else Decimal('0')


def rebuild_project_ledger_balances(project: Project) -> None:
    """Recalculate balance_after for all entries in chronological order."""
    running = Decimal('0')
    for entry in LedgerEntry.objects.filter(project=project).order_by(
        'entry_date', 'created_at', 'pk'
    ):
        if entry.entry_type == LedgerEntry.EntryType.DEBIT:
            running += entry.amount
        else:
            running -= entry.amount
        if entry.balance_after != running:
            entry.balance_after = running
            entry.save(update_fields=['balance_after'])


@transaction.atomic
def ensure_project_contract_ledger(project: Project, *, actor=None) -> None:
    """
    Book contract value as opening debit so ledger balance matches project.balance_due.

    Receipt payments post credits; tax invoices skip ledger debit when contract exists.
    """
    if project.deal_value <= 0:
        rebuild_project_ledger_balances(project)
        return

    ref = contract_ledger_reference(project)
    if LedgerEntry.objects.filter(project=project, reference=ref).exists():
        rebuild_project_ledger_balances(project)
        return

    has_other_debits = LedgerEntry.objects.filter(
        project=project,
        entry_type=LedgerEntry.EntryType.DEBIT,
    ).exists()
    if has_other_debits:
        rebuild_project_ledger_balances(project)
        return

    pkg = project.package.name if project.package_id else 'Project'
    entry_date = timezone.localdate()
    if project.created_at:
        entry_date = timezone.localdate(project.created_at)
    first = (
        LedgerEntry.objects.filter(project=project)
        .order_by('entry_date', 'created_at', 'pk')
        .first()
    )
    if first and first.entry_date < entry_date:
        entry_date = first.entry_date

    LedgerEntry.objects.create(
        project=project,
        client=project.client,
        entry_type=LedgerEntry.EntryType.DEBIT,
        amount=project.deal_value,
        balance_after=Decimal('0'),
        reference=ref,
        description=f'Contract value — {pkg}',
        entry_date=entry_date,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    rebuild_project_ledger_balances(project)


def _append_ledger_entry(
    *,
    project: Project,
    entry_type: str,
    amount: Decimal,
    description: str,
    reference: str = '',
    bill: Bill | None = None,
    payment: BillPayment | None = None,
    entry_date: date | None = None,
    actor=None,
) -> LedgerEntry:
    prev = _project_ledger_balance(project)
    if entry_type == LedgerEntry.EntryType.DEBIT:
        new_bal = prev + amount
    else:
        new_bal = prev - amount
    return LedgerEntry.objects.create(
        project=project,
        client=project.client,
        entry_type=entry_type,
        amount=amount,
        balance_after=new_bal,
        bill=bill,
        payment=payment,
        reference=reference[:120],
        description=description[:300],
        entry_date=entry_date or timezone.localdate(),
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )


def sync_project_financials(project: Project) -> None:
    """Sum verified BillPayments into project.advance_received (single source of truth)."""
    total = (
        BillPayment.objects.filter(
            project=project,
            status=BillPayment.Status.VERIFIED,
        ).aggregate(t=Sum('amount'))['t']
        or Decimal('0')
    )
    project.advance_received = total
    project.save(update_fields=['advance_received', 'balance_due', 'updated_at'])


@transaction.atomic
def record_opening_advance(
    project: Project,
    amount: Decimal,
    *,
    actor=None,
    payment_date: date | None = None,
    notes: str = '',
) -> tuple[BillPayment, Bill] | None:
    """
    Book advance entered at project creation as a verified payment + receipt + ledger credit.
    Idempotent per project (reference OPENING-{pk}).
    """
    amount = Decimal(amount or 0)
    if amount <= 0:
        return None

    opening_ref = opening_advance_reference(project)
    existing = BillPayment.objects.filter(
        project=project,
        transaction_id=opening_ref,
        status=BillPayment.Status.VERIFIED,
    ).first()
    if existing and existing.bill_id:
        sync_project_financials(project)
        return existing, existing.bill

    pay_date = payment_date or timezone.localdate()
    if project.created_at:
        pay_date = timezone.localdate(project.created_at)
    pkg = project.package.name if project.package_id else 'Project'
    desc = f'Opening advance — {pkg}'
    note_text = notes or 'Advance recorded when project was created'

    bill_number = allocate_bill_number()
    bill = Bill.objects.create(
        bill_number=bill_number,
        kind=Bill.Kind.RECEIPT,
        project=project,
        client=project.client,
        bill_date=pay_date,
        gst_percent=Decimal('0'),
        description=note_text,
        status=Bill.Status.PAID,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
        issued_at=timezone.now(),
    )
    BillLineItem.objects.create(
        bill=bill,
        description=desc,
        quantity=Decimal('1'),
        unit_price=amount,
        sort_order=0,
    )
    bill.recalculate_totals()
    bill.amount_paid = bill.total_amount
    bill.balance_due = Decimal('0')
    bill.save()

    payment = BillPayment.objects.create(
        bill=bill,
        project=project,
        amount=amount,
        transaction_id=opening_ref,
        payment_method=BillPayment.PaymentMethod.OTHER,
        payment_date=pay_date,
        notes=note_text,
        status=BillPayment.Status.VERIFIED,
        recorded_by=actor if getattr(actor, 'is_authenticated', False) else None,
        verified_by=actor if getattr(actor, 'is_authenticated', False) else None,
        verified_at=timezone.now(),
    )

    ensure_project_contract_ledger(project, actor=actor)
    _append_ledger_entry(
        project=project,
        entry_type=LedgerEntry.EntryType.CREDIT,
        amount=amount,
        description=desc,
        reference=opening_ref,
        bill=bill,
        payment=payment,
        entry_date=pay_date,
        actor=actor,
    )
    sync_project_financials(project)

    pdf_bytes = render_bill_pdf(bill, payment=payment)
    bill.pdf_file.save(
        f'{bill.bill_number.replace("/", "-")}.pdf',
        ContentFile(pdf_bytes),
        save=True,
    )
    return payment, bill


def reconcile_project_billing(project: Project, *, actor=None) -> None:
    """
    Align contract ledger, opening advance, and project.advance_received.

    - advance_received always equals sum of verified BillPayments
    - Legacy advance on project (no payment rows) is converted to opening receipt once
    """
    project.refresh_from_db()
    ensure_project_contract_ledger(project, actor=actor)

    opening_ref = opening_advance_reference(project)
    has_opening = BillPayment.objects.filter(
        project=project,
        transaction_id=opening_ref,
        status=BillPayment.Status.VERIFIED,
    ).exists()

    payment_total = (
        BillPayment.objects.filter(
            project=project,
            status=BillPayment.Status.VERIFIED,
        ).aggregate(t=Sum('amount'))['t']
        or Decimal('0')
    )

    if has_opening:
        sync_project_financials(project)
        rebuild_project_ledger_balances(project)
        return

    legacy_advance = project.advance_received
    if payment_total == 0 and legacy_advance > 0:
        record_opening_advance(
            project,
            legacy_advance,
            actor=actor,
            notes='Opening advance (migrated from project setup)',
        )
        return

    sync_project_financials(project)
    rebuild_project_ledger_balances(project)


@transaction.atomic
def create_bill(
    *,
    project: Project,
    line_items: list[dict],
    bill_date: date | None = None,
    due_date: date | None = None,
    gst_percent: Decimal = Decimal('0'),
    description: str = '',
    issue: bool = True,
    send_email: bool = True,
    actor=None,
) -> Bill:
    bill_number = allocate_bill_number()
    bill = Bill.objects.create(
        bill_number=bill_number,
        kind=Bill.Kind.INVOICE,
        project=project,
        client=project.client,
        bill_date=bill_date or timezone.localdate(),
        due_date=due_date,
        gst_percent=gst_percent,
        description=description,
        status=Bill.Status.DRAFT,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    for i, row in enumerate(line_items):
        BillLineItem.objects.create(
            bill=bill,
            description=row['description'],
            quantity=row.get('quantity', Decimal('1')),
            unit_price=row['unit_price'],
            sort_order=i,
        )
    bill.recalculate_totals()
    bill.save()

    if issue:
        issue_bill(bill, actor=actor, send_email=send_email)
    return bill


@transaction.atomic
def issue_bill(bill: Bill, *, actor=None, send_email: bool = True) -> Bill:
    if bill.status == Bill.Status.CANCELLED:
        raise ValueError('Cannot issue a cancelled bill.')
    bill.status = Bill.Status.ISSUED
    bill.issued_at = timezone.now()
    bill.recalculate_totals()
    bill.save()

    # Contract already books deal value; tax invoice is documentation unless no contract row.
    if not LedgerEntry.objects.filter(
        project=bill.project,
        reference=contract_ledger_reference(bill.project),
    ).exists():
        _append_ledger_entry(
            project=bill.project,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=bill.total_amount,
            description=f'Bill issued — {bill.bill_number}',
            reference=bill.bill_number,
            bill=bill,
            entry_date=bill.bill_date,
            actor=actor,
        )

    pdf_bytes = render_bill_pdf(bill)
    bill.pdf_file.save(
        f'{bill.bill_number.replace("/", "-")}.pdf',
        ContentFile(pdf_bytes),
        save=True,
    )

    audit_service.log_event(
        category='billing',
        action='bill_issued',
        object_type='Bill',
        object_id=bill.pk,
        object_repr=bill.bill_number,
        actor=actor,
        project=bill.project,
        after_state={'total': str(bill.total_amount), 'status': bill.status},
    )

    if send_email:
        recipient = get_billing_email_for_project(bill.project)
        if recipient:
            send_bill_email(bill, recipient, pdf_bytes)

    return bill


def send_bill_email(bill: Bill, recipient: str, pdf_bytes: bytes | None = None) -> bool:
    if not recipient:
        return False
    if pdf_bytes is None:
        pdf_bytes = render_bill_pdf(bill)
    from_email = (
        getattr(settings, 'BILLING_FROM_EMAIL', '').strip()
        or getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    )
    if not from_email:
        logger.warning('No BILLING_FROM_EMAIL / DEFAULT_FROM_EMAIL — skip bill email')
        return False
    subject = f'Tax Invoice {bill.bill_number} — {COMPANY["legal_name"]}'
    body = (
        f'Dear {bill.client.contact_person or bill.client.business_name},\n\n'
        f'Please find attached our tax invoice {bill.bill_number} '
        f'dated {bill.bill_date:%d %b %Y} for {fmt_inr(bill.total_amount)}.\n\n'
        f'Balance due: {fmt_inr(bill.balance_due)}\n'
    )
    if bill.due_date:
        body += f'Payment due by: {bill.due_date:%d %b %Y}\n'
    body += (
        f'\nRegards,\n{COMPANY["legal_name"]}\n'
        f'{COMPANY["phone"]}\n'
    )
    try:
        msg = EmailMessage(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient],
        )
        msg.attach(
            f'{bill.bill_number.replace("/", "-")}.pdf',
            pdf_bytes,
            'application/pdf',
        )
        msg.send(fail_silently=False)
        bill.email_sent_at = timezone.now()
        bill.email_sent_to = recipient
        bill.save(update_fields=['email_sent_at', 'email_sent_to', 'updated_at'])
        return True
    except Exception:
        logger.exception('Bill email failed bill=%s to=%s', bill.bill_number, recipient)
        return False


@transaction.atomic
def record_payment(
    *,
    project: Project,
    amount: Decimal,
    proof_file,
    bill: Bill | None = None,
    transaction_id: str = '',
    payment_method: str = BillPayment.PaymentMethod.BANK_TRANSFER,
    payment_date: date | None = None,
    notes: str = '',
    auto_verify: bool = True,
    actor=None,
) -> BillPayment:
    payment = BillPayment.objects.create(
        bill=bill,
        project=project,
        amount=amount,
        transaction_id=transaction_id,
        payment_method=payment_method,
        payment_date=payment_date or timezone.localdate(),
        proof_file=proof_file,
        notes=notes,
        status=BillPayment.Status.PENDING,
        recorded_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    if auto_verify:
        verify_payment(payment, actor=actor)
    return payment


@transaction.atomic
def verify_payment(payment: BillPayment, *, actor=None) -> BillPayment:
    if payment.status == BillPayment.Status.VERIFIED:
        return payment
    payment.status = BillPayment.Status.VERIFIED
    payment.verified_by = actor if getattr(actor, 'is_authenticated', False) else None
    payment.verified_at = timezone.now()
    payment.save()

    ref = payment.transaction_id or f'PAY-{payment.pk}'
    _append_ledger_entry(
        project=payment.project,
        entry_type=LedgerEntry.EntryType.CREDIT,
        amount=payment.amount,
        description=f'Payment received — {ref}',
        reference=ref,
        bill=payment.bill,
        payment=payment,
        entry_date=payment.payment_date,
        actor=actor,
    )

    if payment.bill_id:
        bill = payment.bill
        bill.recalculate_totals()
        bill.save()

    sync_project_financials(payment.project)

    audit_service.log_event(
        category='billing',
        action='payment_verified',
        object_type='BillPayment',
        object_id=payment.pk,
        object_repr=ref,
        actor=actor,
        project=payment.project,
        after_state={'amount': str(payment.amount)},
    )
    return payment


@transaction.atomic
def record_payment_with_receipt(
    *,
    project: Project,
    amount: Decimal,
    description: str,
    proof_file,
    transaction_id: str = '',
    payment_method: str = BillPayment.PaymentMethod.BANK_TRANSFER,
    payment_date: date | None = None,
    notes: str = '',
    gst_percent: Decimal = Decimal('0'),
    send_email: bool = True,
    actor=None,
) -> tuple[BillPayment, Bill]:
    """
    Single action: payment proof + receipt bill + ledger credit + PDF + optional email.
    Each payment gets its own numbered receipt (default workflow).
    """
    pay_date = payment_date or timezone.localdate()
    desc = (description or 'Payment received').strip()
    txn = (transaction_id or '').strip()

    bill_number = allocate_bill_number()
    bill = Bill.objects.create(
        bill_number=bill_number,
        kind=Bill.Kind.RECEIPT,
        project=project,
        client=project.client,
        bill_date=pay_date,
        gst_percent=gst_percent or Decimal('0'),
        description=notes or '',
        status=Bill.Status.PAID,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
        issued_at=timezone.now(),
    )
    BillLineItem.objects.create(
        bill=bill,
        description=desc,
        quantity=Decimal('1'),
        unit_price=amount,
        sort_order=0,
    )
    bill.recalculate_totals()
    bill.amount_paid = bill.total_amount
    bill.balance_due = Decimal('0')
    bill.status = Bill.Status.PAID
    bill.save()

    payment = BillPayment.objects.create(
        bill=bill,
        project=project,
        amount=amount,
        transaction_id=txn,
        payment_method=payment_method,
        payment_date=pay_date,
        proof_file=proof_file,
        notes=notes,
        status=BillPayment.Status.VERIFIED,
        recorded_by=actor if getattr(actor, 'is_authenticated', False) else None,
        verified_by=actor if getattr(actor, 'is_authenticated', False) else None,
        verified_at=timezone.now(),
    )

    ensure_project_contract_ledger(project, actor=actor)

    ref = txn or f'PAY-{payment.pk}'
    _append_ledger_entry(
        project=project,
        entry_type=LedgerEntry.EntryType.CREDIT,
        amount=amount,
        description=f'Payment received — {desc}',
        reference=ref,
        bill=bill,
        payment=payment,
        entry_date=pay_date,
        actor=actor,
    )
    sync_project_financials(project)

    pdf_bytes = render_bill_pdf(bill, payment=payment)
    bill.pdf_file.save(
        f'{bill.bill_number.replace("/", "-")}.pdf',
        ContentFile(pdf_bytes),
        save=True,
    )

    audit_service.log_event(
        category='billing',
        action='payment_receipt_recorded',
        object_type='BillPayment',
        object_id=payment.pk,
        object_repr=f'{bill.bill_number} / {ref}',
        actor=actor,
        project=project,
        after_state={'amount': str(amount), 'bill': bill.bill_number},
    )

    if send_email:
        recipient = get_billing_email_for_project(project)
        if recipient:
            send_payment_receipt_email(bill, payment, recipient, pdf_bytes)

    return payment, bill


def send_payment_receipt_email(
    bill: Bill,
    payment: BillPayment,
    recipient: str,
    pdf_bytes: bytes | None = None,
) -> bool:
    if not recipient:
        return False
    if pdf_bytes is None:
        pdf_bytes = render_bill_pdf(bill, payment=payment)
    from_email = (
        getattr(settings, 'BILLING_FROM_EMAIL', '').strip()
        or getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    )
    if not from_email:
        logger.warning('No billing from-email configured')
        return False
    client = bill.client
    txn = payment.transaction_id or f'PAY-{payment.pk}'
    subject = f'Payment Receipt {bill.bill_number} — {COMPANY["legal_name"]}'
    body = (
        f'Dear {client.contact_person or client.business_name},\n\n'
        f'Thank you for your payment of {fmt_inr(payment.amount)} '
        f'received on {payment.payment_date:%d %b %Y}.\n'
        f'Transaction reference: {txn}\n\n'
        f'Please find your payment receipt {bill.bill_number} attached.\n\n'
        f'Project balance (deal): {fmt_inr(bill.project.deal_value)} · '
        f'Total received: {fmt_inr(bill.project.advance_received)} · '
        f'Balance due: {fmt_inr(bill.project.balance_due)}\n\n'
        f'Regards,\n{COMPANY["legal_name"]}\n{COMPANY["phone"]}\n'
    )
    try:
        msg = EmailMessage(subject=subject, body=body, from_email=from_email, to=[recipient])
        msg.attach(
            f'receipt-{bill.bill_number.replace("/", "-")}.pdf',
            pdf_bytes,
            'application/pdf',
        )
        msg.send(fail_silently=False)
        bill.email_sent_at = timezone.now()
        bill.email_sent_to = recipient
        bill.save(update_fields=['email_sent_at', 'email_sent_to', 'updated_at'])
        return True
    except Exception:
        logger.exception('Receipt email failed bill=%s', bill.bill_number)
        return False


def amount_in_words(amount: Decimal) -> str:
    """Indian numbering words for rupees (simplified, whole rupees)."""
    n = int(amount)
    if n == 0:
        return 'Zero Rupees Only'
    ones = [
        '', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine',
        'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen',
        'Seventeen', 'Eighteen', 'Nineteen',
    ]
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']

    def _chunk(num):
        if num < 20:
            return ones[num]
        if num < 100:
            return (tens[num // 10] + (' ' + ones[num % 10] if num % 10 else '')).strip()
        if num < 1000:
            return (
                ones[num // 100] + ' Hundred'
                + (' ' + _chunk(num % 100) if num % 100 else '')
            ).strip()
        return ''

    def _convert(num):
        if num >= 10000000:
            return (
                _convert(num // 10000000) + ' Crore '
                + _convert(num % 10000000)
            ).strip()
        if num >= 100000:
            return (
                _convert(num // 100000) + ' Lakh '
                + _convert(num % 100000)
            ).strip()
        if num >= 1000:
            return (
                _convert(num // 1000) + ' Thousand '
                + _convert(num % 1000)
            ).strip()
        return _chunk(num)

    words = _convert(n)
    paise = int((amount - n) * 100)
    if paise:
        return f'{words} Rupees and {paise} Paise Only'
    return f'{words} Rupees Only'


def fmt_inr(amount) -> str:
    """Indian rupee formatting safe for PDF fonts (no Unicode rupee glyph)."""
    if amount is None:
        return 'Rs. 0.00/-'
    return f'Rs. {Decimal(amount):,.2f}/-'


def _prepare_stamp_image(path: Path, max_width_pt: float = 72) -> io.BytesIO | None:
    """Light-on-dark stamp/signature/logo → black ink on transparent for white PDF."""
    from PIL import Image

    if not path.exists():
        return None
    img = Image.open(path).convert('RGBA')
    datas = img.getdata()
    new_data = []
    for r, g, b, a in datas:
        brightness = (r + g + b) / 3
        if brightness < 55:
            new_data.append((255, 255, 255, 0))
        else:
            ink = max(0, min(255, int((brightness - 55) * 2.5)))
            new_data.append((0, 0, 0, ink))
    img.putdata(new_data)
    w, h = img.size
    scale = max_width_pt / w
    new_h = int(h * scale)
    img = img.resize((int(w * scale), new_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf, new_h


def _pdf_hline(c, x1, x2, y, width=0.5, color=None):
    from reportlab.lib import colors

    c.setStrokeColor(color or colors.HexColor('#cccccc'))
    c.setLineWidth(width)
    c.line(x1, y, x2, y)


def _pdf_draw_box(c, x, y, w, h, fill=None, stroke=None, radius=0):
    from reportlab.lib import colors

    if fill:
        c.setFillColor(fill)
    if stroke:
        c.setStrokeColor(stroke)
        c.setLineWidth(0.6)
    if radius:
        c.roundRect(x, y, w, h, radius, fill=1 if fill else 0, stroke=1 if stroke else 0)
    else:
        c.rect(x, y, w, h, fill=1 if fill else 0, stroke=1 if stroke else 0)


def _pdf_draw_footer_block(c, *, page_w, margin, right_x, bill_date, black, gray, light_gray):
    """Fixed footer zone — seal left, signature stack right (no overlap)."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader

    footer_h = 52 * mm
    footer_bottom = 14 * mm
    footer_top = footer_bottom + footer_h
    mid_x = page_w / 2

    _pdf_hline(c, margin, right_x, footer_top + 4, width=1, color=colors.HexColor('#999999'))
    c.setStrokeColor(colors.HexColor('#cccccc'))
    c.setLineWidth(0.4)
    c.line(margin, footer_top + 2, right_x, footer_top + 2)

    # Left: company seal
    seal_result = _prepare_stamp_image(ASSETS['seal'], max_width_pt=70)
    if seal_result:
        seal_buf, seal_h = seal_result
        seal_w = min(70, 70)
        seal_y = footer_bottom + (footer_h - seal_h) / 2
        c.drawImage(
            ImageReader(seal_buf),
            margin + 4,
            seal_y,
            width=seal_w,
            height=seal_h,
            mask='auto',
            preserveAspectRatio=True,
        )

    # Right: signature column (top → bottom)
    sig_w = 100
    sig_x = right_x - sig_w
    line_h = 12
    ty = footer_top - 8

    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 10)
    c.drawRightString(right_x, ty, 'Thank You!')
    ty -= line_h + 2

    sig_result = _prepare_stamp_image(ASSETS['signature'], max_width_pt=sig_w)
    sig_block_h = 0
    if sig_result:
        sig_buf, sig_img_h = sig_result
        sig_draw_h = min(sig_img_h, 32)
        sig_y = ty - sig_draw_h
        c.drawImage(
            ImageReader(sig_buf),
            sig_x,
            sig_y,
            width=sig_w,
            height=sig_draw_h,
            mask='auto',
            preserveAspectRatio=True,
        )
        ty = sig_y - 4
        sig_block_h = sig_draw_h + 4

    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    c.line(sig_x, ty, right_x, ty)
    ty -= line_h

    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    c.drawRightString(right_x, ty, 'Authorised Signatory')
    ty -= line_h

    c.setFont('Helvetica-Bold', 8)
    c.setFillColor(black)
    c.drawRightString(right_x, ty, f'For {COMPANY["legal_name"]}')
    ty -= line_h

    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    c.drawRightString(right_x, ty, 'Received: Veena J V')
    ty -= line_h

    c.setFont('Helvetica', 8)
    c.drawRightString(right_x, ty, f'Date: {bill_date.strftime("%d-%m-%Y")}')

    c.setFont('Helvetica', 7)
    c.setFillColor(light_gray)
    c.drawCentredString(
        mid_x,
        footer_bottom - 6,
        f'Computer-generated document · {COMPANY["legal_name"]} · {COMPANY["reg_no"]}',
    )

    return footer_top + 8


def render_bill_pdf(bill: Bill, payment: BillPayment | None = None) -> bytes:
    """Premium black & white payment receipt / tax invoice PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    bill.recalculate_totals()
    client = bill.client
    project = bill.project
    is_receipt = bill.kind == Bill.Kind.RECEIPT
    if payment is None and is_receipt:
        payment = (
            bill.payments.filter(status=BillPayment.Status.VERIFIED)
            .order_by('-payment_date', '-pk')
            .first()
        )

    page_w, page_h = A4
    margin = 16 * mm
    right_x = page_w - margin
    content_w = page_w - 2 * margin
    row_h = 20

    black = colors.black
    white = colors.white
    gray = colors.HexColor('#444444')
    light_gray = colors.HexColor('#777777')
    rule = colors.HexColor('#bbbbbb')
    fill_soft = colors.HexColor('#f4f4f4')
    fill_header = colors.HexColor('#1a1a1a')

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(bill.bill_number)

    y = page_h - margin

    # Top accent bar
    c.setFillColor(black)
    c.rect(0, page_h - 3 * mm, page_w, 3 * mm, fill=1, stroke=0)

    # ── Header panel ──
    header_h = 52
    _pdf_draw_box(c, margin, y - header_h, content_w, header_h, fill=fill_soft, stroke=rule, radius=4)

    logo_sz = 14 * mm
    logo_x = margin + 8
    logo_y_center = y - header_h / 2
    text_x = logo_x + logo_sz + 10

    logo_result = _prepare_stamp_image(ASSETS['logo'], max_width_pt=logo_sz * 2.5)
    if logo_result:
        logo_buf, logo_h = logo_result
        logo_draw_h = min(logo_h, logo_sz)
        c.drawImage(
            ImageReader(logo_buf),
            logo_x,
            logo_y_center - logo_draw_h / 2,
            width=logo_sz,
            height=logo_draw_h,
            mask='auto',
            preserveAspectRatio=True,
        )
    else:
        text_x = margin + 10

    ty = y - 14
    c.setFont('Helvetica-Bold', 15)
    c.setFillColor(black)
    c.drawString(text_x, ty, COMPANY['legal_name'])
    ty -= 14
    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    for line in COMPANY['address_lines']:
        c.drawString(text_x, ty, line)
        ty -= 10
    c.drawString(text_x, ty, f'Pin {COMPANY["pin"]}  |  Ph {COMPANY["phone"]}  |  Reg {COMPANY["reg_no"]}')

    y -= header_h + 12

    # Document title band
    doc_title = 'PAYMENT RECEIPT' if is_receipt else 'TAX INVOICE'
    title_h = 26
    _pdf_draw_box(c, margin, y - title_h, content_w, title_h, fill=fill_header, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 12)
    c.drawCentredString(page_w / 2, y - title_h + 8, doc_title)
    y -= title_h + 10

    # Meta row (two cells)
    meta_h = 22
    half = content_w / 2 - 2
    _pdf_draw_box(c, margin, y - meta_h, half, meta_h, stroke=rule)
    _pdf_draw_box(c, margin + half + 4, y - meta_h, half, meta_h, stroke=rule)
    c.setFont('Helvetica', 7)
    c.setFillColor(light_gray)
    c.drawString(margin + 8, y - 8, 'BILL NUMBER')
    c.drawString(margin + half + 12, y - 8, 'DATE')
    c.setFont('Helvetica-Bold', 10)
    c.setFillColor(black)
    c.drawString(margin + 8, y - 18, bill.bill_number)
    c.drawString(margin + half + 12, y - 18, bill.bill_date.strftime('%d-%m-%Y'))
    y -= meta_h + 12

    # Prepared for box
    client_lines = []
    primary = (client.contact_person or client.business_name or '').strip()
    if primary:
        client_lines.append(('bold', primary))
    biz = (client.business_name or '').strip()
    if biz and biz.lower() != primary.lower():
        client_lines.append(('normal', biz))
    if client.address:
        for ln in client.address.strip().splitlines()[:3]:
            if ln.strip():
                client_lines.append(('normal', ln.strip()))
    if client.phone:
        client_lines.append(('normal', f'Phone: {client.phone}'))
    if client.email:
        client_lines.append(('normal', client.email))
    if client.gst_number:
        client_lines.append(('normal', f'GSTIN: {client.gst_number}'))

    prep_h = 16 + len(client_lines) * 11
    _pdf_draw_box(c, margin, y - prep_h, content_w, prep_h, fill=fill_soft, stroke=rule, radius=3)
    c.setFont('Helvetica-Bold', 7)
    c.setFillColor(light_gray)
    c.drawString(margin + 8, y - 10, 'PREPARED FOR')
    cy = y - 22
    for style, text in client_lines:
        c.setFont('Helvetica-Bold' if style == 'bold' else 'Helvetica', 9 if style == 'bold' else 8.5)
        c.setFillColor(black if style == 'bold' else gray)
        c.drawString(margin + 8, cy, text[:90])
        cy -= 11
    y -= prep_h + 12

    # ── Items table (full width, bordered) ──
    col_x = [
        margin,
        margin + content_w * 0.08,
        margin + content_w * 0.58,
        margin + content_w * 0.78,
    ]

    def _table_header():
        nonlocal y
        _pdf_draw_box(c, margin, y - row_h, content_w, row_h, fill=fill_header, stroke=0)
        c.setFillColor(white)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(col_x[1], y - 13, '#')
        c.drawString(col_x[1] + 14, y - 13, 'ITEM / DESCRIPTION')
        c.drawRightString(right_x - 8, y - 13, 'AMOUNT')
        c.drawRightString(col_x[3] - 6, y - 13, 'RATE')
        y -= row_h

    def _table_row(num, desc, rate, amount):
        nonlocal y
        c.setFillColor(white)
        c.setStrokeColor(rule)
        c.setLineWidth(0.4)
        c.rect(margin, y - row_h, content_w, row_h, fill=1, stroke=1)
        c.setFillColor(black)
        c.setFont('Helvetica', 8.5)
        c.drawString(col_x[1], y - 13, str(num))
        desc_short = desc if len(desc) <= 52 else desc[:49] + '...'
        c.drawString(col_x[1] + 14, y - 13, desc_short)
        c.drawRightString(col_x[3] - 6, y - 13, rate)
        c.drawRightString(right_x - 8, y - 13, amount)
        y -= row_h

    _table_header()
    for i, ln in enumerate(bill.line_items.all(), 1):
        _table_row(i, ln.description, fmt_inr(ln.unit_price), fmt_inr(ln.amount))

    y -= 6

    # ── Summary box (right side) ──
    summary_rows = []
    if is_receipt:
        summary_rows.append(('Amount Received', fmt_inr(bill.total_amount), True))
        if payment and payment.transaction_id:
            summary_rows.append(('Transaction ID', payment.transaction_id, False))
        if payment:
            summary_rows.append(('Payment Method', payment.get_payment_method_display(), False))
    else:
        summary_rows.append(('Subtotal', fmt_inr(bill.subtotal), False))
        if bill.gst_percent > 0:
            summary_rows.append((f'GST ({bill.gst_percent:g}%)', fmt_inr(bill.gst_amount), False))
        summary_rows.append(('Invoice Total', fmt_inr(bill.total_amount), True))

    if project.deal_value:
        summary_rows.append(('Project Total', fmt_inr(project.deal_value), False))
    summary_rows.append(('Total Received', fmt_inr(project.advance_received), False))
    if project.balance_due > 0:
        summary_rows.append(('Balance Due', fmt_inr(project.balance_due), True))

    sum_w = content_w * 0.48
    sum_x = right_x - sum_w
    sum_h = 12 + len(summary_rows) * 14 + 8
    _pdf_draw_box(c, sum_x, y - sum_h, sum_w, sum_h, stroke=rule, radius=3)

    sy = y - 14
    for label, value, bold in summary_rows:
        c.setFont('Helvetica-Bold' if bold else 'Helvetica', 9 if bold else 8.5)
        c.setFillColor(black)
        c.drawString(sum_x + 8, sy, label + ':')
        c.drawRightString(right_x - 8, sy, value)
        sy -= 14
    y -= sum_h + 10

    # Status + amount in words
    status_label = 'Closed' if is_receipt else bill.get_status_display()
    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(black)
    c.drawString(margin, y, f'Payment Status: {status_label}')
    y -= 14

    words_h = 22
    _pdf_draw_box(c, margin, y - words_h, content_w * 0.65, words_h, fill=fill_soft, stroke=rule, radius=2)
    c.setFont('Helvetica-Oblique', 8)
    c.setFillColor(gray)
    c.drawString(margin + 8, y - 14, f'Amount in words: {amount_in_words(bill.total_amount)}')
    y -= words_h + 8

    if bill.description:
        c.setFont('Helvetica', 8)
        c.setFillColor(gray)
        c.drawString(margin, y, f'Notes: {bill.description[:100]}')
        y -= 12

    # Fixed footer (always at page bottom)
    _pdf_draw_footer_block(
        c,
        page_w=page_w,
        margin=margin,
        right_x=right_x,
        bill_date=bill.bill_date,
        black=black,
        gray=gray,
        light_gray=light_gray,
    )

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def render_statement_pdf(
    project: Project,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> bytes:
    """Premium account statement PDF (aligned with receipt styling)."""
    reconcile_project_billing(project)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    client = project.client
    qs = LedgerEntry.objects.filter(project=project).select_related('bill', 'payment')
    if date_from:
        qs = qs.filter(entry_date__gte=date_from)
    if date_to:
        qs = qs.filter(entry_date__lte=date_to)
    entries = list(qs.order_by('entry_date', 'created_at', 'pk'))

    page_w, page_h = A4
    margin = 16 * mm
    right_x = page_w - margin
    content_w = page_w - 2 * margin
    row_h = 18

    black = colors.black
    white = colors.white
    gray = colors.HexColor('#444444')
    light_gray = colors.HexColor('#777777')
    rule = colors.HexColor('#bbbbbb')
    fill_soft = colors.HexColor('#f4f4f4')
    fill_header = colors.HexColor('#1a1a1a')

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = page_h - margin

    c.setFillColor(black)
    c.rect(0, page_h - 3 * mm, page_w, 3 * mm, fill=1, stroke=0)

    header_h = 48
    _pdf_draw_box(c, margin, y - header_h, content_w, header_h, fill=fill_soft, stroke=rule, radius=4)
    ty = y - 14
    c.setFont('Helvetica-Bold', 14)
    c.setFillColor(black)
    c.drawString(margin + 10, ty, COMPANY['legal_name'])
    ty -= 12
    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    c.drawString(margin + 10, ty, ' · '.join(COMPANY['address_lines']))
    ty -= 10
    c.drawString(margin + 10, ty, f'Pin {COMPANY["pin"]}  |  Ph {COMPANY["phone"]}  |  Reg {COMPANY["reg_no"]}')
    y -= header_h + 10

    title_h = 24
    _pdf_draw_box(c, margin, y - title_h, content_w, title_h, fill=fill_header, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 11)
    c.drawCentredString(page_w / 2, y - title_h + 8, 'ACCOUNT STATEMENT')
    y -= title_h + 10

    period_end = date_to or timezone.localdate()
    period_start = date_from or 'Start'
    pkg_name = project.package.name if project.package_id else '—'
    meta_h = 56
    _pdf_draw_box(c, margin, y - meta_h, content_w, meta_h, fill=fill_soft, stroke=rule, radius=3)
    c.setFont('Helvetica-Bold', 7)
    c.setFillColor(light_gray)
    c.drawString(margin + 8, y - 10, 'PROJECT ACCOUNT STATEMENT')
    c.setFont('Helvetica-Bold', 10)
    c.setFillColor(black)
    c.drawString(margin + 8, y - 22, client.business_name)
    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    c.drawString(margin + 8, y - 32, f'Contact: {client.contact_person or "—"}')
    c.drawString(margin + 8, y - 42, f'Project #{project.pk}  |  Package: {pkg_name}')
    c.drawString(
        margin + 8,
        y - 52,
        f'Period: {period_start} to {period_end}',
    )
    y -= meta_h + 12

    # Column positions
    col_date = margin + 4
    col_ref = margin + content_w * 0.14
    col_desc = margin + content_w * 0.32
    col_debit = margin + content_w * 0.68
    col_credit = margin + content_w * 0.82
    col_bal = right_x - 4

    _pdf_draw_box(c, margin, y - row_h, content_w, row_h, fill=fill_header, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(col_date, y - 12, 'DATE')
    c.drawString(col_ref, y - 12, 'REFERENCE')
    c.drawString(col_desc, y - 12, 'DESCRIPTION')
    c.drawRightString(col_debit, y - 12, 'DEBIT')
    c.drawRightString(col_credit, y - 12, 'CREDIT')
    c.drawRightString(col_bal, y - 12, 'BALANCE DUE')
    y -= row_h

    c.setFont('Helvetica', 8)
    for e in entries:
        c.setFillColor(white)
        c.setStrokeColor(rule)
        c.rect(margin, y - row_h, content_w, row_h, fill=1, stroke=1)
        c.setFillColor(black)
        c.drawString(col_date, y - 12, e.entry_date.strftime('%d-%b-%Y'))
        ref = (e.reference or '—')[:14]
        c.drawString(col_ref, y - 12, ref)
        desc = e.description[:42] + ('...' if len(e.description) > 42 else '')
        c.drawString(col_desc, y - 12, desc)
        if e.entry_type == LedgerEntry.EntryType.DEBIT:
            c.drawRightString(col_debit, y - 12, fmt_inr(e.amount))
        else:
            c.drawRightString(col_credit, y - 12, fmt_inr(e.amount))
        c.drawRightString(col_bal, y - 12, fmt_inr(e.balance_after))
        y -= row_h

    if not entries:
        c.rect(margin, y - row_h, content_w, row_h, fill=1, stroke=1)
        c.drawCentredString(page_w / 2, y - 12, 'No transactions in this period')
        y -= row_h

    y -= 8
    closing = _project_ledger_balance(project)
    sum_h = 28
    sum_w = content_w * 0.45
    sum_x = right_x - sum_w
    _pdf_draw_box(c, sum_x, y - sum_h, sum_w, sum_h, stroke=rule, radius=3)
    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(black)
    c.drawString(sum_x + 8, y - 14, 'Closing balance (receivable):')
    c.drawRightString(right_x - 8, y - 14, fmt_inr(closing))
    c.setFont('Helvetica', 8)
    c.setFillColor(gray)
    c.drawString(sum_x + 8, y - 26, f'Deal value: {fmt_inr(project.deal_value)}')
    c.drawRightString(right_x - 8, y - 26, f'Received: {fmt_inr(project.advance_received)}')

    c.setFont('Helvetica', 7)
    c.setFillColor(light_gray)
    c.drawCentredString(
        page_w / 2,
        12 * mm,
        f'Generated {timezone.localdate():%d-%m-%Y} · {COMPANY["legal_name"]}',
    )

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def get_project_billing_snapshot(project: Project, *, actor=None) -> dict:
    """Unified project-scoped billing figures for UI."""
    project.refresh_from_db()
    reconcile_project_billing(project, actor=actor)
    project.refresh_from_db()
    ledger_balance = _project_ledger_balance(project)
    payments_count = BillPayment.objects.filter(
        project=project,
        status=BillPayment.Status.VERIFIED,
    ).count()
    receipts_count = Bill.objects.filter(
        project=project,
        kind=Bill.Kind.RECEIPT,
    ).exclude(status=Bill.Status.CANCELLED).count()
    return {
        'deal_value': project.deal_value,
        'advance_received': project.advance_received,
        'balance_due': project.balance_due,
        'ledger_balance': ledger_balance,
        'ledger_matches_contract': ledger_balance == project.balance_due,
        'payments_count': payments_count,
        'receipts_count': receipts_count,
        'billing_email': get_billing_email_for_project(project),
        'billing_email_source': get_billing_email_source_for_project(project),
    }


def statement_filename(project: Project, *, date_from=None, date_to=None) -> str:
    """Safe PDF filename for a project statement."""
    client_slug = ''.join(
        c if c.isalnum() else '_' for c in (project.client.business_name or 'client')[:24]
    )
    parts = [f'statement-project-{project.pk}', client_slug]
    if date_from:
        parts.append(date_from.strftime('%Y%m%d'))
    if date_to:
        parts.append(date_to.strftime('%Y%m%d'))
    return '-'.join(parts) + '.pdf'


def get_billing_summary():
    """Dashboard aggregates for admin billing home."""
    projects = (
        Project.objects.select_related('client', 'package')
        .order_by('-updated_at')[:200]
    )
    total_billed = Bill.objects.exclude(status=Bill.Status.CANCELLED).aggregate(
        t=Sum('total_amount')
    )['t'] or Decimal('0')
    total_outstanding = Bill.objects.filter(
        status__in=[Bill.Status.ISSUED, Bill.Status.PARTIALLY_PAID],
    ).aggregate(t=Sum('balance_due'))['t'] or Decimal('0')
    pending_payments = BillPayment.objects.filter(
        status=BillPayment.Status.PENDING,
    ).count()
    recent_bills = (
        Bill.objects.select_related('project', 'client')
        .order_by('-created_at')[:12]
    )
    return {
        'projects': projects,
        'total_billed': total_billed,
        'total_outstanding': total_outstanding,
        'pending_payments': pending_payments,
        'recent_bills': recent_bills,
    }
