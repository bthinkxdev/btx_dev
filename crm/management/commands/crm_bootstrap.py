from django.core.management.base import BaseCommand

from crm.core import bootstrap


class Command(BaseCommand):
    help = 'Seed roles, chart of accounts, numbering series and defaults (idempotent).'

    def handle(self, *args, **opts):
        out = bootstrap.run()
        self.stdout.write(self.style.SUCCESS(f'Bootstrap complete: {out}'))
