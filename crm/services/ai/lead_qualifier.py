"""Apply Gemini extraction to CRM lead meta + pipeline."""

from __future__ import annotations

import logging

from crm.services.crm import get_lead_funnel_data, update_lead_funnel, update_lead_meta

logger = logging.getLogger(__name__)

QUALITY_TO_PRIORITY = {
    'hot': 'high',
    'warm': 'medium',
    'cold': 'low',
}


def merge_extraction(lead, extracted: dict | None) -> dict:
    """Persist non-empty extracted fields into [WA_META]. Returns updates applied."""
    if not lead or not extracted:
        return {}

    meta = get_lead_funnel_data(lead) or {}
    field_map = {
        'service': 'service',
        'business_type': 'business',
        'budget': 'budget_range',
        'timeline': 'timeline',
        'lead_quality': 'lead_quality',
        'intent': 'intent',
        'sentiment': 'sentiment',
        'language': 'language',
        'preferred_call_time': 'preferred_call_time',
        'preferred_day': 'preferred_day',
        'urgency': 'urgency',
        'business_stage': 'stage_value',
        'budget_range': 'budget_range',
    }

    updates = {}
    for src, dest in field_map.items():
        val = str(extracted.get(src) or '').strip()
        if val and meta.get(dest) != val:
            updates[dest] = val

    if updates:
        update_lead_meta(lead, **updates)

    quality = str(extracted.get('lead_quality') or '').strip().lower()
    if quality == 'hot':
        update_lead_meta(lead, hot_lead=True)
        update_lead_funnel(lead, priority='high')

    return updates


def should_mark_qualified(lead, extracted: dict | None, *, mark_qualified_flag: bool) -> bool:
    if mark_qualified_flag:
        return True
    meta = get_lead_funnel_data(lead) or {}
    has_service = bool(meta.get('service') or meta.get('intent'))
    has_contact = bool(meta.get('preferred_call_time') or meta.get('contact_time'))
    has_budget_or_timeline = bool(meta.get('budget_range') or meta.get('timeline'))
    return has_service and has_contact and has_budget_or_timeline


def apply_qualification(lead, extracted: dict | None, *, mark_qualified: bool = False):
    if not lead:
        return
    merge_extraction(lead, extracted)
    if should_mark_qualified(lead, extracted, mark_qualified_flag=mark_qualified):
        service_label = str((get_lead_funnel_data(lead) or {}).get('service') or 'WhatsApp')
        update_lead_funnel(lead, service=service_label, set_qualified=True, stage='completed')
        update_lead_meta(lead, ai_stage='qualified')
