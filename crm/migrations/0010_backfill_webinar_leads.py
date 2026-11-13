from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    """Every registration made before the webinar section existed gets its follow-up record."""
    Registration = apps.get_model('pages', 'WebinarRegistration')
    WebinarLead = apps.get_model('crm', 'WebinarLead')
    have = set(WebinarLead.objects.values_list('registration_id', flat=True))
    WebinarLead.objects.bulk_create([
        WebinarLead(registration_id=r.pk, created_at=r.created_at or timezone.now())
        for r in Registration.objects.exclude(pk__in=have)])


class Migration(migrations.Migration):
    dependencies = [('crm', '0009_webinar_leads')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
