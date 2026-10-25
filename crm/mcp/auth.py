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
    """Wraps the FastMCP Streamable HTTP app: every request must present
    `Authorization: Bearer <key>` for a non-revoked McpApiKey, resolved to its
    owning service-account User via crm.mcp.context.set_actor()."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers') or [])
        raw = headers.get(b'authorization', b'').decode('latin-1')
        token = raw[7:] if raw.lower().startswith('bearer ') else ''
        key = resolve_token(token) if token else None
        if key is None:
            await _unauthorized(send)
            return
        from . import context
        context.set_actor(key.owner, key)
        await self.app(scope, receive, send)


async def _unauthorized(send):
    body = b'{"error":"invalid or missing bearer token"}'
    await send({'type': 'http.response.start', 'status': 401,
                'headers': [(b'content-type', b'application/json'), (b'www-authenticate', b'Bearer')]})
    await send({'type': 'http.response.body', 'body': body})
