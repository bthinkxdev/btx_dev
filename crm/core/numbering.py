"""Gap-free document numbering (Part 1.5, 7.1, 14.3).

The sequence row for (doc_type, FY) is locked with SELECT ... FOR UPDATE inside
the caller's transaction. If the issuing transaction rolls back, the increment
rolls back too, so numbers are never skipped and never reused.
"""
from __future__ import annotations

from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from .india import fy_label

DEFAULT_SERIES = {
    'QT': ('Quotation', 'QT'),
    'PF': ('Proforma invoice', 'PF'),
    'RV': ('Advance receipt voucher', 'RV'),
    'INV': ('Tax invoice', 'INV'),
    'RCPT': ('Payment receipt', 'RCPT'),
    'CN': ('Credit note', 'CN'),
    'DN': ('Debit note', 'DN'),
    'JV': ('Journal voucher', 'JV'),
    'CT': ('Contract', 'CT'),
    'EXP': ('Expense / vendor bill', 'EXP'),
    'TKT': ('Support ticket', 'TKT'),
    'PRJ': ('Project', 'PRJ'),
    'PL': ('Payment link', 'PL'),
}


def _series(doc_type: str):
    from crm.models import SequenceSeries
    s = SequenceSeries.objects.filter(doc_type=doc_type).first()
    if s:
        return s.prefix, s.padding
    name, prefix = DEFAULT_SERIES.get(doc_type, (doc_type, doc_type))
    return prefix, 4


def allocate(doc_type: str, on: date | None = None) -> str:
    """Return the next number, e.g. 'INV/2026-27/0001'. Must run inside transaction.atomic()."""
    from crm.models import DocumentSequence
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Document numbers must be allocated inside a database transaction.')
    on = on or timezone.localdate()
    fy = fy_label(on)
    prefix, padding = _series(doc_type)
    try:
        with transaction.atomic():
            DocumentSequence.objects.get_or_create(
                doc_type=doc_type, fiscal_year=fy, defaults={'prefix': prefix, 'padding': padding})
    except IntegrityError:
        pass  # created concurrently; fine
    seq = DocumentSequence.objects.select_for_update().get(doc_type=doc_type, fiscal_year=fy)
    number = seq.next_number
    seq.next_number = number + 1
    if seq.first_issued_at is None:
        seq.first_issued_at = timezone.now()
    seq.save(update_fields=['next_number', 'first_issued_at'])
    return f'{seq.prefix}/{fy}/{number:0{seq.padding}d}'


def allocate_simple(doc_type: str) -> str:
    """Non-FY running code (projects, tickets, payment links): 'PRJ-0001'."""
    from crm.models import DocumentSequence
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Codes must be allocated inside a database transaction.')
    prefix, padding = _series(doc_type)
    try:
        with transaction.atomic():
            DocumentSequence.objects.get_or_create(doc_type=doc_type, fiscal_year='ALL',
                                                   defaults={'prefix': prefix, 'padding': padding})
    except IntegrityError:
        pass
    seq = DocumentSequence.objects.select_for_update().get(doc_type=doc_type, fiscal_year='ALL')
    number = seq.next_number
    seq.next_number = number + 1
    seq.save(update_fields=['next_number'])
    return f'{seq.prefix}-{number:0{max(seq.padding, 4)}d}'
