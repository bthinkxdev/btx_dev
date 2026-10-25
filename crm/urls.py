"""BThinkX Business OS URL map. Each module folder owns its own urls.py."""
from importlib import import_module

app_name = 'crm'

MODULES = ['auth', 'common', 'leads', 'sales', 'clients', 'quotations', 'payments', 'billing', 'accounting', 'contracts', 'projects', 'expenses', 'hr', 'reports', 'administration']

urlpatterns = []
for _m in MODULES:
    urlpatterns += import_module(f'crm.modules.{_m}.urls').urlpatterns
