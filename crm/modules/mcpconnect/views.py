"""Consent step of the MCP connector sign-in. claude.ai sends the owner here (via the MCP server's
/authorize); they sign in with the normal CRM login (password + 2FA), then approve or deny."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from crm.core import audit, permissions
from crm.core.views import staff_required
from crm.models import McpOAuthClient, McpOAuthRequest


def _redirect_back(redirect_uri, **query):
    parts = urlparse(redirect_uri)
    pairs = parse_qsl(parts.query) + [(k, v) for k, v in query.items() if v is not None]
    return redirect(urlunparse(parts._replace(query=urlencode(pairs))))


def _finish(req, **query):
    req.resolved_at = req.resolved_at or timezone.now()
    req.save(update_fields=['resolved_at'])
    return _redirect_back(req.params['redirect_uri'], state=req.params.get('state'), **query)


@never_cache
@staff_required
@require_http_methods(['GET', 'POST'])
def authorize(request, pk):
    if not permissions.has_role(request.user, 'owner'):
        audit.security_event('mcp_connect_forbidden', user=request.user, request=request, detail=str(pk))
        return HttpResponseForbidden('Only an owner can connect an AI assistant to the Business OS.')
    req = get_object_or_404(McpOAuthRequest, pk=pk)
    client = McpOAuthClient.objects.filter(client_id=req.client_id).first()
    if not req.is_open() or client is None:
        return render(request, 'crm/mcp/authorize.html', {'expired': True}, status=410)
    if request.method == 'POST':
        if request.POST.get('decision') == 'approve':
            from crm.mcp import oauth      # imported here so the site still starts if the mcp package is missing
            code = oauth.issue_code(req, request.user)
            audit.log('mcp.connect_approved', actor=request.user, object_type='McpOAuthClient', object_id=client.client_id,
                      object_repr=str(client))
            return _finish(req, code=code)
        audit.log('mcp.connect_denied', actor=request.user, object_type='McpOAuthClient', object_id=client.client_id,
                  object_repr=str(client))
        return _finish(req, error='access_denied', error_description='The owner declined the connection.')
    return render(request, 'crm/mcp/authorize.html', {'client': client, 'req': req})
