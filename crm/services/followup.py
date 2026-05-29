import logging
from datetime import timedelta

from django.utils import timezone

from .crm import get_last_followup_sent, get_lead_funnel_data, update_lead_meta
from .whatsapp import (
    get_active_wa_number,
    get_or_create_conversation,
    send_whatsapp_message,
)
from crm.models import Lead, WhatsAppMessage

logger = logging.getLogger(__name__)
REMINDER_TEXT = 'Are you still interested? We have limited slots today.'


def schedule_followup(lead):
    """
    Placeholder scheduler hook.
    Call from Celery beat / cron command to run check_and_send_followups().
    """
    if not lead:
        return False
    meta = get_lead_funnel_data(lead)
    return meta.get('stage') != 'completed'


def check_and_send_followups():
    """
    Cron/Celery-safe polling function:
    - stage != completed
    - no recent update in last 2 hours
    """
    now = timezone.now()
    cutoff = now - timedelta(hours=2)
    sent_count = 0

    leads = (
        Lead.objects.filter(updated_at__lte=cutoff)
        .exclude(phone__isnull=True)
        .exclude(phone__exact='')
        .order_by('id')[:50]
    )
    for lead in leads:
        meta = get_lead_funnel_data(lead)
        stage = meta.get('stage', 'new')
        if stage == 'completed':
            continue
        last_followup_sent = get_last_followup_sent(lead)
        if last_followup_sent and now - last_followup_sent < timedelta(hours=24):
            continue
        wa_pid = str(meta.get('wa_phone_number_id') or '').strip()
        wa_number = get_active_wa_number(wa_pid) if wa_pid else None
        convo = None
        if wa_number:
            convo = get_or_create_conversation(
                wa_number=wa_number, customer_phone=lead.phone, lead=lead
            )
        ok = send_whatsapp_message(
            lead.phone,
            REMINDER_TEXT,
            wa_number=wa_number,
            convo=convo,
            lead=lead,
            source=WhatsAppMessage.Source.SYSTEM,
        )
        if ok:
            sent_count += 1
            update_lead_meta(lead, last_followup_sent=now.isoformat())
            logger.info('Follow-up reminder sent to lead_id=%s', lead.id)
    return sent_count
