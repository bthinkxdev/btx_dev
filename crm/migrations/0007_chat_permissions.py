"""Give the owner and sales-manager roles access to website chats and the AI sales analyst.

Roles live in the database and are never overwritten after bootstrap, so a new module needs its
grants added here. Only these two roles get it; everyone else stays without access.
"""
from django.db import migrations

ALL_ACTIONS = ['view', 'create', 'edit', 'archive', 'approve', 'export', 'assign', 'cancel', 'refund',
               'override', 'reveal_sensitive', 'import', 'configure']


def grant(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for code, actions in (('owner', ALL_ACTIONS), ('sales_manager', ['view'])):
        for role in Role.objects.filter(code=code):
            grants = dict(role.grants or {})
            for module in ('chats', 'ai_analyst'):
                grants.setdefault(module, {a: 'company' for a in actions})
            role.grants = grants
            role.save(update_fields=['grants'])


def revoke(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for role in Role.objects.filter(code__in=['owner', 'sales_manager']):
        grants = dict(role.grants or {})
        grants.pop('chats', None)
        grants.pop('ai_analyst', None)
        role.grants = grants
        role.save(update_fields=['grants'])


class Migration(migrations.Migration):
    dependencies = [('crm', '0006_chatbot')]
    operations = [migrations.RunPython(grant, revoke)]
