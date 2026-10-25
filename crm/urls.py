"""BThinkX Business OS URL map. Each module folder owns its own urls.py."""
from importlib import import_module

from django.urls import URLPattern

app_name = 'crm'

MODULES = ['auth', 'common', 'leads', 'sales', 'clients', 'quotations', 'payments', 'billing', 'accounting', 'contracts', 'projects', 'expenses']

urlpatterns = []
for _m in MODULES:
    urlpatterns += import_module(f'crm.modules.{_m}.urls').urlpatterns

from .modules import placeholder  # noqa: E402

urlpatterns += placeholder.patterns({p.name for p in urlpatterns if isinstance(p, URLPattern)})
