import logging
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from crm.models import Lead

logger = logging.getLogger(__name__)
WA_META_PREFIX = '[WA_META]'
STAGE_KEYS = ('new', 'service_selected', 'budget_selected', 'completed')


def _normalize_phone(phone):
    return ''.join(ch for ch in str(phone or '') if ch.isdigit())


def is_valid_lead_phone(phone) -> bool:
    """
    True for plausible customer mobiles; false for WhatsApp LID opaque ids
    (often 14+ digit strings that are not real phone numbers).
    """
    digits = _normalize_phone(phone)
    if not digits:
        return False
    length = len(digits)
    if length < 10 or length > 15:
        return False
    if length >= 14:
        if digits.startswith('91') and length <= 12:
            return True
        if digits.startswith('1') and length == 11:
            return True
        return False
    return True


def conversation_phone_key(phone, wa_chat_id='') -> str:
    """Stable key for WhatsAppConversation when only a LID chat id is known."""
    normalized = _normalize_phone(phone)
    if is_valid_lead_phone(normalized):
        return normalized
    chat_id = str(wa_chat_id or '').strip()
    if '@' in chat_id:
        return _normalize_phone(chat_id.split('@')[0])
    return normalized


def _find_lead_by_wa_chat_id(wa_chat_id: str):
    chat_id = str(wa_chat_id or '').strip()
    if not chat_id:
        return None
    needle = f'"wa_chat_id":{json.dumps(chat_id, ensure_ascii=True)}'
    return Lead.objects.filter(notes__contains=needle).order_by('id').first()


def _lead_field_names():
    return {f.name for f in Lead._meta.get_fields()}


def _get_lead_owner():
    user_model = get_user_model()
    owner = user_model.objects.filter(is_active=True, is_superuser=True).first()
    if owner:
        return owner
    owner = user_model.objects.filter(is_active=True).order_by('id').first()
    return owner


def _parse_wa_meta(notes):
    text = str(notes or '')
    if WA_META_PREFIX not in text:
        return {}
    raw = text.split(WA_META_PREFIX, 1)[1].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _parse_meta_datetime(value):
    if not value:
        return None
    try:
        dt = timezone.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


def _with_wa_meta(notes, meta):
    base = str(notes or '')
    if WA_META_PREFIX in base:
        base = base.split(WA_META_PREFIX, 1)[0].rstrip()
    encoded = json.dumps(meta, ensure_ascii=True, separators=(',', ':'))
    if base:
        return f'{base}\n\n{WA_META_PREFIX}{encoded}'
    return f'{WA_META_PREFIX}{encoded}'


def _status_values():
    return {value for value, _label in getattr(Lead.Status, 'choices', [])}


def upsert_lead(phone, message, source='WhatsApp Ads', *, owner=None, wa_chat_id='', contact_name=''):
    normalized_phone = _normalize_phone(phone)
    valid_phone = is_valid_lead_phone(normalized_phone)
    wa_chat_id = str(wa_chat_id or '').strip()
    contact_name = str(contact_name or '').strip()[:200]

    if not valid_phone and not wa_chat_id:
        logger.warning('Cannot upsert lead: no valid phone or wa_chat_id')
        return None

    fields = _lead_field_names()
    now = timezone.now()

    with transaction.atomic():
        lead = None
        if valid_phone:
            raw_phone = str(phone or '').strip()
            phone_candidates = [normalized_phone]
            if raw_phone and raw_phone != normalized_phone:
                phone_candidates.append(raw_phone)
            lead = (
                Lead.objects.select_for_update()
                .filter(phone__in=phone_candidates)
                .order_by('id')
                .first()
            )

        if not lead and wa_chat_id:
            lead = _find_lead_by_wa_chat_id(wa_chat_id)
            if lead:
                lead = Lead.objects.select_for_update().filter(pk=lead.pk).first()
            if not lead:
                lid_key = _normalize_phone(wa_chat_id.split('@')[0])
                if lid_key:
                    lead = (
                        Lead.objects.select_for_update()
                        .filter(phone=lid_key)
                        .order_by('id')
                        .first()
                    )

        if lead:
            changed_fields = []
            if valid_phone and lead.phone != normalized_phone:
                # Replace a previously saved LID with the real number when we learn it.
                if not is_valid_lead_phone(lead.phone) or lead.phone != normalized_phone:
                    lead.phone = normalized_phone
                    changed_fields.append('phone')
            if contact_name and lead.name in {'', 'WhatsApp Lead'}:
                lead.name = contact_name
                changed_fields.append('name')
            if getattr(lead, 'source', '') != source:
                lead.source = source
                changed_fields.append('source')
            if 'last_message' in fields and getattr(lead, 'last_message', None) != message:
                setattr(lead, 'last_message', message)
                changed_fields.append('last_message')
            if 'last_contacted' in fields:
                if getattr(lead, 'last_contacted', None) != now:
                    setattr(lead, 'last_contacted', now)
                    changed_fields.append('last_contacted')
            if changed_fields:
                lead.save(update_fields=list(dict.fromkeys(changed_fields + ['updated_at'])))
            if wa_chat_id:
                update_lead_meta(lead, wa_chat_id=wa_chat_id)
            return lead

        lead_owner = owner or _get_lead_owner()
        if not lead_owner:
            logger.error('Cannot create WhatsApp lead: no active user found')
            return None

        create_data = {
            'employee': lead_owner,
            'phone': normalized_phone if valid_phone else '',
            'name': contact_name or 'WhatsApp Lead',
            'source': source,
        }
        if 'status' in fields:
            create_data['status'] = 'new'
        if 'last_message' in fields:
            create_data['last_message'] = message
        if 'last_contacted' in fields:
            create_data['last_contacted'] = now
        lead = Lead.objects.create(**create_data)
        if wa_chat_id:
            update_lead_meta(lead, wa_chat_id=wa_chat_id)
        return lead


def get_lead_by_phone(phone):
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None
    return Lead.objects.filter(phone=normalized_phone).order_by('id').first()


def update_lead_funnel(
    lead,
    *,
    stage=None,
    service=None,
    budget=None,
    priority=None,
    name=None,
    set_qualified=False,
):
    if not lead:
        return None

    changed_fields = []
    meta = _parse_wa_meta(getattr(lead, 'notes', ''))

    if stage:
        if meta.get('stage') != stage:
            meta['stage'] = stage
            changed_fields.append('notes')
    if service:
        if meta.get('service') != service:
            meta['service'] = service
            changed_fields.append('notes')
    if budget:
        if meta.get('budget') != budget:
            meta['budget'] = budget
            changed_fields.append('notes')
    if priority:
        if meta.get('priority') != priority:
            meta['priority'] = priority
            changed_fields.append('notes')
    if name:
        clean_name = str(name).strip()[:200]
        if clean_name and clean_name.lower() != 'whatsapp lead' and lead.name != clean_name:
            lead.name = clean_name
            changed_fields.append('name')
            if meta.get('contact_name') != clean_name:
                meta['contact_name'] = clean_name
                changed_fields.append('notes')

    if set_qualified:
        status_values = _status_values()
        target_status = 'qualified' if 'qualified' in status_values else 'whatsapp_connected'
        if getattr(lead, 'status', None) != target_status:
            lead.status = target_status
            changed_fields.append('status')

    if priority == 'high' and hasattr(lead, 'high_hope') and not lead.high_hope:
        lead.high_hope = True
        changed_fields.append('high_hope')

    if 'notes' in changed_fields:
        lead.notes = _with_wa_meta(getattr(lead, 'notes', ''), meta)

    if changed_fields:
        unique_fields = list(dict.fromkeys(changed_fields + ['updated_at']))
        lead.save(update_fields=unique_fields)
    return lead


def get_lead_stage(lead):
    if not lead:
        return 'new'
    meta = _parse_wa_meta(getattr(lead, 'notes', ''))
    return meta.get('stage', 'new')


def get_lead_funnel_data(lead):
    if not lead:
        return {}
    return _parse_wa_meta(getattr(lead, 'notes', ''))


def update_lead_meta(lead, **meta_updates):
    if not lead:
        return None
    if not meta_updates:
        return lead
    meta = _parse_wa_meta(getattr(lead, 'notes', ''))
    changed = False
    for key, value in meta_updates.items():
        if meta.get(key) != value:
            meta[key] = value
            changed = True
    if changed:
        lead.notes = _with_wa_meta(getattr(lead, 'notes', ''), meta)
        lead.save(update_fields=['notes', 'updated_at'])
    return lead


def get_last_followup_sent(lead):
    meta = get_lead_funnel_data(lead)
    return _parse_meta_datetime(meta.get('last_followup_sent'))


def get_lead_stage_counts(period=None):
    counts = {key: 0 for key in STAGE_KEYS}
    leads = Lead.objects.all().only('notes', 'created_at')
    now = timezone.now()
    if period == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        leads = leads.filter(created_at__gte=start)
    elif period == 'this_week':
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        leads = leads.filter(created_at__gte=start)

    for lead in leads.iterator():
        meta = _parse_wa_meta(getattr(lead, 'notes', ''))
        stage = str(meta.get('stage', 'new'))
        if stage not in counts:
            stage = 'new'
        counts[stage] += 1
    return counts
