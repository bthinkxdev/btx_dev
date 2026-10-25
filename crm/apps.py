from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crm'
    verbose_name = 'BThinkX Business OS'

    def ready(self):
        from . import signals  # noqa: F401
