"""Small shared helpers for MCP tool wrappers."""
from __future__ import annotations

from asgiref.sync import sync_to_async

from crm.core import permissions


class NotFound(Exception):
    pass


def sync_tool(mcp):
    """Use in place of @mcp.tool() on every tool function here: FastMCP calls a plain
    `def` tool directly inside its async event loop, which makes Django's ORM raise
    SynchronousOnlyOperation. sync_to_async runs the function in a worker thread instead,
    propagating contextvars (crm.mcp.context's current actor) into it correctly."""
    def decorator(fn):
        return mcp.tool()(sync_to_async(fn, thread_sensitive=True))
    return decorator


def get_scoped(user, model, pk, module: str, action: str = 'view', owner_fields=None, extra_q=None, queryset=None):
    """Request-free counterpart to crm.core.views.get_scoped, for MCP tools."""
    permissions.require(user, module, action)
    qs = queryset if queryset is not None else model._default_manager.all()
    qs = permissions.scoped(qs, user, module, action, owner_fields, extra_q)
    try:
        return qs.get(pk=pk)
    except (model.DoesNotExist, ValueError, TypeError):
        raise NotFound(f'{model.__name__} {pk} not found or not in scope.')
