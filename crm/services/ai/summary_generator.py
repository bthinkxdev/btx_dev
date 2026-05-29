"""AI-generated CRM lead summaries."""

from __future__ import annotations

import logging
import re

from django.utils import timezone

from crm.services.crm import WA_META_PREFIX, get_lead_funnel_data, _parse_wa_meta, _with_wa_meta
from crm.services.ai import gemini_client
from crm.services.ai.prompts import SUMMARY_SYSTEM

logger = logging.getLogger(__name__)

SUMMARY_MARKER = '[AI Summary'


def _human_notes_without_meta(notes: str) -> str:
    text = str(notes or '')
    if WA_META_PREFIX in text:
        return text.split(WA_META_PREFIX, 1)[0].strip()
    return text.strip()


def _strip_old_summaries(human_part: str) -> str:
    lines = []
    for line in human_part.splitlines():
        if line.strip().startswith(SUMMARY_MARKER):
            continue
        lines.append(line)
    return '\n'.join(lines).strip()


def build_summary_from_meta(lead) -> str:
    """Deterministic summary from structured meta (no API call)."""
    meta = get_lead_funnel_data(lead) or {}
    parts = []
    if meta.get('business') or meta.get('business_type'):
        parts.append(f"Business: {meta.get('business') or meta.get('business_type')}.")
    if meta.get('service') or meta.get('intent'):
        parts.append(f"Needs: {meta.get('service') or meta.get('intent')}.")
    if meta.get('stage_value') or meta.get('business_stage'):
        parts.append(f"Stage: {meta.get('stage_value') or meta.get('business_stage')}.")
    if meta.get('timeline') or meta.get('urgency'):
        parts.append(f"Timeline: {meta.get('timeline') or meta.get('urgency')}.")
    if meta.get('budget_range') or meta.get('budget'):
        parts.append(f"Budget: {meta.get('budget_range') or meta.get('budget')}.")
    if meta.get('preferred_call_time') or meta.get('contact_time'):
        parts.append(
            f"Call: {meta.get('preferred_day', '')} {meta.get('preferred_call_time') or meta.get('contact_time')}".strip()
        )
    if meta.get('ai_summary'):
        return str(meta['ai_summary']).strip()
    return ' '.join(parts).strip()


def generate_ai_summary(lead, *, chat_context: str = '') -> str:
    meta = get_lead_funnel_data(lead) or {}
    if not gemini_client.is_configured():
        return build_summary_from_meta(lead)

    user = f"""Lead meta:
{meta}

Recent conversation excerpt:
{chat_context[:4000]}

Write the CRM summary paragraph."""
    try:
        text = gemini_client.generate_text(system=SUMMARY_SYSTEM, user=user, temperature=0.2)
        text = re.sub(r'\s+', ' ', text).strip()
        return text or build_summary_from_meta(lead)
    except Exception:
        logger.exception('AI summary generation failed')
        return build_summary_from_meta(lead)


def append_summary_to_lead_notes(lead, summary: str):
    if not lead or not summary:
        return
    summary = summary.strip()
    if not summary:
        return

    meta = _parse_wa_meta(getattr(lead, 'notes', ''))
    if meta.get('ai_summary') == summary:
        return

    meta['ai_summary'] = summary
    meta['ai_summary_at'] = timezone.now().isoformat()

    human = _strip_old_summaries(_human_notes_without_meta(getattr(lead, 'notes', '')))
    stamp = timezone.localtime().strftime('%Y-%m-%d %H:%M')
    block = f"{SUMMARY_MARKER} {stamp}]\n{summary}"
    if human:
        new_human = f"{human}\n\n{block}"
    else:
        new_human = block

    lead.notes = _with_wa_meta(new_human, meta)
    lead.save(update_fields=['notes', 'updated_at'])
