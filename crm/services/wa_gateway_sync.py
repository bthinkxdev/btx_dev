"""Sync and verify WhatsApp Web.js gateway sessions against CRM WhatsAppNumber rows."""

from __future__ import annotations

import logging

from django.utils import timezone

from crm.models import EmployeeProfile, WhatsAppNumber
from crm.services.crm import _normalize_phone

logger = logging.getLogger(__name__)


def _digits(phone: str) -> str:
    return _normalize_phone(phone)


def phones_match(expected: str, linked: str) -> bool:
    """True when display_phone_number matches gateway-linked device (last 10 digits)."""
    a = _digits(expected)
    b = _digits(linked)
    if not a or not b:
        return True
    if a == b:
        return True
    if len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]:
        return True
    return False


def apply_gateway_status(
    *,
    session_id: str,
    event: str,
    linked_phone: str = '',
    pushname: str = '',
    reason: str = '',
) -> WhatsAppNumber | None:
    """
    Update WhatsAppNumber gateway_* fields from Node status webhooks.
    Returns the row when found.
    """
    pid = str(session_id or '').strip()
    if not pid:
        return None
    wa = WhatsAppNumber.objects.filter(phone_number_id=pid, is_active=True).first()
    if not wa:
        logger.warning('Gateway status for unknown session=%s event=%s', pid, event)
        return None

    ev = str(event or '').strip().lower()
    linked = _digits(linked_phone)
    now = timezone.now()
    updates = {'gateway_updated_at': now}

    if ev == 'ready' and linked:
        updates['gateway_linked_phone'] = linked
        expected = wa.display_phone_number
        if expected and not phones_match(expected, linked):
            updates['gateway_state'] = 'mismatch'
            logger.error(
                'WA SESSION MISMATCH session=%s expected_display=%s linked=%s executive=%s — '
                'Wrong phone linked to this QR session. Logout session and re-scan correct device.',
                pid,
                expected,
                linked,
                wa.executive.get_username(),
            )
        else:
            updates['gateway_state'] = 'ready'
            logger.info(
                'WA session verified session=%s linked=%s executive=%s pushname=%s',
                pid,
                linked,
                wa.executive.get_username(),
                (pushname or '')[:40],
            )
    elif ev in {'disconnected', 'auth_failure', 'logout', 'start_failed'}:
        updates['gateway_state'] = ev
        if reason:
            logger.warning('WA session %s session=%s reason=%s', ev, pid, reason[:200])
    elif ev == 'starting':
        updates['gateway_state'] = 'starting'
    else:
        updates['gateway_state'] = ev or 'unknown'

    for field, value in updates.items():
        setattr(wa, field, value)
    wa.save(update_fields=list(updates.keys()) + ['updated_at'])
    return wa


def executive_bot_enabled(user) -> bool:
    if not user:
        return False
    try:
        return bool(user.crm_profile.whatsapp_bot_enabled)
    except EmployeeProfile.DoesNotExist:
        return False


def build_session_health_rows(gateway_sessions: list | None) -> list[dict]:
    """Merge CRM WhatsAppNumber rows with live gateway /sessions JSON."""
    by_id = {str(s.get('session') or ''): s for s in (gateway_sessions or [])}
    rows = []
    for wa in WhatsAppNumber.objects.filter(is_active=True).select_related('executive').order_by(
        'phone_number_id'
    ):
        sid = str(wa.phone_number_id)
        live = by_id.get(sid) or {}
        live_state = str(live.get('state') or '')
        linked = _digits(str(live.get('linked_phone') or wa.gateway_linked_phone or ''))
        expected = _digits(wa.display_phone_number)
        mismatch = bool(expected and linked and not phones_match(expected, linked))
        bot_on = executive_bot_enabled(wa.executive)
        ok = (
            live_state == 'ready'
            and not mismatch
            and bot_on
            and str(wa.gateway_state or '') not in {'mismatch', 'auth_failure'}
        )
        rows.append(
            {
                'wa': wa,
                'session_id': sid,
                'live_state': live_state or wa.gateway_state or 'unknown',
                'linked_phone': linked,
                'expected_phone': expected,
                'mismatch': mismatch,
                'bot_enabled': bot_on,
                'executive': wa.executive.get_username(),
                'healthy': ok,
                'has_qr': bool(live.get('has_qr')),
            }
        )
    return rows
