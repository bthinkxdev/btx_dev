"""Builds the FastMCP app: registers every tool/resource module and wraps the
Streamable HTTP app with bearer-token auth. Import this only after django.setup()
has run (the management command that serves it, or a test, does that for you)."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .auth import BearerAuthASGIMiddleware
from .resources import register as register_resources
from .tools import (accounting, billing, clients, contracts, expenses, hr, leads, overview, payments, projects,
                    quotations)

TOOL_MODULES = (leads, clients, quotations, billing, payments, contracts, projects, expenses, hr, accounting,
                overview)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name='bthinkx-business-manager',
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
app = BearerAuthASGIMiddleware(mcp.streamable_http_app())
