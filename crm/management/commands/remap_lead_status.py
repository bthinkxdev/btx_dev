"""
One-time remap of the old 15-value detailed Lead.status onto the new
7-stage pipeline (New/Contacted/Qualified/Demo/Proposal/Negotiation/Won),
plus setting lifecycle_state/won_at/lost_* for the old Won/Lost values.

Written as bulk .update() calls (a handful of single UPDATE statements,
not a per-row loop) so it stays fast at production scale (10k+ leads).
Idempotent: only touches rows still holding an old-style status value, so
re-running it after new leads have started using the new statuses is safe.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import Lead

# old status value -> new Status value
STATUS_REMAP = {
    'whatsapp_connected': Lead.Status.CONTACTED,
    'call_connected': Lead.Status.CONTACTED,
    'closing_ongoing': Lead.Status.QUALIFIED,
    'failed_retry': Lead.Status.CONTACTED,
    'proposal_sent': Lead.Status.PROPOSAL,
    'negotiation_after_proposal': Lead.Status.NEGOTIATION,
    'closed': Lead.Status.WON,
    'advance_received_project_started': Lead.Status.WON,
    'project_handed': Lead.Status.WON,
    'training_completed': Lead.Status.WON,
    'balance_paid_project_completed': Lead.Status.WON,
    'issue_payment_collection': Lead.Status.WON,
    'lost': Lead.Status.QUALIFIED,
    'lost_after_proposal': Lead.Status.PROPOSAL,
}
WON_OLD_VALUES = (
    'closed', 'advance_received_project_started', 'project_handed',
    'training_completed', 'balance_paid_project_completed', 'issue_payment_collection',
)
LOST_OLD_VALUES = ('lost', 'lost_after_proposal')


class Command(BaseCommand):
    help = 'Remap legacy Lead.status values onto the new 7-stage pipeline (bulk, idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report counts; write nothing.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        now = timezone.now()
        total = 0

        for old_value, new_status in STATUS_REMAP.items():
            qs = Lead.objects.filter(status=old_value)
            count = qs.count()
            if not count:
                continue
            total += count
            if dry:
                self.stdout.write(f'[dry-run] {old_value} -> {new_status.value}: {count} lead(s)')
                continue

            update_fields = {'status': new_status}
            if old_value in WON_OLD_VALUES:
                update_fields['lifecycle_state'] = Lead.LifecycleState.WON
                qs.filter(won_at__isnull=True).update(won_at=now)
            elif old_value in LOST_OLD_VALUES:
                update_fields['lifecycle_state'] = Lead.LifecycleState.LOST
                qs.filter(lost_at__isnull=True).update(
                    lost_at=now, lost_reason='Migrated from legacy status (unspecified)',
                )
            qs.update(**update_fields)
            self.stdout.write(self.style.SUCCESS(f'{old_value} -> {new_status.value}: {count} lead(s) updated'))

        prefix = '[dry-run] ' if dry else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}Total leads remapped: {total}'))
