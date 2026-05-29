import logging

from celery import shared_task

from crm.services.followup import check_and_send_followups

logger = logging.getLogger(__name__)


@shared_task(name='crm.tasks.check_whatsapp_followups')
def check_whatsapp_followups():
    """
    Periodic task for WhatsApp stale-lead reminders.

    Scheduling:
    - Celery beat (recommended): set CELERY_ENABLE_BEAT=1 and run celery beat + worker
    - Or use the management command `python manage.py check_whatsapp_followups` via cron/Task Scheduler
    """
    sent = check_and_send_followups()
    logger.info('WhatsApp followups sent=%s', sent)
    return sent

