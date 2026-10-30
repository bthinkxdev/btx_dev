"""OAuth 2.1 authorization server for the MCP endpoint (what claude.ai's "Connect" button speaks).

The MCP SDK supplies the protocol endpoints (discovery, /register, /authorize, /token, /revoke,
PKCE checks); this provider supplies storage and the human step. `authorize()` does not log anyone
in: it parks the request and sends the browser to the CRM's own sign-in (password + 2FA), where an
owner approves or denies it (crm.modules.mcpconnect). Tokens are random, stored hashed, short-lived
and always act as the Business Manager service account, so what Claude can do is exactly what that
role allows. The static keys from `manage.py mcp_issue_key` keep working through load_access_token.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, TokenError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from crm.core import crypto
from crm.core.bootstrap import MCP_SERVICE_ACCOUNT

SCOPE = 'mcp'
CODE_TTL = timedelta(minutes=5)
REQUEST_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)


def public_base_url() -> str:
    return getattr(settings, 'MCP_PUBLIC_BASE_URL', 'http://127.0.0.1:8800').rstrip('/')


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _service_account():
    try:
        return get_user_model().objects.get(username=MCP_SERVICE_ACCOUNT, is_active=True)
    except get_user_model().DoesNotExist:
        raise AuthorizeError('server_error', 'The Business Manager service account is missing. Run manage.py crm_bootstrap.')


# ---------------------------------------------------------------- synchronous core (ORM)

def save_client(info: OAuthClientInformationFull) -> None:
    from crm.models import McpOAuthClient
    McpOAuthClient.objects.update_or_create(client_id=info.client_id, defaults={'info': info.model_dump(mode='json')})


def find_client(client_id: str):
    from crm.models import McpOAuthClient
    row = McpOAuthClient.objects.filter(client_id=client_id).first()
    return OAuthClientInformationFull.model_validate(row.info) if row else None


def park_request(client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
    """Remember the authorization request; return the CRM page where an owner decides."""
    from crm.models import McpOAuthRequest
    _service_account()
    req = McpOAuthRequest.objects.create(
        client_id=client.client_id, expires_at=timezone.now() + REQUEST_TTL,
        params={'state': params.state, 'scopes': params.scopes or [SCOPE], 'code_challenge': params.code_challenge,
                'redirect_uri': str(params.redirect_uri),
                'redirect_uri_provided_explicitly': params.redirect_uri_provided_explicitly,
                'resource': params.resource or ''})
    return f'{public_base_url()}/crm/mcp/authorize/{req.pk}/'


def issue_code(req, approver) -> str:
    """Called by the CRM consent view once an owner approves. Returns the raw one-time code."""
    from crm.models import McpOAuthToken
    p = req.params
    raw = _new_secret()
    McpOAuthToken.objects.create(
        kind='code', token_hash=crypto.token_hash(raw), client_id=req.client_id, scopes=p['scopes'],
        resource=p.get('resource', ''), expires_at=timezone.now() + CODE_TTL, code_challenge=p['code_challenge'],
        redirect_uri=p['redirect_uri'], redirect_uri_provided_explicitly=p['redirect_uri_provided_explicitly'],
        owner=_service_account(), approved_by=approver)
    req.resolved_at = timezone.now()
    req.save(update_fields=['resolved_at'])
    return raw


def _issue_pair(owner, approved_by, client_id, scopes, resource, family=None) -> OAuthToken:
    from crm.models import McpOAuthToken
    now = timezone.now()
    access, refresh = _new_secret(), _new_secret()
    common = dict(client_id=client_id, scopes=list(scopes), resource=resource or '', owner=owner, approved_by=approved_by)
    if family is not None:
        common['family'] = family
    first = McpOAuthToken.objects.create(kind='access', token_hash=crypto.token_hash(access),
                                         expires_at=now + ACCESS_TTL, **common)
    McpOAuthToken.objects.create(kind='refresh', token_hash=crypto.token_hash(refresh), expires_at=now + REFRESH_TTL,
                                 **{**common, 'family': first.family})
    return OAuthToken(access_token=access, token_type='Bearer', expires_in=int(ACCESS_TTL.total_seconds()),
                      scope=' '.join(scopes), refresh_token=refresh)


def _row(kind: str, raw: str, client_id: str | None = None):
    from crm.models import McpOAuthToken
    if not raw or len(raw) > 200:
        return None
    qs = McpOAuthToken.objects.filter(kind=kind, token_hash=crypto.token_hash(raw)).select_related('owner')
    if client_id is not None:
        qs = qs.filter(client_id=client_id)
    row = qs.first()
    return row if row is not None and row.is_usable() else None


def load_code(client_id: str, raw: str):
    row = _row('code', raw, client_id)
    if row is None:
        return None
    return AuthorizationCode(code=raw, scopes=row.scopes, expires_at=row.expires_at.timestamp(), client_id=row.client_id,
                             code_challenge=row.code_challenge, redirect_uri=row.redirect_uri,
                             redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                             resource=row.resource or None)


def exchange_code(client_id: str, raw: str) -> OAuthToken:
    from crm.models import McpOAuthToken
    row = _row('code', raw, client_id)
    if row is None:
        raise TokenError('invalid_grant', 'Authorization code is invalid, expired or already used.')
    row.revoked_at = timezone.now()                      # one use only
    row.save(update_fields=['revoked_at'])
    return _issue_pair(row.owner, row.approved_by, client_id, row.scopes, row.resource)


def load_refresh(client_id: str, raw: str):
    row = _row('refresh', raw, client_id)
    if row is None:
        return None
    return RefreshToken(token=raw, client_id=row.client_id, scopes=row.scopes, expires_at=int(row.expires_at.timestamp()),
                        resource=row.resource or None)


def exchange_refresh(client_id: str, raw: str, scopes: list[str]) -> OAuthToken:
    from crm.models import McpOAuthToken
    row = _row('refresh', raw, client_id)
    if row is None:
        raise TokenError('invalid_grant', 'Refresh token is invalid or expired.')
    if scopes and not set(scopes) <= set(row.scopes):
        raise TokenError('invalid_scope', 'Cannot widen the scope of a refresh token.')
    now = timezone.now()
    McpOAuthToken.objects.filter(family=row.family, revoked_at__isnull=True).update(revoked_at=now)   # rotate
    return _issue_pair(row.owner, row.approved_by, client_id, scopes or row.scopes, row.resource)


def lookup_access(raw: str):
    """The TokenRow for a live OAuth access token, else None. Touches last_used_at."""
    row = _row('access', raw)
    if row is not None:
        row.last_used_at = timezone.now()
        row.save(update_fields=['last_used_at'])
    return row


def revoke_family(raw: str) -> None:
    from crm.models import McpOAuthToken
    row = McpOAuthToken.objects.filter(token_hash=crypto.token_hash(raw), kind__in=('access', 'refresh')).first()
    if row is not None:
        McpOAuthToken.objects.filter(family=row.family, revoked_at__isnull=True).update(revoked_at=timezone.now())


def resolve_actor(raw: str):
    """(user, api_key) behind a bearer token: an OAuth access token or a static key; None if neither."""
    from .auth import resolve_token
    row = lookup_access(raw)
    if row is not None:
        return row.owner, None
    key = resolve_token(raw)
    return (key.owner, key) if key is not None else None


def verify(raw: str) -> AccessToken | None:
    row = lookup_access(raw)
    if row is not None:
        return AccessToken(token=raw, client_id=row.client_id, scopes=row.scopes,
                           expires_at=int(row.expires_at.timestamp()), resource=row.resource or None)
    from .auth import resolve_token
    key = resolve_token(raw)
    if key is not None:
        return AccessToken(token=raw, client_id=f'static-key-{key.pk}', scopes=[SCOPE], expires_at=None)
    return None


# ---------------------------------------------------------------- async provider the SDK calls

class BtxOAuthProvider:
    async def get_client(self, client_id):
        return await sync_to_async(find_client, thread_sensitive=True)(client_id)

    async def register_client(self, client_info):
        await sync_to_async(save_client, thread_sensitive=True)(client_info)

    async def authorize(self, client, params):
        return await sync_to_async(park_request, thread_sensitive=True)(client, params)

    async def load_authorization_code(self, client, authorization_code):
        return await sync_to_async(load_code, thread_sensitive=True)(client.client_id, authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        return await sync_to_async(exchange_code, thread_sensitive=True)(client.client_id, authorization_code.code)

    async def load_refresh_token(self, client, refresh_token):
        return await sync_to_async(load_refresh, thread_sensitive=True)(client.client_id, refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await sync_to_async(exchange_refresh, thread_sensitive=True)(client.client_id, refresh_token.token, scopes)

    async def load_access_token(self, token):
        return await sync_to_async(verify, thread_sensitive=True)(token)

    async def revoke_token(self, token):
        await sync_to_async(revoke_family, thread_sensitive=True)(token.token)
