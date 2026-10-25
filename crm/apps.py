from importlib import import_module

from django.apps import AppConfig

# Modules that register approval handlers at import time. Loaded in ready() so a decision
# made from any entry point (web, job runner, Celery) always finds its handler.
HANDLER_MODULES = [
    'crm.modules.leads.services', 'crm.modules.leads.views', 'crm.modules.quotations.services',
    'crm.modules.payments.services', 'crm.modules.billing.services', 'crm.modules.accounting.services',
    'crm.modules.contracts.services', 'crm.modules.projects.services', 'crm.modules.expenses.services',
]


class CrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crm'
    verbose_name = 'BThinkX Business OS'

    def ready(self):
        from . import signals  # noqa: F401
        for name in HANDLER_MODULES:
            import_module(name)
