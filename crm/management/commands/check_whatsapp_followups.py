from django.core.management.base import BaseCommand

from crm.services.followup import check_and_send_followups


class Command(BaseCommand):
    help = 'Send WhatsApp follow-up reminders for stale leads (cron/Celery safe).'

    def handle(self, *args, **options):
        sent = check_and_send_followups()
        self.stdout.write(self.style.SUCCESS(f'WhatsApp followups sent: {sent}'))

