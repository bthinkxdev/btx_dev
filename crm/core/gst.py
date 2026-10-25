"""
GST calculation engine (Part 6.3).

A pure function: input is a set of lines plus a tax context, output is a frozen
tax breakdown (a plain dict snapshot that documents store). No database access,
no floats. Rules that the CA must sign off (LUT, round-off, supplier
registration) arrive through the context, never as constants.

Rounding: line tax is rounded to paise with round-half-up. CGST is half the line
tax rounded; SGST is the remainder, so CGST + SGST always equals the line tax.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .money import ZERO, D, q0, q2

HUNDRED = Decimal('100')

TAXABLE = 'taxable'
EXEMPT = 'exempt'
NIL = 'nil'
ZERO_RATED = 'zero'
OUT_OF_SCOPE = 'out_of_scope'
TAXABILITY_CHOICES = [
    (TAXABLE, 'Taxable'),
    (EXEMPT, 'Exempt'),
    (NIL, 'Nil-rated'),
    (ZERO_RATED, 'Zero-rated'),
    (OUT_OF_SCOPE, 'Out of scope'),
]

EXCLUSIVE = 'exclusive'
INCLUSIVE = 'inclusive'
PRICE_MODE_CHOICES = [(EXCLUSIVE, 'Tax exclusive'), (INCLUSIVE, 'Tax inclusive')]

CUSTOMER_REGISTERED = 'registered'
CUSTOMER_UNREGISTERED = 'unregistered'
CUSTOMER_SEZ = 'sez'
CUSTOMER_OVERSEAS = 'overseas'
CUSTOMER_TYPE_CHOICES = [
    (CUSTOMER_REGISTERED, 'Registered (B2B)'),
    (CUSTOMER_UNREGISTERED, 'Unregistered (B2C)'),
    (CUSTOMER_SEZ, 'SEZ'),
    (CUSTOMER_OVERSEAS, 'Overseas'),
]

GST_RATES = [Decimal(x) for x in ('0', '0.25', '3', '5', '12', '18', '28')]
GST_RATE_CHOICES = [(str(r), f'{r.normalize()}%') for r in GST_RATES]

DISCOUNT_PERCENT = 'percent'
DISCOUNT_AMOUNT = 'amount'
DISCOUNT_MODE_CHOICES = [(DISCOUNT_PERCENT, 'Percent'), (DISCOUNT_AMOUNT, 'Amount')]

SUPPLY_INTRA = 'intra'
SUPPLY_INTER = 'inter'
SUPPLY_EXPORT_LUT = 'zero_rated_lut'
SUPPLY_NO_TAX = 'unregistered_supplier'


class GSTError(ValueError):
    pass


@dataclass
class LineInput:
    quantity: Decimal
    unit_price: Decimal
    tax_rate: Decimal = Decimal('18')
    taxability: str = TAXABLE
    discount_mode: str = ''
    discount_value: Decimal = ZERO
    key: str = ''


@dataclass
class TaxContext:
    supplier_state: str
    place_of_supply: str
    customer_type: str = CUSTOMER_REGISTERED
    price_mode: str = EXCLUSIVE
    supplier_registered: bool = True
    lut: bool = False
    round_off: bool = False
    discount_mode: str = ''
    discount_value: Decimal = ZERO


@dataclass
class LineResult:
    key: str
    gross: Decimal
    line_discount: Decimal
    doc_discount: Decimal
    taxable_value: Decimal
    tax_rate: Decimal
    taxability: str
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    tax: Decimal
    total: Decimal


@dataclass
class TaxResult:
    supply_type: str
    price_mode: str
    place_of_supply: str
    lines: list[LineResult] = field(default_factory=list)
    subtotal: Decimal = ZERO
    line_discount_total: Decimal = ZERO
    doc_discount_total: Decimal = ZERO
    discount_total: Decimal = ZERO
    taxable_total: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    tax_total: Decimal = ZERO
    exempt_total: Decimal = ZERO
    nil_total: Decimal = ZERO
    zero_rated_total: Decimal = ZERO
    out_of_scope_total: Decimal = ZERO
    round_off: Decimal = ZERO
    grand_total: Decimal = ZERO
    rate_summary: list[dict] = field(default_factory=list)

    def snapshot(self) -> dict:
        """JSON-safe snapshot stored on documents (amounts as strings)."""
        def conv(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, list):
                return [conv(x) for x in v]
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            return v
        return conv(asdict(self))


def determine_supply_type(ctx: TaxContext) -> str:
    if not ctx.supplier_registered:
        return SUPPLY_NO_TAX
    if ctx.customer_type in (CUSTOMER_SEZ, CUSTOMER_OVERSEAS):
        return SUPPLY_EXPORT_LUT if ctx.lut else SUPPLY_INTER
    if not ctx.place_of_supply:
        raise GSTError('Add customer state to determine tax.')
    if ctx.place_of_supply == ctx.supplier_state:
        return SUPPLY_INTRA
    return SUPPLY_INTER


def _line_discount(gross: Decimal, mode: str, value: Decimal) -> Decimal:
    value = D(value)
    if value < 0:
        raise GSTError('Discount cannot be negative.')
    if not mode or value == 0:
        return ZERO
    if mode == DISCOUNT_PERCENT:
        if value > HUNDRED:
            raise GSTError('Discount cannot be more than 100 percent.')
        return q2(gross * value / HUNDRED)
    if mode == DISCOUNT_AMOUNT:
        if value > gross:
            raise GSTError('Line discount cannot be more than the line amount.')
        return q2(value)
    raise GSTError('Unknown discount type.')


def _allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split total across weights proportionally; remainder goes to the largest weight."""
    base = sum(weights, ZERO)
    if total == 0 or base == 0:
        return [ZERO for _ in weights]
    parts = [q2(total * w / base) for w in weights]
    diff = q2(total - sum(parts, ZERO))
    if diff:
        idx = max(range(len(weights)), key=lambda i: weights[i])
        parts[idx] = q2(parts[idx] + diff)
    return parts


def compute(lines: list[LineInput], ctx: TaxContext) -> TaxResult:
    if ctx.price_mode not in (EXCLUSIVE, INCLUSIVE):
        raise GSTError('Choose tax exclusive or tax inclusive pricing.')
    supply = determine_supply_type(ctx)
    result = TaxResult(supply_type=supply, price_mode=ctx.price_mode, place_of_supply=ctx.place_of_supply)

    grosses, line_discs, nets = [], [], []
    for ln in lines:
        qty, price = D(ln.quantity), D(ln.unit_price)
        if qty <= 0:
            raise GSTError('Quantity must be more than 0.')
        if price < 0:
            raise GSTError('Price cannot be negative.')
        rate = D(ln.tax_rate)
        if rate < 0 or rate > HUNDRED:
            raise GSTError('Tax rate must be between 0 and 100 percent.')
        gross = q2(qty * price)
        disc = _line_discount(gross, ln.discount_mode, ln.discount_value)
        grosses.append(gross)
        line_discs.append(disc)
        nets.append(gross - disc)

    net_total = sum(nets, ZERO)
    doc_disc_value = D(ctx.discount_value)
    if doc_disc_value < 0:
        raise GSTError('Discount cannot be negative.')
    if ctx.discount_mode == DISCOUNT_PERCENT and doc_disc_value:
        if doc_disc_value > HUNDRED:
            raise GSTError('Discount cannot be more than 100 percent.')
        doc_disc = q2(net_total * doc_disc_value / HUNDRED)
    elif ctx.discount_mode == DISCOUNT_AMOUNT and doc_disc_value:
        if doc_disc_value > net_total:
            raise GSTError('Overall discount cannot be more than the subtotal.')
        doc_disc = q2(doc_disc_value)
    else:
        doc_disc = ZERO
    doc_allocs = _allocate(doc_disc, nets)

    summary: dict[str, dict] = {}
    for i, ln in enumerate(lines):
        rate = D(ln.tax_rate)
        amount = nets[i] - doc_allocs[i]
        charge_tax = (ln.taxability == TAXABLE and supply in (SUPPLY_INTRA, SUPPLY_INTER) and rate > 0)
        if charge_tax:
            if ctx.price_mode == EXCLUSIVE:
                taxable = amount
                tax = q2(taxable * rate / HUNDRED)
            else:
                tax = q2(amount * rate / (HUNDRED + rate))
                taxable = amount - tax
        else:
            tax, taxable = ZERO, amount
        if supply == SUPPLY_INTRA:
            cgst = q2(tax / 2)
            sgst = tax - cgst
            igst = ZERO
        else:
            cgst = sgst = ZERO
            igst = tax
        effective_taxability = ln.taxability
        if ln.taxability == TAXABLE and supply == SUPPLY_EXPORT_LUT:
            effective_taxability = ZERO_RATED
        lr = LineResult(
            key=ln.key, gross=grosses[i], line_discount=line_discs[i], doc_discount=doc_allocs[i],
            taxable_value=taxable, tax_rate=rate, taxability=effective_taxability,
            cgst=cgst, sgst=sgst, igst=igst, tax=tax, total=taxable + tax,
        )
        result.lines.append(lr)
        result.subtotal += lr.gross
        result.line_discount_total += lr.line_discount
        result.doc_discount_total += lr.doc_discount
        result.taxable_total += taxable
        result.cgst += cgst
        result.sgst += sgst
        result.igst += igst
        if effective_taxability == EXEMPT:
            result.exempt_total += taxable
        elif effective_taxability == NIL:
            result.nil_total += taxable
        elif effective_taxability == ZERO_RATED:
            result.zero_rated_total += taxable
        elif effective_taxability == OUT_OF_SCOPE:
            result.out_of_scope_total += taxable
        bucket_key = f'{effective_taxability}:{rate}'
        b = summary.setdefault(bucket_key, {
            'taxability': effective_taxability, 'rate': rate, 'taxable': ZERO,
            'cgst': ZERO, 'sgst': ZERO, 'igst': ZERO,
        })
        b['taxable'] += taxable
        b['cgst'] += cgst
        b['sgst'] += sgst
        b['igst'] += igst

    result.discount_total = result.line_discount_total + result.doc_discount_total
    result.tax_total = result.cgst + result.sgst + result.igst
    total = result.taxable_total + result.tax_total
    if ctx.round_off:
        result.round_off = q0(total) - total
    result.grand_total = total + result.round_off
    result.rate_summary = sorted(summary.values(), key=lambda b: (b['taxability'], b['rate']))
    return result


def tax_share(collected, rate) -> Decimal:
    """Tax contained in a collected amount at a single rate: collected x rate / (100 + rate)."""
    rate = D(rate)
    return q2(D(collected) * rate / (HUNDRED + rate))


def proportional_split(amount, grand_total, cgst, sgst, igst) -> dict:
    """Split a partial collection proportionally across taxable value and tax heads."""
    amount, grand_total = D(amount), D(grand_total)
    if grand_total <= 0:
        return {'taxable': amount, 'cgst': ZERO, 'sgst': ZERO, 'igst': ZERO, 'tax': ZERO}
    c = q2(amount * D(cgst) / grand_total)
    s = q2(amount * D(sgst) / grand_total)
    i = q2(amount * D(igst) / grand_total)
    tax = c + s + i
    return {'taxable': amount - tax, 'cgst': c, 'sgst': s, 'igst': i, 'tax': tax}


SUPPLY_LABELS = {
    SUPPLY_INTRA: 'Intra-state (CGST + SGST)',
    SUPPLY_INTER: 'Inter-state (IGST)',
    SUPPLY_EXPORT_LUT: 'Zero-rated under LUT',
    SUPPLY_NO_TAX: 'Bill of supply (supplier not registered)',
}


def explain(supply_type: str, place_of_supply_name: str, supplier_state_name: str) -> str:
    """Plain-words reason shown in the 'Why?' tooltip."""
    if supply_type == SUPPLY_INTRA:
        return f'Place of supply ({place_of_supply_name}) is the same as our state, so CGST and SGST apply.'
    if supply_type == SUPPLY_INTER:
        return f'Place of supply ({place_of_supply_name}) differs from our state ({supplier_state_name}), so IGST applies.'
    if supply_type == SUPPLY_EXPORT_LUT:
        return 'Supply to SEZ or overseas under LUT is zero-rated: no tax is charged.'
    return 'We are not registered for GST, so no tax is charged.'
