"""Per-request actor resolution for MCP tool calls.

The auth middleware (crm.mcp.auth) resolves the bearer token to a Django
User once per HTTP request and stores it here; every tool function reads it
back with current_user() instead of trusting a caller-supplied user id.
"""
from __future__ import annotations

from contextvars import ContextVar

_current_user: ContextVar = ContextVar('mcp_current_user', default=None)
_current_key: ContextVar = ContextVar('mcp_current_key', default=None)


class NotAuthenticated(Exception):
    pass


def set_actor(user, api_key) -> None:
    _current_user.set(user)
    _current_key.set(api_key)


def current_user():
    user = _current_user.get()
    if user is None:
        raise NotAuthenticated('No authenticated MCP caller for this request.')
    return user


def current_key():
    return _current_key.get()
