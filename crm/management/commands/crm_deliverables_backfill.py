"""Give clients whose contracts were already active before monthly deliverables existed a delivery plan.

    python manage.py crm_deliverables_backfill            # dry run: lists what would be created
    python manage.py crm_deliverables_backfill --apply    # creates the plans and the next 30 days of deliverables

Plans are built from each service's standard monthly plan. A service with only a free-text description gets items that
are switched off until somebody chooses the days, so nothing is scheduled by guesswork. Safe to run more than once: a
contract that already has a plan is skipped.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from crm.core import india
from crm.models import Contract, DeliveryPlan
from crm.models.catalog import RECURRING_MODELS
from crm.modules.deliverables import services


class Command(BaseCommand):
    help = 'Create delivery plans for contracts that were active before the deliverables module (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, apply, **opts):
        todo, skipped = [], 0
        for c in Contract.objects.filter(status='active').select_related('client', 'owner').order_by('client__display_name'):
            if DeliveryPlan.objects.filter(contract=c).exists():
                skipped += 1
                continue
            lines = list(c.lines.select_related('service'))
            standard = sum(s.deliverable_items.count() for s in {ln.service for ln in lines if ln.service_id})
            text_only = any(ln.billing_model in RECURRING_MODELS and ln.service_id and not ln.service.deliverable_items.exists()
                            and (ln.service.deliverables or '').strip() for ln in lines)
            if not standard and not text_only:
                continue
            todo.append((c, standard, text_only))
        for c, standard, text_only in todo:
            owner = services._default_owner(c)
            note = f'{standard} standard item(s)' + (', plus switched-off items from the service description' if text_only else '')
            self.stdout.write(f'{c.client.display_name} · {c} · owner {owner.get_username()} · {note}')
            if apply:
                plan = services.create_plan(c, None)
                made = plan.deliverables.count() if plan else 0
                self.stdout.write(self.style.SUCCESS(f'    created, {made} deliverable(s) scheduled'))
        self.stdout.write(f'{len(todo)} contract(s) to set up, {skipped} already have a plan, as of {india.today():%d %b %Y}.')
        if todo and not apply:
            self.stdout.write(self.style.WARNING('Dry run. Add --apply to create them.'))
