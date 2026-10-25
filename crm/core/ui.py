"""
Consistent status badges (Part 2.9) and the icon set.

Status is always shown as text + colour + icon so it works for colour-blind users.
Class names are written out in full so Tailwind's scanner picks them up.
"""
from __future__ import annotations

BADGE_CLASSES = {
    'grey': 'badge badge-grey',
    'blue': 'badge badge-blue',
    'amber': 'badge badge-amber',
    'green': 'badge badge-green',
    'red': 'badge badge-red',
    'purple': 'badge badge-purple',
    'brand': 'badge badge-brand',
}

BADGE_ICONS = {'grey': 'dot', 'blue': 'clock', 'amber': 'alert', 'green': 'check', 'red': 'x', 'purple': 'undo',
               'brand': 'dot'}

STATUS_COLORS = {
    # neutral / draft
    'draft': 'grey', 'archived': 'grey', 'superseded': 'grey', 'skipped': 'grey', 'ignored': 'grey',
    'not_required': 'grey', 'planned': 'grey', 'todo': 'grey', 'cold': 'grey', 'closed': 'grey',
    'nurturing': 'grey', 'unmatched': 'grey', 'ended': 'grey', 'exited': 'grey', 'weekly_off': 'grey',
    'expected': 'grey', 'received': 'blue',
    # in progress / pending
    'new': 'blue', 'contacted': 'blue', 'qualified': 'blue', 'quotation_prepared': 'blue', 'quotation_sent': 'blue',
    'negotiation': 'blue', 'active': 'blue', 'sent': 'blue', 'viewed': 'blue', 'awaiting_approval': 'blue',
    'pending_approval': 'blue', 'pending': 'blue', 'pending_verification': 'blue', 'processing': 'blue',
    'issued': 'blue', 'submitted': 'blue', 'in_progress': 'blue', 'in_review': 'blue', 'initiated': 'blue',
    'scheduled': 'blue', 'requested': 'blue', 'open': 'blue', 'queued': 'blue', 'running': 'blue',
    'onboarding': 'blue', 'suggested': 'blue', 'ready_for_review': 'blue', 'generated': 'blue', 'in_review_pr': 'blue',
    'warm': 'amber', 'soft_closed': 'amber', 'present': 'green', 'wfh': 'green', 'evidence_submitted': 'blue',
    'accrued': 'blue', 'certificate_received': 'green', 'waiting': 'amber',
    # needs attention
    'overdue': 'amber', 'expiring': 'amber', 'partially_paid': 'amber', 'on_hold': 'amber', 'review': 'amber',
    'blocked': 'amber', 'correction_requested': 'amber', 'restricted': 'amber', 'suspended': 'amber',
    'hot': 'red', 'on_notice': 'amber', 'half_day': 'amber', 'short': 'amber', 'escalated': 'amber',
    'at_risk': 'amber', 'leave': 'amber', 'on_leave': 'amber', 'stale': 'amber', 'silent': 'amber',
    'expired': 'amber', 'duplicate': 'amber', 'later': 'amber',
    # success
    'paid': 'green', 'approved': 'green', 'won': 'green', 'reconciled': 'green', 'verified': 'green',
    'accepted': 'green', 'converted': 'green', 'done': 'green', 'completed': 'green', 'processed': 'green',
    'succeeded': 'green', 'delivered': 'green', 'matched': 'green', 'posted': 'green', 'resolved': 'green',
    'claimed': 'green', 'merged': 'green', 'imported': 'green', 'recorded': 'green', 'holiday': 'grey',
    'not_duplicate': 'grey',
    # failure / negative
    'failed': 'red', 'rejected': 'red', 'lost': 'red', 'cancelled': 'red', 'void': 'red', 'terminated': 'red',
    'bounced': 'red', 'absent': 'red', 'written_off': 'red', 'abandoned': 'red',
    # reversal / adjustment
    'refunded': 'purple', 'partially_refunded': 'purple', 'credit_noted': 'purple', 'reversed': 'purple',
    'clawed_back': 'purple', 'undone': 'purple', 'captured': 'green', 'authorised': 'blue',
}


def status_color(code: str) -> str:
    return STATUS_COLORS.get(code or '', 'grey')


def humanize_status(code: str) -> str:
    return (code or '').replace('_', ' ').capitalize()


# Stroke icons, 24x24, 1.5px stroke (matches the template's icon weight).
ICONS = {
    'dot': '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>',
    'check': '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    'x': '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    'clock': '<circle cx="12" cy="12" r="8.25"/><path d="M12 7.5V12l3 2"/>',
    'alert': '<path d="M12 3.75L2.75 19.5h18.5L12 3.75z"/><path d="M12 10v4M12 16.75v.01"/>',
    'undo': '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 010 11H11"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'search': '<circle cx="11" cy="11" r="6.75"/><path d="M20 20l-3.5-3.5"/>',
    'phone': '<path d="M5 4h3.5l1.5 4-2 1.5a11 11 0 005.5 5.5l1.5-2 4 1.5V18a2 2 0 01-2 2A15 15 0 013 6a2 2 0 012-2z"/>',
    'whatsapp': '<path d="M4 20l1.3-3.9A8 8 0 1112 20a8 8 0 01-4.1-1.1L4 20z"/><path d="M9 9.5c.5 2 2.5 4 4.5 4.5l1-1 1.5.5v1a1 1 0 01-1 1A6 6 0 018 9.5a1 1 0 011-1h1l.5 1.5-1 1"/>',
    'mail': '<rect x="3.25" y="5.25" width="17.5" height="13.5" rx="2"/><path d="M3.5 7l8.5 6 8.5-6"/>',
    'calendar': '<rect x="3.75" y="5" width="16.5" height="15" rx="2"/><path d="M3.75 9.5h16.5M8 3v4M16 3v4"/>',
    'note': '<path d="M5 4.75h10l4 4v10.5a1 1 0 01-1 1H5a1 1 0 01-1-1V5.75a1 1 0 011-1z"/><path d="M8 11h8M8 15h5"/>',
    'users': '<circle cx="9" cy="8" r="3.25"/><path d="M3 19c.5-3 3-4.75 6-4.75s5.5 1.75 6 4.75"/><path d="M16 5a3 3 0 010 6M17.5 14.5c2 .5 3.25 2 3.5 4.5"/>',
    'user': '<circle cx="12" cy="8" r="3.75"/><path d="M4.5 20c.75-3.5 3.75-5.5 7.5-5.5s6.75 2 7.5 5.5"/>',
    'grid': '<rect x="3.75" y="3.75" width="7" height="7" rx="1.5"/><rect x="13.25" y="3.75" width="7" height="7" rx="1.5"/><rect x="3.75" y="13.25" width="7" height="7" rx="1.5"/><rect x="13.25" y="13.25" width="7" height="7" rx="1.5"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    'funnel': '<path d="M3.75 5h16.5l-6.25 7.5V19l-4 1.5v-8L3.75 5z"/>',
    'board': '<rect x="3.75" y="4" width="4.5" height="16" rx="1"/><rect x="9.75" y="4" width="4.5" height="11" rx="1"/><rect x="15.75" y="4" width="4.5" height="7" rx="1"/>',
    'document': '<path d="M6.5 3.75h7l4 4v12a.5.5 0 01-.5.5h-10.5a.5.5 0 01-.5-.5v-15.5a.5.5 0 01.5-.5z"/><path d="M13.5 3.75v4h4M9 12.5h6M9 16h6"/>',
    'link': '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
    'card': '<rect x="2.75" y="5.25" width="18.5" height="13.5" rx="2"/><path d="M2.75 9.5h18.5M6.5 15h3"/>',
    'invoice': '<path d="M5.75 3.75h12.5v16.5l-2.5-1.5-2.5 1.5-2.5-1.5-2.5 1.5-2.5-1.5V3.75z"/><path d="M9 8.5h6M9 12h6M9 15.5h3"/>',
    'rupee': '<path d="M7 5h10M7 9h10M7 5h3a4 4 0 010 8H7l7 6"/>',
    'book': '<path d="M4.75 5.25a1.5 1.5 0 011.5-1.5H19.25v15H6.25a1.5 1.5 0 00-1.5 1.5V5.25z"/><path d="M4.75 19.75a1.5 1.5 0 001.5 1.5H19.25"/>',
    'bank': '<path d="M3.5 9.5L12 4.5l8.5 5"/><path d="M5 10v7M9.5 10v7M14.5 10v7M19 10v7M3.5 19.5h17"/>',
    'briefcase': '<rect x="3.25" y="7" width="17.5" height="12.5" rx="2"/><path d="M8.5 7V5.5a1.25 1.25 0 011.25-1.25h4.5A1.25 1.25 0 0115.5 5.5V7M3.25 12.5h17.5"/>',
    'folder': '<path d="M3.75 6.5a1.5 1.5 0 011.5-1.5h4l2 2h7.5a1.5 1.5 0 011.5 1.5v9a1.5 1.5 0 01-1.5 1.5H5.25a1.5 1.5 0 01-1.5-1.5v-11z"/>',
    'tasks': '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><path d="M3.75 6.5l1.25 1.25 2-2.25M3.75 12l1.25 1.25 2-2.25M3.75 17.5l1.25 1.25 2-2.25"/>',
    'clock2': '<circle cx="12" cy="12" r="8.25"/><path d="M12 7.5V12h4"/>',
    'ticket': '<path d="M3.75 8.5V6.25a1 1 0 011-1h14.5a1 1 0 011 1V8.5a2.5 2.5 0 000 5v2.25a1 1 0 01-1 1H4.75a1 1 0 01-1-1V13.5a2.5 2.5 0 000-5z"/><path d="M14 5.5v12"/>',
    'receipt': '<path d="M6 3.75h12v16.5l-3-1.75-3 1.75-3-1.75-3 1.75V3.75z"/><path d="M9 9h6M9 12.5h4"/>',
    'store': '<path d="M4 9.5l1.5-5h13L20 9.5M4 9.5h16v.5a2.75 2.75 0 01-5.33.92A2.75 2.75 0 0112 12.75 2.75 2.75 0 019.33 10.92 2.75 2.75 0 014 10v-.5z"/><path d="M5.5 12.5v7h13v-7"/>',
    'id': '<rect x="3.25" y="5.25" width="17.5" height="13.5" rx="2"/><circle cx="9" cy="11" r="2"/><path d="M5.75 16c.5-1.5 1.75-2.25 3.25-2.25s2.75.75 3.25 2.25M14.5 10h3.5M14.5 13.5h2.5"/>',
    'leave': '<path d="M5 20c0-8 5-13 15-15-1 9-6 14-15 15z"/><path d="M5 20l7-7"/>',
    'payroll': '<rect x="2.75" y="6" width="18.5" height="12" rx="2"/><circle cx="12" cy="12" r="2.75"/><path d="M6 9v.01M18 15v.01"/>',
    'chart': '<path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
    'shield': '<path d="M12 3.5l7.5 3v5.5c0 4.5-3.25 7.75-7.5 9-4.25-1.25-7.5-4.5-7.5-9V6.5l7.5-3z"/><path d="M9 12l2 2 4-4"/>',
    'bell': '<path d="M6 16.5V11a6 6 0 1112 0v5.5l1.5 1.5h-15L6 16.5z"/><path d="M10 20.5h4"/>',
    'inbox': '<path d="M3.75 13.5l2.5-8.75h11.5l2.5 8.75v5.25a1 1 0 01-1 1H4.75a1 1 0 01-1-1V13.5z"/><path d="M3.75 13.5h4.75l1 2h5l1-2h4.75"/>',
    'copy': '<rect x="8.75" y="8.75" width="11.5" height="11.5" rx="2"/><path d="M15.25 8.75V5.75a2 2 0 00-2-2h-7.5a2 2 0 00-2 2v7.5a2 2 0 002 2h3"/>',
    'download': '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M4.5 19.5h15"/>',
    'upload': '<path d="M12 15V4M7.5 8.5L12 4l4.5 4.5M4.5 19.5h15"/>',
    'edit': '<path d="M4.5 19.5l1-4.5L16 4.5l3.5 3.5L9 18.5l-4.5 1z"/><path d="M14 6.5l3.5 3.5"/>',
    'trash': '<path d="M4.5 6.5h15M9.5 6.5V4.75h5V6.5M6.5 6.5l.75 13h9.5l.75-13"/>',
    'archive': '<rect x="3.75" y="4.25" width="16.5" height="4.5" rx="1"/><path d="M5 8.75v10a1 1 0 001 1h12a1 1 0 001-1v-10M10 12.5h4"/>',
    'kebab': '<circle cx="12" cy="5.5" r="1.25" fill="currentColor"/><circle cx="12" cy="12" r="1.25" fill="currentColor"/><circle cx="12" cy="18.5" r="1.25" fill="currentColor"/>',
    'chevron_down': '<path d="M6.5 9.5L12 15l5.5-5.5"/>',
    'chevron_right': '<path d="M9.5 6.5L15 12l-5.5 5.5"/>',
    'chevron_left': '<path d="M14.5 6.5L9 12l5.5 5.5"/>',
    'arrow_right': '<path d="M4.5 12h15M13.5 6l6 6-6 6"/>',
    'eye': '<path d="M2.75 12S6 5.75 12 5.75 21.25 12 21.25 12 18 18.25 12 18.25 2.75 12 2.75 12z"/><circle cx="12" cy="12" r="3"/>',
    'lock': '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8 10.5V7.75a4 4 0 018 0v2.75"/>',
    'refresh': '<path d="M19.5 12A7.5 7.5 0 116.2 7.2"/><path d="M4.5 4v4.5H9"/>',
    'filter': '<path d="M4 6.5h16M7 12h10M10 17.5h4"/>',
    'menu': '<path d="M4 6.5h16M4 12h16M4 17.5h10"/>',
    'logout': '<path d="M14.5 4.75h3.75a1 1 0 011 1v12.5a1 1 0 01-1 1H14.5"/><path d="M10 8l-4 4 4 4M6 12h9"/>',
    'moon': '<path d="M19.5 14.5A7.5 7.5 0 019.5 4.5a7.5 7.5 0 1010 10z"/>',
    'target': '<circle cx="12" cy="12" r="8.25"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
    'trophy': '<path d="M8 4.75h8v5a4 4 0 01-8 0v-5zM8 6.5H5v1.5a3 3 0 003 3M16 6.5h3v1.5a3 3 0 01-3 3M12 13.75v3.5M8.5 20h7M9.5 17.25h5V20h-5z"/>',
    'swap': '<path d="M7 7.5h12l-3.5-3.5M17 16.5H5l3.5 3.5"/>',
    'qr': '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><path d="M14 14h2v2h-2zM18 18h2v2h-2zM14 18h2M18 14h2"/>',
    'info': '<circle cx="12" cy="12" r="8.25"/><path d="M12 11v5M12 8v.01"/>',
}


def icon_svg(name: str, size: int = 20, css: str = '') -> str:
    path = ICONS.get(name, ICONS['dot'])
    return (f'<svg class="shrink-0 {css}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true" focusable="false">{path}</svg>')
