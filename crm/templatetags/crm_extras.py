"""Template filters for CRM execution UI."""

import re
from django import template
from django.conf import settings
from django.utils import timezone

register = template.Library()

ACTIVITY_LABELS = {
    'created': 'Lead created',
    'follow_up_scheduled': 'Follow-up scheduled',
    'follow_up_done': 'Follow-up completed',
    'follow_up_rescheduled': 'Follow-up rescheduled',
    'note': 'Note added',
    'notes_updated': 'Notes updated',
    'status_change': 'Status updated',
    'package_change': 'Package updated',
    'task_added': 'Task added',
    'task_updated': 'Task updated',
    'call': 'Call logged',
    'imported': 'Imported',
    'contact_updated': 'Contact updated',
    'whatsapp': 'WhatsApp opened',
}


@register.filter
def activity_label(action):
    """Human-readable label for an ActivityLog action code."""
    label = ACTIVITY_LABELS.get(action or '', None)
    if label:
        return label
    if action:
        return action.replace('_', ' ').title()
    return 'Activity'


@register.filter
def activity_when(created_at):
    """Relative time for recent activity, absolute date once it's a week old."""
    if not created_at:
        return ''
    now = timezone.now()
    secs = int((now - created_at).total_seconds())
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60}m ago'
    if secs < 86400:
        return f'{secs // 3600}h ago'
    if secs < 604800:
        return f'{secs // 86400}d ago'
    return created_at.strftime('%b %d')


@register.filter
def fu_time_context(dt, fu_bounds):
    """
    fu_bounds: tuple (fu_start, fu_end) from view.
    Returns 'overdue' | 'today' | 'future' | 'none'
    """
    if not dt:
        return 'none'
    start, end = fu_bounds
    if dt < start:
        return 'overdue'
    if dt < end:
        return 'today'
    return 'future'


@register.filter
def fu_relative_phrase(dt):
    """e.g. 'in 4h' or '3h ago' relative to next follow-up moment."""
    if not dt:
        return ''
    now = timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    secs = int((dt - now).total_seconds())
    if secs == 0:
        return 'now'
    if secs < 0:
        h = abs(secs) // 3600
        m = (abs(secs) % 3600) // 60
        if h >= 48:
            return f'{abs(secs) // 86400}d ago'
        if h > 0:
            return f'{h}h ago' if m < 15 else f'{h}h {m}m ago'
        return f'{m}m ago'
    h = secs // 3600
    m = (secs % 3600) // 60
    if h >= 48:
        return f'in {secs // 86400}d'
    if h > 0:
        return f'in {h}h' if m < 15 else f'in {h}h {m}m'
    return f'in {m}m'


@register.filter
def days_until(dt):
    """Whole calendar days from today to dt's local date (negative if in the past)."""
    if not dt:
        return None
    target = timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()
    return (target - timezone.localdate()).days


@register.filter
def whatsapp_wa_url(phone):
    """Build https://wa.me/… for clickable WhatsApp chat (digits only, + optional CC)."""
    if not phone:
        return ''
    d = re.sub(r'\D', '', str(phone))
    # Allow shorter local/contact numbers too; some teams store 7-9 digit
    # extensions/local formats and still want quick WhatsApp launch.
    if len(d) < 6:
        return ''
    cc = (getattr(settings, 'CRM_WHATSAPP_DEFAULT_COUNTRY_CODE', None) or '91').strip().lstrip(
        '+'
    )
    if len(d) == 11 and d.startswith('0'):
        d = f'{cc}{d[1:]}'
    elif len(d) == 10:
        d = f'{cc}{d}'
    # For all other lengths (6-9, 12+), use digits as entered.
    return f'https://wa.me/{d}'


@register.filter
def dict_get(mapping, key):
    if not mapping or key is None:
        return key
    return mapping.get(key, key)
