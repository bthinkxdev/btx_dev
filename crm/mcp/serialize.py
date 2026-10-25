"""Turns model instances/querysets into plain, JSON-safe dicts for MCP tool returns."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def _value(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if hasattr(v, 'pk'):
        return {'id': v.pk, 'label': str(v)}
    return v


def row(obj, fields: tuple[str, ...]) -> dict:
    out = {'id': obj.pk}
    for f in fields:
        out[f] = _value(getattr(obj, f, None))
    return out


def rows(qs, fields: tuple[str, ...], limit: int = 100) -> list[dict]:
    return [row(o, fields) for o in qs[:limit]]


def deep(value):
    """Recursively converts Decimals/dates/model instances inside arbitrary nested
    dict/list structures (e.g. the accounting report functions' return values) into
    JSON-safe plain Python."""
    if isinstance(value, dict):
        return {k: deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep(v) for v in value]
    return _value(value)
