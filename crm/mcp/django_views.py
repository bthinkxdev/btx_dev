"""Public MCP endpoints, served by the main site (see crm.mcp.bridge)."""
from __future__ import annotations

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from crm.core.middleware import client_ip

from . import bridge

REGISTER_PER_HOUR = 30


@csrf_exempt            # bearer-token / OAuth endpoints: browsers' cookies are never involved
@never_cache
def gateway(request, alias_path=None):
    if request.path == '/register' and request.method == 'POST':
        key = f'mcp-register:{client_ip(request)}'
        cache.add(key, 0, 3600)
        if cache.incr(key) > REGISTER_PER_HOUR:
            return JsonResponse({'error': 'too_many_requests'}, status=429)
    return bridge.handle(request, alias_path)
