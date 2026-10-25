"""Template helpers: formatting (Part 1.5, 2.3), badges, permission checks, form fields."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from crm.core import india, permissions, ui
from crm.core.money import amount_in_words, format_inr

register = template.Library()


# ---------------------------------------------------------------- formatting

@register.filter
def inr(value):
    if value is None or value == '':
        return '—'
    try:
        return format_inr(value)
    except ValueError:
        return value


@register.filter
def inr0(value):
    if value is None or value == '':
        return '—'
    return format_inr(value, decimals=0)


@register.filter
def inr_plain(value):
    return format_inr(value, symbol=False)


@register.filter
def words(value):
    return amount_in_words(value)


@register.filter
def d(value):
    """25-Oct-2026 (Part 1.5)."""
    if not value:
        return '—'
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        value = value.date()
    if isinstance(value, date):
        return value.strftime('%d-%b-%Y')
    return value


@register.filter
def dt(value):
    """25-Oct-2026 14:32 IST."""
    if not value:
        return '—'
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        return value.strftime('%d-%b-%Y %H:%M')
    return d(value)


@register.filter
def tm(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        value = timezone.localtime(value)
    return value.strftime('%H:%M')


@register.filter
def iso_local(value):
    """Value for <input type=datetime-local>."""
    if not value:
        return ''
    return timezone.localtime(value).strftime('%Y-%m-%dT%H:%M')


@register.filter
def ago(value):
    if not value:
        return '—'
    delta = timezone.now() - value
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = 'just now' if not future else 'in a moment'
        return text
    for unit, size in (('day', 86400), ('h', 3600), ('min', 60)):
        if seconds >= size:
            n = seconds // size
            label = f'{n} {unit}{"s" if unit == "day" and n != 1 else ""}'
            return f'in {label}' if future else f'{label} ago'
    return ''


@register.filter
def pct(value):
    if value is None or value == '':
        return '—'
    v = Decimal(str(value)).normalize()
    return f'{v:f}%'


@register.filter
def phone(value):
    return india.display_phone(value)


@register.filter
def wa(value):
    return india.whatsapp_number(value)


@register.filter
def masked(value, visible=4):
    return india.mask(value or '', int(visible))


@register.filter
def state_name(code):
    return india.state_name(code)


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def attr(obj, name):
    return getattr(obj, name, '')


@register.filter
def initials(user_or_name):
    name = user_or_name if isinstance(user_or_name, str) else (
        (user_or_name.get_full_name() or user_or_name.get_username()) if user_or_name else '')
    parts = [p for p in name.split() if p]
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


@register.filter
def uname(user):
    if not user:
        return '—'
    return user.get_full_name() or user.get_username()


@register.filter
def neg(value):
    try:
        return -Decimal(str(value))
    except Exception:
        return value


@register.filter
def mul(value, arg):
    """Decimal-exact multiply (Django's add/widthratio are not money-safe)."""
    try:
        return Decimal(str(value)) * Decimal(str(arg))
    except Exception:
        return ''


@register.filter
def plus(value, arg):
    """Decimal-exact addition; use instead of |add, which truncates decimals to integers."""
    try:
        return Decimal(str(value or 0)) + Decimal(str(arg or 0))
    except Exception:
        return ''


@register.filter
def minus(value, arg):
    try:
        return Decimal(str(value or 0)) - Decimal(str(arg or 0))
    except Exception:
        return ''


@register.filter
def absval(value):
    try:
        return abs(Decimal(str(value)))
    except Exception:
        return value


# ---------------------------------------------------------------- badges / icons

@register.simple_tag
def badge(code, label=None, color=None):
    color = color or ui.status_color(code)
    text = label if label is not None else ui.humanize_status(code)
    return format_html('<span class="{}">{}<span>{}</span></span>', ui.BADGE_CLASSES.get(color, ui.BADGE_CLASSES['grey']),
                       mark_safe(ui.icon_svg(ui.BADGE_ICONS.get(color, 'dot'), 12)), text)


@register.simple_tag
def status_badge(obj, field='status'):
    code = getattr(obj, field, '')
    if hasattr(obj, 'display_status') and field == 'status':
        code = obj.display_status
    label_getter = getattr(obj, f'get_{field}_display', None)
    label = label_getter() if label_getter and code == getattr(obj, field, None) else ui.humanize_status(code)
    return badge(code, label)


@register.simple_tag
def icon(name, size=20, css=''):
    return mark_safe(ui.icon_svg(name, int(size), css))


# ---------------------------------------------------------------- permissions

@register.simple_tag(takes_context=True)
def can(context, module, action='view'):
    request = context.get('request')
    return bool(request and permissions.has_perm(request.user, module, action))


@register.simple_tag(takes_context=True)
def sees(context, group):
    request = context.get('request')
    return bool(request and permissions.can_see(request.user, group))


# ---------------------------------------------------------------- forms

@register.simple_tag
def idem():
    """One-time submission token (Part 2.2 rule 1)."""
    return format_html('<input type="hidden" name="_idem" value="{}">', uuid.uuid4().hex)


@register.inclusion_tag('crm/components/field.html')
def field(bound_field, label=None, help=None, wrapper_class='', prefix_text='', suffix_text=''):
    return {'f': bound_field, 'label': label or bound_field.label, 'help': help if help is not None else bound_field.help_text,
            'wrapper_class': wrapper_class, 'prefix_text': prefix_text, 'suffix_text': suffix_text}


@register.simple_tag(takes_context=True)
def qs(context, **kwargs):
    """Current query string with some keys replaced (for filters, sorting, pagination)."""
    request = context['request']
    params = request.GET.copy()
    for k, v in kwargs.items():
        if v is None or v == '':
            params.pop(k, None)
        else:
            params[k] = v
    if 'page' not in kwargs:
        params.pop('page', None)
    encoded = params.urlencode()
    return '?' + encoded if encoded else '?'


@register.simple_tag(takes_context=True)
def sort_link(context, field, label):
    request = context['request']
    current = request.GET.get('sort', '')
    if current == field:
        new, arrow = '-' + field, ' ↑'
    elif current == '-' + field:
        new, arrow = field, ' ↓'
    else:
        new, arrow = field, ''
    params = request.GET.copy()
    params['sort'] = new
    params.pop('page', None)
    return format_html('<a href="?{}" class="inline-flex items-center gap-1 hover:text-gray-700 dark:hover:text-gray-200" '
                       'aria-sort="{}">{}{}</a>', params.urlencode(),
                       'ascending' if arrow == ' ↑' else ('descending' if arrow == ' ↓' else 'none'), label, arrow)


@register.filter
def is_active_filter(request, pair):
    """{% if request|is_active_filter:'quick=overdue' %}"""
    key, _, value = pair.partition('=')
    return request.GET.get(key, '') == value


@register.filter
def startswith(text, prefix):
    return str(text or '').startswith(prefix)


@register.filter
def model_name(obj):
    return obj._meta.model_name if hasattr(obj, '_meta') else ''


@register.simple_tag
def urlparams(**kwargs):
    return urlencode({k: v for k, v in kwargs.items() if v not in (None, '')})
