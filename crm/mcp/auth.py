"""Bearer-token auth for the MCP HTTP server, mirroring PaymentLink's token_hash pattern
(crm/modules/payments/services.py) rather than FastMCP's OAuth-shaped auth abstractions -
there is no authorization server here, just admin-issued static keys."""
from __future__ import annotations

from django.utils import timezone

from crm.core import crypto

TOKEN_PURPOSE = 'mcpkey'


def mint_key(owner, *, label: str, created_by) -> tuple:
    """Returns (McpApiKey, raw_token). The raw token is never stored - only its hash."""
    from crm.models import McpApiKey
    nonce = crypto.new_nonce()
    raw = crypto.token_from_nonce(TOKEN_PURPOSE, nonce)
    key = McpApiKey.objects.create(owner=owner, label=label, token_nonce=nonce,
                                   token_hash=crypto.token_hash(raw), created_by=created_by)
    return key, raw


def resolve_token(raw_token: str):
    """Returns the McpApiKey for a presented bearer token, or None if missing/revoked."""
    from crm.models import McpApiKey
    if not raw_token or len(raw_token) > 200:
        return None
    key = (McpApiKey.objects.filter(token_hash=crypto.token_hash(raw_token), revoked_at__isnull=True)
           .select_related('owner').first())
    if key is not None:
        key.last_used_at = timezone.now()
        key.save(update_fields=['last_used_at'])
    return key


class BearerAuthASGIMiddleware:
    """Wraps the FastMCP app. A presented bearer token (OAuth access token or static key) is
    resolved to its service-account User via crm.mcp.context.set_actor(). It never rejects a
    request itself: the SDK's own auth layer answers /mcp without a valid token with a 401
    carrying the OAuth discovery hint, and leaves the OAuth endpoints (/register, /authorize,
    /token, /.well-known/*) reachable, which claude.ai needs before it has any token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            from asgiref.sync import sync_to_async
            from . import context, oauth
            raw = dict(scope.get('headers') or []).get(b'authorization', b'').decode('latin-1')
            token = raw[7:].strip() if raw.lower().startswith('bearer ') else ''
            actor = await sync_to_async(oauth.resolve_actor, thread_sensitive=True)(token) if token else None
            if actor is not None:
                context.set_actor(*actor)
        await self.app(scope, receive, send)
