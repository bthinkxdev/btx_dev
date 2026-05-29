"""Orchestrate Gemini qualification for WhatsApp leads."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from crm.models import WhatsAppConversation, WhatsAppMessage, WhatsAppNumber
from crm.services.crm import get_lead_funnel_data, update_lead_meta
from crm.services.ai import gemini_client
from crm.services.ai.lead_qualifier import apply_qualification, merge_extraction
from crm.services.ai.lead_qualification_track import (
    activate_qualification_track,
    ensure_full_qualification_track,
    is_full_qualification_active,
)
from crm.services.ai.prompts import EXTRACTION_USER_TEMPLATE, SALES_COORDINATOR_SYSTEM
from crm.services.ai.summary_generator import append_summary_to_lead_notes, generate_ai_summary
from crm.services.ai.voice_processor import transcribe_voice
from crm.services.whatsapp_bot import executive_display_name

logger = logging.getLogger(__name__)


@dataclass
class AiReplyResult:
    reply: str = ''
    handoff: bool = False
    pause_bot: bool = False
    qualified: bool = False


DEFAULT_FALLBACK_REPLY = (
    'Online sales system — got it. '
    'Business already running ആണോ, അതോ start ചെയ്യാനാണ് plan?'
)


def _ai_enabled() -> bool:
    return bool(getattr(settings, 'GEMINI_AI_QUALIFICATION_ENABLED', False)) and gemini_client.is_configured()


def _build_context_summary(lead) -> dict:
    meta = get_lead_funnel_data(lead) or {}
    keys = (
        'service',
        'business',
        'business_type',
        'budget_range',
        'budget',
        'timeline',
        'urgency',
        'stage_value',
        'business_stage',
        'lead_quality',
        'intent',
        'sentiment',
        'language',
        'preferred_call_time',
        'preferred_day',
        'contact_time',
        'ai_summary',
        'voice_intent',
        'hot_lead',
    )
    return {k: meta[k] for k in keys if meta.get(k)}


def _format_chat_history(convo: WhatsAppConversation, limit: int | None = None) -> str:
    limit = limit or int(getattr(settings, 'GEMINI_MAX_HISTORY_MESSAGES', 10) or 10)
    msgs = list(
        convo.messages.order_by('-created_at')[:limit]
    )
    msgs.reverse()
    lines = []
    for m in msgs:
        role = 'Lead' if m.direction == WhatsAppMessage.Direction.INBOUND else 'Us'
        text = (m.text or '').strip()
        if not text:
            continue
        if len(text) > 500:
            text = text[:500] + '…'
        lines.append(f"{role}: {text}")
    return '\n'.join(lines) if lines else '(no prior messages)'


def _send_reply(phone, text, *, wa_number, convo, lead) -> bool:
    from crm.services.whatsapp import send_whatsapp_message

    msg = str(text or '').strip()
    if not msg:
        return False
    ok = send_whatsapp_message(phone, msg, wa_number=wa_number, convo=convo, lead=lead)
    if ok:
        update_lead_meta(lead, last_reply_time=timezone.now().isoformat())
    return ok


def _split_reply_chunks(reply: str, *, max_len: int = 320) -> list[str]:
    reply = str(reply or '').strip()
    if not reply:
        return []
    if len(reply) <= max_len:
        return [reply]
    parts = []
    for chunk in reply.replace('\r', '').split('\n'):
        chunk = chunk.strip()
        if not chunk:
            continue
        while len(chunk) > max_len:
            parts.append(chunk[:max_len].rsplit(' ', 1)[0] or chunk[:max_len])
            chunk = chunk[max_len:].strip()
        if chunk:
            parts.append(chunk)
    return parts[:2]


def _pause_bot_for_handoff(convo: WhatsAppConversation, reason: str = 'ai_handoff'):
    if convo.bot_enabled:
        convo.bot_enabled = False
        convo.human_takeover_at = timezone.now()
        convo.bot_disabled_reason = reason
        convo.save(
            update_fields=['bot_enabled', 'human_takeover_at', 'bot_disabled_reason', 'updated_at']
        )


def _notify_handoff(lead, reason: str):
    from crm.services.whatsapp import notify_sales_team

    try:
        notify_sales_team(lead, reason=reason)
    except Exception:
        logger.exception('notify_sales_team failed')
    logger.info('AI handoff lead=%s reason=%s', getattr(lead, 'id', None), reason)


def process_voice_inbound(
    *,
    lead,
    convo: WhatsAppConversation,
    media: dict | None,
    phone: str,
    wa_number: WhatsAppNumber | None = None,
    raw_payload: dict | None = None,
) -> AiReplyResult | None:
    if not _ai_enabled():
        return None

    voice = transcribe_voice(media)
    transcript = str(voice.get('transcript') or '').strip()
    if not transcript:
        reply = (
            'Voice note കിട്ടി — ഒരു line text ആയി repeat ചെയ്യാമോ? '
            'അല്ലെങ്കിൽ എന്താണ് വേണ്ടതെന്ന് type ചെയ്യൂ.'
        )
        from crm.services.whatsapp import send_whatsapp_message

        _send_reply(phone, reply, wa_number=wa_number, convo=convo, lead=lead)
        return AiReplyResult(reply=reply)

    updates = {
        'voice_transcript': transcript[:2000],
        'voice_intent': str(voice.get('intent_summary') or '')[:500],
    }
    if voice.get('media_path'):
        updates['voice_media_path'] = voice['media_path']
    if voice.get('language'):
        updates['language'] = voice['language']
    update_lead_meta(lead, **updates)

    from crm.models import WhatsAppMessage

    last_in = (
        convo.messages.filter(direction=WhatsAppMessage.Direction.INBOUND)
        .order_by('-id')
        .first()
    )
    if last_in and last_in.text == '[voice note]':
        last_in.text = transcript[:2000]
        last_in.save(update_fields=['text', 'updated_at'])

    user_text = f"[Voice note transcript]: {transcript}"
    return process_lead_message(
        lead=lead,
        convo=convo,
        text=user_text,
        phone=phone,
        wa_number=wa_number,
        raw_payload=raw_payload,
        is_voice=True,
    )


def process_lead_message(
    *,
    lead,
    convo: WhatsAppConversation,
    text: str,
    phone: str,
    wa_number: WhatsAppNumber | None = None,
    raw_payload: dict | None = None,
    is_voice: bool = False,
) -> AiReplyResult | None:
    if not _ai_enabled() or not convo.bot_enabled:
        return None

    user_message = str(text or '').strip()
    if not user_message:
        return None

    activate_qualification_track(lead, user_message)
    update_lead_meta(lead, ai_mode='gemini', ai_last_inbound_at=timezone.now().isoformat())

    executive = wa_number.executive if wa_number else getattr(lead, 'employee', None)
    exec_name = executive_display_name(executive)
    qualification_directive = ensure_full_qualification_track(lead, user_message)
    context = _build_context_summary(lead)
    chat_history = _format_chat_history(convo)
    now_ist = timezone.localtime().strftime('%Y-%m-%d %H:%M IST')

    user_prompt = EXTRACTION_USER_TEMPLATE.format(
        context_json=json.dumps(context, ensure_ascii=False),
        chat_history=chat_history,
        executive_name=exec_name,
        current_time_ist=now_ist,
        qualification_directive=qualification_directive or '(qualification mode — follow step instructions)',
        user_message=user_message,
    )

    data = gemini_client.generate_json(
        system=SALES_COORDINATOR_SYSTEM.format(executive_name=exec_name),
        user=user_prompt,
        temperature=0.62,
    )

    reply = str(data.get('reply') or '').strip()
    extracted = data.get('extracted') if isinstance(data.get('extracted'), dict) else {}
    handoff = bool(data.get('handoff_to_human'))
    pause_bot = bool(data.get('pause_bot')) or handoff
    mark_qualified = bool(data.get('mark_qualified'))
    summary_snippet = str(data.get('summary_snippet') or '').strip()

    merge_extraction(lead, extracted)
    apply_qualification(lead, extracted, mark_qualified=mark_qualified)

    if summary_snippet:
        update_lead_meta(lead, ai_turn_note=summary_snippet[:500])

    if mark_qualified or handoff:
        full_summary = generate_ai_summary(lead, chat_context=chat_history)
        append_summary_to_lead_notes(lead, full_summary)
        update_lead_meta(lead, ai_summary=full_summary)

    if handoff or pause_bot:
        _pause_bot_for_handoff(convo, reason='ai_handoff' if handoff else 'ai_pause')
        _notify_handoff(lead, 'handoff' if handoff else 'pause')

    if not reply and handoff:
        reply = (
            'ഇത് phone-ൽ ഒരു minute discuss ചെയ്യണം — എപ്പോൾ free? '
            "I'll explain properly on a quick call."
        )

    max_chunk = 480 if is_full_qualification_active(lead) else 320
    chunks = _split_reply_chunks(reply, max_len=max_chunk)
    if not chunks:
        reply = DEFAULT_FALLBACK_REPLY
        chunks = [reply]

    for chunk in chunks:
        _send_reply(phone, chunk, wa_number=wa_number, convo=convo, lead=lead)

    return AiReplyResult(
        reply=chunks[0],
        handoff=handoff,
        pause_bot=pause_bot,
        qualified=mark_qualified,
    )
