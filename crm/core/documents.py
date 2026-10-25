"""Recalculate a tax document (quotation or invoice) from its lines with the GST engine and freeze a snapshot."""
from __future__ import annotations

from . import gst
from .money import D, q2


def context_for(doc, round_off: bool):
    from crm.models import CompanySettings
    c = CompanySettings.get()
    pos = doc.place_of_supply or doc.customer_state
    if doc.customer_type == gst.CUSTOMER_OVERSEAS:
        pos = pos or '96'
    return gst.TaxContext(
        supplier_state=c.state_code, place_of_supply=pos, customer_type=doc.customer_type, price_mode=doc.price_mode,
        supplier_registered=c.gst_registered, lut=c.lut_enabled, round_off=round_off,
        discount_mode=doc.discount_mode, discount_value=D(doc.discount_value),
    )


def line_inputs(lines):
    return [gst.LineInput(quantity=D(l.quantity), unit_price=D(l.unit_price), tax_rate=D(l.tax_rate),
                          taxability=l.taxability, discount_mode=l.discount_mode, discount_value=D(l.discount_value),
                          key=str(i)) for i, l in enumerate(lines)]


def compute(doc, lines, round_off: bool) -> gst.TaxResult:
    return gst.compute(line_inputs(lines), context_for(doc, round_off))


def apply(doc, lines, result: gst.TaxResult, save_lines: bool = True):
    """Copy engine output onto the document and its lines (lines must be saved objects when save_lines)."""
    for line, lr in zip(lines, result.lines):
        line.gross = lr.gross
        line.discount_amount = lr.line_discount + lr.doc_discount
        line.taxable_value = lr.taxable_value
        line.cgst, line.sgst, line.igst = lr.cgst, lr.sgst, lr.igst
        line.total = lr.total
        if save_lines:
            line.save(update_fields=['gross', 'discount_amount', 'taxable_value', 'cgst', 'sgst', 'igst', 'total'])
    doc.place_of_supply = result.place_of_supply
    doc.supply_type = result.supply_type
    doc.subtotal = result.subtotal
    doc.discount_total = result.discount_total
    doc.taxable_total = result.taxable_total
    doc.cgst_total, doc.sgst_total, doc.igst_total = result.cgst, result.sgst, result.igst
    doc.tax_total = result.tax_total
    doc.round_off = result.round_off
    doc.grand_total = result.grand_total
    doc.tax_snapshot = result.snapshot()


def effective_discount_percent(doc) -> object:
    if not doc.subtotal:
        return D(0)
    return q2(doc.discount_total * 100 / doc.subtotal)
