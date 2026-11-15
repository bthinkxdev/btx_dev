"""Give the existing system roles access to the new Monthly deliverables module.

Roles are seeded once, so a database that already has them does not pick up the new grants from code. This adds
`deliverables` to a role only when the role does not already have it, so anything an administrator has set by hand
in the permission matrix is left alone."""
from django.db import migrations

ALL = ['view', 'create', 'edit', 'archive', 'approve', 'export', 'assign', 'cancel', 'refund', 'override',
       'reveal_sensitive', 'import', 'configure']
GRANTS = {
    'owner': {a: 'company' for a in ALL},
    'sales_manager': {'view': 'team', 'create': 'team', 'edit': 'team', 'assign': 'team', 'approve': 'team',
                      'configure': 'team', 'export': 'team'},
    'sales_executive': {'view': 'own', 'create': 'own', 'edit': 'own'},
    'auditor': {'view': 'company'},
}


def add_grants(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for code, grants in GRANTS.items():
        for role in Role.objects.filter(code=code):
            current = dict(role.grants or {})
            if 'deliverables' not in current:
                current['deliverables'] = grants
                role.grants = current
                role.save(update_fields=['grants'])


def remove_grants(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for role in Role.objects.filter(code__in=list(GRANTS)):
        current = dict(role.grants or {})
        if current.pop('deliverables', None) is not None:
            role.grants = current
            role.save(update_fields=['grants'])


class Migration(migrations.Migration):
    dependencies = [('crm', '0011_deliverables')]
    operations = [migrations.RunPython(add_grants, remove_grants)]
