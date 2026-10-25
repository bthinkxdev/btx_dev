"""India-specific rules: GST state codes, GSTIN/PAN validation, phone (E.164), fiscal year."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from django.utils import timezone

# GST state codes (as printed in GSTIN positions 1-2).
STATES = [
    ('35', 'Andaman and Nicobar Islands'), ('37', 'Andhra Pradesh'), ('12', 'Arunachal Pradesh'),
    ('18', 'Assam'), ('10', 'Bihar'), ('04', 'Chandigarh'), ('22', 'Chhattisgarh'),
    ('26', 'Dadra and Nagar Haveli and Daman and Diu'), ('07', 'Delhi'), ('30', 'Goa'),
    ('24', 'Gujarat'), ('06', 'Haryana'), ('02', 'Himachal Pradesh'), ('01', 'Jammu and Kashmir'),
    ('20', 'Jharkhand'), ('29', 'Karnataka'), ('32', 'Kerala'), ('38', 'Ladakh'),
    ('31', 'Lakshadweep'), ('23', 'Madhya Pradesh'), ('27', 'Maharashtra'), ('14', 'Manipur'),
    ('17', 'Meghalaya'), ('15', 'Mizoram'), ('13', 'Nagaland'), ('21', 'Odisha'),
    ('34', 'Puducherry'), ('03', 'Punjab'), ('08', 'Rajasthan'), ('11', 'Sikkim'),
    ('33', 'Tamil Nadu'), ('36', 'Telangana'), ('16', 'Tripura'), ('09', 'Uttar Pradesh'),
    ('05', 'Uttarakhand'), ('19', 'West Bengal'), ('97', 'Other Territory'),
    ('96', 'Outside India'),
]
STATE_CHOICES = sorted(STATES, key=lambda s: s[1])
STATE_NAMES = dict(STATES)
OUTSIDE_INDIA = '96'

GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')
PAN_RE = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$')
SAC_RE = re.compile(r'^[0-9]{4,8}$')
IFSC_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
_GSTIN_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def state_name(code: str) -> str:
    return STATE_NAMES.get(code or '', '')


def gstin_checksum_ok(gstin: str) -> bool:
    """Official GSTIN check-digit algorithm (mod 36, alternating weights 1 and 2)."""
    total = 0
    for i, ch in enumerate(gstin[:14]):
        value = _GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += value // 36 + value % 36
    check = (36 - total % 36) % 36
    return _GSTIN_CHARS[check] == gstin[14]


def validate_gstin(value: str) -> str | None:
    """Returns an error message or None."""
    value = (value or '').strip().upper()
    if not value:
        return None
    if not GSTIN_RE.match(value):
        return 'Enter a 15-character GSTIN, for example 32AAAAA0000A1Z5.'
    if value[:2] not in STATE_NAMES:
        return 'The first two digits of the GSTIN must be a valid state code.'
    if not gstin_checksum_ok(value):
        return 'This GSTIN fails the checksum. Check it for typing errors.'
    return None


def validate_pan(value: str) -> str | None:
    value = (value or '').strip().upper()
    if value and not PAN_RE.match(value):
        return 'Enter a 10-character PAN, for example ABCDE1234F.'
    return None


def mask(value: str, visible: int = 4) -> str:
    value = value or ''
    if len(value) <= visible:
        return '•' * len(value)
    return '•' * (len(value) - visible) + value[-visible:]


def normalize_phone(raw: str, default_country: str = '91') -> str:
    """Return E.164 (+919544196763) or '' if empty. Raises ValueError if clearly invalid."""
    if not raw:
        return ''
    raw = str(raw).strip()
    has_plus = raw.startswith('+')
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return ''
    if has_plus:
        number = digits
    elif raw.startswith('00'):
        number = digits[2:]
    elif len(digits) == 10:
        number = default_country + digits
    elif len(digits) == 11 and digits.startswith('0'):
        number = default_country + digits[1:]
    elif len(digits) == 12 and digits.startswith(default_country):
        number = digits
    else:
        number = digits
    if number.startswith('91'):
        national = number[2:]
        if len(national) != 10 or national[0] not in '6789':
            raise ValueError('Enter a 10-digit mobile number.')
    elif not 8 <= len(number) <= 15:
        raise ValueError('Enter a valid phone number with country code.')
    return '+' + number


def display_phone(e164: str) -> str:
    if not e164:
        return ''
    if e164.startswith('+91') and len(e164) == 13:
        n = e164[3:]
        return f'+91 {n[:5]} {n[5:]}'
    return e164


def whatsapp_number(e164: str) -> str:
    return (e164 or '').lstrip('+')


# ---------- Fiscal year (1 April to 31 March) ----------

def fy_start_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def fy_label(d: date) -> str:
    """date(2026, 10, 25) -> '2026-27'."""
    start = fy_start_year(d)
    return f'{start}-{str(start + 1)[-2:]}'


def fy_bounds(d: date) -> tuple[date, date]:
    start = fy_start_year(d)
    return date(start, 4, 1), date(start + 1, 3, 31)


def fy_quarter(d: date) -> str:
    """Q1 = Apr-Jun ... Q4 = Jan-Mar."""
    return 'Q' + str(((d.month - 4) % 12) // 3 + 1)


def today() -> date:
    return timezone.localdate()


def next_working_day_at(hour: int = 10, minute: int = 30, start: datetime | None = None) -> datetime:
    """Default next follow-up: next working day (Mon-Sat) at 10:30 IST."""
    start = timezone.localtime(start or timezone.now())
    d = start.date() + timedelta(days=1)
    while d.weekday() == 6:  # Sunday
        d += timedelta(days=1)
    return timezone.make_aware(datetime.combine(d, time(hour, minute)))


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, months: int) -> date:
    import calendar
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def month_end(d: date) -> date:
    return add_months(month_start(d), 1) - timedelta(days=1)
