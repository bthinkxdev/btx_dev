"""
Server-side PDF documents (Part 5.9, 7.1). One renderer for quotations, invoices,
credit notes, receipts, statements and payslips so every document carries the
same company block, tax breakup and wording rules:

* A quotation is labelled Quotation, never Tax invoice.
* Internal notes, margins and cost fields are never printed.
* Drafts carry a DRAFT watermark.
"""
from __future__ import annotations

import hashlib
import io
from decimal import Decimal

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

from .india import state_name
from .money import amount_in_words, format_inr

BRAND = colors.HexColor('#465fff')
GREY = colors.HexColor('#667085')
LINE = colors.HexColor('#e4e7ec')
DARK = colors.HexColor('#101828')

_styles = getSampleStyleSheet()
S = {
    'title': ParagraphStyle('t', parent=_styles['Title'], fontName='Helvetica-Bold', fontSize=18, textColor=DARK, alignment=TA_RIGHT, spaceAfter=2),
    'h': ParagraphStyle('h', parent=_styles['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=DARK, spaceAfter=3),
    'n': ParagraphStyle('n', parent=_styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=11.5, textColor=DARK),
    'small': ParagraphStyle('s', parent=_styles['Normal'], fontName='Helvetica', fontSize=7.5, leading=10, textColor=GREY),
    'r': ParagraphStyle('r', parent=_styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=11.5, alignment=TA_RIGHT, textColor=DARK),
    'rb': ParagraphStyle('rb', parent=_styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=12, alignment=TA_RIGHT, textColor=DARK),
}


def _p(text, style='n'):
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(text or '')).replace('\n', '<br/>'), S[style])


def _money(v):
    return format_inr(v).replace('₹', 'Rs. ')


def _d(v):
    return v.strftime('%d-%b-%Y') if v else '—'


class _Watermark:
    def __init__(self, text, footer):
        self.text, self.footer = text, footer

    def __call__(self, canvas, doc):
        canvas.saveState()
        if self.text:
            canvas.setFont('Helvetica-Bold', 80)
            canvas.setFillColor(colors.Color(0.9, 0.1, 0.1, alpha=0.08))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, self.text)
            canvas.restoreState()
            canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(GREY)
        canvas.drawString(15 * mm, 10 * mm, self.footer)
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f'Page {doc.page}')
        canvas.restoreState()


def _company_block(company):
    lines = [f'<b>{company.legal_name}</b>', company.address_text]
    if company.gstin:
        lines.append(f'GSTIN: {company.gstin}')
    if company.pan:
        lines.append(f'PAN: {company.pan}')
    from .india import display_phone
    lines.append(f'{display_phone(company.phone)} · {company.email}')
    return Paragraph('<br/>'.join(lines), S['n'])


def _logo():
    path = settings.BASE_DIR / 'new' / 'assets' / 'images' / 'logo.jpg'
    if path.exists():
        img = Image(str(path), width=16 * mm, height=19 * mm)
        return img
    return Spacer(1, 1)


def _header(company, title, meta_rows):
    meta = Table([[_p(k, 'small'), _p(v, 'r')] for k, v in meta_rows], colWidths=[28 * mm, 42 * mm])
    meta.setStyle(TableStyle([('BOTTOMPADDING', (0, 0), (-1, -1), 1), ('TOPPADDING', (0, 0), (-1, -1), 1)]))
    right = [Paragraph(title, S['title']), meta]
    t = Table([[_logo(), _company_block(company), right]], colWidths=[20 * mm, 90 * mm, 70 * mm])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    return t


def _party(label, name, address, gstin='', state='', extra=''):
    lines = [f'<font color="#667085">{label}</font>', f'<b>{name}</b>']
    if address:
        lines.append(address.replace('\n', '<br/>'))
    if state:
        lines.append(f'State: {state_name(state)} ({state})')
    if gstin:
        lines.append(f'GSTIN: {gstin}')
    if extra:
        lines.append(extra)
    return Paragraph('<br/>'.join(lines), S['n'])


def _lines_table(doc, lines):
    intra = doc.supply_type == 'intra'
    head = ['#', 'Description', 'SAC/HSN', 'Qty', 'Rate', 'Disc.', 'Taxable', 'GST %', 'Tax', 'Amount']
    rows = [head]
    for i, ln in enumerate(lines, start=1):
        tax = ln.cgst + ln.sgst + ln.igst
        rows.append([str(i), _p(ln.description), ln.sac_code or '', f'{ln.quantity.normalize():f} {ln.unit}',
                     _money(ln.unit_price), _money(ln.discount_amount) if ln.discount_amount else '—',
                     _money(ln.taxable_value), f'{ln.tax_rate.normalize():f}%' if ln.taxability == 'taxable' else ln.get_taxability_display(),
                     _money(tax), _money(ln.total)])
    t = Table(rows, colWidths=[6 * mm, 42 * mm, 14 * mm, 14 * mm, 19 * mm, 13 * mm, 20 * mm, 11 * mm, 19 * mm, 22 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7.5), ('FONT', (0, 1), (-1, -1), 'Helvetica', 7.5),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f4f7')), ('TEXTCOLOR', (0, 0), (-1, -1), DARK),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, LINE), ('BOTTOMPADDING', (0, 0), (-1, -1), 4), ('TOPPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def _totals(doc, extra_rows=()):
    rows = [('Subtotal', doc.subtotal)]
    if doc.discount_total:
        rows.append(('Discount', -doc.discount_total))
    rows.append(('Taxable value', doc.taxable_total))
    if doc.supply_type == 'intra':
        rows += [('CGST', doc.cgst_total), ('SGST', doc.sgst_total)]
    elif doc.igst_total:
        rows.append(('IGST', doc.igst_total))
    if doc.round_off:
        rows.append(('Round-off', doc.round_off))
    rows.append(('Total', doc.grand_total))
    rows += list(extra_rows)
    data = [[_p(k, 'r'), _p(_money(v), 'rb' if k in ('Total', 'Amount due', 'Balance due') else 'r')] for k, v in rows]
    t = Table(data, colWidths=[40 * mm, 35 * mm])
    t.setStyle(TableStyle([('LINEABOVE', (0, len(rows) - 1 - len(extra_rows)), (-1, len(rows) - 1 - len(extra_rows)), 0.8, DARK),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 2), ('TOPPADDING', (0, 0), (-1, -1), 2)]))
    return t


def _rate_summary(doc):
    snap = doc.tax_snapshot or {}
    summary = snap.get('rate_summary') or []
    if not summary:
        return None
    intra = doc.supply_type == 'intra'
    head = ['Taxability', 'Rate', 'Taxable', 'CGST', 'SGST'] if intra else ['Taxability', 'Rate', 'Taxable', 'IGST']
    rows = [head]
    for b in summary:
        base = [b['taxability'].replace('_', ' ').title(), f"{Decimal(b['rate']).normalize():f}%", _money(b['taxable'])]
        rows.append(base + ([_money(b['cgst']), _money(b['sgst'])] if intra else [_money(b['igst'])]))
    t = Table(rows, colWidths=[24 * mm, 14 * mm, 24 * mm] + ([20 * mm, 20 * mm] if intra else [24 * mm]))
    t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 7), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7),
                           ('ALIGN', (1, 0), (-1, -1), 'RIGHT'), ('LINEBELOW', (0, 0), (-1, -1), 0.3, LINE)]))
    return t


def _bank_block(company):
    if not company.bank_account_number:
        return None
    return _p(f'Bank: {company.bank_name}\nAccount name: {company.bank_account_name or company.legal_name}\n'
              f'Account no: {company.bank_account_number}   IFSC: {company.bank_ifsc}\n'
              + (f'UPI: {company.upi_id}' if company.upi_id else ''), 'n')


def _signature(company):
    return _p(f'For {company.legal_name}\n\n\n{company.signatory_name}\n{company.signatory_designation}', 'r')


def _build(story, watermark, footer):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=18 * mm)
    deco = _Watermark(watermark, footer)
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- documents

def quotation_pdf(q) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    draft = q.status in ('draft', 'awaiting_approval') or not q.number
    meta = [('Quotation no.', q.display_number), ('Date', _d(q.quotation_date)), ('Valid until', _d(q.valid_until))]
    if q.reference:
        meta.append(('Reference', q.reference))
    story = [_header(company, 'QUOTATION', meta), Spacer(1, 6 * mm)]
    story.append(Table([[_party('Quotation for', q.customer_display, q.billing_address, q.customer_gstin, q.customer_state,
                                f'Attn: {q.contact_name}' if q.contact_name else ''),
                         _p(f'Place of supply: {state_name(q.place_of_supply) or "—"}\nPrices: {q.get_price_mode_display()}', 'n')]],
                       colWidths=[110 * mm, 70 * mm], style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    if q.title:
        story += [Spacer(1, 4 * mm), _p(q.title, 'h')]
    story += [Spacer(1, 4 * mm), _lines_table(q, list(q.lines.all())), Spacer(1, 3 * mm)]
    extra = []
    if q.advance_amount:
        extra = [('Advance', q.advance_amount), ('Balance', q.grand_total - q.advance_amount)]
    rs = _rate_summary(q)
    story.append(Table([[rs or Spacer(1, 1), _totals(q, extra)]], colWidths=[105 * mm, 75 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 2 * mm), _p('Amount in words: ' + amount_in_words(q.grand_total), 'small'), Spacer(1, 4 * mm)]
    milestones = list(q.milestones.all())
    if milestones:
        rows = [['Payment milestone', '%', 'Amount', 'When']]
        for m in milestones:
            rows.append([m.name, f'{m.percent.normalize():f}%', _money(q.grand_total * m.percent / 100), f'{m.get_trigger_display()} {m.due_rule}'])
        t = Table(rows, colWidths=[60 * mm, 15 * mm, 30 * mm, 75 * mm])
        t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 7.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 7.5),
                               ('LINEBELOW', (0, 0), (-1, -1), 0.3, LINE)]))
        story += [t, Spacer(1, 4 * mm)]
    for label, value in (('Deliverables', q.deliverables), ('Exclusions', q.exclusions), ('Assumptions', q.assumptions),
                         ('Timeline', q.timeline), ('Revisions', q.revision_limits), ('Balance terms', q.balance_terms),
                         ('Late payment', q.late_fee_text), ('Terms and conditions', q.terms)):
        if value:
            story.append(KeepTogether([_p(label, 'h'), _p(value), Spacer(1, 3 * mm)]))
    bank = _bank_block(company)
    story.append(Table([[bank or Spacer(1, 1), _signature(company)]], colWidths=[110 * mm, 70 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 4 * mm), _p('This is a quotation, not a tax invoice.', 'small')]
    return _build(story, 'DRAFT' if draft else '', f'{company.legal_name} · Quotation {q.display_number}')


def invoice_pdf(inv) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    draft = inv.status in ('draft', 'pending_approval') or not inv.number
    title = 'TAX INVOICE' if company.gst_registered else 'BILL OF SUPPLY'
    meta = [('Invoice no.', inv.display_number), ('Invoice date', _d(inv.invoice_date)), ('Due date', _d(inv.due_date))]
    if inv.po_number:
        meta.append(('PO number', inv.po_number))
    if inv.quotation_id:
        meta.append(('Quotation', inv.quotation.display_number))
    story = [_header(company, title, meta), Spacer(1, 6 * mm)]
    story.append(Table([[_party('Bill to', inv.customer_name or str(inv.client), inv.billing_address, inv.customer_gstin, inv.customer_state),
                         _party('Ship to', inv.customer_name or str(inv.client), inv.shipping_address or inv.billing_address),
                         _p(f'Place of supply: {state_name(inv.place_of_supply)} ({inv.place_of_supply})\n'
                            f'Reverse charge: No', 'n')]],
                       colWidths=[70 * mm, 60 * mm, 50 * mm], style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 4 * mm), _lines_table(inv, list(inv.lines.all())), Spacer(1, 3 * mm)]
    extra = []
    if inv.advance_adjusted:
        extra.append(('Advance adjusted', -inv.advance_adjusted))
    if inv.tds_expected:
        extra.append(('TDS expected', -inv.tds_expected))
    if extra or inv.status != 'draft':
        extra.append(('Amount due', inv.balance if inv.status != 'draft' else inv.grand_total - inv.advance_adjusted - inv.tds_expected))
    rs = _rate_summary(inv)
    story.append(Table([[rs or Spacer(1, 1), _totals(inv, extra)]], colWidths=[105 * mm, 75 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 2 * mm), _p('Amount in words: ' + amount_in_words(inv.grand_total), 'small'), Spacer(1, 4 * mm)]
    if inv.notes:
        story += [_p('Notes', 'h'), _p(inv.notes), Spacer(1, 3 * mm)]
    if inv.terms:
        story += [_p('Terms', 'h'), _p(inv.terms), Spacer(1, 3 * mm)]
    bank = _bank_block(company)
    story.append(Table([[bank or Spacer(1, 1), _signature(company)]], colWidths=[110 * mm, 70 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    if inv.irn:
        story += [_p(f'IRN: {inv.irn}', 'small')]
    return _build(story, 'DRAFT' if draft else ('CANCELLED' if inv.status == 'cancelled' else ''),
                  f'{company.legal_name} · {title.title()} {inv.display_number}')


def credit_note_pdf(cn) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    inv = cn.invoice
    story = [_header(company, 'CREDIT NOTE', [('Credit note no.', cn.number or f'Draft #{cn.pk}'), ('Date', _d(cn.note_date)),
                                              ('Against invoice', inv.display_number), ('Invoice date', _d(inv.invoice_date))]),
             Spacer(1, 6 * mm),
             _party('Issued to', inv.customer_name or str(inv.client), inv.billing_address, inv.customer_gstin, inv.customer_state),
             Spacer(1, 4 * mm), _p(f'Reason: {cn.get_reason_display()}. {cn.notes}'), Spacer(1, 4 * mm)]
    rows = [('Taxable value', cn.taxable_value)]
    if cn.cgst or cn.sgst:
        rows += [('CGST', cn.cgst), ('SGST', cn.sgst)]
    if cn.igst:
        rows.append(('IGST', cn.igst))
    rows.append(('Total credit', cn.total))
    t = Table([[_p(k, 'r'), _p(_money(v), 'rb' if k == 'Total credit' else 'r')] for k, v in rows], colWidths=[40 * mm, 35 * mm], hAlign='RIGHT')
    story += [t, Spacer(1, 2 * mm), _p('Amount in words: ' + amount_in_words(cn.total), 'small'), Spacer(1, 10 * mm), _signature(company)]
    return _build(story, '' if cn.status == 'issued' else 'DRAFT', f'{company.legal_name} · Credit note {cn.number}')


def receipt_pdf(payment) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    voucher = getattr(payment, 'receipt_voucher', None)
    title = 'ADVANCE RECEIPT VOUCHER' if voucher and not payment.allocations.filter(reversed_at__isnull=True).exists() else 'PAYMENT RECEIPT'
    number = voucher.number if title.startswith('ADVANCE') else payment.receipt_number
    story = [_header(company, title, [('Receipt no.', number), ('Date', _d(payment.payment_date)),
                                      ('Method', payment.get_method_display())]), Spacer(1, 6 * mm),
             _party('Received from', str(payment.client), payment.client.billing_address, payment.client.gstin,
                    payment.client.billing_state), Spacer(1, 4 * mm)]
    rows = [['Against', 'Amount']]
    for a in payment.allocations.filter(reversed_at__isnull=True).select_related('invoice'):
        rows.append([f'Invoice {a.invoice.display_number}', _money(a.total)])
    if payment.unallocated > 0:
        rows.append(['Advance held on account', _money(payment.unallocated)])
    t = Table(rows, colWidths=[120 * mm, 40 * mm])
    t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 8.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5),
                           ('ALIGN', (1, 0), (1, -1), 'RIGHT'), ('LINEBELOW', (0, 0), (-1, -1), 0.3, LINE)]))
    story += [t, Spacer(1, 4 * mm)]
    totals = [('Amount received', payment.amount)]
    if payment.tds_amount:
        totals.append((f'TDS deducted ({payment.tds_section})', payment.tds_amount))
        totals.append(('Total settled', payment.gross_settled))
    if voucher and voucher.cgst + voucher.sgst + voucher.igst:
        totals += [('of which taxable value', voucher.taxable_value), ('of which GST', voucher.cgst + voucher.sgst + voucher.igst)]
    story.append(Table([[_p(k, 'r'), _p(_money(v), 'rb')] for k, v in totals], colWidths=[50 * mm, 35 * mm], hAlign='RIGHT'))
    story += [Spacer(1, 2 * mm), _p('Amount in words: ' + amount_in_words(payment.amount), 'small'),
              Spacer(1, 3 * mm), _p(f'Reference: {payment.reference_number or payment.provider_payment_id or "—"}', 'small'),
              Spacer(1, 10 * mm), _signature(company)]
    return _build(story, '', f'{company.legal_name} · Receipt {number}')


def statement_pdf(client, rows, closing, start, end) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    story = [_header(company, 'STATEMENT OF ACCOUNT', [('From', _d(start)), ('To', _d(end)), ('Closing balance', _money(closing))]),
             Spacer(1, 6 * mm), _party('Statement for', str(client), client.billing_address, client.gstin, client.billing_state),
             Spacer(1, 5 * mm)]
    data = [['Date', 'Document', 'Debit', 'Credit', 'Balance']]
    for r in rows:
        data.append([_d(r['date']), _p(r['label']), _money(r['debit']) if r['debit'] else '', _money(r['credit']) if r['credit'] else '',
                     _money(r['balance'])])
    t = Table(data, colWidths=[22 * mm, 78 * mm, 26 * mm, 26 * mm, 28 * mm], repeatRows=1)
    t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 8), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8),
                           ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f4f7')), ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
                           ('LINEBELOW', (0, 0), (-1, -1), 0.3, LINE), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [t, Spacer(1, 4 * mm), _p(f'Closing balance due: {_money(closing)}', 'rb')]
    return _build(story, '', f'{company.legal_name} · Statement {client}')


def payslip_pdf(slip) -> bytes:
    from crm.models import CompanySettings
    company = CompanySettings.get()
    emp = slip.employee
    story = [_header(company, 'PAYSLIP', [('Month', slip.run.period.strftime('%B %Y')), ('Employee', emp.code)]),
             Spacer(1, 6 * mm),
             _p(f'{emp.name} · {emp.designation} · {emp.department or ""}', 'h'),
             _p(f'Working days {slip.working_days} · Paid days {slip.paid_days} · Loss of pay {slip.lop_days}', 'small'),
             Spacer(1, 4 * mm)]
    earn = [l for l in slip.lines if l['kind'] == 'earning']
    ded = [l for l in slip.lines if l['kind'] == 'deduction']
    n = max(len(earn), len(ded))
    data = [['Earnings', 'Amount', 'Deductions', 'Amount']]
    for i in range(n):
        e = earn[i] if i < len(earn) else None
        d_ = ded[i] if i < len(ded) else None
        data.append([e['name'] if e else '', _money(e['amount']) if e else '', d_['name'] if d_ else '', _money(d_['amount']) if d_ else ''])
    data.append(['Gross', _money(slip.gross), 'Total deductions', _money(slip.deductions)])
    t = Table(data, colWidths=[55 * mm, 35 * mm, 55 * mm, 35 * mm])
    t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 8.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5),
                           ('FONT', (0, -1), (-1, -1), 'Helvetica-Bold', 8.5), ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                           ('ALIGN', (3, 0), (3, -1), 'RIGHT'), ('LINEBELOW', (0, 0), (-1, -1), 0.3, LINE)]))
    story += [t, Spacer(1, 5 * mm), _p(f'Net pay: {_money(slip.net)}', 'rb'), _p(amount_in_words(slip.net), 'small'),
              Spacer(1, 8 * mm), _p('This is a system-generated payslip.', 'small')]
    return _build(story, '', f'{company.legal_name} · Payslip {emp.code} {slip.run.period:%b-%Y}')
