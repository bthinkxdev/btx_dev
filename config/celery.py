import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Optional beat schedule (enable by setting CELERY_ENABLE_BEAT=1).
if os.environ.get('CELERY_ENABLE_BEAT', '').strip().lower() in ('1', 'true', 'yes'):
    app.conf.beat_schedule = {
        'crm.whatsapp_followups_every_5m': {
            'task': 'crm.tasks.check_whatsapp_followups',
            'schedule': 300.0,
        }
    }

