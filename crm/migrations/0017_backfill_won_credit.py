"""Leads that were won before credit was recorded: the owner at the time of this migration closed it and gets the credit.
That is the best the history allows, and it matches how the numbers have been read until now."""
from django.db import migrations


def forwards(apps, schema_editor):
    Lead = apps.get_model('crm', 'Lead')
    Lead.objects.filter(stage='won', owner__isnull=False, credited_to__isnull=True).update(
        credited_to=models_f('owner_id'), closed_by=models_f('owner_id'))


def models_f(name):
    from django.db.models import F
    return F(name)


class Migration(migrations.Migration):
    dependencies = [('crm', '0016_performance_profile')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
