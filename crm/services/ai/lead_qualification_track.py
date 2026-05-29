"""AI full qualification track (plans + budget) for new ad leads."""

from __future__ import annotations

import random
import re

from crm.services.crm import get_lead_funnel_data, update_lead_meta

AI_TRACK_FULL = 'full_qualification'
_LEGACY_TRACK = 'full_after_hours'


def infer_service_from_message(text: str) -> tuple[str, str]:
    raw = str(text or '').lower()
    if any(
        k in raw
        for k in (
            'ecommerce',
            'e-commerce',
            'online shop',
            'online store',
            'online sales',
            'sales system',
            'sell online',
            'online selling',
        )
    ):
        return 'ecommerce', 'Ecommerce'
    if any(k in raw for k in ('marketing', ' ads', 'ads ', 'advertising', 'promotion', 'meta ads')):
        return 'marketing', 'Marketing'
    if any(k in raw for k in ('website', 'web site', 'web development', 'landing page')):
        return 'website', 'Website'
    if 'online' in raw and any(k in raw for k in ('sales', 'system', 'store', 'shop', 'business')):
        return 'ecommerce', 'Ecommerce'
    return 'ecommerce', 'Ecommerce'


def _budget_plans_text(service: str, stage_value: str, lang: str) -> str:
    service = str(service or 'ecommerce').strip().lower()
    slots = random.choice([1, 2])

    if service == 'ecommerce':
        return (
            f'1) ₹20K–30K — basic ecommerce setup\n'
            f'2) ₹30K–45K — better design + conversions\n'
            f'(1 month marketing support free — only {slots} slot left this month)'
        )

    if service == 'marketing':
        return (
            f'1) ₹10K–15K/mo — starting enquiries\n'
            f'2) ₹15K–25K/mo — consistent leads\n'
            f'3) ₹25K+/mo — scaling\n'
            f'(limited slots — {slots} available)'
        )

    return (
        f'1) ₹10K–25K — basic website\n'
        f'2) ₹25K–40K — better design + trust\n'
        f'3) ₹40K–60K — conversion-focused\n'
        f'(only {slots} slot left this month)'
    )


def _current_step(meta: dict) -> str:
    if not meta.get('stage_value') and not meta.get('business_stage'):
        return 'business_situation'
    if not meta.get('timeline'):
        return 'timeline'
    if not meta.get('budget_range') and not meta.get('budget'):
        return 'budget_plans'
    if not meta.get('preferred_call_time') and not meta.get('contact_time'):
        return 'name_and_call'
    return 'complete'


def _normalize_track(meta: dict) -> dict:
    track = meta.get('ai_qualification_track')
    if track == _LEGACY_TRACK:
        meta = dict(meta)
        meta['ai_qualification_track'] = AI_TRACK_FULL
    if meta.get('qualification_mode') == 'structured_after_hours':
        meta = dict(meta)
        meta['qualification_mode'] = ''
        if not meta.get('ai_qualification_track'):
            meta['ai_qualification_track'] = AI_TRACK_FULL
    return meta


def activate_qualification_track(lead, user_message: str) -> None:
    meta = _normalize_track(get_lead_funnel_data(lead) or {})
    if meta.get('ai_qualification_track') == AI_TRACK_FULL:
        return
    from crm.services.whatsapp_bot import is_qualification_opener

    if not is_qualification_opener(user_message):
        return
    service, _label = infer_service_from_message(user_message)
    lang = 'ml' if re.search(r'[\u0D00-\u0D7F]', user_message) else 'en'
    update_lead_meta(
        lead,
        ai_qualification_track=AI_TRACK_FULL,
        qualification_mode='',
        service=service,
        language=lang,
        intent_opener=str(user_message or '')[:500],
    )
    from crm.services.crm import update_lead_funnel

    update_lead_funnel(lead, stage='ai_qualification')


def ensure_full_qualification_track(lead, user_message: str) -> str:
    """Build Gemini directive when full qualification track is active."""
    meta = _normalize_track(get_lead_funnel_data(lead) or {})
    if meta.get('ai_qualification_track') != AI_TRACK_FULL:
        return ''

    step = _current_step(meta)
    service = str(meta.get('service') or 'ecommerce').lower()
    stage_value = str(meta.get('stage_value') or meta.get('business_stage') or '')
    lang = 'ml' if str(meta.get('language') or '').lower() == 'ml' else 'en'
    plans = _budget_plans_text(service, stage_value, lang)

    step_instructions = {
        'business_situation': (
            'STEP: Business situation. Warm ack in their language, then ONE casual question: '
            'already running with low sales, or planning to start? (I / ഞാൻ tone). '
            'Map business_stage: running | planning | just_checking.'
        ),
        'timeline': (
            'STEP: Timeline. Ask when they want to start — this week / this month / just looking. '
            'One short line, first person.'
        ),
        'budget_plans': (
            'STEP: Budget. Short numbered list (1, 2, 3) only here. Explain like a friend, not a brochure. '
            'Ask which feels right (reply with number):\n'
            f'{plans}'
        ),
        'name_and_call': (
            'STEP: Name + call time. Ask name and when to call — morning/afternoon/evening. '
            'Say YOU will call ("ഞാൻ വിളിക്കാം" / "I\'ll call"), not team. Optional: I call mostly 10–7.'
        ),
        'complete': (
            'STEP: Wrap up. Confirm YOU will call at the time they said (first person). '
            'Use their name naturally once. No "Let\'s talk!" or signing your full name. '
            'mark_qualified true if budget + call time known.'
        ),
    }

    return (
        '=== FULL AI QUALIFICATION (active) ===\n'
        f'Service: {service}. Current step: {step}.\n'
        f'{step_instructions.get(step, "")}\n'
        'FIRST PERSON ONLY. Sound like a real Kerala sales person on WhatsApp — not a script.\n'
        '=== END ==='
    )


def is_full_qualification_active(lead) -> bool:
    meta = _normalize_track(get_lead_funnel_data(lead) or {})
    return meta.get('ai_qualification_track') == AI_TRACK_FULL
