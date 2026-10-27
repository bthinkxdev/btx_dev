"""Bring the stored system roles in line with the role templates in crm/core/roles.py.

    python manage.py crm_sync_roles            # dry run: shows what would change
    python manage.py crm_sync_roles --apply    # overwrite grants of system roles

Bootstrap only creates missing roles, so changed templates never reach an existing database
until this is run. Custom roles (is_system=False) are never touched.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from crm.core.roles import ROLE_TEMPLATES
from crm.models import Role


class Command(BaseCommand):
    help = 'Sync system roles with the code templates (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **o):
        for tpl in ROLE_TEMPLATES:
            role = Role.objects.filter(code=tpl['code']).first()
            if role is None:
                self.stdout.write(f'{tpl["code"]}: missing (run crm_bootstrap)')
                continue
            if not role.is_system:
                self.stdout.write(f'{tpl["code"]}: custom role, skipped')
                continue
            diffs = []
            for module in sorted(set(role.grants or {}) | set(tpl['grants'])):
                old, new = (role.grants or {}).get(module, {}), tpl['grants'].get(module, {})
                if old != new:
                    diffs.append(f'    {module}: {old or "-"} -> {new or "-"}')
            if sorted(role.field_groups or []) != sorted(tpl['field_groups']):
                diffs.append(f'    field groups: {role.field_groups} -> {tpl["field_groups"]}')
            if not diffs:
                self.stdout.write(f'{tpl["code"]}: up to date')
                continue
            self.stdout.write(self.style.WARNING(f'{tpl["code"]}: {len(diffs)} change(s)'))
            self.stdout.write('\n'.join(diffs))
            if o['apply']:
                role.grants = tpl['grants']
                role.field_groups = tpl['field_groups']
                role.save(update_fields=['grants', 'field_groups'])
        self.stdout.write(self.style.SUCCESS('Applied.' if o['apply'] else 'Dry run only. Re-run with --apply to write.'))
