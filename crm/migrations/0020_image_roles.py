"""Roles for client photos and editing.

Roles are seeded once, so an existing database does not pick up new grants from code. This adds the `images` grants to the
existing roles that need them, only where a role has none (anything an administrator set by hand in the permission matrix is
left alone), and creates the two new roles if they do not exist. Nothing is removed."""
from django.db import migrations

ALL = ['view', 'create', 'edit', 'archive', 'approve', 'export', 'assign', 'cancel', 'refund', 'override',
       'reveal_sensitive', 'import', 'configure']
GRANTS = {
    'owner': {a: 'company' for a in ALL},
    'sales_manager': {'view': 'team', 'create': 'team', 'edit': 'team', 'assign': 'team', 'approve': 'team',
                      'export': 'team', 'configure': 'team'},
    'sales_executive': {'view': 'own', 'create': 'own', 'edit': 'own', 'assign': 'own', 'approve': 'own'},
    'hr_manager': {'view': 'company'},
    'auditor': {'view': 'company'},
}
NEW_ROLES = [
    ('image_editor', 'Image Editor', 'Edits client photos assigned to them: downloads the original, uploads edited versions, sends for review.',
     {'images': {'view': 'own', 'edit': 'own'}, 'leave': {'view': 'own', 'create': 'own'},
      'attendance': {'view': 'own', 'create': 'own'}, 'expenses': {'view': 'own', 'create': 'own'}}),
    ('image_client', 'Client (photo portal)', 'A customer login. Sees only their own company photos: uploads, follows progress, approves or asks for changes.',
     {'images': {'view': 'own', 'create': 'own'}}),
]


def forwards(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for code, grants in GRANTS.items():
        for role in Role.objects.filter(code=code):
            current = dict(role.grants or {})
            if 'images' not in current:
                current['images'] = grants
                role.grants = current
                role.save(update_fields=['grants'])
    for code, name, description, grants in NEW_ROLES:
        if not Role.objects.filter(code=code).exists() and not Role.objects.filter(name=name).exists():
            Role.objects.create(code=code, name=name, description=description, grants=grants, field_groups=[],
                                home='crm:image_home', is_system=True)


def backwards(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for role in Role.objects.filter(code__in=list(GRANTS)):
        current = dict(role.grants or {})
        if current.pop('images', None) is not None:
            role.grants = current
            role.save(update_fields=['grants'])
    for code, *_ in NEW_ROLES:
        role = Role.objects.filter(code=code).first()
        if role is not None and not role.profiles.exists():
            role.delete()


class Migration(migrations.Migration):
    dependencies = [('crm', '0019_image_workflow')]
    operations = [migrations.RunPython(forwards, backwards)]
