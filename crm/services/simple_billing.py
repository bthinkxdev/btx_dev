"""
Simple billing: create a Bill (optionally against a Lead, or with no lead at
all), attach line items, record a payment, and render a PDF receipt.

No ledger, no accounts, no project/client management, no email, no manual
payment verification — a payment is verified the moment it's recorded.
Partial payment is supported (received amount may be less than the total);
`Bill.recalculate_totals()` already computes `balance_due` and the correct
status (PAID / PARTIALLY_PAID) from verified BillPayment rows, so it's
reused as-is.

The PDF renderer (`render_bill_pdf`) and bill-numbering (`allocate_bill_number`)
below are carried over from the old crm/services/billing.py, trimmed to not
require a Project — they draw an identical-looking receipt.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from ..billing_constants import ASSETS, COMPANY
from ..models import Bill, BillLineItem, BillPayment, BillSequence, Lead


def fiscal_year_suffix(d: date | None = None) -> str:
    """Indian FY suffix e.g. 2627 for FY 2026-27."""
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


def create_quick_bill(
    *,
    created_by,
    lead: Lead | None,
    line_items: list[dict],
    received_amount: Decimal,
    payment_method: str,
    notes: str = '',
    transaction_id: str = '',
) -> Bill:
    """
    line_items: [{'description': str, 'quantity': Decimal, 'unit_price': Decimal}, ...]
    Creates the Bill + BillLineItems + one already-verified BillPayment,
    computes totals/balance, and attaches the rendered PDF receipt.
    """
    with transaction.atomic():
        bill = Bill.objects.create(
            bill_number=allocate_bill_number(),
            kind=Bill.Kind.RECEIPT,
            lead=lead,
            description=notes,
            status=Bill.Status.ISSUED,
            issued_at=timezone.now(),
            created_by=created_by,
        )
        for i, item in enumerate(line_items):
            BillLineItem.objects.create(
                bill=bill,
                description=item['description'][:500],
                quantity=item['quantity'],
                unit_price=item['unit_price'],
                sort_order=i,
            )
        bill.recalculate_totals()
        bill.save()

        if received_amount and received_amount > 0:
            BillPayment.objects.create(
                bill=bill,
                amount=received_amount,
                payment_method=payment_method,
                transaction_id=transaction_id,
                status=BillPayment.Status.VERIFIED,
                recorded_by=created_by,
                verified_by=created_by,
                verified_at=timezone.now(),
            )
            bill.recalculate_totals()

    pdf_bytes = render_bill_pdf(bill)
    from django.core.files.base import ContentFile

    bill.pdf_file.save(f'{bill.bill_number.replace("/", "-")}.pdf', ContentFile(pdf_bytes), save=True)
    return bill


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


def _prepare_stamp_image(path: Path, max_width_pt: float = 72):
    """Light-on-dark stamp/signature/logo -> black ink on transparent for white PDF."""
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
    """Fixed footer zone - seal left, signature stack right (no overlap)."""
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

    sig_w = 100
    sig_x = right_x - sig_w
    line_h = 12
    ty = footer_top - 8

    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 10)
    c.drawRightString(right_x, ty, 'Thank You!')
    ty -= line_h + 2

    sig_result = _prepare_stamp_image(ASSETS['signature'], max_width_pt=sig_w)
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
    c.drawRightString(right_x, ty, f'Date: {bill_date.strftime("%d-%m-%Y")}')

    c.setFont('Helvetica', 7)
    c.setFillColor(light_gray)
    c.drawCentredString(
        mid_x,
        footer_bottom - 6,
        f'Computer-generated document · {COMPANY["legal_name"]} · {COMPANY["reg_no"]}',
    )

    return footer_top + 8


def render_bill_pdf(bill: Bill) -> bytes:
    """Premium black & white payment receipt PDF — no Project required."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    bill.recalculate_totals()
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

    c.setFillColor(black)
    c.rect(0, page_h - 3 * mm, page_w, 3 * mm, fill=1, stroke=0)

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

    doc_title = 'PAYMENT RECEIPT'
    title_h = 26
    _pdf_draw_box(c, margin, y - title_h, content_w, title_h, fill=fill_header, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 12)
    c.drawCentredString(page_w / 2, y - title_h + 8, doc_title)
    y -= title_h + 10

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

    # Prepared for box — client, or lead, or a plain walk-in label.
    client_lines = []
    client = bill.client
    lead = bill.lead
    if client is not None:
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
    elif lead is not None:
        client_lines.append(('bold', lead.name))
        if lead.phone:
            client_lines.append(('normal', f'Phone: {lead.phone}'))
        if lead.email:
            client_lines.append(('normal', lead.email))
    else:
        client_lines.append(('bold', 'Walk-in Customer'))

    prep_h = 16 + len(client_lines) * 11
    _pdf_draw_box(c, margin, y - prep_h, content_w, prep_h, fill=fill_soft, stroke=rule, radius=3)
    c.setFont('Helvetica-Bold', 7)
    c.setFillColor(light_gray)
    c.drawString(margin + 8, y - 10, 'RECEIVED FROM')
    cy = y - 22
    for style, text in client_lines:
        c.setFont('Helvetica-Bold' if style == 'bold' else 'Helvetica', 9 if style == 'bold' else 8.5)
        c.setFillColor(black if style == 'bold' else gray)
        c.drawString(margin + 8, cy, text[:90])
        cy -= 11
    y -= prep_h + 12

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

    summary_rows = [('Bill Total', fmt_inr(bill.total_amount), True)]
    if payment and payment.transaction_id:
        summary_rows.append(('Transaction ID', payment.transaction_id, False))
    if payment:
        summary_rows.append(('Payment Method', payment.get_payment_method_display(), False))
    summary_rows.append(('Amount Received', fmt_inr(bill.amount_paid), False))
    if bill.balance_due > 0:
        summary_rows.append(('Balance Due', fmt_inr(bill.balance_due), True))

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

    status_label = bill.get_status_display()
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
