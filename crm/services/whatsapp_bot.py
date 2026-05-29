"""CRM master switch + routing for WhatsApp auto-replies."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.models import EmployeeProfile, Lead, WhatsAppBotExcludePhone
from crm.services.crm import _normalize_phone, get_lead_funnel_data, get_lead_stage

logger = logging.getLogger(__name__)

User = get_user_model()

CANONICAL_QUALIFICATION_OPENER = (
    'hi, i want a complete online sales system for my business.'
)


def executive_display_name(user) -> str:
    if not user:
        return 'Our team'
    name = (user.get_full_name() or '').strip()
    return name or user.get_username() or 'Our team'


def _phones_match(sender: str, excluded: str) -> bool:
    a = _normalize_phone(sender)
    b = _normalize_phone(excluded)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]:
        return True
    return False


def is_sender_excluded(executive, phone: str) -> bool:
    """True when this sender is on the executive's bot exclude list."""
    if not executive:
        return False
    normalized = _normalize_phone(phone)
    if not normalized:
        return False
    for excluded in WhatsAppBotExcludePhone.objects.filter(executive=executive).values_list(
        'phone', flat=True
    ):
        if _phones_match(normalized, excluded):
            return True
    return False


def human_takeover_cooldown() -> timedelta:
    minutes = int(getattr(settings, 'WHATSAPP_HUMAN_TAKEOVER_COOLDOWN_MINUTES', 30) or 30)
    return timedelta(minutes=max(1, minutes))


def human_takeover_blocks_reply(convo) -> bool:
    """
    True when the bot must not reply because the executive took over recently.

    If human_takeover was more than cooldown ago, auto-resume bot for this conversation.
    Other disable reasons (ai_handoff, etc.) stay off until manually resumed.
    """
    if convo is None:
        return True

    reason = str(convo.bot_disabled_reason or '').strip()
    taken_at = convo.human_takeover_at

    if reason == 'human_takeover' and taken_at:
        if timezone.now() - taken_at < human_takeover_cooldown():
            return True
        if not convo.bot_enabled:
            convo.bot_enabled = True
            convo.bot_disabled_reason = ''
            convo.save(update_fields=['bot_enabled', 'bot_disabled_reason', 'updated_at'])
            logger.info('Human takeover cooldown ended — bot resumed convo=%s', convo.id)
        return False

    return not convo.bot_enabled


def record_human_takeover(convo) -> None:
    """Pause bot when executive sends a message from their phone."""
    if convo is None:
        return
    convo.bot_enabled = False
    convo.human_takeover_at = timezone.now()
    convo.bot_disabled_reason = 'human_takeover'
    convo.save(
        update_fields=['bot_enabled', 'human_takeover_at', 'bot_disabled_reason', 'updated_at']
    )


def crm_whatsapp_bot_enabled(user) -> bool:
    """True when executive turned on WA bot in CRM (master switch)."""
    if not user:
        return False
    try:
        profile = user.crm_profile
    except EmployeeProfile.DoesNotExist:
        return False
    return bool(profile.whatsapp_bot_enabled)


def is_qualification_opener(text: str, normalized: str | None = None) -> bool:
    raw = (normalized if normalized is not None else str(text or '').strip().lower()).strip()
    if not raw:
        return False
    cleaned = re.sub(r'[.!?,]+$', '', raw).strip()
    canonical = re.sub(r'[.!?,]+$', '', CANONICAL_QUALIFICATION_OPENER).strip()
    if cleaned == canonical:
        return True
    return 'complete online sales system' in cleaned and any(
        w in cleaned.split() for w in ('want', 'need', 'looking')
    )


def is_new_lead_for_qualification(lead) -> bool:
    if not lead:
        return False
    meta = get_lead_funnel_data(lead) or {}
    if meta.get('ai_qualification_track') in ('full_qualification', 'full_after_hours'):
        return True
    if lead.status != Lead.Status.NEW:
        return False
    stage = (get_lead_stage(lead) or 'new').strip().lower()
    return stage in ('new', 'completed', 'step_lang', '')


def should_run_ai_qualification(lead, text: str, normalized: str) -> bool:
    """Only new leads with the ads opener (or already in qual track)."""
    if not is_new_lead_for_qualification(lead):
        return False
    meta = get_lead_funnel_data(lead) or {}
    if meta.get('ai_qualification_track') in ('full_qualification', 'full_after_hours'):
        return True
    return is_qualification_opener(text, normalized)


def unavailable_reply_text(executive, *, lang: str = 'en') -> str:
    if lang == 'ml':
        return 'Hi — ഞാൻ ഇപ്പോൾ available അല്ല. ഉടൻ reply തരാം 🙏'
    return "Hi — I'm not on WhatsApp right now. I'll get back to you soon 🙏"


def should_send_unavailable_reply(lead) -> bool:
    """Avoid spamming the same away message on every text."""
    meta = get_lead_funnel_data(lead) or {}
    last = meta.get('last_unavailable_sent_at')
    if not last:
        return True
    try:
        last_dt = timezone.datetime.fromisoformat(str(last))
        if timezone.is_naive(last_dt):
            last_dt = timezone.make_aware(last_dt, timezone.get_current_timezone())
        return timezone.now() - last_dt >= timedelta(minutes=30)
    except Exception:
        return True
