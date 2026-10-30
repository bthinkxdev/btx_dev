"""Builds the FastMCP app: registers every tool/resource module and wraps the
Streamable HTTP app with bearer-token auth. Import this only after django.setup()
has run (the management command that serves it, or a test, does that for you)."""
from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .auth import BearerAuthASGIMiddleware
from .oauth import SCOPE, BtxOAuthProvider, public_base_url
from .resources import register as register_resources
from .tools import (accounting, billing, clients, contracts, expenses, hr, leads, overview, payments, projects,
                    quotations)

TOOL_MODULES = (leads, clients, quotations, billing, payments, contracts, projects, expenses, hr, accounting,
                overview)


def _transport_security() -> TransportSecuritySettings:
    """Accept the public hostname nginx forwards (the SDK's default only allows localhost), plus loopback."""
    host = urlparse(public_base_url()).netloc
    origin = public_base_url()
    if origin.startswith('https://'):
        # Behind TLS every request needs a bearer token, which is what rebinding protection stands in for;
        # claude.ai's own Origin must not be refused.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, '127.0.0.1:*', 'localhost:*'],
        allowed_origins=[origin, 'http://127.0.0.1:*', 'http://localhost:*'])


def build_mcp() -> FastMCP:
    base = public_base_url()
    mcp = FastMCP(
        name='bthinkx-business-manager',
        auth_server_provider=BtxOAuthProvider(),
        auth=AuthSettings(
            issuer_url=base, resource_server_url=f'{base}{settings.MCP_HTTP_PATH}', validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True)),
        transport_security=_transport_security(),
        streamable_http_path=settings.MCP_HTTP_PATH,
        stateless_http=True,     # each request stands alone, so any worker can answer it
        json_response=True,      # plain JSON replies, nothing to stream through a proxy
        instructions='Create, read and update business records as the Business Manager role. '
                     'No delete/approve/configure exists here. Read glossary://business-manuals '
                     'for the full list of role manuals, and call business_overview first for '
                     'a snapshot of how the business is doing.',
    )
    for module in TOOL_MODULES:
        module.register(mcp)
    register_resources(mcp)
    return mcp


mcp = build_mcp()
inner = mcp.streamable_http_app()
app = BearerAuthASGIMiddleware(inner)
