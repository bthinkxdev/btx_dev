"""Money helpers (Part 1.5): Decimal only, round-half-up, Indian grouping, amount in words."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal('0.00')
PAISA = Decimal('0.01')
RUPEE = Decimal('1')


def D(value) -> Decimal:
    """Convert to Decimal without ever going through float."""
    if value is None or value == '':
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Floats are forbidden for money; repr keeps the shortest exact form.
        value = repr(value)
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'Not a valid amount: {value!r}') from exc


def q2(value) -> Decimal:
    """Round to paise, half-up."""
    return D(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def q0(value) -> Decimal:
    """Round to whole rupee, half-up."""
    return D(value).quantize(RUPEE, rounding=ROUND_HALF_UP)


def indian_group(int_part: str) -> str:
    """'125000' -> '1,25,000'."""
    if len(int_part) <= 3:
        return int_part
    last3 = int_part[-3:]
    rest = int_part[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return ','.join(groups) + ',' + last3


def format_inr(value, symbol: bool = True, decimals: int = 2) -> str:
    """Format as ₹1,25,000.00 (Part 2.3)."""
    if value is None or value == '':
        value = ZERO
    amount = D(value)
    negative = amount < 0
    amount = abs(amount)
    if decimals == 0:
        amount = q0(amount)
        int_part, frac = str(amount), ''
    else:
        amount = q2(amount)
        int_part, _, frac = f'{amount:.2f}'.partition('.')
    text = indian_group(int_part) + (('.' + frac) if frac else '')
    if symbol:
        text = '₹' + text
    return ('-' if negative else '') + text


_ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
         'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen',
         'Eighteen', 'Nineteen']
_TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (' ' + _ONES[n % 10] if n % 10 else '')).strip()


def _three(n: int) -> str:
    hundred, rest = divmod(n, 100)
    parts = []
    if hundred:
        parts.append(_ONES[hundred] + ' Hundred')
    if rest:
        parts.append(_two(rest))
    return ' '.join(parts)


def _int_words(n: int) -> str:
    if n == 0:
        return 'Zero'
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(_int_words(crore) + ' Crore')
    if lakh:
        parts.append(_two(lakh) + ' Lakh')
    if thousand:
        parts.append(_two(thousand) + ' Thousand')
    if n:
        parts.append(_three(n))
    return ' '.join(parts)


def amount_in_words(value) -> str:
    """₹11,800.50 -> 'Rupees Eleven Thousand Eight Hundred and Fifty Paise Only'."""
    amount = q2(abs(D(value)))
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    text = 'Rupees ' + _int_words(rupees)
    if paise:
        text += ' and ' + _two(paise) + ' Paise'
    return text + ' Only'


def percent_of(amount, pct) -> Decimal:
    return q2(D(amount) * D(pct) / Decimal('100'))
