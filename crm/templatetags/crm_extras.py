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
def crm_activity_line(action, created_at):
    """Human-readable last activity + relative time."""
    if not created_at:
        return '—'
    label = ACTIVITY_LABELS.get(action or '', None)
    if not label:
        if action:
            label = action.replace('_', ' ').title()
        else:
            label = 'Activity'
    now = timezone.now()
    if timezone.is_aware(created_at) and timezone.is_naive(now):
        pass
    delta = now - created_at
    secs = int(delta.total_seconds())
    if secs < 60:
        rel = 'just now'
    elif secs < 3600:
        rel = f'{secs // 60}m ago'
    elif secs < 86400:
        rel = f'{secs // 3600}h ago'
    elif secs < 604800:
        rel = f'{secs // 86400}d ago'
    else:
        rel = created_at.strftime('%b %d')
    # Refine copy per action
    if action == 'follow_up_scheduled':
        return f'Follow-up added {rel}'
    if action == 'follow_up_done':
        return f'Follow-up done {rel}'
    if action == 'note':
        return f'Note added {rel}'
    if action == 'task_updated' and 'Done' in str(action):
        return f'Task completed {rel}'
    if action == 'task_updated':
        return f'Task updated {rel}'
    if action == 'task_added':
        return f'Task added {rel}'
    if action == 'created':
        return f'Created {rel}'
    if action == 'imported':
        return f'Imported {rel}'
    if action == 'contact_updated':
        return f'Contact updated {rel}'
    if action == 'call':
        return f'Call logged {rel}'
    return f'{label} {rel}'


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
def whatsapp_wa_url(phone):
    """Build https://wa.me/… for clickable WhatsApp chat (digits only, + optional CC)."""
    if not phone:
        return ''
    d = re.sub(r'\D', '', str(phone))
    if len(d) < 10:
        return ''
    cc = (getattr(settings, 'CRM_WHATSAPP_DEFAULT_COUNTRY_CODE', None) or '91').strip().lstrip(
        '+'
    )
    if len(d) == 11 and d.startswith('0'):
        d = f'{cc}{d[1:]}'
    elif len(d) == 10:
        d = f'{cc}{d}'
    return f'https://wa.me/{d}'
