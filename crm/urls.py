"""BThinkX Business OS URL map. Each module folder owns its own urls.py."""
from importlib import import_module

app_name = 'crm'

MODULES = ['auth', 'common', 'leads', 'sales', 'clients', 'quotations', 'payments', 'billing', 'accounting', 'contracts', 'deliverables', 'worktime', 'performance', 'projects', 'expenses', 'hr', 'careers', 'webinars', 'works', 'reports', 'desk', 'chatbot', 'mcpconnect', 'administration']

urlpatterns = []
for _m in MODULES:
    urlpatterns += import_module(f'crm.modules.{_m}.urls').urlpatterns
