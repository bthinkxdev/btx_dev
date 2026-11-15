"""Two settings that changed with working-time tracking.

* People are now signed out after 12 hours without activity (it was 30 minutes). Every company already has the old
  value stored, so it is moved to 720 minutes here.
* Sales managers can see their own team's working time. This only widens a sales manager role that still has the
  original attendance grants, so anything an administrator edited by hand is left alone."""
from django.db import migrations

OLD_ATTENDANCE = {'view': 'own', 'create': 'own'}
NEW_ATTENDANCE = {'view': 'team', 'create': 'own', 'export': 'team'}


def forwards(apps, schema_editor):
    CompanySettings = apps.get_model('crm', 'CompanySettings')
    CompanySettings.objects.filter(idle_timeout_minutes__lt=720).update(idle_timeout_minutes=720)
    Role = apps.get_model('crm', 'Role')
    for role in Role.objects.filter(code='sales_manager'):
        grants = dict(role.grants or {})
        if grants.get('attendance') == OLD_ATTENDANCE:
            grants['attendance'] = NEW_ATTENDANCE
            role.grants = grants
            role.save(update_fields=['grants'])


def backwards(apps, schema_editor):
    Role = apps.get_model('crm', 'Role')
    for role in Role.objects.filter(code='sales_manager'):
        grants = dict(role.grants or {})
        if grants.get('attendance') == NEW_ATTENDANCE:
            grants['attendance'] = OLD_ATTENDANCE
            role.grants = grants
            role.save(update_fields=['grants'])


class Migration(migrations.Migration):
    dependencies = [('crm', '0014_worktime_and_handled_by')]
    operations = [migrations.RunPython(forwards, backwards)]
