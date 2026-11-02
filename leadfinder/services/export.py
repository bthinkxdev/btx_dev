"""Excel and PDF exports of Lead Finder results. Standalone — uses openpyxl and reportlab directly."""
from __future__ import annotations

import io

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COLUMNS = (
    # header, accessor, excel width, pdf: include?
    ('Business', lambda b: b.name, 32, True),
    ('Category', lambda b: b.category, 16, True),
    ('Location searched', lambda b: b.search_location, 18, False),
    ('Phone', lambda b: b.phone, 17, True),
    ('Address', lambda b: b.address, 40, True),
    ('Website', lambda b: b.website or 'No website', 30, True),
    ('Website status', lambda b: b.website_status or 'unknown', 14, False),
    ('Instagram', lambda b: b.instagram_url, 26, False),
    ('Facebook', lambda b: b.facebook_url, 26, False),
    ('Rating', lambda b: float(b.rating) if b.rating is not None else None, 8, True),
    ('Reviews', lambda b: b.review_count, 9, True),
    ('Business status', lambda b: b.business_status, 16, False),
    ('Website opportunity', lambda b: b.get_website_opportunity_label_display(), 18, True),
    ('Meta opportunity', lambda b: b.get_meta_opportunity_label_display(), 16, True),
    ('Overall score', lambda b: b.overall_score, 12, True),
    ('Google Maps', lambda b: b.maps_uri, 30, False),
    ('Found on', lambda b: timezone.localtime(b.first_seen_at).strftime('%Y-%m-%d %H:%M'), 17, False),
)


def _xlsx_safe(value):
    """Stop spreadsheet formula injection from scraped text."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def to_xlsx(businesses, title: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Leads'
    ws.append([c[0] for c in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='344054')
        cell.alignment = Alignment(vertical='center')
    for b in businesses:
        ws.append([_xlsx_safe(c[1](b)) for c in COLUMNS])
    for i, c in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = c[2]
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(businesses, title: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    styles = getSampleStyleSheet()
    cell = styles['BodyText']
    cell.fontSize, cell.leading = 7, 9
    head = [Paragraph(f'<b>{c[0]}</b>', cell) for c in COLUMNS if c[3]]
    rows = [head]
    for b in businesses:
        rows.append([Paragraph(_esc(c[1](b)), cell) for c in COLUMNS if c[3]])
    widths = [c[2] for c in COLUMNS if c[3]]
    scale = (landscape(A4)[0] - 20 * mm) / sum(widths)
    table = Table(rows, colWidths=[w * scale for w in widths], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e4e7ec')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#98a2b3')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9fafb')]),
    ]))
    story = [Paragraph(_esc(title), styles['Title']),
             Paragraph(f'{len(rows) - 1} businesses · generated {timezone.localtime().strftime("%d %b %Y %H:%M")}',
                       styles['Normal']),
             Spacer(1, 6 * mm), table]
    doc.build(story)
    return buf.getvalue()


def _esc(value) -> str:
    text = '' if value is None else str(value)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
