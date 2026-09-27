"""
One-time, explicit, re-runnable migration of existing FollowUp/Task rows
into the new Activity model. Never run automatically — the operator runs
this by hand, ideally with --dry-run first.

Never invents a follow-up date: an active lead that ends up with no
resulting Activity is left with next_activity=None and simply appears in
the Workbench's "No Next Action" tab for a human to triage.

lifecycle_state is NOT touched here — run `remap_lead_status` first (it
sets Won/Lost from the legacy status values); this command only creates
Activity rows and, for leads still Active, assigns the earliest one as
next_activity.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from crm.models import Activity, Lead


class Command(BaseCommand):
    help = 'Migrate existing FollowUp/Task rows into Activity records (additive, idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would happen; write nothing.')
        parser.add_argument('--employee', default=None, help='Limit to one employee username (staged rollout).')

    def handle(self, *args, **options):
        dry = options['dry_run']
        leads = Lead.objects.all().prefetch_related('followups', 'tasks')
        if options['employee']:
            leads = leads.filter(employee__username=options['employee'])

        migrated, skipped, no_action = 0, 0, 0

        for lead in leads:
            if Activity.objects.filter(lead=lead, source=Activity.Source.MIGRATED).exists():
                skipped += 1
                continue

            created_pending = []

            with transaction.atomic():
                for fu in lead.followups.all():
                    if fu.is_done:
                        if not dry:
                            Activity.objects.create(
                                lead=lead,
                                owner=fu.employee,
                                activity_type=Activity.Type.CALL,
                                status=Activity.Status.COMPLETED,
                                scheduled_at=fu.datetime,
                                completed_at=fu.datetime,
                                notes=fu.note,
                                source=Activity.Source.MIGRATED,
                            )
                    else:
                        if dry:
                            created_pending.append(fu.datetime)
                        else:
                            act = Activity.objects.create(
                                lead=lead,
                                owner=fu.employee,
                                activity_type=Activity.Type.CALL,
                                status=Activity.Status.PENDING,
                                scheduled_at=fu.datetime,
                                notes=fu.note,
                                source=Activity.Source.MIGRATED,
                            )
                            created_pending.append(act)

                for task in lead.tasks.filter(is_completed=False):
                    scheduled = timezone.make_aware(
                        timezone.datetime.combine(task.due_date, timezone.datetime.min.time())
                    ) if task.due_date else timezone.now()
                    if dry:
                        created_pending.append(scheduled)
                    else:
                        act = Activity.objects.create(
                            lead=lead,
                            owner=task.employee,
                            activity_type=Activity.Type.TASK,
                            status=Activity.Status.PENDING,
                            scheduled_at=scheduled,
                            notes=task.title,
                            source=Activity.Source.MIGRATED,
                        )
                        created_pending.append(act)

                is_active = lead.lifecycle_state == Lead.LifecycleState.ACTIVE
                if not dry and is_active and created_pending:
                    earliest = min(created_pending, key=lambda a: a.scheduled_at)
                    lead.next_activity = earliest
                    lead.save(update_fields=['next_activity', 'updated_at'])

            migrated += 1
            if is_active and not created_pending:
                no_action += 1

        prefix = '[dry-run] ' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Leads migrated: {migrated}, already migrated (skipped): {skipped}, '
            f'left with no next action (surfaced, not faked): {no_action}'
        ))
