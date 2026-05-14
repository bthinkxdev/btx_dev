"""
Process renewal expiry and send reminder emails.

Usage:
    python manage.py process_renewals
    python manage.py process_renewals --dry-run
    python manage.py process_renewals --type 7d
    python manage.py process_renewals --skip-expiry
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from crm.models import RenewalReminderLog, RenewalTracker
from crm.services import renewals as renewals_service


class Command(BaseCommand):
    help = 'Expire overdue renewals (optional) and send idempotent reminder emails.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print actions only; do not write DB or send mail.',
        )
        parser.add_argument(
            '--type',
            choices=['30d', '7d', '1d', 'expired', 'internal', 'all'],
            default='all',
            help='Run only for a specific reminder bucket.',
        )
        parser.add_argument(
            '--skip-expiry',
            action='store_true',
            help='Skip process_expired_renewals().',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        only = options['type']
        skip_expiry = options['skip_expiry']

        expired_n = 0
        sent_ok = 0
        skipped = 0
        failed = 0

        if not skip_expiry:
            if dry:
                today = renewals_service._local_today()
                start_today = renewals_service._start_of_local_day(today)
                expired_n = (
                    RenewalTracker.objects.filter(expires_at__lt=start_today)
                    .exclude(status=RenewalTracker.Status.EXPIRED)
                    .count()
                )
                self.stdout.write(
                    self.style.WARNING(
                        f'[dry-run] Would mark {expired_n} renewals expired (+ suspend projects).'
                    )
                )
            else:
                expired_n = renewals_service.process_expired_renewals()
                self.stdout.write(
                    self.style.SUCCESS(f'Expired renewals processed: {expired_n}')
                )

        buckets = renewals_service.get_renewals_due_for_reminder()

        for rtype, renewals in buckets.items():
            rtype_val = rtype.value if hasattr(rtype, 'value') else str(rtype)
            if only != 'all' and rtype_val != only:
                continue
            for renewal in renewals:
                if rtype_val == RenewalReminderLog.ReminderType.INTERNAL.value:
                    recipient = (
                        getattr(settings, 'RENEWAL_INTERNAL_ALERT_EMAIL', '') or ''
                    ).strip()
                else:
                    recipient = (renewal.project.client.email or '').strip()

                if dry:
                    has_log = RenewalReminderLog.objects.filter(
                        renewal=renewal, reminder_type=rtype_val
                    ).exists()
                    if has_log:
                        skipped += 1
                        self.stdout.write(
                            f'[dry-run] Skip {rtype_val} renewal #{renewal.pk} (log exists)'
                        )
                    elif not recipient:
                        skipped += 1
                        self.stdout.write(
                            f'[dry-run] Skip {rtype_val} renewal #{renewal.pk} (no recipient)'
                        )
                    else:
                        sent_ok += 1
                        self.stdout.write(
                            self.style.NOTICE(
                                f'[dry-run] Would send {rtype_val} to {recipient} for renewal #{renewal.pk}'
                            )
                        )
                    continue

                if not recipient:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f'Skip {rtype_val} renewal #{renewal.pk}: empty recipient email'
                        )
                    )
                    continue

                ok = renewals_service.send_renewal_reminder(
                    renewal, rtype_val, recipient_email=recipient
                )
                if ok:
                    sent_ok += 1
                else:
                    if RenewalReminderLog.objects.filter(
                        renewal=renewal, reminder_type=rtype_val
                    ).exists():
                        skipped += 1
                    else:
                        failed += 1

        exp_label = 'skipped' if skip_expiry else str(expired_n)
        self.stdout.write(
            self.style.SUCCESS(
                f'Summary: expired_processed={exp_label}, '
                f'reminders_sent={sent_ok}, skipped={skipped}, failed={failed}'
            )
        )
