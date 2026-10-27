"""Show what each CRM user can actually reach (effective scope per module, dashboards, sidebar).

    python manage.py crm_user_access                 # every user, summary
    python manage.py crm_user_access lois jenni -v 2 # chosen users, with per-module scopes
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from crm.core import permissions
from crm.core.nav import build_nav
from crm.modules.reports import dashboards


class Command(BaseCommand):
    help = 'List effective roles, dashboards and sidebar entries per user.'

    def add_arguments(self, parser):
        parser.add_argument('usernames', nargs='*')

    def handle(self, *args, **o):
        User = get_user_model()
        users = User.objects.filter(username__in=o['usernames']) if o['usernames'] else User.objects.all()
        for u in users.order_by('username'):
            permissions.clear_cache(u)
            roles = sorted(permissions._load(u)['roles'])
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'{u.username}  superuser={u.is_superuser}  roles={roles}'))
            self.stdout.write('  dashboards: ' + (', '.join(k for k in dashboards.DASHBOARDS if dashboards.can_open(u, k)) or '-'))
            sidebar = [f'{g["label"]}: ' + ', '.join(i['label'] for i in g['items']) for g in build_nav(u, '')]
            for line in sidebar:
                self.stdout.write('  sidebar  ' + line)
            if o['verbosity'] >= 2:
                grants = permissions._load(u)['grants']
                for module in permissions.MODULES:
                    acts = grants.get(module)
                    if acts:
                        self.stdout.write(f'  {module:14} ' + ', '.join(f'{a}={s}' for a, s in sorted(acts.items())))
